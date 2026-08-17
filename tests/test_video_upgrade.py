"""P0 model-routing and native-audio upgrade tests."""
from pathlib import Path
import asyncio
import inspect
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


from factory import (  # noqa: E402
    character_assets,
    db,
    models,
    orchestrator,
    prompts,
    scene_assets,
    video_routing,
)
from factory.clients import bigasr, ffmpeg, music, seedance, video_qa  # noqa: E402
from factory.steps import s1_storyboard, s3_shots, s6_compose  # noqa: E402


class SeedancePayloadTests(unittest.TestCase):
    def test_invalid_parameter_submission_is_non_retryable(self):
        response = Mock(
            status_code=400,
            text=(
                '{"error":{"code":"InvalidParameter","message":'
                '"the duration specified in the request is not valid"}}'
            ),
        )

        error = seedance._submission_error(
            response,
            endpoint="https://ark.example/tasks",
            model="doubao-seedance-2-5-260628",
            content_roles=["reference_image"],
        )

        self.assertIsInstance(error, seedance.SeedanceParameterError)
        self.assertTrue(error.non_retryable)
        self.assertIn("请求参数", str(error))

    def test_generation_duration_rejects_invalid_values_before_submission(self):
        for invalid in (0, -1, float("nan"), 30.1):
            with self.subTest(invalid=invalid):
                with self.assertRaises(seedance.SeedanceParameterError):
                    seedance.normalize_generation_duration(invalid)

    def test_transport_disconnect_is_classified_as_unknown_submission(self):
        error = seedance._submission_transport_error(
            RuntimeError("Server disconnected without sending a response")
        )

        self.assertIsInstance(error, seedance.SeedanceSubmissionUnknownError)
        self.assertTrue(error.non_retryable)
        self.assertIn("是否已创建未知", str(error))

    def test_output_copyright_policy_error_is_non_retryable_and_readable(self):
        error = seedance._task_failure_error(
            "task-policy",
            {
                "status": "failed",
                "error": {
                    "code": (
                        "OutputVideoSensitiveContentDetected.PolicyViolation"
                    ),
                    "message": "output may be related to copyright restrictions",
                },
            },
        )

        self.assertIsInstance(error, seedance.SeedanceOutputPolicyError)
        self.assertTrue(error.non_retryable)
        self.assertIn("版权或敏感内容策略", str(error))
        self.assertNotIn("{'status'", str(error))

    def test_usage_cost_uses_provider_completion_tokens(self):
        self.assertEqual(
            seedance.usage_cost_cents({"completion_tokens": 260100}),
            1821,
        )

    def test_edit_payload_uses_adaptive_duration_and_mov(self):
        payload = seedance._video_task_payload(
            model="doubao-seedance-2-5-260628",
            content=[{
                "type": "video_url",
                "video_url": {"url": "https://cdn/source.mp4"},
                "role": "reference_video",
            }],
            duration=-1,
            resolution="720p",
            ratio="adaptive",
            camera_fixed=False,
            last_frame=None,
            seed=None,
            motion_strength=None,
            generate_audio=True,
            output_format="mov",
            task_intent="edit",
        )

        self.assertEqual(payload["duration"], -1)
        self.assertEqual(payload["ratio"], "adaptive")
        self.assertEqual(payload["output_format"], "mov")


class MusicFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_optional_library_falls_back_to_silent_bgm(self):
        with patch.dict(os.environ, {"DEFAULT_MUSIC_PROVIDER": "library"}), \
                patch.object(
                    music, "_via_library",
                    side_effect=RuntimeError("no bgm in local library"),
                ), patch.object(
                    music, "_via_silent", new=AsyncMock(return_value=b"silent")
                ) as silent:
            audio = await music.generate("business", 30)

        self.assertEqual(audio, b"silent")
        silent.assert_awaited_once_with(30)

    def test_edit_contract_rejects_fixed_ratio_before_api_call(self):
        with self.assertRaisesRegex(ValueError, "ratio='adaptive'"):
            seedance._video_task_payload(
                model="doubao-seedance-2-5-260628",
                content=[{
                    "type": "video_url",
                    "video_url": {"url": "https://cdn/source.mp4"},
                    "role": "reference_video",
                }],
                duration=-1,
                resolution="720p",
                ratio="9:16",
                camera_fixed=False,
                last_frame=None,
                seed=None,
                motion_strength=None,
                generate_audio=True,
                output_format="mov",
                task_intent="edit",
            )

    def test_edit_contract_requires_reference_media_before_api_call(self):
        with self.assertRaisesRegex(ValueError, "reference_video or reference_audio"):
            seedance._video_task_payload(
                model="doubao-seedance-2-5-260628",
                content=[{"type": "text", "text": "edit this"}],
                duration=-1,
                resolution="720p",
                ratio="adaptive",
                camera_fixed=False,
                last_frame=None,
                seed=None,
                motion_strength=None,
                generate_audio=True,
                output_format="mov",
                task_intent="edit",
            )

    def test_seedance25_accepts_official_reference_limits(self):
        seedance._validate_reference_limits(
            "doubao-seedance-2-5-260628",
            image_count=30,
            video_count=10,
            audio_count=10,
        )

    def test_seedance25_rejects_more_than_fifty_references(self):
        with self.assertRaisesRegex(ValueError, "reference_image.*max 30"):
            seedance._validate_reference_limits(
                "doubao-seedance-2-5-260628",
                image_count=31,
                video_count=10,
                audio_count=10,
            )

    def test_seedance25_rejects_duration_over_live_api_maximum(self):
        with self.assertRaisesRegex(ValueError, "maximum is 30 seconds"):
            seedance._video_task_payload(
                model="doubao-seedance-2-5-260628",
                content=[{"type": "text", "text": "test"}],
                duration=36,
                resolution="720p",
                ratio="9:16",
                camera_fixed=False,
                last_frame=None,
                seed=None,
                motion_strength=None,
                generate_audio=True,
            )

    def test_seedance25_pads_short_editorial_duration_before_submission(self):
        payload = seedance._video_task_payload(
            model="doubao-seedance-2-5-260628",
            content=[{"type": "text", "text": "test"}],
            duration=0.4,
            resolution="720p",
            ratio="9:16",
            camera_fixed=False,
            last_frame=None,
            seed=None,
            motion_strength=None,
            generate_audio=True,
        )

        self.assertEqual(payload["duration"], 4)

    def test_seedance25_rounds_fractional_duration_up_not_down(self):
        payload = seedance._video_task_payload(
            model="doubao-seedance-2-5-260628",
            content=[{"type": "text", "text": "test"}],
            duration=7.33,
            resolution="720p",
            ratio="9:16",
            camera_fixed=False,
            last_frame=None,
            seed=None,
            motion_strength=None,
            generate_audio=True,
        )

        self.assertEqual(payload["duration"], 8)

    def test_seedance25_rejects_unsupported_1080p_output(self):
        with self.assertRaisesRegex(ValueError, "480p or 720p"):
            seedance._video_task_payload(
                model="doubao-seedance-2-5-260628",
                content=[{"type": "text", "text": "test"}],
                duration=8,
                resolution="1080p",
                ratio="9:16",
                camera_fixed=False,
                last_frame=None,
                seed=None,
                motion_strength=None,
                generate_audio=True,
            )

    def test_local_image_accepts_path_objects(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            frame = Path(tmp) / "frame.png"
            frame.write_bytes(b"png")

            encoded = seedance._to_image_arg(frame)

        self.assertTrue(encoded.startswith("data:image/png;base64,"))

    def test_native_audio_uses_official_generate_audio_field(self):
        self.assertTrue(hasattr(seedance, "_video_task_payload"))
        payload = seedance._video_task_payload(
            model="doubao-seedance-test",
            content=[{"type": "text", "text": "test"}],
            duration=4,
            resolution="480p",
            ratio="9:16",
            camera_fixed=False,
            last_frame=None,
            seed=None,
            motion_strength=0.4,
            generate_audio=True,
        )

        self.assertTrue(payload["generate_audio"])
        self.assertNotIn("audio_output", payload)
        self.assertEqual(payload["model"], "doubao-seedance-test")

    def test_reference_wav_uses_wav_data_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "anchor.wav"
            audio.write_bytes(b"wav")
            content = []

            seedance._push_audio(content, "reference_audio", str(audio))

        self.assertTrue(
            content[0]["audio_url"]["url"].startswith(
                "data:audio/wav;base64,"
            )
        )

    def test_fast_t2v_omits_unsupported_camera_fixed_parameter(self):
        payload = seedance._video_task_payload(
            model="doubao-seedance-2-0-fast-260128",
            content=[{"type": "text", "text": "test"}],
            duration=4,
            resolution="480p",
            ratio="9:16",
            camera_fixed=False,
            last_frame=None,
            seed=None,
            motion_strength=0.25,
            generate_audio=True,
        )

        self.assertNotIn("camera_fixed", payload)

    def test_full_t2v_omits_unsupported_camera_fixed_parameter(self):
        payload = seedance._video_task_payload(
            model="doubao-seedance-2-0-260128",
            content=[{"type": "text", "text": "test"}],
            duration=4,
            resolution="480p",
            ratio="9:16",
            camera_fixed=True,
            last_frame=None,
            seed=None,
            motion_strength=0.25,
            generate_audio=True,
        )

        self.assertNotIn("camera_fixed", payload)

    def test_full_i2v_omits_unsupported_camera_fixed_parameter(self):
        payload = seedance._video_task_payload(
            model="doubao-seedance-2-0-260128",
            content=[
                {"type": "text", "text": "test"},
                {
                    "type": "image_url",
                    "role": "first_frame",
                    "image_url": {"url": "https://example.com/frame.png"},
                },
            ],
            duration=4,
            resolution="480p",
            ratio="9:16",
            camera_fixed=True,
            last_frame=None,
            seed=None,
            motion_strength=0.25,
            generate_audio=True,
        )

        self.assertNotIn("camera_fixed", payload)

    def test_seedance25_first_frame_omits_ratio_and_camera_fixed(self):
        payload = seedance._video_task_payload(
            model="doubao-seedance-2-5-260628",
            content=[
                {"type": "text", "text": "test"},
                {
                    "type": "image_url",
                    "role": "first_frame",
                    "image_url": {"url": "https://example.com/frame.png"},
                },
            ],
            duration=4,
            resolution="720p",
            ratio="9:16",
            camera_fixed=True,
            last_frame=None,
            seed=None,
            motion_strength=0.4,
            generate_audio=True,
        )

        self.assertNotIn("ratio", payload)
        self.assertNotIn("camera_fixed", payload)


class ObservabilitySchemaTests(unittest.TestCase):
    def test_shot_schema_records_model_native_audio_and_qa(self):
        self.assertTrue(hasattr(db, "SHOT_OBSERVABILITY_COLUMNS"))
        names = {name for name, _ in db.SHOT_OBSERVABILITY_COLUMNS}
        self.assertEqual(
            {"video_model", "native_audio", "video_qa"} - names,
            set(),
        )

    def test_provider_call_cost_is_idempotent_by_task_id(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        calls = ({
            "task_id": "cgt-recovered-001",
            "intent": "generate",
            "cost_cents": 1482,
            "usage": {"completion_tokens": 211765},
        },)

        first = s3_shots._record_provider_calls(
            memory_db, "J-TEST", 2, "seedance", calls
        )
        second = s3_shots._record_provider_calls(
            memory_db, "J-TEST", 2, "seedance", calls
        )

        self.assertEqual(first, 1482)
        self.assertEqual(second, 0)
        rows = memory_db.execute(
            "SELECT task_id, cost_cents FROM provider_calls"
        ).fetchall()
        self.assertEqual(
            [(row["task_id"], row["cost_cents"]) for row in rows],
            [("cgt-recovered-001", 1482)],
        )

    def test_distinct_provider_tasks_are_billed_separately(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.executescript(db.DDL)

        first = s3_shots._record_provider_calls(
            memory_db,
            "J-TEST",
            1,
            "seedance",
            ({"task_id": "cgt-001", "cost_cents": 406},),
        )
        second = s3_shots._record_provider_calls(
            memory_db,
            "J-TEST",
            2,
            "seedance",
            ({"task_id": "cgt-002", "cost_cents": 1482},),
        )

        self.assertEqual(first + second, 1888)


class VideoRoutingTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "ARK_VIDEO_PRIMARY_MODEL": "doubao-seedance-2-5-260628",
        }

    def test_broll_defaults_to_seedance25_native_audio(self):
        self.assertTrue(hasattr(video_routing, "select_route"))
        route = video_routing.select_route(
            {"narrative_type": "broll"}, {}, env=self.env
        )

        self.assertEqual(route.model, "doubao-seedance-2-5-260628")
        self.assertEqual(route.tier, "seedance25")
        self.assertTrue(route.generate_audio)
        self.assertTrue(route.qa_required)
        self.assertIsNone(route.fallback_model)

    def test_scene_dialogue_uses_seedance25_native_voice(self):
        self.assertTrue(hasattr(video_routing, "select_route"))
        route = video_routing.select_route(
            {"narrative_type": "dialogue"},
            {"render_mode": "scene"},
            env=self.env,
        )

        self.assertEqual(route.model, "doubao-seedance-2-5-260628")
        self.assertTrue(route.generate_audio)
        self.assertTrue(route.qa_required)

    def test_digital_human_dialogue_uses_seedance25_native_voice(self):
        self.assertTrue(hasattr(video_routing, "select_route"))
        route = video_routing.select_route(
            {"narrative_type": "dialogue"},
            {"render_mode": "digital_human", "native_audio": False},
            env=self.env,
        )

        self.assertEqual(route.model, "doubao-seedance-2-5-260628")
        self.assertTrue(route.generate_audio)

    def test_precision_gauge_motion_is_rejected_before_generation(self):
        reason = video_routing.precision_motion_reason({
            "scene": "施工机械液压压力表特写",
            "scene_dynamics": "红色指针逆时针回落到 2 MPa",
        })

        self.assertIsNotNone(reason)
        self.assertIn("指针", reason)

    def test_absolute_object_direction_is_rejected(self):
        reason = video_routing.precision_motion_reason({
            "scene": "三名工人抬着钢管",
            "scene_dynamics": "工人缓慢向画面右侧行走",
        })

        self.assertIsNotNone(reason)
        self.assertIn("方向", reason)

    def test_camera_pan_direction_remains_eligible(self):
        reason = video_routing.precision_motion_reason({
            "prompt": "镜头向右平移，工人自然经过施工现场",
        })

        self.assertIsNone(reason)

    def test_natural_broll_motion_remains_eligible(self):
        reason = video_routing.precision_motion_reason({
            "scene": "施工机械液压管路特写",
            "scene_dynamics": "管路轻微振动，浮尘缓慢飘动",
        })

        self.assertIsNone(reason)

    def test_static_numbers_do_not_block_camera_motion(self):
        reason = video_routing.precision_motion_reason({
            "scene": "设备铭牌上有静态数字",
            "prompt": "镜头从前景缓慢移到背景",
            "scene_dynamics": "浮尘缓慢飘动",
        })

        self.assertIsNone(reason)


class StoryboardPromptSafetyTests(unittest.TestCase):
    def test_roster_summary_exposes_fixed_character_reference_image(self):
        summary = prompts.character_roster_summary()

        self.assertIn("sales_xiaoneng", summary)
        self.assertIn(
            "references/characters/sales_xiaoneng/canonical-v1.png", summary
        )

    def test_storyboard_requires_visible_natural_motion(self):
        prompt = prompts.storyboard_prompt(
            title="设备场地日常",
            track="工程设备",
            duration=30,
        )

        self.assertIn("中等尺度、清晰可见的自然运动", prompt)
        self.assertIn("不能只写浮尘、光影、云层或树叶轻动", prompt)

    def test_storyboard_prioritizes_one_primary_motion(self):
        prompt = prompts.storyboard_prompt(
            title="设备场地日常",
            track="工程设备",
            duration=30,
        )

        self.assertIn("有且只有一个优先级最高的中等尺度主动态", prompt)
        self.assertIn("其他背景动作只作可选增强", prompt)

    def test_storyboard_avoids_precision_motion_for_generative_video(self):
        prompt = prompts.storyboard_prompt(
            title="液压设备检查",
            track="工程设备",
            duration=30,
        )

        self.assertIn("精密视觉动作", prompt)
        self.assertIn("不得把仪表指针", prompt)
        self.assertIn("人物和车辆不指定向左/向右", prompt)
        self.assertNotIn("仪表指针缓慢回落", prompt)

    def test_end_frame_is_optional_and_limited_to_natural_transitions(self):
        prompt = prompts.storyboard_prompt(
            title="设备巡检",
            track="工程设备",
            duration=30,
        )

        self.assertIn("`end_scene` 可选", prompt)
        self.assertIn("自然状态过渡", prompt)


class StoryboardLongSceneTests(unittest.TestCase):
    def test_asset_backed_virtual_portrait_does_not_require_duplicate_look(self):
        storyboard = self._storyboard(20)
        storyboard["characters"] = [{
            "id": "virtual_host",
            "name": "Virtual Host",
            "identity_type": "virtual_person",
            "seedance_asset_uri": "asset://asset-virtual-host-01",
        }]
        for shot in storyboard["shots"]:
            shot["speaker"] = "virtual_host"

        s1_storyboard._validate(storyboard)

    @staticmethod
    def _storyboard(duration: int) -> dict:
        return {
            "characters": [{
                "id": "host", "name": "主持人", "gender": "female",
                "look": "完全虚构的成年女性主持人",
            }],
            "hook": "完整讲清一个重点",
            "bgm": "克制的轻音乐",
            "shots": [
                {
                    "idx": 1, "type": "dialogue", "speaker": "host",
                    "render_mode": "presenter", "duration": duration,
                    "scene": "明亮工作室", "action": "自然讲解并轻微点头",
                    "voiceover": "这是一段用于验证长镜头能力的完整讲解台词。",
                },
                {
                    "idx": 2, "type": "dialogue", "speaker": "host",
                    "render_mode": "presenter", "duration": 4,
                    "scene": "同一工作室", "action": "自然收束",
                    "voiceover": "讲解到这里结束。",
                },
            ],
        }

    def test_long_dialogue_is_enabled_by_default_after_live_validation(self):
        with patch.dict("os.environ", {}, clear=True):
            s1_storyboard._validate(self._storyboard(20))

    def test_legacy_long_scene_toggle_does_not_restore_a_hard_limit(self):
        with patch.dict(
            "os.environ", {"SEEDANCE25_LONG_SCENE_ENABLED": "false"}, clear=True
        ):
            s1_storyboard._validate(self._storyboard(20))

    def test_long_dialogue_allows_up_to_thirty_seconds_when_enabled(self):
        with patch.dict(
            "os.environ", {"SEEDANCE25_LONG_SCENE_ENABLED": "true"}, clear=True
        ):
            s1_storyboard._validate(self._storyboard(20))

    def test_long_dialogue_over_thirty_seconds_is_not_locally_blocked(self):
        with patch.dict(
            "os.environ", {"SEEDANCE25_LONG_SCENE_ENABLED": "1"}, clear=True
        ):
            s1_storyboard._validate(self._storyboard(31))


class VideoQATests(unittest.TestCase):
    def test_continuity_analyzer_compares_current_video_to_canonical_and_previous_refs(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "choices": [{"message": {"content": json.dumps({
                "identity_checked": True, "identity_alignment": 0.93,
                "scene_checked": True, "scene_alignment": 0.90,
                "blocking_checked": True, "blocking_alignment": 0.88,
                "wardrobe_checked": True, "wardrobe_match": True,
                "prop_state_checked": True, "prop_state_match": True,
                "mismatches": [], "reason": "连续",
            }, ensure_ascii=False)}}],
        }
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.post.return_value = response

        with (
            patch.dict(os.environ, {"ARK_API_KEY": "test-key"}, clear=False),
            patch.object(video_qa.httpx, "Client", return_value=client),
        ):
            raw, decision = video_qa.analyze_continuity(
                "https://cdn/current.mp4",
                contract={"scene": {"key": "yard"}, "characters": []},
                identity_image_urls=["https://cdn/lead.png"],
                scene_image_url="https://cdn/yard.png",
                previous_video_url="https://cdn/previous.mp4",
            )

        self.assertTrue(decision.passed)
        self.assertEqual(raw["scene_alignment"], 0.90)
        payload = client.post.call_args.kwargs["json"]
        content = payload["messages"][0]["content"]
        self.assertEqual(
            [item["type"] for item in content],
            ["text", "video_url", "image_url", "image_url", "video_url"],
        )
        self.assertIn("continuity_contract", content[0]["text"])

    def test_continuity_gate_rejects_identity_scene_and_blocking_drift(self):
        decision = video_qa.evaluate_continuity({
            "identity_checked": True, "identity_alignment": 0.61,
            "scene_checked": True, "scene_alignment": 0.68,
            "blocking_checked": True, "blocking_alignment": 0.52,
            "wardrobe_match": False, "prop_state_match": False,
        })

        self.assertFalse(decision.passed)
        self.assertIn("identity_drift", decision.reasons)
        self.assertIn("scene_drift", decision.reasons)
        self.assertIn("blocking_drift", decision.reasons)
        self.assertIn("wardrobe_drift", decision.reasons)
        self.assertIn("prop_state_mismatch", decision.reasons)

    def test_continuity_gate_ignores_dimensions_without_reference_evidence(self):
        decision = video_qa.evaluate_continuity({
            "identity_checked": False, "identity_alignment": 0,
            "scene_checked": True, "scene_alignment": 0.91,
            "blocking_checked": False, "blocking_alignment": 0,
            "wardrobe_checked": False, "prop_state_checked": False,
        })

        self.assertTrue(decision.passed)
        self.assertEqual(decision.reasons, ())

    def test_broll_semantic_gate_requires_industry_subject_and_environment(self):
        decision = video_qa.evaluate(
            {
                "transcript": "",
                "confidence": 1.0,
                "broll_semantics": {
                    "industry_subject_visible": False,
                    "environment_context_visible": False,
                    "dominant_subject": "hands",
                    "hand_closeup_ratio": 0.82,
                },
            },
            expect_speech=False,
            require_broll_semantics=True,
        )

        self.assertFalse(decision.passed)
        self.assertIn("missing_industry_subject", decision.reasons)
        self.assertIn("missing_environment_context", decision.reasons)
        self.assertIn("meaningless_hand_closeup", decision.reasons)

    def test_broll_semantic_gate_accepts_scene_subject_and_action_evidence(self):
        decision = video_qa.evaluate(
            {
                "transcript": "",
                "confidence": 1.0,
                "broll_semantics": {
                    "industry_subject_visible": True,
                    "environment_context_visible": True,
                    "meaningful_action_visible": True,
                    "dominant_subject": "equipment",
                    "hand_closeup_ratio": 0.12,
                },
            },
            expect_speech=False,
            require_broll_semantics=True,
        )

        self.assertTrue(decision.passed)
        self.assertEqual(decision.reasons, ())

    def test_readable_generated_text_fails_when_forbidden(self):
        decision = video_qa.evaluate(
            {
                "transcript": "",
                "confidence": 1.0,
                "has_readable_text": True,
            },
            expect_speech=False,
            forbid_readable_text=True,
        )

        self.assertFalse(decision.passed)
        self.assertIn("unexpected_readable_text", decision.reasons)

    def test_transcript_normalization_equates_chinese_and_itn_numbers(self):
        spoken = "二零二六年八月十日，AI视频工厂升级到二点五，设备编号AI三一六。"
        itn = "2026年8月10日，AI 视频工厂升级到2.5，设备编号 AI 316。"

        self.assertEqual(
            video_qa.normalize_transcript_text(spoken),
            video_qa.normalize_transcript_text(itn),
        )

    def test_exact_dialogue_accepts_equivalent_itn_output(self):
        decision = video_qa.evaluate(
            {
                "transcript": "2026年8月10日，AI 视频工厂完成升级，设备编号 AI316。",
                "confidence": 1.0,
            },
            expect_speech=True,
            expected_text="二零二六年八月十日，AI视频工厂完成升级，设备编号AI三一六。",
        )

        self.assertTrue(decision.passed)
        self.assertEqual(decision.transcript_similarity, 1.0)

    def test_transcript_normalization_does_not_hide_semantic_changes(self):
        decision = video_qa.evaluate(
            {"transcript": "设备编号 AI316，复核已经驳回。", "confidence": 1.0},
            expect_speech=True,
            expected_text="设备编号AI三一六，复核已经通过。",
        )

        self.assertFalse(decision.passed)
        self.assertIn("transcript_mismatch", decision.reasons)

    def test_broll_rejects_unexpected_speech(self):
        self.assertTrue(hasattr(video_qa, "evaluate"))
        decision = video_qa.evaluate(
            {"transcript": "你好，我是豆包。", "confidence": 0.98},
            expect_speech=False,
        )

        self.assertFalse(decision.passed)
        self.assertIn("unexpected_speech", decision.reasons)

    def test_broll_accepts_speech_free_audio(self):
        self.assertTrue(hasattr(video_qa, "evaluate"))
        decision = video_qa.evaluate(
            {"transcript": "", "confidence": 0.95},
            expect_speech=False,
        )

        self.assertTrue(decision.passed)
        self.assertEqual(decision.reasons, ())

    def test_speech_free_broll_ignores_zero_transcript_confidence(self):
        decision = video_qa.evaluate(
            {"transcript": "", "confidence": 0.0},
            expect_speech=False,
        )

        self.assertTrue(decision.passed)
        self.assertNotIn("low_confidence", decision.reasons)

    def test_exact_dialogue_rejects_material_transcript_change(self):
        self.assertTrue(hasattr(video_qa, "evaluate"))
        decision = video_qa.evaluate(
            {"transcript": "你好，我是豆包。", "confidence": 0.98},
            expect_speech=True,
            expected_text="今天风很大，记得带伞。",
        )

        self.assertFalse(decision.passed)
        self.assertIn("transcript_mismatch", decision.reasons)

    def test_visual_prompt_mismatch_is_rejected(self):
        self.assertIn(
            "expected_visual", inspect.signature(video_qa.evaluate).parameters
        )
        decision = video_qa.evaluate(
            {
                "transcript": "",
                "confidence": 1.0,
                "visual_alignment": 0.2,
                "has_visible_person": True,
            },
            expect_speech=False,
            expected_visual="施工机械液压表，无人物",
            forbid_people=True,
        )

        self.assertFalse(decision.passed)
        self.assertIn("visual_mismatch", decision.reasons)
        self.assertIn("unexpected_person", decision.reasons)

    def test_independent_visual_comparison_overrides_self_score(self):
        decision = video_qa.evaluate(
            {
                "transcript": "",
                "confidence": 1.0,
                "visual_alignment": 1.0,
                "observed_visual": "手摇磨豆机中倒入咖啡豆并研磨",
                "visual_comparison": {
                    "alignment": 0.0,
                    "mismatches": ["目标是施工机械液压表，实际是咖啡磨豆机"],
                },
            },
            expect_speech=False,
            expected_visual="施工机械液压表，指针缓慢回落",
        )

        self.assertFalse(decision.passed)
        self.assertIn("visual_mismatch", decision.reasons)

    def test_visual_comparison_prompt_separates_expected_and_observed(self):
        self.assertTrue(hasattr(video_qa, "_visual_comparison_prompt"))
        prompt = video_qa._visual_comparison_prompt(
            "施工机械液压表",
            "手摇磨豆机和咖啡豆",
        )

        self.assertIn("施工机械液压表", prompt)
        self.assertIn("手摇磨豆机和咖啡豆", prompt)

    def test_visual_comparison_does_not_treat_omissions_as_mismatches(self):
        prompt = video_qa._visual_comparison_prompt(
            "生锈钢管、逆光薄雾和工人",
            "建筑工地里有钢管和工人",
        )

        self.assertIn("未提及不等于不匹配", prompt)
        self.assertIn("明确冲突", prompt)


class BigASRTests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_audio_builds_16k_mono_wav(self):
        with patch.object(ffmpeg, "_run", new=AsyncMock()) as run:
            await ffmpeg.extract_audio_wav(
                Path("input.mp4"), Path("output.wav")
            )

        args = run.await_args.args[0]
        self.assertIn("-vn", args)
        self.assertEqual(args[args.index("-ac") + 1], "1")
        self.assertEqual(args[args.index("-ar") + 1], "16000")
        self.assertEqual(args[-1], "output.wav")

    def test_bigasr_parses_flash_response(self):
        from factory.clients import bigasr

        self.assertEqual(
            bigasr.parse_transcript({"result": {"text": "新台词"}}),
            "新台词",
        )

    def test_bigasr_parses_transcript_with_speech_bounds(self):
        from factory.clients import bigasr

        result = bigasr.parse_transcription({
            "result": {
                "text": "新台词",
                "utterances": [
                    {"text": "新", "start_time": 620, "end_time": 980},
                    {"text": "台词", "start_time": 990, "end_time": 2860},
                ],
            }
        })

        self.assertEqual(result.text, "新台词")
        self.assertEqual(result.speech_start_ms, 620)
        self.assertEqual(result.speech_end_ms, 2860)

    def test_bigasr_preserves_timed_utterances_for_post_generation_subtitles(self):
        result = bigasr.parse_transcription({
            "result": {
                "text": "狗不干？我干。拼的不是腿，是信息和效率。",
                "utterances": [
                    {"text": "狗不干？我干。", "start_time": 2370,
                     "end_time": 3650},
                    {"text": "拼的不是腿，是信息和效率。", "start_time": 4010,
                     "end_time": 6830},
                ],
            }
        })

        self.assertEqual(
            [(item.text, item.start_ms, item.end_ms)
             for item in result.utterances],
            [
                ("狗不干？我干。", 2370, 3650),
                ("拼的不是腿，是信息和效率。", 4010, 6830),
            ],
        )

    def test_bigasr_handles_response_without_word_timings(self):
        from factory.clients import bigasr

        result = bigasr.parse_transcription({"result": {"text": "仅文本"}})

        self.assertEqual(result.text, "仅文本")
        self.assertIsNone(result.speech_start_ms)
        self.assertIsNone(result.speech_end_ms)

    def test_bigasr_sends_audio_to_official_flash_endpoint(self):
        import httpx
        from factory.clients import bigasr

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["resource"] = request.headers["X-Api-Resource-Id"]
            return httpx.Response(
                200,
                headers={"X-Api-Status-Code": "20000000"},
                json={"result": {"text": "原生台词"}},
            )

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "voice.wav"
            audio.write_bytes(b"wav")
            text = bigasr.transcribe(
                audio,
                app_id="app",
                access_token="token",
                transport=httpx.MockTransport(handler),
            )

        self.assertEqual(text, "原生台词")
        self.assertTrue(captured["url"].endswith("/recognize/flash"))
        self.assertEqual(captured["resource"], "volc.bigasr.auc_turbo")

    def test_bigasr_uses_asr_specific_environment_credentials(self):
        import httpx
        from factory.clients import bigasr

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["app"] = request.headers["X-Api-App-Key"]
            captured["token"] = request.headers["X-Api-Access-Key"]
            return httpx.Response(
                200,
                headers={"X-Api-Status-Code": "20000000"},
                json={"result": {"text": "ok"}},
            )

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"VOLC_ASR_APPID": "asr-app", "VOLC_ASR_TOKEN": "asr-token"},
            clear=True,
        ):
            audio = Path(tmp) / "voice.wav"
            audio.write_bytes(b"wav")
            bigasr.transcribe(
                audio, transport=httpx.MockTransport(handler)
            )

        self.assertEqual(captured, {"app": "asr-app", "token": "asr-token"})

    def test_bigasr_prefers_new_console_api_key(self):
        import httpx

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["api_key"] = request.headers.get("X-Api-Key")
            captured["legacy_app"] = request.headers.get("X-Api-App-Key")
            captured["legacy_token"] = request.headers.get("X-Api-Access-Key")
            return httpx.Response(
                200,
                headers={"X-Api-Status-Code": "20000000"},
                json={"result": {"text": "ok"}},
            )

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {
                "VOLC_ASR_API_KEY": "new-api-key",
                "VOLC_ASR_APPID": "legacy-app",
                "VOLC_ASR_TOKEN": "legacy-token",
            },
            clear=True,
        ):
            audio = Path(tmp) / "voice.wav"
            audio.write_bytes(b"wav")
            bigasr.transcribe(audio, transport=httpx.MockTransport(handler))

        self.assertEqual(captured["api_key"], "new-api-key")
        self.assertIsNone(captured["legacy_app"])
        self.assertIsNone(captured["legacy_token"])

    def test_bigasr_new_console_uses_seed_asr_2_standard_submit_and_query(self):
        import httpx

        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((str(request.url), request.headers["X-Api-Resource-Id"]))
            if str(request.url).endswith("/submit"):
                return httpx.Response(
                    200, headers={"X-Api-Status-Code": "20000000"}, json={}
                )
            return httpx.Response(
                200,
                headers={"X-Api-Status-Code": "20000000"},
                json={"result": {"text": "新版识别"}},
            )

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "voice.wav"
            audio.write_bytes(b"wav")
            result = bigasr.transcribe(
                audio, api_key="new-api-key",
                transport=httpx.MockTransport(handler), poll_interval=0,
            )

        self.assertEqual(result, "新版识别")
        self.assertEqual(
            [url.rsplit("/", 1)[-1] for url, _ in requests],
            ["submit", "query"],
        )
        self.assertTrue(all(resource == "volc.seedasr.auc" for _, resource in requests))


