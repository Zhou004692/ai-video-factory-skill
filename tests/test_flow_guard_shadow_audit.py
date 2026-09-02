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

    def test_actionable_business_pause_is_attention_with_recovery_action(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "factory.db"
            database = sqlite3.connect(str(path))
            database.executescript(db.DDL)
            database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state,execution_engine,"
                "production_spec_json,storyboard) VALUES(?,?,?,?,?,?,?)",
                (
                    "J-REVIEW", "T-REVIEW", "review", "PRODUCTION_REVIEW_REQUIRED",
                    "legacy", "{}", "{}",
                ),
            )
            database.execute(
                "INSERT INTO production_nodes(job_id,node_id,node_type,state,"
                "spec_hash,idempotency_key,next_action) VALUES(?,?,?,?,?,?,?)",
                (
                    "J-REVIEW", "master:1", "master", "REVIEW_REQUIRED",
                    "spec", "review-master", "review_candidate",
                ),
            )
            database.commit()
            database.close()

            report = audit_production_shadow(path, recent_limit=10)

        self.assertEqual(report["status"], "attention")
        self.assertEqual(report["actionable_pause_job_count"], 1)
        self.assertEqual(report["unactionable_pause_job_count"], 0)
        self.assertEqual(
            report["jobs"][0]["recovery_action"],
            "review_master_candidates",
        )
        self.assertEqual(report["jobs"][0]["availability_status"], "actionable")

    def test_unknown_pause_action_is_a_blocking_availability_regression(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "factory.db"
            database = sqlite3.connect(str(path))
            database.executescript(db.DDL)
            database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state,execution_engine,"
                "production_spec_json,storyboard) VALUES(?,?,?,?,?,?,?)",
                (
                    "J-DEAD-END", "T-DEAD-END", "dead end",
                    "PROVIDER_ACTION_REQUIRED", "legacy", "{}", "{}",
                ),
            )
            database.execute(
                "INSERT INTO production_nodes(job_id,node_id,node_type,state,"
                "spec_hash,idempotency_key,next_action) VALUES(?,?,?,?,?,?,?)",
                (
                    "J-DEAD-END", "master:1", "master", "ACTION_REQUIRED",
                    "spec", "dead-end-master", "unknown_operator_action",
                ),
            )
            database.commit()
            database.close()

            report = audit_production_shadow(path, recent_limit=10)

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["unactionable_pause_job_count"], 1)
        self.assertIn("production_pause_unactionable", report["regression_codes"])
        self.assertEqual(report["jobs"][0]["availability_status"], "blocked")

    def test_failed_job_without_paused_nodes_is_operational_attention(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "factory.db"
            database = sqlite3.connect(str(path))
            database.executescript(db.DDL)
            database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state,execution_engine,"
                "production_spec_json,storyboard,error) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "J-FAILED", "T-FAILED", "failed", "FAILED", "legacy",
                    "{}", "{}", "generation stopped",
                ),
            )
            database.commit()
            database.close()

            report = audit_production_shadow(path, recent_limit=10)

        self.assertEqual(report["status"], "attention")
        self.assertEqual(report["actionable_pause_job_count"], 1)
        self.assertEqual(report["jobs"][0]["availability_status"], "actionable")
        self.assertEqual(
            report["jobs"][0]["recovery_action"], "preserve_and_diagnose",
        )

    def test_default_shadow_window_does_not_hide_the_thirty_first_job(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "factory.db"
            database = sqlite3.connect(str(path))
            database.executescript(db.DDL)
            database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state,execution_engine,"
                "production_spec_json,storyboard,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    "J-OLDER-FAILED", "T-OLDER", "older failed", "FAILED",
                    "legacy", "{}", "{}", "2026-01-01 00:00:00",
                ),
            )
            for index in range(30):
                database.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,execution_engine,"
                    "production_spec_json,storyboard,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        f"J-NEW-{index:02d}", f"T-NEW-{index:02d}", "new",
                        "PUBLISHED", "legacy", "{}", "{}",
                        f"2026-02-{index % 28 + 1:02d} 00:00:00",
                    ),
                )
            database.commit()
            database.close()

            report = audit_production_shadow(path)

        self.assertEqual(report["audited_job_count"], 31)
        self.assertEqual(report["status"], "attention")
        self.assertIn("J-OLDER-FAILED", {job["job_id"] for job in report["jobs"]})


if __name__ == "__main__":
    unittest.main()
