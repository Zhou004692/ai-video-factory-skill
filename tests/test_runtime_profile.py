from __future__ import annotations

import os
import inspect
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class RuntimeProfileTests(unittest.TestCase):
    def test_desktop_defaults_to_state_outside_replaceable_skill_directory(self):
        from factory.runtime_profile import resolve_runtime_profile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills" / "ai-video-factory"
            runtime_home = Path(tmp) / "runtime-home"
            profile = resolve_runtime_profile(
                root=root,
                environ={"FACTORY_RUNTIME_HOME": str(runtime_home)},
            )

        self.assertEqual(profile.mode, "desktop")
        self.assertEqual(profile.data_dir, (runtime_home / "data").resolve())
        self.assertFalse(profile.data_dir.is_relative_to(root.resolve()))
        self.assertIsNone(profile.public_base_url)
        self.assertEqual(profile.worker_mode, "child_process")
        self.assertEqual(profile.bind_host, "127.0.0.1")

    def test_desktop_replay_can_disable_background_workers_for_browser_regression(self):
        from factory.runtime_profile import resolve_runtime_profile

        with tempfile.TemporaryDirectory() as tmp:
            profile = resolve_runtime_profile(
                root=Path(tmp),
                environ={
                    "FACTORY_PROVIDER_MODE": "replay",
                    "FACTORY_REPLAY_DISABLE_WORKERS": "1",
                },
            )

        self.assertEqual(profile.mode, "desktop")
        self.assertEqual(profile.worker_mode, "external")

    def test_desktop_live_mode_ignores_replay_worker_override(self):
        from factory.runtime_profile import resolve_runtime_profile

        with tempfile.TemporaryDirectory() as tmp:
            profile = resolve_runtime_profile(
                root=Path(tmp),
                environ={
                    "FACTORY_PROVIDER_MODE": "live",
                    "FACTORY_REPLAY_DISABLE_WORKERS": "1",
                },
            )

        self.assertEqual(profile.worker_mode, "child_process")

    def test_desktop_rejects_non_loopback_bind(self):
        from factory.runtime_profile import RuntimeProfileError, resolve_runtime_profile

        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
            RuntimeProfileError, "本地模式只允许回环地址"
        ):
            resolve_runtime_profile(
                root=Path(tmp),
                environ={"FACTORY_BIND_HOST": "0.0.0.0"},
            )

    def test_server_requires_https_data_dir_and_session_secret(self):
        from factory.runtime_profile import RuntimeProfileError, resolve_runtime_profile

        cases = (
            ({"FACTORY_RUNTIME": "server"}, "FACTORY_PUBLIC_BASE_URL"),
            ({
                "FACTORY_RUNTIME": "server",
                "FACTORY_PUBLIC_BASE_URL": "https://video.example.com",
            }, "FACTORY_DATA_DIR"),
            ({
                "FACTORY_RUNTIME": "server",
                "FACTORY_PUBLIC_BASE_URL": "https://video.example.com",
                "FACTORY_DATA_DIR": "data",
            }, "FACTORY_SESSION_SECRET"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            for environ, missing in cases:
                with self.subTest(missing=missing), self.assertRaisesRegex(
                    RuntimeProfileError, missing
                ):
                    resolve_runtime_profile(root=Path(tmp), environ=environ)

    def test_server_profile_is_serializable_without_secret(self):
        from factory.runtime_profile import resolve_runtime_profile

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "persistent"
            profile = resolve_runtime_profile(
                root=Path(tmp),
                environ={
                    "FACTORY_RUNTIME": "server",
                    "FACTORY_PUBLIC_BASE_URL": "https://video.example.com",
                    "FACTORY_DATA_DIR": str(data_dir),
                    "FACTORY_SESSION_SECRET": "s" * 32,
                    "FACTORY_ADMIN_PASSWORD_HASH": "pbkdf2_sha256$1$salt$digest",
                },
            )
            public = profile.as_public_dict()

        self.assertEqual(profile.worker_mode, "external")
        self.assertEqual(profile.trusted_origins, ("https://video.example.com",))
        self.assertNotIn("secret", repr(public).lower())
        self.assertNotIn("s" * 32, repr(profile))

    def test_server_rejects_relative_data_directory(self):
        from factory.runtime_profile import RuntimeProfileError, resolve_runtime_profile

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeProfileError, "绝对路径"):
                resolve_runtime_profile(
                    root=Path(tmp),
                    environ={
                        "FACTORY_RUNTIME": "server",
                        "FACTORY_PUBLIC_BASE_URL": "https://video.example.com",
                        "FACTORY_DATA_DIR": "relative-data",
                        "FACTORY_SESSION_SECRET": "x" * 32,
                        "FACTORY_ADMIN_PASSWORD_HASH": "hash",
                    },
                )

    def test_database_path_uses_runtime_data_dir_unless_explicitly_overridden(self):
        from factory import db

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "runtime-data"
            with mock.patch.dict(
                os.environ,
                {"FACTORY_DATA_DIR": str(data_dir)},
                clear=False,
            ):
                self.assertEqual(
                    db.database_path(),
                    (data_dir / "factory.db").resolve(),
                )
            explicit = Path(tmp) / "isolated.db"
            with mock.patch.dict(
                os.environ,
                {
                    "FACTORY_DATA_DIR": str(data_dir),
                    "FACTORY_DB_PATH": str(explicit),
                },
                clear=False,
            ):
                self.assertEqual(db.database_path(), explicit.resolve())

    def test_management_service_uses_external_data_and_environment_paths(self):
        from factory.db import conn
        from factory.management_api import ManagementService

        parameters = inspect.signature(ManagementService).parameters
        self.assertIn("data_dir", parameters)
        self.assertIn("env_path", parameters)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "replaceable-skill"
            root.mkdir()
            runtime_home = Path(tmp) / "runtime-home"
            data_dir = runtime_home / "data"
            env_file = runtime_home / ".env"
            env_file.parent.mkdir(parents=True)
            env_file.write_text("ARK_API_KEY=<required>\n", encoding="utf-8")
            database_path = data_dir / "factory.db"
            with mock.patch.dict(
                os.environ,
                {
                    "FACTORY_DB_PATH": str(database_path),
                    "FACTORY_DATA_DIR": str(data_dir),
                },
                clear=False,
            ):
                database = conn()
            try:
                service = ManagementService(
                    database,
                    root,
                    worker_mode="external",
                    data_dir=data_dir,
                    env_path=env_file,
                )
                self.assertEqual(service.data_dir, data_dir.resolve())
                self.assertEqual(service.env_path, env_file.resolve())
                self.assertEqual(
                    service.production_service.env_path,
                    env_file.resolve(),
                )
                service.create_custom_character(
                    character_id="runtime-hero",
                    display_name="Runtime Hero",
                    background_description="External runtime state fixture",
                    gender="female",
                )
                source_image = Path(tmp) / "source.png"
                source_image.write_bytes(b"runtime-image")
                created = service.add_custom_character_image(
                    "runtime-hero", source_image
                )
                stored = service.character_image_file(
                    "runtime-hero", created["image_id"]
                )
                self.assertTrue(stored.is_relative_to(data_dir.resolve()))
                self.assertFalse(stored.is_relative_to(root.resolve()))
                relative = database.execute(
                    "SELECT relative_path FROM character_reference_images "
                    "WHERE image_id=?",
                    (created["image_id"],),
                ).fetchone()[0]
                database.execute(
                    "UPDATE character_reference_images SET relative_path=? "
                    "WHERE image_id=?",
                    (f"data/{relative}", created["image_id"]),
                )
                database.commit()
                self.assertEqual(
                    service.character_image_file(
                        "runtime-hero", created["image_id"]
                    ),
                    stored,
                )
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
