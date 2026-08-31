"""Safety-contract tests for the zero-cost release gate."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from factory import provider_runtime  # noqa: E402
from tests import zero_cost_release_gate  # noqa: E402
from tests.management_workflow_browser_check import (  # noqa: E402
    _signature as management_signature,
)


class ZeroCostReleaseGateTests(unittest.TestCase):
    def test_unknown_replay_request_fails_before_socket_open(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = Path(directory)
            (fixture_root / "manifest.json").write_text(json.dumps({
                "fixture_schema_version": 1,
                "fixture_id": "release-gate-missing-request",
                "operations": [],
            }), encoding="utf-8")
            runtime = provider_runtime.ProviderRuntime(
                mode="replay",
                fixture_root=fixture_root,
            )

            with mock.patch.object(socket.socket, "connect") as connect:
                with self.assertRaises(provider_runtime.ReplayFixtureMissing):
                    runtime.lookup(
                        "video.submit",
                        {"prompt": "request-without-a-recorded-fixture"},
                    )

            connect.assert_not_called()

    def test_tree_signature_detects_content_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "assets" / "final.mp4"
            nested.parent.mkdir(parents=True)
            nested.write_bytes(b"first")
            before = zero_cost_release_gate.tree_signature(root)

            nested.write_bytes(b"second")
            after = zero_cost_release_gate.tree_signature(root)

            self.assertNotEqual(before, after)

    def test_management_production_guard_signs_asset_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "assets"
            first = root / "job-1" / "final.mp4"
            second = root / "job-2" / "cover.jpg"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_bytes(b"video")
            second.write_bytes(b"cover")

            before = management_signature(root)
            second.write_bytes(b"changed-cover")
            after = management_signature(root)

            self.assertIsNotNone(before)
            self.assertNotEqual(before, after)

    def test_summary_contract_is_machine_readable(self):
        summary = zero_cost_release_gate.build_summary(
            status="passed",
            paid_provider_calls=0,
            external_requests=0,
            scenarios=10,
            invariant_issues=0,
        )

        self.assertEqual(json.loads(json.dumps(summary)), {
            "status": "passed",
            "paid_provider_calls": 0,
            "external_requests": 0,
            "scenarios": 10,
            "invariant_issues": 0,
        })

    def test_r4_runs_the_continuous_flow_guard_contract(self):
        commands = zero_cost_release_gate.LEVEL_COMMANDS["r4"]

        flattened = " ".join(part for command in commands for part in command)
        self.assertIn("tests.test_flow_guard_scenarios", flattened)
        self.assertIn("tests.test_flow_guard_diagnostics", flattened)
        self.assertIn("tests.test_flow_guard_management_actions", flattened)
        self.assertIn("scripts/flow_guard.py", flattened)
        self.assertIn("--max-scenarios 6", flattened)


if __name__ == "__main__":
    unittest.main()
