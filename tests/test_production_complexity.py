"""Explainable production-complexity classification tests."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

from tests.fixtures.production_profiles import load_profile_fixture


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import creative_ir, production_complexity  # noqa: E402


class ProductionComplexityTests(unittest.TestCase):
    def analyze(self, profile_id: str):
        creative = creative_ir.from_storyboard(load_profile_fixture(profile_id))
        return production_complexity.analyze(creative)

    def test_content_is_never_rejected_only_for_complexity(self):
        report = self.analyze("experimental_multi_character_action")

        self.assertEqual(report.mode, "experimental")
        self.assertGreater(report.score, 70)
        self.assertFalse(report.content_blocked)
        self.assertIn("multi_character_staging", report.risk_codes)

    def test_two_person_continuity_selects_enhanced_mode(self):
        report = self.analyze("enhanced_two_person_continuity")

        self.assertEqual(report.mode, "enhanced")
        self.assertIn("cross_master_character", report.risk_codes)
        self.assertEqual(report.dimensions["scene_count"], 1)

    def test_single_dialogue_selects_stable_mode(self):
        report = self.analyze("stable_single_dialogue")

        self.assertEqual(report.mode, "stable")
        self.assertLessEqual(report.score, 35)

    def test_multi_scene_reports_scene_risk_without_blocking(self):
        report = self.analyze("enhanced_multi_scene")

        self.assertEqual(report.mode, "enhanced")
        self.assertEqual(report.dimensions["scene_count"], 2)
        self.assertIn("multiple_scenes", report.risk_codes)
        self.assertTrue(report.recommendations)

    def test_report_is_deterministic_and_explainable(self):
        first = self.analyze("experimental_multi_character_action")
        second = self.analyze("experimental_multi_character_action")

        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertEqual(
            first.score,
            sum(first.weighted_dimensions.values()),
        )


if __name__ == "__main__":
    unittest.main()
