"""Atomic outcome publication and stale-worker protection."""
import json
import sqlite3
import sys
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from factory import db, node_state_machine


class NodeCommitTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.DDL)
        self.c.execute("INSERT INTO jobs(job_id,topic_id,title,state) VALUES('J','T','test','PRODUCTION_RUNNING')")
        self.c.execute("INSERT INTO production_nodes(job_id,node_id,node_type,state,spec_hash,input_json,idempotency_key,lease_id,output_json) VALUES('J','master:1','master','RUNNING','s','{}','i','lease','{\"checkpoint\":true}')")
        self.c.commit()

    def tearDown(self):
        self.c.close()

    def finish(self, **kwargs):
        return node_state_machine.finish_attempt(
            self.c, 'J', 'master:1', 'require_review', lease_id='lease',
            event_id='done', output={'candidate_path': 'paid.mp4'}, **kwargs,
        )

    def test_result_event_and_lease_commit_together(self):
        self.assertTrue(self.finish().applied)
        row = self.c.execute('SELECT * FROM production_nodes').fetchone()
        self.assertEqual(row['state'], 'REVIEW_REQUIRED')
        self.assertIsNone(row['lease_id'])
        self.assertEqual(row['next_action'], 'review_candidate')
        self.assertEqual(json.loads(row['output_json']), {'candidate_path': 'paid.mp4'})

    def test_transition_failure_preserves_checkpoint_and_lease(self):
        with mock.patch.object(node_state_machine, 'transition_node_in_transaction', side_effect=RuntimeError('injected')):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                self.finish()
        row = self.c.execute('SELECT * FROM production_nodes').fetchone()
        self.assertEqual((row['state'], row['lease_id']), ('RUNNING', 'lease'))
        self.assertEqual(json.loads(row['output_json']), {'checkpoint': True})
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM production_node_events').fetchone()[0], 0)

    def test_old_lease_cannot_replace_new_attempt(self):
        self.c.execute("UPDATE production_nodes SET lease_id='new',provider_task_id='new-task'")
        self.c.commit()
        self.assertFalse(self.finish().applied)
        self.assertEqual(tuple(self.c.execute('SELECT lease_id,provider_task_id FROM production_nodes').fetchone()), ('new', 'new-task'))

    def test_repeated_event_cannot_rewrite_result(self):
        self.finish()
        self.assertFalse(self.finish().applied)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM production_node_events').fetchone()[0], 1)

    def test_caller_transaction_remains_uncommitted(self):
        self.c.execute('BEGIN IMMEDIATE')
        self.finish()
        self.c.rollback()
        self.assertEqual(self.c.execute('SELECT state FROM production_nodes').fetchone()[0], 'RUNNING')

    def test_event_identity_cannot_be_reused_for_other_node(self):
        self.finish()
        with self.assertRaises(ValueError):
            node_state_machine.finish_attempt(self.c, 'J', 'other', 'fail', lease_id='lease', event_id='done', output={})

    def test_terminated_job_cannot_lease_ready_work(self):
        from factory import production_executor
        self.c.execute("UPDATE production_nodes SET state='READY',lease_id=NULL")
        self.c.execute("UPDATE jobs SET state='REJECTED'")
        self.c.commit()
        self.assertIsNone(production_executor.acquire_lease(self.c,'J','master:1'))

    def test_projection_preserves_termination_even_with_unfinished_graph(self):
        from factory import production_projection
        self.c.execute("UPDATE jobs SET state='REJECTED'")
        self.c.commit()
        self.assertEqual(production_projection.reconcile(self.c,'J').projected_job_state,'REJECTED')

    def test_terminal_projection_cannot_advance_to_review(self):
        from factory import production_projection
        self.c.execute("UPDATE jobs SET state='REJECTED'")
        self.c.commit()
        result=production_projection.persist(self.c,'J')
        self.assertEqual(result.projected_job_state,'REJECTED')
        self.assertEqual(self.c.execute('SELECT state FROM jobs').fetchone()[0],'REJECTED')

    def test_terminated_job_cannot_reserve_paid_submission(self):
        from factory import provider_tasks
        self.c.execute("UPDATE jobs SET state='REJECTED'")
        self.c.commit()
        with self.assertRaises(provider_tasks.ProviderTaskGateError):
            provider_tasks.reserve_paid_submit(self.c,job_id='J',shot_idx=1,provider='seedance',intent='master')
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM provider_calls').fetchone()[0],0)

    def test_expired_lease_with_unbound_reservation_cannot_be_requeued(self):
        from factory import production_executor
        self.c.execute("UPDATE production_nodes SET output_json='{}',paid=1")
        self.c.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,usage_json) VALUES('local-submit:x','J',1,'seedance','master','{\"status\":\"submitting\"}')")
        self.c.commit()
        recovered=production_executor.recover_expired_nodes(self.c,'J')
        self.assertEqual(recovered['master:1'],'ACTION_REQUIRED')

    def test_corrupt_checkpoint_never_becomes_unsubmitted_work(self):
        from factory import production_executor
        self.c.execute("UPDATE production_nodes SET output_json='{broken'")
        self.c.commit()
        self.assertEqual(production_executor.recover_expired_nodes(self.c, 'J')['master:1'], 'ACTION_REQUIRED')
        self.assertIn('{broken', self.c.execute('SELECT output_json FROM production_nodes').fetchone()[0])

    def test_known_contract_failure_with_diagnostic_output_can_retry_once(self):
        from factory import production_executor
        node_state_machine.finish_attempt(self.c, 'J', 'master:1', 'fail', lease_id='lease',
            event_id='failed', output={'error': 'frozen scene anchor is not ready: old'}, error_code='RuntimeError')
        self.assertEqual(production_executor.retry_repaired_contract_nodes(self.c, 'J'), ('master:1',))
        self.assertEqual(production_executor.retry_repaired_contract_nodes(self.c, 'J'), ())
