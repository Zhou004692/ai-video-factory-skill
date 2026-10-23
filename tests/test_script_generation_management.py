"""Offline management/HTTP boundaries for persistent candidate generation."""
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from factory import db, script_sources
from factory.management_api import ManagementService, BriefRevisionConflict
from tests.test_script_drafts import _brief_payload
with patch("dotenv.load_dotenv", return_value=False):
    from management_server import ConsoleHandler


class ScriptGenerationManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        (self.root / ".env").write_text("", encoding="utf-8")
        self.database = sqlite3.connect(self.root / "data/factory.db", check_same_thread=False)
        self.database.row_factory = sqlite3.Row
        self.database.executescript(db.DDL)
        self.service = ManagementService(self.database, self.root, worker_mode="external")
        self.lock = threading.RLock()
        self.service.bind_service_lock(self.lock)
        source = script_sources.ingest_text(self.database, "设备租赁客户访谈和流程说明。", title="测试来源")
        self.source_id = source.source_id
        self.brief_id = "BRIEF-async"
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json) VALUES(?,?,?,?)",
            (self.brief_id, "candidate", "测试简报", json.dumps(_brief_payload(), ensure_ascii=False)),
        )
        self.database.execute("INSERT INTO topic_brief_sources(brief_id,source_id) VALUES(?,?)", (self.brief_id, self.source_id))
        self.database.commit()

    def tearDown(self):
        for worker in getattr(self.service, "_script_generation_workers", {}).copy().values():
            worker.join(3)
        self.database.close()
        self.temp.cleanup()

    def start(self, **options):
        self.assertTrue(callable(getattr(self.service, "start_script_generation", None)), "background script generation submission is missing")
        with self.lock:
            return self.service.start_script_generation(self.brief_id, angle_index=0, expected_revision=1, **options)

    def serve(self):
        handler = type("AsyncScriptHandler", (ConsoleHandler,), {
            "service": self.service, "service_lock": self.lock, "runtime_mode": "desktop", "auth_manager": None,
        })
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def http(self, server, method, path, payload=None):
        client = HTTPConnection(*server.server_address, timeout=1)
        try:
            client.request(method, path, body=None if payload is None else json.dumps(payload), headers={"Content-Type": "application/json"})
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def test_submit_persists_run_and_duplicate_does_not_reset_it(self):
        with patch.object(self.service, "_dispatch_script_generation", create=True) as dispatch:
            first = self.start(generation_preferences={"suggested_character_count": 3})
            second = self.start(generation_preferences={"suggested_character_count": 3})
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertTrue(second["cached"])
        self.assertEqual(first["completed_count"], 0)
        self.assertEqual(first["target_count"], 3)
        self.assertFalse(self.database.in_transaction)
        self.assertEqual(dispatch.call_args.args, (first["run_id"],))
        with self.lock:
            listed = self.service.scripts()["generation_runs"]
        self.assertEqual(listed[0]["run_id"], first["run_id"])

    def test_revision_or_preferences_rejected_before_angle_approval(self):
        self.assertTrue(callable(getattr(self.service, "start_script_generation", None)))
        with patch.object(self.service, "_dispatch_script_generation", create=True):
            with self.assertRaises(BriefRevisionConflict):
                self.service.start_script_generation(self.brief_id, angle_index=0, expected_revision=2)
            with self.assertRaises(ValueError):
                self.start(generation_preferences={"suggested_character_count": -1})
        row = self.database.execute("SELECT status,approved_angle_index FROM topic_briefs WHERE brief_id=?", (self.brief_id,)).fetchone()
        self.assertEqual(tuple(row), ("candidate", None))

    def test_real_http_background_submit_and_reads_remain_responsive_during_model(self):
        self.assertTrue(callable(getattr(self.service, "start_script_generation", None)))
        entered, release = threading.Event(), threading.Event()
        calls = []
        def slow_model(*args, **kwargs):
            calls.append(1)
            self.assertFalse(self.database.in_transaction, "model waiting must have no open write transaction")
            entered.set()
            release.wait(5)
            raise TimeoutError("simulated unknown transport outcome")
        server = self.serve()
        try:
            with patch("factory.management_api.script_drafts.doubao_llm.complete_json", side_effect=slow_model):
                status, result = self.http(server, "POST", f"/api/briefs/{self.brief_id}/generate-scripts", {
                    "confirmed": True, "background": True, "angle_index": 0, "expected_revision": 1,
                })
                self.assertEqual(status, 200, result)
                self.assertIn("run_id", result)
                self.assertTrue(entered.wait(2))
                for path in ("/readyz", "/api/scripts", f"/api/script-generation-runs/{result['run_id']}"):
                    status, body = self.http(server, "GET", path)
                    self.assertEqual(status, 200, body)
                duplicate_status, duplicate = self.http(server, "POST", f"/api/briefs/{self.brief_id}/generate-scripts", {
                    "confirmed": True, "background": True, "angle_index": 0, "expected_revision": 1,
                })
                self.assertEqual(duplicate_status, 200)
                self.assertEqual(duplicate["run_id"], result["run_id"])
                self.assertEqual(len(calls), 1)
                release.set()
                for worker in self.service._script_generation_workers.copy().values():
                    worker.join(3)
                status, body = self.http(server, "GET", f"/api/script-generation-runs/{result['run_id']}")
                self.assertEqual(body["status"], "unknown")
                self.assertEqual(len(calls), 1, "transport failure must never auto retry")
        finally:
            release.set()

    def test_http_requires_confirmation_for_submit_and_resume(self):
        server = self.serve()
        status, _ = self.http(server, "POST", f"/api/briefs/{self.brief_id}/generate-scripts", {"background": True})
        self.assertEqual(status, 400)
        status, _ = self.http(server, "POST", "/api/script-generation-runs/missing/resume", {})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
