from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class FreshInstallTests(unittest.TestCase):
    def test_installer_rejects_unsupported_python_before_writing_files(self):
        from install import install

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ai-video-factory"
            with patch("install.sys.version_info", (3, 10, 14)):
                with self.assertRaisesRegex(RuntimeError, "Python 3.11"):
                    install(root, create_venv=False, install_dependencies=False)
            self.assertFalse(root.exists())

    def test_fresh_package_initializes_local_runtime_without_secrets(self):
        from distribute_skill import build_distribution
        from install import install

        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            package = build_distribution(ROOT, temp_root / "dist")

            report = install(
                package,
                create_venv=False,
                install_dependencies=False,
            )

            self.assertTrue((package / ".env").is_file())
            env_text = (package / ".env").read_text(encoding="utf-8")
            self.assertIn("ARK_API_KEY=<required>", env_text)
            self.assertNotIn("secret-value", env_text)

            database = package / "data" / "factory.db"
            self.assertTrue(database.is_file())
            db = sqlite3.connect(database)
            try:
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            finally:
                db.close()
            self.assertIn("jobs", tables)
            self.assertIn("script_sources", tables)

            windows_launcher = package / "打开 AI 视频工厂.cmd"
            portable_launcher = package / "open-ai-video-factory.sh"
            self.assertTrue(windows_launcher.is_file())
            self.assertTrue(portable_launcher.is_file())
            launcher_text = windows_launcher.read_text(encoding="utf-8")
            portable_text = portable_launcher.read_text(encoding="utf-8")
            self.assertIn("scripts\\open_management.ps1", launcher_text)
            self.assertNotIn("C:\\Users\\", launcher_text)
            self.assertIn("python3", portable_text)
            self.assertNotIn("powershell", portable_text.lower())

            self.assertEqual(report["status"], "configuration_required")
            self.assertEqual(report["start_page"], "config")
            self.assertEqual(report["paid_provider_calls"], 0)
            self.assertTrue(report["database_initialized"])

    def test_open_management_script_self_installs_then_routes_configuration(self):
        script = (ROOT / "scripts" / "open_management.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("install.py", script)
        self.assertIn("--start-page", script)
        self.assertIn("config", script)
        self.assertIn("--no-deps", script)
        self.assertNotIn("Write-Host", script)

        app = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("location.hash", app)
        self.assertIn("navigate(initialPage)", app)


if __name__ == "__main__":
    unittest.main()
