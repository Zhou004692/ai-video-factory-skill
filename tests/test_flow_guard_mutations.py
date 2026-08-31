"""Historical blocker mutations calibrate Flow Guard detector sensitivity."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.flow_guard import mutations  # noqa: E402


class FlowGuardMutationTests(unittest.TestCase):
    def test_historical_mutation_module_is_available(self):
        self.assertIsNotNone(
            importlib.util.find_spec("factory.flow_guard.mutations")
        )

    def test_all_historical_mutations_are_killed(self):
        runner = getattr(
            mutations, "run_historical_mutation_calibration", None
        )
        self.assertTrue(callable(runner))
        if not callable(runner):
            return

        report = runner()

        self.assertGreaterEqual(report["mutation_count"], 8)
        self.assertEqual(report["killed"], report["mutation_count"])
        self.assertEqual(report["survived"], 0)
        self.assertEqual(report["score_pct"], 100.0)
        self.assertEqual(report["status"], "passed")


if __name__ == "__main__":
    unittest.main()
