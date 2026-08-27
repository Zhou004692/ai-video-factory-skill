"""Versioned deterministic response replay for external provider boundaries."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import provider_replay, provider_runtime  # noqa: E402


class ProviderReplayTests(unittest.TestCase):
    def test_fingerprint_removes_credentials_and_normalizes_paths(self):
        left = provider_replay.fingerprint(
            "video.submit",
            {
                "api_key": "secret-a",
                "nested": {"Authorization": "Bearer A"},
                "image": r"C:\\fixture\\anchor.png",
            },
        )
        right = provider_replay.fingerprint(
            "video.submit",
            {
                "api_key": "secret-b",
                "nested": {"authorization": "Bearer B"},
                "image": "C:/fixture/anchor.png",
            },
        )

        self.assertEqual(left, right)
        self.assertTrue(left.startswith("sha256:"))

    def test_manifest_requires_supported_schema_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(
                provider_replay.ReplayFixtureInvalid,
                "fixture_schema_version",
            ):
                provider_replay.ReplayStore(root)

    def test_lookup_advances_then_repeats_terminal_response(self):
        request = {"task_id": "replay:video:1"}
        request_hash = provider_replay.fingerprint("video.inspect", request)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_manifest(root, [{
                "operation": "video.inspect",
                "request_hash": request_hash,
                "responses": [
                    {"status": "queued"},
                    {"status": "running", "progress": 62},
                    {"status": "succeeded", "media": "media/master-01.mp4"},
                ],
            }])
            media = root / "media" / "master-01.mp4"
            media.parent.mkdir()
            media.write_bytes(b"fixture-video")
            store = provider_replay.ReplayStore(root)

            self.assertEqual(
                store.lookup("video.inspect", request)["status"], "queued"
            )
            self.assertEqual(
                store.lookup("video.inspect", request)["status"], "running"
            )
            terminal = store.lookup("video.inspect", request)
            repeated = store.lookup("video.inspect", request)

        self.assertEqual(terminal["status"], "succeeded")
        self.assertEqual(terminal, repeated)
        self.assertTrue(Path(terminal["media_path"]).is_absolute())
        self.assertEqual(terminal["fixture_id"], "unit-replay")
        self.assertEqual(terminal["mode"], "replay")
        self.assertFalse(terminal["paid"])

    def test_missing_request_fails_closed_with_operation_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_manifest(root, [])
            store = provider_replay.ReplayStore(root)

            with self.assertRaises(provider_runtime.ReplayFixtureMissing) as caught:
                store.lookup("video.submit", {"prompt": "unrecorded"})

        message = str(caught.exception)
        self.assertIn("video.submit", message)
        self.assertIn("sha256:", message)

    @staticmethod
    def _write_manifest(root: Path, operations: list[dict]) -> None:
        (root / "manifest.json").write_text(json.dumps({
            "fixture_schema_version": 1,
            "fixture_id": "unit-replay",
            "operations": operations,
        }), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
