"""Zero-cost regression through the real management-service workflow."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory import db, full_flow_regression  # noqa: E402
from factory.management_api import ManagementService  # noqa: E402


class FullFlowRegressionTests(unittest.TestCase):
    def test_zero_cost_worker_handlers_require_explicit_replay_boundary(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {
                "FACTORY_PROVIDER_MODE": "live",
                "FACTORY_FLOW_GUARD_ZERO_COST_WORKER": "1",
                "FACTORY_DATA_DIR": directory,
                "FACTORY_FLOW_GUARD_MEDIA_ROOT": directory,
            }, clear=False):
                with self.assertRaisesRegex(RuntimeError, "replay mode"):
                    full_flow_regression.build_zero_cost_worker_handlers(database)
            with mock.patch.dict(os.environ, {
                "FACTORY_PROVIDER_MODE": "replay",
                "FACTORY_FLOW_GUARD_ZERO_COST_WORKER": "1",
                "FACTORY_DATA_DIR": directory,
                "FACTORY_FLOW_GUARD_MEDIA_ROOT": directory,
            }, clear=False):
                handlers = full_flow_regression.build_zero_cost_worker_handlers(
                    database
                )
        self.assertIn("master", handlers)
        self.assertIn("local_compose", handlers)
        database.close()

    def test_manual_source_reaches_compiled_graph_without_paid_provider_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "templates").mkdir()
            (root / "templates" / "env.example").write_text(
                "FACTORY_PROVIDER_MODE=live\n"
                "PRODUCTION_EXECUTION_ENGINE=legacy\n",
                encoding="utf-8",
            )
            (root / ".env").write_text(
                "FACTORY_PROVIDER_MODE=replay\n"
                "PRODUCTION_EXECUTION_ENGINE=graph_v2\n"
                "DIALOGUE_AUDIO_MODE=seedance_native\n",
                encoding="utf-8",
            )
            database = sqlite3.connect(":memory:")
            database.row_factory = sqlite3.Row
            database.executescript(db.DDL)
            service = ManagementService(database, root, worker_mode="external")
            scenario = {
                "scenario_id": "manual_three_person_multiscene",
                "source": {
                    "type": "manual",
                    "title": "白天高空车跨场景救援",
                    "content": (
                        "林夏在调度中心接到白天施工现场的高空车故障告警，"
                        "高远在设备场检查备用高空车，周工在施工现场确认支腿传感器异常。"
                        "三人分别发言，调度中心、设备场和施工现场形成连续证据链。"
                        "林夏确认双保险调度，高远完成设备复检，周工最终恢复施工。"
                    ),
                },
            }
            with mock.patch.dict(os.environ, {}, clear=False):
                report = full_flow_regression.run_source_to_compiled(
                    service,
                    scenario,
                    fixture_root=root / "data" / "replay",
                )

            self.assertEqual(report["provider_mode"], "replay")
            self.assertEqual(report["external_cost_cents"], 0)
            self.assertEqual(report["draft_count"], 3)
            self.assertEqual(report["execution_engine"], "graph_v2")
            self.assertTrue(report["production_spec_hash"])
            self.assertGreaterEqual(report["master_count"], 3)
            self.assertEqual(
                report["completed_stages"],
                ["source", "brief", "scripts", "approved", "compiled"],
            )
            self.assertEqual(
                database.execute(
                    "SELECT COUNT(*) FROM provider_calls WHERE cost_cents > 0"
                ).fetchone()[0],
                0,
            )
            database.close()

    def test_real_graph_reaches_ready_to_publish_with_manual_anchor_gate(self):
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
                "PRODUCTION_EXECUTION_ENGINE=graph_v2\n"
                "DIALOGUE_AUDIO_MODE=seedance_native\n"
                "APPEND_CTA=false\n"
                "GEN_THUMBNAIL=false\n"
                "GEN_MARKETING_THUMBNAIL=false\n",
                encoding="utf-8",
            )
            database_path = root / "data" / "factory.db"
            database = sqlite3.connect(str(database_path))
            database.row_factory = sqlite3.Row
            database.executescript(db.DDL)
            service = ManagementService(database, root, worker_mode="external")
            scenario = {
                "scenario_id": "ready_to_publish",
                "source": {
                    "type": "manual",
                    "title": "三人跨场景零费用验收",
                    "content": (
                        "林夏在调度中心收到告警，高远在设备场复检备用高空车，"
                        "周工在白天施工现场隔离故障。三人分别发言并完成复工闭环。"
                    ),
                },
            }
            with mock.patch.dict(os.environ, {
                "FACTORY_DB_PATH": str(database_path),
                "APPEND_CTA": "false",
                "GEN_THUMBNAIL": "false",
                "GEN_MARKETING_THUMBNAIL": "false",
            }, clear=False):
                try:
                    report = full_flow_regression.run_source_to_ready_to_publish(
                        service,
                        scenario,
                        fixture_root=root / "data" / "replay",
                        media_root=root / "data" / "assets",
                    )

                    self.assertEqual(report["job_state"], "READY_TO_PUBLISH")
                    self.assertEqual(report["external_cost_cents"], 0)
                    self.assertTrue(Path(report["final_path"]).is_file())
                    self.assertIn(
                        "ANCHOR_REVIEW_REQUIRED", report["observed_states"]
                    )
                    self.assertEqual(report["failed_nodes"], [])
                    self.assertTrue(report["all_nodes_succeeded"])
                    self.assertEqual(
                        database.execute(
                            "SELECT COUNT(*) FROM provider_calls"
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    database.close()


if __name__ == "__main__":
    unittest.main()
