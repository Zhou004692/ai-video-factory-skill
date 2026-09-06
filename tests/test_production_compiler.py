"""Creative IR to Production Spec v2 compiler tests."""
from __future__ import annotations

from pathlib import Path
from copy import deepcopy
import base64
import json
import sys
import tempfile
import unittest
from unittest import mock

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

    def test_runtime_director_timeline_keeps_broll_before_later_cta(self):
        runtime_source = {
            "title": "B-roll before CTA",
            "production_mode": "native_master_local_director",
            "characters": [{"id": "lead", "name": "Lead"}],
            "masters": [
                {
                    "idx": 1,
                    "type": "dialogue",
                    "scene_anchor_key": "scene-repair",
                    "scene": "repair point",
                    "cast": ["lead"],
                    "speaker": "lead",
                    "duration": 6,
                    "voiceover": "repair completed",
                    "performance_beats": [{
                        "source_beat_id": "beat-reversal",
                        "beat": "reversal",
                        "speaker": "lead",
                        "dialogue": "repair completed",
                        "duration": 6,
                        "visual_intent": "the panel stops shaking",
                    }],
                },
                {
                    "idx": 2,
                    "type": "dialogue",
                    "scene_anchor_key": "scene-finish",
                    "scene": "open production lane",
                    "cast": ["lead"],
                    "speaker": "lead",
                    "duration": 6,
                    "voiceover": "production resumes",
                    "performance_beats": [{
                        "source_beat_id": "beat-cta",
                        "beat": "cta",
                        "speaker": "lead",
                        "dialogue": "production resumes",
                        "duration": 6,
                        "visual_intent": "the lane reopens",
                    }],
                },
                {
                    "idx": 3,
                    "type": "broll",
                    "scene_anchor_key": "scene-repair",
                    "scene": "repair point",
                    "cast": ["lead"],
                    "duration": 6,
                    "action": "close-up comparison of the stable panel",
                },
            ],
            "shots": [
                {
                    "idx": 1,
                    "source_beat_id": "beat-reversal",
                    "beat": "reversal",
                    "video_master_idx": 3,
                    "audio_master_idx": 1,
                    "video_start": 0,
                    "audio_start": 0,
                    "duration": 6,
                    "subtitle": "repair completed",
                    "visual_intent": "the panel stops shaking",
                },
                {
                    "idx": 2,
                    "source_beat_id": "beat-cta",
                    "beat": "cta",
                    "video_master_idx": 2,
                    "audio_master_idx": 2,
                    "video_start": 0,
                    "audio_start": 0,
                    "duration": 6,
                    "subtitle": "production resumes",
                    "visual_intent": "the lane reopens",
                },
            ],
        }

        result = production_compiler.compile_ir(
            creative_ir.from_storyboard(runtime_source),
            job_id="J-broll-before-cta",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
            runtime_storyboard=runtime_source,
        )

        self.assertTrue(result.compilable)
        runtime = result.spec.runtime_storyboard
        self.assertEqual(
            [shot["source_beat_id"] for shot in runtime["shots"]],
            ["beat-reversal", "beat-cta"],
        )
        self.assertEqual(
            [shot["video_master_idx"] for shot in runtime["shots"]],
            [3, 2],
        )
        self.assertEqual(
            [shot["audio_master_idx"] for shot in runtime["shots"]],
            [1, 2],
        )

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


