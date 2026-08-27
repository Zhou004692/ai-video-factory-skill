"""Node projection and legacy reconciliation tests."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import creative_ir, db as factory_db, production_compiler, production_graph, production_projection  # noqa: E402
from tests.fixtures.production_profiles import load_profile_fixture  # noqa: E402


class ProductionProjectionTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(factory_db.DDL)
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) VALUES(?,?,?,?)",
            ("J-E2E", "T-E2E", "投影测试", "PRODUCTION_REVIEW_REQUIRED"),
        )
        source = load_profile_fixture("stable_single_dialogue")
        spec = production_compiler.compile_ir(
            creative_ir.from_storyboard(source), job_id="J-E2E",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
        ).spec
        production_graph.persist_graph(self.database, spec)
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state) VALUES(?,?,?)",
            ("J-E2E", 1, "REVIEW_REQUIRED"),
        )
        self.database.commit()

    def tearDown(self):
        self.database.close()

    def test_projection_matches_existing_job_and_shot_states(self):
        production_projection.mirror_legacy_execution(self.database, "J-E2E")
        self.database.commit()
        report = production_projection.reconcile(self.database, "J-E2E")
        self.assertEqual(report.differences, ())
        self.assertEqual(report.projected_job_state, "PRODUCTION_REVIEW_REQUIRED")
        self.assertEqual(report.next_action, "review_candidate")

    def test_provider_reconciliation_has_highest_priority(self):
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,usage_json) "
            "VALUES(?,?,?,?,?,?)",
            ("remote-1", "J-E2E", 1, "seedance", "generate", '{"status":"unknown"}'),
        )
        self.database.commit()
        report = production_projection.reconcile(self.database, "J-E2E")
        self.assertEqual(report.projected_job_state, "PROVIDER_ACTION_REQUIRED")
        self.assertEqual(report.next_action, "reconcile_provider_task")

    def test_final_compose_success_is_required_for_composed_projection(self):
        self.database.execute(
            "UPDATE jobs SET state='COMPOSED' WHERE job_id='J-E2E'"
        )
        self.database.commit()
        before = production_projection.reconcile(self.database, "J-E2E")
        self.assertNotEqual(before.projected_job_state, "COMPOSED")
        self.database.execute(
            "UPDATE production_nodes SET state='SUCCEEDED' "
            "WHERE job_id='J-E2E' AND node_id='local_compose'"
        )
        self.database.commit()
        after = production_projection.reconcile(self.database, "J-E2E")
        self.assertEqual(after.projected_job_state, "COMPOSED")

    def test_anchor_review_projects_distinct_business_pause(self):
        self.database.execute(
            "UPDATE production_nodes SET state='REVIEW_REQUIRED' "
            "WHERE job_id='J-E2E' AND node_type='character_anchor'"
        )
        self.database.commit()

        report = production_projection.reconcile(self.database, "J-E2E")

        self.assertEqual(report.projected_job_state, "ANCHOR_REVIEW_REQUIRED")
        self.assertEqual(report.next_action, "approve_continuity_anchors")

    def test_failed_anchor_is_not_hidden_by_other_anchor_review(self):
        self.database.execute(
            "UPDATE production_nodes SET state='REVIEW_REQUIRED' "
            "WHERE job_id='J-E2E' AND node_type='character_anchor'"
        )
        self.database.execute(
            "INSERT INTO production_nodes("
            "job_id,node_id,node_type,paid,state,spec_hash,input_json,"
            "idempotency_key,error_code) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "J-E2E", "scene_anchor:single-use-yard", "scene_anchor", 1,
                "FAILED", "spec-a", "{}", "idem-scene-a", "RuntimeError",
            ),
        )
        self.database.commit()

        report = production_projection.reconcile(self.database, "J-E2E")

        self.assertEqual(report.projected_job_state, "FAILED")
        self.assertEqual(report.next_action, "preserve_and_diagnose")

    def test_budget_required_projects_explicit_recoverable_business_pause(self):
        self.database.execute(
            "UPDATE production_nodes SET state='BUDGET_REQUIRED',"
            "error_code='budget_confirmation_required',"
            "next_action='increase_job_budget' "
            "WHERE job_id='J-E2E' AND node_type='master'"
        )
        self.database.commit()

        report = production_projection.reconcile(self.database, "J-E2E")

        self.assertEqual(
            report.projected_job_state, "BUDGET_CONFIRMATION_REQUIRED"
        )
        self.assertEqual(report.next_action, "increase_job_budget")

    def test_published_job_is_not_regressed_by_projection(self):
        self.database.execute(
            "UPDATE production_nodes SET state='SUCCEEDED' "
            "WHERE job_id='J-E2E' AND node_id IN ('local_compose','final_review')"
        )
        self.database.execute(
            "UPDATE jobs SET state='PUBLISHED' WHERE job_id='J-E2E'"
        )
        self.database.commit()

        report = production_projection.reconcile(self.database, "J-E2E")

        self.assertEqual(report.projected_job_state, "PUBLISHED")
        self.assertEqual(report.next_action, "view_publication")
        self.assertEqual(report.differences, ())


if __name__ == "__main__":
    unittest.main()
