"""Production shadow audit is read-only and distinguishes bugs from pauses."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory import db  # noqa: E402
from factory.flow_guard.shadow_audit import audit_production_shadow  # noqa: E402


class FlowGuardShadowAuditTests(unittest.TestCase):
    def test_audit_never_mutates_db_and_reports_real_blocker_and_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "factory.db"
            database = sqlite3.connect(str(path))
            database.executescript(db.DDL)
            database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state,execution_engine,"
                "production_spec_json,storyboard) VALUES(?,?,?,?,?,?,?)",
                (
                    "J-SHADOW", "T-SHADOW", "shadow", "PRODUCTION_RUNNING",
                    "graph_v2", "{}", "{}",
                ),
            )
            database.execute(
                "INSERT INTO production_nodes(job_id,node_id,node_type,state,"
                "spec_hash,idempotency_key,next_action) VALUES(?,?,?,?,?,?,?)",
                (
                    "J-SHADOW", "master:1", "master", "ACTION_REQUIRED",
                    "spec", "shadow-master", "reconcile_provider_task",
                ),
            )
            database.commit()
            database.close()
            before = hashlib.sha256(path.read_bytes()).hexdigest()

            report = audit_production_shadow(path, recent_limit=10)

            after = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(before, after)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["audited_job_count"], 1)
            self.assertEqual(report["action_required_job_count"], 1)
            self.assertIn("frozen_spec_missing", report["regression_codes"])
            self.assertEqual(report["write_attempts"], 0)

    def test_missing_production_database_is_skipped_not_fabricated_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            report = audit_production_shadow(Path(directory) / "missing.db")

        self.assertEqual(report["status"], "skipped")
        self.assertEqual(report["skip_reason"], "production_db_missing")
        self.assertEqual(report["audited_job_count"], 0)


if __name__ == "__main__":
    unittest.main()
