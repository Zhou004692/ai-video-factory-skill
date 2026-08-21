from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
import inspect

from scripts.factory.db import DDL
from scripts.factory.state_machine import (
    InvalidTransitionError,
    transition,
)
from scripts.factory import orchestrator
from scripts.factory.management_api import ManagementService


class ProductionStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "factory.db"
        database = self.connection()
        database.executescript(DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) VALUES(?,?,?,?)",
            ("J-1", "T-1", "state machine", "PRODUCTION_QUEUED"),
        )
        database.commit()
        database.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def connection(self) -> sqlite3.Connection:
        database = sqlite3.connect(
            str(self.path), timeout=2, check_same_thread=False,
        )
        database.row_factory = sqlite3.Row
        return database

    def test_transition_is_atomic_and_idempotent(self):
        database = self.connection()
        first = transition(
            database, "J-1", "production_started", event_id="E-1",
        )
        second = transition(
            database, "J-1", "production_started", event_id="E-1",
        )
        self.assertEqual(first.state, "PRODUCTION_RUNNING")
        self.assertEqual(second.state, "PRODUCTION_RUNNING")
        self.assertTrue(first.applied)
        self.assertFalse(second.applied)
        self.assertEqual(
            database.execute(
                "SELECT COUNT(*) FROM job_transitions WHERE event_id='E-1'"
            ).fetchone()[0],
            1,
        )
        database.close()

    def test_illegal_transition_preserves_job_and_writes_no_event(self):
        database = self.connection()
        with self.assertRaises(InvalidTransitionError):
            transition(database, "J-1", "composed", event_id="E-bad")
        self.assertEqual(
            database.execute(
                "SELECT state FROM jobs WHERE job_id='J-1'"
            ).fetchone()[0],
            "PRODUCTION_QUEUED",
        )
        self.assertEqual(
            database.execute(
                "SELECT COUNT(*) FROM job_transitions"
            ).fetchone()[0],
            0,
        )
        database.close()

    def test_event_cannot_override_its_allowed_target(self):
        database = self.connection()
        with self.assertRaises(InvalidTransitionError):
            transition(
                database,
                "J-1",
                "production_started",
                event_id="E-target",
                target_state="PUBLISHED",
            )
        self.assertEqual(
            database.execute(
                "SELECT state FROM jobs WHERE job_id='J-1'"
            ).fetchone()[0],
            "PRODUCTION_QUEUED",
        )
        database.close()

    def test_competing_transitions_have_one_winner(self):
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def apply(event_id: str) -> None:
            database = self.connection()
            barrier.wait()
            try:
                result = transition(
                    database, "J-1", "production_started",
                    event_id=event_id,
                )
                outcomes.append(f"ok:{result.state}")
            except InvalidTransitionError:
                outcomes.append("invalid")
            finally:
                database.close()

        threads = [
            threading.Thread(target=apply, args=("E-A",)),
            threading.Thread(target=apply, args=("E-B",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(
            sorted(outcomes), ["invalid", "ok:PRODUCTION_RUNNING"],
        )
        database = self.connection()
        self.assertEqual(
            database.execute(
                "SELECT COUNT(*) FROM job_transitions"
            ).fetchone()[0],
            1,
        )
        database.close()

    def test_transition_updates_business_fields_in_same_commit(self):
        database = self.connection()
        result = transition(
            database,
            "J-1",
            "provider_paused",
            event_id="E-pause",
            updates={
                "error": "远端任务待核对",
                "failure_code": "transient_network",
                "production_worker_pid": None,
            },
        )
        self.assertEqual(result.state, "PROVIDER_ACTION_REQUIRED")
        row = database.execute(
            "SELECT state,error,failure_code FROM jobs WHERE job_id='J-1'"
        ).fetchone()
        self.assertEqual(tuple(row), (
            "PROVIDER_ACTION_REQUIRED",
            "远端任务待核对",
            "transient_network",
        ))
        database.close()

    def test_orchestrator_delegates_all_job_state_changes(self):
        source = inspect.getsource(orchestrator)
        self.assertNotIn("UPDATE jobs SET state", source)
        self.assertIn("transition(", source)

    def test_core_management_entries_delegate_job_state_changes(self):
        for name in (
            "start_video_job",
            "dispatch_production_queue",
            "resume_failed_video",
            "review_master_candidate",
        ):
            with self.subTest(entry=name):
                source = inspect.getsource(getattr(ManagementService, name))
                self.assertNotIn("UPDATE jobs SET state", source)
                self.assertIn("state_machine.transition(", source)

    def test_transition_joins_caller_transaction_and_can_be_rolled_back(self):
        database = self.connection()
        database.execute("BEGIN IMMEDIATE")
        transition(
            database, "J-1", "production_started", event_id="E-nested",
        )
        database.rollback()
        self.assertEqual(
            database.execute(
                "SELECT state FROM jobs WHERE job_id='J-1'"
            ).fetchone()[0],
            "PRODUCTION_QUEUED",
        )
        self.assertEqual(
            database.execute(
                "SELECT COUNT(*) FROM job_transitions WHERE event_id='E-nested'"
            ).fetchone()[0],
            0,
        )
        database.close()


if __name__ == "__main__":
    unittest.main()
