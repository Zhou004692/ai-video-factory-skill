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
    def test_visible_cast_and_lip_sync_semantics_matrix(self):
        cases = (
            ({"speaker": "worker", "dialogue": "Ready."}, 1, 1),
            ({"speaker": "customer_voice", "dialogue": "Ready?",
              "speech_mode": "off_screen"}, 0, 0),
            ({"speaker": "narrator", "dialogue": "They leave."}, 0, 0),
            ({"speaker": "背景台词", "dialogue": "Ready?"}, 0, 0),
            ({"speaker": "旁白", "dialogue": "They leave."}, 0, 0),
            ({"speaker": "worker", "dialogue": "Ready?",
              "speech_mode": "narration"}, 0, 0),
            ({"speaker": "narrator", "dialogue": "Ready?",
              "speech_mode": "on_screen"}, 1, 1),
            ({"speaker": "worker", "dialogue": ""}, 1, 0),
            ({"cast": ["worker"], "dialogue": ""}, 1, 0),
            ({"speaker": "worker", "dialogue": "Ready?",
              "speech_mode": "off_screen", "characters": ["worker"]}, 1, 0),
            ({"speaker": "narrator", "dialogue": "They leave.",
              "characters": [{"id": "worker"}, {"id": "worker"}]}, 1, 0),
            ({"speakers": ["背景台词", "worker", "narrator"],
              "dialogue": "Ready? Ready."}, 1, 1),
            ({"speakers": ["worker", "narrator", "背景台词"],
              "dialogue": "Ready? Ready."}, 1, 1),
            ({"speakers": ["worker", "narrator"], "speech_mode": "off_screen",
              "dialogue": "Ready? Ready."}, 0, 0),
            ({"dialogue": "Anonymous voice", "speech_mode": "off_screen"}, 0, 0),
            ({}, 0, 0),
        )
        for beat, visible_count, lip_sync_count in cases:
            with self.subTest(beat=beat):
                creative = creative_ir.from_storyboard({
                    "target_duration_sec": 8,
                    "characters": [{"id": "worker"}],
                    "beats": [{"scene": "yard", **beat}],
                })
                report = production_complexity.analyze(creative)
                self.assertEqual(
                    report.dimensions["max_on_screen_characters"], visible_count,
                )
                self.assertEqual(
                    report.dimensions["lip_sync_required_count"], lip_sync_count,
                )
                self.assertEqual("lip_sync_required" in report.risk_codes,
                                 bool(lip_sync_count))

    def test_repeated_audio_does_not_create_visible_continuity(self):
        creative = creative_ir.from_storyboard({
            "beats": [
                {"scene": "yard", "speaker": "narrator", "dialogue": "Start."},
                {"scene": "yard", "speaker": "narrator", "dialogue": "End."},
            ],
        })
        report = production_complexity.analyze(creative)
        self.assertEqual(report.dimensions["cross_master_character_count"], 0)
        self.assertEqual(report.dimensions["continuity_edge_count"], 0)
        self.assertGreater(report.dimensions["dialogue_density"], 0)

    def test_multi_actor_recommendation_allows_moving_blocking(self):
        report = self.analyze("experimental_multi_character_action")
        staging = next(text for text in report.recommendations if "多人同镜" in text)
        self.assertNotIn("固定站位", staging)
        self.assertIn("身份", staging)
        self.assertIn("空间连续性", staging)
        self.assertIn("走位", staging)

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


class OffscreenComplexityRegressionTests(unittest.TestCase):
    def test_offscreen_voice_is_not_a_visible_lipsync_actor(self):
        from factory import production_compiler

        creative = creative_ir.from_storyboard({
            "title": "工人检查设备，客户画外确认",
            "target_duration_sec": 8,
            "characters": [{"id": "worker"}],
            "beats": [{
                "id": "b1",
                "scene": "yard",
                "speaker": "customer_voice",
                "dialogue": "Ready for delivery",
                "duration_sec": 8,
                "characters": ["worker"],
                "speech_mode": "off_screen",
                "action": "worker checks machine",
            }],
        })
        report = production_complexity.analyze(creative)
        self.assertEqual(
            tuple(production_compiler._beat_cast(creative.beats[0])),
            ("worker",),
        )
        self.assertEqual(report.dimensions["max_on_screen_characters"], 1)
        self.assertEqual(report.dimensions["lip_sync_required_count"], 0)
        self.assertNotIn("lip_sync_required", report.risk_codes)


if __name__ == "__main__":
    unittest.main()
