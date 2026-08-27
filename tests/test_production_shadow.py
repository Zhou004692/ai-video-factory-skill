"""Production Spec v2 shadow compilation tests."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import db as factory_db, production_shadow  # noqa: E402
from tests.fixtures.production_profiles import load_profile_fixture  # noqa: E402


class ProductionShadowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(factory_db.DDL)

    def tearDown(self):
        self.database.close()
        self.tempdir.cleanup()

    def test_shadow_compile_records_diff_without_provider_work(self):
        storyboard = load_profile_fixture("enhanced_multi_scene")
        # Legacy v1 reads masters while v2 reads the lossless beats.
        storyboard["masters"] = [{
            "idx": index,
            "type": "dialogue",
            "duration": 8,
            "speaker": beat["speaker"],
            "scene": beat["scene"],
            "scene_anchor_key": beat["scene_id"],
            "cast": beat["characters"],
            "performance_beats": [{
                "speaker": beat["speaker"], "dialogue": beat["dialogue"],
            }],
        } for index, beat in enumerate(storyboard["beats"], 1)]
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,?,?)",
            ("J-SHADOW", "T-SHADOW", "影子编译", "STORYBOARDED", json.dumps(storyboard)),
        )
        self.database.commit()

        result = production_shadow.compile_and_compare(
            self.database, "J-SHADOW", self.root,
        )

        self.assertIn(result.status, {"match", "compatible_difference"})
        self.assertIsNotNone(result.v2_spec_hash)
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE job_id='J-SHADOW'"
            ).fetchone()[0],
            0,
        )
        row = self.database.execute(
            "SELECT status,v2_spec_hash FROM production_compilation_runs "
            "WHERE job_id='J-SHADOW'"
        ).fetchone()
        self.assertEqual(row["status"], result.status)
        self.assertEqual(row["v2_spec_hash"], result.v2_spec_hash)
        self.assertEqual(
            self.database.execute(
                "SELECT production_spec_version FROM jobs WHERE job_id='J-SHADOW'"
            ).fetchone()[0],
            0,
        )

    def test_shadow_internal_error_is_recorded_not_raised(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,?,?)",
            ("J-BAD", "T-BAD", "损坏输入", "STORYBOARDED", "not-json"),
        )
        self.database.commit()
        result = production_shadow.compile_and_compare(
            self.database, "J-BAD", self.root,
        )
        self.assertEqual(result.status, "compiler_internal_error")
        self.assertTrue(result.error)


if __name__ == "__main__":
    unittest.main()
