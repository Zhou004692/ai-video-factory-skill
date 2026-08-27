"""Graph v2 rollout metric tests."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import db as factory_db, production_metrics  # noqa: E402


class ProductionV2MetricsTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(factory_db.DDL)
        modes = ["stable", "stable", "enhanced", "enhanced", "experimental"]
        for index, mode in enumerate(modes, 1):
            job_id = f"J-V2-{index}"
            self.database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state,execution_engine,cost_cents) "
                "VALUES(?,?,?,?,?,?)",
                (job_id, f"T-{index}", job_id, "READY_TO_PUBLISH", "graph_v2", 100),
            )
            self.database.execute(
                "INSERT INTO production_compilation_runs("
                "job_id,status,v1_spec_hash,v2_spec_hash,mode) VALUES(?,?,?,?,?)",
                (job_id, "match", f"v1-{index}", f"v2-{index}", mode),
            )
            self.database.execute(
                "INSERT INTO review_events(job_id,decision,first_pass) VALUES(?,?,?)",
                (job_id, "approved", 1),
            )
            self.database.execute(
                "INSERT INTO provider_calls("
                "task_id,job_id,shot_idx,provider,intent,cost_cents,usage_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (f"task-{index}", job_id, 1, "seedance", "generate", 100,
                 json.dumps({"status": "completed"})),
            )
        production_metrics.record_engineering_intervention(
            self.database, "J-V2-5", "database_edit",
            description="manual repair during rollout",
        )
        self.database.commit()

    def tearDown(self):
        self.database.close()

    def test_first_pass_success_requires_no_engineering_intervention(self):
        metrics = production_metrics.production_v2_summary(self.database)
        self.assertEqual(metrics["first_pass_usable_rate"], 0.8)
        self.assertEqual(metrics["unknown_stuck_jobs"], 0)
        self.assertEqual(metrics["duplicate_paid_submissions"], 0)
        self.assertEqual(metrics["compilation_success_rate"], 1.0)
        self.assertFalse(metrics["switch_gate"]["eligible"])

    def test_human_budget_and_quality_review_are_not_engineering_interventions(self):
        types = production_metrics.ENGINEERING_INTERVENTION_TYPES
        self.assertNotIn("budget_confirmation", types)
        self.assertNotIn("anchor_review", types)
        self.assertNotIn("quality_review", types)


if __name__ == "__main__":
    unittest.main()
