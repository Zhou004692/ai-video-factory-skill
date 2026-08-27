"""Regression tests for local-video speaker and performance reconstruction."""
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


from factory import creative_ir, media_sources, script_drafts, source_analysis  # noqa: E402
from factory.clients import bigasr  # noqa: E402


class SourcePerformanceIRTests(unittest.TestCase):
    def test_frame_sampling_includes_speech_turn_boundaries(self):
        transcription = bigasr.Transcription(
            "先停机。收到。",
            utterances=(
                bigasr.TimedUtterance("先停机。", 1200, 2800, "voice-a"),
                bigasr.TimedUtterance("收到。", 5200, 6800, "voice-b"),
            ),
        )

        timestamps = media_sources._performance_timestamps(
            8.0, 4.0, transcription, max_frames=16
        )

        self.assertEqual(timestamps[0], 0.0)
        self.assertIn(2.0, timestamps)
        self.assertIn(6.0, timestamps)
        self.assertLessEqual(len(timestamps), 16)

    def test_visual_analysis_keeps_frames_nearest_speech_turns(self):
        frames = [
            {"index": index + 1, "timestamp_sec": index * 4.0,
             "path": f"frame-{index + 1}.jpg"}
            for index in range(20)
        ]
        utterances = [
            {"start_ms": 5000, "end_ms": 7000},
            {"start_ms": 52000, "end_ms": 56000},
        ]

        selected = source_analysis._select_analysis_frames(
            frames, utterances=utterances, max_frames=5
        )

        timestamps = [item["timestamp_sec"] for item in selected]
        self.assertEqual(timestamps[0], 0.0)
        self.assertEqual(timestamps[-1], 76.0)
        self.assertIn(4.0, timestamps)
        self.assertIn(52.0, timestamps)

    def test_asr_preserves_speaker_cluster_and_voice_performance(self):
        transcription = bigasr.parse_transcription({
            "result": {
                "text": "先停机。收到。",
                "utterances": [
                    {
                        "text": "先停机。", "start_time": 0, "end_time": 900,
                        "speaker_id": "voice-a", "speaker_confidence": 0.96,
                        "emotion": "紧张", "tone": "短促命令",
                    },
                    {
                        "text": "收到。", "start_time": 950, "end_time": 1500,
                        "speaker_id": "voice-b", "speaker_confidence": 0.91,
                        "emotion": "坚定", "tone": "快速回应",
                    },
                ],
            }
        })

        first, second = transcription.utterances
        self.assertEqual(first.speaker_id, "voice-a")
        self.assertEqual(second.speaker_id, "voice-b")
        self.assertEqual(first.emotion, "紧张")
        self.assertEqual(first.tone, "短促命令")
        self.assertEqual(first.speaker_confidence, 0.96)
        self.assertEqual(
            media_sources._utterance_metadata(transcription)[0]["speaker_id"],
            "voice-a",
        )

    def test_asr_accepts_official_additions_speaker_field(self):
        transcription = bigasr.parse_transcription({
            "result": {
                "text": "先停机。",
                "utterances": [{
                    "text": "先停机。", "start_time": 0, "end_time": 900,
                    "additions": {
                        "speaker": "2", "emotion": "紧张",
                        "speech_rate": "快", "volume": "低",
                    },
                }],
            }
        })

        utterance = transcription.utterances[0]
        self.assertEqual(utterance.speaker_id, "2")
        self.assertEqual(utterance.emotion, "紧张")
        self.assertEqual(utterance.pace, "快")
        self.assertEqual(utterance.volume, "低")

    def test_frame_observation_preserves_visible_person_performance(self):
        observation = source_analysis.normalize_frame_observation(
            {
                "visual": "两名维修人员对话",
                "people_count": 2,
                "people": [
                    {
                        "track_id": "person-a", "appearance": "黄色安全帽",
                        "mouth_activity": "speaking", "facial_expression": "焦急",
                        "gaze": "看向同伴", "gesture": "指向配电柜",
                        "body_action": "快步靠近",
                    },
                    {
                        "track_id": "person-b", "appearance": "白色安全帽",
                        "mouth_activity": "silent", "facial_expression": "专注",
                    },
                ],
                "confidence": 0.9,
            },
            frame_index=3,
            timestamp_sec=2.4,
            path="frame-003.jpg",
        )

        self.assertEqual(observation["people"][0]["track_id"], "person-a")
        self.assertEqual(observation["people"][0]["mouth_activity"], "speaking")
        self.assertEqual(observation["people"][0]["gesture"], "指向配电柜")

    def test_structure_report_marks_unresolved_speaker_for_review(self):
        report = source_analysis.normalize_structure_report(
            {
                "summary": "双人抢修对话",
                "character_tracks": [
                    {"id": "person-a", "label": "林夏"},
                    {"id": "person-b", "label": "周工"},
                ],
                "segments": [{
                    "role": "conflict", "start_sec": 0, "end_sec": 3,
                    "characters": ["林夏", "周工"],
                    "visible_character_ids": ["person-a", "person-b"],
                    "voice_cluster_id": "voice-a", "speaker": "",
                    "speaker_track_id": "", "speaker_confidence": 0.42,
                    "dialogue": "现在必须断电。",
                    "emotion": "紧张", "tone": "压低声音、短促",
                    "pace": "快", "volume": "低",
                    "facial_expression": "眉头紧锁", "gaze": "看向配电柜",
                    "gesture": "抬手制止", "body_action": "向前半步",
                }],
            },
            duration_sec=3,
            observations=[],
        )

        segment = report["segments"][0]
        self.assertEqual(report["performance_ir_version"], 2)
        self.assertEqual(report["character_tracks"][0]["id"], "person-a")
        self.assertEqual(segment["voice_cluster_id"], "voice-a")
        self.assertTrue(segment["speaker_review_required"])
        self.assertEqual(segment["emotion"], "紧张")
        self.assertEqual(segment["gesture"], "抬手制止")

    def test_reconstruction_never_guesses_first_visible_character(self):
        source = {
            "source_id": "SRC-video", "source_type": "video", "title": "抢修",
            "structure_analysis": {
                "summary": "双人对话",
                "segments": [{
                    "role": "conflict", "start_sec": 0, "end_sec": 3,
                    "characters": ["林夏", "周工"],
                    "visible_character_ids": ["person-a", "person-b"],
                    "voice_cluster_id": "voice-a", "speaker": "",
                    "speaker_confidence": 0.4, "speaker_review_required": True,
                    "dialogue": "现在必须断电。", "scene": "暴雨中的配电间",
                    "action": "其中一人抬手制止", "emotion": "紧张",
                    "tone": "短促命令", "gesture": "抬手制止",
                    "camera": "手持中景推进",
                }],
            },
        }

        reconstruction = script_drafts._reconstruction_variant([source])
        beat = reconstruction["beats"][0]
        self.assertNotEqual(beat["speaker"], "林夏")
        self.assertNotEqual(beat["speaker"], "周工")
        self.assertEqual(beat["speaker"], "unresolved_voice_voice-a")
        self.assertTrue(beat["speaker_review_required"])
        self.assertEqual(beat["emotion"], "紧张")
        self.assertEqual(beat["tone"], "短促命令")
        self.assertEqual(beat["gesture"], "抬手制止")

    def test_script_normalization_and_creative_ir_keep_performance_direction(self):
        normalized = script_drafts._normalize_variant(
            {
                "title": "抢修", "creative_thesis": "双人协作",
                "beats": [{
                    "beat": "conflict", "speaker": "lin_xia",
                    "dialogue": "现在必须断电。", "duration_sec": 3,
                    "visual_intent": "林夏抬手制止周工继续操作",
                    "source_ids": ["SRC-video"],
                    "emotion": "紧张", "tone": "短促命令", "pace": "快",
                    "facial_expression": "眉头紧锁", "gaze": "看向周工",
                    "gesture": "抬手制止", "body_action": "向前半步",
                    "speaker_confidence": 0.98,
                    "speaker_review_required": False,
                }],
            },
            allowed_source_ids={"SRC-video"},
        )
        beat = normalized["beats"][0]
        self.assertEqual(beat["emotion"], "紧张")
        self.assertEqual(beat["tone"], "短促命令")
        self.assertEqual(beat["gesture"], "抬手制止")

        creative = creative_ir.from_storyboard(normalized)
        self.assertEqual(creative.beats[0].emotion, "紧张")
        self.assertEqual(creative.beats[0].extensions["tone"], "短促命令")
        self.assertEqual(creative.beats[0].extensions["gesture"], "抬手制止")

        storyboard = script_drafts._storyboard(normalized)
        self.assertEqual(
            storyboard["masters"][0]["performance_beats"][0]["emotion"],
            "紧张",
        )
        self.assertEqual(storyboard["shots"][0]["tone"], "短促命令")
        self.assertEqual(storyboard["shots"][0]["gesture"], "抬手制止")


if __name__ == "__main__":
    unittest.main()
