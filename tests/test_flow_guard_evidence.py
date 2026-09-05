"""Evidence-led coverage must not conflate simulated and integrated paths."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.flow_guard.evidence import (  # noqa: E402
    BROWSER_EVIDENCE_PREFIX,
    CoverageEvidence,
    parse_browser_evidence,
)
from factory.flow_guard import runner  # noqa: E402


class FlowGuardEvidenceTests(unittest.TestCase):
    def boundaries(self, observations=None, implementations=None):
        builder = getattr(runner, "build_evidence_boundaries", None)
        self.assertIsNotNone(builder, "machine-readable evidence boundaries missing")
        return builder(observations or {}, implementations or {})

    def test_full_or_passed_flag_never_implies_strict_or_quality_evidence(self):
        boundaries = self.boundaries({
            "status": "passed", "mode": "full", "strict_e2e": True,
        })

        self.assertEqual(boundaries["status_scope"], "technical_suite")
        for key in ("strict_transport_replay", "recognition_accuracy", "actual_content_quality"):
            self.assertEqual(boundaries["dimensions"][key]["status"], "not_run")
            self.assertEqual(boundaries["dimensions"][key]["observed_checks"], [])

    def test_prefilled_video_answers_only_prove_propagation(self):
        boundaries = self.boundaries({
            "video_source_golden": {
                "status": "passed", "source_type": "video_replay",
                "passed_guards": ["gesture_preserved", "speaker_bound"],
                "failed_guards": [],
            },
        }, {"video_source_golden": runner.run_video_performance_golden})

        evidence = boundaries["dimensions"]["video_performance_propagation"]
        self.assertEqual(evidence["status"], "passed")
        self.assertEqual(evidence["level"], "component_propagation")
        self.assertEqual(evidence["source_kind"], "synthetic_media_with_prefilled_answers")
        self.assertIn("ASR", evidence["replacement_boundary"])
        self.assertIn("gesture_preserved", evidence["observed_checks"])
        self.assertEqual(boundaries["dimensions"]["recognition_accuracy"]["status"], "not_run")

    def test_formal_service_keeps_high_level_handler_replacement_explicit(self):
        boundaries = self.boundaries({
            "journey_suite": {"status": "passed", "journeys": [{
                "scenario_id": "core", "status": "passed",
                "completed_stages": ["compiled", "final_review", "ready_to_publish"],
            }]},
        }, {"journey_suite": runner.run_full_journey_suite})

        evidence = boundaries["dimensions"]["formal_service_with_high_level_fixture"]
        self.assertEqual(evidence["status"], "passed")
        for handler in ("character_anchor", "scene_anchor", "master", "bgm"):
            self.assertIn(handler, evidence["replacement_boundary"])
        self.assertIn("ASR", evidence["replacement_boundary"])
        self.assertIn("脚本", evidence["replacement_boundary"])
        self.assertTrue(evidence["proves"])
        self.assertTrue(evidence["does_not_prove"])

    def test_synthetic_media_checks_do_not_prove_content_quality(self):
        boundaries = self.boundaries({
            "perceptual_media_golden": {"status": "passed", "clean_findings": [],
                "results": [{"mutation_id": "freeze_segment", "killed": True}]},
        }, {"perceptual_media_golden": runner.run_perceptual_media_golden})

        evidence = boundaries["dimensions"]["media_technical_checks"]
        self.assertEqual(evidence["status"], "passed")
        self.assertEqual(evidence["source_kind"], "synthetic_media")
        self.assertEqual(boundaries["dimensions"]["actual_content_quality"]["status"], "not_run")

    def test_injected_runner_cannot_claim_builtin_provenance(self):
        boundaries = self.boundaries({
            "journey_suite": {"status": "passed", "journeys": [{"scenario_id": "fake"}]},
        }, {"journey_suite": lambda: None})

        evidence = boundaries["dimensions"]["formal_service_with_high_level_fixture"]
        self.assertEqual(evidence["status"], "unknown")
        self.assertEqual(evidence["source_kind"], "unknown")
        self.assertEqual(evidence["observed_checks"], [])

    def test_missing_results_do_not_receive_passed_provenance(self):
        boundaries = self.boundaries({"journey_suite": {"status": "passed"}},
            {"journey_suite": runner.run_full_journey_suite})

        self.assertEqual(boundaries["dimensions"]["formal_service_with_high_level_fixture"]["status"], "unknown")

    def test_malformed_known_results_remain_unknown(self):
        for results in (None, "scenario-not-a-list", ["not-a-result"], [{}]):
            with self.subTest(results=results):
                boundaries = self.boundaries({
                    "journey_suite": {"status": "passed", "journeys": results},
                }, {"journey_suite": runner.run_full_journey_suite})
                self.assertEqual(boundaries["dimensions"]["formal_service_with_high_level_fixture"]["status"], "unknown")

    def test_skipped_checks_are_not_run_and_outcomes_remain_distinct(self):
        for status in ("partial", "attention", "failed", "skipped"):
            with self.subTest(status=status):
                boundaries = self.boundaries({
                    "journey_suite": {"status": status, "journeys": [{"scenario_id": "core", "status": status}]},
                }, {"journey_suite": runner.run_full_journey_suite})
                evidence = boundaries["dimensions"]["formal_service_with_high_level_fixture"]
                self.assertEqual(evidence["status"], "not_run" if status == "skipped" else status)
                if status == "skipped":
                    self.assertEqual(evidence["source_kind"], "unknown")

    def test_shadow_lookup_does_not_claim_recovery_execution_or_fault_equivalence(self):
        boundaries = self.boundaries({
            "production_shadow_audit": {"status": "attention", "audited_job_count": 1,
                "jobs": [{"job_id": "review", "recovery_action": "review_final_video"}]},
        }, {"production_shadow_audit": runner.audit_production_shadow})

        evidence = boundaries["dimensions"]["production_shadow_lookup"]
        self.assertEqual(evidence["status"], "attention")
        self.assertIn("恢复动作已执行", evidence["does_not_prove"])
        self.assertIn("人工待审核等同程序故障", evidence["does_not_prove"])

    def test_evidence_sources_remain_separate(self):
        evidence = CoverageEvidence()
        evidence.record("graph_action", "review_final_video")
        evidence.record("ui_dispatch", "submit_publish")
        evidence.record("backend_action", "review_continuity_anchors")
        evidence.record("worker_action", "review_continuity_anchors")
        evidence.record("worker_node", "master")
        evidence.record("source_type", "video_replay")
        evidence.record("provider_response", "replay_success")

        payload = evidence.as_dict()

        self.assertEqual(payload["graph_actions"], ["review_final_video"])
        self.assertEqual(payload["ui_dispatch_actions"], ["submit_publish"])
        self.assertEqual(
            payload["integrated_backend_actions"],
            ["review_continuity_anchors"],
        )
        self.assertEqual(payload["worker_node_types"], ["master"])
        self.assertEqual(
            payload["worker_actions"], ["review_continuity_anchors"]
        )
        self.assertEqual(payload["source_types"], ["video_replay"])
        self.assertEqual(payload["provider_response_types"], ["replay_success"])

    def test_browser_evidence_requires_runtime_sentinel(self):
        payload, errors = parse_browser_evidence("browser suite passed")

        self.assertEqual(payload, CoverageEvidence().as_dict())
        self.assertEqual(errors, ["browser_evidence_missing"])

    def test_browser_evidence_parses_only_explicit_runtime_facts(self):
        stdout = (
            "browser suite passed\n"
            + BROWSER_EVIDENCE_PREFIX
            + '{"ui_dispatch_actions":["submit_publish"],'
              '"integrated_backend_actions":["review_final_video"],'
              '"worker_node_types":["master"],'
              '"source_types":["manual"],'
              '"provider_response_types":["replay_success"]}'
        )

        payload, errors = parse_browser_evidence(stdout)

        self.assertEqual(errors, [])
        self.assertEqual(payload["ui_dispatch_actions"], ["submit_publish"])
        self.assertEqual(
            payload["integrated_backend_actions"], ["review_final_video"]
        )
        self.assertNotIn("submit_publish", payload["graph_actions"])


class FlowGuardBoundaryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_matrix_adds_boundaries_without_running_optional_suites(self):
        with tempfile.TemporaryDirectory() as directory:
            report = await runner.run_matrix((), work_root=Path(directory))

        self.assertIn("evidence_boundaries", report)
        self.assertEqual(report["status"], "partial")
        dimensions = report["evidence_boundaries"]["dimensions"]
        self.assertEqual(dimensions["strict_transport_replay"]["status"], "not_run")
        self.assertEqual(dimensions["formal_service_with_high_level_fixture"]["status"], "not_run")


if __name__ == "__main__":
    unittest.main()
