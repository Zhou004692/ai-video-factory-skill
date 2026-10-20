import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import importlib.util
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
ENTRY=ROOT/'scripts/recreation/recreate.py'

class PortableEntryTests(unittest.TestCase):
    def call(self,*args):
        return subprocess.run([sys.executable,'-I',str(ENTRY),*map(str,args)],capture_output=True,text=True,encoding='utf-8')

    def fixture(self,base):
        source=base/'source.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x96:rate=10',
                        '-frames:v','4','-c:v','libx264',str(source)],capture_output=True,check=True)
        roles=base/'roles.json';roles.write_text(json.dumps([dict(id='role1',animal='husky',anchors=[dict(frame_index=0,bbox_xyxy=[20,5,80,90])])]),encoding='utf-8')
        return source,roles

    def test_inspect_and_init_run_without_cloud_keys_or_production_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);source,roles=self.fixture(base);project=base/'project';runtime=base/'runtime'
            result=self.call('--runtime-root',runtime,'inspect','--source',source,'--output',base/'inspection')
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            self.assertTrue((base/'inspection/selection.html').is_file())
            result=self.call('--runtime-root',runtime,'init','--source',source,'--project',project,'--roles',roles)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            self.assertTrue((project/'project.json').is_file())
            result=self.call('--runtime-root',runtime,'status','--project',project)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            report=json.loads(result.stdout)
            self.assertEqual(report['source_frames'],4)
            self.assertEqual(report['paid_provider_calls'],0)
            self.assertFalse((project/'factory.db').exists());self.assertFalse((project/'.env').exists())

    def test_local_stops_at_missing_pose_stage_and_does_not_install_or_buy(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);source,roles=self.fixture(base);project=base/'project';runtime=base/'runtime'
            result=self.call('--runtime-root',runtime,'init','--source',source,'--project',project,'--roles',roles)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            result=self.call('--runtime-root',runtime,'local','--project',project)
            self.assertNotEqual(result.returncode,0)
            report=json.loads(result.stdout)
            self.assertEqual(report['status'],'dependencies_required')
            self.assertEqual(report['paid_provider_calls'],0)
            self.assertFalse((runtime/'venvs').exists())

    def test_help_exposes_project_review_resume_and_optional_generation(self):
        result=self.call('--help')
        self.assertEqual(result.returncode,0,result.stderr)
        for word in ['init','select','local','accept','status','generation','doctor','model']:
            self.assertIn(word,result.stdout)

    def test_tos_upload_selects_optional_stage_but_https_and_recovery_do_not(self):
        spec=importlib.util.spec_from_file_location('entry_env',ENTRY)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        for arguments,needs_sdk in [(['upload','p','r'],True),(['upload','p','r','--reference-config-file','ref.json'],False),
                                    (['verify-upload','p','r'],True),(['status','p','r'],False),(['prepare','p'],False)]:
            with (self.subTest(arguments=arguments),patch.object(module,'_stage_python',return_value='external-tos-python') as stage,
                    patch.object(module.subprocess,'run') as run):
                run.return_value.returncode=0
                self.assertEqual(module.generation_command(Path('runtime'),arguments),0)
                if needs_sdk:stage.assert_called_once_with(Path('runtime'),'generate-tos')
                else:stage.assert_not_called()
                self.assertEqual(run.call_args.args[0][0],'external-tos-python' if needs_sdk else sys.executable)

    def test_stage_output_symlink_is_rejected_before_any_external_write(self):
        spec=importlib.util.spec_from_file_location('entry_paths',ENTRY)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);project=base/'project';project.mkdir();outside=base/'outside';outside.mkdir()
            try:(project/'observations').symlink_to(outside,target_is_directory=True)
            except OSError as exc:self.skipTest(str(exc))
            with self.assertRaises(ValueError):module._next_path(project,'observations','.json')
            self.assertEqual(list(outside.iterdir()),[])

if __name__=='__main__':unittest.main()
