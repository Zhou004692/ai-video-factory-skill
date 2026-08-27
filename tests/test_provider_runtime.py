"""Fail-closed selection of live versus zero-cost provider execution."""
from __future__ import annotations

import os
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import provider_runtime  # noqa: E402
from factory.provider_replay import fingerprint  # noqa: E402


class ProviderRuntimeTests(unittest.TestCase):
    def test_live_mode_is_the_default(self):
        environment = dict(os.environ)
        environment.pop("FACTORY_PROVIDER_MODE", None)
        environment.pop("FACTORY_REPLAY_FIXTURE_ROOT", None)
        with mock.patch.dict(os.environ, environment, clear=True):
            runtime = provider_runtime.from_environment()

        self.assertEqual(runtime.mode, "live")
        self.assertIsNone(runtime.fixture_root)

    def test_replay_mode_never_falls_back_when_fixture_root_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "does-not-exist"
            with mock.patch.dict(os.environ, {
                "FACTORY_PROVIDER_MODE": "replay",
                "FACTORY_REPLAY_FIXTURE_ROOT": str(missing),
            }, clear=False):
                runtime = provider_runtime.from_environment()

            self.assertEqual(runtime.mode, "replay")
            with self.assertRaisesRegex(
                provider_runtime.ReplayFixtureMissing,
                "fixture root does not exist",
            ):
                runtime.lookup("video.submit", {"prompt": "unknown"})

    def test_unknown_provider_mode_is_rejected(self):
        with mock.patch.dict(os.environ, {
            "FACTORY_PROVIDER_MODE": "sometimes-live",
        }, clear=False):
            with self.assertRaisesRegex(ValueError, "FACTORY_PROVIDER_MODE"):
                provider_runtime.from_environment()

    def test_replay_sequence_survives_runtime_reconstruction(self):
        request = {"task_id": "replay:video:sequence"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(json.dumps({
                "fixture_schema_version": 1,
                "fixture_id": "runtime-sequence",
                "operations": [{
                    "operation": "video.inspect",
                    "request_hash": fingerprint("video.inspect", request),
                    "responses": [
                        {"status": "queued"},
                        {"status": "succeeded"},
                    ],
                }],
            }), encoding="utf-8")
            environment = {
                "FACTORY_PROVIDER_MODE": "replay",
                "FACTORY_REPLAY_FIXTURE_ROOT": str(root),
            }

            first = provider_runtime.from_environment(environment).lookup(
                "video.inspect", request
            )
            second = provider_runtime.from_environment(environment).lookup(
                "video.inspect", request
            )

        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
