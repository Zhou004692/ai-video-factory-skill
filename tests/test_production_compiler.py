"""Creative IR to Production Spec v2 compiler tests."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

from tests.fixtures.production_profiles import load_profile_fixture


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import creative_ir, production_compiler  # noqa: E402


class ProductionCompilerTests(unittest.TestCase):
    def compile_profile(self, profile_id: str):
        creative = creative_ir.from_storyboard(load_profile_fixture(profile_id))
        return production_compiler.compile_ir(
            creative,
            job_id=f"J-{profile_id}",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
        )

    def test_experimental_content_compiles_without_losing_required_content(self):
        result = self.compile_profile("experimental_multi_character_action")

        self.assertTrue(result.compilable)
        self.assertEqual(result.spec.version, 2)
        self.assertEqual(result.spec.mode, "experimental")
        preserved = {
            requirement
            for master in result.spec.masters
            for requirement in master.preserved_requirements
        }
        self.assertTrue(set(result.required_content).issubset(preserved))
        self.assertEqual(result.blockers, ())
        self.assertTrue(result.spec.nodes)
        self.assertTrue(result.spec.edges)

    def test_compiler_collects_all_deterministic_blockers_in_one_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            creative = creative_ir.from_storyboard({
                "title": "非法生产输入",
                "characters": [],
                "beats": [{
                    "id": "beat-1",
                    "kind": "dialogue",
                    "scene": "仓库",
                    "speaker": "unknown",
                    "dialogue": "这是一段无法在一秒内完整说完的超长台词",
                    "duration_sec": 1,
                    "duration_locked": True,
                    "reference_path": "missing.png",
                    "must_preserve": ["完整台词"],
                }],
            })

            result = production_compiler.compile_ir(
                creative,
                job_id="J-invalid",
                capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
                root=tmp,
                budget_cents=1000,
            )

        self.assertFalse(result.compilable)
        self.assertIsNone(result.spec)
        self.assertEqual(
            {issue.code for issue in result.blockers},
            {"speaker_unknown", "reference_missing", "dialogue_timing_impossible"},
        )

    def test_compilation_is_deterministic(self):
        first = self.compile_profile("enhanced_multi_scene")
        second = self.compile_profile("enhanced_multi_scene")

        self.assertEqual(first.spec.spec_hash, second.spec.spec_hash)
        self.assertEqual(first.spec.as_dict(), second.spec.as_dict())

    def test_multi_scene_is_adapted_into_scene_safe_masters(self):
        source = load_profile_fixture("enhanced_multi_scene")
        result = production_compiler.compile_ir(
            creative_ir.from_storyboard(source),
            job_id="J-enhanced_multi_scene",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
            runtime_storyboard=source,
        )

        self.assertTrue(result.compilable)
        self.assertEqual(result.spec.mode, "enhanced")
        self.assertEqual(len(result.spec.masters), 2)
        self.assertTrue(all(master.duration_sec <= 30 for master in result.spec.masters))
        self.assertIn("scene_split", {
            action
            for master in result.spec.masters
            for action in master.adaptation_actions
        })
        runtime_masters = result.spec.runtime_storyboard.get("masters") or []
        self.assertEqual(
            [master["idx"] for master in runtime_masters],
            [master.idx for master in result.spec.masters],
        )
        self.assertEqual(
            [tuple(master["source_beat_ids"]) for master in runtime_masters],
            [master.creative_beat_ids for master in result.spec.masters],
        )
        self.assertEqual(
            "".join(master["voiceover"] for master in runtime_masters),
            "".join(beat.dialogue for beat in creative_ir.from_storyboard(source).beats),
        )

    def test_runtime_director_plan_matches_compiled_masters(self):
        creative_source = {
            "characters": [{"id": "lead", "name": "Lead"}],
            "beats": [
                {
                    "id": "beat-1", "kind": "hook", "scene": "room-a",
                    "speaker": "lead", "dialogue": "first line",
                    "duration_sec": 6, "action": "act in room a",
                },
                {
                    "id": "beat-2", "kind": "solution", "scene": "room-b",
                    "speaker": "lead", "dialogue": "second line",
                    "duration_sec": 6, "action": "act in room b",
                },
            ],
        }
        runtime_source = {
            **creative_source,
            "masters": [
                {"idx": 1, "type": "dialogue", "duration": 6},
                {"idx": 2, "type": "dialogue", "duration": 6},
                {"idx": 3, "type": "broll", "duration": 6},
            ],
            "director_plan": {
                "strategy": "dialogue_plus_broll",
                "dialogue_master_count": 2,
                "broll_master_count": 1,
                "paid_master_count": 3,
            },
        }

        result = production_compiler.compile_ir(
            creative_ir.from_storyboard(creative_source),
            job_id="J-runtime-plan",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
            runtime_storyboard=runtime_source,
        )

        plan = result.spec.runtime_storyboard["director_plan"]
        self.assertEqual(len(result.spec.runtime_storyboard["masters"]), 2)
        self.assertEqual(plan["paid_master_count"], 2)
        self.assertEqual(plan["dialogue_master_count"], 2)
        self.assertEqual(plan["broll_master_count"], 0)
        self.assertEqual(plan["strategy"], "cinematic_dialogue_only")

    def test_v2_compiler_keeps_offscreen_voice_out_of_cast_and_anchor_nodes(self):
        source = {
            "title": "画外质疑",
            "characters": [{"id": "女博主", "name": "女博主"}],
            "beats": [
                {
                    "id": "beat-1", "kind": "hook", "scene": "studio",
                    "speaker": "女博主", "dialogue": "设备租赁。",
                    "duration_sec": 2, "action": "女博主面对镜头说话",
                    "characters": ["女博主"], "speech_mode": "on_screen",
                },
                {
                    "id": "beat-2", "kind": "conflict", "scene": "studio",
                    "speaker": "背景台词", "dialogue": "没客户，看你怎么干？",
                    "duration_sec": 2, "action": "女博主闭嘴看向画外",
                    "characters": ["女博主"], "speech_mode": "off_screen",
                    "tone": "画外反问", "facial_expression": "女博主眉头微紧",
                },
            ],
        }

        result = production_compiler.compile_ir(
            creative_ir.from_storyboard(source),
            job_id="J-offscreen",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
            runtime_storyboard=source,
        )

        self.assertTrue(result.compilable)
        self.assertEqual(result.spec.masters[0].character_ids, ("女博主",))
        self.assertEqual(result.spec.masters[0].speaker_ids, ("女博主",))
        runtime = result.spec.runtime_storyboard["masters"][0]
        self.assertEqual(runtime["audio_speakers"], ["女博主", "背景台词"])
        self.assertEqual(runtime["performance_beats"][1]["speech_mode"], "off_screen")
        self.assertNotIn(
            "character_anchor:背景台词",
            {node.node_id for node in result.spec.nodes},
        )

    def test_runtime_director_timeline_references_only_compiled_masters(self):
        creative_source = {
            "characters": [{"id": "lead", "name": "Lead"}],
            "beats": [
                {
                    "id": "beat-1", "kind": "hook", "scene": "room-a",
                    "speaker": "lead", "dialogue": "first line",
                    "duration_sec": 6, "action": "act in room a",
                },
                {
                    "id": "beat-2", "kind": "solution", "scene": "room-b",
                    "speaker": "lead", "dialogue": "second line",
                    "duration_sec": 6, "action": "act in room b",
                },
            ],
        }
        runtime_source = {
            **creative_source,
            "production_mode": "native_master_local_director",
            "masters": [
                {"idx": 1, "type": "dialogue", "duration": 6},
                {"idx": 2, "type": "dialogue", "duration": 6},
                {"idx": 3, "type": "broll", "duration": 6},
            ],
            "shots": [
                {
                    "idx": 1, "video_master_idx": 1,
                    "audio_master_idx": 1, "duration": 6,
                },
                {
                    "idx": 2, "video_master_idx": 3,
                    "audio_master_idx": 2, "duration": 6,
                },
            ],
        }

        result = production_compiler.compile_ir(
            creative_ir.from_storyboard(creative_source),
            job_id="J-runtime-timeline",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
            runtime_storyboard=runtime_source,
        )

        runtime = result.spec.runtime_storyboard
        master_indexes = {master["idx"] for master in runtime["masters"]}
        self.assertEqual(master_indexes, {1, 2})
        self.assertEqual(len(runtime["shots"]), 2)
        self.assertEqual(
            {shot["source_beat_id"] for shot in runtime["shots"]},
            {"beat-1", "beat-2"},
        )
        self.assertTrue(all(
            shot["video_master_idx"] in master_indexes
            and shot["audio_master_idx"] in master_indexes
            for shot in runtime["shots"]
        ))

    def test_provider_capabilities_use_seedance_single_source_of_truth(self):
        from factory.clients import seedance

        capability = production_compiler.ProviderCapabilities.seedance_2_5()
        profile = seedance.capability_profile(capability.model)
        self.assertEqual(
            capability.max_request_duration_sec,
            seedance.SEEDANCE_25_MAX_GENERATION_DURATION_SEC,
        )
        self.assertEqual(
            capability.max_reference_images,
            profile["reference_limits"]["reference_image"],
        )
        self.assertEqual(
            capability.max_reference_videos,
            profile["reference_limits"]["reference_video"],
        )

    def test_empty_content_and_unknown_silent_cast_are_local_blockers(self):
        empty = production_compiler.compile_ir(
            creative_ir.from_storyboard({"characters": [], "beats": []}),
            job_id="J-empty",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=1000,
        )
        unknown = production_compiler.compile_ir(
            creative_ir.from_storyboard({
                "characters": [{"id": "known"}],
                "beats": [{
                    "id": "beat-1", "scene": "现场",
                    "characters": ["missing"], "action": "人物走入画面",
                }],
            }),
            job_id="J-unknown-cast",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=1000,
        )
        self.assertEqual({item.code for item in empty.blockers}, {"creative_empty"})
        self.assertIn("character_unknown", {item.code for item in unknown.blockers})

    def test_runtime_storyboard_is_frozen_inside_v2_spec_hash(self):
        source = load_profile_fixture("stable_single_dialogue")
        source["production_settings"] = {
            "generation_snapshot": {
                "model": "doubao-seedance-2-5-260628",
                "resolution": "720p",
                "ratio": "9:16",
            }
        }
        creative = creative_ir.from_storyboard(source)

        first = production_compiler.compile_ir(
            creative,
            job_id="J-FROZEN",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
            runtime_storyboard=source,
        )
        changed = {
            **source,
            "production_settings": {
                "generation_snapshot": {
                    **source["production_settings"]["generation_snapshot"],
                    "resolution": "1080p",
                }
            },
        }
        second = production_compiler.compile_ir(
            creative,
            job_id="J-FROZEN",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
            runtime_storyboard=changed,
        )

        self.assertEqual(
            first.spec.runtime_storyboard["production_settings"]
            ["generation_snapshot"]["resolution"],
            "720p",
        )
        self.assertNotEqual(first.spec.spec_hash, second.spec.spec_hash)

    def test_compiled_scene_anchor_removes_stale_legacy_site_anchor(self):
        source = {
            "title": "legacy anchor",
            "characters": [{"id": "linxia", "name": "Lin Xia"}],
            "masters": [{
            "idx": 1,
            "type": "dialogue",
            "speaker": "linxia",
            "cast": ["linxia"],
            "scene": "storm command room",
            "scene_anchor_key": "scene-current",
            "duration": 8,
            "voiceover": "ready",
            "site_anchor_key": "site-legacy",
            "site_anchor_description": "legacy location",
            }],
        }
        result = production_compiler.compile_ir(
            creative_ir.from_storyboard(source),
            job_id="J-SCENE-AUTHORITY",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
            runtime_storyboard=source,
        )

        runtime_master = result.spec.runtime_storyboard["masters"][0]
        self.assertTrue(runtime_master["scene_anchor_key"])
        self.assertNotIn("site_anchor_key", runtime_master)
        self.assertNotIn("site_anchor_description", runtime_master)

    def test_compiled_scene_anchor_uses_empty_location_contract_not_actor_action(self):
        source = {
            "title": "clean scene anchor",
            "characters": [{"id": "linxia", "name": "Lin Xia"}],
            "masters": [{
                "idx": 1,
                "type": "dialogue",
                "speaker": "linxia",
                "cast": ["linxia"],
                "scene": "close-up of Lin Xia speaking in the storm room",
                "action": "Lin Xia walks toward camera",
                "scene_anchor_key": "scene-storm-room",
                "scene_anchor_description": "actor-contaminated legacy text",
                "continuity_contract": {
                    "scene": {
                        "key": "scene-storm-room",
                        "description": "empty storm command room with radar wall",
                    }
                },
                "duration": 8,
                "voiceover": "ready",
            }],
        }
        result = production_compiler.compile_ir(
            creative_ir.from_storyboard(source),
            job_id="J-CLEAN-SCENE-ANCHOR",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
            runtime_storyboard=source,
        )

        runtime_master = result.spec.runtime_storyboard["masters"][0]
        self.assertEqual(
            runtime_master["scene_anchor_description"],
            "empty storm command room with radar wall",
        )
        self.assertNotIn("Lin Xia", runtime_master["scene_anchor_description"])

    def test_paid_music_is_an_explicit_owned_graph_node(self):
        source = load_profile_fixture("stable_single_dialogue")
        source["production_settings"] = {"music_provider": "suno"}
        result = production_compiler.compile_ir(
            creative_ir.from_storyboard(source),
            job_id="J-PAID-MUSIC",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
            runtime_storyboard=source,
        )

        bgm = next(node for node in result.spec.nodes if node.node_id == "bgm")
        self.assertTrue(bgm.paid)
        self.assertEqual(bgm.input["provider"], "suno")
        self.assertIn(
            ("bgm", "local_compose"),
            {(edge.from_node_id, edge.to_node_id) for edge in result.spec.edges},
        )


if __name__ == "__main__":
    unittest.main()
