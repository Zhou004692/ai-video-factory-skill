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


EXPECTED_INCIDENT_IDS = [
    "test21",
    "test22",
    "test23",
    "test24",
    "test25",
    "frozen-runtime-master-drift",
    "equivalent-paid-graph-rebind",
    "missing-candidate-preview",
    "local-recompose-resume",
    "reviewed-budget-node-reconcile",
    "extra-meaningless-master",
    "director-shot-missing-master",
    "dialogue-master-empty-voiceover",
    "unknown-character-or-speaker",
    "edit-shot-exceeds-master",
    "master-dependency-missing",
    "graph-edge-missing-node",
]


class FlowGuardIncidentReplayTests(unittest.TestCase):
    def test_manifest_names_user_visible_and_formal_pipeline_regressions(self):
        self.assertEqual(
            [incident.incident_id for incident in HISTORICAL_INCIDENTS],
            EXPECTED_INCIDENT_IDS,
        )
        for incident in HISTORICAL_INCIDENTS:
            self.assertTrue(incident.title)
            self.assertTrue(incident.failure_signature)
            self.assertTrue(incident.detection_boundary)
            self.assertTrue(incident.test_selectors)
            self.assertTrue(incident.expected_guards)

    def test_each_incident_runs_as_an_independent_zero_cost_check(self):
        calls = []

        def fake_runner(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        report = run_historical_incident_replays(command_runner=fake_runner)

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["incident_count"], len(EXPECTED_INCIDENT_IDS))
        self.assertEqual(report["passed"], len(EXPECTED_INCIDENT_IDS))
        self.assertEqual(len(calls), len(EXPECTED_INCIDENT_IDS))
        self.assertTrue(all("unittest" in command for command in calls))
        self.assertTrue(all(item["paid_provider_calls"] == 0 for item in report["results"]))
        self.assertTrue(all(item["failure_signature"] for item in report["results"]))
        self.assertTrue(all(item["detection_boundary"] for item in report["results"]))
        self.assertEqual(report["manifest_errors"], [])

    def test_one_failed_replay_fails_only_that_incident_and_the_suite(self):
        responses = iter(
            1 if index == 2 else 0
            for index in range(len(EXPECTED_INCIDENT_IDS))
        )

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

    def test_duplicate_or_incomplete_manifest_fails_closed_before_running_tests(self):
        calls = []

        def fake_runner(*args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        duplicated = (HISTORICAL_INCIDENTS[0], HISTORICAL_INCIDENTS[0])
        report = run_historical_incident_replays(
            incidents=duplicated,
            command_runner=fake_runner,
        )

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["passed"], 0)
        self.assertEqual(report["failed"], 2)
        self.assertTrue(report["manifest_errors"])
        self.assertIn("duplicate incident_id: test21", report["manifest_errors"])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
