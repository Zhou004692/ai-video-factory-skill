"""v2.5 scene-dialogue regression tests.

These tests stay provider-free: they only verify routing, prompt safety and
legacy-keyframe handling.  No image, video, TTS or object-storage API is used.
"""
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import prompts  # noqa: E402
from factory.steps import s1_storyboard, s2_character, s3_shots  # noqa: E402


SCENE_SHOT = {
    "idx": 2,
    "render_mode": "scene",
    "camera": "镜头从图纸缓慢平移到设备仪表盘",
    "shot_type": "物件特写",
    "lens": "50mm, f/2.8",
    "lighting": "午后侧光",
    "scene": "工地办公室木桌上摊开报价单，窗外设备虚化",
    "scene_dynamics": "报价单页角被风轻轻吹动，仪表指针缓慢回落",
    "action": "老周正面对镜头说话并指向报价单",
    "voiceover": "VOICEOVER_SENTINEL：这句只应该进入音轨，不能进入画面提示词。",
}

LEGACY_SCENE_SHOT = {
    **SCENE_SHOT,
    "scene": "工地办公室木桌旁，老周正面对镜头说话，报价单和仪表盘铺在桌面",
    "camera": "从老周侧脸推近后摇到报价单特写",
}


class SceneVoiceoverKeyframeTests(unittest.TestCase):
    def test_character_keyframe_keeps_shot_level_lighting(self):
        prompt = s2_character._build_prompt(
            {"look": "稳定角色外观"},
            SCENE_SHOT,
        )

        self.assertIn("光线: 午后侧光", prompt)

    def test_scene_dialogue_routes_to_faceless_keyframe(self):
        self.assertEqual(
            s2_character._dialogue_keyframe_kind(SCENE_SHOT),
            "scene_voiceover",
        )
        self.assertEqual(
            s2_character._dialogue_keyframe_kind({}),
            "digital_human",
        )

    def test_keyframe_prompt_is_faceless_and_does_not_use_spoken_copy(self):
        prompt = s2_character._build_scene_voiceover_prompt(SCENE_SHOT)

        self.assertIn(SCENE_SHOT["scene"], prompt)
        self.assertIn(SCENE_SHOT["scene_dynamics"], prompt)
        self.assertIn("画外音", prompt)
        self.assertIn("不得出现任何可识别人脸", prompt)
        self.assertIn("不做口型同步", prompt)
        self.assertNotIn("VOICEOVER_SENTINEL", prompt)
        self.assertNotIn("正面对镜头说话", prompt)

    def test_legacy_human_clauses_are_removed_from_keyframe_prompt(self):
        prompt = s2_character._build_scene_voiceover_prompt(LEGACY_SCENE_SHOT)

        self.assertIn("工地办公室木桌旁", prompt)
        self.assertIn("报价单和仪表盘铺在桌面", prompt)
        self.assertNotIn("老周", prompt)

    def test_keyframe_prompt_keeps_shot_level_lighting(self):
        prompt = s2_character._build_scene_voiceover_prompt(SCENE_SHOT)

        self.assertIn("光线: 午后侧光", prompt)

    def test_broll_keyframe_keeps_shot_level_lighting(self):
        prompt = s2_character._build_broll_prompt(SCENE_SHOT)

        self.assertIn("光线: 午后侧光", prompt)


class StoryboardValidationTests(unittest.TestCase):
    def test_storyboard_accepts_missing_global_style_prefix(self):
        storyboard = {
            "characters": [{
                "id": "boss",
                "name": "老板",
                "gender": "male",
                "look": "稳定角色外观",
            }],
            "hook": "测试",
            "bgm": {},
            "shots": [
                {"idx": 1, "type": "broll", "prompt": "自然工地现场"},
                {"idx": 2, "type": "broll", "prompt": "设备真实特写"},
            ],
        }
        s1_storyboard._validate(storyboard)

    def test_storyboard_prompt_does_not_request_or_inject_global_style(self):
        prompt = prompts.storyboard_prompt(
            title="测试主题",
            track="工业",
            duration=12,
        )

        self.assertNotIn('"style_prefix"', prompt)
        self.assertNotIn("风格底色:", prompt)
        self.assertNotIn("工业纪实风", prompt)
        self.assertNotIn("TTS_STRICT_V2", prompt)
        self.assertNotIn("SeedTTS", prompt)
        self.assertNotIn("OmniHuman", prompt)
        self.assertNotIn("{{voice_catalog}}", prompt)

    def test_invalid_dialogue_render_mode_is_rejected(self):
        storyboard = {
            "characters": [{
                "id": "boss",
                "name": "老板",
                "gender": "male",
                "voice_id": "male_voice",
                "look": "稳定角色外观",
            }],
            "narrator": {"voice_id": "narrator_voice"},
            "style_prefix": "写实工业纪录片风格",
            "hook": "测试",
            "bgm": {},
            "shots": [
                {
                    "idx": 1,
                    "type": "dialogue",
                    "speaker": "boss",
                    "render_mode": "portrait_video",
                    "duration": 5,
                    "scene": "办公室物件特写",
                    "action": "报价单页角轻动",
                    "voiceover": "这是第一段足够长的测试台词，用来验证非法渲染模式会被拦截。",
                },
                {
                    "idx": 2,
                    "type": "dialogue",
                    "speaker": "boss",
                    "render_mode": "scene",
                    "duration": 5,
                    "scene": "设备仪表盘特写",
                    "action": "仪表指针缓慢回落",
                    "voiceover": "这是第二段足够长的测试台词，用来满足最少两个镜头的校验条件。",
                },
            ],
        }
        with self.assertRaisesRegex(ValueError, "render_mode"):
            s1_storyboard._validate(storyboard)


