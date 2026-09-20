from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class FreshInstallTests(unittest.TestCase):
    def test_upgrade_failure_restores_env_database_and_character_assets(self):
        from distribute_skill import build_distribution
        from install import install

        with tempfile.TemporaryDirectory() as tmp:
            package = build_distribution(ROOT, Path(tmp) / "dist")
            install(package, create_venv=False, install_dependencies=False)
            (package / "VERSION").write_text("9.9.9-test", encoding="utf-8")
            env_file = package / ".env"
            database = package / "data" / "factory.db"
            character = package / "data" / "character_assets" / "hero" / "01.png"
            character.parent.mkdir(parents=True)
            character.write_bytes(b"original-character")
            env_file.write_text("ARK_API_KEY=original\n", encoding="utf-8")
            original_database = database.read_bytes()

            def corrupt_then_fail(root):
                env_file.write_text("ARK_API_KEY=corrupted\n", encoding="utf-8")
                database.write_bytes(b"corrupted-db")
                character.write_bytes(b"corrupted-character")
                raise RuntimeError("injected migration failure")

            with patch("install._initialize_database", side_effect=corrupt_then_fail):
                with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
                    install(package, create_venv=False, install_dependencies=False)

            self.assertEqual(
                env_file.read_text(encoding="utf-8"), "ARK_API_KEY=original\n"
            )
            self.assertEqual(database.read_bytes(), original_database)
            self.assertEqual(character.read_bytes(), b"original-character")

    def test_successful_upgrade_reports_a_recoverable_snapshot(self):
        from distribute_skill import build_distribution
        from install import install

        with tempfile.TemporaryDirectory() as tmp:
            package = build_distribution(ROOT, Path(tmp) / "dist")
            install(package, create_venv=False, install_dependencies=False)
            (package / "VERSION").write_text("9.9.9-test", encoding="utf-8")
            report = install(package, create_venv=False, install_dependencies=False)

            backup = Path(report["upgrade_backup"])
            self.assertTrue(backup.is_dir())
            self.assertTrue((backup / "manifest.json").is_file())
            self.assertTrue((backup / ".env").is_file())
            self.assertTrue((backup / "factory.db").is_file())

    def test_repeated_startup_on_same_version_does_not_create_upgrade_backup(self):
        from distribute_skill import build_distribution
        from install import install

        with tempfile.TemporaryDirectory() as tmp:
            package = build_distribution(ROOT, Path(tmp) / "dist")
            first = install(package, create_venv=False, install_dependencies=False)
            second = install(package, create_venv=False, install_dependencies=False)

            self.assertIsNone(first["upgrade_backup"])
            self.assertIsNone(second["upgrade_backup"])

    def test_runtime_initializer_honors_external_runtime_data_directory(self):
        from distribute_skill import build_distribution
        from install import install

        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            package = build_distribution(ROOT, temp_root / "dist")
            external_data = temp_root / "persistent-data"
            (package / ".env").write_text(
                f"FACTORY_RUNTIME=desktop\nFACTORY_DATA_DIR={external_data}\n"
                "ARK_API_KEY=<required>\n",
                encoding="utf-8",
            )

            report = install(package, create_venv=False, install_dependencies=False)

            self.assertEqual(Path(report["data_dir"]), external_data.resolve())
            self.assertTrue((external_data / "factory.db").is_file())
            self.assertFalse((package / "data" / "factory.db").exists())

            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import management_server; "
                    "from factory.db import database_path; print(database_path())",
                ],
                cwd=package,
                env={**os.environ, "PYTHONPATH": str(package / "scripts")},
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                Path(result.stdout.strip()), (external_data / "factory.db").resolve()
            )

    def test_runtime_initializer_rejects_unsupported_python_before_writing_files(self):
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

            self.assertEqual(report["status"], "configuration_required")
            self.assertEqual(report["start_page"], "config")
            self.assertEqual(report["paid_provider_calls"], 0)
            self.assertTrue(report["database_initialized"])

    def test_open_management_script_self_initializes_then_routes_configuration(self):
        script = (ROOT / "scripts" / "open_management.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("install.py", script)
        self.assertIn("--start-page", script)
        self.assertIn("config", script)
        self.assertIn("--no-deps", script)
        self.assertIn("/readyz", script)
        self.assertIn('WindowStyle = "Hidden"', script)
        self.assertIn("--no-browser", script)
        self.assertIn("NoBrowser", script)
        self.assertNotIn("Write-Host", script)

        app = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("location.hash", app)
        self.assertIn("navigate(initialPage)", app)


if __name__ == "__main__":
    unittest.main()
