"""Frozen creative-profile baselines for the production compiler."""
from __future__ import annotations

import unittest

from tests.fixtures.production_profiles import load_profile_fixtures


EXPECTED_PROFILES = {
    "stable_single_dialogue",
    "enhanced_two_person_continuity",
    "enhanced_multi_scene",
    "experimental_multi_character_action",
}


class ProductionProfileBaselineTests(unittest.TestCase):
    def test_all_profiles_have_unique_ids_and_preservation_contracts(self):
        fixtures = load_profile_fixtures()

        self.assertEqual(len(fixtures), 4)
        self.assertEqual(
            {item["profile_id"] for item in fixtures}, EXPECTED_PROFILES
        )
        for item in fixtures:
            self.assertEqual(item["fixture_version"], 1)
            self.assertTrue(item["title"].strip())
            self.assertGreater(float(item["target_duration_sec"]), 0)
            self.assertTrue(item["beats"])
            self.assertTrue(
                all(beat.get("must_preserve") for beat in item["beats"])
            )

    def test_profile_beats_reference_declared_characters(self):
        for item in load_profile_fixtures():
            character_ids = {
                character["id"] for character in item["characters"]
            }
            for beat in item["beats"]:
                speaker = str(beat.get("speaker") or "").strip()
                if speaker:
                    self.assertIn(speaker, character_ids)


if __name__ == "__main__":
    unittest.main()
