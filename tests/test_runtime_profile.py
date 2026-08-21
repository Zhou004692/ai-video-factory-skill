from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class RuntimeProfileTests(unittest.TestCase):
    def test_desktop_defaults_to_private_local_runtime(self):
        from factory.runtime_profile import resolve_runtime_profile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = resolve_runtime_profile(root=root, environ={})

        self.assertEqual(profile.mode, "desktop")
        self.assertEqual(profile.data_dir, (root / "data").resolve())
        self.assertIsNone(profile.public_base_url)
        self.assertEqual(profile.worker_mode, "child_process")
        self.assertEqual(profile.bind_host, "127.0.0.1")

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


if __name__ == "__main__":
    unittest.main()
