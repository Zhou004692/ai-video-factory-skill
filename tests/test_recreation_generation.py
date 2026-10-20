"""Offline safety regressions. No provider account, credentials or network."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import shutil
import subprocess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'recreation'))
import generation as g
import generation_package as gp


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.post = (200, {'id': 'cgt-test-1'})
        self.get = (200, {'id': 'cgt-test-1', 'status': 'running'})
        self.media = b'silent synthetic cg'
        self.entered = None
        self.release = None

    def request_json(self, method, url, key, payload=None):
        self.calls.append(method)
        if method == 'POST' and self.entered:
            self.entered.set()
            self.release.wait(5)
        value = self.post if method == 'POST' else self.get
        if isinstance(value, Exception):
            raise value
        return value

    def get_bytes(self, url, limit):
        self.calls.append('GET-media')
        return self.media


class GenerationSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        self.cg = self.project / 'cg.mp4'
        self.cg.write_bytes(b'silent synthetic cg')
        self.artifact = {'name': 'cg_silent', 'path': str(self.cg), 'sha256': g.sha(self.cg),
                         'status': 'accepted', 'binding_sha256': 'binding', 'source_sha256': 'source',
                         'acceptance': {'sha256': g.sha(self.cg), 'user_confirmation': 'accept this CG'}}
        self.artifact_patch = patch.object(g, '_get_artifact', side_effect=lambda *a: dict(self.artifact))
        self.artifact_patch.start()
        self.addCleanup(self.artifact_patch.stop)
        self.probe_patch = patch.object(g, 'probe_media', return_value={
            'video': {'width': 640, 'height': 720, 'fps': 24, 'duration': 4.0,
                      'frame_count': 96, 'codec_name': 'h264'}, 'audio_stream_count': 0})
        self.probe_patch.start()
        self.addCleanup(self.probe_patch.stop)
        self.account = {'account_id': 'offline-test', 'region': 'cn-beijing',
                        'enabled_models': ['doubao-seedance-2-0-mini-260615'],
                        'entitlement_evidence': 'Operator checked selected account console this round'}
        self.pricing = {'model': self.account['enabled_models'][0], 'currency': 'CNY',
                        'account_id':'offline-test', 'region':'cn-beijing', 'resolution':'720p',
                        'includes_reference_video_cost':True, 'covered_output_seconds':4,
                        'upper_cost_cents': 100, 'basis': 'Current account conservative estimate including reference video',
                        'source_url': 'https://docs.volcengine.com/docs/ark/model-pricing',
                        'checked_at': g.now(), 'valid_until': '2099-01-01T00:00:00+00:00'}
        self.transport = FakeTransport()
        self.auth = {'account_id': 'offline-test', 'region': 'cn-beijing', 'ARK_API_KEY': 'fake-secret'}

    def prepare(self, **kw):
        return g.prepare(self.project, model=self.account['enabled_models'][0], prompt='编辑视频1为动物',
                         account=self.account, pricing=self.pricing, budget_cents=100,
                         options={'resolution': '720p', 'duration': 4}, **kw)

    def approved(self):
        p = self.prepare()
        g.approve(self.project, p['run_id'], p['plan_sha256'], 'This round: upload synthetic inputs and buy one generation',
                  synthetic_inputs_only=True)
        g.upload(self.project, p['run_id'], self.auth, self.transport,
                 reference={'url': 'https://media.example.test/cg?signature=secret', 'expires_at': '2099-01-01T00:00:00+00:00'})
        return p['run_id']

    def test_unaccepted_and_changed_cg_fail_before_prepare(self):
        self.artifact['status'] = 'needs_review'
        with self.assertRaises(ValueError): self.prepare()
        self.artifact['status'] = 'accepted'
        self.cg.write_bytes(b'mutated')
        with self.assertRaises(ValueError): self.prepare()

    def test_budget_profile_entitlement_and_audio_fail_closed(self):
        self.pricing['upper_cost_cents'] = 101
        with self.assertRaises(ValueError): self.prepare()
        self.pricing['upper_cost_cents'] = 100
        self.account['enabled_models'] = ['unknown']
        with self.assertRaises(ValueError): self.prepare()
        with self.assertRaises(ValueError): g.compile_parameters('unknown', {}, {})

    def test_no_upload_or_post_without_current_approval(self):
        p = self.prepare()
        with self.assertRaises(ValueError): g.upload(self.project, p['run_id'], self.auth, self.transport, reference={})
        with self.assertRaises(ValueError): g.submit(self.project, p['run_id'], self.auth, self.transport)
        self.assertEqual(self.transport.calls, [])
        with self.assertRaises(ValueError): g.approve(self.project, p['run_id'], '0'*64, 'yes', synthetic_inputs_only=True)

    def test_single_post_and_get_only_restart(self):
        run = self.approved()
        self.assertEqual(g.submit(self.project, run, self.auth, self.transport)['task_id'], 'cgt-test-1')
        with self.assertRaises(ValueError): g.submit(self.project, run, self.auth, self.transport)
        g.status(self.project, run, self.auth, self.transport)
        self.assertEqual(self.transport.calls.count('POST'), 1)
        self.assertEqual(self.transport.calls.count('GET'), 1)

    def test_ambiguous_and_rejected_outcomes_never_repeat(self):
        for outcome in (TimeoutError('https://secret.test/?key=fake-secret'), (200, {}), (503, {}),
                        (403, {'error': {'message': 'fake-secret https://signed.test/private?sig=x'}})):
            with self.subTest(outcome=type(outcome).__name__):
                run = self.approved()
                self.transport.post = outcome
                result = g.submit(self.project, run, self.auth, self.transport)
                self.assertIn(result['status'], ('ambiguous_GET_only', 'rejected_no_retry'))
                with self.assertRaises(ValueError): g.submit(self.project, run, self.auth, self.transport)
                journal = (self.project/'generation'/run/'submit-journal.json').read_text()
                self.assertNotIn('fake-secret', journal)
                self.assertNotIn('signed.test', journal)

    def test_concurrent_submission_has_exactly_one_post(self):
        run = self.approved()
        self.transport.entered, self.transport.release = threading.Event(), threading.Event()
        result = []
        t = threading.Thread(target=lambda: result.append(g.submit(self.project, run, self.auth, self.transport)))
        t.start()
        self.assertTrue(self.transport.entered.wait(3))
        with self.assertRaises(ValueError): g.submit(self.project, run, self.auth, self.transport)
        self.transport.release.set()
        t.join(5)
        self.assertEqual(self.transport.calls.count('POST'), 1)
        self.assertEqual(len(result), 1)

    def test_approved_input_and_remote_bytes_are_rechecked(self):
        p = self.prepare()
        g.approve(self.project, p['run_id'], p['plan_sha256'], 'current purchase', synthetic_inputs_only=True)
        self.transport.media = b'wrong bytes'
        with self.assertRaises(ValueError): g.upload(self.project, p['run_id'], self.auth, self.transport,
            reference={'url': 'https://example.test/cg', 'expires_at': '2099-01-01T00:00:00+00:00'})
        self.transport.media = self.cg.read_bytes()
        run = self.approved()
        self.cg.write_bytes(b'changed after upload')
        with self.assertRaises(ValueError): g.submit(self.project, run, self.auth, self.transport)
        self.assertNotIn('POST', self.transport.calls)

    def test_25_parameters_differ_from_mini(self):
        probe = {'video': {'width':640,'height':720,'fps':24,'duration':4,'codec_name':'h264'}, 'audio_stream_count':0}
        mini = g.compile_parameters('doubao-seedance-2-0-mini-260615', {'resolution':'720p','duration':4}, probe)
        full = g.compile_parameters('doubao-seedance-2-5-260628', {'resolution':'720p','duration':-1}, probe)
        self.assertNotIn('omni_reference_task_type', mini)
        self.assertNotIn('output_format', mini)
        self.assertEqual(full['omni_reference_task_type'], 'edit')
        self.assertEqual(full['duration'], -1)
        with self.assertRaises(ValueError): g.compile_parameters('doubao-seedance-2-5-260628', {'duration':8}, probe)
        mov = g.compile_parameters('doubao-seedance-2-5-260628',
                                  {'resolution':'720p','duration':-1,'output_format':'mov'},probe)
        self.assertEqual(mov['output_format'],'mov')
        for model in ('doubao-seedance-2-0-260128','doubao-seedance-2-0-fast-260128','doubao-seedance-2-0-mini-260615'):
            with self.subTest(model=model), self.assertRaises(ValueError):
                g.compile_parameters(model,{'resolution':'720p','duration':4,'output_format':'mov'},probe)
            with self.subTest(model=model), self.assertRaises(ValueError):
                g.compile_parameters(model,{'resolution':'1080p','duration':4},probe)

    def test_switching_model_requires_new_entitlement_price_and_approval(self):
        p = self.prepare()
        plan_path = self.project/'generation'/p['run_id']/'plan.json'
        g.approve(self.project,p['run_id'],p['plan_sha256'],'one Mini purchase this round',synthetic_inputs_only=True)
        plan = g.read_json(plan_path)
        plan['parameters']['model'] = 'doubao-seedance-2-5-260628'
        g.write_json(plan_path,plan)
        with self.assertRaises(ValueError): g.upload(self.project,p['run_id'],self.auth,self.transport,reference={})
        self.assertEqual(self.transport.calls,[])

    def test_expired_price_and_reference_block_submission(self):
        run = self.approved()
        reference_path = self.project/'generation'/run/'reference-private.json'
        reference = g.read_json(reference_path)
        reference['expires_at'] = '2000-01-01T00:00:00+00:00'
        g.write_json(reference_path,reference)
        with self.assertRaises(ValueError): g.submit(self.project,run,self.auth,self.transport)
        run = self.approved()
        plan = g.read_json(self.project/'generation'/run/'plan.json')
        plan['pricing']['valid_until'] = '2000-01-01T00:00:00+00:00'
        g.write_json(self.project/'generation'/run/'plan.json', plan)
        with self.assertRaises(ValueError): g.submit(self.project, run, self.auth, self.transport)
        self.assertNotIn('POST', self.transport.calls)

    def test_credentials_are_explicit_external_file_only(self):
        with self.assertRaises(ValueError): g.load_credentials(None)
        with patch.dict('os.environ', {'ARK_API_KEY':'inherited-forbidden'}):
            f = Path(self.temp.name)/'credentials.json'
            f.write_text(json.dumps({'account_id':'test','region':'cn-beijing'}))
            with self.assertRaises(ValueError): g.load_credentials(f)

    def test_audio_and_stale_prices_are_rejected(self):
        self.probe_patch.stop()
        with patch.object(g, 'probe_media', return_value={
            'video': {'width':640,'height':720,'fps':24,'duration':4,'codec_name':'h264'}, 'audio_stream_count':1}):
            with self.assertRaises(ValueError): self.prepare()
        self.pricing['checked_at'] = '2000-01-01T00:00:00+00:00'
        with self.assertRaises(ValueError): self.prepare()

    def test_price_must_cover_selected_account_and_output_window(self):
        for field, value in [('covered_output_seconds',3), ('includes_reference_video_cost',False),
                             ('resolution','480p'), ('account_id','different-account')]:
            original = self.pricing[field]
            self.pricing[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.prepare()
            self.pricing[field] = original

    def test_one_put_timeout_and_get_only_upload_recovery(self):
        p = self.prepare()
        g.approve(self.project, p['run_id'], p['plan_sha256'], 'current one upload approval', synthetic_inputs_only=True)
        with patch.object(g, '_tos_preflight'), patch.object(g, '_tos_reference', side_effect=TimeoutError('unknown PUT outcome')) as tos:
            with self.assertRaises(ValueError): g.upload(self.project, p['run_id'], self.auth, self.transport)
            with self.assertRaises(ValueError): g.upload(self.project, p['run_id'], self.auth, self.transport)
            self.assertEqual(tos.call_count, 1)
        journal = g.read_json(self.project/'generation'/p['run_id']/'upload-journal.json')
        self.assertEqual(journal['put_attempts'], 1)
        g.verify_upload(self.project, p['run_id'], self.auth, self.transport,
            reference={'url':'https://example.test/existing', 'expires_at':'2099-01-01T00:00:00+00:00'})
        self.assertEqual(g.read_json(self.project/'generation'/p['run_id']/'upload-journal.json')['put_attempts'], 1)

    def test_concurrent_upload_has_at_most_one_put(self):
        p = self.prepare()
        g.approve(self.project,p['run_id'],p['plan_sha256'],'one upload this round',synthetic_inputs_only=True)
        entered, release = threading.Event(), threading.Event()
        results = []
        def put(*a, **kw):
            entered.set()
            release.wait(5)
            return {'url':'https://example.test/object','expires_at':'2099-01-01T00:00:00+00:00'}
        with patch.object(g,'_tos_preflight'), patch.object(g,'_tos_reference',side_effect=put) as tos:
            thread = threading.Thread(target=lambda: results.append(g.upload(self.project,p['run_id'],self.auth,self.transport)))
            thread.start()
            self.assertTrue(entered.wait(3))
            with self.assertRaises(ValueError): g.upload(self.project,p['run_id'],self.auth,self.transport)
            release.set()
            thread.join(5)
            self.assertEqual(tos.call_count,1)
        self.assertEqual(len(results),1)

    def test_missing_tos_sdk_does_not_reserve_put_attempt(self):
        p = self.prepare()
        g.approve(self.project,p['run_id'],p['plan_sha256'],'one upload this round',synthetic_inputs_only=True)
        with patch.object(g,'_tos_preflight',side_effect=ModuleNotFoundError('tos missing')):
            with self.assertRaises(ModuleNotFoundError): g.upload(self.project,p['run_id'],self.auth,self.transport)
        self.assertFalse((self.project/'generation'/p['run_id']/'upload-journal.json').exists())
        self.assertEqual(self.transport.calls,[])

    def test_delivery_junction_escape_is_rejected_before_directory_or_encoding(self):
        run = self.project/'generation'/('c'*32)
        run.mkdir(parents=True)
        outside = Path(self.temp.name)/'outside'
        real_resolve = Path.resolve
        delivery = run.resolve()/'delivery'
        def resolve(path, *args, **kwargs):
            # Model a Windows directory junction at run/delivery without
            # needing symlink privileges in the test environment.
            if path.parent == delivery: return outside/path.name
            return real_resolve(path,*args,**kwargs)
        with patch.object(Path,'resolve',autospec=True,side_effect=resolve), patch.object(gp,'_run') as encode:
            with self.assertRaises(ValueError): gp._delivery_dir(self.project,run)
            encode.assert_not_called()
        self.assertFalse(outside.exists())
        self.assertFalse((run/'delivery').exists())

    def test_windows_path_gate_covers_final_delivery_before_approval(self):
        with self.assertRaises(ValueError): g._validate_output_paths(Path(self.temp.name)/('x'*230),platform='nt')
        g._validate_output_paths(Path(self.temp.name)/'short',platform='nt')
        # Atomic updates use a short sibling filename, even when the final
        # supported filename nearly reaches the platform path boundary.
        target = Path(self.temp.name)/'reference-private.json'
        original = g.os.open
        seen = []
        def opened(path,*a,**kw):
            seen.append(Path(path).name)
            return original(path,*a,**kw)
        with patch.object(g.os,'open',side_effect=opened): g.write_json(target,{'safe':True})
        self.assertRegex(seen[0],r'^\.g-[a-f0-9]{12}\.tmp$')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_local_packaging_keeps_shorter_native_video_and_source_audio(self):
        self.artifact_patch.stop()
        self.probe_patch.stop()
        import project_store as store
        source = Path(self.temp.name)/'source-audio.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=640x720:rate=30:duration=2',
            '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=2','-c:v','libx264',
            '-preset','ultrafast','-pix_fmt','yuv420p','-c:a','aac',str(source)],check=True,capture_output=True)
        project = Path(self.temp.name)/'packaging-project'
        state = store.init_project(source,project,[{'id':'one','animal':'husky','anchors':[
            {'frame_index':0,'bbox_xyxy':[1,1,600,700]}]}])
        run = 'a'*32
        directory = project/'generation'/run
        directory.mkdir(parents=True)
        native = directory/'model-original.bin'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=white:size=640x720:rate=24:duration=1.25',
            '-an','-c:v','libx264','-bf','0','-pix_fmt','yuv420p','-f','mp4',str(native)],check=True,capture_output=True)
        digest = g.sha(native)
        g.write_json(directory/'plan.json',{'cg':{'source_sha256':state['source']['sha256']}})
        g.write_json(directory/'download.json',{'sha256':digest})
        result = g.package_download(project,run)
        self.assertEqual(g.sha(native),digest)
        self.assertEqual(result['native_frames'],30)
        self.assertAlmostEqual(result['native_video_coverage_seconds'],1.25,places=3)
        self.assertAlmostEqual(result['comparison_common_window_seconds'],1.25,places=3)
        self.assertEqual(result['original_audio_probe']['video']['frame_count'],30)
        self.assertEqual(result['comparison_probe']['video']['frame_count'],30)
        self.assertEqual(result['original_audio_probe']['audio_stream_count'],1)
        self.assertFalse(result['duration_padding_applied'])
        for path in result['artifacts'].values(): self.assertTrue(Path(path).is_file())
        # A second version covers the opposite boundary: native video is longer
        # than the original, while source audio/comparison stop at real coverage.
        long_run = 'b'*32
        long_dir = project/'generation'/long_run
        long_dir.mkdir()
        long_native = long_dir/'model-original.bin'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=blue:size=640x720:rate=24:duration=2.5',
            '-an','-c:v','libx264','-bf','0','-pix_fmt','yuv420p','-f','mp4',str(long_native)],check=True,capture_output=True)
        g.write_json(long_dir/'plan.json',{'cg':{'source_sha256':state['source']['sha256']}})
        g.write_json(long_dir/'download.json',{'sha256':g.sha(long_native)})
        longer = g.package_download(project,long_run)
        self.assertEqual(longer['native_frames'],60)
        self.assertAlmostEqual(longer['native_video_coverage_seconds'],2.5,places=3)
        self.assertAlmostEqual(longer['comparison_common_window_seconds'],2.0,places=3)
        self.assertEqual(longer['comparison_probe']['video']['frame_count'],48)
        self.assertEqual(longer['original_audio_probe']['video']['frame_count'],60)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_comparison_native_clock_includes_source_final_frame_actual_duration(self):
        self.artifact_patch.stop()
        self.probe_patch.stop()
        import project_store as store
        for source_fps, source_seconds, expected_frames in ((24,1,30),(5,.6,18)):
            with self.subTest(source_fps=source_fps):
                root = Path(self.temp.name)/('clock-'+str(source_fps))
                root.mkdir()
                source = root/'source.mkv'
                subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',
                    f'testsrc2=size=320x320:rate={source_fps}:duration={source_seconds}',
                    '-c:v','ffv1',str(source)],check=True,capture_output=True)
                project = root/'project'
                state = store.init_project(source,project,[{'id':'one','animal':'husky','anchors':[
                    {'frame_index':0,'bbox_xyxy':[1,1,300,300]}]}])
                run = 'd'*32
                directory = project/'generation'/run
                directory.mkdir(parents=True)
                native = directory/'model-original.bin'
                subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',
                    'color=white:size=320x320:rate=30:duration=1','-an','-c:v','libx264',
                    '-bf','0','-pix_fmt','yuv420p','-f','mp4',str(native)],check=True,capture_output=True)
                g.write_json(directory/'plan.json',{'cg':{'source_sha256':state['source']['sha256']}})
                g.write_json(directory/'download.json',{'sha256':g.sha(native)})
                result = g.package_download(project,run)
                self.assertEqual(result['native_frames'],30)
                self.assertEqual(result['original_audio_probe']['video']['frame_count'],30)
                self.assertEqual(result['comparison_probe']['video']['frame_count'],expected_frames)
                measured_source = (state['source']['frames'][-1]['pts_seconds'] +
                    state['source']['frames'][-1]['duration_seconds'] - state['source']['frames'][0]['pts_seconds'])
                self.assertAlmostEqual(result['comparison_common_window_seconds'],measured_source,places=8)
                self.assertFalse(result['duration_padding_applied'])

    def test_get_recovery_after_cg_mutates_and_original_download_is_unpadded(self):
        run = self.approved()
        g.submit(self.project, run, self.auth, self.transport)
        self.cg.write_bytes(b'changed after purchase')
        self.transport.get = (200, {'id':'cgt-test-1','status':'succeeded','duration':3,
            'content':{'video_url':'https://example.test/original?signature=private'},
            'error':{'message':'fake-secret https://sensitive.test/?sig=private'}})
        result = g.download(self.project, run, self.auth, self.transport)
        self.assertFalse(result['duration_padding_applied'])
        self.assertEqual(Path(result['path']).read_bytes(), self.transport.media)
        self.assertEqual(self.transport.calls.count('POST'), 1)
        private = (self.project/'generation'/run/'provider-response-private.json').read_text()
        self.assertNotIn('fake-secret', private)
        self.assertNotIn('sensitive.test', json.dumps(g.status(self.project, run)))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_project_store_and_real_silent_cg_gate(self):
        self.artifact_patch.stop()
        self.probe_patch.stop()
        import project_store as store
        source = Path(self.temp.name)/'source.mkv'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=128x96:rate=10:duration=0.5',
                        '-c:v','ffv1',str(source)], check=True, capture_output=True)
        project = Path(self.temp.name)/'real-project'
        store.init_project(source, project, [{'id':'one','animal':'husky','anchors':[
            {'frame_index':0,'bbox_xyxy':[1,1,60,80]}]}])
        cg = project/'cg.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=white:size=640x720:rate=24:duration=4',
                        '-an','-c:v','libx264','-pix_fmt','yuv420p',str(cg)],check=True,capture_output=True)
        item = store.record_artifact(project,'cg_silent',cg)
        def prepare():
            return g.prepare(project,model=self.account['enabled_models'][0],prompt='编辑视频为动物',
                account=self.account,pricing=self.pricing,budget_cents=100,options={'resolution':'720p','duration':4})
        with self.assertRaises(ValueError): prepare()
        store.accept_artifact(project,'cg_silent',item['sha256'],'accepted this synthetic CG')
        plan = prepare()
        frozen = g.read_json(project/'generation'/plan['run_id']/'plan.json')
        self.assertEqual(frozen['cg_probe']['video']['frame_count'],96)
        self.assertEqual(frozen['cg_probe']['audio_stream_count'],0)
        mov = project/'cg.mov'
        subprocess.run(['ffmpeg','-v','error','-i',str(cg),'-c','copy',str(mov)],check=True,capture_output=True)
        mov_probe = g.probe_media(mov)
        parameters = g.compile_parameters('doubao-seedance-2-5-260628',
            {'duration':-1,'resolution':'720p','output_format':'mov'},mov_probe)
        self.assertEqual(parameters['output_format'],'mov')
        self.assertEqual(mov_probe['video']['frame_count'],96)
        image = project/'identity.png'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=blue:size=640x720',
                        '-frames:v','1',str(image)],check=True,capture_output=True)
        self.assertEqual(g._image_facts(image),'image/png')
        item = store.record_artifact(project,'identity_one',image)
        store.accept_artifact(project,'identity_one',item['sha256'],'accepted synthetic image')
        with_image = g.prepare(project,model=self.account['enabled_models'][0],prompt='编辑视频为动物',
            account=self.account,pricing=self.pricing,budget_cents=100,options={'resolution':'720p','duration':4},
            image_artifacts=['identity_one'])
        g.approve(project,with_image['run_id'],with_image['plan_sha256'],'buy with this synthetic image',synthetic_inputs_only=True)
        image.write_bytes(b'changed after approval')
        with self.assertRaises(ValueError): g.upload(project,with_image['run_id'],self.auth,self.transport,reference={})
        store.update_roles(project,[{'id':'one','animal':'husky','anchors':[
            {'frame_index':0,'bbox_xyxy':[2,2,60,80]}]}], 'corrected identity anchor')
        with self.assertRaises(ValueError): g.approve(project,plan['run_id'],plan['plan_sha256'],'buy this',synthetic_inputs_only=True)


if __name__ == '__main__':
    unittest.main()
