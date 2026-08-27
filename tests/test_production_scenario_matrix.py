"""Pairwise creative freedom release matrix."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import creative_ir, production_compiler, production_graph  # noqa: E402
from tests.production_scenarios import (  # noqa: E402
    DIMENSIONS, all_pairs_covered, build_pairwise_scenarios,
    scenario_storyboard,
)


class ProductionScenarioMatrixTests(unittest.TestCase):
    def test_pairwise_matrix_covers_every_dimension_pair(self):
        scenarios = build_pairwise_scenarios(DIMENSIONS)
        self.assertTrue(all_pairs_covered(scenarios, DIMENSIONS))
        self.assertLessEqual(len(scenarios), 24)

    def test_every_pairwise_scenario_preserves_release_invariants(self):
        capabilities = production_compiler.ProviderCapabilities.seedance_2_5()
        for index, scenario in enumerate(build_pairwise_scenarios(), 1):
            with self.subTest(index=index, scenario=scenario):
                creative = creative_ir.from_storyboard(
                    scenario_storyboard(scenario, index)
                )
                first = production_compiler.compile_ir(
                    creative, job_id=f"J-MATRIX-{index}",
                    capabilities=capabilities, budget_cents=100000,
                )
                second = production_compiler.compile_ir(
                    creative, job_id=f"J-MATRIX-{index}",
                    capabilities=capabilities, budget_cents=100000,
                )
                self.assertTrue(first.compilable)
                self.assertFalse(first.spec.complexity["content_blocked"])
                self.assertEqual(first.spec.spec_hash, second.spec.spec_hash)
                self.assertTrue(all(
                    master.generation_duration_sec
                    <= capabilities.max_request_duration_sec
                    for master in first.spec.masters
                ))
                preserved = {
                    item for master in first.spec.masters
                    for item in master.preserved_requirements
                }
                self.assertEqual(set(first.required_content), preserved)
                production_graph.compile_graph(first.spec)


if __name__ == "__main__":
    unittest.main()
