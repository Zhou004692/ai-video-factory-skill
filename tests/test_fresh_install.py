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
    def test_windows_launcher_runtime_path_precedence_matches_python(self):
        launcher = (ROOT / "scripts" / "open_management.ps1").read_text(
            encoding="utf-8"
        )
        local = launcher.index("$env:LOCALAPPDATA")
        roaming = launcher.index("$env:APPDATA")
        portable = launcher.index("Join-Path (Split-Path -Parent $root)")
        self.assertLess(local, roaming)
        self.assertLess(roaming, portable)
        self.assertNotIn("$env:USERPROFILE \"AppData\\Local", launcher)

    def test_distribution_replacement_preserves_genuine_legacy_tree_before_delete(self):
        from distribute_skill import build_distribution

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"FACTORY_RUNTIME_HOME": str(Path(tmp) / "runtime-home")},
            clear=False,
        ):
            output_root = Path(tmp) / "distribution"
            legacy = output_root / "ai-video-factory"
            legacy_data = legacy / "data"
            legacy_data.mkdir(parents=True)
            (legacy / ".env").write_text(
                "ARK_API_KEY=legacy-secret\n", encoding="utf-8"
            )
            asset = legacy_data / "assets" / "legacy.mp4"
            asset.parent.mkdir()
            asset.write_bytes(b"legacy-media")
            database = sqlite3.connect(legacy_data / "factory.db")
            try:
                database.execute(
                    "CREATE TABLE legacy_marker(value TEXT NOT NULL,path TEXT)"
                )
                database.execute(
                    "INSERT INTO legacy_marker(value,path) VALUES(?,?)",
                    ("preserved-before-delete", str(asset.resolve())),
                )
                database.commit()
            finally:
                database.close()

            replacement = build_distribution(ROOT, output_root)

            runtime_home = Path(tmp) / "runtime-home"
            external_data = runtime_home / "data"
            migrated = sqlite3.connect(external_data / "factory.db")
            try:
                row = migrated.execute(
                    "SELECT value,path FROM legacy_marker"
                ).fetchone()
            finally:
                migrated.close()
            self.assertEqual(row[0], "preserved-before-delete")
            self.assertTrue(Path(row[1]).samefile(external_data / "assets" / "legacy.mp4"))
            self.assertEqual(
                (runtime_home / ".env").read_text(encoding="utf-8"),
                "ARK_API_KEY=legacy-secret\n",
            )
            self.assertFalse((replacement / ".env").exists())
            self.assertFalse((replacement / "data" / "factory.db").exists())

    def test_bootstrap_migrates_legacy_state_when_runtime_already_exists(self):
        from bootstrap import _venv_python, bootstrap
        from distribute_skill import build_distribution

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "FACTORY_RUNTIME_HOME": str(Path(tmp) / "runtime-home"),
                "FACTORY_DATA_DIR": "",
            },
            clear=False,
        ):
            package = build_distribution(ROOT, Path(tmp) / "distribution")
            (package / ".env").write_text(
                "ARK_API_KEY=legacy-secret\n", encoding="utf-8"
            )
            legacy_data = package / "data"
            legacy_data.mkdir(exist_ok=True)
            database = sqlite3.connect(legacy_data / "factory.db")
            try:
                database.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
                database.execute(
                    "INSERT INTO legacy_marker(value) VALUES ('copied')"
                )
                database.commit()
            finally:
                database.close()
            runtime = _venv_python(package)
            runtime.parent.mkdir(parents=True)
            runtime.write_bytes(b"existing-runtime")

            with patch(
                "bootstrap._run_doctor",
                return_value={"ready": True, "checks": {}, "paid_provider_calls": 0},
            ):
                report = bootstrap(package, profile="script")
                second_report = bootstrap(package, profile="script")

            external_data = Path(tmp) / "runtime-home" / "data"
            copied = sqlite3.connect(external_data / "factory.db")
            try:
                value = copied.execute("SELECT value FROM legacy_marker").fetchone()[0]
            finally:
                copied.close()
            self.assertEqual(value, "copied")
            self.assertEqual(report["paid_provider_calls"], 0)
            self.assertEqual(second_report["paid_provider_calls"], 0)
            self.assertEqual(
                (Path(tmp) / "runtime-home" / ".env").read_text(encoding="utf-8"),
                "ARK_API_KEY=legacy-secret\n",
            )

    def test_existing_in_skill_state_is_copied_to_external_runtime_home(self):
        from distribute_skill import build_distribution
        from install import install

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "FACTORY_RUNTIME_HOME": str(Path(tmp) / "runtime-home"),
                "FACTORY_DATA_DIR": "",
            },
            clear=False,
        ):
            package = build_distribution(ROOT, Path(tmp) / "distribution")
            legacy_env = package / ".env"
            legacy_env.write_text("ARK_API_KEY=legacy-secret\n", encoding="utf-8")
            legacy_data = package / "data"
            legacy_data.mkdir(exist_ok=True)
            legacy_database = legacy_data / "factory.db"
            connection = sqlite3.connect(legacy_database)
            try:
                connection.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
                connection.execute(
                    "INSERT INTO legacy_marker(value) VALUES ('copied')"
                )
                connection.commit()
            finally:
                connection.close()
            legacy_character = legacy_data / "character_assets" / "hero" / "01.png"
            legacy_character.parent.mkdir(parents=True)
            legacy_character.write_bytes(b"legacy-character")

            report = install(package, create_venv=False, install_dependencies=False)

            runtime_home = Path(tmp) / "runtime-home"
            external_env = runtime_home / ".env"
            external_data = runtime_home / "data"
            self.assertIn("env_file", report)
            self.assertEqual(Path(report["env_file"]), external_env.resolve())
            self.assertEqual(Path(report["data_dir"]), external_data.resolve())
            self.assertEqual(
                external_env.read_text(encoding="utf-8"),
                "ARK_API_KEY=legacy-secret\n",
            )
            self.assertEqual(
                (external_data / "character_assets" / "hero" / "01.png").read_bytes(),
                b"legacy-character",
            )
            copied = sqlite3.connect(external_data / "factory.db")
            try:
                value = copied.execute("SELECT value FROM legacy_marker").fetchone()[0]
            finally:
                copied.close()
            self.assertEqual(value, "copied")
            self.assertTrue(legacy_env.is_file())
            self.assertTrue(legacy_database.is_file())

    def test_legacy_database_paths_rebase_only_inside_old_data_root(self):
        from distribute_skill import build_distribution
        from install import install

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"FACTORY_RUNTIME_HOME": str(Path(tmp) / "runtime-home")},
            clear=False,
        ):
            package = build_distribution(ROOT, Path(tmp) / "distribution")
            legacy_data = package / "data"
            legacy_asset = legacy_data / "assets" / "source.mp4"
            legacy_frame = legacy_data / "assets" / "frames" / "001.jpg"
            legacy_asset.parent.mkdir(parents=True, exist_ok=True)
            legacy_frame.parent.mkdir(parents=True)
            legacy_asset.write_bytes(b"legacy-video")
            legacy_frame.write_bytes(b"legacy-frame")
            external_asset = Path(tmp) / "external" / "keep.mp4"
            external_asset.parent.mkdir()
            external_asset.write_bytes(b"external")
            database = sqlite3.connect(legacy_data / "factory.db")
            try:
                database.execute(
                    "CREATE TABLE migration_paths("
                    "id INTEGER PRIMARY KEY, local_path TEXT, payload_json TEXT, "
                    "external_path TEXT)"
                )
                database.execute(
                    "INSERT INTO migration_paths(local_path,payload_json,external_path) "
                    "VALUES(?,?,?)",
                    (
                        str(legacy_asset.resolve()),
                        json.dumps(
                            {
                                "archived_path": str(legacy_asset.resolve()),
                                "frames": [str(legacy_frame.resolve())],
                                "external": str(external_asset.resolve()),
                            }
                        ),
                        str(external_asset.resolve()),
                    ),
                )
                database.commit()
            finally:
                database.close()

            report = install(package, create_venv=False, install_dependencies=False)

            external_data = Path(report["data_dir"])
            migrated = sqlite3.connect(external_data / "factory.db")
            try:
                row = migrated.execute(
                    "SELECT local_path,payload_json,external_path FROM migration_paths"
                ).fetchone()
            finally:
                migrated.close()
            payload = json.loads(row[1])
            self.assertEqual(row[0], str(external_data / "assets" / "source.mp4"))
            self.assertEqual(
                payload["archived_path"],
                str(external_data / "assets" / "source.mp4"),
            )
            self.assertEqual(
                payload["frames"],
                [str(external_data / "assets" / "frames" / "001.jpg")],
            )
            self.assertEqual(payload["external"], str(external_asset.resolve()))
            self.assertEqual(row[2], str(external_asset.resolve()))
            self.assertTrue(Path(row[0]).is_file())
            self.assertTrue(Path(payload["frames"][0]).is_file())

    def test_absolute_data_path_inside_replaced_skill_is_relocated(self):
        from distribute_skill import build_distribution

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "FACTORY_RUNTIME_HOME": str(Path(tmp) / "runtime-home"),
                "FACTORY_DATA_DIR": "",
            },
            clear=False,
        ):
            output_root = Path(tmp) / "distribution"
            legacy = output_root / "ai-video-factory"
            legacy_data = legacy / "custom-absolute-data"
            legacy_data.mkdir(parents=True)
            (legacy / ".env").write_text(
                f"FACTORY_DATA_DIR={legacy_data.resolve()}\n"
                "ARK_API_KEY=legacy-secret\n",
                encoding="utf-8",
            )
            (legacy_data / "assets").mkdir()
            (legacy_data / "assets" / "paid.mp4").write_bytes(b"paid-media")
            database = sqlite3.connect(legacy_data / "factory.db")
            database.execute("CREATE TABLE markers(value TEXT NOT NULL)")
            database.execute("INSERT INTO markers(value) VALUES ('preserved')")
            database.commit()
            database.close()

            build_distribution(ROOT, output_root)

            runtime_home = Path(tmp) / "runtime-home"
            external_data = runtime_home / "data"
            self.assertEqual(
                (external_data / "assets" / "paid.mp4").read_bytes(),
                b"paid-media",
            )
            migrated = sqlite3.connect(external_data / "factory.db")
            try:
                self.assertEqual(
                    migrated.execute("SELECT value FROM markers").fetchone()[0],
                    "preserved",
                )
            finally:
                migrated.close()
            external_env = (runtime_home / ".env").read_text(encoding="utf-8")
            self.assertIn(f"FACTORY_DATA_DIR={external_data.resolve()}", external_env)
            self.assertFalse((legacy / "custom-absolute-data").exists())
            self.assertFalse((legacy / ".env").exists())

    def test_legacy_state_migrates_when_external_data_directory_is_empty(self):
        from distribute_skill import build_distribution
        from install import install

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "FACTORY_RUNTIME_HOME": str(Path(tmp) / "runtime-home"),
                "FACTORY_DATA_DIR": "",
            },
            clear=False,
        ):
            package = build_distribution(ROOT, Path(tmp) / "distribution")
            legacy_data = package / "data"
            legacy_data.mkdir(exist_ok=True)
            legacy_database = legacy_data / "factory.db"
            connection = sqlite3.connect(legacy_database)
            try:
                connection.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
                connection.execute(
                    "INSERT INTO legacy_marker(value) VALUES ('copied')"
                )
                connection.commit()
            finally:
                connection.close()

            external_data = Path(tmp) / "runtime-home" / "data"
            external_data.mkdir(parents=True)

            report = install(package, create_venv=False, install_dependencies=False)

            copied = sqlite3.connect(external_data / "factory.db")
            try:
                value = copied.execute("SELECT value FROM legacy_marker").fetchone()[0]
            finally:
                copied.close()
            self.assertEqual(value, "copied")
            self.assertTrue(report["legacy_state_migrated"]["data"])

    def test_conflicting_populated_legacy_and_external_state_fails_closed(self):
        from distribute_skill import build_distribution
        from install import install

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"FACTORY_RUNTIME_HOME": str(Path(tmp) / "runtime-home")},
            clear=False,
        ):
            package = build_distribution(ROOT, Path(tmp) / "distribution")
            legacy_database = package / "data" / "factory.db"
            legacy_database.parent.mkdir(exist_ok=True)
            connection = sqlite3.connect(legacy_database)
            connection.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
            connection.commit()
            connection.close()
            external_data = Path(tmp) / "runtime-home" / "data"
            external_data.mkdir(parents=True)
            (external_data / "partial-state.txt").write_text(
                "incomplete", encoding="utf-8"
            )

            with self.assertRaisesRegex(RuntimeError, "同时存在"):
                install(package, create_venv=False, install_dependencies=False)

            self.assertTrue(legacy_database.is_file())
            self.assertTrue((external_data / "partial-state.txt").is_file())

    def test_legacy_relative_custom_data_directory_migrates_with_new_semantics(self):
        from distribute_skill import build_distribution
        from install import install

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"FACTORY_RUNTIME_HOME": str(Path(tmp) / "runtime-home")},
            clear=False,
        ):
            package = build_distribution(ROOT, Path(tmp) / "distribution")
            (package / ".env").write_text(
                "FACTORY_DATA_DIR=custom-runtime\nARK_API_KEY=legacy-secret\n",
                encoding="utf-8",
            )
            legacy_data = package / "custom-runtime"
            legacy_data.mkdir()
            hardcoded_character = (
                package / "data" / "character_assets" / "hero" / "01.png"
            )
            hardcoded_character.parent.mkdir(parents=True, exist_ok=True)
            hardcoded_character.write_bytes(b"legacy-character")
            database = sqlite3.connect(legacy_data / "factory.db")
            try:
                database.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
                database.execute(
                    "INSERT INTO legacy_marker(value) VALUES ('custom-copied')"
                )
                database.execute(
                    "CREATE TABLE migration_paths(id INTEGER PRIMARY KEY,path TEXT)"
                )
                database.execute(
                    "INSERT INTO migration_paths(path) VALUES(?)",
                    (str(hardcoded_character.resolve()),),
                )
                database.commit()
            finally:
                database.close()

            report = install(package, create_venv=False, install_dependencies=False)

            external_data = Path(tmp) / "runtime-home" / "custom-runtime"
            self.assertEqual(Path(report["data_dir"]), external_data.resolve())
            copied = sqlite3.connect(external_data / "factory.db")
            try:
                value = copied.execute("SELECT value FROM legacy_marker").fetchone()[0]
                character_path = copied.execute(
                    "SELECT path FROM migration_paths"
                ).fetchone()[0]
            finally:
                copied.close()
            self.assertEqual(value, "custom-copied")
            self.assertTrue(
                Path(character_path).samefile(
                    external_data / "character_assets" / "hero" / "01.png"
                )
            )
            self.assertEqual(Path(character_path).read_bytes(), b"legacy-character")
            self.assertTrue(report["legacy_state_migrated"]["data"])

    def test_skill_replacement_preserves_external_runtime_state(self):
        from distribute_skill import build_distribution
        from install import install

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "FACTORY_RUNTIME_HOME": str(Path(tmp) / "runtime-home"),
                "FACTORY_DATA_DIR": "",
            },
            clear=False,
        ):
            temp_root = Path(tmp)
            output_root = temp_root / "distribution"
            package = build_distribution(ROOT, output_root)
            (package / "VERSION").write_text("3.2.0.0", encoding="utf-8")
            first = install(package, create_venv=False, install_dependencies=False)

            runtime_home = temp_root / "runtime-home"
            self.assertTrue(runtime_home.is_dir())
            env_file = runtime_home / ".env"
            data_dir = runtime_home / "data"
            database = data_dir / "factory.db"
            env_file.write_text("ARK_API_KEY=preserved-secret\n", encoding="utf-8")
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE update_marker(value TEXT NOT NULL)")
                connection.execute(
                    "INSERT INTO update_marker(value) VALUES ('preserved')"
                )
                connection.commit()
            finally:
                connection.close()
            character = data_dir / "character_assets" / "hero" / "01.png"
            character.parent.mkdir(parents=True)
            character.write_bytes(b"preserved-character")

            replacement = build_distribution(ROOT, output_root)
            second = install(
                replacement,
                create_venv=False,
                install_dependencies=False,
            )

            self.assertEqual(Path(first["data_dir"]), data_dir.resolve())
            self.assertEqual(Path(second["data_dir"]), data_dir.resolve())
            self.assertEqual(Path(second["env_file"]), env_file.resolve())
            self.assertFalse((replacement / ".env").exists())
            self.assertFalse((replacement / "data" / "factory.db").exists())
            self.assertEqual(
                env_file.read_text(encoding="utf-8"),
                "ARK_API_KEY=preserved-secret\n",
            )
            self.assertEqual(character.read_bytes(), b"preserved-character")
            recovered = sqlite3.connect(database)
            try:
                marker = recovered.execute(
                    "SELECT value FROM update_marker"
                ).fetchone()[0]
            finally:
                recovered.close()
            self.assertEqual(marker, "preserved")
            self.assertIsNotNone(second["upgrade_backup"])
            self.assertEqual(second["paid_provider_calls"], 0)

    def test_upgrade_failure_restores_env_database_and_character_assets(self):
        from distribute_skill import build_distribution
        from install import install

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"FACTORY_RUNTIME_HOME": str(Path(tmp) / "runtime-home")},
            clear=False,
        ):
            package = build_distribution(ROOT, Path(tmp) / "dist")
            initial = install(package, create_venv=False, install_dependencies=False)
            (package / "VERSION").write_text("9.9.9-test", encoding="utf-8")
            env_file = Path(initial["env_file"])
            data_dir = Path(initial["data_dir"])
            database = data_dir / "factory.db"
            character = data_dir / "character_assets" / "hero" / "01.png"
            character.parent.mkdir(parents=True)
            character.write_bytes(b"original-character")
            env_file.write_text("ARK_API_KEY=original\n", encoding="utf-8")
            original = sqlite3.connect(database)
            try:
                original.execute(
                    "CREATE TABLE upgrade_restore_marker(value TEXT NOT NULL)"
                )
                original.execute(
                    "INSERT INTO upgrade_restore_marker(value) VALUES ('original')"
                )
                original.commit()
            finally:
                original.close()

            def corrupt_then_fail(root, **_kwargs):
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
            restored = sqlite3.connect(database)
            try:
                marker = restored.execute(
                    "SELECT value FROM upgrade_restore_marker"
                ).fetchone()[0]
                integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                restored.close()
            self.assertEqual(marker, "original")
            self.assertEqual(integrity, "ok")
            self.assertEqual(character.read_bytes(), b"original-character")

    def test_successful_upgrade_reports_a_recoverable_snapshot(self):
        from distribute_skill import build_distribution
        from install import install

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"FACTORY_RUNTIME_HOME": str(Path(tmp) / "runtime-home")},
            clear=False,
        ):
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

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"FACTORY_RUNTIME_HOME": str(Path(tmp) / "runtime-home")},
            clear=False,
        ):
            package = build_distribution(ROOT, Path(tmp) / "dist")
            first = install(package, create_venv=False, install_dependencies=False)
            second = install(package, create_venv=False, install_dependencies=False)

            self.assertIsNone(first["upgrade_backup"])
            self.assertIsNone(second["upgrade_backup"])

    def test_runtime_initializer_honors_external_runtime_data_directory(self):
        from distribute_skill import build_distribution
        from install import install

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"FACTORY_RUNTIME_HOME": str(Path(tmp) / "runtime-home")},
            clear=False,
        ):
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

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"FACTORY_RUNTIME_HOME": str(Path(tmp) / "runtime-home")},
            clear=False,
        ):
            root = Path(tmp) / "ai-video-factory"
            with patch("install.sys.version_info", (3, 10, 14)):
                with self.assertRaisesRegex(RuntimeError, "Python 3.11"):
                    install(root, create_venv=False, install_dependencies=False)
            self.assertFalse(root.exists())

    def test_fresh_package_initializes_local_runtime_without_secrets(self):
        from distribute_skill import build_distribution
        from install import install

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"FACTORY_RUNTIME_HOME": str(Path(tmp) / "runtime-home")},
            clear=False,
        ):
            temp_root = Path(tmp)
            package = build_distribution(ROOT, temp_root / "dist")

            report = install(
                package,
                create_venv=False,
                install_dependencies=False,
            )

            env_file = Path(report["env_file"])
            self.assertTrue(env_file.is_file())
            env_text = env_file.read_text(encoding="utf-8")
            self.assertIn("ARK_API_KEY=<required>", env_text)
            self.assertNotIn("secret-value", env_text)

            database = Path(report["data_dir"]) / "factory.db"
            self.assertTrue(database.is_file())
            self.assertFalse((package / ".env").exists())
            self.assertFalse((package / "data" / "factory.db").exists())
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
