"""Reconciled downloads must release only unsubmitted sibling gate pauses."""
import unittest
from unittest import mock
from tests import test_network_master_recovery as fixtures
from factory import production_executor


class ResolvedProviderGateTests(unittest.TestCase):
    setUp = fixtures.NetworkMasterRecoveryTests.setUp

    def gate(self):
        self.database.execute("UPDATE jobs SET state='PRODUCTION_REVIEW_REQUIRED'")
        self.database.execute("UPDATE shots SET state='PENDING',error=NULL,video_qa=NULL WHERE idx=1")
        self.database.execute("UPDATE production_nodes SET state='ACTION_REQUIRED',error_code='ProviderTaskGateError',next_action='reconcile_provider_task' WHERE node_id='master:1'")
        self.database.commit()

    def test_release_preserves_candidate_and_is_idempotent(self):
        self.gate()
        candidate=dict(self.database.execute("SELECT * FROM production_nodes WHERE node_id='master:3'").fetchone())
        with self.database:
            released=production_executor.release_resolved_provider_gates(self.database,'J-NET')
        self.assertEqual(released,['master:1'])
        self.assertEqual(self.database.execute("SELECT state FROM production_nodes WHERE node_id='master:1'").fetchone()[0],'READY')
        self.assertEqual(dict(self.database.execute("SELECT * FROM production_nodes WHERE node_id='master:3'").fetchone()),candidate)
        self.assertEqual(production_executor.release_resolved_provider_gates(self.database,'J-NET'),[])
        self.assertEqual(self.database.execute('SELECT state FROM jobs').fetchone()[0],'PRODUCTION_REVIEW_REQUIRED')

    def test_unresolved_remote_task_keeps_gate(self):
        self.gate()
        with mock.patch('factory.production_executor.provider_tasks.unresolved_task_ids',return_value=['remote']):
            self.assertEqual(production_executor.release_resolved_provider_gates(self.database,'J-NET'),[])

    def test_actual_reconciliation_releases_sibling_after_candidate_publication(self):
        self.gate()
        self.database.execute("UPDATE jobs SET state='PROVIDER_ACTION_REQUIRED'")
        self.database.execute("UPDATE production_nodes SET state='ACTION_REQUIRED' WHERE node_id='master:3'")
        self.database.execute("UPDATE shots SET state='FAILED',video_qa=NULL WHERE idx=3")
        self.database.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,cost_cents,usage_json) VALUES('paid-candidate','J-NET',3,'seedance','generate',944,'{\"status\":\"action_required\"}')")
        self.database.commit()
        with mock.patch('factory.management_api.seedance.inspect_task',new=mock.AsyncMock(return_value={
            'status':'completed','cost_cents':944,'usage':{},'video':b'recovered existing media','video_url':'https://example.com/result.mp4',
        })) as inspect:
            result=self.service.reconcile_provider_task('J-NET','paid-candidate')
        inspect.assert_awaited_once_with('paid-candidate')
        self.assertEqual(result['state'],'PRODUCTION_REVIEW_REQUIRED')
        self.assertEqual(self.database.execute("SELECT state FROM production_nodes WHERE node_id='master:1'").fetchone()[0],'READY')
        self.assertEqual(self.database.execute('SELECT cost_cents FROM jobs').fetchone()[0],944)
        self.assertEqual(self.service.master_candidate_file('J-NET',3).read_bytes(),b'recovered existing media')

    def test_paid_gate_and_terminal_job_are_not_released(self):
        self.gate()
        self.database.execute("UPDATE production_nodes SET provider_task_id='remote' WHERE node_id='master:1'")
        self.assertEqual(production_executor.release_resolved_provider_gates(self.database,'J-NET'),[])
        self.database.execute("UPDATE production_nodes SET provider_task_id=NULL WHERE node_id='master:1'")
        self.database.execute("UPDATE jobs SET state='REJECTED_PENDING_RECONCILIATION'")
        self.assertEqual(production_executor.release_resolved_provider_gates(self.database,'J-NET'),[])


if __name__=='__main__':
    unittest.main()
