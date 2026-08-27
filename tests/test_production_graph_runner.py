"""End-to-end orchestration tests for the graph_v2 worker loop."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import (  # noqa: E402
    creative_ir, db as factory_db, production_compiler, production_graph,
    production_graph_runner, production_invariants,
)
from factory.services import ProductionService  # noqa: E402
from tests.fixtures.production_profiles import load_profile_fixture  # noqa: E402


class ProductionGraphRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(factory_db.DDL)
        self.database.execute(
            "INSERT INTO jobs("
            "job_id,topic_id,title,state,execution_engine) VALUES(?,?,?,?,?)",
            (
                "J-RUNNER", "T-RUNNER", "graph runner",
                "PRODUCTION_RUNNING", "graph_v2",
            ),
        )
        source = load_profile_fixture("stable_single_dialogue")
        spec = production_compiler.compile_ir(
            creative_ir.from_storyboard(source), job_id="J-RUNNER",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
        ).spec
        ProductionService(
            self.database, PROJECT_ROOT,
        ).persist_compiled_execution(spec)
        self.database.commit()

    def tearDown(self):
        self.database.close()

    async def test_runner_drains_graph_and_projects_final_review(self):
        async def succeed(_row):
            return {"ok": True}

        async def review(_row):
            return production_graph_runner.NodeOutcome.require_review(
                {"review": "final"}
            )

        result = await production_graph_runner.run_graph_job(
            self.database,
            "J-RUNNER",
            configured_limit=8,
            handlers={
                "character_anchor": succeed,
                "scene_anchor": succeed,
                "master": succeed,
                "bgm": succeed,
                "local_compose": succeed,
                "final_review": review,
            },
        )

        self.assertEqual(result.job_state, "COMPOSED")
        self.assertEqual(result.next_action, "review_final_video")
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM production_nodes "
                "WHERE job_id='J-RUNNER' AND node_id='final_review'"
            ).fetchone()[0],
            "REVIEW_REQUIRED",
        )
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-RUNNER'"
            ).fetchone()[0],
            "COMPOSED",
        )

    async def test_runner_rejects_legacy_job_before_any_handler(self):
        self.database.execute(
            "UPDATE jobs SET execution_engine='legacy' WHERE job_id='J-RUNNER'"
        )
        self.database.commit()
        calls = 0

        async def handler(_row):
            nonlocal calls
            calls += 1
            return {"ok": True}

        with self.assertRaises(ValueError):
            await production_graph_runner.run_graph_job(
                self.database, "J-RUNNER", configured_limit=8,
                handlers={"character_anchor": handler},
            )
        self.assertEqual(calls, 0)

    async def test_runner_recovers_repaired_unpaid_contract_node(self):
        anchor = self.database.execute(
            "SELECT node_id FROM production_nodes WHERE job_id='J-RUNNER' "
            "AND node_type='character_anchor' ORDER BY node_id LIMIT 1"
        ).fetchone()
        node_id = str(anchor["node_id"])
        self.database.execute(
            "UPDATE production_nodes SET state='FAILED',"
            "error_code='FrozenSpecMismatchError',actual_cost_cents=0,"
            "provider_task_id=NULL,output_json='{}' "
            "WHERE job_id='J-RUNNER' AND node_id=?",
            (node_id,),
        )
        self.database.execute(
            "INSERT INTO production_node_events("
            "job_id,event_id,node_id,event,from_state,to_state,payload_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "J-RUNNER", "old-contract-failure", node_id, "fail",
                "RUNNING", "FAILED",
                '{"error":"frozen runtime masters do not match compiled master nodes"}',
            ),
        )
        self.database.commit()

        async def succeed(_row):
            return {"ok": True}

        async def review(_row):
            return production_graph_runner.NodeOutcome.require_review(
                {"review": "final"}
            )

        result = await production_graph_runner.run_graph_job(
            self.database,
            "J-RUNNER",
            configured_limit=8,
            handlers={
                "character_anchor": succeed,
                "scene_anchor": succeed,
                "master": succeed,
                "bgm": succeed,
                "local_compose": succeed,
                "final_review": review,
            },
        )

        self.assertEqual(result.recovered_nodes[node_id], "READY")
        self.assertEqual(result.job_state, "COMPOSED")

    async def test_runner_stops_before_handlers_when_frozen_inputs_diverge(self):
        self.database.execute(
            "DELETE FROM shots WHERE job_id='J-RUNNER'"
        )
        self.database.execute(
            "INSERT INTO provider_calls("
            "task_id,job_id,shot_idx,provider,intent,cost_cents) "
            "VALUES(?,?,?,?,?,?)",
            ("paid-master", "J-RUNNER", 1, "seedance", "generate", 100),
        )
        self.database.commit()
        calls = 0

        async def handler(_row):
            nonlocal calls
            calls += 1
            return {"ok": True}

        with self.assertRaises(production_invariants.ProductionInvariantError):
            await production_graph_runner.run_graph_job(
                self.database, "J-RUNNER", configured_limit=8,
                handlers={
                    node_type: handler for node_type in (
                        "character_anchor", "scene_anchor", "master", "bgm",
                        "local_compose", "final_review",
                    )
                },
            )
        self.assertEqual(calls, 0)


if __name__ == "__main__":
    unittest.main()
