"""Unified Brief -> script drafts -> storyboard tests."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

from click.testing import CliRunner


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


from factory import db, script_drafts, script_sources, topic_briefs  # noqa: E402
from factory.steps import s1_storyboard  # noqa: E402
import cli as cli_module  # noqa: E402


def _brief_payload() -> dict:
    angle = {
        "title": "设备租赁最贵的不是设备",
        "hook": "你以为设备租赁最贵的是设备？",
        "promise": "看懂信息和效率为什么更重要",
        "story_arc": "误区—事实—方法—行动",
        "evidence": ["客户需求散落在聊天记录里"],
        "risk_notes": [],
        "production_notes": "真人讲解加办公场景 B-roll",
    }
    return {
        "audience": "设备租赁老板",
        "source_score": {
            "relevance": 90,
            "story_potential": 86,
            "evidence_quality": 82,
            "production_fit": 88,
            "overall": 86.5,
            "reason": "来源明确",
        },
        "angles": [angle, {**angle, "title": "客户不是少，是信息散"},
                   {**angle, "title": "别再靠腿跑客户"}],
        "recommended_index": 0,
        "recommendation_reason": "最适合目标受众",
    }


def _variant(index: int, source_id: str) -> dict:
    labels = ("钩子", "冲突", "证据", "反转", "方案", "行动")
    beat_types = ("hook", "conflict", "evidence", "reversal", "solution", "cta")
    beats = []
    for beat_index, (beat_type, label) in enumerate(zip(beat_types, labels), 1):
        beats.append({
            "beat": beat_type,
            "speaker": "sales_xiaoneng",
            "dialogue": f"版本{index + 1}{label}台词",
            "duration_sec": 4,
            "visual_intent": f"{label}画面",
            "source_ids": [source_id],
            "broll_hint": f"{label}辅助画面",
        })
    return {
        "title": f"完整剧本版本{index + 1}",
        "creative_thesis": "信息与效率决定租赁增长",
        "target_duration_sec": 24,
        "beats": beats,
    }


class ScriptDraftGenerationTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(db.DDL)
        source = script_sources.ingest_text(
            self.database,
            "客户需求散落在多个业务员的聊天记录里，信息无法统一管理。",
            title="客户访谈",
            source_uri="manual://interview-01",
        )
        self.source_id = source.source_id
        self.brief_id = "BRIEF-script-engine"
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,"
            "brief_json,approved_topic_id,approved_job_id,approved_angle_index) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                self.brief_id, "approved", "设备租赁最贵的不是设备",
                json.dumps(_brief_payload(), ensure_ascii=False),
                "T-SCRIPT-01", "J-T-SCRIPT-01", 0,
            ),
        )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,target_audience) "
            "VALUES(?,?,?,?)",
            ("J-T-SCRIPT-01", "T-SCRIPT-01", "设备租赁最贵的不是设备",
             "设备租赁老板"),
        )
        self.database.execute(
            "INSERT INTO topic_brief_sources(brief_id,source_id) VALUES(?,?)",
            (self.brief_id, self.source_id),
        )
        self.database.commit()

    def tearDown(self):
        self.database.close()

    def test_generate_creates_exactly_three_complete_traceable_variants(self):
        calls = []

        def fake_generate(prompt, max_tokens):
            calls.append((prompt, max_tokens))
            return {
                "variants": [_variant(i, self.source_id) for i in range(3)],
                "recommended_index": 1,
                "recommendation_reason": "第二版节奏最好",
            }, {"input_tokens": 120, "output_tokens": 360}

        result = script_drafts.generate_drafts(
            self.database, self.brief_id, generate_json=fake_generate
        )

        self.assertEqual(len(result["drafts"]), 3)
        self.assertEqual(result["recommended_index"], 1)
        self.assertEqual(len(calls), 1)
        self.assertIn(self.source_id, calls[0][0])
        for draft in result["drafts"]:
            self.assertEqual(
                [beat["beat"] for beat in draft["script"]["beats"]],
                ["hook", "conflict", "evidence", "reversal", "solution", "cta"],
            )
            self.assertEqual(draft["status"], "candidate")
            self.assertEqual(draft["script"]["total_duration_sec"], 24)
            self.assertTrue(all(
                beat["source_ids"] == [self.source_id]
                for beat in draft["script"]["beats"]
            ))

    def test_single_usable_variant_is_cached_without_another_model_call(self):
        calls = []

        def fake_generate(prompt, max_tokens):
            calls.append((prompt, max_tokens))
            return {
                "variants": [_variant(0, self.source_id)],
                "recommended_index": 0,
                "recommendation_reason": "唯一完整版本",
            }, {"input_tokens": 10, "output_tokens": 20}

        first = script_drafts.generate_drafts(
            self.database, self.brief_id, generate_json=fake_generate
        )
        second = script_drafts.generate_drafts(
            self.database, self.brief_id, generate_json=fake_generate
        )

        self.assertEqual(len(first["drafts"]), 1)
        self.assertTrue(second["cached"])
        self.assertEqual(len(calls), 1)

    def test_generated_beat_has_no_script_level_upper_duration_limit(self):
        variant = _variant(0, self.source_id)
        variant["beats"][2]["duration_sec"] = 36
        variant["target_duration_sec"] = 56

        normalized = script_drafts._normalize_variant(
            variant, allowed_source_ids={self.source_id}
        )

        self.assertEqual(normalized["beats"][2]["duration_sec"], 36)
        self.assertEqual(normalized["total_duration_sec"], 56)

    def test_model_selected_beat_count_is_preserved(self):
        variant = _variant(0, self.source_id)
        variant["beats"].pop(4)
        variant["target_duration_sec"] = 20

        normalized = script_drafts._normalize_variant(
            variant, allowed_source_ids={self.source_id}
        )

        self.assertEqual(len(normalized["beats"]), 5)
        self.assertEqual(
            [beat["beat"] for beat in normalized["beats"]],
            ["hook", "conflict", "evidence", "reversal", "cta"],
        )

    def test_longer_narrative_compiles_without_edit_shot_count_gate(self):
        variant = _variant(0, self.source_id)
        for index in range(3):
            extra = deepcopy(variant["beats"][-1])
            extra["beat"] = f"follow_up_{index + 1}"
            extra["dialogue"] = f"补充行动信息 {index + 1}"
            variant["beats"].append(extra)
        variant["target_duration_sec"] = 36

        normalized = script_drafts._normalize_variant(
            variant, allowed_source_ids={self.source_id}
        )
        storyboard = script_drafts._storyboard(normalized)

        self.assertEqual(len(storyboard["shots"]), 9)
        s1_storyboard._validate(storyboard)

    def test_available_candidate_versions_are_preserved_without_exact_three_gate(self):
        raw = {
            "variants": [_variant(i, self.source_id) for i in range(2)],
            "recommended_index": 1,
            "recommendation_reason": "两版均完整",
        }

        normalized = script_drafts.normalize_response(
            raw, allowed_source_ids={self.source_id}
        )

        self.assertEqual(len(normalized["variants"]), 2)
        self.assertEqual(normalized["recommended_index"], 1)

    def test_long_dialogue_duration_estimate_has_no_upper_clip(self):
        dialogue = "这是一段需要完整表达的长台词，" * 16

        duration = script_drafts.estimate_dialogue_duration(dialogue)

        self.assertGreater(duration, 30)

    def test_invalid_derived_durations_are_reestimated_from_dialogue(self):
        invalid_values = (None, "", 0, -3, float("nan"), float("inf"))
        for invalid in invalid_values:
            with self.subTest(duration=invalid):
                variant = _variant(0, self.source_id)
                variant["beats"][2]["dialogue"] = "证据需要完整说清楚"
                variant["beats"][2]["duration_sec"] = invalid
                variant["target_duration_sec"] = 0

                normalized = script_drafts._normalize_variant(
                    variant, allowed_source_ids={self.source_id}
                )

                repaired = normalized["beats"][2]["duration_sec"]
                self.assertEqual(
                    repaired,
                    script_drafts.estimate_dialogue_duration(
                        "证据需要完整说清楚"
                    ),
                )
                self.assertGreater(repaired, 0)
                self.assertEqual(
                    normalized["target_duration_sec"],
                    normalized["total_duration_sec"],
                )

    def test_invalid_duration_does_not_hide_missing_dialogue(self):
        variant = _variant(0, self.source_id)
        variant["beats"][2]["dialogue"] = ""
        variant["beats"][2]["duration_sec"] = 0

        with self.assertRaisesRegex(
            ValueError, "script beat evidence.dialogue is required"
        ):
            script_drafts._normalize_variant(
                variant, allowed_source_ids={self.source_id}
            )

    def test_generate_repairs_zero_duration_without_second_model_call(self):
        calls = 0
        variants = [_variant(i, self.source_id) for i in range(3)]
        variants[1]["beats"][2]["dialogue"] = "这条证据必须完整保留"
        variants[1]["beats"][2]["duration_sec"] = 0
        variants[1]["target_duration_sec"] = 20

        def fake_generate(prompt, max_tokens):
            nonlocal calls
            calls += 1
            return ({
                "variants": variants,
                "recommended_index": 1,
                "recommendation_reason": "第二版证据最完整",
            }, {"input_tokens": 10, "output_tokens": 20})

        result = script_drafts.generate_drafts(
            self.database, self.brief_id, generate_json=fake_generate
        )

        self.assertEqual(calls, 1)
        script = result["drafts"][1]["script"]
        self.assertGreater(script["beats"][2]["duration_sec"], 0)
        self.assertEqual(
            script["target_duration_sec"], script["total_duration_sec"]
        )

    def test_generate_repairs_one_malformed_model_response_once(self):
        calls = []
        malformed = [_variant(i, self.source_id) for i in range(3)]
        malformed[0]["beats"][1].pop("speaker")
        responses = [
            ({
                "variants": malformed,
                "recommended_index": 0,
            }, {"input_tokens": 10, "output_tokens": 20}),
            ({
                "variants": [_variant(i, self.source_id) for i in range(3)],
                "recommended_index": 2,
                "recommendation_reason": "修复后第三版最佳",
            }, {"input_tokens": 30, "output_tokens": 40}),
        ]

        def fake_generate(prompt, max_tokens):
            calls.append(prompt)
            return responses.pop(0)

        result = script_drafts.generate_drafts(
            self.database, self.brief_id, generate_json=fake_generate
        )

        self.assertEqual(len(calls), 2)
        self.assertIn("契约修复", calls[1])
        self.assertEqual(len(result["drafts"]), 3)
        usage = self.database.execute(
            "SELECT input_tokens,output_tokens FROM script_drafts LIMIT 1"
        ).fetchone()
        self.assertEqual(usage["input_tokens"], 40)
        self.assertEqual(usage["output_tokens"], 60)

    def test_generate_retries_once_when_model_returns_invalid_json_syntax(self):
        calls = []

        def fake_generate(prompt, max_tokens):
            calls.append((prompt, max_tokens))
            if len(calls) == 1:
                raise json.JSONDecodeError(
                    "Expecting ',' delimiter", '{"variants": [}', 14
                )
            return ({
                "variants": [_variant(i, self.source_id) for i in range(3)],
                "recommended_index": 0,
                "recommendation_reason": "语法修复后第一版最佳",
            }, {"input_tokens": 30, "output_tokens": 40})

        result = script_drafts.generate_drafts(
            self.database, self.brief_id, generate_json=fake_generate
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual([item[1] for item in calls], [6000, 12000])
        self.assertIn("JSON 语法错误", calls[1][0])
        self.assertIn("重新生成完整 JSON", calls[1][0])
        self.assertEqual(len(result["drafts"]), 3)

    def test_generate_reports_chinese_error_after_two_invalid_json_responses(self):
        def invalid_json(prompt, max_tokens):
            raise json.JSONDecodeError(
                "Expecting ',' delimiter", '{"variants": [}', 14
            )

        with self.assertRaisesRegex(
            ValueError, "AI 连续两次返回的剧本 JSON 格式无效"
        ):
            script_drafts.generate_drafts(
                self.database, self.brief_id, generate_json=invalid_json
            )

    def test_standard_generation_preserves_explicit_speaker_roles(self):
        variants = [_variant(i, self.source_id) for i in range(3)]
        variants[0]["beats"][0]["speaker"] = "industry_boss"
        variants[0]["beats"][1]["speaker"] = "presenter"

        result = script_drafts.generate_drafts(
            self.database,
            self.brief_id,
            generate_json=lambda prompt, max_tokens: ({
                "variants": variants,
                "recommended_index": 0,
                "recommendation_reason": "",
            }, {}),
        )

        self.assertEqual(
            {beat["speaker"] for beat in result["drafts"][0]["script"]["beats"]},
            {"industry_boss", "presenter", "sales_xiaoneng"},
        )

    def test_generation_prompt_requires_directable_cinematic_visuals(self):
        context, sources = script_drafts._load_context(
            self.database, self.brief_id
        )

        prompt = script_drafts._prompt(context, sources)

        self.assertIn("多人剧情不得默认写成办公室站桩口播", prompt)
        self.assertIn("人物走位、动作或技能展示", prompt)
        self.assertIn("景别与运镜", prompt)
        self.assertEqual(
            script_drafts.PROMPT_VERSION, "complete-script-v7-video-reconstruction"
        )

    def test_video_structure_is_passed_to_reconstruction_prompt(self):
        metadata = {
            "duration_sec": 7.5,
            "structure_analysis": {
                "summary": "现场销售介绍客户，客户随后点赞",
                "segments": [{
                    "role": "hook", "start_sec": 0, "end_sec": 3,
                    "scene": "户外工程设备停放场",
                    "characters": ["女销售", "海外客户"],
                    "speaker": "女销售",
                    "action": "女销售指向身后的高空作业设备",
                    "camera": "中景固定机位后轻微推近",
                    "dialogue": "这是来自非洲的客户，租了我们一万台设备",
                    "on_screen_text": "", "audio": "现场环境声",
                }],
            },
        }
        self.database.execute(
            "UPDATE script_sources SET source_type='video',metadata_json=? "
            "WHERE source_id=?",
            (json.dumps(metadata, ensure_ascii=False), self.source_id),
        )
        self.database.commit()

        context, sources = script_drafts._load_context(
            self.database, self.brief_id
        )
        prompt = script_drafts._prompt(context, sources)

        self.assertEqual(sources[0]["source_type"], "video")
        self.assertEqual(
            sources[0]["structure_analysis"]["segments"][0]["speaker"],
            "女销售",
        )
        self.assertIn("原片高还原稿", prompt)
        self.assertIn("户外工程设备停放场", prompt)
        self.assertIn("不得把人物原话改成主持人或旁白转述", prompt)

    def test_video_generation_persists_exact_reconstruction_as_first_draft(self):
        metadata = {
            "duration_sec": 7.5,
            "structure_analysis": {
                "summary": "现场销售介绍客户，客户随后点赞",
                "segments": [
                    {
                        "role": "setup", "start_sec": 0, "end_sec": 4,
                        "scene": "户外工程设备停放场",
                        "characters": ["女销售", "海外客户"],
                        "speaker": "女销售",
                        "action": "女销售指向身后的高空作业设备",
                        "camera": "中景固定机位后轻微推近",
                        "dialogue": "这是来自非洲的客户，租了我们一万台设备",
                        "on_screen_text": "", "audio": "现场环境声",
                    },
                    {
                        "role": "proof", "start_sec": 4, "end_sec": 7.5,
                        "scene": "同一设备停放场",
                        "characters": ["海外客户", "女销售"],
                        "speaker": "海外客户",
                        "action": "客户面对镜头竖起大拇指",
                        "camera": "近景固定镜头",
                        "dialogue": "众能联合，Good Good Good",
                        "on_screen_text": "", "audio": "现场环境声",
                    },
                ],
            },
        }
        self.database.execute(
            "UPDATE script_sources SET source_type='video',metadata_json=? "
            "WHERE source_id=?",
            (json.dumps(metadata, ensure_ascii=False), self.source_id),
        )
        self.database.commit()

        result = script_drafts.generate_drafts(
            self.database,
            self.brief_id,
            generate_json=lambda prompt, max_tokens: ({
                "variants": [_variant(i, self.source_id) for i in range(3)],
                "recommended_index": 2,
                "recommendation_reason": "创意版更有传播性",
            }, {}),
        )

        reconstruction = result["drafts"][0]["script"]
        self.assertEqual(result["recommended_index"], 0)
        self.assertIn("原片高还原稿", reconstruction["title"])
        self.assertEqual(
            [beat["dialogue"] for beat in reconstruction["beats"]],
            [
                "这是来自非洲的客户，租了我们一万台设备",
                "众能联合，Good Good Good",
            ],
        )
        self.assertEqual(
            [beat["speaker"] for beat in reconstruction["beats"]],
            ["女销售", "海外客户"],
        )
        self.assertEqual(reconstruction["target_duration_sec"], 7.5)
        self.assertIn(
            "户外工程设备停放场",
            reconstruction["beats"][0]["visual_intent"],
        )
        self.assertIn(
            "中景固定机位后轻微推近",
            reconstruction["beats"][0]["visual_intent"],
        )
        self.assertEqual(
            reconstruction["beats"][0]["scene"], "户外工程设备停放场"
        )
        self.assertEqual(
            reconstruction["beats"][0]["characters"], ["女销售", "海外客户"]
        )

        approved = script_drafts.approve_draft(
            self.database, result["drafts"][0]["draft_id"]
        )
        self.assertEqual(len(approved["storyboard"]["shots"]), 2)
        self.assertEqual(
            approved["storyboard"]["masters"][0]["render_mode"],
            "cinematic_character",
        )

    def test_optional_generation_preferences_are_prompted_and_persisted(self):
        prompts = []
        preferences = {
            "suggested_duration_sec": 48,
            "suggested_shot_count": 8,
            "suggested_character_count": 3,
        }

        def fake_generate(prompt, max_tokens):
            prompts.append(prompt)
            return {
                "variants": [_variant(i, self.source_id) for i in range(3)],
                "recommended_index": 0,
                "recommendation_reason": "第一版最清晰",
            }, {}

        result = script_drafts.generate_drafts(
            self.database,
            self.brief_id,
            preferences=preferences,
            generate_json=fake_generate,
        )

        self.assertIn("创作建议（软约束）", prompts[0])
        self.assertIn('"suggested_duration_sec": 48', prompts[0])
        self.assertIn('"suggested_shot_count": 8', prompts[0])
        self.assertIn('"suggested_character_count": 3', prompts[0])
        self.assertEqual(
            result["drafts"][0]["script"]["generation_preferences"],
            preferences,
        )

    def test_generation_preferences_are_part_of_cache_fingerprint(self):
        calls = 0

        def fake_generate(prompt, max_tokens):
            nonlocal calls
            calls += 1
            return {
                "variants": [_variant(i, self.source_id) for i in range(3)],
                "recommended_index": 0,
                "recommendation_reason": "第一版最清晰",
            }, {}

        first = script_drafts.generate_drafts(
            self.database,
            self.brief_id,
            preferences={"suggested_character_count": 2},
            generate_json=fake_generate,
        )
        second = script_drafts.generate_drafts(
            self.database,
            self.brief_id,
            preferences={"suggested_character_count": 3},
            generate_json=fake_generate,
        )

        self.assertEqual(calls, 2)
        self.assertNotEqual(
            [item["draft_id"] for item in first["drafts"]],
            [item["draft_id"] for item in second["drafts"]],
        )

    def test_generation_preferences_reject_out_of_range_values(self):
        invalid_cases = (
            ({"suggested_duration_sec": 14}, "建议总时长需在 15–72 秒之间"),
            ({"suggested_shot_count": 9}, "建议分镜数需在 4–8 个之间"),
            ({"suggested_character_count": 0}, "建议角色数需在 1–6 人之间"),
        )
        for preferences, message in invalid_cases:
            with self.subTest(preferences=preferences):
                with self.assertRaisesRegex(ValueError, message):
                    script_drafts.normalize_generation_preferences(preferences)

    def test_dialogue_duration_estimate_scales_and_uses_half_second_steps(self):
        short = script_drafts.estimate_dialogue_duration("你好")
        long = script_drafts.estimate_dialogue_duration(
            "这是一段编辑后明显更长的台词，需要留出自然停顿，让人物能够完整说完。"
        )

        self.assertGreater(long, short)
        self.assertEqual(short, 1.5)
        self.assertEqual(long * 2, int(long * 2))
        self.assertLessEqual(long, 12)

    def test_generation_prompt_does_not_limit_talking_head_scripts_to_one_person(self):
        context, sources = script_drafts._load_context(
            self.database, self.brief_id
        )

        prompt = script_drafts._prompt(context, sources)

        self.assertIn("不得因为口播形式默认只有一名 presenter", prompt)
        self.assertIn("单人主讲、双人问答、多人接力或同场对谈", prompt)
        self.assertIn("确实只有单一视角时无需硬凑多人", prompt)
        self.assertEqual(
            script_drafts.PROMPT_VERSION,
            "complete-script-v7-video-reconstruction",
        )

    def test_generation_prompt_assigns_story_conflict_to_story_characters(self):
        context, sources = script_drafts._load_context(
            self.database, self.brief_id
        )

        prompt = script_drafts._prompt(context, sources)

        self.assertIn("presenter 不是默认人物", prompt)
        self.assertIn("冲突、质疑、反驳、决策和行动", prompt)
        self.assertIn("事件当事人物", prompt)
        self.assertIn("不得由 presenter 转述或代说", prompt)
        self.assertEqual(
            script_drafts.PROMPT_VERSION,
            "complete-script-v7-video-reconstruction",
        )

    def test_normalization_rejects_missing_speaker_instead_of_adding_presenter(self):
        variant = _variant(0, self.source_id)
        variant["beats"][1].pop("speaker")

        with self.assertRaisesRegex(
            ValueError, "script beat conflict.speaker is required"
        ):
            script_drafts._normalize_variant(
                variant,
                allowed_source_ids={self.source_id},
            )

    def test_generate_is_cached_by_brief_source_and_prompt_contract(self):
        calls = 0

        def fake_generate(prompt, max_tokens):
            nonlocal calls
            calls += 1
            return {
                "variants": [_variant(i, self.source_id) for i in range(3)],
                "recommended_index": 0,
                "recommendation_reason": "第一版最清晰",
            }, {}

        first = script_drafts.generate_drafts(
            self.database, self.brief_id, generate_json=fake_generate
        )
        second = script_drafts.generate_drafts(
            self.database, self.brief_id, generate_json=fake_generate
        )

        self.assertEqual(calls, 1)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(
            [item["draft_id"] for item in first["drafts"]],
            [item["draft_id"] for item in second["drafts"]],
        )

    def test_unknown_source_reference_is_rejected_without_persistence(self):
        invalid = [_variant(i, self.source_id) for i in range(3)]
        invalid[0]["beats"][2]["source_ids"] = ["SRC-NOT-ALLOWED"]

        with self.assertRaisesRegex(ValueError, "unknown source_id"):
            script_drafts.generate_drafts(
                self.database,
                self.brief_id,
                generate_json=lambda prompt, max_tokens: ({
                    "variants": invalid,
                    "recommended_index": 0,
                    "recommendation_reason": "",
                }, {}),
            )

        self.assertEqual(
            self.database.execute("SELECT COUNT(*) FROM script_drafts").fetchone()[0],
            0,
        )

    def test_generate_requires_an_approved_brief(self):
        self.database.execute(
            "UPDATE topic_briefs SET status='candidate' WHERE brief_id=?",
            (self.brief_id,),
        )
        self.database.commit()

        with self.assertRaisesRegex(ValueError, "must be approved"):
            script_drafts.generate_drafts(
                self.database,
                self.brief_id,
                generate_json=lambda prompt, max_tokens: ({}, {}),
            )


class ScriptDraftApprovalTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(db.DDL)
        source = script_sources.ingest_text(
            self.database,
            "客户需求散落在多个业务员的聊天记录里，信息无法统一管理。",
            title="客户访谈",
            source_uri="manual://interview-01",
        )
        self.source_id = source.source_id
        self.brief_id = "BRIEF-approval"
        self.job_id = "J-T-APPROVAL"
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,"
            "brief_json,approved_topic_id,approved_job_id,approved_angle_index) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                self.brief_id, "approved", "设备租赁最贵的不是设备",
                json.dumps(_brief_payload(), ensure_ascii=False),
                "T-APPROVAL", self.job_id, 0,
            ),
        )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,target_audience) "
            "VALUES(?,?,?,?)",
            (self.job_id, "T-APPROVAL", "设备租赁最贵的不是设备",
             "设备租赁老板"),
        )
        self.database.execute(
            "INSERT INTO topic_brief_sources(brief_id,source_id) VALUES(?,?)",
            (self.brief_id, self.source_id),
        )
        self.database.commit()
        variants = [_variant(i, self.source_id) for i in range(3)]
        for variant in variants:
            for beat in variant["beats"]:
                beat["dialogue"] += "。"
        generated = script_drafts.generate_drafts(
            self.database,
            self.brief_id,
            generate_json=lambda prompt, max_tokens: ({
                "variants": variants,
                "recommended_index": 1,
                "recommendation_reason": "第二版最佳",
            }, {}),
        )
        self.draft_ids = [item["draft_id"] for item in generated["drafts"]]

    def tearDown(self):
        self.database.close()

    def test_approve_builds_dialogue_and_broll_masters_with_dual_track_edit(self):
        result = script_drafts.approve_draft(self.database, self.draft_ids[1])
        storyboard = result["storyboard"]

        self.assertEqual(result["job_id"], self.job_id)
        self.assertEqual(storyboard["production_profile"], "standard")
        self.assertEqual(
            storyboard["production_mode"], "native_master_local_director"
        )
        self.assertEqual(
            [master["type"] for master in storyboard["masters"]],
            ["dialogue", "broll"],
        )
        self.assertLessEqual(
            sum(item["duration"] for item in storyboard["masters"]), 36
        )
        self.assertEqual(len(storyboard["shots"]), 6)
        self.assertEqual(
            [shot["beat"] for shot in storyboard["shots"]],
            ["hook", "conflict", "evidence", "reversal", "solution", "cta"],
        )
        self.assertTrue(all(
            not shot["subtitle"].endswith("。") for shot in storyboard["shots"]
        ))
        broll_edits = [
            shot for shot in storyboard["shots"]
            if shot["video_master_idx"] != shot["audio_master_idx"]
        ]
        self.assertGreaterEqual(len(broll_edits), 1)
        self.assertTrue(all(shot["video_master_idx"] == 2 for shot in broll_edits))
        self.assertTrue(all(shot["audio_master_idx"] == 1 for shot in broll_edits))
        self.assertEqual(
            storyboard["director_plan"]["strategy"], "dialogue_plus_broll"
        )
        broll_prompt = storyboard["masters"][1]["prompt"]
        self.assertIn("完整环境建立镜头", broll_prompt)
        self.assertIn("具体行业场景", broll_prompt)
        self.assertIn("禁止仅用无语义的手部特写", broll_prompt)
        self.assertIn("严格复现画面意图和辅助画面中的主体", broll_prompt)
        self.assertIn("证据辅助画面", broll_prompt)
        self.assertNotIn("evidence：证据画面", broll_prompt)
        s1_storyboard._validate(storyboard)

        job = self.database.execute(
            "SELECT storyboard,state FROM jobs WHERE job_id=?", (self.job_id,)
        ).fetchone()
        self.assertEqual(json.loads(job["storyboard"]), storyboard)
        self.assertEqual(job["state"], "STORYBOARDED")

    def test_ai_generated_character_does_not_default_to_female(self):
        result = script_drafts.approve_draft(self.database, self.draft_ids[0])

        self.assertTrue(result["storyboard"]["characters"])
        self.assertTrue(all(
            character["gender"] == "unspecified"
            for character in result["storyboard"]["characters"]
        ))

    def test_approve_splits_only_provider_master_over_thirty_seconds(self):
        draft_id = self.draft_ids[0]
        row = self.database.execute(
            "SELECT script_json FROM script_drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
        script = json.loads(row["script_json"])
        for beat in script["beats"]:
            beat["duration_sec"] = 6
        script["target_duration_sec"] = 36
        script["total_duration_sec"] = 36
        self.database.execute(
            "UPDATE script_drafts SET script_json=? WHERE draft_id=?",
            (json.dumps(script, ensure_ascii=False), draft_id),
        )
        self.database.commit()

        result = script_drafts.approve_draft(self.database, draft_id)
        storyboard = result["storyboard"]

        dialogue_masters = [
            master for master in storyboard["masters"]
            if master["type"] == "dialogue"
        ]
        self.assertEqual(
            [master["duration"] for master in dialogue_masters], [30, 6]
        )
        self.assertEqual(
            "".join(master["voiceover"] for master in dialogue_masters),
            "".join(beat["dialogue"] for beat in script["beats"]),
        )
        self.assertEqual(storyboard["masters"][2]["type"], "broll")
        self.assertLessEqual(storyboard["masters"][2]["duration"], 12)
        self.assertEqual(
            [shot["audio_master_idx"] for shot in storyboard["shots"]],
            [1, 1, 1, 1, 1, 2],
        )
        self.assertEqual(storyboard["shots"][-1]["audio_start"], 0)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id=?", (self.job_id,)
            ).fetchone()["state"],
            "STORYBOARDED",
        )
        s1_storyboard._validate(storyboard)

    def test_approve_transparently_chunks_one_long_script_beat(self):
        draft_id = self.draft_ids[0]
        row = self.database.execute(
            "SELECT script_json FROM script_drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
        script = json.loads(row["script_json"])
        script["beats"][2]["dialogue"] = (
            "完整证据说明，前半段交代事实，后半段给出结论。"
        )
        script["beats"][2]["duration_sec"] = 36
        script["target_duration_sec"] = 56
        script["total_duration_sec"] = 56
        self.database.execute(
            "UPDATE script_drafts SET script_json=? WHERE draft_id=?",
            (json.dumps(script, ensure_ascii=False), draft_id),
        )
        self.database.commit()

        result = script_drafts.approve_draft(self.database, draft_id)
        storyboard = result["storyboard"]
        dialogue_masters = [
            master for master in storyboard["masters"]
            if master["type"] == "dialogue"
        ]

        self.assertTrue(all(
            master["duration"] <= 30 for master in dialogue_masters
        ))
        self.assertEqual(
            round(sum(master["duration"] for master in dialogue_masters), 2),
            56,
        )
        self.assertEqual(
            "".join(master["voiceover"] for master in dialogue_masters),
            "".join(beat["dialogue"] for beat in script["beats"]),
        )
        self.assertEqual(len(storyboard["shots"]), 7)
        self.assertEqual(
            round(sum(shot["duration"] for shot in storyboard["shots"]), 2),
            56,
        )
        stored_script = json.loads(self.database.execute(
            "SELECT script_json FROM script_drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()["script_json"])
        self.assertEqual(stored_script["beats"][2]["duration_sec"], 36)
        s1_storyboard._validate(storyboard)

    def test_reapprove_recreates_job_after_failed_video_was_deleted(self):
        draft_id = self.draft_ids[0]
        self.database.execute("DELETE FROM jobs WHERE job_id=?", (self.job_id,))
        self.database.execute(
            "UPDATE script_drafts SET status='candidate',approved_job_id=NULL,"
            "storyboard_json=NULL WHERE draft_id=?", (draft_id,),
        )
        self.database.commit()

        result = script_drafts.approve_draft(self.database, draft_id)

        self.assertEqual(result["job_id"], self.job_id)
        job = self.database.execute(
            "SELECT state,storyboard FROM jobs WHERE job_id=?", (self.job_id,)
        ).fetchone()
        self.assertEqual(job["state"], "STORYBOARDED")
        self.assertTrue(job["storyboard"])

    def test_approve_creates_video_job_at_script_handoff_boundary(self):
        draft_id = self.draft_ids[0]
        self.database.execute("DELETE FROM jobs WHERE job_id=?", (self.job_id,))
        self.database.execute(
            "UPDATE topic_briefs SET approved_job_id=NULL WHERE brief_id=?",
            (self.brief_id,),
        )
        self.database.commit()

        result = script_drafts.approve_draft(self.database, draft_id)

        self.assertEqual(result["job_id"], self.job_id)
        job = self.database.execute(
            "SELECT state,storyboard FROM jobs WHERE job_id=?", (self.job_id,)
        ).fetchone()
        self.assertEqual(job["state"], "STORYBOARDED")
        brief = self.database.execute(
            "SELECT approved_job_id FROM topic_briefs WHERE brief_id=?",
            (self.brief_id,),
        ).fetchone()
        self.assertEqual(brief["approved_job_id"], self.job_id)
        self.assertTrue(job["storyboard"])

    def test_approve_preserves_multiple_speakers_as_separate_masters(self):
        draft_id = self.draft_ids[0]
        row = self.database.execute(
            "SELECT script_json FROM script_drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
        script = json.loads(row["script_json"])
        speakers = ["sales", "boss", "sales", "boss", "sales", "boss"]
        for beat, speaker in zip(script["beats"], speakers):
            beat["speaker"] = speaker
        self.database.execute(
            "UPDATE script_drafts SET script_json=? WHERE draft_id=?",
            (json.dumps(script, ensure_ascii=False), draft_id),
        )
        self.database.commit()

        result = script_drafts.approve_draft(self.database, draft_id)
        storyboard = result["storyboard"]

        dialogue_masters = [
            master for master in storyboard["masters"]
            if master["type"] == "dialogue"
        ]
        self.assertEqual(
            [master["speaker"] for master in dialogue_masters], ["sales", "boss"]
        )
        self.assertEqual(
            [master["type"] for master in storyboard["masters"]].count("broll"), 1
        )
        self.assertEqual(
            [character["id"] for character in storyboard["characters"]],
            ["sales", "boss"],
        )
        self.assertEqual(
            [shot["audio_master_idx"] for shot in storyboard["shots"]],
            [1, 2, 1, 2, 1, 2],
        )
        self.assertEqual(
            [shot["audio_start"] for shot in storyboard["shots"]],
            [0, 0, 4, 4, 8, 8],
        )
        s1_storyboard._validate(storyboard)

    def test_three_speaker_story_uses_three_cinematic_dialogue_masters(self):
        draft_id = self.draft_ids[0]
        row = self.database.execute(
            "SELECT script_json FROM script_drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
        script = json.loads(row["script_json"])
        for beat, speaker in zip(
            script["beats"], ["klein", "audrey", "alger", "klein", "audrey", "alger"]
        ):
            beat["speaker"] = speaker
        self.database.execute(
            "UPDATE script_drafts SET script_json=? WHERE draft_id=?",
            (json.dumps(script, ensure_ascii=False), draft_id),
        )
        self.database.commit()

        storyboard = script_drafts.approve_draft(
            self.database, draft_id
        )["storyboard"]

        self.assertEqual(len(storyboard["masters"]), 3)
        self.assertTrue(all(
            master["type"] == "dialogue"
            and master["render_mode"] == "cinematic_character"
            for master in storyboard["masters"]
        ))
        self.assertEqual(
            storyboard["director_plan"]["strategy"], "cinematic_dialogue_only"
        )
        s1_storyboard._validate(storyboard)

    def test_multi_character_story_builds_cinematic_performance_masters(self):
        script = _variant(0, self.source_id)
        speakers = ["klein", "klein", "klein", "audrey", "alger", "audrey"]
        visuals = [
            "午夜雾港，废弃蒸汽船旁的黑色海面卷起旋涡",
            "三人踏上甲板，水手雕像突然睁眼",
            "克莱恩弹起硬币，甲板裂开并化解危机",
            "奥黛丽释放金色心灵波纹安抚众人",
            "阿尔杰拔出弯刀，引导风暴击中黑雾核心",
            "三人并肩站在船头，月光穿透乌云",
        ]
        for beat, speaker, visual in zip(script["beats"], speakers, visuals):
            beat["speaker"] = speaker
            beat["visual_intent"] = visual
            beat["broll_hint"] = visual

        storyboard = script_drafts._storyboard(script)

        self.assertEqual(
            [master["render_mode"] for master in storyboard["masters"]],
            ["cinematic_character", "cinematic_character", "cinematic_character"],
        )
        self.assertTrue(all(
            master["shot_type"] == "电影化连续情境表演"
            for master in storyboard["masters"]
        ))
        self.assertTrue(all(
            "办公室" not in master["scene"]
            for master in storyboard["masters"]
        ))
        self.assertEqual(
            [item["visual_intent"] for item in storyboard["masters"][0]["performance_beats"]],
            visuals[:3],
        )
        self.assertEqual(
            sum(item["duration"] for item in storyboard["masters"][0]["performance_beats"]),
            storyboard["masters"][0]["duration"],
        )
        s1_storyboard._validate(storyboard)

    def test_single_presenter_script_keeps_presenter_master(self):
        script = _variant(0, self.source_id)
        script["generation_preferences"] = {
            "suggested_duration_sec": 36,
            "suggested_shot_count": 8,
            "suggested_character_count": 1,
        }
        storyboard = script_drafts._storyboard(script)

        self.assertEqual(storyboard["masters"][0]["render_mode"], "presenter")
        self.assertIn("办公室", storyboard["masters"][0]["scene"])
        self.assertEqual(
            storyboard["director_plan"]["suggested_edit_shot_count"], 8
        )
        self.assertEqual(
            storyboard["script_metadata"]["generation_preferences"],
            script["generation_preferences"],
        )

    def test_approve_folds_generic_presenter_into_first_story_character(self):
        draft_id = self.draft_ids[0]
        row = self.database.execute(
            "SELECT script_json FROM script_drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
        script = json.loads(row["script_json"])
        speakers = ["presenter", "presenter", "klein", "audrey", "alger", "klein"]
        for beat, speaker in zip(script["beats"], speakers):
            beat["speaker"] = speaker
        self.database.execute(
            "UPDATE script_drafts SET script_json=? WHERE draft_id=?",
            (json.dumps(script, ensure_ascii=False), draft_id),
        )
        self.database.commit()

        result = script_drafts.approve_draft(self.database, draft_id)
        storyboard = result["storyboard"]

        self.assertEqual(
            [master["speaker"] for master in storyboard["masters"]],
            ["klein", "audrey", "alger"],
        )
        self.assertEqual(
            [character["id"] for character in storyboard["characters"]],
            ["klein", "audrey", "alger"],
        )
        self.assertEqual(
            [shot["audio_master_idx"] for shot in storyboard["shots"]],
            [1, 1, 1, 2, 3, 1],
        )
        s1_storyboard._validate(storyboard)

    def test_approving_one_draft_rejects_siblings_and_blocks_another_choice(self):
        first = script_drafts.approve_draft(self.database, self.draft_ids[0])
        second = script_drafts.approve_draft(self.database, self.draft_ids[0])

        self.assertEqual(first, second)
        statuses = self.database.execute(
            "SELECT variant_index,status FROM script_drafts "
            "WHERE brief_id=? ORDER BY variant_index", (self.brief_id,),
        ).fetchall()
        self.assertEqual(
            [(row["variant_index"], row["status"]) for row in statuses],
            [(0, "approved"), (1, "rejected"), (2, "rejected")],
        )
        with self.assertRaisesRegex(ValueError, "already has approved draft"):
            script_drafts.approve_draft(self.database, self.draft_ids[1])


class ScriptDraftCliTests(ScriptDraftApprovalTests):
    def test_script_commands_generate_list_approve_and_export(self):
        runner = CliRunner()
        generated_payload = {
            "brief_id": self.brief_id,
            "recommended_index": 1,
            "recommendation_reason": "第二版最佳",
            "cached": False,
            "drafts": [{
                "draft_id": draft_id,
                "variant_index": index,
                "status": "candidate",
                "script": _variant(index, self.source_id),
            } for index, draft_id in enumerate(self.draft_ids)],
        }
        with runner.isolated_filesystem(), patch.object(
            cli_module, "conn", return_value=self.database
        ), patch.object(
            cli_module.script_drafts, "generate_drafts",
            return_value=generated_payload,
        ):
            generated = runner.invoke(
                cli_module.cli, ["script-generate", self.brief_id]
            )
            listed = runner.invoke(
                cli_module.cli, ["script-list", self.brief_id]
            )
            approved = runner.invoke(
                cli_module.cli, ["script-approve", self.draft_ids[1]]
            )
            exported = runner.invoke(
                cli_module.cli,
                ["script-export", self.draft_ids[1], "storyboard.json"],
            )
            exported_json = json.loads(
                Path("storyboard.json").read_text(encoding="utf-8")
            )

        self.assertEqual(generated.exit_code, 0, generated.output)
        self.assertIn("3 variants", generated.output)
        self.assertIn(self.draft_ids[1], generated.output)
        self.assertEqual(listed.exit_code, 0, listed.output)
        self.assertIn("24.0s", listed.output)
        self.assertEqual(approved.exit_code, 0, approved.output)
        self.assertIn(self.job_id, approved.output)
        self.assertEqual(exported.exit_code, 0, exported.output)
        self.assertEqual(
            exported_json["production_mode"], "native_master_local_director"
        )

    def test_produce_approved_resumes_after_storyboard_without_rerunning_s1(self):
        runner = CliRunner()
        script_drafts.approve_draft(self.database, self.draft_ids[0])

        async def fake_process(job_id, semaphore):
            self.assertEqual(job_id, self.job_id)
            state = self.database.execute(
                "SELECT state FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()["state"]
            self.assertEqual(state, "PRODUCTION_RUNNING")

        with patch.object(cli_module, "conn", return_value=self.database), patch.object(
            cli_module, "_process_job", side_effect=fake_process
        ) as process, patch.dict(
            "os.environ", {"SEEDANCE_JOB_BUDGET_CENTS": "5000"}
        ):
            preview = runner.invoke(
                cli_module.cli, ["produce-approved", self.job_id]
            )
            produced = runner.invoke(
                cli_module.cli, ["produce-approved", self.job_id, "--yes"]
            )

        self.assertEqual(preview.exit_code, 0, preview.output)
        self.assertIn("no provider call", preview.output)
        self.assertEqual(produced.exit_code, 0, produced.output)
        self.assertEqual(process.call_count, 1)

    def test_produce_approved_blocks_confirmed_job_over_hard_budget(self):
        runner = CliRunner()
        script_drafts.approve_draft(self.database, self.draft_ids[0])

        with patch.object(cli_module, "conn", return_value=self.database), patch.object(
            cli_module, "_process_job"
        ) as process, patch.dict(
            "os.environ", {"SEEDANCE_JOB_BUDGET_CENTS": "1"}
        ):
            produced = runner.invoke(
                cli_module.cli, ["produce-approved", self.job_id, "--yes"]
            )

        self.assertNotEqual(produced.exit_code, 0, produced.output)
        self.assertIn("exceeds the confirmed job budget", produced.output)
        process.assert_not_called()

    def test_queued_worker_rejects_legacy_master_over_thirty_seconds_before_provider(self):
        script_drafts.approve_draft(self.database, self.draft_ids[0])
        storyboard = json.loads(self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id=?", (self.job_id,)
        ).fetchone()["storyboard"])
        storyboard["masters"][0]["duration"] = 36
        storyboard["shots"][-1]["duration"] = 16
        storyboard["production_settings"] = {"budget_cents": 10000}
        self.database.execute(
            "UPDATE jobs SET state='PRODUCTION_QUEUED',storyboard=?,error=NULL "
            "WHERE job_id=?",
            (json.dumps(storyboard, ensure_ascii=False), self.job_id),
        )
        self.database.commit()

        runner = CliRunner()
        with patch.object(cli_module, "conn", return_value=self.database), patch.object(
            cli_module, "_process_job"
        ) as process, patch.dict(
            "os.environ", {"SEEDANCE_JOB_BUDGET_CENTS": "5000"}
        ):
            produced = runner.invoke(
                cli_module.cli, ["produce-approved", self.job_id, "--yes"]
            )

        self.assertNotEqual(produced.exit_code, 0, produced.output)
        self.assertIn("maximum is 30 seconds", produced.output)
        row = self.database.execute(
            "SELECT state,error FROM jobs WHERE job_id=?", (self.job_id,)
        ).fetchone()
        self.assertEqual(row["state"], "FAILED")
        self.assertIn("maximum is 30 seconds", row["error"])
        process.assert_not_called()

    def test_dispatched_worker_accepts_an_atomically_claimed_running_job(self):
        script_drafts.approve_draft(self.database, self.draft_ids[0])
        self.database.execute(
            "UPDATE jobs SET state='PRODUCTION_RUNNING',error=NULL WHERE job_id=?",
            (self.job_id,),
        )
        self.database.commit()

        runner = CliRunner()
        with patch.object(cli_module, "conn", return_value=self.database), patch.object(
            cli_module, "_process_job"
        ) as process, patch.dict(
            "os.environ", {"SEEDANCE_JOB_BUDGET_CENTS": "5000"}
        ):
            produced = runner.invoke(
                cli_module.cli,
                ["produce-approved", self.job_id, "--yes", "--dispatched"],
            )

        self.assertEqual(produced.exit_code, 0, produced.output)
        process.assert_called_once()

    def test_dispatched_worker_preflight_failure_releases_queue_slot(self):
        script_drafts.approve_draft(self.database, self.draft_ids[0])
        self.database.execute(
            "UPDATE jobs SET state='PRODUCTION_RUNNING',error=NULL WHERE job_id=?",
            (self.job_id,),
        )
        self.database.commit()

        runner = CliRunner()
        with patch.object(cli_module, "conn", return_value=self.database), patch.object(
            cli_module.production_planner,
            "analyze_storyboard",
            side_effect=RuntimeError("preflight unavailable"),
        ), patch.object(cli_module, "_process_job") as process:
            produced = runner.invoke(
                cli_module.cli,
                ["produce-approved", self.job_id, "--yes", "--dispatched"],
            )

        self.assertNotEqual(produced.exit_code, 0)
        row = self.database.execute(
            "SELECT state,error FROM jobs WHERE job_id=?", (self.job_id,)
        ).fetchone()
        self.assertEqual(row["state"], "FAILED")
        self.assertIn("preflight unavailable", row["error"])
        process.assert_not_called()

    def test_dispatched_worker_fails_closed_on_budget_overage(self):
        script_drafts.approve_draft(self.database, self.draft_ids[0])
        self.database.execute(
            "UPDATE jobs SET state='PRODUCTION_RUNNING',error=NULL WHERE job_id=?",
            (self.job_id,),
        )
        self.database.commit()

        report = {
            "paid_master_count": 1,
            "estimated_cost_cents": 2000,
            "budget_cents": 1000,
            "budget_ok": False,
        }
        runner = CliRunner()
        with patch.object(cli_module, "conn", return_value=self.database), patch.object(
            cli_module.production_planner, "analyze_storyboard", return_value=report
        ), patch.object(cli_module, "_process_job") as process:
            produced = runner.invoke(
                cli_module.cli,
                ["produce-approved", self.job_id, "--yes", "--dispatched"],
            )

        self.assertNotEqual(produced.exit_code, 0, produced.output)
        row = self.database.execute(
            "SELECT state,error FROM jobs WHERE job_id=?", (self.job_id,)
        ).fetchone()
        self.assertEqual(row["state"], "FAILED")
        self.assertIn("exceeds the confirmed job budget", row["error"])
        process.assert_not_called()

    def test_queued_worker_preflight_failure_is_persisted_instead_of_staying_queued(self):
        script_drafts.approve_draft(self.database, self.draft_ids[0])
        self.database.execute(
            "UPDATE jobs SET state='PRODUCTION_QUEUED',error=NULL WHERE job_id=?",
            (self.job_id,),
        )
        self.database.commit()

        runner = CliRunner()
        with patch.object(cli_module, "conn", return_value=self.database), patch.object(
            cli_module.production_planner,
            "analyze_storyboard",
            side_effect=RuntimeError("preflight unavailable"),
        ), patch.object(cli_module, "_process_job") as process:
            produced = runner.invoke(
                cli_module.cli, ["produce-approved", self.job_id, "--yes"]
            )

        self.assertNotEqual(produced.exit_code, 0)
        row = self.database.execute(
            "SELECT state,error FROM jobs WHERE job_id=?", (self.job_id,)
        ).fetchone()
        self.assertEqual(row["state"], "FAILED")
        self.assertIn("preflight unavailable", row["error"])
        process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
