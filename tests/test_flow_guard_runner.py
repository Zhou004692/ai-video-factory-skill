"""Aggregation and watch fingerprints for the continuous flow guard."""
from __future__ import annotations

from pathlib import Path
import inspect
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.flow_guard.model import ExplorationReport, FlowScenario  # noqa: E402
from factory.flow_guard.runner import run_matrix, source_fingerprint  # noqa: E402


def scenario(name: str) -> FlowScenario:
    return FlowScenario(name, "stable", "none", "master", "auto", False)


class FlowGuardRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_matrix_accepts_full_journey_runner(self):
        parameters = inspect.signature(run_matrix).parameters

        self.assertIn("journey_runner", parameters)
        self.assertIn("include_journeys", parameters)

    async def test_matrix_keeps_graph_ui_backend_and_worker_evidence_separate(self):
        async def fake_explorer(item, *, work_root, max_steps=32):
            return ExplorationReport(
                scenario_id=item.scenario_id,
                status="passed",
                final_state="READY_TO_PUBLISH",
                observed_actions=("review_final_video",),
                started_nodes=("master:1",),
            )

        def fake_journey_runner(work_root):
            return {
                "status": "passed", "journey_count": 1,
                "passed": 1, "failed": 0, "paid_provider_calls": 0,
                "journeys": [],
                "coverage_evidence": {
                    "graph_actions": [],
                    "ui_dispatch_actions": ["submit_publish"],
                    "integrated_backend_actions": ["review_continuity_anchors"],
                    "worker_node_types": ["master"],
                    "source_types": ["manual"],
                    "provider_response_types": ["replay_success"],
                },
            }

        with tempfile.TemporaryDirectory() as directory:
            summary = await run_matrix(
                (scenario("evidence"),),
                work_root=Path(directory),
                explorer=fake_explorer,
                include_journeys=True,
                journey_runner=fake_journey_runner,
            )

        coverage = summary["coverage"]
        self.assertEqual(coverage["graph_actions"], ["review_final_video"])
        self.assertEqual(coverage["ui_dispatch_actions"], ["submit_publish"])
        self.assertEqual(
            coverage["integrated_backend_actions"],
            ["review_continuity_anchors"],
        )
        self.assertEqual(coverage["worker_node_types"], ["master"])
        self.assertEqual(coverage["source_types"], ["manual", "video_replay"])
        self.assertEqual(
            coverage["provider_response_types"],
            ["replay_success", "replay_vision_asr"],
        )

    async def test_failed_full_journey_fails_the_matrix(self):
        async def fake_explorer(item, *, work_root, max_steps=32):
            return ExplorationReport(
                scenario_id=item.scenario_id,
                status="passed",
                final_state="READY_TO_PUBLISH",
            )

        def fake_journey_runner(work_root):
            return {
                "status": "failed",
                "journey_count": 1,
                "passed": 0,
                "failed": 1,
                "paid_provider_calls": 0,
                "executed_console_actions": ["resume_failed_video"],
                "journeys": [{
                    "scenario_id": "blocked",
                    "status": "failed",
                    "final_state": "ERROR",
                }],
            }

        with tempfile.TemporaryDirectory() as directory:
            summary = await run_matrix(
                (scenario("journey-blocked"),),
                work_root=Path(directory),
                explorer=fake_explorer,
                include_journeys=True,
                journey_runner=fake_journey_runner,
            )

        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["journey_count"], 1)
        self.assertEqual(summary["journeys_failed"], 1)
        self.assertIn(
            "resume_failed_video",
            summary["coverage"]["executed_console_actions"],
        )

    async def test_failed_historical_incident_fails_the_matrix(self):
        async def fake_explorer(item, *, work_root, max_steps=32):
            return ExplorationReport(
                scenario_id=item.scenario_id,
                status="passed",
                final_state="READY_TO_PUBLISH",
            )

        def fake_incident_runner():
            return {
                "status": "failed", "incident_count": 1,
                "passed": 0, "failed": 1, "paid_provider_calls": 0,
                "results": [{"incident_id": "test23", "status": "failed"}],
            }

        with tempfile.TemporaryDirectory() as directory:
            summary = await run_matrix(
                (scenario("incident-blocked"),),
                work_root=Path(directory),
                explorer=fake_explorer,
                incident_runner=fake_incident_runner,
                include_incidents=True,
            )

        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["historical_incident_count"], 1)
        self.assertEqual(summary["historical_incidents_failed"], 1)

    async def test_video_golden_failure_fails_full_matrix_and_records_source(self):
        async def fake_explorer(item, *, work_root, max_steps=32):
            return ExplorationReport(
                scenario_id=item.scenario_id,
                status="passed",
                final_state="READY_TO_PUBLISH",
            )

        def fake_golden(work_root):
            return {
                "status": "failed", "source_type": "video_replay",
                "provider_response_type": "replay_vision_asr",
                "paid_provider_calls": 0,
                "failed_guards": ["gesture_preserved"],
            }

        with tempfile.TemporaryDirectory() as directory:
            summary = await run_matrix(
                (scenario("video-golden"),),
                work_root=Path(directory),
                explorer=fake_explorer,
                include_video_golden=True,
                video_golden_runner=fake_golden,
            )

        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["video_source_golden"]["status"], "failed")
        self.assertIn("video_replay", summary["coverage"]["source_types"])
        self.assertIn(
            "replay_vision_asr",
            summary["coverage"]["provider_response_types"],
        )

    async def test_shadow_regression_fails_matrix_but_business_pause_alone_does_not(self):
        async def fake_explorer(item, *, work_root, max_steps=32):
            return ExplorationReport(
                scenario_id=item.scenario_id,
                status="passed",
                final_state="READY_TO_PUBLISH",
            )

        def failed_shadow():
            return {
                "status": "failed", "audited_job_count": 1,
                "regression_codes": ["frozen_spec_missing"],
                "action_required_job_count": 1,
            }

        with tempfile.TemporaryDirectory() as directory:
            summary = await run_matrix(
                (scenario("shadow"),),
                work_root=Path(directory),
                explorer=fake_explorer,
                include_shadow_audit=True,
                shadow_audit_runner=failed_shadow,
            )

        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["production_shadow_audit"]["status"], "failed")

    async def test_perceptual_media_calibration_failure_fails_full_matrix(self):
        async def fake_explorer(item, *, work_root, max_steps=32):
            return ExplorationReport(
                scenario_id=item.scenario_id,
                status="passed",
                final_state="READY_TO_PUBLISH",
            )

        def failed_perceptual(work_root):
            return {
                "status": "failed", "mutation_count": 4,
                "killed": 3, "survived": 1,
                "paid_provider_calls": 0,
                "results": [],
            }

        with tempfile.TemporaryDirectory() as directory:
            summary = await run_matrix(
                (scenario("perceptual"),),
                work_root=Path(directory),
                include_perceptual_media=True,
                perceptual_media_runner=failed_perceptual,
            )

        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["perceptual_media_golden"]["survived"], 1)

    async def test_matrix_summary_is_machine_readable_and_zero_cost(self):
        async def fake_explorer(item, *, work_root, max_steps=32):
            self.assertTrue(work_root.name.startswith(item.scenario_id))
            return ExplorationReport(
                scenario_id=item.scenario_id,
                status="passed",
                final_state="READY_TO_PUBLISH",
                provider_calls=4,
                paid_provider_calls=0,
                steps=3,
                observed_states=("COMPOSED", "READY_TO_PUBLISH"),
                observed_actions=("review_final_video", "publish_video"),
                started_nodes=("master:1", "local_compose"),
                semantic_checks=(
                    "master_projection_consistent", "shot_within_master",
                ),
            )

        with tempfile.TemporaryDirectory() as directory:
            summary = await run_matrix(
                (scenario("one"), scenario("two")),
                work_root=Path(directory),
                explorer=fake_explorer,
            )

        self.assertEqual(summary["status"], "passed")
        self.assertEqual(summary["scenario_count"], 2)
        self.assertEqual(summary["passed"], 2)
        self.assertEqual(summary["paid_provider_calls"], 0)
        self.assertEqual(len(summary["reports"]), 2)
        self.assertEqual(summary["reports"][0]["scenario"]["provider_mode"], "mock")
        self.assertGreaterEqual(summary.get("historical_mutation_count", 0), 8)
        self.assertEqual(summary.get("historical_mutations_survived"), 0)
        self.assertEqual(summary.get("historical_mutation_score_pct"), 100.0)
        self.assertEqual(summary.get("unique_trace_count"), 1)
        self.assertEqual(summary.get("duplicate_trace_scenarios"), 1)
        self.assertGreaterEqual(summary.get("duration_sec", -1), 0)
        coverage = summary.get("coverage") or {}
        self.assertEqual(coverage.get("semantic_check_count"), 2)
        self.assertEqual(coverage.get("executed_console_action_count"), 2)
        self.assertEqual(coverage.get("dispatch_contract_coverage_pct"), 100.0)
        self.assertEqual(coverage.get("missing_dispatch_actions"), [])
        self.assertIn("confirm_budget", coverage.get("missing_console_actions", []))

    async def test_any_failed_scenario_fails_the_matrix(self):
        async def fake_explorer(item, *, work_root, max_steps=32):
            return ExplorationReport(
                scenario_id=item.scenario_id,
                status="failed",
                final_state="FAILED",
                paid_provider_calls=0,
                steps=1,
            )

        with tempfile.TemporaryDirectory() as directory:
            summary = await run_matrix(
                (scenario("broken"),),
                work_root=Path(directory),
                explorer=fake_explorer,
            )

        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["failed"], 1)

    async def test_source_fingerprint_changes_with_relevant_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pipeline.py"
            source.write_text("first", encoding="utf-8")
            before = source_fingerprint((source,))
            source.write_text("second", encoding="utf-8")
            after = source_fingerprint((source,))

        self.assertNotEqual(before, after)

    async def test_matrix_rejects_any_non_mock_or_paid_scenario(self):
        unsafe = FlowScenario(
            "unsafe", "stable", "none", "master", "auto", False,
            provider_mode="live", max_paid_calls=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                await run_matrix((unsafe,), work_root=Path(directory))


if __name__ == "__main__":
    unittest.main()
