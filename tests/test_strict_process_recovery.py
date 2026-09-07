"""Cross-process strict replay: durable paid-task/media recovery, no live I/O."""
import hashlib
import os
import socket
import sqlite3
import subprocess
from pathlib import Path
import unittest

from tests.strict_process_support import StrictProcessJourney, master_fixture


class StrictProcessRecoveryTests(unittest.TestCase):
    def test_supervisor_rejects_unregistered_io(self):
        with StrictProcessJourney() as journey:
            with self.assertRaisesRegex(AssertionError, "database outside"):
                sqlite3.connect(str(journey.root.parent / "outside.db"))
            with self.assertRaisesRegex(AssertionError, "DNS denied"):
                socket.getaddrinfo("example.com", 443)
            with socket.socket() as connection:
                with self.assertRaisesRegex(AssertionError, "socket denied"):
                    connection.connect(("93.184.216.34", 443))
            with self.assertRaisesRegex(AssertionError, "unregistered supervisor child"):
                subprocess.Popen(["unregistered-program"])

    def test_worker_contract_mismatch_is_not_reported_as_success(self):
        with StrictProcessJourney() as journey:
            contract = journey.fixture.contract()
            contract["steps"][0]["request"]["url"] += "/wrong"
            journey.write_contract("normal", contract)
            with self.assertRaisesRegex(AssertionError, "strict Worker failed"):
                journey.dispatch()
            self.assertEqual(journey.database.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0], 0)

    def test_abrupt_worker_death_preserves_unknown_remote_outcome(self):
        with StrictProcessJourney() as journey:
            record = journey.dispatch("interrupted")
            self.assertTrue(record["interrupted"])
            self.assertEqual(record["http_requests_matched"], 4)
            self.assertEqual(journey.database.execute("SELECT state FROM jobs").fetchone()[0], "FAILED")
            # A transport receipt is not a committed completed-provider record.
            # The existing reconciliation gate must retain ownership, not POST.
            result = journey.service.resume_failed_video(master_fixture.JOB_ID)
            self.assertEqual(result["state"], "PROVIDER_ACTION_REQUIRED")
            self.assertEqual(journey.service.dispatch_production_queue()["dispatched_jobs"], [])
            self.assertEqual(len(journey.records), 1)
            self.assertEqual(journey.database.execute("SELECT provider_task_id FROM production_nodes WHERE node_id='master:1'").fetchone()[0], "strict-master-task-1")

    def assert_paid_once(self, journey):
        calls = journey.database.execute(
            "SELECT task_id,cost_cents FROM provider_calls WHERE job_id=?",
            (master_fixture.JOB_ID,),
        ).fetchall()
        self.assertEqual([tuple(row) for row in calls], [("strict-master-task-1", 7)])
        self.assertEqual(journey.database.execute("SELECT cost_cents FROM jobs").fetchone()[0], 7)
        requests = [r for record in journey.records for r in record["requests"]]
        self.assertEqual(sum(r["method"] == "POST" and r["route"].endswith("/contents/generations/tasks") for r in requests), 1)

    def test_formal_supervisor_dispatches_strict_cli_worker(self):
        with StrictProcessJourney() as journey:
            record = journey.dispatch()
            self.assertEqual(record["http_requests_matched"], 11)
            self.assertNotEqual(record["pid"], os.getpid())
            self.assertEqual(journey.database.execute("SELECT state FROM jobs").fetchone()[0], "COMPOSED")
            self.assert_paid_once(journey)

    def test_corrupt_download_recovers_in_new_worker_without_purchase(self):
        with StrictProcessJourney() as journey:
            first = journey.dispatch("corrupt")
            self.assertEqual(journey.database.execute("SELECT state FROM jobs").fetchone()[0], "FAILED")
            frozen = journey.fixture.frozen_graph_snapshot(journey.database)
            os.environ["STRICT_PROCESS_PHASE"] = "recover"
            journey.service.resume_failed_video(master_fixture.JOB_ID)
            queued = journey.fixture.frozen_graph_snapshot(journey.database)
            with self.assertRaises(ValueError):
                journey.service.resume_failed_video(master_fixture.JOB_ID)
            self.assertEqual(journey.fixture.frozen_graph_snapshot(journey.database), queued)
            second = journey.dispatch("recover")
            self.assertNotEqual(first["pid"], second["pid"])
            self.assertEqual(journey.database.execute("SELECT state FROM jobs").fetchone()[0], "COMPOSED")
            self.assertEqual(journey.fixture.frozen_graph_snapshot(journey.database), frozen)
            self.assert_paid_once(journey)
            shot = journey.database.execute("SELECT video_path FROM shots").fetchone()
            self.assertEqual(hashlib.sha256(Path(shot[0]).read_bytes()).hexdigest(), "5e729fe4dd835115728487f6135f0f415a164cf042b8d4a593e6c6b220917ef6")
            journey.service.review_video(master_fixture.JOB_ID, decision="approved", checks=master_fixture.REVIEW_CHECKS)
            self.assertEqual(journey.database.execute("SELECT state FROM jobs").fetchone()[0], "READY_TO_PUBLISH")


if __name__ == "__main__":
    unittest.main()
