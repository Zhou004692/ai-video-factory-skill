import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from factory import db, provider_executor, provider_tasks
from factory.management_api import ManagementService


class ProviderAliasRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'data').mkdir()
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
        self.c.executescript(db.DDL);self.addCleanup(self.c.close)
        self.c.execute("INSERT INTO jobs(job_id,topic_id,title,state,execution_engine,production_spec_hash,cost_cents) VALUES('J','T','alias','PROVIDER_ACTION_REQUIRED','graph_v2','spec',100)")
        self.c.execute("INSERT INTO production_nodes(job_id,node_id,node_type,paid,state,spec_hash,input_json,idempotency_key,provider_task_id,actual_cost_cents) VALUES('J','master:1','master',1,'SUCCEEDED','spec','{}','idem','remote',100)")
        self.c.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,cost_cents,usage_json) VALUES('remote','J',1,'seedance','generate',100,?)",(json.dumps({'status':'action_required'}),))
        self.alias='local-submit:seedance:J:1:generate'
        self.c.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,cost_cents,usage_json) VALUES(?,'J',1,'seedance','generate',0,?)",(self.alias,json.dumps({'status':'action_required','remote_task_id':'remote'})))
        self.video=self.root/'data/assets/J/shots/01.mp4';self.video.parent.mkdir(parents=True);self.video.write_bytes(b'accepted remote video')
        self.qa={'status':'candidate_accepted_pending','human_decision':'accept_candidate',
            'provider_calls':[{'task_id':'remote','cost_cents':100}],
            'review_operation':{'decision':'accept_candidate','phase':'committed','active_path':str(self.video),
                                'candidate_sha256':hashlib.sha256(self.video.read_bytes()).hexdigest()}}
        self.c.execute("INSERT INTO shots(job_id,idx,state,video_path,video_qa,cost_cents) VALUES('J',1,'OK',?,?,100)",(str(self.video),json.dumps(self.qa)))
        self.c.execute("INSERT INTO production_nodes(job_id,node_id,node_type,paid,state,spec_hash,input_json,idempotency_key,error_code) VALUES('J','master:2','master',1,'ACTION_REQUIRED','spec','{}','idem2','ProviderTaskGateError')")
        self.c.execute("INSERT INTO shots(job_id,idx,state) VALUES('J',2,'PENDING')")
        self.c.commit();self.service=ManagementService(self.c,self.root,worker_mode='external')

    def test_accepted_media_closes_both_aliases_without_remote_download_or_reapproval(self):
        before=self.video.read_bytes()
        with mock.patch('factory.management_api.seedance.inspect_task',new=mock.AsyncMock(return_value={'status':'unknown'})) as remote:
            r=self.service.reconcile_provider_task('J','remote')
        remote.assert_not_awaited()
        self.assertEqual(r['remote_status'],'completed')
        self.assertEqual(provider_tasks.unresolved_task_ids(self.c,'J'),[])
        self.assertFalse(self.c.execute('SELECT 1 FROM provider_calls WHERE task_id=?',(self.alias,)).fetchone())
        self.assertEqual(self.c.execute("SELECT state FROM production_nodes WHERE node_id='master:2'").fetchone()[0],'READY')
        self.assertEqual(self.c.execute('SELECT cost_cents FROM jobs').fetchone()[0],100)
        self.assertEqual(self.c.execute('SELECT state FROM shots WHERE idx=1').fetchone()[0],'OK')
        self.assertEqual(self.video.read_bytes(),before)

    def test_corrupt_accepted_media_does_not_close_unknown_task(self):
        self.video.write_bytes(b'changed')
        with mock.patch('factory.management_api.seedance.inspect_task',new=mock.AsyncMock(return_value={'status':'unknown'})) as remote:
            r=self.service.reconcile_provider_task('J','remote')
        remote.assert_awaited_once()
        self.assertEqual(r['remote_status'],'unknown')
        self.assertEqual(provider_tasks.unresolved_task_ids(self.c,'J'),['remote'])

    def test_completed_ledger_is_not_downgraded_by_duplicate_sweep(self):
        self.c.execute('DELETE FROM provider_calls WHERE task_id=?',(self.alias,))
        self.c.execute("UPDATE provider_calls SET usage_json='{\"status\":\"completed\"}' WHERE task_id='remote'")
        self.c.execute("UPDATE shots SET video_qa='{}'")
        self.c.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,usage_json) VALUES('other','J',2,'seedance','generate','{\"status\":\"unknown\"}')")
        self.c.commit()
        with mock.patch('factory.management_api.seedance.inspect_task',new=mock.AsyncMock(return_value={'status':'unknown'})) as remote:
            result=self.service.reconcile_provider_task('J','remote')
        remote.assert_not_awaited()
        self.assertEqual(result['remote_status'],'completed')
        self.assertEqual(provider_tasks.unresolved_task_ids(self.c,'J'),['other'])

    def test_retry_binding_preserves_unresolved_or_differently_owned_attempt(self):
        for status,node_id in [('pending','master:1'),('completed','master:2')]:
            with self.subTest(status=status,node_id=node_id):
                self.c.execute("UPDATE production_nodes SET state='RUNNING' WHERE node_id='master:1'")
                self.c.execute('UPDATE provider_calls SET usage_json=? WHERE task_id=\'remote\'',(json.dumps({'status':status}),))
                self.c.execute('UPDATE provider_calls SET usage_json=? WHERE task_id=?',
                    (json.dumps({'remote_task_id':'new','owner_node_id':node_id,'owner_spec_hash':'spec'}),self.alias));self.c.commit()
                with self.assertRaises(provider_executor.ProductionContractRequiredError):
                    provider_executor._bind_frozen_node_task(self.c,provider_executor.FrozenNodeOwner('J','master:1','spec'),'new')
                self.assertEqual(self.c.execute("SELECT provider_task_id FROM production_nodes WHERE node_id='master:1'").fetchone()[0],'remote')

    def test_remote_id_promotion_checks_local_owner_and_binding(self):
        for update in ["shot_idx=2", "usage_json='{\"remote_task_id\":\"another\"}'", 'cost_cents=10']:
            with self.subTest(update=update):
                self.c.execute("UPDATE provider_calls SET shot_idx=1,cost_cents=0,usage_json=? WHERE task_id=?",(json.dumps({'remote_task_id':'remote'}),self.alias))
                self.c.execute('UPDATE provider_calls SET '+update+' WHERE task_id=?',(self.alias,));self.c.commit()
                with self.assertRaises(provider_tasks.ProviderTaskGateError):
                    provider_tasks.promote_reservation(self.c,self.alias,task_id='remote',job_id='J',shot_idx=1,provider='seedance',intent='generate')
                self.assertTrue(self.c.execute('SELECT 1 FROM provider_calls WHERE task_id=?',(self.alias,)).fetchone())

    def test_completed_attempt_can_bind_a_new_authorized_retry_on_same_node(self):
        self.c.execute('DELETE FROM provider_calls WHERE task_id=?',(self.alias,))
        self.c.execute("UPDATE jobs SET state='PRODUCTION_RUNNING'")
        self.c.execute("UPDATE production_nodes SET state='RUNNING' WHERE node_id='master:1'")
        self.c.execute("UPDATE provider_calls SET usage_json='{\"status\":\"completed\"}' WHERE task_id='remote'");self.c.commit()
        with provider_executor.paid_task_owner_context(job_id='J',node_id='master:1',spec_hash='spec'):
            reservation=provider_executor.reserve_paid_submit(self.c,job_id='J',shot_idx=1,provider='seedance',intent='generate')
            provider_executor.persist_remote_task_id(self.c,reservation,remote_task_id='retry-remote',provider='seedance',intent='generate')
            provider_executor.promote_reservation(self.c,reservation,task_id='retry-remote',job_id='J',shot_idx=1,provider='seedance',intent='generate')
        row=self.c.execute("SELECT provider_task_id,actual_cost_cents FROM production_nodes WHERE node_id='master:1'").fetchone()
        self.assertEqual(tuple(row),('retry-remote',100))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM provider_calls WHERE task_id NOT LIKE 'local-submit:%'").fetchone()[0],2)


if __name__=='__main__':unittest.main()
