"""Aggregation and watch fingerprints for the continuous flow guard."""
from __future__ import annotations

from pathlib import Path
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
