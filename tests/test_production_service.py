"""Production application-service boundary tests."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import db as factory_db, production_planner  # noqa: E402
from factory.management_api import ManagementService  # noqa: E402
from factory.services import ProductionReadModel, ProductionService  # noqa: E402
from tests.fixtures.production_profiles import load_profile_fixture  # noqa: E402


class ProductionServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "data").mkdir()
        (self.root / ".env").write_text("", encoding="utf-8")
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(factory_db.DDL)
        source = load_profile_fixture("stable_single_dialogue")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,?,?)",
            (
                "J-SERVICE", "T-SERVICE", "service", "STORYBOARDED",
                json.dumps(source),
            ),
        )
        self.database.commit()

    def tearDown(self):
        self.database.close()
        self.tempdir.cleanup()

    def test_compile_assign_and_read_graph_stay_inside_production_boundary(self):
        service = ProductionService(self.database, self.root)

        result = service.assign_execution_engine("J-SERVICE", "graph_v2")
        graph = ProductionReadModel(self.database).graph_detail("J-SERVICE")

        self.assertTrue(result["compilable"])
        self.assertEqual(result["execution_engine"], "graph_v2")
        self.assertTrue(graph["nodes"])

    def test_v2_spec_is_the_authoritative_budget_and_master_preflight(self):
        legacy = {
            "total_masters": 3,
            "paid_master_count": 3,
            "estimated_paid_calls": 8,
            "estimated_cost_cents": 6868,
            "current_cost_cents": 0,
            "projected_total_cost_cents": 6868,
            "budget_cents": 6000,
            "budget_ok": False,
            "paid_masters": [{"master_idx": index} for index in (1, 2, 3)],
            "paid_identity_anchor_count": 3,
            "identity_anchor_cost_cents": 816,
            "paid_scene_anchor_count": 2,
            "scene_anchor_cost_cents": 544,
            "high_complexity_master_indexes": [1, 2, 3],
            "requires_master_feasibility_confirmation": True,
        }
        spec = {
            "version": 2,
            "spec_hash": "v2-frozen",
            "mode": "experimental",
            "reliability_commitment": "best_effort",
            "complexity": {"score": 88},
            "budget_cents": 6000,
            "masters": [
                {
                    "idx": 1,
                    "editorial_duration_sec": 17,
                    "generation_duration_sec": 18,
                },
                {
                    "idx": 2,
                    "editorial_duration_sec": 16.5,
                    "generation_duration_sec": 18,
                },
            ],
            "nodes": [
                *[
                    {
                        "node_id": f"character_anchor:{name}",
                        "node_type": "character_anchor",
                        "paid": True,
                        "estimated_cost_cents": 272,
                        "input": {"character_id": name},
                    }
                    for name in ("lin_xia", "zhou_gong", "malik")
                ],
                *[
                    {
                        "node_id": f"scene_anchor:{name}",
                        "node_type": "scene_anchor",
                        "paid": True,
                        "estimated_cost_cents": 272,
                        "input": {"scene_key": name},
                    }
                    for name in ("dispatch", "site")
                ],
                {
                    "node_id": "master:1",
                    "node_type": "master",
                    "paid": True,
                    "estimated_cost_cents": 1156,
                    "input": {"master_idx": 1},
                },
                {
                    "node_id": "master:2",
                    "node_type": "master",
                    "paid": True,
                    "estimated_cost_cents": 1836,
                    "input": {"master_idx": 2},
                },
                {
                    "node_id": "local_compose",
                    "node_type": "local_compose",
                    "paid": False,
                    "estimated_cost_cents": 0,
                    "input": {},
                },
            ],
        }

        aligned = production_planner.align_preflight_with_compiled_spec(
            legacy, spec
        )

        self.assertEqual(aligned["preflight_authority"], "production_spec_v2")
        self.assertEqual(aligned["spec_hash"], "v2-frozen")
        self.assertEqual(aligned["total_masters"], 2)
        self.assertEqual(aligned["paid_master_count"], 2)
        self.assertEqual(aligned["estimated_paid_calls"], 7)
        self.assertEqual(aligned["estimated_cost_cents"], 4352)
        self.assertEqual(aligned["projected_total_cost_cents"], 4352)
        self.assertEqual(aligned["paid_identity_anchor_count"], 3)
        self.assertEqual(aligned["identity_anchor_cost_cents"], 816)
        self.assertEqual(aligned["paid_scene_anchor_count"], 2)
        self.assertEqual(aligned["scene_anchor_cost_cents"], 544)
        self.assertTrue(aligned["budget_ok"])
        self.assertEqual(
            [item["master_idx"] for item in aligned["paid_masters"]], [1, 2]
        )
        self.assertTrue(
            all(
                item["quality_risk"]["level"] == "high"
                and item["quality_risk"]["recommendation"]
                for item in aligned["paid_masters"]
            )
        )

    def test_graph_v2_management_preflight_uses_persisted_compiled_spec(self):
        production = ProductionService(self.database, self.root)
        assigned = production.assign_execution_engine("J-SERVICE", "graph_v2")

        row = self.database.execute(
            "SELECT production_spec_json FROM jobs WHERE job_id='J-SERVICE'"
        ).fetchone()
        self.assertTrue(row["production_spec_json"])

        view = ManagementService(self.database, self.root).videos()["jobs"][0]

        self.assertEqual(
            view["preflight"]["preflight_authority"], "production_spec_v2"
        )
        self.assertEqual(view["preflight"]["spec_hash"], assigned["spec_hash"])
        self.assertEqual(
            view["preflight"]["estimated_cost_cents"],
            sum(
                int(node["estimated_cost_cents"] or 0)
                for node in view["production_graph_nodes"]
                if node["paid"]
            ),
        )

    def test_management_service_delegates_compile_and_assignment(self):
        production = mock.Mock()
        production.compile.return_value = {"compilable": True}
        production.assign_execution_engine.return_value = {
            "execution_engine": "graph_v2"
        }
        production.start.return_value = {"status": "queued"}
        production.resume.return_value = {"state": "PRODUCTION_QUEUED"}
        production.review.return_value = {"publish_unlocked": True}
        production.reconcile.return_value = {"state": "PRODUCTION_RUNNING"}
        read_model = mock.Mock()
        read_model.graph_detail.return_value = {"nodes": [], "edges": []}
        api = ManagementService(
            self.database,
            self.root,
            production_service=production,
            production_read_model=read_model,
        )

        api.compile_video_job("J-SERVICE")
        api.assign_execution_engine("J-SERVICE", "graph_v2")
        api.production_graph_detail("J-SERVICE")
        api.start_video_job("J-SERVICE", confirmed=True)
        api.resume_failed_video("J-SERVICE")
        api.review_video("J-SERVICE", decision="approved", checks={})
        api.reconcile_provider_task("J-SERVICE", "remote-1")

        production.compile.assert_called_once_with("J-SERVICE")
        production.assign_execution_engine.assert_called_once_with(
            "J-SERVICE", "graph_v2"
        )
        read_model.graph_detail.assert_called_once_with("J-SERVICE")
        production.start.assert_called_once_with(
            "J-SERVICE",
            confirmed=True,
            approved_budget_cents=None,
            high_risk_confirmed=False,
        )
        production.resume.assert_called_once_with("J-SERVICE")
        production.review.assert_called_once_with(
            "J-SERVICE",
            decision="approved",
            manual_minutes=0,
            notes="",
            first_pass=None,
            checks={},
        )
        production.reconcile.assert_called_once_with("J-SERVICE", "remote-1")

    def test_compile_rebinds_equivalent_paid_graph_without_losing_paid_work(self):
        service = ProductionService(self.database, self.root)
        assigned = service.assign_execution_engine("J-SERVICE", "graph_v2")
        original = self.database.execute(
            "SELECT production_spec_json,production_spec_hash FROM jobs "
            "WHERE job_id='J-SERVICE'"
        ).fetchone()
        paid_node = self.database.execute(
            "SELECT node_id FROM production_nodes WHERE job_id='J-SERVICE' "
            "AND paid=1 ORDER BY node_id LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(paid_node)
        self.database.execute(
            "UPDATE production_nodes SET state='SUCCEEDED',actual_cost_cents=272 "
            "WHERE job_id='J-SERVICE' AND node_id=?",
            (paid_node["node_id"],),
        )
        storyboard = json.loads(self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id='J-SERVICE'"
        ).fetchone()["storyboard"])
        storyboard["beats"][0]["dialogue"] += " changed"
        self.database.execute(
            "UPDATE jobs SET storyboard=? WHERE job_id='J-SERVICE'",
            (json.dumps(storyboard),),
        )
        self.database.commit()

        result = service.compile("J-SERVICE")

        current = self.database.execute(
            "SELECT production_spec_json,production_spec_hash FROM jobs "
            "WHERE job_id='J-SERVICE'"
        ).fetchone()
        graph_hashes = {
            row["spec_hash"] for row in self.database.execute(
                "SELECT spec_hash FROM production_nodes WHERE job_id='J-SERVICE'"
            ).fetchall()
        }
        paid = self.database.execute(
            "SELECT state,actual_cost_cents FROM production_nodes "
            "WHERE job_id='J-SERVICE' AND node_id=?",
            (paid_node["node_id"],),
        ).fetchone()
        self.assertNotEqual(result["spec_hash"], assigned["spec_hash"])
        self.assertNotEqual(current["production_spec_json"], original["production_spec_json"])
        self.assertEqual(current["production_spec_hash"], result["spec_hash"])
        self.assertEqual(graph_hashes, {result["spec_hash"]})
        self.assertEqual(paid["state"], "SUCCEEDED")
        self.assertEqual(paid["actual_cost_cents"], 272)
