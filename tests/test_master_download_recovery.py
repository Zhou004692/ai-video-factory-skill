"""Paid master recovery uses its durable task through formal handlers."""
from __future__ import annotations

import base64
import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from tests import test_strict_master_compose_journey as journey
from tests.strict_handler_support import StrictMasterComposeReplay
from factory.management_api import ManagementService
from factory import db as factory_db, recovery_plan

JOB_ID = journey.JOB_ID

class MasterDownloadRecoveryTests(unittest.IsolatedAsyncioTestCase):
    prepare = journey.StrictMasterComposeJourneyTests.prepare
    run_job = journey.StrictMasterComposeJourneyTests.run_job
    contract = journey.StrictMasterComposeJourneyTests.contract
    frozen_graph_snapshot = journey.StrictMasterComposeJourneyTests.frozen_graph_snapshot
    assert_single_paid_master_submission = (
        journey.StrictMasterComposeJourneyTests.assert_single_paid_master_submission
    )

    async def _recover(
        self, *, missing=False, remote_status=None, corrupt_binding=False,
        corrupt_output=False,
    ):
        contract = self.contract()
        recovery_steps = self.contract()["steps"][2:]
        corrupt = b"authored corrupt master media"
        response = contract["steps"][3]["response"]
        response.pop("media_fixture")
        response.update(
            body_base64=base64.b64encode(corrupt).decode("ascii"),
            body_sha256=hashlib.sha256(corrupt).hexdigest(),
        )
        contract["steps"] = contract["steps"][:4] + recovery_steps
        if remote_status:
            recovery_steps[0]["response"]["json"]["status"] = remote_status
            contract["steps"] = contract["steps"][:5]
        if corrupt_binding or corrupt_output:
            contract["steps"] = contract["steps"][:4]
        with StrictMasterComposeReplay(contract=contract) as replay:
            self.prepare(replay)
            frozen = self.frozen_graph_snapshot(replay.database)
            first = await self.run_job(replay)
            self.assertEqual(first.job_state, "FAILED")
            self.assert_single_paid_master_submission(replay)
            if missing:
                for media in (replay.root / "data" / "assets" / JOB_ID).rglob("*.mp4"):
                    media.unlink()
            service = ManagementService(replay.database, replay.root, worker_mode="external")
            if corrupt_binding:
                replay.database.execute(
                    "UPDATE production_nodes SET provider_task_id='unrelated-task' "
                    "WHERE job_id=? AND node_id='master:1'", (JOB_ID,),
                )
                replay.database.commit()
                with self.assertRaisesRegex(ValueError, "不能重新提交"):
                    service.resume_failed_video(JOB_ID)
                self.assertEqual(replay.database.execute(
                    "SELECT state FROM jobs WHERE job_id=?", (JOB_ID,),
                ).fetchone()[0], "FAILED")
                return
            if corrupt_output:
                replay.database.execute(
                    "UPDATE production_nodes SET output_json='{broken' "
                    "WHERE job_id=? AND node_id='master:1'", (JOB_ID,),
                )
                replay.database.commit()
                with self.assertRaisesRegex(ValueError, "证据损坏"):
                    service.resume_failed_video(JOB_ID)
                self.assertEqual(replay.database.execute(
                    "SELECT state FROM jobs WHERE job_id=?", (JOB_ID,),
                ).fetchone()[0], "FAILED")
                return
            resumed = service.resume_failed_video(JOB_ID)
            self.assertEqual(resumed["state"], "PRODUCTION_QUEUED")
            node = replay.database.execute(
                "SELECT state,provider_task_id FROM production_nodes "
                "WHERE job_id=? AND node_id='master:1'", (JOB_ID,),
            ).fetchone()
            self.assertEqual(tuple(node), ("READY", "strict-master-task-1"))
            second = await self.run_job(replay)
            if remote_status:
                self.assertEqual(second.job_state, "FAILED")
                self.assert_single_paid_master_submission(replay)
                self.assertEqual(self.frozen_graph_snapshot(replay.database), frozen)
                repeat = await self.run_job(replay)
                self.assertEqual(repeat.started_nodes, ())
                return
            self.assertEqual(second.job_state, "COMPOSED")
            self.assertEqual(self.frozen_graph_snapshot(replay.database), frozen)
            self.assert_single_paid_master_submission(replay)
            self.assertEqual(replay.database.execute(
                "SELECT actual_cost_cents FROM production_nodes "
                "WHERE job_id=? AND node_id='master:1'", (JOB_ID,),
            ).fetchone()[0], 7)
            self.assertEqual(replay.database.execute(
                "SELECT cost_cents FROM shots WHERE job_id=? AND idx=1", (JOB_ID,),
            ).fetchone()[0], 7)
            request_count = len(replay.requests)
            third = await self.run_job(replay)
            self.assertEqual(third.started_nodes, ())
            self.assertEqual(len(replay.requests), request_count)

    async def test_corrupt_paid_master_recovers_without_submit_or_rebilling(self):
        await self._recover()

    async def test_missing_paid_master_recovers_without_submit_or_rebilling(self):
        await self._recover(missing=True)

    async def test_remote_failed_task_never_falls_back_to_generation(self):
        await self._recover(remote_status="failed")

    async def test_remote_expired_task_never_falls_back_to_generation(self):
        await self._recover(remote_status="expired")

    async def test_nonmatching_task_evidence_cannot_requeue_paid_master(self):
        await self._recover(corrupt_binding=True)

    async def test_damaged_node_output_cannot_requeue_paid_master(self):
        await self._recover(corrupt_output=True)

    def _assert_mixed_recovery_refused(self, *, paid_idx, mismatched_later=False):
        with tempfile.TemporaryDirectory() as directory, closing(sqlite3.connect(":memory:")) as database:
            database.row_factory = sqlite3.Row
            database.executescript(factory_db.DDL)
            root = Path(directory)
            (root / "data").mkdir()
            (root / "templates").mkdir()
            (root / "templates" / "env.example").write_text("", encoding="utf-8")
            (root / ".env").write_text("", encoding="utf-8")
            service = ManagementService(database, root, worker_mode="external")
            self.prepare(SimpleNamespace(database=database))
            database.execute(
                "INSERT INTO shots(job_id,idx,state) VALUES(?,2,'PENDING')", (JOB_ID,),
            )
            database.execute(
                "INSERT INTO production_nodes(job_id,node_id,node_type,paid,state,"
                "spec_hash,input_json,idempotency_key) SELECT job_id,'master:2',"
                "node_type,paid,'PENDING',spec_hash,input_json,idempotency_key || ':2' "
                "FROM production_nodes WHERE job_id=? AND node_id='master:1'", (JOB_ID,),
            )
            database.execute(
                "UPDATE production_nodes SET state='PENDING' WHERE job_id=? "
                "AND node_id='master:1'", (JOB_ID,),
            )
            database.execute("UPDATE shots SET state='PENDING' WHERE job_id=?", (JOB_ID,))
            paid_indexes = (1, 2) if mismatched_later else (paid_idx,)
            for idx in paid_indexes:
                database.execute(
                    "UPDATE production_nodes SET state='FAILED',provider_task_id=?,"
                    "actual_cost_cents=7 WHERE job_id=? AND node_id=?",
                    (f"paid-{idx}", JOB_ID, f"master:{idx}"),
                )
                database.execute(
                    "UPDATE shots SET state='FAILED',cost_cents=7 WHERE job_id=? AND idx=?",
                    (JOB_ID, idx),
                )
                database.execute(
                    "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
                    "cost_cents,usage_json) VALUES(?,?,?,'seedance','generate',7,?)",
                    (f"paid-{idx}", JOB_ID, 99 if mismatched_later and idx == 2 else idx,
                     '{"status":"completed"}'),
                )
            database.execute(
                "UPDATE jobs SET state='FAILED',cost_cents=? WHERE job_id=?",
                (7 * len(paid_indexes), JOB_ID),
            )
            database.commit()
            def snapshot():
                return {
                    table: [tuple(row) for row in database.execute(
                        f"SELECT * FROM {table} WHERE job_id=?", (JOB_ID,),
                    ).fetchall()]
                    for table in ("jobs", "shots", "production_nodes", "provider_calls",
                                  "production_node_events", "job_transitions")
                }
            before = snapshot()
            with self.assertRaisesRegex(ValueError, "不能重新提交"):
                service.resume_failed_video(JOB_ID)
            self.assertEqual(snapshot(), before)

    def test_paid_first_pending_second_cannot_fall_back_to_generation(self):
        self._assert_mixed_recovery_refused(paid_idx=1)

    def test_pending_first_paid_second_cannot_fall_back_to_generation(self):
        self._assert_mixed_recovery_refused(paid_idx=2)

    def test_later_mismatched_paid_binding_cannot_partially_requeue(self):
        self._assert_mixed_recovery_refused(paid_idx=1, mismatched_later=True)

    def _assert_interrupted_provider_resume(self, *, failure_code, unresolved):
        with tempfile.TemporaryDirectory() as directory, closing(sqlite3.connect(":memory:")) as database:
            database.row_factory = sqlite3.Row
            database.executescript(factory_db.DDL)
            root = Path(directory)
            (root / "data").mkdir()
            (root / "templates").mkdir()
            (root / "templates" / "env.example").write_text("", encoding="utf-8")
            (root / ".env").write_text("", encoding="utf-8")
            service = ManagementService(database, root, worker_mode="external")
            self.prepare(SimpleNamespace(database=database))
            database.execute(
                "UPDATE jobs SET state='FAILED',failure_code=? WHERE job_id=?",
                (failure_code, JOB_ID),
            )
            if unresolved:
                database.execute(
                    "UPDATE production_nodes SET state='RUNNING',provider_task_id='crashed-task' "
                    "WHERE job_id=? AND node_id='master:1'", (JOB_ID,),
                )
                database.execute(
                    "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
                    "usage_json) VALUES('crashed-task',?,1,'seedance','generate',?)",
                    (JOB_ID, '{"status":"pending"}'),
                )
            database.commit()
            plan = recovery_plan.resolve(database, JOB_ID)
            if unresolved:
                self.assertEqual(plan["next_action"]["id"], "reconcile_provider_task")
                self.assertEqual(plan["task_ids"], ["crashed-task"])
                with self.assertRaises(ValueError):
                    service.resume_failed_video(JOB_ID)
                self.assertEqual(tuple(database.execute(
                    "SELECT state,provider_task_id FROM production_nodes "
                    "WHERE job_id=? AND node_id='master:1'", (JOB_ID,),
                ).fetchone()), ("RUNNING", "crashed-task"))
            else:
                with self.assertRaises(ValueError):
                    service.resume_failed_video(JOB_ID)
            self.assertEqual(database.execute(
                "SELECT state FROM jobs WHERE job_id=?", (JOB_ID,),
            ).fetchone()[0], "FAILED")
            self.assertEqual(database.execute(
                "SELECT cost_cents FROM jobs WHERE job_id=?", (JOB_ID,),
            ).fetchone()[0], 0)

    def test_internal_worker_failure_with_pending_task_pauses_for_reconciliation(self):
        self._assert_interrupted_provider_resume(
            failure_code="internal_error", unresolved=True,
        )

    def test_internal_worker_failure_without_task_stays_failed(self):
        self._assert_interrupted_provider_resume(
            failure_code="internal_error", unresolved=False,
        )

    def test_policy_failure_with_pending_task_cannot_resume(self):
        self._assert_interrupted_provider_resume(
            failure_code="output_policy_rejected", unresolved=True,
        )


if __name__ == "__main__":
    unittest.main()
