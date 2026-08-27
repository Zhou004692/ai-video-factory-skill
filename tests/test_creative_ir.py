"""Lossless creative intermediate representation tests."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
import unittest

from tests.fixtures.production_profiles import load_profile_fixture


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import creative_ir


class CreativeIRTests(unittest.TestCase):
    def test_normalized_ir_can_be_restored_losslessly(self):
        source = load_profile_fixture("experimental_multi_character_action")
        first = creative_ir.from_storyboard(source)
        second = creative_ir.from_dict(first.as_dict())
        self.assertEqual(second, first)

    def test_round_trip_preserves_dialogue_action_scene_and_requirements(self):
        source = load_profile_fixture("enhanced_two_person_continuity")

        creative = creative_ir.from_storyboard(source)
        payload = creative.as_dict()

        self.assertEqual(payload["title"], source["title"])
        self.assertEqual(payload["target_duration_sec"], 18.0)
        self.assertEqual(
            [beat["dialogue"] for beat in payload["beats"]],
            [beat["dialogue"] for beat in source["beats"]],
        )
        self.assertEqual(
            payload["beats"][0]["must_preserve"],
            tuple(source["beats"][0]["must_preserve"]),
        )
        self.assertEqual(payload["beats"][1]["speakers"], ("linxia",))
        with self.assertRaises(FrozenInstanceError):
            creative.title = "mutated"

    def test_unknown_creative_fields_are_preserved_as_extensions(self):
        source = {
            "title": "测试",
            "beats": [],
            "world_rule": "雨水会倒流",
            "editor_note": {"tone": "克制"},
        }

        result = creative_ir.from_storyboard(source)

        self.assertEqual(result.extensions["world_rule"], "雨水会倒流")
        self.assertEqual(result.extensions["editor_note"], {"tone": "克制"})

    def test_master_storyboard_can_be_migrated_without_mutating_source(self):
        source = {
            "script_metadata": {"title": "历史母片", "target_duration_sec": 8},
            "characters": [{"id": "linxia", "name": "林夏"}],
            "masters": [{
                "idx": 1,
                "type": "dialogue",
                "scene": "服务中心",
                "speaker": "linxia",
                "duration": 8,
                "action": "看向镜头",
                "voiceover": "立即为客户处理",
                "performance_beats": [{
                    "speaker": "linxia",
                    "dialogue": "立即为客户处理",
                    "visual_intent": "看向镜头",
                }],
            }],
        }

        result = creative_ir.from_storyboard(source)

        self.assertEqual(result.title, "历史母片")
        self.assertEqual(result.beats[0].dialogue, "立即为客户处理")
        self.assertEqual(result.beats[0].scene, "服务中心")
        self.assertEqual(result.beats[0].extensions["source_master_idx"], 1)
        self.assertNotIn("creative_ir_version", source)


if __name__ == "__main__":
    unittest.main()
