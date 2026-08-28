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


if __name__ == "__main__":
    unittest.main()
