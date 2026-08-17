from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory import db, management_api  # noqa: E402
from factory.management_api import ManagementService  # noqa: E402
from tests.fixtures.management_factory import (  # noqa: E402
    FIXTURE_IDS,
    seed_management_factory,
)


class ManagementWorkflowIsolationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows-specific process probe")
    def test_windows_process_probe_never_uses_os_kill(self):
        with mock.patch.object(management_api.os, "kill") as kill:
            self.assertTrue(management_api._process_is_alive(os.getpid()))

        kill.assert_not_called()

    def test_factory_database_path_can_be_overridden_by_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            target = (Path(temp) / "isolated" / "factory-e2e.db").resolve()
            with mock.patch.dict(
                os.environ, {"FACTORY_DB_PATH": str(target)}, clear=False
            ):
                resolver = getattr(db, "database_path", lambda: db._DB_PATH)

                self.assertEqual(resolver(), target)

    def test_factory_connection_initializes_the_overridden_database(self):
        with tempfile.TemporaryDirectory() as temp:
            target = (Path(temp) / "isolated" / "factory-e2e.db").resolve()
            with mock.patch.dict(
                os.environ, {"FACTORY_DB_PATH": str(target)}, clear=False
            ):
                database = db.conn()
                try:
                    attached_path = Path(
                        database.execute("PRAGMA database_list").fetchone()[2]
                    ).resolve()
                    tables = {
                        row[0]
                        for row in database.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                finally:
                    database.close()

            self.assertEqual(attached_path, target)
            self.assertTrue(target.exists())
            self.assertIn("jobs", tables)

    def test_management_factory_covers_the_six_user_facing_stages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "templates").mkdir()
            (root / "templates" / "env.example").write_text(
                "ARK_API_KEY=<required>\n", encoding="utf-8"
            )
            target = (root / "data" / "factory-e2e.db").resolve()
            with mock.patch.dict(
                os.environ, {"FACTORY_DB_PATH": str(target)}, clear=False
            ):
                database = db.conn()
                try:
                    seed_management_factory(database)
                    service = ManagementService(database, root)
                    projects = {
                        item["project_id"]: item
                        for item in service.content_projects()["projects"]
                    }
                    running_pid = database.execute(
                        "SELECT production_worker_pid FROM jobs WHERE job_id='J-E2E-RUNNING'"
                    ).fetchone()[0]
                    automatic_candidates = database.execute(
                        "SELECT COUNT(*) FROM source_candidates WHERE status='candidate'"
                    ).fetchone()[0]
                finally:
                    database.close()

            expected = {
                "source": ("source", "generate_brief"),
                "brief": ("brief", "generate_scripts"),
                "script": ("script", "review_script"),
                "setup": ("production_setup", "configure_video"),
                "running": ("production", "view_progress"),
                "review": ("review", "review_video"),
            }
            self.assertEqual(set(projects), set(FIXTURE_IDS.values()))
            self.assertEqual(running_pid, os.getpid())
            self.assertEqual(automatic_candidates, 1)
            for key, (stage, action) in expected.items():
                project = projects[FIXTURE_IDS[key]]
                self.assertEqual(project["stage"], stage)
                self.assertEqual(project["next_action"], action)


if __name__ == "__main__":
    unittest.main()
