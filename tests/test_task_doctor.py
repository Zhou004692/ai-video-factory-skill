"""Task Doctor diagnostics and zero-cost repair boundary tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import db as factory_db, task_doctor  # noqa: E402
from factory.management_api import ManagementService  # noqa: E402
from management_server import ConsoleHandler  # noqa: E402


class TaskDoctorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "data").mkdir()
        (self.root / "templates").mkdir()
        (self.root / "templates" / "env.example").write_text(
            "", encoding="utf-8"
        )
        (self.root / ".env").write_text("", encoding="utf-8")
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(factory_db.DDL)
        self.doctor = task_doctor.TaskDoctor(self.database, self.root)

    def tearDown(self):
        self.database.close()
        self.tempdir.cleanup()

    def _insert_job(self, job_id: str, *, state: str = "PRODUCTION_RUNNING"):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,execution_engine) "
            "VALUES(?,?,?,?,?)",
            (job_id, f"T-{job_id}", job_id, state, "legacy"),
        )
        self.database.commit()

    def _insert_node(
        self,
        job_id: str,
        node_id: str,
        *,
        state: str,
        heartbeat_at: str | None = None,
        provider_task_id: str | None = None,
        actual_cost_cents: int = 0,
        output: dict | None = None,
        error_code: str | None = None,
    ):
        self.database.execute(
            "INSERT INTO production_nodes("
            "job_id,node_id,node_type,paid,state,spec_hash,input_json,"
            "output_json,idempotency_key,heartbeat_at,provider_task_id,"
            "actual_cost_cents,error_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                node_id,
                "master",
                1,
                state,
                "spec",
                "{}",
                json.dumps(output or {}),
                f"idem-{job_id}-{node_id}",
                heartbeat_at,
                provider_task_id,
                actual_cost_cents,
                error_code,
            ),
        )
        self.database.commit()

    def test_diagnose_and_repair_expired_lease_without_paid_submit(self):
        self._insert_job("J-LEASE")
        expired = datetime.now(timezone.utc) - timedelta(minutes=10)
        self._insert_node(
            "J-LEASE", "master:1", state="RUNNING",
            heartbeat_at=expired.isoformat(),
        )

        report = self.doctor.diagnose_video("J-LEASE")

        self.assertEqual(report["status"], "repairable")
        self.assertIn(
            "recover_expired_leases",
            {item["id"] for item in report["safe_actions"]},
        )
        result = self.doctor.repair_video("J-LEASE")
        node = self.database.execute(
            "SELECT state,lease_id,heartbeat_at FROM production_nodes "
            "WHERE job_id='J-LEASE' AND node_id='master:1'"
        ).fetchone()
        self.assertEqual(node["state"], "READY")
        self.assertIsNone(node["lease_id"])
        self.assertIsNone(node["heartbeat_at"])
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE job_id='J-LEASE'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(result["after"]["status"], "healthy")
        self.assertTrue(result["audit_id"])

        second = self.doctor.repair_video("J-LEASE")
        self.assertEqual(second["applied_actions"], [])
        self.assertEqual(second["status"], "no_change")

    def test_unresolved_provider_task_is_never_auto_retried(self):
        self._insert_job("J-REMOTE", state="PROVIDER_ACTION_REQUIRED")
        self._insert_node(
            "J-REMOTE", "master:1", state="ACTION_REQUIRED",
            provider_task_id="remote-123",
        )
        self.database.execute(
            "INSERT INTO provider_calls("
            "task_id,job_id,shot_idx,provider,intent,usage_json,cost_cents) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "remote-123", "J-REMOTE", 1, "seedance", "generate",
                json.dumps({"status": "action_required"}), 0,
            ),
        )
        self.database.commit()

        report = self.doctor.diagnose_video("J-REMOTE")

        self.assertEqual(report["status"], "external_blocked")
        self.assertEqual(report["safe_actions"], [])
        self.assertTrue(report["external_actions"])
        before = self.database.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE job_id='J-REMOTE'"
        ).fetchone()[0]
        result = self.doctor.repair_video("J-REMOTE")
        after = self.database.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE job_id='J-REMOTE'"
        ).fetchone()[0]
        self.assertEqual(result["status"], "no_change")
        self.assertEqual(before, after)

    def test_expired_remote_node_moves_to_external_review_not_retry(self):
        self._insert_job("J-REMOTE-LEASE")
        expired = datetime.now(timezone.utc) - timedelta(minutes=10)
        self._insert_node(
            "J-REMOTE-LEASE", "master:1", state="RUNNING",
            heartbeat_at=expired.isoformat(), provider_task_id="remote-lease-1",
        )

        repaired = self.doctor.repair_video("J-REMOTE-LEASE")

        node = self.database.execute(
            "SELECT state,provider_task_id FROM production_nodes "
            "WHERE job_id='J-REMOTE-LEASE' AND node_id='master:1'"
        ).fetchone()
        self.assertEqual(node["state"], "ACTION_REQUIRED")
        self.assertEqual(node["provider_task_id"], "remote-lease-1")
        self.assertEqual(repaired["after"]["status"], "external_blocked")
        self.assertEqual(repaired["after"]["safe_actions"], [])
        self.assertTrue(repaired["after"]["external_actions"])

    def test_state_projection_is_reconciled_after_local_recovery(self):
        self._insert_job("J-PROJECTION", state="FAILED")
        self._insert_node("J-PROJECTION", "master:1", state="READY")

        report = self.doctor.diagnose_video("J-PROJECTION")

        self.assertIn(
            "reconcile_state_projection",
            {item["id"] for item in report["safe_actions"]},
        )
        repaired = self.doctor.repair_video("J-PROJECTION")
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-PROJECTION'"
            ).fetchone()[0],
            "PRODUCTION_QUEUED",
        )
        self.assertEqual(repaired["after"]["status"], "healthy")

    def test_management_service_exposes_doctor_and_audit_history(self):
        self._insert_job("J-SERVICE", state="FAILED")
        self._insert_node("J-SERVICE", "master:1", state="READY")
        service = ManagementService(
            self.database, self.root, worker_mode="external"
        )

        diagnosis = service.diagnose_video_task("J-SERVICE")
        result = service.repair_video_task("J-SERVICE")
        refreshed = service.diagnose_video_task("J-SERVICE")

        self.assertEqual(diagnosis["status"], "repairable")
        self.assertEqual(result["after"]["status"], "healthy")
        self.assertEqual(len(refreshed["audit_history"]), 1)
        self.assertEqual(refreshed["audit_history"][0]["status"], "repaired")

    def test_management_server_exposes_diagnose_and_safe_repair_routes(self):
        service = mock.Mock()
        service.diagnose_video_task.return_value = {
            "entity_id": "J-HTTP", "status": "repairable"
        }
        service.repair_video_task.return_value = {
            "audit_id": "doctor-http", "status": "repaired"
        }
        ConsoleHandler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            response = request.urlopen(
                f"{base_url}/api/videos/J-HTTP/doctor"
            )
            diagnosis = json.loads(response.read().decode("utf-8"))
            response = request.urlopen(request.Request(
                f"{base_url}/api/videos/J-HTTP/doctor/repair",
                data=json.dumps({
                    "action_ids": ["recover_expired_leases"]
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            ))
            repaired = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(diagnosis["status"], "repairable")
        self.assertEqual(repaired["status"], "repaired")
        service.diagnose_video_task.assert_called_once_with("J-HTTP")
        service.repair_video_task.assert_called_once_with(
            "J-HTTP", action_ids=["recover_expired_leases"]
        )

    def test_management_ui_has_task_doctor_button_and_result_panel(self):
        source = (
            PROJECT_ROOT / "scripts" / "management_ui" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn("诊断修复", source)
        self.assertIn("openTaskDoctor", source)
        self.assertIn("/doctor/repair", source)
        self.assertIn("执行安全修复", source)


if __name__ == "__main__":
    unittest.main()
