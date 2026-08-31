"""Full management workflow journeys remain zero-cost and user-completable."""
from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.flow_guard import journeys  # noqa: E402


class FlowGuardJourneyTests(unittest.TestCase):
    def test_journey_suite_supports_browser_execution(self):
        parameters = inspect.signature(
            getattr(journeys, "run_full_journey_suite")
        ).parameters

        self.assertIn("include_browser", parameters)
        self.assertIn("browser_runner", parameters)

    def test_full_journey_module_is_available(self):
        self.assertIsNotNone(
            importlib.util.find_spec("factory.flow_guard.journeys")
        )

    def test_browser_suite_is_merged_into_journey_evidence(self):
        browser_calls = []

        def fake_browser_runner():
            browser_calls.append(True)
            return {
                "status": "passed",
                "scenario_count": 2,
                "passed": 2,
                "failed": 0,
                "paid_provider_calls": 0,
                "executed_console_actions": [
                    "review_master_candidates",
                    "review_final_video",
                ],
                "journeys": [
                    {"scenario_id": "browser-full", "status": "passed"},
                    {"scenario_id": "browser-fault", "status": "passed"},
                ],
            }

        with tempfile.TemporaryDirectory() as directory:
            report = journeys.run_full_journey_suite(
                Path(directory),
                scenarios=(),
                include_browser=True,
                browser_runner=fake_browser_runner,
            )

        self.assertEqual(browser_calls, [True])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["journey_count"], 2)
        self.assertEqual(report["passed"], 2)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(
            report["executed_console_actions"],
            ["review_final_video", "review_master_candidates"],
        )
        self.assertEqual(report["browser_suite"]["scenario_count"], 2)

    def test_browser_action_coverage_only_counts_passing_suites(self):
        responses = iter((
            SimpleNamespace(returncode=0, stdout="full passed", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr="fault failed"),
        ))

        def fake_command_runner(*args, **kwargs):
            return next(responses)

        report = journeys.run_management_browser_suite(
            command_runner=fake_command_runner
        )

        self.assertEqual(report["status"], "failed")
        self.assertIn(
            "review_continuity_anchors",
            report["executed_console_actions"],
        )
        self.assertNotIn(
            "reconcile_provider_task",
            report["executed_console_actions"],
        )

    def test_manual_source_reaches_publishable_video_without_cost(self):
        runner = getattr(journeys, "run_full_journey_suite", None)
        self.assertTrue(callable(runner))
        if not callable(runner):
            return
        scenario = {
            "scenario_id": "flow_guard_manual_three_person",
            "source": {
                "type": "manual",
                "title": "白天高空车跨场景抢修",
                "content": (
                    "林夏在调度中心收到故障告警，高远在设备场完成备用"
                    "高空车复检，周工在白天施工现场恢复设备。三人分别"
                    "发言，跨越多个场景完成闭环。"
                ),
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            report = runner(
                Path(directory), scenarios=(scenario,), include_browser=False
            )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["journey_count"], 1)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["paid_provider_calls"], 0)
        self.assertEqual(report["journeys"][0]["final_state"], "READY_TO_PUBLISH")
        self.assertGreater(
            (report["journeys"][0].get("media_metrics") or {}).get("fps", 0),
            0,
        )

    def test_background_voiceover_journey_stays_offscreen(self):
        scenario = next(
            item for item in journeys.FULL_JOURNEY_SCENARIOS
            if item["scenario_id"] == "manual_background_voiceover"
        )
        with tempfile.TemporaryDirectory() as directory:
            report = journeys.run_full_journey_suite(
                Path(directory),
                scenarios=(scenario,),
                include_browser=False,
            )

        self.assertEqual(report["status"], "passed")
        self.assertGreaterEqual(
            report["journeys"][0].get("background_voiceover_master_count", 0),
            1,
        )


if __name__ == "__main__":
    unittest.main()
