"""Mixed failed/pending/candidate jobs must retain an executable recovery path."""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from factory import db, creative_ir, production_compiler, production_graph, production_adapters, production_graph_runner
from factory.clients import seedance
from factory.steps import s3_shots
import httpx
from factory.management_api import ManagementService


class NetworkMasterRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'data').mkdir()
        self.database = sqlite3.connect(':memory:')
        self.database.row_factory = sqlite3.Row
        self.database.executescript(db.DDL)
        self.addCleanup(self.database.close)
        board = {'characters':[{'id':'actor','name':'Actor'}], 'masters':[
            {'idx':i,'speaker':'actor','scene':f'scene {i}','scene_anchor_key':f'scene-{i}',
             'duration':6,'voiceover':'Hello','type':'dialogue'} for i in (1,2,3)],
            'production_settings':{'budget_cents':10000}}
        self.spec = production_compiler.compile_ir(creative_ir.from_storyboard(board),
            job_id='J-NET',capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,runtime_storyboard=board).spec
        self.database.execute('INSERT INTO jobs(job_id,topic_id,title,state,storyboard,production_spec_json,production_spec_hash,production_spec_version,execution_engine,cost_cents) VALUES(?,?,?,?,?,?,?,?,?,?)',
            ('J-NET','topic','Network recovery','FAILED',json.dumps(self.spec.runtime_storyboard),json.dumps(self.spec.as_dict()),self.spec.spec_hash,2,'graph_v2',944))
        production_graph.persist_graph(self.database,self.spec)
        production_adapters.materialize_frozen_execution_inputs(self.database,self.spec)
        self.candidate = self.root/'data/assets/J-NET/shots/review/03-candidate.mp4'
        self.candidate.parent.mkdir(parents=True)
        self.candidate.write_bytes(b'existing paid candidate')
        self.database.execute("UPDATE production_nodes SET state='SUCCEEDED' WHERE node_type LIKE '%anchor'")
        self.database.execute("UPDATE production_nodes SET state='FAILED',error_code='RuntimeError' WHERE node_id='master:1'")
        self.database.execute("UPDATE production_nodes SET state='PENDING' WHERE node_id='master:2'")
        self.database.execute("UPDATE production_nodes SET state='REVIEW_REQUIRED',provider_task_id='paid-candidate',actual_cost_cents=944 WHERE node_id='master:3'")
        self.database.execute("UPDATE shots SET state='FAILED',error=?,video_qa=? WHERE idx=1",
            ('SeedanceSubmissionUnknownError: Seedance 提交前连接失败；本次错误归类为 transient_network，可按重试策略处理。',json.dumps({'status':'generation_failed','failure_code':'transient_network'})))
        self.database.execute("UPDATE shots SET state='REVIEW_REQUIRED',cost_cents=944,video_qa=? WHERE idx=3",
            (json.dumps({'status':'manual_review_required','candidate_path':str(self.candidate),'reasons':['qa_error']}),))
        self.database.commit()
        self.service = ManagementService(self.database,self.root,worker_mode='external')

    def test_read_model_exposes_recovery_instead_of_invalid_repair(self):
        job = self.service.videos()['jobs'][0]
        self.assertEqual(job.get('network_retry_indexes'),[1])
        self.assertEqual(job['next_action']['id'],'retry_network_failures')
        self.assertFalse(job['shots'][0]['regeneration_eligible'])
        self.assertTrue(job['shots'][2]['candidate_preview_url'])

    def test_confirmed_retry_requeues_graph_without_recompiling_or_touching_candidate(self):
        before=dict(self.database.execute("SELECT * FROM production_nodes WHERE node_id='master:3'").fetchone())
        with mock.patch.object(self.service,'dispatch_production_queue',return_value={'dispatched_jobs':[]}) as dispatch:
            result=self.service.retry_network_failures('J-NET',confirmed=True)
        self.assertEqual(result['retried_master_indexes'],[1])
        self.assertEqual(self.database.execute("SELECT state FROM production_nodes WHERE node_id='master:1'").fetchone()[0],'READY')
        self.assertEqual(self.database.execute("SELECT state FROM jobs").fetchone()[0],'PRODUCTION_QUEUED')
        self.assertEqual(dict(self.database.execute("SELECT * FROM production_nodes WHERE node_id='master:3'").fetchone()),before)
        self.assertEqual(self.candidate.read_bytes(),b'existing paid candidate')
        self.assertEqual(self.database.execute('SELECT production_spec_hash FROM jobs').fetchone()[0],self.spec.spec_hash)
        dispatch.assert_called_once()

    def test_ambiguous_submit_is_not_retryable(self):
        self.database.execute("UPDATE shots SET error='submit result unknown' WHERE idx=1")
        self.database.commit()
        self.assertEqual(self.service.videos()['jobs'][0].get('network_retry_indexes'),[])
        with self.assertRaises(ValueError):
            self.service.retry_network_failures('J-NET',confirmed=True)

    def test_worker_drains_retried_and_unblocked_masters_then_returns_to_candidate_review(self):
        with mock.patch.object(self.service,'dispatch_production_queue',return_value={'dispatched_jobs':[]}):
            self.service.retry_network_failures('J-NET',confirmed=True)
        visited=[]
        async def generated(node):
            visited.append(node['node_id'])
            idx=int(node['node_id'].split(':')[1])
            self.database.execute("UPDATE shots SET state='OK' WHERE idx=?",(idx,))
            self.database.commit()
            return {}
        result=asyncio.run(production_graph_runner.run_graph_job(self.database,'J-NET',configured_limit=1,handlers={'master':generated}))
        self.assertEqual(visited,['master:1','master:2'])
        self.assertEqual(result.job_state,'PRODUCTION_REVIEW_REQUIRED')
        self.assertEqual(self.candidate.read_bytes(),b'existing paid candidate')
        self.assertEqual(self.service.videos()['jobs'][0]['next_action']['id'],'review_master_candidates')

    def test_remote_task_or_cost_blocks_retry(self):
        self.database.execute("UPDATE production_nodes SET provider_task_id='unknown-remote' WHERE node_id='master:1'")
        self.database.commit()
        self.assertEqual(self.service.videos()['jobs'][0].get('network_retry_indexes'),[])

    def test_paid_failed_node_is_never_retried(self):
        self.database.execute("UPDATE production_nodes SET actual_cost_cents=944 WHERE node_id='master:1'")
        self.database.commit()
        with self.assertRaises(ValueError):
            self.service.retry_network_failures('J-NET',confirmed=True)

    def test_connection_failure_persists_explicit_no_submit_evidence(self):
        exc=seedance._submission_transport_error(httpx.ConnectError('connection failed'))
        s3_shots._record_failed_generation(self.database,'J-NET',1,exc)
        qa=json.loads(self.database.execute('SELECT video_qa FROM shots WHERE idx=1').fetchone()[0])
        self.assertIs(qa['submit_result_unknown'],False)
        self.assertEqual(self.service.videos()['jobs'][0]['network_retry_indexes'],[1])

    def test_explicit_unknown_marker_overrides_legacy_error_message(self):
        self.database.execute('UPDATE shots SET video_qa=? WHERE idx=1',
            (json.dumps({'failure_code':'transient_network','submit_result_unknown':True}),))
        self.database.commit()
        self.assertEqual(self.service.videos()['jobs'][0]['network_retry_indexes'],[])

    def test_duplicate_confirmation_does_not_requeue_again(self):
        with mock.patch.object(self.service,'dispatch_production_queue',return_value={'dispatched_jobs':[]}):
            self.service.retry_network_failures('J-NET',confirmed=True)
            with self.assertRaises(ValueError):
                self.service.retry_network_failures('J-NET',confirmed=True)

    def test_failed_queue_transition_rolls_back_node_and_shot_changes(self):
        before=dict(self.database.execute('SELECT * FROM shots WHERE idx=1').fetchone())
        with mock.patch('factory.network_recovery.state_machine.transition',side_effect=RuntimeError('queue failure')):
            with self.assertRaises(RuntimeError):
                self.service.retry_network_failures('J-NET',confirmed=True)
        self.assertEqual(dict(self.database.execute('SELECT * FROM shots WHERE idx=1').fetchone()),before)
        self.assertEqual(self.database.execute("SELECT state FROM production_nodes WHERE node_id='master:1'").fetchone()[0],'FAILED')

    def test_confirmation_required_and_active_job_rejected(self):
        with self.assertRaises(ValueError):
            self.service.retry_network_failures('J-NET',confirmed=False)
        self.database.execute("UPDATE jobs SET state='PRODUCTION_RUNNING'")
        self.database.commit()
        with self.assertRaises(ValueError):
            self.service.retry_network_failures('J-NET',confirmed=True)


if __name__ == '__main__':
    unittest.main()
