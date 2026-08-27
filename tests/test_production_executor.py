"""Lease, dependency and recovery tests for graph execution."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import (  # noqa: E402
    creative_ir, db as factory_db, production_compiler, production_executor,
    production_graph, provider_executor,
)
from tests.fixtures.production_profiles import load_profile_fixture  # noqa: E402


class ProductionExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(factory_db.DDL)
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) VALUES(?,?,?,?)",
            ("J-EXEC", "T-EXEC", "节点执行", "PRODUCTION_RUNNING"),
        )
        source = load_profile_fixture("enhanced_multi_scene")
        self.spec = production_compiler.compile_ir(
            creative_ir.from_storyboard(source), job_id="J-EXEC",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
        ).spec
        production_graph.persist_graph(self.database, self.spec)

    def tearDown(self):
        self.database.close()

    async def test_independent_anchor_nodes_run_in_one_concurrent_wave(self):
        active = 0
        peak = 0

        async def handler(_row):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"ok": True}

        handlers = {"character_anchor": handler, "scene_anchor": handler}
        result = await production_executor.run_ready_wave(
            self.database, "J-EXEC", configured_limit=8, handlers=handlers,
        )
        self.assertTrue(result.started)
        self.assertGreaterEqual(peak, 2)
        self.assertTrue(all(
            node.startswith(("character_anchor:", "scene_anchor:"))
            for node in result.started
        ))

    async def test_continuity_dependent_master_waits_for_predecessor(self):
        async def handler(_row):
            return {"ok": True}

        anchor_handlers = {"character_anchor": handler, "scene_anchor": handler}
        while production_executor.ready_nodes(
            self.database, "J-EXEC", configured_limit=8,
        ) and any(
            row["node_type"] in anchor_handlers
            for row in production_executor.ready_nodes(
                self.database, "J-EXEC", configured_limit=8,
            )
        ):
            await production_executor.run_ready_wave(
                self.database, "J-EXEC", configured_limit=8,
                handlers=anchor_handlers,
            )
        ready_before = {
            row["node_id"] for row in production_executor.ready_nodes(
                self.database, "J-EXEC", configured_limit=8,
            )
        }
        self.assertIn("master:1", ready_before)
        self.assertNotIn("master:2", ready_before)
        await production_executor.run_ready_wave(
            self.database, "J-EXEC", configured_limit=8,
            handlers={"master": handler},
        )
        ready_after = {
            row["node_id"] for row in production_executor.ready_nodes(
                self.database, "J-EXEC", configured_limit=8,
            )
        }
        self.assertIn("master:2", ready_after)

    async def test_expired_provider_node_is_reconciled_not_resubmitted(self):
        node_id = "character_anchor:linxia"
        stale = datetime.now(timezone.utc) - timedelta(minutes=5)
        self.database.execute(
            "UPDATE production_nodes SET state='RUNNING',lease_id='old',"
            "heartbeat_at=?,provider_task_id='remote-1' "
            "WHERE job_id='J-EXEC' AND node_id=?",
            (stale.isoformat(), node_id),
        )
        self.database.commit()
        result = production_executor.recover_expired_nodes(
            self.database, "J-EXEC", lease_timeout_sec=60,
        )
        self.assertEqual(result[node_id], "ACTION_REQUIRED")
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE job_id='J-EXEC'"
            ).fetchone()[0],
            0,
        )

    async def test_handler_can_pause_node_for_human_review(self):
        async def handler(_row):
            return production_executor.NodeOutcome.require_review(
                {"candidate_path": "candidate.mp4"}
            )

        selected = production_executor.ready_nodes(
            self.database, "J-EXEC", configured_limit=1,
        )[0]
        result = await production_executor.run_ready_wave(
            self.database, "J-EXEC", configured_limit=1,
            handlers={str(selected["node_type"]): handler},
        )

        self.assertEqual(result.review_required, (str(selected["node_id"]),))
        row = self.database.execute(
            "SELECT state,output_json FROM production_nodes "
            "WHERE job_id='J-EXEC' AND node_id=?",
            (selected["node_id"],),
        ).fetchone()
        self.assertEqual(row["state"], "REVIEW_REQUIRED")
        self.assertIn("candidate.mp4", row["output_json"])

    async def test_paid_handler_inherits_frozen_node_owner(self):
        async def handler(row):
            reservation = provider_executor.reserve_paid_submit(
                self.database,
                job_id="J-EXEC",
                shot_idx=9000 + len(str(row["node_id"])),
                provider="seedance",
                intent=str(row["node_id"]),
            )
            provider_executor.release_reservation(self.database, reservation)
            return {"owned": True}

        selected = next(
            row for row in production_executor.ready_nodes(
                self.database, "J-EXEC", configured_limit=8,
            )
            if bool(row["paid"])
        )
        for row in production_executor.ready_nodes(
            self.database, "J-EXEC", configured_limit=8,
        ):
            if row["node_id"] != selected["node_id"]:
                self.database.execute(
                    "UPDATE production_nodes SET state='PENDING' "
                    "WHERE job_id='J-EXEC' AND node_id=?",
                    (row["node_id"],),
                )
        self.database.commit()

        with mock.patch.dict(
            "os.environ", {"FACTORY_REQUIRE_V2_CONTRACT": "1"}
        ):
            result = await production_executor.run_ready_wave(
                self.database, "J-EXEC", configured_limit=1,
                handlers={str(selected["node_type"]): handler},
            )
        self.assertEqual(result.succeeded, (str(selected["node_id"]),))

    async def test_provider_pause_exception_becomes_action_required(self):
        class ProviderPause(RuntimeError):
            pause_state = "PROVIDER_ACTION_REQUIRED"

        async def handler(_row):
            raise ProviderPause("remote task ownership must be reconciled")

        selected = production_executor.ready_nodes(
            self.database, "J-EXEC", configured_limit=1,
        )[0]
        result = await production_executor.run_ready_wave(
            self.database, "J-EXEC", configured_limit=1,
            handlers={str(selected["node_type"]): handler},
        )

        self.assertEqual(result.action_required, (str(selected["node_id"]),))
        self.assertFalse(result.failed)
        row = self.database.execute(
            "SELECT state,error_code,next_action FROM production_nodes "
            "WHERE job_id='J-EXEC' AND node_id=?",
            (selected["node_id"],),
        ).fetchone()
        self.assertEqual(row["state"], "ACTION_REQUIRED")
        self.assertEqual(row["error_code"], "ProviderPause")
        self.assertEqual(row["next_action"], "reconcile_provider_task")

    async def test_quality_pause_exception_becomes_review_required(self):
        class ReviewPause(RuntimeError):
            pause_state = "PRODUCTION_REVIEW_REQUIRED"
            candidate_path = "candidate.mp4"

        async def handler(_row):
            raise ReviewPause("paid candidate needs human review")

        selected = production_executor.ready_nodes(
            self.database, "J-EXEC", configured_limit=1,
        )[0]
        result = await production_executor.run_ready_wave(
            self.database, "J-EXEC", configured_limit=1,
            handlers={str(selected["node_type"]): handler},
        )

        self.assertEqual(result.review_required, (str(selected["node_id"]),))
        self.assertFalse(result.failed)
        row = self.database.execute(
            "SELECT state,output_json,next_action FROM production_nodes "
            "WHERE job_id='J-EXEC' AND node_id=?",
            (selected["node_id"],),
        ).fetchone()
        self.assertEqual(row["state"], "REVIEW_REQUIRED")
        self.assertIn("candidate.mp4", row["output_json"])
        self.assertEqual(row["next_action"], "review_candidate")

    async def test_budget_pause_exception_waits_for_confirmation_without_failure(self):
        class BudgetPause(RuntimeError):
            pause_state = "BUDGET_CONFIRMATION_REQUIRED"
            stable_code = "budget_confirmation_required"
            current_cost_cents = 3857
            next_estimate_cents = 1836
            budget_limit_cents = 6000

        async def handler(_row):
            raise BudgetPause("next paid call exceeds the confirmed job budget")

        selected = production_executor.ready_nodes(
            self.database, "J-EXEC", configured_limit=1,
        )[0]
        result = await production_executor.run_ready_wave(
            self.database, "J-EXEC", configured_limit=1,
            handlers={str(selected["node_type"]): handler},
        )

        self.assertEqual(result.budget_required, (str(selected["node_id"]),))
        self.assertFalse(result.failed)
        row = self.database.execute(
            "SELECT state,error_code,output_json,next_action FROM production_nodes "
            "WHERE job_id='J-EXEC' AND node_id=?",
            (selected["node_id"],),
        ).fetchone()
        self.assertEqual(row["state"], "BUDGET_REQUIRED")
        self.assertEqual(row["error_code"], "budget_confirmation_required")
        self.assertEqual(row["next_action"], "increase_job_budget")
        self.assertIn('"next_estimate_cents": 1836', row["output_json"])

    def test_known_local_anchor_contract_failure_can_be_retried_without_cost(self):
        row = self.database.execute(
            "SELECT node_id FROM production_nodes WHERE job_id='J-EXEC' "
            "AND node_type='scene_anchor' ORDER BY node_id LIMIT 1"
        ).fetchone()
        node_id = str(row["node_id"])
        self.database.execute(
            "UPDATE production_nodes SET state='FAILED',error_code='RuntimeError',"
            "actual_cost_cents=0,provider_task_id=NULL,output_json='{}' "
            "WHERE job_id='J-EXEC' AND node_id=?",
            (node_id,),
        )
        self.database.execute(
            "INSERT INTO production_node_events("
            "job_id,event_id,node_id,event,from_state,to_state,payload_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "J-EXEC", "failed-local-anchor", node_id, "fail", "RUNNING",
                "FAILED", '{"error":"scene anchor is not ready: yard-a"}',
            ),
        )
        self.database.commit()

        recovered = production_executor.retry_repaired_contract_nodes(
            self.database, "J-EXEC"
        )

        self.assertEqual(recovered, (node_id,))
        state = self.database.execute(
            "SELECT state,error_code FROM production_nodes "
            "WHERE job_id='J-EXEC' AND node_id=?",
            (node_id,),
        ).fetchone()
        self.assertEqual(state["state"], "READY")
        self.assertIsNone(state["error_code"])

    def test_anchor_contract_retry_refuses_any_provider_cost_evidence(self):
        row = self.database.execute(
            "SELECT node_id FROM production_nodes WHERE job_id='J-EXEC' "
            "AND node_type='scene_anchor' ORDER BY node_id LIMIT 1"
        ).fetchone()
        node_id = str(row["node_id"])
        self.database.execute(
            "UPDATE production_nodes SET state='FAILED',error_code='RuntimeError',"
            "actual_cost_cents=272,provider_task_id='remote-scene',output_json='{}' "
            "WHERE job_id='J-EXEC' AND node_id=?",
            (node_id,),
        )
        self.database.execute(
            "INSERT INTO production_node_events("
            "job_id,event_id,node_id,event,from_state,to_state,payload_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "J-EXEC", "failed-paid-anchor", node_id, "fail", "RUNNING",
                "FAILED", '{"error":"scene anchor is not ready: yard-a"}',
            ),
        )
        self.database.commit()

        recovered = production_executor.retry_repaired_contract_nodes(
            self.database, "J-EXEC"
        )

        self.assertEqual(recovered, ())
        state = self.database.execute(
            "SELECT state FROM production_nodes WHERE job_id='J-EXEC' "
            "AND node_id=?", (node_id,),
        ).fetchone()["state"]
        self.assertEqual(state, "FAILED")

    def test_local_master_anchor_lookup_failure_can_be_retried_without_cost(self):
        row = self.database.execute(
            "SELECT node_id FROM production_nodes WHERE job_id='J-EXEC' "
            "AND node_type='master' ORDER BY node_id LIMIT 1"
        ).fetchone()
        node_id = str(row["node_id"])
        self.database.execute(
            "UPDATE production_nodes SET state='FAILED',error_code='RuntimeError',"
            "actual_cost_cents=0,provider_task_id=NULL,output_json='{}' "
            "WHERE job_id='J-EXEC' AND node_id=?",
            (node_id,),
        )
        self.database.execute(
            "INSERT INTO production_node_events("
            "job_id,event_id,node_id,event,from_state,to_state,payload_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "J-EXEC", "failed-local-master-anchor", node_id, "fail",
                "RUNNING", "FAILED",
                '{"error":"frozen scene anchor is not ready: yard-a"}',
            ),
        )
        self.database.execute(
            "INSERT INTO production_node_events("
            "job_id,event_id,node_id,event,from_state,to_state,payload_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "J-EXEC", f"J-EXEC:{node_id}:contract-repair-v1", node_id,
                "retry", "FAILED", "READY",
                '{"reason":"implementation_contract_repaired"}',
            ),
        )
        self.database.commit()

        recovered = production_executor.retry_repaired_contract_nodes(
            self.database, "J-EXEC"
        )

        self.assertEqual(recovered, (node_id,))
        state = self.database.execute(
            "SELECT state,error_code FROM production_nodes "
            "WHERE job_id='J-EXEC' AND node_id=?",
            (node_id,),
        ).fetchone()
        self.assertEqual(state["state"], "READY")
        self.assertIsNone(state["error_code"])
        retry_v2 = self.database.execute(
            "SELECT 1 FROM production_node_events WHERE job_id='J-EXEC' "
            "AND event_id=?",
            (f"J-EXEC:{node_id}:contract-repair-v2",),
        ).fetchone()
        self.assertIsNotNone(retry_v2)


if __name__ == "__main__":
    unittest.main()