class SpeechSemanticsCompilerTests(unittest.TestCase):
    def compile_source(self, beats, characters=None):
        source = {
            "characters": [{"id": item} for item in (
                characters if characters is not None else ["worker"]
            )],
            "beats": [{"id": f"turn-{index}", "scene": "yard",
                       "duration_sec": 4, **beat}
                      for index, beat in enumerate(beats, 1)],
        }
        return production_compiler.compile_ir(
            creative_ir.from_storyboard(source), job_id="J-speech",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000, runtime_storyboard=source,
        )

    def test_audio_only_modes_never_fabricate_an_onscreen_speaker(self):
        for mode, speaker in (("off_screen", "customer_voice"),
                              ("narration", "worker"),
                              (None, "旁白"), (None, "背景台词")):
            for visible in ([], ["worker"]):
                with self.subTest(mode=mode, speaker=speaker, visible=visible):
                    beat = {"speaker": speaker, "dialogue": "Ready?",
                            "characters": visible, "tone": "quiet"}
                    if mode:
                        beat["speech_mode"] = mode
                    result = self.compile_source([beat])
                    self.assertTrue(result.compilable, result.issues)
                    master = result.spec.masters[0]
                    self.assertEqual(master.character_ids, tuple(visible))
                    self.assertEqual(master.speaker_ids, ())
                    self.assertNotIn("lip_sync", master.quality_gates)
                    self.assertIn("transcript_complete", master.quality_gates)
                    self.assertIn("subtitle_sync", master.quality_gates)
                    self.assertEqual(result.spec.complexity["dimensions"]
                                     ["lip_sync_required_count"], 0)
                    runtime = result.spec.runtime_storyboard["masters"][0]
                    self.assertEqual(runtime["speaker"], "")
                    self.assertEqual(runtime["speakers"], [])
                    self.assertEqual(runtime["audio_speakers"], [speaker])
                    self.assertEqual(runtime["voiceover"], "Ready?")
                    self.assertEqual(runtime["subtitle"], "Ready?")
                    self.assertEqual(runtime["performance_beats"][0]["tone"], "quiet")
                    identity_edges = [edge.from_node_id for edge in result.spec.edges
                                      if edge.edge_type == "identity_reference"]
                    self.assertEqual(identity_edges,
                                     [f"character_anchor:{item}" for item in visible])

    def test_explicit_visible_audio_character_is_preserved_without_lip_sync(self):
        result = self.compile_source([{
            "speaker": "worker", "speech_mode": "off_screen",
            "dialogue": "Inner thought", "cast": [{"id": "worker"}],
        }])
        self.assertTrue(result.compilable, result.issues)
        self.assertEqual(result.spec.masters[0].character_ids, ("worker",))
        self.assertEqual(result.spec.masters[0].speaker_ids, ())
        self.assertNotIn("lip_sync", result.spec.masters[0].quality_gates)

    def test_silent_onscreen_actor_cannot_trigger_lip_sync_for_other_audio(self):
        for beats in (
            [{"speaker": "worker", "dialogue": ""}],
            [{"speaker": "worker", "dialogue": ""},
             {"speaker": "narrator", "dialogue": "They leave."}],
            [{"characters": ["worker"]}],
        ):
            with self.subTest(beats=beats):
                result = self.compile_source(beats)
                self.assertTrue(result.compilable, result.issues)
                self.assertEqual(result.spec.masters[0].character_ids, ("worker",))
                self.assertEqual(result.spec.masters[0].speaker_ids, ())
                runtime = result.spec.runtime_storyboard["masters"][0]
                self.assertEqual(runtime["speaker"], "")
                self.assertEqual(runtime["speakers"], [])
                self.assertNotIn("lip_sync", result.spec.masters[0].quality_gates)
                if not any(beat.get("dialogue") for beat in beats):
                    self.assertNotIn("transcript_complete", result.spec.masters[0].quality_gates)
                    self.assertNotIn("subtitle_sync", result.spec.masters[0].quality_gates)

    def test_mixed_inferred_modes_in_one_turn_need_explicit_correction(self):
        for speakers in (["worker", "背景台词", "narrator"],
                         ["narrator", "背景台词", "worker"]):
            with self.subTest(speakers=speakers):
                result = self.compile_source([{
                    "speakers": speakers, "dialogue": "Ready? Ready.",
                }])
                self.assertFalse(result.compilable)
                self.assertEqual({issue.code for issue in result.blockers},
                                 {"speech_mode_ambiguous"})
                self.assertEqual(result.blockers[0].stage, "creative_ir")
                self.assertEqual(result.blockers[0].subject, "turn-1")

    def test_whitespace_only_duration_split_has_no_spoken_quality_gates(self):
        result = self.compile_source([{
            "speaker": "worker", "dialogue": "A B", "duration_sec": 90,
        }])
        self.assertTrue(result.compilable, result.issues)
        self.assertEqual([master.dialogue for master in result.spec.masters],
                         ["A", " ", "B"])
        silent = result.spec.masters[1]
        self.assertEqual(silent.character_ids, ("worker",))
        self.assertEqual(silent.speaker_ids, ())
        self.assertTrue({"lip_sync", "transcript_complete", "subtitle_sync"}
                        .isdisjoint(silent.quality_gates))
        self.assertEqual(result.spec.runtime_storyboard["masters"][1]["type"], "broll")

    def test_explicit_global_mode_overrides_all_speaker_hints(self):
        for mode, expected_cast, expected_speakers in (
            ("on_screen", ("worker", "narrator"), ("worker", "narrator")),
            ("off_screen", (), ()), ("narration", (), ()),
        ):
            with self.subTest(mode=mode):
                result = self.compile_source([{
                    "speakers": ["worker", "narrator"],
                    "speech_mode": mode, "dialogue": "Ready? Ready.",
                }], characters=["worker", "narrator"])
                self.assertTrue(result.compilable, result.issues)
                self.assertEqual(result.spec.masters[0].character_ids, expected_cast)
                self.assertEqual(result.spec.masters[0].speaker_ids, expected_speakers)
                self.assertEqual("lip_sync" in result.spec.masters[0].quality_gates,
                                 mode == "on_screen")

    def test_homogeneous_implicit_speaker_lists_remain_supported(self):
        for speakers, characters, expected in (
            (["worker", "lead"], ["worker", "lead"], ("worker", "lead")),
            (["旁白", "narrator"], [], ()),
            (["背景台词", "offscreen"], [], ()),
        ):
            with self.subTest(speakers=speakers):
                result = self.compile_source([{
                    "speakers": speakers, "dialogue": "Ready? Ready.",
                }], characters=characters)
                self.assertTrue(result.compilable, result.issues)
                self.assertEqual(result.spec.masters[0].speaker_ids, expected)
                self.assertEqual(result.spec.runtime_storyboard["masters"][0]
                                 ["audio_speakers"], speakers)

    def test_unknown_visible_identity_is_still_blocking(self):
        for beat, codes in (
            ({"speaker": "unknown", "dialogue": "Ready."}, {"speaker_unknown"}),
            ({"speaker": "narrator", "speech_mode": "on_screen",
              "dialogue": "Ready."}, {"speaker_unknown"}),
            ({"speaker": "unknown", "speech_mode": "off_screen",
              "characters": ["unknown"], "dialogue": "Ready."}, {"character_unknown"}),
            ({"speakers": ["narrator", "unknown"], "dialogue": "Ready."},
             {"speech_mode_ambiguous", "speaker_unknown"}),
        ):
            with self.subTest(beat=beat):
                result = self.compile_source([beat])
                self.assertFalse(result.compilable)
                self.assertEqual({issue.code for issue in result.blockers}, codes)

    def test_invalid_explicit_modes_are_reported_without_changing_normalization(self):
        for mode in ("offscreen", "on_sceen", "unsupported", 0, False, []):
            with self.subTest(mode=mode):
                result = self.compile_source([{
                    "speaker": "worker", "speech_mode": mode, "dialogue": "Ready.",
                }])
                self.assertFalse(result.compilable)
                self.assertEqual({issue.code for issue in result.blockers},
                                 {"speech_mode_invalid"})
                self.assertEqual(result.blockers[0].stage, "creative_ir")
                self.assertIn(str(mode).lower(), result.blockers[0].message)
                self.assertEqual(creative_ir.normalize_speech_mode(mode), "on_screen")

    def test_separate_turn_modes_preserve_audio_and_performance_metadata(self):
        source = {
            "characters": [{"id": "worker"}],
            "masters": [{
                "idx": 1, "scene": "yard", "cast": ["worker"],
                "speaker": "customer_voice", "speech_mode": "off_screen",
                "duration": 12,
                "performance_beats": [
                    {"dialogue": "Ready?", "duration": 4, "tone": "question",
                     "facial_expression": "listening", "gesture": "checks machine"},
                    {"speaker": "worker", "dialogue": "Ready.", "duration": 4,
                     "speech_mode": "on_screen", "tone": "confident"},
                    {"speaker": "narrator", "dialogue": "They leave.", "duration": 4,
                     "speech_mode": "narration", "tone": "calm"},
                ],
            }],
        }
        original = deepcopy(source)
        result = production_compiler.compile_ir(
            creative_ir.from_storyboard(source), job_id="J-turns",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000, runtime_storyboard=source,
        )
        self.assertTrue(result.compilable, result.issues)
        self.assertEqual(len(result.spec.masters), 1)
        master = result.spec.masters[0]
        self.assertEqual(master.character_ids, ("worker",))
        self.assertEqual(master.speaker_ids, ("worker",))
        self.assertIn("lip_sync", master.quality_gates)
        self.assertEqual(result.spec.complexity["dimensions"]["lip_sync_required_count"], 1)
        runtime = result.spec.runtime_storyboard["masters"][0]
        self.assertEqual(runtime["audio_speakers"], ["customer_voice", "worker", "narrator"])
        self.assertEqual(runtime["voiceover"], "Ready?Ready.They leave.")
        turns = runtime["performance_beats"]
        self.assertEqual([turn["speech_mode"] for turn in turns],
                         ["off_screen", "on_screen", "narration"])
        self.assertEqual([turn["tone"] for turn in turns], ["question", "confident", "calm"])
        self.assertEqual(turns[0]["facial_expression"], "listening")
        self.assertEqual(turns[0]["gesture"], "checks machine")
        self.assertEqual(source, original)

    def test_onscreen_turn_replaces_a_stale_background_voiceover_route(self):
        source = {
            "characters": [{"id": "worker"}],
            "masters": [{
                "idx": 1, "scene": "yard", "cast": ["worker"],
                "render_mode": "background_voiceover",
                "performance_beats": [{
                    "speaker": "worker", "speech_mode": "on_screen",
                    "dialogue": "Ready.", "duration": 4,
                }],
            }],
        }
        result = production_compiler.compile_ir(
            creative_ir.from_storyboard(source), job_id="J-stale-route",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000, runtime_storyboard=source,
        )
        self.assertTrue(result.compilable, result.issues)
        self.assertEqual(result.spec.runtime_storyboard["masters"][0]["render_mode"],
                         "cinematic_character")


