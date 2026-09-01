"""Integrated backend proof for action paths not covered by the full journey."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory import db  # noqa: E402
from factory.management_api import ManagementService  # noqa: E402


class FlowGuardManagementActionBackendTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        (self.root / "templates").mkdir()
        (self.root / "templates" / "env.example").write_text(
            "ARK_API_KEY=<required>\n", encoding="utf-8"
        )
        (self.root / ".env").write_text(
            "ARK_API_KEY=mock-only\n", encoding="utf-8"
        )
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(db.DDL)
        self.service = ManagementService(self.database, self.root)

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()
        self.environment.stop()

    def test_execution_contract_repair_persists_queue_and_dispatches_worker(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,"
            "execution_engine,failure_code,error) VALUES(?,?,?,?,?,?,?,?)",
            (
                "J-CONTRACT-REPAIR", "T-1", "contract repair", "FAILED",
                "{}", "graph_v2", "execution_contract_invalid",
                "compiled graph mismatch",
            ),
        )
        self.database.commit()

        report = SimpleNamespace(
            blockers=(), repaired_codes=("mutable_storyboard_stale",)
        )
        with mock.patch(
            "factory.management_api.production_invariants.repair_safe_projections",
            return_value=report,
        ) as repair, mock.patch(
            "factory.management_api.production_executor.retry_repaired_contract_nodes",
            return_value=("master:1",),
        ) as retry, mock.patch.object(
            self.service, "dispatch_production_queue",
            return_value={"dispatched_jobs": ["J-CONTRACT-REPAIR"]},
        ) as dispatch:
            result = self.service.repair_execution_contract(
                "J-CONTRACT-REPAIR"
            )

        row = self.database.execute(
            "SELECT state,failure_code,error FROM jobs WHERE job_id=?",
            ("J-CONTRACT-REPAIR",),
        ).fetchone()
        self.assertEqual(row["state"], "PRODUCTION_QUEUED")
        self.assertIsNone(row["failure_code"])
        self.assertIsNone(row["error"])
        self.assertEqual(result["dispatched_jobs"], ["J-CONTRACT-REPAIR"])
        repair.assert_called_once_with(self.database, "J-CONTRACT-REPAIR")
        retry.assert_called_once_with(self.database, "J-CONTRACT-REPAIR")
        dispatch.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
