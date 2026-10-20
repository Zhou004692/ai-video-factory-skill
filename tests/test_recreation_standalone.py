"""Recreation must work from a clean skill without the production runtime."""
import importlib.util
import importlib.metadata
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'scripts/recreation/bootstrap.py'

def module():
    spec = importlib.util.spec_from_file_location('recreation_bootstrap', ENTRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class StandaloneRecreationTests(unittest.TestCase):
    def test_inspect_initializes_without_venv_credentials_or_pipeline_state(self):
        self.assertTrue(ENTRY.is_file(), 'missing independent recreation initializer')
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); runtime = base / 'recreation'; old = base / 'old'
            old.mkdir(); sentinel = old / '.env'; sentinel.write_text('unchanged', encoding='utf-8')
            env = {**os.environ, 'FACTORY_RUNTIME_HOME': str(old), 'FACTORY_DB_PATH': str(old/'factory.db'), 'ARK_API_KEY': '', 'ANTHROPIC_API_KEY': ''}
            p = subprocess.run([sys.executable, '-I', str(ENTRY), '--runtime-root', str(runtime), '--json'], env=env, capture_output=True, text=True, encoding='utf-8')
            report = json.loads(p.stdout)
            self.assertEqual(report['stage'], 'inspect')
            self.assertEqual(report['python_dependencies'], [])
            self.assertEqual(report['paid_provider_calls'], 0)
            self.assertEqual(report['source_uploads'], 0)
            self.assertTrue((runtime/'runtime.json').is_file())
            self.assertFalse((runtime/'.venv').exists())
            self.assertFalse((runtime/'.env').exists())
            self.assertFalse((runtime/'data').exists())
            self.assertEqual(list(old.iterdir()), [sentinel])
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'unchanged')

    def test_check_only_has_no_side_effects_and_stage_envs_are_isolated(self):
        self.assertTrue(ENTRY.is_file(), 'missing independent recreation initializer')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'state'; mod = module()
            r = mod.bootstrap(stage='fit', runtime_root=root, check_only=True)
            self.assertFalse(root.exists())
            self.assertEqual(r['status'], 'dependencies_required')
            self.assertEqual(r['python_dependencies'], ['numpy>=1.26,<3', 'scipy>=1.11,<2'])
            self.assertIn('venvs', r['runtime_python'])
            self.assertNotIn('torch', ' '.join(r['python_dependencies']))
            with self.assertRaises(ValueError):
                mod.bootstrap(stage='unknown', runtime_root=root)

    def test_rejects_state_in_replaceable_skill(self):
        self.assertTrue(ENTRY.is_file(), 'missing independent recreation initializer')
        with self.assertRaisesRegex(ValueError, 'Skill'):
            module().bootstrap(runtime_root=ROOT/'recreation-state', check_only=True)

    def test_pose_install_uses_one_opencv_and_never_constructs_a_model(self):
        self.assertTrue(ENTRY.is_file(), 'missing independent recreation initializer')
        mod = module()
        with tempfile.TemporaryDirectory() as tmp, patch.object(mod, '_run', return_value={'returncode': 0, 'stdout': '', 'stderr': ''}) as run:
            mod.bootstrap(stage='pose', runtime_root=Path(tmp)/'runtime', install=True)
            commands = [c.args[0] for c in run.call_args_list]
            installs = [c for c in commands if 'install' in c]
            self.assertTrue(installs)
            self.assertEqual(mod.IMPORTS['pose'], ('numpy', 'cv2', 'onnxruntime'))
            self.assertEqual(mod.STAGES['pose'], ('numpy>=1.26,<3', 'opencv-python-headless>=4.10,<6', 'onnxruntime>=1.20,<2'))
            self.assertFalse(any('rtmlib' in ' '.join(c) for c in installs))
            self.assertFalse(any('opencv-contrib-python' in ' '.join(c) or 'torch' in ' '.join(c) for c in installs))

    def test_render_rejects_node_older_than_documented_runtime(self):
        mod = module()
        with patch.object(mod.shutil, 'which', return_value='node'), patch.object(mod, '_run', return_value={'returncode': 0, 'stdout': 'v18.20.0', 'stderr': ''}):
            self.assertFalse(mod._tool('node')['ok'])
        with patch.object(mod.shutil, 'which', return_value='node'), patch.object(mod, '_run', return_value={'returncode': 0, 'stdout': 'v22.0.0', 'stderr': ''}):
            self.assertTrue(mod._tool('node')['ok'])

    def test_package_probe_rejects_prerelease_extra_version_segments_and_duplicate_cv2(self):
        mod = module()
        versions = {'numpy': '2.5.4', 'opencv-python-headless': '5.0.0.93', 'onnxruntime': '1.31.0', 'tqdm': '4.70.1', 'rtmlib': '0.0.16'}
        def get_version(name):
            if name not in versions:
                raise importlib.metadata.PackageNotFoundError(name)
            return versions[name]
        def execute_probe(args, **kwargs):
            output = io.StringIO()
            with patch.object(importlib.metadata, 'version', side_effect=get_version), patch.object(importlib, 'import_module', return_value=None), contextlib.redirect_stdout(output):
                exec(args[-1], {})
            return {'returncode': 0, 'stdout': output.getvalue(), 'stderr': ''}
        with patch.object(mod, '_run', side_effect=execute_probe):
            self.assertTrue(mod._packages(Path(sys.executable), 'pose')['ok'])
            for version in ['1.31.0rc1', '1.31.0.post1', '2.0.0']:
                versions['onnxruntime'] = version
                self.assertFalse(mod._packages(Path(sys.executable), 'pose')['ok'], version)
            versions['onnxruntime'] = '1.31.0'
            versions['opencv-python'] = '5.0.0.93'
            self.assertFalse(mod._packages(Path(sys.executable), 'pose')['ok'])

    def test_reinitialization_preserves_projects_models_and_previous_stage(self):
        mod = module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)/'runtime';mod.bootstrap(runtime_root=home)
            project = home/'projects'/'demo';project.mkdir(parents=True)
            model = home/'models';model.mkdir()
            (project/'accepted.mp4').write_bytes(b'accepted-history')
            (model/'source.onnx').write_bytes(b'existing-weight')
            mod.bootstrap(stage='fit', runtime_root=home)
            state = json.loads((home/'runtime.json').read_text(encoding='utf-8'))
            self.assertEqual(set(state['stages']), {'inspect', 'fit'})
            self.assertEqual((project/'accepted.mp4').read_bytes(), b'accepted-history')
            self.assertEqual((model/'source.onnx').read_bytes(), b'existing-weight')

    def test_distribution_refuses_recreation_state_inside_replacement_target(self):
        sys.path.insert(0, str(ROOT/'scripts'))
        from distribute_skill import build_distribution
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'packed';target = output/'ai-video-factory'
            marker = target/'private-recreation'/'accepted.mp4';marker.parent.mkdir(parents=True);marker.write_bytes(b'keep')
            with patch.dict(os.environ, {'AI_VIDEO_RECREATION_HOME': str(marker.parent)}):
                with self.assertRaisesRegex(RuntimeError, 'AI_VIDEO_RECREATION_HOME'):
                    build_distribution(ROOT, output)
            self.assertEqual(marker.read_bytes(), b'keep')

    def test_clean_package_contains_and_runs_recreation_without_pipeline_imports(self):
        sys.path.insert(0, str(ROOT/'scripts'))
        from distribute_skill import build_distribution
        with tempfile.TemporaryDirectory() as tmp:
            package = build_distribution(ROOT, Path(tmp)/'packed')
            for name in ['scripts/recreation/bootstrap.py', 'scripts/recreation/inspect_media.py', 'references/recreation/workflow.md', 'references/recreation/initialization.md', 'scripts/recreation/vitpose_inference.py', 'scripts/recreation/pose_schema.py', 'scripts/recreation/retarget.py', 'scripts/recreation/temporal.py', 'scripts/recreation/kinematic_fit.py', 'scripts/recreation/motion_control.py', 'scripts/recreation/vendor_mmpose_msra.py', 'scripts/recreation/wholebody133.json', 'scripts/recreation/LICENSE-mmpose.txt', 'scripts/recreation/vitpose-provenance.json', 'references/recreation/vitpose.md']:
                self.assertTrue((package/name).is_file(), name)
            control = subprocess.run([sys.executable, '-I', '-S', str(package/'scripts/recreation/motion_control.py')], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(control.returncode, 0, control.stderr)
            temporal = subprocess.run([sys.executable, '-I', '-S', '-c',
                "import runpy,sys; m=runpy.run_path(sys.argv[1]); r=m['content_time_map']([0,1],[0.,.033],[{'source_frames':[0,1],'same_content':True,'evidence_id':'review'}]); assert r['representative_source_frames']==[0,0]; assert not any(x in sys.modules for x in ['numpy','scipy','factory'])",
                str(package/'scripts/recreation/temporal.py')], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(temporal.returncode, 0, temporal.stderr)
            measurement = subprocess.run([sys.executable, '-I', '-S', '-c',
                "import runpy,sys; m=runpy.run_path(sys.argv[1]); r=m['source_body_measurements']([[0.,0.]]*17); assert r['independent_fit_indices']==[1,2,3,4,5,6,11,12,13,14,15,16]; assert r['xy_h36m17'][9] is None; assert not any(x in sys.modules for x in ['numpy','scipy','factory'])",
                str(package/'scripts/recreation/pose_schema.py')], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(measurement.returncode, 0, measurement.stderr)
            p = subprocess.run([sys.executable, '-I', str(package/'scripts/recreation/bootstrap.py'), '--check-only', '--runtime-root', str(Path(tmp)/'state'), '--json'], capture_output=True, text=True, encoding='utf-8')
            r = json.loads(p.stdout)
            self.assertEqual(r['stage'], 'inspect')
            self.assertFalse((Path(tmp)/'state').exists())

    def test_probe_real_media_from_clean_script_without_python_packages(self):
        script = ROOT/'scripts/recreation/inspect_media.py'
        self.assertTrue(script.is_file(), 'missing standalone media inspector')
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'source [test].mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=30', '-frames:v', '6', '-c:v', 'libx264', str(source)], check=True, capture_output=True)
            p = subprocess.run([sys.executable, '-I', str(script), str(source)], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(p.returncode, 0, p.stderr)
            r = json.loads(p.stdout)
            self.assertEqual(r['video']['width'], 160)
            self.assertEqual(r['video']['frame_count'], 6)
            self.assertEqual(r['video']['avg_frame_rate'], '30/1')
            self.assertEqual(len(r['source_sha256']), 64)
            self.assertFalse(r['full_recreation_accepted'])

if __name__ == '__main__':
    unittest.main()