class Seedance25DialogueTests(unittest.TestCase):
    def test_character_makeup_plan_deduplicates_fixed_aliases_and_marks_ai(self):
        characters = [
            {
                "id": "sales-wide-shot",
                "name": "Sales",
                "generation_mode": "fixed_asset",
                "identity_type": "virtual_person",
                "seedance_asset_uri": "asset://asset-sales",
            },
            {
                "id": "sales-close-up",
                "name": "Sales",
                "generation_mode": "fixed_asset",
                "identity_type": "virtual_person",
                "seedance_asset_uri": "asset://asset-sales",
            },
            {
                "id": "customer",
                "name": "Customer",
                "generation_mode": "ai_generated",
                "look": "A young customer in a grey sweater",
            },
            {
                "id": "custom-expert",
                "name": "Custom Expert",
                "generation_mode": "fixed_asset",
                "identity_type": "custom",
                "asset_character_id": "custom-expert",
            },
        ]

        plan = character_assets.character_makeup_plan(characters)

        self.assertEqual(len(plan), 3)
        fixed = next(item for item in plan if item["source"] == "volc_asset")
        generated = next(
            item for item in plan if item["source"] == "seedream_identity_image"
        )
        custom = next(
            item for item in plan if item["source"] == "seedance_identity_master"
        )
        self.assertEqual(
            fixed["character_ids"], ["sales-wide-shot", "sales-close-up"]
        )
        self.assertEqual(fixed["status"], "ready")
        self.assertEqual(generated["status"], "pending")
        self.assertTrue(generated["requires_paid_call"])
        self.assertEqual(generated["anchor_mode"], "static_image")
        self.assertEqual(generated["duration_sec"], 0)
        self.assertEqual(custom["anchor_mode"], "dynamic_video")
        self.assertEqual(custom["duration_sec"], 4)

    def test_dialogue_request_carries_primary_and_cast_identity_references(self):
        primary = {
            "id": "customer",
            "name": "Customer",
            "generation_mode": "ai_generated",
        }
        fixed_cast = {
            "id": "sales",
            "name": "Sales",
            "generation_mode": "fixed_asset",
            "identity_type": "virtual_person",
            "seedance_asset_uri": "asset://asset-sales",
        }
        ai_cast = {
            "id": "assistant",
            "name": "Assistant",
            "generation_mode": "ai_generated",
        }
        anchors = {
            character_assets.character_identity_key(primary):
                s3_shots.DialogueAnchor("https://cdn/identity-customer.mp4", 4),
            character_assets.character_identity_key(ai_cast):
                s3_shots.DialogueAnchor("https://cdn/identity-assistant.mp4", 4),
        }

        request = s3_shots._dialogue_request_kwargs(
            {"duration_sec": 8},
            None,
            character=primary,
            cast_characters=[fixed_cast, ai_cast],
            identity_anchors=anchors,
        )

        self.assertEqual(request["references"], ["asset://asset-sales"])
        self.assertEqual(
            request["reference_videos"],
            [
                "https://cdn/identity-customer.mp4",
                "https://cdn/identity-assistant.mp4",
            ],
        )
        self.assertEqual(request["reference_video_duration_sec"], 8)

    def test_dialogue_request_reuses_static_identity_with_previous_master(self):
        primary = {
            "id": "customer",
            "name": "Customer",
            "generation_mode": "ai_generated",
        }
        static_identity = s3_shots.DialogueAnchor(
            image_reference="https://cdn/customer-identity.png",
            anchor_mode="static_image",
        )
        previous_master = s3_shots.DialogueAnchor(
            "https://cdn/previous-master.mp4", 8
        )

        request = s3_shots._dialogue_request_kwargs(
            {"duration_sec": 8},
            previous_master,
            character=primary,
            identity_anchors={
                character_assets.character_identity_key(primary):
                    static_identity,
            },
        )

        self.assertEqual(
            request["references"], ["https://cdn/customer-identity.png"]
        )
        self.assertEqual(
            request["reference_videos"],
            ["https://cdn/previous-master.mp4"],
        )
        self.assertEqual(request["reference_video_duration_sec"], 8)

    def test_scene_cast_is_limited_to_characters_named_in_the_scene(self):
        characters = {
            "sales": {"id": "sales", "name": "Sales"},
            "customer": {"id": "customer", "name": "Customer"},
            "unrelated": {"id": "unrelated", "name": "Unrelated"},
        }
        shot = {
            "scene": "Sales and Customer stand beside a rented excavator",
            "action": "Customer gives a thumbs-up",
        }

        cast = s3_shots._scene_cast_characters(shot, characters, "sales")

        self.assertEqual([item["id"] for item in cast], ["customer"])

    def test_identity_anchor_stage_generates_ai_once_and_reuses_it(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        memory_db.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES(?,?,?,?,?,?)",
            ("J-ID", "T-ID", "Identity", "PRODUCTION_RUNNING", "{}", 0),
        )
        memory_db.commit()
        characters = [{
            "id": "customer",
            "name": "Customer",
            "generation_mode": "ai_generated",
            "look": "A young customer in a grey sweater",
        }]
        with tempfile.TemporaryDirectory() as tmp:
            video_path = Path(tmp) / "identity.mp4"
            video_path.write_bytes(b"video")
            preview_path = Path(tmp) / "frame-001.jpg"
            preview_path.write_bytes(b"jpg")
            result = s3_shots.SeedanceGenerationResult(
                video=b"video",
                video_path=video_path,
                video_url="https://cdn/identity-customer.mp4",
                qa_note="{}",
                native_audio=False,
                model="doubao-seedance-2-5-260628",
                audio_path=None,
                cost_cents=272,
                provider_calls=({
                    "task_id": "identity-task-1",
                    "intent": "character_identity_anchor",
                    "usage": {},
                    "cost_cents": 272,
                },),
            )
            with (
                patch.object(s3_shots, "conn", return_value=memory_db),
                patch.object(s3_shots, "ASSETS_DIR", Path(tmp)),
                patch.object(
                    s3_shots.seedance, "text_to_image",
                    AsyncMock(return_value=b"png"),
                ) as text_to_image,
                patch.object(
                    s3_shots.tos, "upload",
                    return_value="https://cdn/identity-customer.png",
                ) as upload,
                patch.object(
                    s3_shots, "_generate_seedance_video",
                    AsyncMock(return_value=result),
                ) as generate,
                patch.object(
                    s3_shots.ffmpeg, "extract_frames",
                    AsyncMock(return_value=[preview_path]),
                ),
            ):
                first = asyncio.run(
                    s3_shots._ensure_character_identity_anchors(
                        "J-ID", characters
                    )
                )
                second = asyncio.run(
                    s3_shots._ensure_character_identity_anchors(
                        "J-ID", characters
                    )
                )

        identity_key = character_assets.character_identity_key(characters[0])
        self.assertEqual(text_to_image.await_count, 1)
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(generate.await_count, 0)
        self.assertEqual(
            first[identity_key].image_reference,
            "https://cdn/identity-customer.png",
        )
        self.assertEqual(
            second[identity_key].image_reference,
            "https://cdn/identity-customer.png",
        )
        row = memory_db.execute(
            "SELECT state,image_url,cost_cents FROM character_identity_anchors "
            "WHERE job_id=? AND identity_key=?",
            ("J-ID", identity_key),
        ).fetchone()
        self.assertEqual(dict(row), {
            "state": "READY",
            "image_url": "https://cdn/identity-customer.png",
            "cost_cents": 29,
        })
        self.assertEqual(
            memory_db.execute(
                "SELECT cost_cents FROM jobs WHERE job_id='J-ID'"
            ).fetchone()[0],
            29,
        )

    def test_dialogue_privacy_rejection_falls_back_to_dynamic_identity(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        memory_db.execute("ALTER TABLE shots ADD COLUMN video_url TEXT")
        memory_db.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES(?,?,?,?,?,?)",
            ("J-FALLBACK", "T", "Fallback", "PRODUCTION_RUNNING", "{}", 29),
        )
        memory_db.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,voiceover,state) "
            "VALUES(?,?,?,?,?)",
            ("J-FALLBACK", 1, 8, "Hello", "PENDING"),
        )
        character = {
            "id": "customer",
            "name": "Customer",
            "generation_mode": "ai_generated",
            "look": "A young customer in a grey sweater",
        }
        identity_key = character_assets.character_identity_key(character)
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "identity.png"
            image_path.write_bytes(b"png")
            memory_db.execute(
                "INSERT INTO character_identity_anchors("
                "job_id,identity_key,character_id,display_name,source_type,"
                "state,anchor_mode,image_path,image_url,cost_cents) "
                "VALUES(?,?,?,?,?,'READY','static_image',?,?,29)",
                (
                    "J-FALLBACK", identity_key, "customer", "Customer",
                    "seedream_identity_image", str(image_path),
                    "https://cdn/customer.png",
                ),
            )
            memory_db.commit()
            video_path = Path(tmp) / "result.mp4"
            video_path.write_bytes(b"video")
            audio_path = Path(tmp) / "result.wav"
            audio_path.write_bytes(b"wav")
            result = s3_shots.SeedanceGenerationResult(
                video=b"video",
                video_path=video_path,
                video_url="https://cdn/customer-dynamic.mp4",
                qa_note="{}",
                native_audio=True,
                model="doubao-seedance-2-5-260628",
                audio_path=audio_path,
                cost_cents=272,
                provider_calls=(),
            )
            anchors = {
                identity_key: s3_shots.DialogueAnchor(
                    image_reference="https://cdn/customer.png",
                    anchor_mode="static_image",
                )
            }
            with (
                patch.object(s3_shots, "conn", return_value=memory_db),
                patch.object(s3_shots, "ASSETS_DIR", Path(tmp)),
                patch.object(
                    s3_shots, "_generate_seedance_video",
                    AsyncMock(side_effect=[
                        s3_shots.seedance.SeedanceInputPrivacyError("privacy"),
                        result,
                        result,
                    ]),
                ) as generate,
                patch.object(
                    s3_shots.ffmpeg, "extract_frames",
                    AsyncMock(return_value=[]),
                ),
                patch.object(s3_shots, "_persist_master_transcription"),
            ):
                idx, error, returned_anchor = asyncio.run(
                    s3_shots._gen_dialogue(
                        "J-FALLBACK",
                        {
                            "idx": 1,
                            "duration_sec": 8,
                            "voiceover": "Hello",
                            "shot_type": "dialogue",
                        },
                        character,
                        {"voiceover": "Hello"},
                        asyncio.Semaphore(1),
                        identity_anchors=anchors,
                    )
                )

        self.assertEqual(idx, 1)
        self.assertIsNone(error)
        self.assertIsNotNone(returned_anchor)
        self.assertEqual(generate.await_count, 3)
        self.assertEqual(anchors[identity_key].anchor_mode, "dynamic_video")
        self.assertEqual(
            generate.await_args_list[-1].kwargs["request_kwargs"][
                "reference_videos"
            ],
            ["https://cdn/customer-dynamic.mp4"],
        )

    def test_identity_anchor_failure_blocks_all_scene_generation(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        memory_db.execute("ALTER TABLE jobs ADD COLUMN characters_json TEXT")
        memory_db.execute("ALTER TABLE shots ADD COLUMN narrative_type TEXT")
        memory_db.execute("ALTER TABLE shots ADD COLUMN speaker_id TEXT")
        storyboard = {
            "characters": [{
                "id": "customer",
                "name": "Customer",
                "generation_mode": "ai_generated",
                "look": "A young customer in a grey sweater",
            }],
            "masters": [{
                "idx": 1,
                "type": "dialogue",
                "speaker": "customer",
                "duration": 4,
                "render_mode": "presenter",
            }],
        }
        memory_db.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,characters_json) "
            "VALUES(?,?,?,?,?,?)",
            (
                "J-GATE", "T-GATE", "Gate", "PRODUCTION_RUNNING",
                json.dumps(storyboard),
                json.dumps(storyboard["characters"]),
            ),
        )
        memory_db.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,state,narrative_type,"
            "speaker_id) VALUES(?,?,?,?,?,?)",
            ("J-GATE", 1, 4, "PENDING", "dialogue", "customer"),
        )
        memory_db.commit()

        with (
            patch.object(s3_shots, "conn", return_value=memory_db),
            patch.object(
                s3_shots, "_ensure_character_identity_anchors",
                AsyncMock(side_effect=RuntimeError("identity anchor failed")),
            ),
            patch.object(s3_shots, "_gen_dialogue", AsyncMock()) as generate,
        ):
            with self.assertRaisesRegex(RuntimeError, "identity anchor failed"):
                asyncio.run(s3_shots.run("J-GATE"))

        generate.assert_not_awaited()

    def test_run_passes_previous_same_scene_master_across_speakers(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        memory_db.execute("ALTER TABLE jobs ADD COLUMN characters_json TEXT")
        memory_db.execute("ALTER TABLE shots ADD COLUMN narrative_type TEXT")
        memory_db.execute("ALTER TABLE shots ADD COLUMN speaker_id TEXT")
        memory_db.execute("ALTER TABLE shots ADD COLUMN anchor_character TEXT")
        memory_db.execute("ALTER TABLE shots ADD COLUMN video_url TEXT")
        characters = [
            {
                "id": "sales", "name": "Sales", "gender": "female",
                "generation_mode": "ai_generated", "look": "young sales",
            },
            {
                "id": "customer", "name": "Customer", "gender": "male",
                "generation_mode": "ai_generated", "look": "young customer",
            },
        ]
        masters = [
            {
                "idx": 1, "type": "dialogue", "speaker": "sales",
                "render_mode": "cinematic_character", "duration": 4,
                "scene_anchor_key": "yard-a",
            },
            {
                "idx": 2, "type": "dialogue", "speaker": "sales",
                "render_mode": "cinematic_character", "duration": 4,
                "scene_anchor_key": "yard-a",
            },
            {
                "idx": 3, "type": "dialogue", "speaker": "customer",
                "render_mode": "cinematic_character", "duration": 4,
                "scene_anchor_key": "yard-a",
            },
        ]
        storyboard = {"characters": characters, "masters": masters, "shots": []}
        memory_db.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,characters_json) "
            "VALUES(?,?,?,?,?,?)",
            (
                "J-SCENE-CHAIN", "T-SCENE-CHAIN", "Scene chain",
                "PRODUCTION_RUNNING", json.dumps(storyboard),
                json.dumps(characters),
            ),
        )
        for master in masters:
            memory_db.execute(
                "INSERT INTO shots(job_id,idx,duration_sec,state,narrative_type,"
                "speaker_id) VALUES(?,?,?,?,?,?)",
                (
                    "J-SCENE-CHAIN", master["idx"], 4, "PENDING",
                    "dialogue", master["speaker"],
                ),
            )
        memory_db.commit()
        received_scene_anchors = []

        async def generate(job_id, shot, char, sb_shot, sem, anchor=None, **kwargs):
            scene_anchor = kwargs.get("scene_video_anchor")
            received_scene_anchors.append(
                scene_anchor.video_url if scene_anchor else None
            )
            return (
                shot["idx"], None,
                s3_shots.DialogueAnchor(
                    f"https://cdn/master-{shot['idx']}.mp4", 4
                ),
            )

        with (
            patch.object(s3_shots, "conn", return_value=memory_db),
            patch.object(
                s3_shots, "_ensure_character_identity_anchors",
                AsyncMock(return_value={}),
            ),
            patch.object(
                s3_shots, "_ensure_scene_visual_anchors",
                AsyncMock(return_value={"yard-a": "https://cdn/yard.png"}),
            ),
            patch.object(s3_shots, "_gen_dialogue", side_effect=generate),
            patch.object(s3_shots.asset_library, "reuse_broll_assets"),
        ):
            asyncio.run(s3_shots.run("J-SCENE-CHAIN"))

        self.assertEqual(
            received_scene_anchors,
            [None, "https://cdn/master-1.mp4", "https://cdn/master-2.mp4"],
        )

    def test_static_makeup_reuses_existing_image_without_rebuy(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        memory_db.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-PREVIEW','T-PREVIEW','Preview','PRODUCTION_RUNNING','{}',0)"
        )
        memory_db.commit()
        character = {
            "id": "customer",
            "name": "Customer",
            "generation_mode": "ai_generated",
            "look": "A young fictional customer",
        }
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(s3_shots, "conn", return_value=memory_db),
                patch.object(s3_shots, "ASSETS_DIR", Path(tmp)),
                patch.object(
                    s3_shots.seedance, "text_to_image",
                    AsyncMock(return_value=b"png"),
                ) as generate,
                patch.object(
                    s3_shots.tos, "upload",
                    return_value="https://cdn/identity-preview.png",
                ),
            ):
                first = asyncio.run(
                    s3_shots._ensure_character_identity_anchors(
                        "J-PREVIEW", [character]
                    )
                )
                second = asyncio.run(
                    s3_shots._ensure_character_identity_anchors(
                        "J-PREVIEW", [character]
                    )
                )

        identity_key = character_assets.character_identity_key(character)
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(
            first[identity_key].image_reference,
            "https://cdn/identity-preview.png",
        )
        self.assertEqual(
            second[identity_key].image_reference,
            "https://cdn/identity-preview.png",
        )
        row = memory_db.execute(
            "SELECT state,preview_path,cost_cents,error "
            "FROM character_identity_anchors WHERE job_id='J-PREVIEW'"
        ).fetchone()
        self.assertEqual(row["state"], "READY")
        self.assertTrue(row["preview_path"])
        self.assertEqual(row["cost_cents"], 29)
        self.assertIsNone(row["error"])

    def test_static_makeup_upload_retry_does_not_rebuy_image(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        memory_db.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-UPLOAD','T-UPLOAD','Upload','PRODUCTION_RUNNING','{}',0)"
        )
        memory_db.commit()
        character = {
            "id": "customer",
            "name": "Customer",
            "generation_mode": "ai_generated",
            "look": "A young fictional customer",
        }
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(s3_shots, "conn", return_value=memory_db),
                patch.object(s3_shots, "ASSETS_DIR", Path(tmp)),
                patch.object(
                    s3_shots.seedance, "text_to_image",
                    AsyncMock(return_value=b"png"),
                ) as generate,
                patch.object(
                    s3_shots.tos, "upload",
                    side_effect=[
                        RuntimeError("upload failed"),
                        "https://cdn/identity-upload.png",
                    ],
                ) as upload,
            ):
                with self.assertRaisesRegex(RuntimeError, "upload failed"):
                    asyncio.run(
                        s3_shots._ensure_character_identity_anchors(
                            "J-UPLOAD", [character]
                        )
                    )
                anchors = asyncio.run(
                    s3_shots._ensure_character_identity_anchors(
                        "J-UPLOAD", [character]
                    )
                )

        identity_key = character_assets.character_identity_key(character)
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(upload.call_count, 2)
        self.assertEqual(
            anchors[identity_key].image_reference,
            "https://cdn/identity-upload.png",
        )
        self.assertEqual(
            memory_db.execute(
                "SELECT cost_cents FROM jobs WHERE job_id='J-UPLOAD'"
            ).fetchone()[0],
            29,
        )

    def test_fixed_custom_asset_uses_script_role_and_appearance_only(self):
        shot = {
            "idx": 1,
            "duration_sec": 8,
            "render_mode": "cinematic_character",
            "scene": "海外设备租赁项目现场",
            "voiceover": "项目交付按计划推进。",
            "cast": [{
                "id": "主管",
                "name": "奥黛丽",
                "script_character_name": "主管",
                "generation_mode": "fixed_asset",
                "identity_type": "custom",
                "seedance_reference_mode": "description_only",
                "look": "《诡秘之主》塔罗会成员",
                "background_description": (
                    "《诡秘之主》塔罗会代号正义。"
                    "真人形象设定为18岁的欧洲年轻女性，金色长卷发、绿色眼睛。"
                ),
            }],
        }

        prompt = s3_shots._build_native_dialogue_prompt(
            shot,
            {
                "id": "员工A",
                "name": "老大",
                "script_character_name": "员工A",
                "generation_mode": "fixed_asset",
                "identity_type": "virtual_person",
                "seedance_asset_uri": "asset://employee-a",
                "look": "古代戏楼名伶",
                "background_description": "先秦战乱中的后宅名伶",
            },
        )

        self.assertIn("人物姓名与设定：员工A", prompt)
        self.assertIn("主管：18岁的欧洲年轻女性", prompt)
        self.assertNotIn("奥黛丽", prompt)
        self.assertNotIn("诡秘之主", prompt)
        self.assertNotIn("塔罗会", prompt)
        self.assertNotIn("先秦战乱", prompt)

    def test_cinematic_character_prompt_uses_story_action_and_dynamic_camera(self):
        shot = {
            "idx": 1,
            "duration_sec": 12,
            "render_mode": "cinematic_character",
            "scene": "午夜雾港的废弃蒸汽船甲板",
            "camera": "低机位跟拍后环绕人物",
            "action": "人物弹起硬币，躲过裂开的甲板",
            "voiceover": "罗盘是诱饵。真正的危险，在我们脚下。",
            "performance_beats": [
                {
                    "duration": 5,
                    "visual_intent": "人物登上甲板，雕像突然睁眼",
                    "dialogue": "罗盘是诱饵。",
                },
                {
                    "duration": 7,
                    "visual_intent": "人物弹起硬币，甲板裂开",
                    "dialogue": "真正的危险，在我们脚下。",
                },
            ],
            "cast": [
                {"name": "奥黛丽", "look": "金发绿眼的欧洲贵族少女"},
                {"name": "阿尔杰", "look": "深蓝卷发的年轻航海者"},
            ],
        }

        prompt = s3_shots._build_native_dialogue_prompt(
            shot,
            {"name": "克莱恩", "look": "年轻的东亚神秘学调查者"},
        )

        self.assertIn("电影叙事情境表演", prompt)
        self.assertIn("不得持续正对镜头站桩讲解", prompt)
        self.assertIn("人物登上甲板，雕像突然睁眼", prompt)
        self.assertIn("人物弹起硬币，甲板裂开", prompt)
        self.assertIn("低机位跟拍后环绕人物", prompt)
        self.assertIn("奥黛丽：金发绿眼的欧洲贵族少女", prompt)
        self.assertIn("阿尔杰：深蓝卷发的年轻航海者", prompt)
        self.assertIn("只有克莱恩说出台词", prompt)
        self.assertNotIn("建立场景、人物或物件关系", prompt)

    def test_cinematic_generation_resolves_only_scene_cast(self):
        source = inspect.getsource(s3_shots.run)

        self.assertIn('sb_shot["cast"]', source)
        self.assertIn("_scene_cast_characters", source)
        self.assertNotIn("if character_id != shot[\"speaker_id\"]", source)

    def test_ai_generated_character_ignores_cached_roster_reference(self):
        character = {
            "id": "sales_xiaoneng",
            "name": "芳白",
            "look": "年轻、理性的设备租赁顾问",
            "generation_mode": "ai_generated",
        }

        with patch.object(
            s3_shots,
            "_roster_character",
            return_value={
                "identity_type": "real_person",
                "seedance_asset_uri": "asset://asset-old-fixed-person",
            },
        ):
            request = s3_shots._dialogue_request_kwargs(
                {"duration_sec": 8}, None, character=character
            )
            prompt = s3_shots._build_native_dialogue_prompt(
                {"idx": 1, "duration_sec": 8, "voiceover": "你好"},
                character,
            )

        self.assertNotIn("references", request)
        self.assertIn("完全虚构", prompt)

    def test_dialogue_prompt_contains_exact_native_line(self):
        shot = {
            "idx": 1,
            "scene": "明亮设备交付工作室",
            "camera": "镜头缓慢推近",
            "action": "说到交付时轻微点头",
            "voiceover": "设备编号AI三一六，确认无误，可以交付。",
            "voice_directive": "专业沉稳的成年男性普通话",
            "expression_arc": "认真核对后确认完成",
        }
        character = {
            "name": "老周",
            "look": "四十岁中国男性，短黑发，深灰西装",
        }

        prompt = s3_shots._build_native_dialogue_prompt(shot, character)

        self.assertIn(character["look"], prompt)
        self.assertIn(shot["voiceover"], prompt)
        self.assertIn("完整说出且只能说出", prompt)
        self.assertIn(shot["voice_directive"], prompt)
        self.assertNotIn("SeedTTS", prompt)
        self.assertNotIn("OmniHuman", prompt)

    def test_dialogue_prompt_uses_character_background_context(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 1,
                "scene": "设备租赁办公室",
                "voiceover": "先把设备利用率算清楚。",
            },
            {
                "name": "小玲",
                "look": "固定的设备租赁顾问",
                "background_description": "设备租赁行业一线销售，表达干练可信。",
            },
        )

        self.assertIn("角色背景：设备租赁行业一线销售，表达干练可信。", prompt)

    def test_dialogue_prompt_uses_director_timeline_and_audio_grammar(self):
        shot = {
            "idx": 1,
            "duration_sec": 8,
            "scene": "明亮设备交付工作室",
            "camera": "镜头缓慢推近",
            "action": "说到交付时轻微点头",
            "voiceover": "设备编号AI三一六，确认无误，可以交付。",
            "expression_arc": "认真核对后确认完成",
            "sound_design": "纸张轻响与安静室内底噪",
        }

        prompt = s3_shots._build_native_dialogue_prompt(
            shot, {"name": "老周", "look": "虚构的中年工程师"}
        )

        self.assertIn("创作意图：", prompt)
        self.assertIn("时间轴：", prompt)
        self.assertIn("0.0-", prompt)
        self.assertIn("-8.0秒：", prompt)
        self.assertIn(f"{{台词：{shot['voiceover']}}}", prompt)
        self.assertIn(f"<环境音：{shot['sound_design']}>", prompt)
        self.assertIn("无 BGM", prompt)

    def test_default_sound_design_is_derived_from_scene_dynamics(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 1,
                "duration_sec": 8,
                "scene": "设备间",
                "scene_dynamics": "纸页被风轻轻带动",
                "voiceover": "请再次核对设备记录。",
            },
            {"name": "老周", "look": "虚构的中年工程师"},
        )

        self.assertIn("由“纸页被风轻轻带动”产生的轻微环境声与物件声", prompt)

    def test_anchored_dialogue_uses_video_for_identity_and_native_voice(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 2,
                "duration_sec": 8,
                "scene": "同一工作室",
                "voiceover": "现在进入第二项检查。",
            },
            {"name": "老周", "look": "虚构的中年工程师"},
            anchored=True,
        )

        self.assertIn("素材职责：", prompt)
        self.assertIn("身份参考图只锁定人物外貌", prompt)
        self.assertIn("前序视频只延续原生声线", prompt)
        self.assertIn("不得复述参考素材中的旧台词", prompt)
        self.assertNotIn("音频1", prompt)

    def test_fixed_character_prompt_does_not_claim_the_person_is_fictional(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 1,
                "duration_sec": 8,
                "scene": "设备租赁办公室",
                "voiceover": "现在做租赁，拼的是信息和效率。",
            },
            {
                "id": "sales_xiaoneng",
                "name": "小玲",
                "look": "固定的设备租赁顾问",
                "reference_image": (
                    "references/characters/sales_xiaoneng/canonical-v1.png"
                ),
            },
        )

        self.assertIn("固定角色身份参考", prompt)
        self.assertNotIn("完全虚构", prompt)

    def test_first_dialogue_is_prompt_only(self):
        shot = {"duration_sec": 8}

        request = s3_shots._dialogue_request_kwargs(shot, None)

        self.assertNotIn("first_frame", request)
        self.assertNotIn("reference_videos", request)
        self.assertNotIn("reference_audios", request)

    def test_short_editorial_dialogue_uses_provider_safe_duration(self):
        request = s3_shots._dialogue_request_kwargs(
            {"duration_sec": 0.4}, None
        )

        self.assertEqual(request["duration"], 4)

    def test_real_person_character_requires_authorized_seedance_asset(self):
        shot = {"duration_sec": 8}
        character = {"id": "sales_xiaoneng"}

        with patch.object(
            s3_shots,
            "_roster_character",
            return_value={
                "identity_type": "real_person",
                "reference_image": "references/characters/person.png",
            },
        ):
            with self.assertRaisesRegex(
                s3_shots.SeedanceIdentityAuthorizationError,
                "authorized.*asset",
            ):
                s3_shots._dialogue_request_kwargs(
                    shot, None, character=character
                )

    def test_real_person_character_uses_authorized_seedance_asset_uri(self):
        shot = {"duration_sec": 8}
        character = {"id": "sales_xiaoneng"}

        with patch.object(
            s3_shots,
            "_roster_character",
            return_value={
                "identity_type": "real_person",
                "seedance_asset_uri": "asset://asset-xiaoling-authorized",
            },
        ):
            request = s3_shots._dialogue_request_kwargs(
                shot, None, character=character
            )

        self.assertEqual(
            request["references"], ["asset://asset-xiaoling-authorized"]
        )
        self.assertNotIn("reference_videos", request)

    def test_virtual_portrait_character_uses_volc_asset_uri(self):
        shot = {"duration_sec": 8}
        character = {
            "id": "virtual_host",
            "identity_type": "virtual_person",
            "seedance_asset_uri": "asset://asset-virtual-host-01",
        }

        request = s3_shots._dialogue_request_kwargs(
            shot, None, character=character
        )

        self.assertEqual(
            request["references"], ["asset://asset-virtual-host-01"]
        )

    def test_virtual_portrait_character_requires_virtual_portrait_asset(self):
        shot = {"duration_sec": 8}
        character = {"id": "virtual_host", "identity_type": "virtual_person"}

        with self.assertRaisesRegex(
            s3_shots.SeedanceIdentityAuthorizationError,
            "virtual-portrait.*asset",
        ):
            s3_shots._dialogue_request_kwargs(shot, None, character=character)

    def test_custom_character_uses_description_only_for_first_seedance_master(self):
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "klein-1.png"
            second = Path(temp) / "klein-2.png"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            shot = {"duration_sec": 8}
            character = {
                "id": "custom_klein",
                "identity_type": "custom",
                "generation_mode": "fixed_asset",
                "image_path": str(first),
                "reference_images": [str(first), str(second)],
                "background_description": (
                    "A fictional young Asian occult detective"
                ),
            }

            request = s3_shots._dialogue_request_kwargs(
                shot, None, character=character
            )

        self.assertNotIn("references", request)

    def test_custom_character_prompt_does_not_claim_an_image_reference(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 1,
                "duration_sec": 8,
                "scene": "A Victorian harbor",
                "voiceover": "The ritual has already begun.",
            },
            {
                "id": "custom_klein",
                "name": "Klein",
                "identity_type": "custom",
                "generation_mode": "fixed_asset",
                "image_path": "C:/characters/klein.png",
                "look": "A fictional young Asian occult detective",
            },
        )

        self.assertNotIn("固定角色身份参考", prompt)
        self.assertIn("完全虚构", prompt)

    def test_seedance_privacy_rejection_is_non_retryable(self):
        response = Mock(
            status_code=400,
            text=(
                '{"error":{"code":"InputImageSensitiveContentDetected.'
                'PrivacyInformation","message":"input may contain real person"}}'
            ),
        )

        error = seedance._submission_error(
            response,
            endpoint="https://ark.example/tasks",
            model="doubao-seedance-2-5-260628",
            content_roles=["reference_image"],
        )

        self.assertIsInstance(error, seedance.SeedanceInputPrivacyError)
        self.assertTrue(error.non_retryable)
        self.assertIn("authorized asset://", str(error))

    def test_seedance_preserves_authorized_asset_uri(self):
        self.assertEqual(
            seedance._to_image_arg("asset://asset-xiaoling-authorized"),
            "asset://asset-xiaoling-authorized",
        )

    def test_s3_resolves_real_person_asset_before_paid_generation(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        characters = [{
            "id": "sales_person",
            "name": "销售顾问",
            "identity_type": "real_person",
        }]
        resolved = Mock(
            status="resolved",
            asset_uri="asset://asset-authorized-example",
        )

        with patch.object(
            s3_shots.volc_assets, "from_environment", return_value=Mock()
        ), patch.object(
            s3_shots.character_assets,
            "resolve_character_asset",
            return_value=resolved,
        ):
            changed = s3_shots._resolve_real_person_bindings(
                database, characters
            )

        self.assertTrue(changed)
        self.assertEqual(
            characters[0]["seedance_asset_uri"],
            "asset://asset-authorized-example",
        )

    def test_followup_dialogue_reuses_native_video_only(self):
        shot = {"duration_sec": 8}
        anchor = s3_shots.DialogueAnchor(
            video_url="https://cdn/anchor.mp4",
            duration_sec=30,
        )

        request = s3_shots._dialogue_request_kwargs(shot, anchor)

        self.assertEqual(request["reference_videos"], [anchor.video_url])
        self.assertEqual(request["reference_video_duration_sec"], 30)
        self.assertNotIn("reference_audios", request)

    def test_reference_video_cost_includes_input_and_output_duration(self):
        estimate = s3_shots._estimated_request_cost_cents({
            "duration": 6,
            "resolution": "480p",
            "reference_videos": ["https://cdn/anchor.mp4"],
            "reference_video_duration_sec": 30,
        })

        self.assertEqual(estimate, 2448)

    def test_runtime_has_no_omnihuman_dependency(self):
        source = inspect.getsource(s3_shots._gen_dialogue)

        self.assertNotIn("omnihuman", source.lower())
        self.assertNotIn("audio_url", source)

    def test_generated_dialogue_returns_video_only_anchor(self):
        source = inspect.getsource(s3_shots._gen_dialogue)

        self.assertIn("seedance.normalize_generation_duration", source)
        self.assertNotIn(
            "DialogueAnchor(result.video_url, result.audio_path)", source
        )

    def test_completed_dialogue_video_can_seed_a_later_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "01.mp4"
            video.write_bytes(b"video")

            anchor = s3_shots._dialogue_anchor_from_completed_shot({
                "video_path": str(video),
                "video_url": "https://cdn/01.mp4",
                "duration_sec": 12,
            })

        self.assertEqual(anchor.video_url, "https://cdn/01.mp4")
        self.assertEqual(anchor.duration_sec, 12)

    def test_completed_short_dialogue_anchor_uses_generated_clip_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "01.mp4"
            video.write_bytes(b"video")

            anchor = s3_shots._dialogue_anchor_from_completed_shot({
                "video_path": str(video),
                "video_url": "https://cdn/01.mp4",
                "duration_sec": 0.4,
            })

        self.assertEqual(anchor.duration_sec, 4)

    def test_short_dialogue_prompt_explicitly_pads_after_editorial_end(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 1,
                "duration_sec": 0.4,
                "scene": "设备停放场",
                "voiceover": "啊！",
            },
            {
                "id": "ai-character",
                "name": "员工甲",
                "identity_type": "custom",
                "generation_mode": "ai_generated",
            },
        )

        self.assertIn("0.4-4.0秒", prompt)
        self.assertIn("供本地剪辑裁切", prompt)

    def test_native_dialogue_prompt_drops_source_on_screen_text_instructions(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 1,
                "duration_sec": 4,
                "render_mode": "cinematic_character",
                "scene": (
                    "户外设备场；人物竖起拇指；画面文字：XCMG XG10.4HD\n"
                    "众能联合 400 805 8558"
                ),
                "action": "自然交流；画面文字：订10000台设备大客户？",
                "voiceover": "这是来自非洲的客户",
                "performance_beats": [{
                    "start": 0,
                    "duration": 4,
                    "dialogue": "这是来自非洲的客户",
                    "visual_intent": (
                        "人物在设备场交流；画面文字：众能联合 400 805 8558"
                    ),
                }],
            },
            {
                "id": "ai-character",
                "name": "员工甲",
                "identity_type": "custom",
                "generation_mode": "ai_generated",
            },
        )

        self.assertIn("户外设备场", prompt)
        self.assertNotIn("XCMG", prompt)
        self.assertNotIn("众能联合 400", prompt)
        self.assertNotIn("订10000台", prompt)

    def test_short_broll_request_uses_provider_safe_duration(self):
        request = s3_shots._broll_request_kwargs({"duration_sec": 3.33})

        self.assertEqual(request["duration"], 4)


class SeedanceGenerationGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_continuity_drift_is_merged_into_master_quality_gate(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=False, qa_required=True,
        )
        continuity_failed = video_qa.QADecision(False, ("identity_drift",))
        with tempfile.TemporaryDirectory() as tmp, \
            patch.dict(os.environ, {"SEEDANCE_MAX_AUTO_RETRIES": "0"}, clear=False), \
            patch.object(
                s3_shots.seedance, "text_to_video",
                new=AsyncMock(return_value=b"candidate"),
            ) as generate, \
            patch.object(
                s3_shots.tos, "upload",
                new=Mock(return_value="https://qa/current.mp4"),
            ), \
            patch.object(
                s3_shots.video_qa, "analyze",
                new=Mock(return_value=({
                    "visual_comparison": {"alignment": 1.0},
                    "transcript": "", "confidence": 1.0,
                }, video_qa.QADecision(True, ()))),
            ), \
            patch.object(
                s3_shots.video_qa, "analyze_continuity",
                new=Mock(return_value=({
                    "identity_checked": True, "identity_alignment": 0.4,
                }, continuity_failed)),
            ) as continuity:
            with self.assertRaises(s3_shots.SeedanceQualityError) as raised:
                await s3_shots._generate_seedance_video(
                    prompt="cinematic scene",
                    route=route,
                    request_kwargs={"duration": 4, "resolution": "480p"},
                    out_path=Path(tmp) / "shot.mp4",
                    key_hint="continuity-shot",
                    continuity_context={
                        "contract": {"scene": {"key": "yard"}},
                        "identity_image_urls": ["https://qa/lead.png"],
                        "scene_image_url": "https://qa/yard.png",
                        "previous_video_url": "https://qa/previous.mp4",
                    },
                )

        self.assertEqual(generate.await_count, 1)
        self.assertEqual(continuity.call_count, 1)
        self.assertEqual(raised.exception.reasons, ("identity_drift",))

    async def test_broll_semantic_failure_never_auto_retries_or_overwrites_master(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=False, qa_required=True,
        )
        failed = video_qa.QADecision(False, (
            "missing_industry_subject", "meaningless_hand_closeup",
        ))
        with tempfile.TemporaryDirectory() as tmp:
            master = Path(tmp) / "shot.mp4"
            master.write_bytes(b"accepted-master")
            with patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(return_value=b"bad-candidate"),
                ) as generate, patch.object(
                    s3_shots.tos, "upload",
                    new=Mock(return_value="https://qa/candidate"),
                ), patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(return_value=({
                        "visual_comparison": {"alignment": 1.0},
                        "broll_semantics": {
                            "industry_subject_visible": False,
                            "environment_context_visible": True,
                            "meaningful_action_visible": True,
                            "dominant_subject": "hands",
                            "hand_closeup_ratio": 0.9,
                        },
                    }, failed)),
                ):
                with self.assertRaisesRegex(
                    s3_shots.SeedanceQualityError,
                    "manual review",
                ) as raised:
                    await s3_shots._generate_seedance_video(
                        prompt="equipment rental yard",
                        route=route,
                        request_kwargs={"duration": 6, "resolution": "480p"},
                        out_path=master,
                        key_hint="job-shot-02",
                        require_broll_semantics=True,
                    )

            self.assertEqual(generate.await_count, 1)
            self.assertEqual(master.read_bytes(), b"accepted-master")
            self.assertEqual(raised.exception.reasons, (
                "missing_industry_subject", "meaningless_hand_closeup",
            ))
            self.assertTrue(raised.exception.candidate_path.is_file())
            self.assertEqual(
                raised.exception.candidate_path.read_bytes(), b"bad-candidate"
            )
            database = sqlite3.connect(":memory:")
            database.row_factory = sqlite3.Row
            database.executescript(db.DDL)
            database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state) "
                "VALUES('J-REVIEW','T-1','review','PRODUCTION_RUNNING')"
            )
            database.execute(
                "INSERT INTO shots(job_id,idx,state) "
                "VALUES('J-REVIEW',2,'PENDING')"
            )
            s3_shots._record_failed_generation(
                database, "J-REVIEW", 2, raised.exception
            )
            qa = json.loads(database.execute(
                "SELECT video_qa FROM shots WHERE job_id='J-REVIEW' AND idx=2"
            ).fetchone()[0])
            database.close()
            self.assertEqual(
                Path(qa["candidate_path"]), raised.exception.candidate_path
            )
            self.assertEqual(qa["candidate_url"], "https://qa/candidate")

    async def test_generation_qa_always_forbids_generated_readable_text(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=True, qa_required=True,
        )
        passed = video_qa.QADecision(True, ())
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(return_value=b"video"),
                ), \
                patch.object(
                    s3_shots.tos, "upload", new=Mock(return_value="https://qa/video")
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(return_value=({"visual_alignment": 0.95}, passed)),
                ) as analyze, \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ), \
                patch.object(
                    s3_shots.bigasr, "transcribe_detailed",
                    new=Mock(return_value=bigasr.Transcription("")),
                ):
            await s3_shots._generate_seedance_video(
                prompt="machine close-up",
                route=route,
                request_kwargs={"duration": 4, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-01",
            )

        self.assertTrue(analyze.call_args.kwargs["forbid_readable_text"])

    async def test_provider_usage_is_recorded_on_generation_result(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=True, qa_required=False,
        )

        async def generate_with_usage(**kwargs):
            kwargs["metadata_out"].update({
                "usage": {"completion_tokens": 260100}
            })
            return b"video"

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(side_effect=generate_with_usage),
                ), \
                patch.object(
                    s3_shots.tos, "upload", new=Mock(return_value="https://qa/video")
                ), \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ):
            result = await s3_shots._generate_seedance_video(
                prompt="presenter",
                route=route,
                request_kwargs={"duration": 12, "resolution": "720p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-01",
                expected_text="exact line",
            )

        self.assertEqual(result.cost_cents, 1821)
        self.assertEqual(result.provider_calls[0]["task_id"], None)
        self.assertEqual(
            result.provider_calls[0]["usage"]["completion_tokens"], 260100
        )

    async def test_retry_cost_and_provider_calls_are_cumulative(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=True, qa_required=True,
        )
        generated = iter([
            (b"first", "task-first", 100000),
            (b"retry", "task-retry", 200000),
        ])

        async def generate_with_usage(**kwargs):
            video, task_id, tokens = next(generated)
            kwargs["metadata_out"].update({
                "task_id": task_id,
                "usage": {"completion_tokens": tokens},
            })
            return video

        failed = video_qa.QADecision(False, ("unexpected_readable_text",))
        passed = video_qa.QADecision(True, ())
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(side_effect=generate_with_usage),
                ), \
                patch.object(
                    s3_shots.tos, "upload",
                    new=Mock(side_effect=["https://qa/first", "https://qa/retry"]),
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(side_effect=[
                        ({"visual_alignment": 0.95,
                          "has_readable_text": True}, failed),
                        ({"visual_alignment": 0.95}, passed),
                    ]),
                ), \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ), \
                patch.object(
                    s3_shots.bigasr, "transcribe_detailed",
                    new=Mock(return_value=bigasr.Transcription("")),
                ):
            result = await s3_shots._generate_seedance_video(
                prompt="machine close-up",
                route=route,
                request_kwargs={"duration": 4, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-01",
            )

        self.assertEqual(result.cost_cents, 2100)
        self.assertEqual(
            [call["task_id"] for call in result.provider_calls],
            ["task-first", "task-retry"],
        )
        self.assertIn("task-first", result.qa_note)
        self.assertIn("task-retry", result.qa_note)

    async def test_visual_mismatch_requires_manual_review_without_retry(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=False, qa_required=True,
        )

        async def generate_with_usage(**kwargs):
            kwargs["metadata_out"].update({
                "task_id": "task-visual",
                "usage": {"completion_tokens": 58045},
            })
            return b"video"

        failed = video_qa.QADecision(False, ("visual_mismatch",))
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(side_effect=generate_with_usage),
                ) as generate, \
                patch.object(
                    s3_shots.tos, "upload", new=Mock(return_value="https://qa/video")
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(return_value=(
                        {"visual_alignment": 0.2}, failed
                    )),
                ):
            with self.assertRaisesRegex(
                s3_shots.SeedanceQualityError,
                "manual review",
            ) as raised:
                await s3_shots._generate_seedance_video(
                    prompt="modern office entrance",
                    route=route,
                    request_kwargs={"duration": 6, "resolution": "480p"},
                    out_path=Path(tmp) / "shot.mp4",
                    key_hint="job-shot-02",
                )
            self.assertEqual(generate.await_count, 1)
            self.assertEqual(raised.exception.cost_cents, 406)
            self.assertIsNotNone(raised.exception.candidate_path)
            self.assertTrue(raised.exception.candidate_path.is_file())
            self.assertEqual(
                raised.exception.candidate_path.read_bytes(), b"video"
            )
            self.assertEqual(raised.exception.candidate_url, "https://qa/video")

    async def test_retry_budget_keeps_transcript_mismatch_for_manual_review(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=True, qa_required=True,
        )

        async def generate_with_usage(**kwargs):
            kwargs["metadata_out"].update({
                "task_id": "task-first",
                "usage": {"completion_tokens": 58045},
            })
            return b"video"

        visual_pass = video_qa.QADecision(True, ())
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {
                    "SEEDANCE_AUTO_RETRY_BUDGET_CENTS": "300",
                }), \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(side_effect=generate_with_usage),
                ) as generate, \
                patch.object(
                    s3_shots.tos, "upload", new=Mock(return_value="https://qa/video")
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(return_value=(
                        {"visual_alignment": 0.95}, visual_pass
                    )),
                ), \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ), \
                patch.object(
                    s3_shots.bigasr, "transcribe_detailed",
                    new=Mock(return_value=bigasr.Transcription("wrong line")),
                ):
            result = await s3_shots._generate_seedance_video(
                prompt="presenter",
                route=route,
                request_kwargs={"duration": 6, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-02",
                expected_text="exact line",
            )

        self.assertEqual(generate.await_count, 1)
        self.assertEqual(result.cost_cents, 406)
        self.assertEqual(result.transcription.text, "wrong line")
        qa_note = json.loads(result.qa_note)
        self.assertEqual(qa_note["status"], "manual_review_required")
        self.assertEqual(qa_note["reasons"], ["transcript_mismatch"])
        self.assertIn("retry budget exceeded", qa_note["auto_retry_skipped"])

    async def test_qa_service_error_keeps_paid_master_for_manual_review(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=False, qa_required=True,
        )

        async def generate_with_usage(**kwargs):
            kwargs["metadata_out"].update({
                "task_id": "task-qa-unavailable",
                "usage": {"completion_tokens": 58045},
            })
            return b"video"

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(side_effect=generate_with_usage),
                ) as generate, patch.object(
                    s3_shots.tos, "upload", new=Mock(return_value="https://qa/video")
                ), patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(side_effect=RuntimeError("qa endpoint unavailable")),
                ):
            result = await s3_shots._generate_seedance_video(
                prompt="presenter in office",
                route=route,
                request_kwargs={"duration": 6, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-qa-error",
            )

        self.assertEqual(generate.await_count, 1)
        self.assertEqual(result.video, b"video")
        self.assertIn("manual_review_required", result.qa_note)
        self.assertIn("qa_error", result.qa_note)

    async def test_job_budget_blocks_paid_call_before_provider_submission(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=False, qa_required=False,
        )
        budget_db = Mock()
        budget_db.execute.return_value.fetchone.return_value = {
            "cost_cents": 1700,
            "storyboard": json.dumps({
                "production_settings": {"job_budget_cents": 1800}
            }),
        }

        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {
                    "SEEDANCE_JOB_BUDGET_CENTS": "1800",
                }), \
                patch.object(s3_shots, "conn", return_value=budget_db), \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(return_value=b"video"),
                ) as generate, patch.object(
                    s3_shots.tos, "upload", return_value="https://video"
                ):
            with self.assertRaisesRegex(
                s3_shots.SeedanceBudgetError,
                "exceeded before paid call",
            ):
                await s3_shots._generate_seedance_video(
                    prompt="presenter",
                    route=route,
                    request_kwargs={"duration": 4, "resolution": "480p"},
                    out_path=Path(tmp) / "shot.mp4",
                    key_hint="budget-shot",
                    job_id="J-BUDGET",
                )

        generate.assert_not_awaited()

    def test_production_route_has_no_paid_local_edit_policy(self):
        self.assertFalse(hasattr(s3_shots, "_should_try_local_dialogue_repair"))
        self.assertFalse(hasattr(s3_shots, "_build_dialogue_repair_prompt"))

    async def test_failed_native_dialogue_retries_full_generation(self):
        self.assertTrue(hasattr(s3_shots, "_generate_seedance_video"))
        route = video_routing.VideoRoute(
            model="seedance-fast", tier="fast",
            generate_audio=True, qa_required=True,
        )
        passed = video_qa.QADecision(True, ())

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(side_effect=[b"wrong", b"correct"]),
                ) as generate, \
                patch.object(
                    s3_shots.tos, "upload",
                    new=Mock(side_effect=["https://qa/first", "https://qa/final"]),
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(return_value=(
                        {"visual_alignment": 0.95}, passed
                    )),
                ), \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ), \
                patch.object(
                    s3_shots.bigasr, "transcribe_detailed",
                    new=Mock(side_effect=[
                        bigasr.Transcription("exact lime", 700, 3100),
                        bigasr.Transcription("exact line", 650, 3050),
                    ]),
                ):
            result = await s3_shots._generate_seedance_video(
                prompt="machine close-up",
                route=route,
                request_kwargs={"duration": 4, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-01",
                expected_text="exact line",
            )

        self.assertEqual(generate.await_count, 2)
        self.assertTrue(generate.await_args_list[0].kwargs["generate_audio"])
        self.assertTrue(generate.await_args_list[1].kwargs["generate_audio"])
        self.assertEqual(generate.await_args_list[1].kwargs["duration"], 4)
        self.assertNotIn("ratio", generate.await_args_list[1].kwargs)
        self.assertNotIn("output_format", generate.await_args_list[1].kwargs)
        self.assertNotIn("task_intent", generate.await_args_list[1].kwargs)
        self.assertNotIn("reference_videos", generate.await_args_list[1].kwargs)
        self.assertEqual(generate.await_args_list[1].kwargs["prompt"], "machine close-up")
        self.assertTrue(result.native_audio)
        self.assertEqual(result.video, b"correct")
        self.assertEqual(result.video_path.suffix, ".mp4")
        self.assertIn("transcript_mismatch", result.qa_note)

    async def test_passing_native_audio_qa_keeps_first_generation(self):
        self.assertTrue(hasattr(s3_shots, "_generate_seedance_video"))
        route = video_routing.VideoRoute(
            model="seedance-fast", tier="fast",
            generate_audio=True, qa_required=True,
        )
        passed = video_qa.QADecision(True, ())

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(return_value=b"native"),
                ) as generate, \
                patch.object(
                    s3_shots.tos, "upload", new=Mock(return_value="https://qa/video")
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(return_value=(
                        {"transcript": "", "visual_alignment": 0.95}, passed
                    )),
                ), \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ), \
                patch.object(
                    s3_shots.bigasr, "transcribe_detailed",
                    new=Mock(return_value=bigasr.Transcription("")),
                ):
            result = await s3_shots._generate_seedance_video(
                prompt="machine close-up",
                route=route,
                request_kwargs={"duration": 4, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-01",
            )

        self.assertEqual(generate.await_count, 1)
        self.assertTrue(result.native_audio)
        self.assertEqual(result.video_url, "https://qa/video")

    async def test_hard_visual_failure_retries_same_seedance25_model(self):
        self.assertTrue(hasattr(s3_shots, "_generate_seedance_video"))
        self.assertIn(
            "fallback_model", video_routing.VideoRoute.__dataclass_fields__
        )
        route = video_routing.VideoRoute(
            model="seedance-fast", tier="fast",
            generate_audio=True, qa_required=True,
            fallback_model="seedance-full",
        )
        failed = video_qa.QADecision(False, ("unexpected_person",))
        passed = video_qa.QADecision(True, ())

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(side_effect=[b"wrong", b"full"]),
                ) as generate, \
                patch.object(
                    s3_shots.tos, "upload",
                    new=Mock(side_effect=["https://qa/first", "https://qa/final"]),
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(side_effect=[
                        ({"visual_alignment": 0.95,
                          "has_visible_person": True}, failed),
                        ({"visual_alignment": 0.95}, passed),
                    ]),
                ), \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ), \
                patch.object(
                    s3_shots.bigasr, "transcribe_detailed",
                    new=Mock(return_value=bigasr.Transcription("")),
                ):
            result = await s3_shots._generate_seedance_video(
                prompt="machine gauge, no people",
                route=route,
                request_kwargs={"duration": 4, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-01",
                forbid_people=True,
            )

        self.assertEqual(
            generate.await_args_list[1].kwargs["model"], "seedance-fast"
        )
        self.assertEqual(generate.await_args_list[1].kwargs["duration"], 4)
        self.assertNotIn("task_intent", generate.await_args_list[1].kwargs)
        self.assertEqual(result.model, "seedance-fast")
        self.assertTrue(result.native_audio)

    async def test_bigasr_transcript_overrides_visual_model_transcript(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=True, qa_required=True,
        )
        visual_pass = video_qa.QADecision(True, ())
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(return_value=b"video"),
                ), \
                patch.object(
                    s3_shots.tos, "upload", new=Mock(return_value="https://qa/video")
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(return_value=(
                        {"transcript": "hallucinated", "visual_alignment": 0.95},
                        visual_pass,
                    )),
                ), \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ), \
                patch.object(
                    s3_shots.bigasr, "transcribe_detailed",
                    new=Mock(return_value=bigasr.Transcription("exact", 500, 2800)),
                ):
            result = await s3_shots._generate_seedance_video(
                prompt="presenter",
                route=route,
                request_kwargs={"duration": 4, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-01",
                expected_text="exact",
            )

        self.assertTrue(result.native_audio)
        self.assertIn('"transcript": "exact"', result.qa_note)
        self.assertNotIn("hallucinated", result.qa_note)

    async def test_failed_retry_stops_after_two_full_generations(self):
        route = video_routing.VideoRoute(
            model="seedance-fast", tier="fast",
            generate_audio=True, qa_required=True,
        )
        visual_pass = video_qa.QADecision(True, ())
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(side_effect=[b"wrong", b"still-wrong"]),
                ) as generate, \
                patch.object(
                    s3_shots.tos, "upload",
                    new=Mock(side_effect=["https://qa/first", "https://qa/retry"]),
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(return_value=({"visual_alignment": 0.95}, visual_pass)),
                ), \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ), \
                patch.object(
                    s3_shots.bigasr, "transcribe_detailed",
                    new=Mock(side_effect=[
                        bigasr.Transcription("exact lime", 600, 2600),
                        bigasr.Transcription("exact pine", 600, 2600),
                    ]),
                ):
            with self.assertRaisesRegex(
                RuntimeError, "failed after retry"
            ) as raised:
                await s3_shots._generate_seedance_video(
                    prompt="presenter",
                    route=route,
                    request_kwargs={"duration": 4, "resolution": "480p"},
                    out_path=Path(tmp) / "shot.mp4",
                    key_hint="job-shot-01",
                    expected_text="exact line",
                )

        self.assertEqual(generate.await_count, 2)
        self.assertEqual(generate.await_args_list[1].kwargs["duration"], 4)
        self.assertNotIn("task_intent", generate.await_args_list[1].kwargs)
        self.assertEqual(raised.exception.cost_cents, 544)
        self.assertEqual(len(raised.exception.provider_calls), 2)


