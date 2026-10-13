"""Validate the actual sanitized ZIP and its HTTP assets in a private temp runtime."""
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

REQUIRED_ASSETS = ('api-client.js', 'presentation.js', 'ui-components.js', 'ui-tokens.css',
    'workbench.js', 'workbench.css', 'review-workspace.js', 'review-workspace.css',
    'production-settings.js', 'content-workspace.js', 'publication-workspace.js')


def isolated_paths(root):
    runtime = root / 'runtime'
    return {'FACTORY_RUNTIME_HOME': str(runtime), 'FACTORY_ENV_FILE': str(runtime / '.env'),
            'FACTORY_DATA_DIR': str(runtime / 'data'), 'FACTORY_DB_PATH': str(runtime / 'data/factory.db')}


class InteractionDistributionTests(unittest.TestCase):
    def test_audit_covers_browser_javascript_and_css_secrets(self):
        from distribute_skill import audit_distribution
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in [('api-client.js', "const API_KEY = 'never-distribute-this-key';"),
                                  ('ui-tokens.css', '/* Bearer abc.def.ghi-secret */')]:
                with self.subTest(name=name):
                    target = root / name
                    target.write_text(content, encoding='utf-8')
                    try:
                        findings = audit_distribution(root)['findings']
                        self.assertTrue(any(f['path'] == name for f in findings))
                    finally:
                        target.unlink()

    def test_missing_required_asset_keeps_previous_skill_and_runtime_bytes(self):
        import distribute_skill
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            distribute_skill._populate_distribution(ROOT, source)
            values = isolated_paths(root)
            runtime = root / 'runtime'
            runtime.mkdir()
            (runtime / '.env').write_text('TEST_MARKER=keep-runtime\n', encoding='utf-8')
            (runtime / 'state.bin').write_bytes(b'old-state-exact-bytes')
            before_runtime = {p.relative_to(runtime).as_posix(): p.read_bytes() for p in runtime.rglob('*') if p.is_file()}
            with patch.dict(os.environ, values, clear=False):
                installed = distribute_skill.build_distribution(source, root / 'installed')
                before = {p.relative_to(installed).as_posix(): p.read_bytes() for p in installed.rglob('*') if p.is_file()}
                for name in REQUIRED_ASSETS:
                    target = source / 'scripts/management_ui' / name
                    payload = target.read_bytes()
                    target.unlink()
                    try:
                        with self.subTest(name=name), self.assertRaisesRegex(ValueError, re.escape(name)):
                            distribute_skill.build_distribution(source, root / 'installed')
                        self.assertEqual({p.relative_to(installed).as_posix(): p.read_bytes() for p in installed.rglob('*') if p.is_file()}, before)
                        self.assertEqual({p.relative_to(runtime).as_posix(): p.read_bytes() for p in runtime.rglob('*') if p.is_file()}, before_runtime)
                    finally:
                        target.write_bytes(payload)

    def test_actual_zip_assets_have_exact_bytes_load_order_and_http_mime(self):
        from distribute_skill import build_archive
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, isolated_paths(root), clear=False):
                archive = build_archive(ROOT, root / 'distribution')
            with zipfile.ZipFile(archive) as bundle:
                for stem in ('design-options', 'studio-concepts'):
                    for suffix in ('.html', '.js', '.css'):
                        self.assertNotIn('ai-video-factory/scripts/management_ui/' + stem + suffix, bundle.namelist())
                for name in REQUIRED_ASSETS:
                    relative = f'scripts/management_ui/{name}'
                    self.assertEqual(bundle.read('ai-video-factory/' + relative), (ROOT / relative).read_bytes())
                index = bundle.read('ai-video-factory/scripts/management_ui/index.html').decode('utf-8')
                script_order = re.findall(r'<script[^>]+src="/([^"?]+)', index)
                self.assertEqual(script_order[-1], 'app.js')
                for name in [name for name in REQUIRED_ASSETS if name.endswith('.js')]:
                    self.assertIn(name, script_order)
                    self.assertLess(script_order.index(name), script_order.index('app.js'))
                self.assertLess(script_order.index('api-client.js'), script_order.index('ui-components.js'))
                css_order = re.findall(r'<link[^>]+href="/([^"?]+)', index)
                self.assertLess(css_order.index('ui-tokens.css'), css_order.index('workbench.css'))
                self.assertLess(css_order.index('styles.css'), css_order.index('review-workspace.css'))
                references = re.findall(r'(?:src|href)="(/[^"?#]+(?:\?[^"#]*)?)"', index)
                expected = {path: hashlib.sha256(bundle.read('ai-video-factory/scripts/management_ui/' + path.split('?', 1)[0].lstrip('/'))).hexdigest()
                            for path in references if path.split('?', 1)[0].endswith(('.js', '.css'))}
                bundle.extractall(root / 'extracted')
            package = root / 'extracted/ai-video-factory'
            # This is a new process so all imports come from the ZIP, not this
            # test runner's existing worktree module cache. No workers start.
            harness = r'''
import hashlib,json,os,socket,sys,threading
from pathlib import Path
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
package=Path(sys.argv[1]);runtime=Path(sys.argv[2]);expected=json.loads(sys.argv[3])
runtime.mkdir(parents=True);(runtime/'.env').write_text('',encoding='utf-8')
sys.path.insert(0,str(package/'scripts'))
socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(AssertionError('external connector forbidden'))
from factory import db
from factory.management_api import ManagementService
from management_server import ConsoleHandler
database=db.conn()
service=ManagementService(database,package,worker_mode='external',data_dir=runtime/'data',env_path=runtime/'.env')
handler=type('PackagedHandler',(ConsoleHandler,),{'service':service,'service_lock':threading.RLock(),'runtime_mode':'desktop','auth_manager':None})
server=ThreadingHTTPServer(('127.0.0.1',0),handler)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
def local_connection(address,timeout=socket._GLOBAL_DEFAULT_TIMEOUT,source_address=None,**kwargs):
    assert tuple(address)==server.server_address,address
    sock=socket.socket();sock.settimeout(timeout);sock.connect(address);return sock
socket.create_connection=local_connection
results=[]
try:
    for path,digest in expected.items():
        client=HTTPConnection(*server.server_address,timeout=2)
        client.request('GET',path);response=client.getresponse();payload=response.read()
        assert response.status==200,(path,response.status)
        assert hashlib.sha256(payload).hexdigest()==digest,path
        mime=response.getheader('Content-Type').split(';')[0]
        assert mime in ({'text/css'} if path.split('?')[0].endswith('.css') else {'text/javascript','application/javascript'}),(path,mime)
        results.append({'path':path,'status':response.status,'content_type':mime,'sha256':digest});client.close()
    for path in ('/missing-interaction.js','/missing-interaction.css'):
        client=HTTPConnection(*server.server_address,timeout=2);client.request('GET',path)
        response=client.getresponse();payload=response.read()
        assert response.status==404,(path,response.status)
        assert b'<!doctype html>' not in payload.lower(),path
        client.close()
    print(json.dumps({'assets':results,'port':server.server_port,'workers':len(service._production_workers)}))
finally:
    server.shutdown();server.server_close();thread.join(2);database.close()
'''
            # Keep only OS process essentials, then set every runtime path
            # before imports. No inherited provider credentials reach this process.
            env = {key: value for key, value in os.environ.items() if key.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'COMSPEC'}}
            env.update(isolated_paths(root / 'host'))
            env.update(PYTHONDONTWRITEBYTECODE='1', FACTORY_RUNTIME='desktop', FACTORY_WORKER_MODE='external',
                       FACTORY_PROVIDER_MODE='replay', PROVIDER_RECONCILE_ENABLED='false')
            result = subprocess.run([sys.executable, '-X', 'utf8', '-c', harness, str(package),
                                     str(root / 'host/runtime'), json.dumps(expected)],
                                    cwd=package, env=env, capture_output=True, text=True, encoding='utf-8', timeout=35)
            self.assertEqual(result.returncode, 0, result.stderr[-3000:])
            proof = json.loads(result.stdout)
            self.assertEqual(proof['workers'], 0)
            self.assertNotEqual(proof['port'], 8765)
            self.assertEqual(len(proof['assets']), len(expected))
