"""Deterministic branch coverage for the continuous flow explorer."""
from __future__ import annotations

import json
from pathlib import Path
import inspect
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.flow_guard import scenarios as scenario_model  # noqa: E402
import flow_guard as flow_guard_cli  # noqa: E402


BRANCH_DIMENSIONS = scenario_model.BRANCH_DIMENSIONS
all_pairs_covered = scenario_model.all_pairs_covered
build_scenarios = scenario_model.build_scenarios


class FlowGuardScenarioTests(unittest.TestCase):
    def test_cli_defaults_to_full_suite_and_supports_smoke(self):
        parser = flow_guard_cli.build_parser()
        full = parser.parse_args(["run"])

        self.assertEqual(getattr(full, "suite", None), "full")
        if getattr(full, "suite", None) != "full":
            return
        smoke = parser.parse_args(["run", "--suite", "smoke"])
        self.assertEqual(getattr(smoke, "suite", None), "smoke")
        self.assertIn(
            "suite", inspect.signature(flow_guard_cli._select_scenarios).parameters
        )

    def test_watch_scope_includes_management_server_and_guard_entrypoint(self):
        watched = {Path(path).name for path in flow_guard_cli.WATCH_PATHS}

        self.assertIn("management_server.py", watched)
        self.assertIn("flow_guard.py", watched)

    def test_smoke_matrix_covers_every_dimension_pair(self):
        builder = getattr(scenario_model, "build_smoke_scenarios", None)
        self.assertTrue(callable(builder))
        if not callable(builder):
            return
        scenarios = builder()

        self.assertTrue(all_pairs_covered(scenarios, BRANCH_DIMENSIONS))
        self.assertLessEqual(len(scenarios), 48)

    def test_incremental_matrix_is_small_but_covers_each_effective_branch_value(self):
        builder = getattr(scenario_model, "build_incremental_scenarios", None)
        self.assertTrue(callable(builder))
        if not callable(builder):
            return
        scenarios = builder()

        self.assertLessEqual(len(scenarios), 10)
        self.assertEqual(
            {item.profile for item in scenarios},
            set(BRANCH_DIMENSIONS["profile"]),
        )
        self.assertEqual(
            {item.pause for item in scenarios},
            set(BRANCH_DIMENSIONS["pause"]),
        )
        self.assertEqual(
            {item.resolution for item in scenarios},
            set(BRANCH_DIMENSIONS["resolution"]),
        )
        self.assertEqual(
            {item.pause_node for item in scenarios},
            set(scenario_model.FAULT_NODE_TYPES),
        )
        self.assertEqual({item.restart for item in scenarios}, {False, True})
        self.assertTrue(all(
            item.provider_mode == "mock" and item.max_paid_calls == 0
            for item in scenarios
        ))

    def test_full_matrix_covers_every_effective_fault_node_path(self):
        scenarios = build_scenarios()
        fault_node_types = getattr(scenario_model, "FAULT_NODE_TYPES", ())
        self.assertTrue(fault_node_types)

        self.assertGreaterEqual(len(scenarios), 80)
        self.assertLessEqual(len(scenarios), 120)
        expected = {
            (pause, node_type)
            for pause in (
                "budget", "provider_action", "recoverable_failure"
            )
            for node_type in fault_node_types
        }
        actual = {(item.pause, item.pause_node) for item in scenarios}
        self.assertEqual(expected - actual, set())

    def test_full_matrix_contains_no_phantom_resolution_combinations(self):
        scenarios = build_scenarios()
        resolutions = getattr(scenario_model, "RESOLUTION_BY_PAUSE", {})
        self.assertTrue(resolutions)

        for scenario in scenarios:
            with self.subTest(scenario=scenario.scenario_id):
                self.assertEqual(
                    scenario.resolution,
                    resolutions[scenario.pause],
                )
        effective_keys = {
            (
                item.profile,
                item.pause,
                item.pause_node,
                item.resolution,
                item.restart,
            )
            for item in scenarios
        }
        self.assertEqual(len(effective_keys), len(scenarios))

    def test_every_effective_path_has_restart_coverage(self):
        scenarios = build_scenarios()
        paths = {(item.pause, item.pause_node) for item in scenarios}
        restarted = {
            (item.pause, item.pause_node)
            for item in scenarios if item.restart
        }

        self.assertEqual(paths, restarted)

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
