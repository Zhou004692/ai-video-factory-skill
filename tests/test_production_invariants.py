"""Authoritative graph-v2 invariants and safe projection repair tests."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import (  # noqa: E402
    creative_ir,
    db as factory_db,
    production_compiler,
    production_invariants,
)
from factory.services import ProductionService  # noqa: E402
from factory.management_api import ManagementService  # noqa: E402
from tests.fixtures.production_profiles import load_profile_fixture  # noqa: E402


class ProductionInvariantTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "data").mkdir()
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(factory_db.DDL)
        source = load_profile_fixture("stable_single_dialogue")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,"
            "execution_engine) VALUES(?,?,?,?,?,?)",
            (
                "J-INVARIANT", "T-INVARIANT", "invariant", "STORYBOARDED",
                json.dumps(source), "graph_v2",
            ),
        )
        spec = production_compiler.compile_ir(
            creative_ir.from_storyboard(source),
            job_id="J-INVARIANT",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
        ).spec
        self.assertIsNotNone(spec)
        ProductionService(
            self.database, self.root,
        ).persist_compiled_execution(spec)
        self.database.commit()

    def tearDown(self):
        self.database.close()
        self.tempdir.cleanup()

    def test_safe_repair_rebinds_mutable_storyboard_to_frozen_masters(self):
        frozen_row = self.database.execute(
            "SELECT production_spec_json,production_spec_hash FROM jobs "
            "WHERE job_id='J-INVARIANT'"
        ).fetchone()
        frozen = json.loads(frozen_row["production_spec_json"])
        stale = json.loads(json.dumps(frozen["runtime_storyboard"]))
        stale["masters"].append({"idx": 99, "duration": 10})
        stale["shots"] = [{
            "idx": 1, "duration": 10,
            "video_master_idx": 99, "audio_master_idx": 99,
        }]
        self.database.execute(
            "UPDATE jobs SET storyboard=? WHERE job_id='J-INVARIANT'",
            (json.dumps(stale),),
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent) "
            "VALUES(?,?,?,?,?)",
            ("paid-1", "J-INVARIANT", 1, "seedance", "generate"),
        )
        self.database.commit()

        report = production_invariants.repair_safe_projections(
            self.database, "J-INVARIANT"
        )

        current = self.database.execute(
            "SELECT storyboard,production_spec_hash FROM jobs "
            "WHERE job_id='J-INVARIANT'"
        ).fetchone()
        storyboard = json.loads(current["storyboard"])
        master_ids = {int(master["idx"]) for master in storyboard["masters"]}
        refs = {
            int(shot[key])
            for shot in storyboard["shots"]
            for key in ("video_master_idx", "audio_master_idx")
        }
        self.assertIn("mutable_storyboard_stale", report.repaired_codes)
        self.assertEqual(master_ids, refs)
        self.assertEqual(current["production_spec_hash"], frozen_row["production_spec_hash"])
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE job_id='J-INVARIANT'"
            ).fetchone()[0],
            1,
        )

    def test_audit_blocks_completed_master_when_media_file_is_missing(self):
        self.database.execute(
            "UPDATE production_nodes SET state='SUCCEEDED' "
            "WHERE job_id='J-INVARIANT' AND node_type='master'"
        )
        self.database.execute(
            "UPDATE shots SET state='OK',video_path=? "
            "WHERE job_id='J-INVARIANT'",
            (str(self.root / "missing.mp4"),),
        )
        self.database.commit()

        report = production_invariants.audit(self.database, "J-INVARIANT")

        self.assertIn(
            "completed_master_file_missing",
            {issue.code for issue in report.blockers},
        )

    def test_safe_repair_rebuilds_unpaid_materialized_master_rows(self):
        expected = self.database.execute(
            "SELECT COUNT(*) FROM production_nodes WHERE job_id='J-INVARIANT' "
            "AND node_type='master'"
        ).fetchone()[0]
        self.database.execute(
            "DELETE FROM shots WHERE job_id='J-INVARIANT'"
        )
        self.database.commit()

        report = production_invariants.repair_safe_projections(
            self.database, "J-INVARIANT"
        )

        self.assertIn(
            "materialized_master_stale", report.repaired_codes,
        )
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM shots WHERE job_id='J-INVARIANT'"
            ).fetchone()[0],
            expected,
        )
        self.assertFalse(report.blockers)

    def test_management_startup_repairs_stale_graph_projection(self):
        frozen = json.loads(self.database.execute(
            "SELECT production_spec_json FROM jobs WHERE job_id='J-INVARIANT'"
        ).fetchone()[0])
        stale = json.loads(json.dumps(frozen["runtime_storyboard"]))
        stale["masters"].append({"idx": 99, "duration": 10})
        stale["shots"] = [{
            "idx": 1, "duration": 10,
            "video_master_idx": 99, "audio_master_idx": 99,
        }]
        self.database.execute(
            "UPDATE jobs SET storyboard=? WHERE job_id='J-INVARIANT'",
            (json.dumps(stale),),
        )
        self.database.commit()

        ManagementService(self.database, self.root, worker_mode="external")

        current = json.loads(self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id='J-INVARIANT'"
        ).fetchone()[0])
        self.assertNotIn(99, {int(item["idx"]) for item in current["masters"]})
        self.assertNotIn(
            99,
            {
                int(shot[key])
                for shot in current["shots"]
                for key in ("video_master_idx", "audio_master_idx")
            },
        )

    def test_management_repair_action_requeues_safe_contract_failure(self):
        self.database.execute(
            "UPDATE jobs SET state='FAILED',"
            "failure_code='execution_contract_invalid',error='safe message' "
            "WHERE job_id='J-INVARIANT'"
        )
        self.database.execute(
            "DELETE FROM shots WHERE job_id='J-INVARIANT'"
        )
        self.database.commit()
        service = ManagementService(
            self.database, self.root, worker_mode="external",
        )

        result = service.repair_execution_contract("J-INVARIANT")

        row = self.database.execute(
            "SELECT state,error,failure_code FROM jobs "
            "WHERE job_id='J-INVARIANT'"
        ).fetchone()
        self.assertTrue(result["recovered"])
        self.assertEqual(row["state"], "PRODUCTION_QUEUED")
        self.assertIsNone(row["error"])
        self.assertIsNone(row["failure_code"])
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM shots WHERE job_id='J-INVARIANT'"
            ).fetchone()[0],
            self.database.execute(
                "SELECT COUNT(*) FROM production_nodes "
                "WHERE job_id='J-INVARIANT' AND node_type='master'"
            ).fetchone()[0],
        )

    def test_management_repair_never_overwrites_paid_master_facts(self):
        self.database.execute(
            "UPDATE jobs SET state='FAILED',"
            "failure_code='execution_contract_invalid',error='safe message' "
            "WHERE job_id='J-INVARIANT'"
        )
        self.database.execute(
            "DELETE FROM shots WHERE job_id='J-INVARIANT'"
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent) "
            "VALUES(?,?,?,?,?)",
            ("paid-master-1", "J-INVARIANT", 1, "seedance", "generate"),
        )
        self.database.commit()
        service = ManagementService(
            self.database, self.root, worker_mode="external",
        )

        with self.assertRaisesRegex(ValueError, "不能自动覆盖"):
            service.repair_execution_contract("J-INVARIANT")

        row = self.database.execute(
            "SELECT state,failure_code FROM jobs WHERE job_id='J-INVARIANT'"
        ).fetchone()
        self.assertEqual(row["state"], "FAILED")
        self.assertEqual(row["failure_code"], "execution_contract_invalid")
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM provider_calls "
                "WHERE job_id='J-INVARIANT'"
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
