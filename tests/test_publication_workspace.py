import json
from pathlib import Path
import subprocess
import unittest
ROOT=Path(__file__).resolve().parents[1]
class PublicationWorkspaceTests(unittest.TestCase):
    def label(self,item):
        module=ROOT/'scripts/management_ui/publication-workspace.js';self.assertTrue(module.is_file(),'publication evidence labels missing')
        result=subprocess.run(['node','-e',"require(process.argv[1]);console.log(JSON.stringify(AVFPublicationWorkspace.label(JSON.parse(process.argv[2]))))",str(module),json.dumps(item)],capture_output=True,text=True,timeout=10,encoding='utf-8');self.assertEqual(result.returncode,0,result.stderr);return json.loads(result.stdout)
    def test_submitted_pending_is_not_success(self):
        self.assertEqual(self.label({'verification_status':'pending_review','binding_status':'server_verified_douyin_upload'}),'平台审核中')
    def test_registration_and_unverified_are_explicit(self):
        self.assertEqual(self.label({'verification_status':'verified','binding_status':'local_asset_captured'}),'已登记 · 平台成片待核实')
        self.assertEqual(self.label({}),'历史状态待核实')
    def test_platform_rejection_and_verified_upload_distinct(self):
        self.assertEqual(self.label({'verification_status':'verified','metadata':{'review_status':'rejected'}}),'平台审核未通过')
        self.assertEqual(self.label({'verification_status':'verified','binding_status':'server_verified_douyin_upload','metadata':{'review_status':'approved'}}),'已发布 · 上传成片已核实')
if __name__=='__main__':unittest.main()
