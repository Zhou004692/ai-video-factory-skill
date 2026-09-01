"""Historical user-visible blockers are explicit Flow Guard evidence."""
from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.flow_guard.incidents import (  # noqa: E402
    HISTORICAL_INCIDENTS,
    run_historical_incident_replays,
)


class FlowGuardIncidentReplayTests(unittest.TestCase):
    def test_manifest_names_user_visible_test_21_to_25_regressions(self):
        self.assertEqual(
            [incident.incident_id for incident in HISTORICAL_INCIDENTS],
            ["test21", "test22", "test23", "test24", "test25"],
        )
        for incident in HISTORICAL_INCIDENTS:
            self.assertTrue(incident.title)
            self.assertTrue(incident.test_selectors)
            self.assertTrue(incident.expected_guards)

    def test_each_incident_runs_as_an_independent_zero_cost_check(self):
        calls = []

        def fake_runner(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        report = run_historical_incident_replays(command_runner=fake_runner)

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["incident_count"], 5)
        self.assertEqual(report["passed"], 5)
        self.assertEqual(len(calls), 5)
        self.assertTrue(all("unittest" in command for command in calls))
        self.assertTrue(all(item["paid_provider_calls"] == 0 for item in report["results"]))

    def test_one_failed_replay_fails_only_that_incident_and_the_suite(self):
        responses = iter((0, 0, 1, 0, 0))

        def fake_runner(*args, **kwargs):
            code = next(responses)
            return SimpleNamespace(
                returncode=code,
                stdout="",
                stderr="contract mismatch" if code else "",
            )

        report = run_historical_incident_replays(command_runner=fake_runner)

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failed"], 1)
        failed = [item for item in report["results"] if item["status"] == "failed"]
        self.assertEqual([item["incident_id"] for item in failed], ["test23"])
        self.assertIn("contract mismatch", failed[0]["stderr_tail"])


if __name__ == "__main__":
    unittest.main()
