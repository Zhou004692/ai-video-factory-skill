from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import unittest

from scripts.factory.db import DDL
from scripts.factory.recovery_controller import (
    admit_stopped_provider_reconciliation, reconcile_job,
)


class RecoveryControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(DDL)
        self.now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.database.close()

    def seed_job(
        self, job_id: str = "J-1", *, failure_code: str | None = None,
        attempts: int = 0,
    ) -> None:
        stale = (self.now - timedelta(minutes=10)).isoformat()
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,failure_code,"
            "production_worker_pid,production_heartbeat_at,worker_lease_id,"
            "recovery_attempts) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                job_id, "T-1", "recover", "PRODUCTION_RUNNING", failure_code,
                999999, stale, "lease-old", attempts,
            ),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state) VALUES(?,1,'PENDING')",
            (job_id,),
        )
        self.database.commit()

    def test_dead_worker_with_no_remote_task_returns_to_queue(self):
        self.seed_job()
        outcome = reconcile_job(
            self.database, "J-1", now=self.now, worker_alive=lambda _pid: False,
        )
        self.assertEqual(outcome.action, "requeue")
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-1'"
            ).fetchone()[0],
            "PRODUCTION_QUEUED",
        )

    def test_remote_task_is_reconciled_without_requeue(self):
        self.seed_job()
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json) VALUES('remote-1','J-1',1,'seedance','generate',?)",
            (json.dumps({"status": "pending"}),),
        )
        self.database.commit()
        outcome = reconcile_job(
            self.database, "J-1", now=self.now, worker_alive=lambda _pid: False,
        )
        self.assertEqual(outcome.action, "reconcile_provider")
        self.assertEqual(outcome.task_ids, ("remote-1",))
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-1'"
            ).fetchone()[0],
            "PROVIDER_ACTION_REQUIRED",
        )

    def test_manual_review_candidate_is_never_auto_skipped(self):
        self.seed_job()
        self.database.execute(
            "UPDATE shots SET state='REVIEW_REQUIRED',video_qa=? "
            "WHERE job_id='J-1'",
            (json.dumps({"status": "manual_review_required"}),),
        )
        self.database.commit()
        outcome = reconcile_job(
            self.database, "J-1", now=self.now, worker_alive=lambda _pid: False,
        )
        self.assertEqual(outcome.action, "manual_review")
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-1'"
            ).fetchone()[0],
            "PRODUCTION_REVIEW_REQUIRED",
        )

    def test_policy_failure_is_terminal_and_never_requeued(self):
        self.seed_job(failure_code="input_privacy_rejected")
        outcome = reconcile_job(
            self.database, "J-1", now=self.now, worker_alive=lambda _pid: False,
        )
        self.assertEqual(outcome.action, "terminal_policy_failure")
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-1'"
            ).fetchone()[0],
            "FAILED",
        )

    def test_three_failed_recoveries_require_manual_diagnosis(self):
        self.seed_job(attempts=3)
        outcome = reconcile_job(
            self.database, "J-1", now=self.now, worker_alive=lambda _pid: False,
        )
        self.assertEqual(outcome.action, "manual_recovery")
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-1'"
            ).fetchone()[0],
            "RECOVERY_REQUIRED",
        )

    def test_live_lease_is_left_untouched(self):
        self.seed_job()
        self.database.execute(
            "UPDATE jobs SET production_heartbeat_at=? WHERE job_id='J-1'",
            (self.now.isoformat(),),
        )
        self.database.commit()
        outcome = reconcile_job(
            self.database, "J-1", now=self.now, worker_alive=lambda _pid: False,
        )
        self.assertEqual(outcome.action, "none")
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-1'"
            ).fetchone()[0],
            "PRODUCTION_RUNNING",
        )

    def seed_graph(self, state, output=None):
        self.seed_job()
        self.database.execute("UPDATE jobs SET execution_engine='graph_v2'")
        self.database.execute("INSERT INTO production_nodes(job_id,node_id,node_type,state,spec_hash,input_json,idempotency_key,lease_id,output_json) VALUES('J-1','master:1','master',?,'s','{}','i','old',?)", (state, json.dumps(output or {})))
        self.database.commit()

    def test_graph_failure_is_not_requeued_by_dead_worker_recovery(self):
        self.seed_graph('FAILED')
        outcome = reconcile_job(self.database, 'J-1', now=self.now)
        self.assertEqual(outcome.state, 'FAILED')
        self.assertNotEqual(outcome.action, 'requeue')

    def test_completed_checkpoint_projects_review_after_restart(self):
        self.seed_graph('RUNNING', {'candidate_path': 'paid.mp4', '_executor_checkpoint': {'event': 'require_review', 'event_id': 'completed'}})
        outcome = reconcile_job(self.database, 'J-1', now=self.now)
        self.assertEqual(outcome.state, 'PRODUCTION_REVIEW_REQUIRED')
        self.assertEqual(self.database.execute('SELECT state FROM production_nodes').fetchone()[0], 'REVIEW_REQUIRED')

    def test_stranded_pending_graph_requires_diagnosis(self):
        self.seed_graph('PENDING')
        self.assertEqual(reconcile_job(self.database, 'J-1', now=self.now).state, 'RECOVERY_REQUIRED')

    def test_stopped_graph_known_task_releases_fresh_lease_without_submit(self):
        self.seed_graph('RUNNING')
        self.database.execute("UPDATE jobs SET state='FAILED',failure_code='internal_error' WHERE job_id='J-1'")
        self.database.execute("UPDATE production_nodes SET provider_task_id='remote-1',heartbeat_at='2099-01-01T00:00:00+00:00' WHERE job_id='J-1'")
        self.database.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,usage_json) VALUES('remote-1','J-1',1,'seedance','generate',?)", (json.dumps({'status':'action_required'}),))
        self.database.commit()
        self.assertTrue(admit_stopped_provider_reconciliation(
            self.database, 'J-1', 'remote-1', worker_alive=lambda _pid: False,
        ))
        self.assertEqual(self.database.execute('SELECT state FROM jobs').fetchone()[0], 'PROVIDER_ACTION_REQUIRED')
        node = self.database.execute('SELECT state,lease_id FROM production_nodes').fetchone()
        self.assertEqual((node['state'], node['lease_id']), ('ACTION_REQUIRED', None))
        self.assertFalse(admit_stopped_provider_reconciliation(
            self.database, 'J-1', 'remote-1', worker_alive=lambda _pid: False,
        ))

    def test_stopped_graph_does_not_steal_live_worker_or_unknown_task(self):
        self.seed_graph('RUNNING')
        self.database.execute("UPDATE jobs SET state='FAILED',failure_code='internal_error' WHERE job_id='J-1'")
        self.database.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,usage_json) VALUES('remote-1','J-1',1,'seedance','generate',?)", (json.dumps({'status':'action_required'}),))
        self.database.commit()
        with self.assertRaisesRegex(ValueError, 'still running'):
            admit_stopped_provider_reconciliation(
                self.database, 'J-1', 'remote-1', worker_alive=lambda _pid: True,
            )
        with self.assertRaisesRegex(ValueError, 'not pending'):
            admit_stopped_provider_reconciliation(
                self.database, 'J-1', 'other-task', worker_alive=lambda _pid: False,
            )
        self.database.execute("UPDATE provider_calls SET shot_idx=-2001,provider='seedance_scene'")
        self.database.commit()
        with self.assertRaisesRegex(ValueError, 'no unique graph node owner'):
            admit_stopped_provider_reconciliation(
                self.database, 'J-1', 'remote-1', worker_alive=lambda _pid: False,
            )
        self.assertEqual(self.database.execute('SELECT state FROM jobs').fetchone()[0], 'FAILED')
        self.assertEqual(self.database.execute('SELECT state FROM production_nodes').fetchone()[0], 'RUNNING')

    def test_policy_failure_cannot_transfer_to_provider_recovery(self):
        self.seed_graph('RUNNING')
        self.database.execute("UPDATE jobs SET state='FAILED',failure_code='output_policy_rejected' WHERE job_id='J-1'")
        self.database.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,usage_json) VALUES('remote-1','J-1',1,'seedance','generate',?)", (json.dumps({'status':'action_required'}),))
        self.database.commit()
        self.assertFalse(admit_stopped_provider_reconciliation(
            self.database, 'J-1', 'remote-1', worker_alive=lambda _pid: False,
        ))
        self.assertEqual(self.database.execute('SELECT state FROM jobs').fetchone()[0], 'FAILED')


if __name__ == "__main__":
    unittest.main()
