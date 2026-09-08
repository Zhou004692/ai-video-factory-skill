"""One recovery policy for API presentation and command admission."""
import json
import sqlite3
import sys
from pathlib import Path
import unittest
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from factory import db


class RecoveryPlanTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:'); self.c.row_factory=sqlite3.Row
        self.c.executescript(db.DDL)
        self.c.execute("INSERT INTO jobs(job_id,topic_id,title,state,execution_engine) VALUES('J','T','test','FAILED','graph_v2')")
        self.c.execute("INSERT INTO production_nodes(job_id,node_id,node_type,state,spec_hash,input_json,idempotency_key,error_code) VALUES('J','master:1','master','FAILED','s','{}','i','RuntimeError')")
        self.c.execute("INSERT INTO shots(job_id,idx,state) VALUES('J',1,'PENDING')")
        self.c.execute("INSERT INTO production_node_events(job_id,event_id,node_id,event,from_state,to_state,payload_json) VALUES('J','f','master:1','fail','RUNNING','FAILED',?)",(json.dumps({'error':'reference limit'}),))
        self.c.commit()

    def tearDown(self): self.c.close()

    def plan(self):
        from factory.recovery_plan import resolve
        return resolve(self.c,'J')

    def test_failed_graph_never_advertises_blind_resume(self):
        p=self.plan()
        self.assertEqual(p['next_action']['id'],'preserve_and_diagnose')
        self.assertIn('reference limit',p['reason'])
        self.assertNotIn('resume_failed_video',p['action_ids'])

    def test_paid_unknown_result_takes_precedence_over_retry(self):
        self.c.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,usage_json) VALUES('remote','J',1,'seedance','master','{\"status\":\"unknown\"}')")
        self.assertEqual(self.plan()['next_action']['id'],'reconcile_provider_task')

    def test_terminal_state_does_not_resume_from_remaining_nodes(self):
        self.c.execute("UPDATE jobs SET state='REJECTED'")
        self.assertEqual(self.plan()['action_ids'],[])

    def test_candidate_can_be_reviewed_alongside_failed_sibling(self):
        candidate = tempfile.NamedTemporaryFile(delete=False)
        candidate.close()
        self.addCleanup(Path(candidate.name).unlink)
        self.c.execute("INSERT INTO shots(job_id,idx,state,video_qa) VALUES('J',2,'REVIEW_REQUIRED',?)",(json.dumps({'status':'manual_review_required','candidate_path':candidate.name}),))
        self.c.execute("INSERT INTO production_nodes(job_id,node_id,node_type,state,spec_hash,input_json,idempotency_key) VALUES('J','master:2','master','REVIEW_REQUIRED','s','{}','i2')")
        self.assertIn('review_master_candidates',self.plan()['action_ids'])
        self.c.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,usage_json) VALUES('remote','J',1,'seedance','master','{\"status\":\"unknown\"}')")
        self.assertNotIn('regenerate_master', self.plan()['candidate_decisions'])
        from factory.recovery_plan import require_review_decision
        with self.assertRaises(ValueError): require_review_decision(self.c, 'J', 'regenerate_master')
        require_review_decision(self.c, 'J', 'accept_candidate')
        Path(candidate.name).unlink()
        self.assertNotIn('review_master_candidates', self.plan()['action_ids'])
        Path(candidate.name).touch()

    def test_running_nodes_prevent_reentrant_commands_even_with_stale_job(self):
        self.c.execute("UPDATE production_nodes SET state='RUNNING',lease_id='live'")
        self.assertEqual(self.plan()['action_ids'],['view_progress'])

    def test_blocked_pending_graph_has_diagnostic_action(self):
        self.c.execute("UPDATE production_nodes SET state='PENDING',error_code=NULL")
        self.assertEqual(self.plan()['next_action']['id'],'preserve_and_diagnose')

    def test_runnable_failed_job_can_resume(self):
        self.c.execute("UPDATE production_nodes SET state='READY',error_code=NULL")
        self.assertEqual(self.plan()['next_action']['id'],'resume_failed_video')

    def test_command_rechecks_current_facts(self):
        from factory.recovery_plan import require_action
        self.c.execute("UPDATE production_nodes SET state='READY',error_code=NULL")
        self.assertIn('resume_failed_video',self.plan()['action_ids'])
        self.c.execute("UPDATE jobs SET state='REJECTED'")
        with self.assertRaises(ValueError): require_action(self.c,'J','resume_failed_video')
