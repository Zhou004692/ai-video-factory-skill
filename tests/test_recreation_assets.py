import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]

def load(name):
    spec=importlib.util.spec_from_file_location('portable_'+name,ROOT/'scripts/recreation'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

class ModelAssetTests(unittest.TestCase):
    def fixture_spec(self):
        return dict(id='test-model',family='vitpose',schema='coco_wholebody133',input_size_wh=[192,256],
                    decoder='mmpose-msra-default',bytes=8,sha256=hashlib.sha256(b'fixture!').hexdigest(),
                    url='https://example.invalid/fixed-model.onnx',source_revision='test-pinned',
                    license_notice='test fixture; no real model download')

    def test_missing_model_is_explicit_and_no_download_without_confirmation(self):
        m=load('model_assets')
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)/'runtime'
            self.assertFalse(m.model_status(home)['ready'])
            with self.assertRaises(ValueError):m.prepare_model(home)
            self.assertFalse(home.exists())

    def test_register_verified_cache_without_network_or_copying_weight(self):
        m=load('model_assets')
        with tempfile.TemporaryDirectory() as tmp,patch.object(m,'MODEL_SPEC',self.fixture_spec()):
            base=Path(tmp);weight=base/'model.onnx';weight.write_bytes(b'fixture!')
            home=base/'runtime';r=m.prepare_model(home,source_path=weight)
            self.assertEqual(r['model_downloads'],0)
            status=m.model_status(home)
            self.assertTrue(status['ready']);self.assertEqual(Path(status['model_path']),weight.resolve())
            manifest=json.loads(Path(r['manifest_path']).read_text(encoding='utf-8'))
            self.assertEqual(manifest['sha256'],self.fixture_spec()['sha256'])
            weight.write_bytes(b'changed!')
            self.assertFalse(m.model_status(home)['ready'])

    def test_download_hash_mismatch_never_publishes_manifest(self):
        m=load('model_assets')
        with tempfile.TemporaryDirectory() as tmp,patch.object(m,'MODEL_SPEC',self.fixture_spec()):
            home=Path(tmp)/'runtime'
            with self.assertRaises(ValueError):m.prepare_model(home,download_approved=True,open_url=lambda *a,**k:io.BytesIO(b'bad-data'))
            self.assertFalse((home/'models/vitpose-wholebody-b/model-manifest.json').exists())
            self.assertFalse(list(home.rglob('*.part')))

    def test_download_is_bounded_verified_and_not_repeated_when_cached(self):
        m=load('model_assets');calls=[]
        with tempfile.TemporaryDirectory() as tmp,patch.object(m,'MODEL_SPEC',self.fixture_spec()):
            home=Path(tmp)/'runtime'
            def fetch(*args,**kwargs):calls.append(args);return io.BytesIO(b'fixture!')
            m.prepare_model(home,download_approved=True,open_url=fetch)
            self.assertTrue(m.model_status(home)['ready'])
            m.prepare_model(home,download_approved=True,open_url=fetch)
            self.assertEqual(len(calls),1)

    def test_replaceable_skill_cannot_hold_runtime_or_weight(self):
        m=load('model_assets')
        with self.assertRaises(ValueError):m.prepare_model(ROOT/'not-a-runtime',download_approved=True)

    def test_runtime_model_symlink_cannot_write_into_skill_or_escape_runtime(self):
        m=load('model_assets')
        with tempfile.TemporaryDirectory() as tmp,patch.object(m,'MODEL_SPEC',self.fixture_spec()):
            base=Path(tmp);home=base/'runtime';home.mkdir();fake_skill=base/'skill';fake_skill.mkdir()
            weight=base/'cached.onnx';weight.write_bytes(b'fixture!')
            try:(home/'models').symlink_to(fake_skill,target_is_directory=True)
            except OSError as exc:self.skipTest('Directory symlink unavailable: '+str(exc))
            with patch.object(m,'SKILL_ROOT',fake_skill):
                with self.assertRaises(ValueError):m.prepare_model(home,source_path=weight)
            self.assertEqual(list(fake_skill.iterdir()),[])

    def test_interrupted_unheld_lock_file_does_not_block_cache_registration(self):
        m=load('model_assets')
        with tempfile.TemporaryDirectory() as tmp,patch.object(m,'MODEL_SPEC',self.fixture_spec()):
            base=Path(tmp);weight=base/'cached.onnx';weight.write_bytes(b'fixture!')
            home=base/'runtime';directory=m.manifest_path(home).parent;directory.mkdir(parents=True)
            (directory/'.prepare.lock').write_text('interrupted process',encoding='utf-8')
            self.assertTrue(m.prepare_model(home,source_path=weight)['ready'])

class ExecutionReadinessTests(unittest.TestCase):
    def test_dependency_ready_is_distinct_from_pose_model_readiness(self):
        d=load('doctor')
        with tempfile.TemporaryDirectory() as tmp,patch.object(d,'dependency_status',return_value={'ready':True}),patch.object(d,'model_status',return_value={'ready':False,'reason':'missing model'}):
            r=d.check_runtime(Path(tmp),stage='pose')
            self.assertTrue(r['dependencies_ready']);self.assertFalse(r['execution_ready'])
            self.assertIn('model',r['resources'])

    def test_browser_or_rig_missing_stops_render_execution(self):
        d=load('doctor')
        with tempfile.TemporaryDirectory() as tmp,patch.object(d,'dependency_status',return_value={'ready':True}),patch.object(d,'render_resources',return_value={'ready':False,'missing':['browser','rig']}):
            r=d.check_runtime(Path(tmp),stage='render')
            self.assertFalse(r['execution_ready'])

if __name__=='__main__':unittest.main()
