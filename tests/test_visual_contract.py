"""Offline visual direction contract across script, IR, compiler and prompts."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from factory import creative_ir, production_compiler, script_drafts
from factory.steps import s3_shots


def draft():
    return {"title": "Two scenes", "creative_thesis": "A visible decision",
            "characters": [{"id": "a", "name": "Alice"}, {"id": "b", "name": "Bob"}],
            "beats": [
                {"beat_id": "arrival", "beat": "hook", "speaker": "a", "dialogue": "请先停下。",
                 "duration_sec": 3, "visual_intent": "Alice raises a hand at the gate",
                 "scene": "gate", "continuity_id": "gate", "characters": ["a", "b"],
                 "source_ids": ["SRC-1"], "camera_intent": "wide tracking left",
                 "must_preserve": [{"id": "decision", "description": "Alice gives the stop order", "severity": "critical"}, "exact words"],
                 "may_adapt": ["lens"], "custom_direction": {"keep": True},
                 "visual_requirements": [{"id": "hand", "description": "Raised hand is visible", "source_ids": ["SRC-1"], "priority": "critical"}],
                 "reference_control": {"choice": "motion_reference", "rationale": "Authored gesture timing"}},
                {"id": "reaction", "beat": "solution", "speaker": "b", "dialogue": "收到。",
                 "speech_mode": "off_screen", "duration_sec": 3,
                 "visual_intent": "Alice silently opens her mouth in surprise inside the workshop",
                 "scene": "workshop", "continuity_id": "workshop", "characters": ["a"],
                 "source_ids": ["SRC-1"], "camera_intent": "reaction close-up on Alice",
                 "visual_requirements": [{"id": "reaction-visible", "description": "Silent surprise is visible"}]}]}


class VisualContractTests(unittest.TestCase):
    def normalize(self, value=None):
        return script_drafts.normalize_response({"variants": [value or draft()]}, allowed_source_ids={"SRC-1"})["variants"][0]

    def compile(self, storyboard):
        ir = creative_ir.from_storyboard(storyboard)
        restored = creative_ir.from_dict(ir.as_dict())
        self.assertEqual(restored.as_dict(), ir.as_dict())
        result = production_compiler.compile_ir(restored, job_id="visual-offline",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000, runtime_storyboard=storyboard)
        self.assertTrue(result.compilable, result.issues)
        return restored, result.spec.runtime_storyboard

    def test_normalization_keeps_stable_identity_and_structured_direction(self):
        source = draft()
        original = deepcopy(source)
        normalized = self.normalize(source)
        first = normalized["beats"][0]
        self.assertEqual(first["beat_id"], "arrival")
        self.assertEqual(normalized["beats"][1]["id"], "reaction")
        for field in ("camera_intent", "must_preserve", "may_adapt", "visual_requirements", "reference_control", "custom_direction"):
            self.assertEqual(first[field], source["beats"][0][field])
        self.assertEqual(source, original)
        del source["beats"][0]["beat_id"]
        self.assertEqual(self.normalize(source)["beats"][0]["beat_id"], "beat-1")

    def test_end_to_end_visual_plan_and_native_prompt(self):
        normalized = self.normalize()
        original = deepcopy(normalized)
        board = script_drafts._storyboard(normalized)
        ir, runtime = self.compile(board)
        first = ir.beats[0]
        self.assertEqual(first.beat_id, "arrival")
        self.assertEqual(first.must_preserve, ("decision", "exact words"))
        self.assertEqual(first.extensions["must_preserve_details"][0]["description"], "Alice gives the stop order")
        self.assertTrue(first.extensions["custom_direction"]["keep"])
        masters = runtime["masters"]
        for master in masters:
            plan = master["visual_plan"]
            self.assertTrue(plan["intent_only"])
            self.assertEqual(len(plan["requirement_set_hash"]), 64)
            self.assertEqual(plan["beats"][0]["video_master_idx"], master["idx"])
        self.assertEqual(masters[0]["visual_plan"]["beats"][0]["reference_control"]["choice"], "motion_reference")
        first_master = deepcopy(masters[0])
        first_master["cast"] = [{"id": "a", "name": "Alice"}, {"id": "b", "name": "Bob"}]
        prompt = s3_shots._build_native_dialogue_prompt(first_master, {"id": "a", "name": "Alice", "generation_mode": "ai_generated"})
        self.assertIn("wide tracking left", prompt)
        self.assertIn("Raised hand is visible", prompt)
        self.assertIn("Alice gives the stop order", prompt)
        self.assertIn("嘴部保持清晰可见", prompt)
        background = s3_shots._build_background_voiceover_prompt(masters[1], cast_characters=[{"id": "a"}], reference_manifest=[])
        self.assertIn("reaction close-up on Alice", background)
        self.assertIn("Silent surprise is visible", background)
        self.assertIn("惊讶张嘴", background)
        self.assertNotIn("嘴部保持清晰可见", background)
        self.assertEqual(normalized, original)

    def test_broll_overlay_requirement_links_to_video_master(self):
        source = draft()
        source["beats"][0]["broll_required"] = True
        board = script_drafts._storyboard(self.normalize(source))
        _, runtime = self.compile(board)
        edit = runtime["shots"][0]
        self.assertNotEqual(edit["video_master_idx"], edit["audio_master_idx"])
        master = next(m for m in runtime["masters"] if m["idx"] == edit["video_master_idx"])
        link = next(r for r in master["visual_plan"]["requirement_links"] if r["id"] == "hand")
        self.assertEqual(link["source_beat_id"], "arrival")
        self.assertEqual(link["video_master_idx"], edit["video_master_idx"])
        self.assertEqual(link["description"], "Raised hand is visible")
        self.assertIn("Raised hand is visible", s3_shots._build_broll_video_prompt(master))
        self.assertEqual(runtime["shots"][1]["source_beat_id"], "reaction")

    def test_fragmented_beats_keep_source_identity_without_collision(self):
        source = draft()
        source["beats"][0]["dialogue"] = "请马上停下并检查设备。" * 18
        source["beats"][0]["duration_sec"] = 48
        board = script_drafts._storyboard(self.normalize(source))
        ir, runtime = self.compile(board)
        ids = [b.beat_id for b in ir.beats]
        self.assertEqual(len(ids), len(set(ids)))
        linked = [s for s in runtime["shots"] if s["source_beat_id"] == "arrival"]
        self.assertGreater(len(linked), 1)
        for edit in linked:
            self.assertEqual(edit["visual_requirements"][0]["description"], "Raised hand is visible")

    def test_duplicate_beat_and_conflicting_requirement_ids_fail(self):
        for mutation in ("beat", "alias", "requirement", "preserve"):
            source = draft()
            if mutation == "beat": source["beats"][1]["id"] = "arrival"
            if mutation == "alias": source["beats"][0]["id"] = "other"
            if mutation == "requirement": source["beats"][1]["visual_requirements"] = [{"id": "hand", "description": "Different action"}]
            if mutation == "preserve": source["beats"][1]["must_preserve"] = [{"id": "decision", "description": "Different decision"}]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.normalize(source)

    def test_malformed_requirement_shape_text_source_and_control_fail(self):
        for value in ("hand", [{}], [{"id": "hand", "description": " "}], [{"id": 2, "description": "hand"}],
                      [{"id": "hand", "description": "hand", "source_ids": "SRC-1"}],
                      [{"id": "hand", "description": "hand", "source_ids": ["unknown"]}]):
            source = draft(); source["beats"][0]["visual_requirements"] = value
            with self.subTest(value=value), self.assertRaises(ValueError): self.normalize(source)
        source = draft(); source["beats"][0]["reference_control"]["choice"] = "generate_paid_refs"
        with self.assertRaises(ValueError): self.normalize(source)

    def test_ir_boundaries_reject_duplicate_authored_ids(self):
        source = draft(); source["beats"][1]["id"] = "arrival"
        with self.assertRaises(ValueError): creative_ir.from_storyboard(source)
        valid = creative_ir.from_storyboard(draft()).as_dict()
        valid["beats"][1]["beat_id"] = "arrival"
        with self.assertRaises(ValueError): creative_ir.from_dict(valid)

    def test_public_hash_is_stable_and_sensitive_to_description(self):
        from factory import visual_contract
        reqs = draft()["beats"][0]["visual_requirements"] + draft()["beats"][1]["visual_requirements"]
        self.assertEqual(visual_contract.requirement_set_hash(reqs), visual_contract.requirement_set_hash(list(reversed(reqs))))
        changed = deepcopy(reqs); changed[0]["description"] = "Different hand"
        self.assertNotEqual(visual_contract.requirement_set_hash(reqs), visual_contract.requirement_set_hash(changed))
        self.assertEqual(visual_contract.normalize_requirements(reqs, allowed_source_ids={"SRC-1"}), reqs)

    def test_legacy_factual_preservation_does_not_create_visual_requirements(self):
        source = draft()
        for beat in source["beats"]:
            for field in ("visual_requirements", "camera_intent", "reference_control", "beat_id", "id"):
                beat.pop(field, None)
        _, runtime = self.compile(script_drafts._storyboard(self.normalize(source)))
        for master in runtime["masters"]:
            self.assertEqual(master.get("visual_requirements", []), [])
            self.assertNotIn("visual_requirements", master["quality_gates"])

    def test_generation_prompt_requests_direction_without_paid_authorization(self):
        prompt = script_drafts._prompt({}, [], {})
        for field in ("beat_id", "camera_intent", "must_preserve", "may_adapt", "visual_requirements", "reference_control", "speech_mode"):
            self.assertIn(field, prompt)
        self.assertIn("预算", prompt)
        self.assertIn("事实", prompt)

    def test_raw_ir_provider_fragments_have_distinct_unit_links(self):
        source = draft(); source["beats"][0]["duration_sec"] = 65
        _, runtime = self.compile(source)
        plans = [b for m in runtime["masters"] for b in m["visual_plan"]["beats"]
                 if b["source_beat_id"] == "arrival"]
        self.assertEqual(len(plans), 3)
        self.assertEqual(len({b["unit_id"] for b in plans}), 3)
        self.assertEqual([b["fragment_index"] for b in plans], [1, 2, 3])

    def test_faceless_voiceover_uses_authored_requirements_and_camera(self):
        source = draft(); source["beats"][0]["speech_mode"] = "narration"
        source["beats"][0]["characters"] = []
        source["beats"][0]["visual_intent"] = "Gate latch closes"
        _, runtime = self.compile(source)
        prompt = s3_shots._build_scene_voiceover_video_prompt(runtime["masters"][0])
        self.assertIn("wide tracking left", prompt)
        self.assertIn("Raised hand is visible", prompt)

    def test_mixed_prompt_allows_authored_reaction_without_removing_onscreen_lips(self):
        source = draft(); source["beats"][1]["continuity_id"] = "gate"
        source["beats"][1]["scene"] = "gate"
        board = script_drafts._storyboard(self.normalize(source))
        _, runtime = self.compile(board)
        master = deepcopy(runtime["masters"][0]); master["cast"] = [{"id": "a"}, {"id": "b"}]
        prompt = s3_shots._build_native_dialogue_prompt(master, {"id": "a", "generation_mode": "ai_generated"})
        self.assertIn("嘴部保持清晰可见", prompt)
        self.assertIn("惊讶张嘴", prompt)
        self.assertNotIn("其他同场人物始终闭嘴", prompt)
        self.assertNotIn("不得加入旁白、未列出的第三人声", prompt)
        self.assertNotIn("只在台词所在的关键音节时间窗保护口型可见性", prompt)
        self.assertIn("仅 on_screen", prompt)

    def test_ir_restore_retains_new_unknown_fields(self):
        payload = creative_ir.from_storyboard(draft()).as_dict()
        payload["unknown_root"] = {"new": 1}
        payload["beats"][0]["unknown_beat"] = {"new": 2}
        restored = creative_ir.from_dict(payload)
        self.assertEqual(restored.extensions["unknown_root"], {"new": 1})
        self.assertEqual(restored.beats[0].extensions["unknown_beat"], {"new": 2})

    def test_single_character_visual_contract_is_not_replaced_by_presenter_template(self):
        source = draft(); source["beats"] = source["beats"][:1]
        source["characters"] = source["characters"][:1]
        source["beats"][0]["characters"] = ["a"]
        board = script_drafts._storyboard(self.normalize(source))
        master = board["masters"][0]
        self.assertEqual(master["action"], source["beats"][0]["visual_intent"])
        self.assertNotIn("设备租赁办公室", master["scene"])
        _, runtime = self.compile(board)
        master = deepcopy(runtime["masters"][0]); master["cast"] = [{"id": "a"}]
        prompt = s3_shots._build_native_dialogue_prompt(master, {"id": "a", "generation_mode": "ai_generated"})
        self.assertIn("Raised hand is visible", prompt)
        self.assertIn("wide tracking left", prompt)

    def test_compilation_rebuilds_stale_plans_without_mutating_frozen_input(self):
        source = script_drafts._storyboard(self.normalize())
        for master in source["masters"]:
            for beat in master["performance_beats"]:
                beat.pop("visual_requirements", None)
            master["visual_requirements"] = [{"id": "stale", "description": "Old requirement"}]
            master["visual_plan"] = {"requirement_set_hash": "old"}
        source["visual_requirements"] = [{"id": "stale", "description": "Old requirement"}]
        source["requirement_set_hash"] = "old"
        original = deepcopy(source)
        _, runtime = self.compile(source)
        self.assertEqual(source, original)
        self.assertEqual(runtime.get("visual_requirements", []), [])
        self.assertNotEqual(runtime.get("requirement_set_hash"), "old")
        for master in runtime["masters"]:
            self.assertEqual(master.get("visual_requirements", []), [])

    def test_legacy_runtime_timeline_validation_leaves_paid_plan_untouched(self):
        source = script_drafts._storyboard(self.normalize())
        original = deepcopy(source)
        self.assertEqual(production_compiler.ensure_director_timeline(source), source)
        self.assertEqual(source, original)

    def test_offscreen_broll_does_not_add_audio_identity_to_visible_cast(self):
        source = draft(); source["beats"][1]["broll_required"] = True
        board = script_drafts._storyboard(self.normalize(source))
        broll = next(m for m in board["masters"] if m["type"] == "broll")
        self.assertEqual(broll["cast"], ["a"])
        _, runtime = self.compile(board)
        broll = next(m for m in runtime["masters"] if m["type"] == "broll")
        self.assertEqual(broll["cast"], ["a"])


if __name__ == "__main__": unittest.main()
