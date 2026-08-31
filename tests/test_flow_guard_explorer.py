"""Real Graph V2 traversal driven only by deterministic provider mocks."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.flow_guard.explorer import explore_scenario  # noqa: E402
from factory.flow_guard.model import FlowScenario  # noqa: E402


def scenario(
    pause: str,
    *,
    pause_node: str = "master",
    restart: bool = False,
) -> FlowScenario:
    return FlowScenario(
        scenario_id=f"test-{pause}-{pause_node}-{int(restart)}",
        profile="stable",
        pause=pause,
        pause_node=pause_node,
        resolution="auto",
        restart=restart,
    )


class FlowGuardExplorerTests(unittest.IsolatedAsyncioTestCase):
    async def test_clean_graph_reaches_ready_to_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            report = await explore_scenario(
                scenario("none"), work_root=Path(directory)
            )

        self.assertEqual(report.status, "passed")
        self.assertEqual(report.final_state, "READY_TO_PUBLISH")
        self.assertIn("review_final_video", report.observed_actions)
        self.assertEqual(report.paid_provider_calls, 0)
        self.assertGreater(report.provider_calls, 0)

    async def test_human_and_provider_pauses_are_reachable_and_resolved(self):
        cases = {
            "anchor_review": "approve_continuity_anchors",
            "master_review": "review_candidate",
            "budget": "increase_job_budget",
            "provider_action": "reconcile_provider_task",
            "recoverable_failure": "preserve_and_diagnose",
        }
        for pause, expected_action in cases.items():
            with self.subTest(pause=pause):
                with tempfile.TemporaryDirectory() as directory:
                    report = await explore_scenario(
                        scenario(pause), work_root=Path(directory)
                    )
                self.assertEqual(report.status, "passed")
                self.assertEqual(report.final_state, "READY_TO_PUBLISH")
                self.assertIn(expected_action, report.observed_actions)
                self.assertEqual(report.paid_provider_calls, 0)

    async def test_restart_after_pause_uses_persisted_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            report = await explore_scenario(
                scenario("budget", restart=True), work_root=Path(directory)
            )

        self.assertEqual(report.status, "passed")
        self.assertEqual(report.final_state, "READY_TO_PUBLISH")
        self.assertIn("increase_job_budget", report.observed_actions)
        self.assertGreaterEqual(report.steps, 2)


if __name__ == "__main__":
    unittest.main()