class LegacyRuntimeRemovalTests(unittest.TestCase):
    def test_tts_and_digital_human_runtime_modules_are_deleted(self):
        removed = [
            "scripts/factory/steps/s4_voice.py",
            "scripts/factory/clients/doubao_tts.py",
            "scripts/factory/clients/seedtts_v2.py",
            "scripts/factory/clients/seedtts_clone.py",
            "scripts/factory/clients/omnihuman.py",
            "scripts/factory/banned_words.py",
            "scripts/svg_diagrams.py",
        ]

        self.assertEqual(
            [path for path in removed if (PROJECT_ROOT / path).exists()],
            [],
        )

    def test_compose_reads_only_seedance_video_with_native_audio(self):
        source = inspect.getsource(s6_compose)

        self.assertFalse(hasattr(s6_compose, "_audio_strategy"))
        self.assertNotIn("audio_path", source)
        self.assertNotIn("provider", source)

    def test_smoke_check_contains_seedance_and_no_legacy_service(self):
        source = (PROJECT_ROOT / "scripts/smoke_check.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("ARK_VIDEO", source)
        self.assertNotIn("SeedTTS", source)
        self.assertNotIn("OmniHuman", source)

    def test_prompt_loader_has_no_voice_catalog_api(self):
        self.assertFalse(hasattr(prompts, "load_voice_catalog"))
        self.assertFalse(hasattr(prompts, "voice_catalog_table"))

    def test_requirements_drop_legacy_visual_service_sdk(self):
        requirements = (PROJECT_ROOT / "scripts/requirements.txt").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("volcengine", requirements.lower())


class Seedance25StateMachineTests(unittest.TestCase):
    def test_anchor_review_pause_is_not_recorded_as_generation_failure(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-PAUSE','T-PAUSE','锚点审核暂停','PRODUCTION_RUNNING','{}')"
        )
        database.commit()
        try:
            with (
                patch.object(orchestrator, "conn", return_value=database),
                patch.object(
                    orchestrator, "_run_step", new=AsyncMock(
                        side_effect=s3_shots.ContinuityAnchorReviewPendingError(
                            "人物与场景锚点等待审核"
                        )
                    ),
                ),
            ):
                asyncio.run(
                    orchestrator._process_job(
                        "J-PAUSE", asyncio.Semaphore(1)
                    )
                )
            row = database.execute(
                "SELECT state,error FROM jobs WHERE job_id='J-PAUSE'"
            ).fetchone()
            self.assertEqual(row["state"], "ANCHOR_REVIEW_REQUIRED")
            self.assertIsNone(row["error"])
        finally:
            database.close()

    def test_paid_quality_guard_stops_step_level_retry(self):
        quality_error = s3_shots.SeedanceQualityError(
            "manual review",
            cost_cents=406,
            provider_calls=(),
        )
        aggregate = s3_shots._aggregate_shot_failures([
            (2, quality_error),
        ])

        self.assertFalse(orchestrator._should_retry_step(aggregate))

    def test_transient_step_error_remains_retryable(self):
        self.assertTrue(
            orchestrator._should_retry_step(RuntimeError("temporary timeout"))
        )

    def test_successful_steps_clear_stale_retry_errors(self):
        dialogue_source = inspect.getsource(s3_shots._gen_dialogue)
        broll_source = inspect.getsource(s3_shots._gen_broll)
        orchestrator_source = inspect.getsource(orchestrator._process_job)

        self.assertIn("error=NULL", dialogue_source)
        self.assertIn("error=NULL", broll_source)
        self.assertIn("error=NULL", orchestrator_source)

    def test_state_machine_skips_voice_generation_step(self):
        step_names = [step for _, step, _ in models.TRANSITIONS]

        self.assertNotIn("s4_voice", step_names)
        self.assertNotIn("s2_character", step_names)
        self.assertNotIn("s4_voice", orchestrator.STEP_MODULES)
        self.assertNotIn("s2_character", orchestrator.STEP_MODULES)

    def test_character_step_flows_directly_to_seedance_shots(self):
        transition = next(
            item for item in models.TRANSITIONS
            if item[0] is models.State.CHARACTER_OK
        )

        self.assertEqual(transition[1], "s3_shots")
        self.assertIs(transition[2], models.State.SHOTS_OK)

    def test_storyboard_flows_directly_to_seedance_shots(self):
        transition = next(
            item for item in models.TRANSITIONS
            if item[0] is models.State.STORYBOARDED
        )

        self.assertEqual(transition[1], "s3_shots")
        self.assertIs(transition[2], models.State.SHOTS_OK)


class HookOverlaySizingTests(unittest.TestCase):
    def test_long_hook_font_size_fits_single_line(self):
        hook = "设备租赁？狗都不干！狗不干，我干。"
        size = ffmpeg._hook_font_size(hook, 1080)

        self.assertLess(size, int(1080 * 0.072))
        self.assertLessEqual(size * len(hook), int(1080 * 0.90))

    def test_short_hook_keeps_large_display_size(self):
        self.assertEqual(
            ffmpeg._hook_font_size("我干", 1080),
            int(1080 * 0.072),
        )


    def test_thumbnail_long_hook_fits_inside_safe_width(self):
        hook = "设备租赁？狗都不干！狗不干，我干。"
        size = ffmpeg._thumbnail_hook_font_size(hook, 720)

        self.assertLessEqual(size * len(hook), 720 - 80)

    def test_thumbnail_short_hook_keeps_large_display_size(self):
        self.assertEqual(
            ffmpeg._thumbnail_hook_font_size("我干", 720),
            int(720 * 0.13),
        )


class ThumbnailEncodingTests(unittest.IsolatedAsyncioTestCase):
    async def test_thumbnail_uses_full_range_single_thread_mjpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            out = base / "thumbnail.jpg"

            async def render(_args):
                out.write_bytes(b"jpeg")

            with (
                patch.object(ffmpeg, "probe_duration", new=AsyncMock(return_value=8.0)),
                patch.object(ffmpeg, "_run", new=AsyncMock(side_effect=render)) as run,
            ):
                await ffmpeg.generate_thumbnail(
                    base / "final.mp4",
                    "验收封面",
                    out,
                )

        args = run.await_args.args[0]
        self.assertEqual(args[args.index("-pix_fmt") + 1], "yuvj420p")
        self.assertEqual(args[args.index("-threads") + 1], "1")

    async def test_thumbnail_seek_is_clamped_inside_short_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            out = base / "thumbnail.jpg"

            async def render(_args):
                out.write_bytes(b"jpeg")

            with (
                patch.object(ffmpeg, "probe_duration", new=AsyncMock(return_value=4.04)),
                patch.object(ffmpeg, "_run", new=AsyncMock(side_effect=render)) as run,
            ):
                await ffmpeg.generate_thumbnail(
                    base / "final.mp4", "短片封面", out, ts=4.5
                )

        args = run.await_args.args[0]
        self.assertLess(float(args[args.index("-ss") + 1]), 4.04)

    async def test_thumbnail_fails_when_ffmpeg_writes_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with (
                patch.object(ffmpeg, "probe_duration", new=AsyncMock(return_value=4.04)),
                patch.object(ffmpeg, "_run", new=AsyncMock()),
            ):
                with self.assertRaisesRegex(ffmpeg.FFmpegError, "no output"):
                    await ffmpeg.generate_thumbnail(
                        base / "final.mp4", "短片封面", base / "thumbnail.jpg"
                    )


class SceneVisualAnchorTests(unittest.IsolatedAsyncioTestCase):
    def test_continuity_qa_context_prefers_canonical_refs_and_same_scene_video(self):
        speaker = {"id": "lead", "generation_mode": "ai_generated"}
        cast = [{"id": "client", "generation_mode": "ai_generated"}]
        identity_anchors = {
            "script:lead": s3_shots.DialogueAnchor(
                image_reference="https://assets/lead.png",
                anchor_mode="static_image",
            ),
            "script:client": s3_shots.DialogueAnchor(
                image_reference="https://assets/client.png",
                anchor_mode="static_image",
            ),
        }
        previous_scene = s3_shots.DialogueAnchor(
            "https://cdn/scene-previous.mp4", 8, chain_depth=1,
        )
        previous_speaker = s3_shots.DialogueAnchor(
            "https://cdn/speaker-previous.mp4", 4, chain_depth=1,
        )
        contract = {"version": 1, "scene": {"key": "yard-a"}}

        context = s3_shots._continuity_qa_context(
            {"continuity_contract": contract},
            speaker,
            cast_characters=cast,
            identity_anchors=identity_anchors,
            scene_reference="https://assets/yard.png",
            scene_video_anchor=previous_scene,
            speaker_anchor=previous_speaker,
        )

        self.assertEqual(context["contract"], contract)
        self.assertEqual(
            context["identity_image_urls"],
            ["https://assets/lead.png", "https://assets/client.png"],
        )
        self.assertEqual(context["scene_image_url"], "https://assets/yard.png")
        self.assertEqual(
            context["previous_video_url"],
            "https://cdn/scene-previous.mp4",
        )

    def test_incremental_video_anchor_resets_at_configured_chain_depth(self):
        shallow = s3_shots.DialogueAnchor(
            "https://cdn/shot-2.mp4", 4, chain_depth=2,
        )
        drift_prone = s3_shots.DialogueAnchor(
            "https://cdn/shot-3.mp4", 4, chain_depth=3,
        )

        with patch.dict(os.environ, {"SEEDANCE_CONTINUITY_RESET_INTERVAL": "3"}):
            self.assertIs(s3_shots._incremental_anchor(shallow), shallow)
            self.assertIsNone(s3_shots._incremental_anchor(drift_prone))

    def test_generated_continuity_anchors_must_be_approved_before_master_calls(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-GATE','T-GATE','审核门','PRODUCTION_RUNNING','{}')"
        )
        database.execute(
            "INSERT INTO character_identity_anchors("
            "job_id,identity_key,character_id,display_name,source_type,state,approval_state) "
            "VALUES('J-GATE','script:lead','lead','主角','seedream_identity_image',"
            "'READY','PENDING_REVIEW')"
        )
        database.execute(
            "INSERT INTO character_identity_anchors("
            "job_id,identity_key,character_id,display_name,source_type,state,approval_state) "
            "VALUES('J-GATE','volc:asset://boss','boss','老大','volc_asset',"
            "'READY','PENDING_REVIEW')"
        )
        database.execute(
            "INSERT INTO scene_visual_anchors("
            "job_id,anchor_key,description,master_indexes,state,approval_state) "
            "VALUES('J-GATE','yard','停车场','[1,2]','READY','APPROVED')"
        )
        database.commit()
        try:
            with self.assertRaisesRegex(
                s3_shots.ContinuityAnchorReviewPendingError, "主角"
            ):
                s3_shots._assert_continuity_anchors_approved(
                    database, "J-GATE"
                )
            database.execute(
                "UPDATE character_identity_anchors SET approval_state='APPROVED' "
                "WHERE identity_key='script:lead'"
            )
            database.commit()
            s3_shots._assert_continuity_anchors_approved(database, "J-GATE")
        finally:
            database.close()

    def test_continuity_contract_materializes_canonical_scene_and_character_state(self):
        characters = [
            {
                "id": "boss", "name": "老大", "look": "白色安全帽、荧光绿反光背心",
                "generation_mode": "fixed_asset", "identity_type": "virtual_person",
                "seedance_asset_uri": "asset://asset-boss",
            },
            {
                "id": "client", "name": "客户", "look": "灰色V领毛衣",
                "generation_mode": "ai_generated",
            },
        ]
        storyboard = {
            "characters": characters,
            "masters": [
                {
                    "idx": 1, "speaker": "boss", "scene_anchor_key": "yard-a",
                    "scene": "场景：户外设备停车场；出镜人物：老大、客户；人物动作：面对面交谈",
                    "action": "老大拿着手机，客户站在右侧", "props": ["手机"],
                    "time_of_day": "白天", "lighting": "左前方自然光",
                },
                {
                    "idx": 2, "speaker": "client", "scene_anchor_key": "yard-a",
                    "scene": "场景：户外设备停车场；出镜人物：客户、老大；人物动作：客户回应",
                    "action": "客户继续站在右侧，老大保持左侧",
                    "time_of_day": "白天", "lighting": "左前方自然光",
                },
            ],
        }

        changed = scene_assets.apply_continuity_metadata(storyboard, characters)

        self.assertTrue(changed)
        first = storyboard["masters"][0]
        contract = first["continuity_contract"]
        self.assertEqual(contract["version"], 1)
        self.assertEqual(contract["scene"]["key"], "yard-a")
        self.assertEqual(contract["scene"]["time_of_day"], "白天")
        self.assertEqual(contract["scene"]["lighting"], "左前方自然光")
        self.assertEqual(contract["props"], ["手机"])
        self.assertEqual(contract["camera"]["axis"], "preserve-180-degree-axis")
        by_name = {item["display_name"]: item for item in contract["characters"]}
        self.assertEqual(by_name["老大"]["wardrobe"], "白色安全帽、荧光绿反光背心")
        self.assertEqual(by_name["老大"]["screen_position"], "screen-left")
        self.assertEqual(by_name["客户"]["screen_position"], "screen-right")
        self.assertEqual(
            storyboard["masters"][1]["continuity_contract"]["scene"]["key"],
            "yard-a",
        )

    def test_source_scene_metadata_locks_repeated_space_and_two_identities(self):
        characters = [
            {
                "id": "女员工-近景", "name": "老大",
                "generation_mode": "fixed_asset",
                "identity_type": "virtual_person",
                "seedance_asset_uri": "asset://asset-boss",
            },
            {
                "id": "女员工-中景", "name": "老大",
                "generation_mode": "fixed_asset",
                "identity_type": "virtual_person",
                "seedance_asset_uri": "asset://asset-boss",
            },
            {
                "id": "黑人客户", "name": "黑人客户",
                "generation_mode": "ai_generated",
            },
        ]
        storyboard = {
            "characters": characters,
            "masters": [
                {
                    "idx": 1, "speaker": "女员工-近景",
                    "scene": "场景：户外工程设备停放场地，背景有黄色工程机械；"
                             "出镜人物：女员工-近景、黑人客户；人物动作：二人站立",
                    "action": "女员工-近景与黑人客户面向镜头",
                },
                {
                    "idx": 2, "speaker": "女员工-中景",
                    "scene": "场景：户外工程设备停放场地，地面为水泥路面；"
                             "出镜人物：女员工-中景、黑人客户；人物动作：二人点赞",
                    "action": "女员工-中景与黑人客户保持原站位",
                },
                {
                    "idx": 3, "speaker": "黑人客户",
                    "scene": "场景：户外工程设备停放场地，背景有黄色吊车；"
                             "出镜人物：黑人客户、女员工-中景；人物动作：客户点赞",
                    "action": "黑人客户说话，女员工-中景在旁边",
                },
            ],
        }

        changed = scene_assets.apply_continuity_metadata(
            storyboard, characters
        )

        self.assertTrue(changed)
        keys = {master["scene_anchor_key"] for master in storyboard["masters"]}
        self.assertEqual(len(keys), 1)
        plan = scene_assets.recurring_scene_anchors(storyboard)
        self.assertEqual(plan[0]["master_indexes"], [1, 2, 3])
        positions = storyboard["masters"][0]["blocking_anchor"]["positions"]
        self.assertEqual(
            [item["screen_position"] for item in positions],
            ["screen-left", "screen-right"],
        )
        self.assertEqual(len(positions), 2)

    def test_dialogue_request_reuses_previous_master_from_same_scene(self):
        previous_scene = s3_shots.DialogueAnchor(
            "https://cdn/scene-previous.mp4", 8
        )
        previous_speaker = s3_shots.DialogueAnchor(
            "https://cdn/speaker-previous.mp4", 4
        )

        request = s3_shots._dialogue_request_kwargs(
            {"duration_sec": 4},
            previous_speaker,
            character={"id": "lead", "generation_mode": "ai_generated"},
            scene_video_anchor=previous_scene,
        )

        self.assertEqual(
            request["reference_videos"],
            [
                "https://cdn/scene-previous.mp4",
                "https://cdn/speaker-previous.mp4",
            ],
        )
        self.assertEqual(request["reference_video_duration_sec"], 12)

    def test_dialogue_prompt_contains_locked_scene_blocking_contract(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 2,
                "duration_sec": 4,
                "voiceover": "继续介绍项目。",
                "scene": "户外设备停车场",
                "action": "两人继续对话",
                "scene_anchor_key": "scene-yard",
                "blocking_anchor": {
                    "axis": "preserve-180-degree-axis",
                    "positions": [
                        {"display_name": "老大", "screen_position": "screen-left"},
                        {"display_name": "客户", "screen_position": "screen-right"},
                    ],
                },
            },
            {"id": "boss", "name": "老大", "generation_mode": "ai_generated"},
            anchored=True,
            scene_anchored=True,
            previous_scene_anchored=True,
        )

        self.assertIn("scene-yard", prompt)
        self.assertIn("screen-left", prompt)
        self.assertIn("screen-right", prompt)
        self.assertIn("180", prompt)
        self.assertIn("人物身份仍以定妆照或授权角色资产为准", prompt)

    def test_dialogue_prompt_serializes_structured_continuity_contract(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 2, "duration_sec": 4, "voiceover": "继续介绍。",
                "scene": "户外停车场", "action": "老大拿着手机与客户交流",
                "scene_anchor_key": "yard-a",
                "blocking_anchor": {"axis": "preserve-180-degree-axis", "positions": []},
                "continuity_contract": {
                    "version": 1,
                    "scene": {
                        "key": "yard-a", "description": "户外停车场",
                        "time_of_day": "白天", "lighting": "左前方自然光",
                    },
                    "characters": [{
                        "display_name": "老大", "wardrobe": "白色安全帽、荧光绿反光背心",
                        "screen_position": "screen-left",
                    }],
                    "props": ["手机"],
                    "camera": {"axis": "preserve-180-degree-axis"},
                },
            },
            {"id": "boss", "name": "老大", "generation_mode": "ai_generated"},
            anchored=True, scene_anchored=True,
        )

        self.assertIn("结构化连续性契约", prompt)
        self.assertIn("白色安全帽、荧光绿反光背心", prompt)
        self.assertIn("screen-left", prompt)
        self.assertIn("手机", prompt)
        self.assertIn("左前方自然光", prompt)

    def test_dialogue_request_combines_scene_and_character_image_references(self):
        request = s3_shots._dialogue_request_kwargs(
            {"duration_sec": 4},
            None,
            character={"id": "lead", "generation_mode": "ai_generated"},
            identity_anchors={
                "script:lead": s3_shots.DialogueAnchor(
                    image_reference="https://assets/lead.png",
                    anchor_mode="static_image",
                )
            },
            scene_reference="https://assets/repair-bay.png",
        )

        self.assertEqual(
            request["references"],
            ["https://assets/repair-bay.png", "https://assets/lead.png"],
        )

    async def test_repeated_scene_anchor_is_generated_once_and_resumed(self):
        storyboard = {
            "production_settings": {"budget_cents": 1000},
            "masters": [
                {
                    "idx": 1, "scene": "设备检修车间",
                    "scene_anchor_key": "repair-bay-a",
                },
                {
                    "idx": 2, "scene": "设备检修车间",
                    "scene_anchor_key": "repair-bay-a",
                },
            ],
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(tmp)
            database_path = base / "factory.db"
            with (
                patch.dict(os.environ, {"FACTORY_DB_PATH": str(database_path)}),
                patch.object(s3_shots, "ASSETS_DIR", base / "assets"),
                patch.object(
                    s3_shots.seedance, "text_to_image",
                    new=AsyncMock(return_value=b"png"),
                ) as generate,
                patch.object(
                    s3_shots.tos, "upload",
                    return_value="https://assets/repair-bay.png",
                ),
            ):
                database = db.conn()
                database.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
                    "VALUES(?,?,?,?,?)",
                    ("J-SCENE", "T-SCENE", "scene", "STORYBOARDED",
                     json.dumps(storyboard)),
                )
                database.commit()
                first = await s3_shots._ensure_scene_visual_anchors(
                    "J-SCENE", storyboard
                )
                second = await s3_shots._ensure_scene_visual_anchors(
                    "J-SCENE", storyboard
                )
                row = database.execute(
                    "SELECT state,cost_cents FROM scene_visual_anchors "
                    "WHERE job_id='J-SCENE'"
                ).fetchone()

        self.assertEqual(first, {"repair-bay-a": "https://assets/repair-bay.png"})
        self.assertEqual(second, first)
        self.assertEqual(generate.await_count, 1)
        self.assertEqual((row["state"], row["cost_cents"]), ("READY", 29))


