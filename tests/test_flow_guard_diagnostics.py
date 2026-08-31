"""Safety boundaries for blocker detection and automatic repair."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.flow_guard.invariants import evaluate_report  # noqa: E402
from factory.flow_guard.minimizer import minimize_failure  # noqa: E402
from factory.flow_guard.model import ExplorationReport  # noqa: E402
from factory.flow_guard.repairs import apply_repair, allowed_recipe_ids  # noqa: E402
from factory.production_invariants import InvariantReport  # noqa: E402


class FlowGuardDiagnosticTests(unittest.TestCase):
    def test_detects_dead_end_loop_and_paid_provider_call(self):
        report = ExplorationReport(
            scenario_id="broken",
            status="failed",
            final_state="PRODUCTION_RUNNING",
            observed_states=("PRODUCTION_RUNNING", "PRODUCTION_RUNNING"),
            observed_actions=("view_progress", "view_progress"),
            paid_provider_calls=1,
            steps=32,
        )

        codes = {item.code for item in evaluate_report(report, max_steps=32)}

        self.assertIn("nonterminal_dead_end", codes)
        self.assertIn("state_action_loop", codes)
        self.assertIn("paid_provider_call", codes)
        self.assertIn("step_limit_exceeded", codes)

    def test_minimizer_removes_unrelated_faults(self):
        faults = ("projection_drift", "budget_pause", "missing_action")

        minimized = minimize_failure(
            faults,
            lambda items: "projection_drift" in items,
        )

        self.assertEqual(minimized, ("projection_drift",))

    def test_only_allowlisted_zero_cost_repairs_can_run(self):
        self.assertEqual(
            allowed_recipe_ids(),
            (
                "repair_safe_projections",
                "recover_expired_leases",
                "retry_repaired_contract_nodes",
            ),
        )
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        with patch(
            "factory.flow_guard.repairs.production_invariants."
            "repair_safe_projections",
            return_value=InvariantReport(
                "J-1", (), ("mutable_storyboard_stale",)
            ),
        ):
            result = apply_repair(
                database, "J-1", "repair_safe_projections"
            )
        self.assertTrue(result.applied)
        self.assertEqual(
            result.after["repaired_codes"],
            ["mutable_storyboard_stale"],
        )

        for recipe_id in (
            "replace_paid_master",
            "accept_candidate",
            "increase_budget",
            "rewrite_frozen_spec",
        ):
            with self.subTest(recipe_id=recipe_id):
                with self.assertRaises(ValueError):
                    apply_repair(database, "J-1", recipe_id)


if __name__ == "__main__":
    unittest.main()
