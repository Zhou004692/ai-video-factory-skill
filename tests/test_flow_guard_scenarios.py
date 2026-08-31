"""Deterministic branch coverage for the continuous flow explorer."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.flow_guard.scenarios import (  # noqa: E402
    BRANCH_DIMENSIONS,
    all_pairs_covered,
    build_scenarios,
)


class FlowGuardScenarioTests(unittest.TestCase):
    def test_branch_matrix_covers_every_dimension_pair(self):
        scenarios = build_scenarios()

        self.assertTrue(all_pairs_covered(scenarios, BRANCH_DIMENSIONS))
        self.assertLessEqual(len(scenarios), 48)

    def test_scenarios_are_deterministic_serializable_and_zero_cost(self):
        first = build_scenarios()
        second = build_scenarios()

        self.assertEqual(first, second)
        self.assertEqual(
            len({scenario.scenario_id for scenario in first}), len(first)
        )
        for scenario in first:
            with self.subTest(scenario=scenario.scenario_id):
                payload = json.loads(json.dumps(scenario.as_dict()))
                self.assertEqual(payload["scenario_id"], scenario.scenario_id)
                self.assertEqual(payload["provider_mode"], "mock")
                self.assertEqual(payload["max_paid_calls"], 0)

    def test_every_critical_pause_and_resolution_is_represented(self):
        scenarios = build_scenarios()

        self.assertEqual(
            {scenario.pause for scenario in scenarios},
            set(BRANCH_DIMENSIONS["pause"]),
        )
        self.assertEqual(
            {scenario.resolution for scenario in scenarios},
            set(BRANCH_DIMENSIONS["resolution"]),
        )


if __name__ == "__main__":
    unittest.main()
