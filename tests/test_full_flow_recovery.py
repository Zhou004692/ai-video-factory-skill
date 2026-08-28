"""Crash-window recovery tests for the zero-cost production workflow."""
from __future__ import annotations

import os
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory import (  # noqa: E402
    creative_ir,
    db,
    production_compiler,
    production_executor,
    production_graph,
    production_graph_runner,
    provider_executor,
    full_flow_regression,
)
from factory.testing import fault_injection  # noqa: E402
from factory.management_api import ManagementService  # noqa: E402
from tests.fixtures.production_profiles import load_profile_fixture  # noqa: E402


class FaultInjectionTests(unittest.TestCase):
    def test_replay_failpoint_crashes_once_and_persists_across_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "factory.db"
            database = sqlite3.connect(database_path)
            database.row_factory = sqlite3.Row
            database.executescript(db.DDL)
            environment = {
                "FACTORY_PROVIDER_MODE": "replay",
                "FACTORY_REPLAY_FAILPOINTS": "after_service_restart",
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                with self.assertRaises(fault_injection.InjectedCrash):
                    fault_injection.hit(
                        database,
                        "after_service_restart",
                        job_id="J-RECOVERY",
                    )
            database.close()

            restarted = sqlite3.connect(database_path)
            restarted.row_factory = sqlite3.Row
            with mock.patch.dict(os.environ, environment, clear=False):
                self.assertFalse(fault_injection.hit(
                    restarted,
                    "after_service_restart",
                    job_id="J-RECOVERY",
                ))
            stats = fault_injection.stats(restarted, job_id="J-RECOVERY")
            self.assertEqual(stats[0]["invocation_count"], 2)
            self.assertEqual(stats[0]["triggered_count"], 1)
            restarted.close()


class ExecutorCrashRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_handler_checkpoint_promotes_node_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "factory.db"
            database = sqlite3.connect(database_path)
            database.row_factory = sqlite3.Row
            database.executescript(db.DDL)
            database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state) VALUES(?,?,?,?)",
                ("J-CHECKPOINT", "T-1", "checkpoint", "PRODUCTION_RUNNING"),
            )
            source = load_profile_fixture("enhanced_multi_scene")
            spec = production_compiler.compile_ir(
                creative_ir.from_storyboard(source),
                job_id="J-CHECKPOINT",
                capabilities=(
                    production_compiler.ProviderCapabilities.seedance_2_5()
                ),
                budget_cents=10_000,
            ).spec
            production_graph.persist_graph(database, spec)
            selected = production_executor.ready_nodes(
                database, "J-CHECKPOINT", configured_limit=1,
            )[0]
            selected_id = str(selected["node_id"])
            environment = {
                "FACTORY_PROVIDER_MODE": "replay",
                "FACTORY_REPLAY_FAILPOINTS": (
                    "after_handler_completion_before_node_update"
                ),
            }

            async def completed_handler(_row):
                return {"artifact_path": "fixture://completed"}

            with mock.patch.dict(os.environ, environment, clear=False):
                with self.assertRaises(fault_injection.InjectedCrash):
                    await production_executor.run_ready_wave(
                        database,
                        "J-CHECKPOINT",
                        configured_limit=1,
                        handlers={str(selected["node_type"]): completed_handler},
                    )
            database.close()

            restarted = sqlite3.connect(database_path)
            restarted.row_factory = sqlite3.Row
            recovered = production_executor.recover_expired_nodes(
                restarted,
                "J-CHECKPOINT",
                now=datetime.now(timezone.utc) + timedelta(minutes=5),
                lease_timeout_sec=60,
            )
            self.assertEqual(recovered[selected_id], "SUCCEEDED")
            row = restarted.execute(
                "SELECT state,output_json FROM production_nodes "
                "WHERE job_id='J-CHECKPOINT' AND node_id=?",
                (selected_id,),
            ).fetchone()
            self.assertEqual(row["state"], "SUCCEEDED")
            self.assertIn("fixture://completed", row["output_json"])
            restarted.close()

    async def test_local_compose_crash_requeues_only_local_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "factory.db"
            database = sqlite3.connect(database_path)
            database.row_factory = sqlite3.Row
            database.executescript(db.DDL)
            database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state) VALUES(?,?,?,?)",
                ("J-COMPOSE", "T-1", "compose", "PRODUCTION_RUNNING"),
            )
            source = load_profile_fixture("enhanced_multi_scene")
            spec = production_compiler.compile_ir(
                creative_ir.from_storyboard(source),
                job_id="J-COMPOSE",
                capabilities=(
                    production_compiler.ProviderCapabilities.seedance_2_5()
                ),
                budget_cents=10_000,
            ).spec
            production_graph.persist_graph(database, spec)
            database.execute(
                "UPDATE production_nodes SET state=CASE "
                "WHEN node_id='local_compose' THEN 'READY' ELSE 'PENDING' END "
                "WHERE job_id='J-COMPOSE'"
            )
            database.commit()
            calls = 0

            async def compose_handler(_row):
                nonlocal calls
                calls += 1
                return {"final_path": "fixture://final.mp4"}

            environment = {
                "FACTORY_PROVIDER_MODE": "replay",
                "FACTORY_REPLAY_FAILPOINTS": "during_local_compose",
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                with self.assertRaises(fault_injection.InjectedCrash):
                    await production_executor.run_ready_wave(
                        database,
                        "J-COMPOSE",
                        configured_limit=1,
                        handlers={"local_compose": compose_handler},
                    )
            self.assertEqual(calls, 0)
            database.close()

            restarted = sqlite3.connect(database_path)
            restarted.row_factory = sqlite3.Row
            recovered = production_executor.recover_expired_nodes(
                restarted,
                "J-COMPOSE",
                now=datetime.now(timezone.utc) + timedelta(minutes=5),
                lease_timeout_sec=60,
            )
            self.assertEqual(recovered["local_compose"], "READY")
            with mock.patch.dict(os.environ, environment, clear=False):
                wave = await production_executor.run_ready_wave(
                    restarted,
                    "J-COMPOSE",
                    configured_limit=1,
                    handlers={"local_compose": compose_handler},
                )
            self.assertEqual(wave.succeeded, ("local_compose",))
            self.assertEqual(calls, 1)
            restarted.close()


class ProviderBoundaryFailpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_crash_after_remote_task_binding_keeps_reconcile_evidence(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) VALUES(?,?,?,?)",
            ("J-REMOTE", "T-1", "remote", "PRODUCTION_RUNNING"),
        )
        database.commit()
        executor = provider_executor.ProviderExecutor(
            database,
            provider_executor.PaidTaskOwner(
                job_id="J-REMOTE",
                shot_idx=1,
                provider="seedance",
                intent="dialogue_master",
            ),
            estimated_cost_cents=0,
            budget_limit_cents=10_000,
        )

        async def submit(on_task_created):
            on_task_created("replay:remote-master-1")
            return {"status": "succeeded"}

        with mock.patch.dict(os.environ, {
            "FACTORY_PROVIDER_MODE": "replay",
            "FACTORY_REPLAY_FAILPOINTS": "after_provider_submission",
        }, clear=False):
            with self.assertRaises(fault_injection.InjectedCrash):
                await executor.submit_once(submit)

        row = database.execute(
            "SELECT task_id,usage_json,cost_cents FROM provider_calls "
            "WHERE job_id='J-REMOTE'"
        ).fetchone()
        self.assertEqual(row["task_id"], "replay:remote-master-1")
        self.assertEqual(
            json.loads(row["usage_json"])["status"], "action_required"
        )
        self.assertEqual(row["cost_cents"], 0)
        database.close()


class FullFlowRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_graph_resumes_after_node_commit_before_job_projection_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "templates").mkdir()
            (root / "templates" / "env.example").write_text(
                "FACTORY_PROVIDER_MODE=live\n",
                encoding="utf-8",
            )
            (root / ".env").write_text(
                "FACTORY_PROVIDER_MODE=replay\n"
                "PRODUCTION_EXECUTION_ENGINE=graph_v2\n",
                encoding="utf-8",
            )
            database_path = root / "data" / "factory.db"
            environment = {
                "FACTORY_DB_PATH": str(database_path),
                "FACTORY_PROVIDER_MODE": "replay",
                "FACTORY_REPLAY_FAILPOINTS": (
                    "after_node_update_before_job_projection"
                ),
            }
            scenario = {
                "scenario_id": "restart_after_node_commit",
                "source": {
                    "type": "manual",
                    "title": "节点提交后重启",
                    "content": (
                        "林夏在调度中心接到告警，高远在设备场复检备用车，"
                        "周工在施工现场完成故障隔离。"
                    ),
                },
            }
            database = sqlite3.connect(database_path)
            database.row_factory = sqlite3.Row
            database.executescript(db.DDL)
            service = ManagementService(database, root, worker_mode="external")
            with mock.patch.dict(os.environ, environment, clear=False):
                compiled = full_flow_regression.run_source_to_compiled(
                    service,
                    scenario,
                    fixture_root=root / "data" / "replay",
                    asr_enabled=False,
                )
                job_id = str(compiled["job_id"])
                service.start_video_job(
                    job_id,
                    confirmed=True,
                    approved_budget_cents=10_000,
                    high_risk_confirmed=True,
                )
                handlers = full_flow_regression.build_zero_cost_handlers(
                    database, media_root=root / "data" / "assets"
                )
                with self.assertRaises(fault_injection.InjectedCrash):
                    await production_graph_runner.run_graph_job(
                        database,
                        job_id,
                        configured_limit=4,
                        handlers=handlers,
                    )
            database.close()

            restarted = sqlite3.connect(database_path)
            restarted.row_factory = sqlite3.Row
            restarted_service = ManagementService(
                restarted, root, worker_mode="external"
            )
            del restarted_service
            with mock.patch.dict(os.environ, environment, clear=False):
                handlers = full_flow_regression.build_zero_cost_handlers(
                    restarted, media_root=root / "data" / "assets"
                )
                resumed = await production_graph_runner.run_graph_job(
                    restarted,
                    job_id,
                    configured_limit=4,
                    handlers=handlers,
                )
            self.assertEqual(resumed.job_state, "ANCHOR_REVIEW_REQUIRED")
            self.assertEqual(
                restarted.execute(
                    "SELECT COUNT(*) FROM provider_calls WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                fault_injection.stats(restarted, job_id=job_id)[0][
                    "triggered_count"
                ],
                1,
            )
            restarted.close()

    async def test_recovered_lease_can_crash_once_at_restart_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "templates").mkdir()
            (root / "templates" / "env.example").write_text(
                "FACTORY_PROVIDER_MODE=live\n",
                encoding="utf-8",
            )
            (root / ".env").write_text(
                "FACTORY_PROVIDER_MODE=replay\n"
                "PRODUCTION_EXECUTION_ENGINE=graph_v2\n",
                encoding="utf-8",
            )
            database_path = root / "data" / "factory.db"
            environment = {
                "FACTORY_DB_PATH": str(database_path),
                "FACTORY_PROVIDER_MODE": "replay",
                "FACTORY_REPLAY_FAILPOINTS": "after_service_restart",
            }
            database = sqlite3.connect(database_path)
            database.row_factory = sqlite3.Row
            database.executescript(db.DDL)
            try:
                service = ManagementService(
                    database, root, worker_mode="external"
                )
                with mock.patch.dict(os.environ, environment, clear=False):
                    compiled = full_flow_regression.run_source_to_compiled(
                        service,
                        {
                            "scenario_id": "restart_boundary",
                            "source": {
                                "type": "manual",
                                "title": "恢复租约后继续",
                                "content": (
                                    "林夏调度，高远复检，周工完成现场隔离。"
                                ),
                            },
                        },
                        fixture_root=root / "data" / "replay",
                        asr_enabled=False,
                    )
                    job_id = str(compiled["job_id"])
                    service.start_video_job(
                        job_id,
                        confirmed=True,
                        approved_budget_cents=10_000,
                        high_risk_confirmed=True,
                    )
                    node = database.execute(
                        "SELECT node_id FROM production_nodes "
                        "WHERE job_id=? AND state='READY' ORDER BY node_id LIMIT 1",
                        (job_id,),
                    ).fetchone()
                    stale = datetime.now(timezone.utc) - timedelta(minutes=5)
                    database.execute(
                        "UPDATE production_nodes SET state='RUNNING',"
                        "lease_id='stale-worker',heartbeat_at=? "
                        "WHERE job_id=? AND node_id=?",
                        (stale.isoformat(), job_id, node["node_id"]),
                    )
                    database.commit()
                    handlers = full_flow_regression.build_zero_cost_handlers(
                        database, media_root=root / "data" / "assets"
                    )
                    with self.assertRaises(fault_injection.InjectedCrash):
                        await production_graph_runner.run_graph_job(
                            database,
                            job_id,
                            configured_limit=4,
                            handlers=handlers,
                        )
            finally:
                database.close()

            restarted = sqlite3.connect(database_path)
            restarted.row_factory = sqlite3.Row
            ManagementService(restarted, root, worker_mode="external")
            with mock.patch.dict(os.environ, environment, clear=False):
                resumed = await production_graph_runner.run_graph_job(
                    restarted,
                    job_id,
                    configured_limit=4,
                    handlers=full_flow_regression.build_zero_cost_handlers(
                        restarted, media_root=root / "data" / "assets"
                    ),
                )
            self.assertEqual(resumed.job_state, "ANCHOR_REVIEW_REQUIRED")
            restarted.close()

    async def test_crash_after_provider_completion_still_requires_reconcile(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) VALUES(?,?,?,?)",
            ("J-COMPLETE", "T-1", "complete", "PRODUCTION_RUNNING"),
        )
        database.commit()
        executor = provider_executor.ProviderExecutor(
            database,
            provider_executor.PaidTaskOwner(
                job_id="J-COMPLETE",
                shot_idx=2,
                provider="seedance",
                intent="dialogue_master",
            ),
            estimated_cost_cents=0,
            budget_limit_cents=10_000,
        )

        async def submit(on_task_created):
            on_task_created("replay:remote-master-2")
            return {"status": "succeeded", "artifact": "fixture://master-2"}

        with mock.patch.dict(os.environ, {
            "FACTORY_PROVIDER_MODE": "replay",
            "FACTORY_REPLAY_FAILPOINTS": (
                "after_provider_completion_before_promotion"
            ),
        }, clear=False):
            with self.assertRaises(fault_injection.InjectedCrash):
                await executor.submit_once(submit)

        row = database.execute(
            "SELECT task_id,usage_json,cost_cents FROM provider_calls "
            "WHERE job_id='J-COMPLETE'"
        ).fetchone()
        self.assertEqual(row["task_id"], "replay:remote-master-2")
        self.assertEqual(
            json.loads(row["usage_json"])["status"], "action_required"
        )
        self.assertEqual(row["cost_cents"], 0)
        database.close()


if __name__ == "__main__":
    unittest.main()