class SceneVoiceoverVideoTests(unittest.TestCase):
    def test_voice_anchored_scene_binds_audio_without_old_dialogue(self):
        prompt = s3_shots._build_scene_voiceover_video_prompt(
            {**SCENE_SHOT, "duration_sec": 8},
            voice_anchored=True,
        )

        self.assertIn("音频1只参考基础音色", prompt)
        self.assertIn("不得复述音频1中的旧台词", prompt)

    def test_scene_voiceover_request_uses_audio_only_anchor(self):
        anchor = s3_shots.NativeVoiceAnchor(Path("01.wav"))

        request = s3_shots._scene_voiceover_request_kwargs(
            {"duration_sec": 6}, anchor
        )

        self.assertEqual(request["reference_audios"], ["01.wav"])
        self.assertNotIn("reference_videos", request)

    def test_completed_presenter_audio_can_anchor_scene_voiceover(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "01.mp4"
            audio = video.with_suffix(".wav")
            video.write_bytes(b"video")
            audio.write_bytes(b"audio")

            anchor = s3_shots._voice_anchor_from_completed_shot({
                "video_path": str(video),
            })

        self.assertEqual(anchor.audio_path, audio)

    def test_identifier_objects_are_reframed_without_readable_marks(self):
        prompt = s3_shots._build_scene_voiceover_video_prompt({
            "duration_sec": 6,
            "speaker": "boss_laozhou",
            "scene": "设备侧面的编号贴纸与金属铭牌",
            "camera": "从铭牌缓慢平移到车架号钢印",
            "scene_dynamics": "手套划过车架号边缘",
            "voiceover": "车架号和原始记录必须核对。",
        })

        visual_section = prompt.split("时间轴：", 1)[0]
        self.assertNotIn("车架号", visual_section)
        self.assertNotIn("编号", visual_section)
        self.assertIn("完全失焦", prompt)
        self.assertIn("不得出现任何字母或数字", prompt)

    def test_only_v25_faceless_keyframe_is_accepted(self):
        safe = {"character_in_scene_path": r"C:\job\scenes\02_scene_vo.png"}
        legacy_face = {
            "character_in_scene_path": r"C:\job\scenes\02_boss_laozhou.png"
        }

        self.assertEqual(
            s3_shots._safe_scene_voiceover_keyframe(safe),
            safe["character_in_scene_path"],
        )
        self.assertIsNone(s3_shots._safe_scene_voiceover_keyframe(legacy_face))
        self.assertIsNone(s3_shots._safe_scene_voiceover_keyframe({}))

    def test_video_prompt_places_dialogue_in_native_audio(self):
        prompt = s3_shots._build_scene_voiceover_video_prompt({
            **SCENE_SHOT,
            "duration_sec": 8,
            "sound_design": "纸张翻动与轻微设备底噪",
        })

        self.assertIn(SCENE_SHOT["scene"], prompt)
        self.assertIn(SCENE_SHOT["scene_dynamics"], prompt)
        self.assertIn("画外音", prompt)
        self.assertIn(SCENE_SHOT["voiceover"], prompt)
        self.assertIn("完整说出且只能说出", prompt)
        self.assertIn("不得出现任何可识别人脸", prompt)
        self.assertIn("不做口型同步", prompt)
        self.assertNotIn("正面对镜头说话", prompt)
        self.assertIn("创作意图：", prompt)
        self.assertIn("时间轴：", prompt)
        self.assertIn(f"{{画外音：{SCENE_SHOT['voiceover']}}}", prompt)
        self.assertIn("<环境音：纸张翻动与轻微设备底噪>", prompt)
        self.assertIn("0-1秒：", prompt)
        self.assertIn("1-7秒：", prompt)
        self.assertIn("7-8秒：", prompt)
        self.assertNotRegex(prompt, r"\d+\.\d+-\d+")

    def test_legacy_human_clauses_are_removed_from_video_prompt(self):
        prompt = s3_shots._build_scene_voiceover_video_prompt(
            LEGACY_SCENE_SHOT
        )

        self.assertIn("报价单和仪表盘铺在桌面", prompt)
        self.assertNotIn("老周", prompt)

    def test_video_prompt_keeps_shot_level_lighting(self):
        prompt = s3_shots._build_scene_voiceover_video_prompt(SCENE_SHOT)

        self.assertIn("光线: 午后侧光", prompt)

    def test_broll_video_uses_shot_level_prompt(self):
        self.assertTrue(hasattr(s3_shots, "_build_broll_video_prompt"))
        prompt = s3_shots._build_broll_video_prompt(
            {"prompt": "自然工地现场，保留真实环境色"},
        )

        self.assertIn("场景：自然工地现场，保留真实环境色", prompt)
        self.assertIn("动作：自然工地现场，保留真实环境色", prompt)
        self.assertIn("连续整数秒导演时间轴", prompt)
        self.assertIn("无对白、无人声、无旁白", prompt)
        self.assertIn("禁止任何可读文字", prompt)


if __name__ == "__main__":
    unittest.main()
