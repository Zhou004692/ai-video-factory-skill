from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import unittest

from scripts.factory.db import DDL
from scripts.factory.recovery_controller import reconcile_job


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


if __name__ == "__main__":
    unittest.main()