class RuntimeSpeechDispatchTests(unittest.IsolatedAsyncioTestCase):
    """Real compiler/materialization/dispatch with approved local anchor fixtures.

    Only infrastructure paths and the final provider boundary are replaced.
    These tests prove routing and request semantics, not provider/media quality.
    """

    def setUp(self):
        from factory import db
        from factory.steps import s3_shots

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

        def tracked_connection():
            database = db.conn()
            self.addCleanup(database.close)
            return database

        for patcher in (
            mock.patch.object(db, "database_path", return_value=self.root / "factory.db"),
            mock.patch.object(s3_shots, "conn", side_effect=tracked_connection),
            mock.patch.object(s3_shots, "ASSETS_DIR", self.root / "assets"),
            mock.patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.anchor_path = self.root / "approved.png"
        self.anchor_path.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jz1kAAAAASUVORK5CYII="
        ))

    def prepare(self, beats, *, visible=True, approval="APPROVED", character_ids=("worker",)):
        from factory import character_assets, db, production_adapters, production_config

        characters = ([{"id": item, "name": item.title(),
                        "generation_mode": "ai_generated"}
                       for item in character_ids] if visible else [])
        source = {
            "characters": characters,
            "production_settings": {
                "character_anchor_generator": "seedream_5_0",
                "scene_anchor_generator": "seedream_5_0",
                "generation_snapshot": production_config.freeze_generation_snapshot(
                    {"audio_mode": "seedance_native", "qa_enabled": True}, env={},
                ),
            },
            "beats": [{"id": f"turn-{index}", "scene": "yard",
                       "duration_sec": 4, "cast": list(character_ids) if visible else [],
                       **beat} for index, beat in enumerate(beats, 1)],
        }
        result = production_compiler.compile_ir(
            creative_ir.from_storyboard(source), job_id="J-runtime-speech",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000, runtime_storyboard=source,
        )
        self.assertTrue(result.compilable, result.issues)
        spec = result.spec
        database = db.conn()
        try:
            database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) VALUES(?,?,?,?,?)",
                (spec.job_id, "T-speech", "Runtime speech", "PRODUCTION_RUNNING",
                 json.dumps(spec.runtime_storyboard)),
            )
            production_adapters.materialize_frozen_execution_inputs(database, spec)
            for character in characters:
                database.execute(
                    "INSERT INTO character_identity_anchors(job_id,identity_key,character_id,"
                    "display_name,source_type,state,anchor_mode,image_path,image_url,"
                    "generator,approval_state) VALUES(?,?,?,?,?,'READY','static_image',?,?,?,?)",
                    (spec.job_id, character_assets.character_identity_key(character),
                     character["id"], character["name"], "seedream_identity_image",
                     str(self.anchor_path), f"https://example.invalid/{character['id']}.png",
                     "seedream_5_0", approval),
                )
            # Explicit pre-approved fixture for the executor's current scene requirement;
            # this does not assert that the compiler has scheduled the scene anchor.
            for scene_key in {master.scene_key for master in spec.masters}:
                database.execute(
                    "INSERT INTO scene_visual_anchors(job_id,anchor_key,state,image_path,"
                    "image_url,generator,approval_state) VALUES(?,?,'READY',?,?,?,?)",
                    (spec.job_id, scene_key, str(self.anchor_path),
                     "https://example.invalid/yard.png", "seedream_5_0", approval),
                )
            database.commit()
        finally:
            database.close()
        return spec

    async def request_at_boundary(self, spec):
        from factory.steps import s3_shots

        with mock.patch.object(
            s3_shots, "_generate_seedance_video",
            new=mock.AsyncMock(side_effect=RuntimeError("LOCAL_PROVIDER_BOUNDARY")),
        ) as generate:
            with self.assertRaisesRegex(RuntimeError, "LOCAL_PROVIDER_BOUNDARY"):
                await s3_shots.run(
                    spec.job_id, storyboard_override=spec.runtime_storyboard,
                    characters_override=list(spec.characters),
                    target_master_indexes={1}, prepare_anchors=False,
                )
        self.assertEqual(generate.await_count, 1)
        return generate.await_args.kwargs

    async def test_offscreen_visible_actor_reaches_voiceover_request_without_fake_speaker(self):
        spec = self.prepare([{
            "speaker": "customer_voice", "speech_mode": "off_screen",
            "dialogue": "Ready?", "action": "worker checks machine",
            "tone": "curious", "gesture": "checks latch",
        }])
        request = await self.request_at_boundary(spec)
        self.assertEqual(spec.masters[0].speaker_ids, ())
        self.assertEqual(request["expected_text"], "Ready?")
        self.assertFalse(request["forbid_people"])
        self.assertFalse(request["require_lip_sync"])
        self.assertFalse(request["require_broll_semantics"])
        self.assertTrue(request["route"].generate_audio)
        self.assertTrue(request["route"].qa_required)
        self.assertEqual(request["request_kwargs"]["references"],
                         ["https://example.invalid/yard.png", "https://example.invalid/worker.png"])
        manifest = request["request_kwargs"]["reference_manifest"]
        self.assertEqual([item["duty"] for item in manifest],
                         ["scene_continuity", "supporting_identity"])
        self.assertIn("customer_voice", request["prompt"])
        self.assertIn("Ready?", request["prompt"])
        self.assertIn("curious", request["prompt"])
        self.assertIn("checks latch", request["prompt"])
        self.assertIn("不做口型", request["prompt"])
        self.assertNotIn("不得出现任何可识别人脸", request["prompt"])
        self.assertEqual(request["continuity_context"]["identity_image_urls"],
                         ["https://example.invalid/worker.png"])

    async def test_narration_without_visible_cast_keeps_audio_and_scene_reference(self):
        spec = self.prepare([{
            "speaker": "narrator", "speech_mode": "narration", "dialogue": "They leave.",
        }], visible=False)
        request = await self.request_at_boundary(spec)
        self.assertEqual(spec.masters[0].character_ids, ())
        self.assertEqual(request["expected_text"], "They leave.")
        self.assertTrue(request["forbid_people"])
        self.assertFalse(request["require_lip_sync"])
        self.assertEqual(request["request_kwargs"]["references"],
                         ["https://example.invalid/yard.png"])
        self.assertIn("旁白", request["prompt"])

    async def test_explicit_visible_cast_is_not_filtered_by_audio_name_hints(self):
        spec = self.prepare([{
            "speaker": "customer_voice", "speech_mode": "off_screen", "dialogue": "Ready?",
        }], character_ids=("worker", "narrator"))
        request = await self.request_at_boundary(spec)
        self.assertEqual(spec.masters[0].character_ids, ("worker", "narrator"))
        self.assertIn("https://example.invalid/narrator.png",
                      request["request_kwargs"]["references"])
        self.assertIn("https://example.invalid/narrator.png",
                      request["continuity_context"]["identity_image_urls"])

    async def test_mixed_turns_keep_the_normal_onscreen_execution_route(self):
        spec = self.prepare([
            {"speaker": "customer_voice", "speech_mode": "off_screen", "dialogue": "Ready?"},
            {"speaker": "worker", "speech_mode": "on_screen", "dialogue": "Ready."},
        ])
        request = await self.request_at_boundary(spec)
        self.assertEqual(spec.masters[0].speaker_ids, ("worker",))
        self.assertEqual(spec.runtime_storyboard["masters"][0]["render_mode"], "cinematic_character")
        self.assertEqual(request["expected_text"], "Ready?Ready.")
        self.assertIn("customer_voice", request["prompt"])
        self.assertIn("worker", request["prompt"])
        self.assertTrue(request.get("require_lip_sync", True))

    async def test_voiceover_route_does_not_bypass_pending_anchor_review(self):
        from factory.steps import s3_shots

        spec = self.prepare([{
            "speaker": "narrator", "speech_mode": "narration", "dialogue": "They leave.",
        }], approval="PENDING_REVIEW")
        with mock.patch.object(s3_shots, "_generate_seedance_video", new=mock.AsyncMock()) as generate:
            with self.assertRaises(s3_shots.ContinuityAnchorReviewPendingError):
                await s3_shots.run(
                    spec.job_id, storyboard_override=spec.runtime_storyboard,
                    characters_override=list(spec.characters),
                    target_master_indexes={1}, prepare_anchors=False,
                )
        generate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