class ComposeNativeAudioTests(unittest.IsolatedAsyncioTestCase):
    def test_asr_timings_replace_estimated_static_subtitles_in_director_shots(self):
        storyboard = {
            "production_mode": "native_master_local_director",
            "shots": [
                {
                    "idx": 1, "audio_master_idx": 2,
                    "audio_start": 0, "duration": 4,
                    "subtitle": "estimated opening label",
                },
                {
                    "idx": 2, "audio_master_idx": 2,
                    "audio_start": 4, "duration": 4,
                    "subtitle": "estimated closing label",
                },
                {
                    "idx": 3, "audio_master_idx": 9,
                    "audio_start": 0, "duration": 4,
                    "subtitle": "another master",
                },
            ],
        }
        transcription = bigasr.Transcription(
            "狗不干？我干。拼的不是腿，是信息和效率。",
            2370,
            6830,
            (
                bigasr.TimedUtterance("狗不干？我干。", 2370, 3650),
                bigasr.TimedUtterance(
                    "拼的不是腿，是信息和效率。", 4010, 6830
                ),
            ),
        )

        changed = s3_shots._apply_master_transcription(
            storyboard, 2, transcription
        )

        self.assertTrue(changed)
        self.assertIsNone(storyboard["shots"][0]["subtitle"])
        self.assertEqual(
            storyboard["shots"][0]["subtitle_cues"],
            [{"start": 2.37, "end": 3.65, "text": "狗不干？我干。"}],
        )
        self.assertEqual(
            storyboard["shots"][1]["subtitle_cues"],
            [{"start": 0.01, "end": 2.83,
              "text": "拼的不是腿，是信息和效率。"}],
        )
        self.assertEqual(storyboard["shots"][2]["subtitle"], "another master")

    def test_asr_text_bounds_create_a_single_timed_cue_when_utterances_absent(self):
        storyboard = {
            "production_mode": "native_master_local_director",
            "shots": [{
                "idx": 1, "audio_master_idx": 1,
                "audio_start": 1, "duration": 5,
                "subtitle": "estimated",
            }],
        }

        s3_shots._apply_master_transcription(
            storyboard, 1,
            bigasr.Transcription("真实台词。", 1500, 4200),
        )

        self.assertEqual(
            storyboard["shots"][0]["subtitle_cues"],
            [{"start": 0.5, "end": 3.2, "text": "真实台词。"}],
        )

    def test_director_segments_preserve_timed_subtitle_cues(self):
        storyboard = {
            "shots": [{
                "idx": 1,
                "video_master_idx": 2,
                "audio_master_idx": 2,
                "duration": 6,
                "subtitle": "legacy always-on label",
                "subtitle_cues": [
                    {"start": 2.37, "end": 3.65, "text": "狗不干？我干。"},
                    {"start": 4.01, "end": 6.0, "text": "拼的不是腿，是信息和效率。"},
                ],
            }]
        }
        rows = [{"idx": 2, "video_path": "master-02.mp4"}]

        segments = s6_compose._director_segments(storyboard, rows)

        self.assertEqual(
            segments[0]["subtitle_cues"],
            storyboard["shots"][0]["subtitle_cues"],
        )

    def test_timed_subtitle_filters_are_enabled_only_inside_cue_windows(self):
        with tempfile.TemporaryDirectory() as td:
            filters = ffmpeg._timed_subtitle_filters(
                Path(td),
                "segment-02",
                [
                    {"start": 2.37, "end": 3.65, "text": "狗不干？我干。"},
                    {"start": 4.01, "end": 6.0, "text": "拼的不是腿，是信息和效率。"},
                ],
                width=1080,
            )

            self.assertEqual(len(filters), 2)
            self.assertIn("between(t,2.370,3.650)", filters[0])
            self.assertIn("between(t,4.010,6.000)", filters[1])
            self.assertEqual(
                (Path(td) / "segment-02_sub_01_line_01.txt").read_text(
                    encoding="utf-8"
                ),
                "狗不干？我干",
            )

    def test_subtitle_display_removes_terminal_full_stop_only(self):
        self.assertEqual(ffmpeg._subtitle_display_text("需求自动匹配。"), "需求自动匹配")
        self.assertEqual(ffmpeg._subtitle_display_text("狗都不干！"), "狗都不干！")
        self.assertEqual(ffmpeg._subtitle_display_text("设备租赁？"), "设备租赁？")

    def test_subtitle_text_wraps_inside_vertical_video_safe_width(self):
        text = "我们可以顺着这个思路，去深入挖掘数字背后的真实含义，助力设备租赁业务"

        wrapped = ffmpeg._wrap_subtitle_text(text, width=1080)

        lines = wrapped.splitlines()
        self.assertEqual("".join(lines), text)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(len(line) <= 17 for line in lines))

    def test_subtitle_wrapping_does_not_split_ascii_number_tokens(self):
        text = "看，来源中明确提到了‘2312’，这肯定不是偶然"

        lines = ffmpeg._wrap_subtitle_text(text, width=1080).splitlines()

        self.assertTrue(any("2312" in line for line in lines))
        self.assertFalse(any(line.endswith("‘") for line in lines))

    def test_timed_subtitle_files_use_wrapped_display_text(self):
        with tempfile.TemporaryDirectory() as td:
            filters = ffmpeg._timed_subtitle_filters(
                Path(td),
                "segment-long",
                [{"start": 0, "end": 5, "text": "大家都在猜测这个数字到底和设备租赁有啥关系，毫无头绪啊。"}],
                width=1080,
            )

            rendered = [
                (Path(td) / f"segment-long_sub_01_line_{line:02d}.txt").read_text(
                    encoding="utf-8"
                )
                for line in (1, 2)
            ]
            self.assertNotIn("。", "".join(rendered))
            self.assertTrue(all(len(line) <= 17 for line in rendered))
            self.assertEqual(len(filters), 2)
            self.assertIn("y=h*0.80-34", filters[0])
            self.assertIn("y=h*0.80+34", filters[1])
            self.assertTrue(all("between(t,0.000,5.000)" in item for item in filters))

    def test_native_mix_normalizes_ambience_without_lowering_narration(self):
        self.assertTrue(hasattr(ffmpeg, "_native_narration_filter"))

        filt = ffmpeg._native_narration_filter(
            ambient_lufs=-32.0, narration_db=0.0
        )

        self.assertIn("loudnorm=I=-32.0:TP=-9.0:LRA=7.0", filt)
        self.assertIn("normalize=0", filt)
        self.assertIn("alimiter=limit=0.95", filt)
        self.assertNotIn("volume=-18", filt)

    async def test_seedance_native_track_is_kept_without_external_mix(self):
        row = {
            "idx": 1,
            "video_path": "shot.mp4",
            "audio_path": "voice.mp3",
            "provider": "seedance",
        }
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s6_compose.ffmpeg, "has_audio_stream",
                    new=AsyncMock(return_value=True),
                ), \
                patch.object(
                    s6_compose.ffmpeg, "mix_video_narration",
                    new=AsyncMock(),
                ) as mix, \
                patch.object(
                    s6_compose.ffmpeg, "probe_duration",
                    new=AsyncMock(return_value=4.0),
                ), \
                patch.object(
                    s6_compose.ffmpeg, "normalize_shot",
                    new=AsyncMock(),
                ):
            await s6_compose._prep_shot(
                row, Path(tmp), subtitle=None, hook=None
            )

        mix.assert_not_awaited()


class StandardDirectorPipelineTests(unittest.TestCase):
    @staticmethod
    def _storyboard() -> dict:
        return {
            "production_profile": "standard",
            "production_mode": "native_master_local_director",
            "characters": [{
                "id": "host",
                "name": "Host",
                "gender": "female",
                "look": "A fictional Chinese equipment-rental consultant",
            }],
            "hook": "Stop generating every shot",
            "bgm": {"mood": "restrained", "prompt": "light rhythm"},
            "masters": [
                {
                    "idx": 1,
                    "type": "dialogue",
                    "speaker": "host",
                    "render_mode": "presenter",
                    "duration": 6,
                    "scene": "equipment yard office",
                    "action": "speaks naturally to camera",
                    "voiceover": "The opening hook is delivered here.",
                },
                {
                    "idx": 2,
                    "type": "dialogue",
                    "speaker": "host",
                    "render_mode": "presenter",
                    "duration": 18,
                    "scene": "same equipment yard office",
                    "action": "continues the explanation",
                    "voiceover": "The complete body narration is delivered continuously here.",
                },
                {
                    "idx": 3,
                    "type": "broll",
                    "duration": 10,
                    "prompt": "Natural close-ups of rental equipment in operation",
                },
            ],
            "shots": [
                {
                    "idx": 1,
                    "video_master_idx": 1,
                    "audio_master_idx": 1,
                    "video_start": 0,
                    "audio_start": 0,
                    "duration": 6,
                    "subtitle": "Opening hook",
                },
                {
                    "idx": 2,
                    "video_master_idx": 2,
                    "audio_master_idx": 2,
                    "video_start": 0,
                    "audio_start": 0,
                    "duration": 6,
                    "subtitle": "Body point one",
                },
                {
                    "idx": 3,
                    "video_master_idx": 3,
                    "audio_master_idx": 2,
                    "video_start": 0,
                    "audio_start": 6,
                    "duration": 6,
                    "subtitle": "Body point two",
                },
                {
                    "idx": 4,
                    "video_master_idx": 2,
                    "audio_master_idx": 2,
                    "video_start": 12,
                    "audio_start": 12,
                    "duration": 6,
                    "subtitle": "Closing line",
                },
            ],
        }

    def test_standard_profile_accepts_three_masters_and_four_edit_shots(self):
        s1_storyboard._validate(self._storyboard())

    def test_standard_profile_accepts_more_than_three_paid_masters_within_duration(self):
        storyboard = self._storyboard()
        storyboard["masters"].append({
            "idx": 4,
            "type": "broll",
            "duration": 4,
            "prompt": "Another paid visual master",
        })

        s1_storyboard._validate(storyboard)

    def test_generation_units_are_masters_not_final_edit_shots(self):
        self.assertTrue(hasattr(s1_storyboard, "_generation_units"))
        units = s1_storyboard._generation_units(self._storyboard())

        self.assertEqual([unit["idx"] for unit in units], [1, 2, 3])

    def test_director_timeline_can_take_video_and_audio_from_different_masters(self):
        self.assertTrue(hasattr(s6_compose, "_director_segments"))
        rows = [
            {"idx": 1, "video_path": "master-1.mp4"},
            {"idx": 2, "video_path": "master-2.mp4"},
            {"idx": 3, "video_path": "master-3.mp4"},
        ]

        segments = s6_compose._director_segments(self._storyboard(), rows)

        cutaway = segments[2]
        self.assertEqual(cutaway["video_path"], "master-3.mp4")
        self.assertEqual(cutaway["native_audio"], "master-2.mp4")
        self.assertEqual(cutaway["audio_start"], 6)

    def test_storyboard_prompt_defaults_to_standard_director_mode(self):
        prompt = prompts.storyboard_prompt(
            title="Equipment rental",
            track="B2B",
            duration=30,
        )

        self.assertIn('"production_profile": "standard"', prompt)
        self.assertIn('"production_mode": "native_master_local_director"', prompt)
        self.assertIn('"masters"', prompt)
        self.assertIn("video_master_idx", prompt)
        self.assertIn("audio_master_idx", prompt)
        self.assertNotIn(
            '"shots": [\n    {\n      "idx": 1,\n      "type": "dialogue"',
            prompt,
        )

    def test_ffmpeg_exposes_local_director_cut(self):
        self.assertTrue(hasattr(ffmpeg, "cut_director_segment"))

    def test_dialogue_audio_master_must_be_consumed_continuously(self):
        storyboard = self._storyboard()
        storyboard["shots"][2]["audio_start"] = 7

        with self.assertRaisesRegex(ValueError, "audio timeline.*continuous"):
            s1_storyboard._validate(storyboard)

    def test_standard_profile_default_job_budget_is_twenty_five_yuan(self):
        env_template = (PROJECT_ROOT / "templates" / "env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("SEEDANCE_JOB_BUDGET_CENTS=2500", env_template)
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(s3_shots._job_budget_cents({}), 2500)


if __name__ == "__main__":
    unittest.main()
