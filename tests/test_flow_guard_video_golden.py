"""A local-video golden corpus protects speaker and performance semantics."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.flow_guard.video_golden import (  # noqa: E402
    run_video_performance_golden,
)


class FlowGuardVideoGoldenTests(unittest.TestCase):
    def test_video_source_preserves_speaker_gesture_emotion_and_offscreen_voice(self):
        with tempfile.TemporaryDirectory() as directory:
            report = run_video_performance_golden(Path(directory))

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["source_type"], "video_replay")
        self.assertEqual(report["paid_provider_calls"], 0)
        self.assertGreaterEqual(report["analysis_frame_count"], 2)
        self.assertIn("gesture_preserved", report["passed_guards"])
        self.assertIn("emotion_preserved", report["passed_guards"])
        self.assertIn("tone_preserved", report["passed_guards"])
        self.assertIn("speaker_cluster_preserved", report["passed_guards"])
        self.assertIn("offscreen_voice_stays_offscreen", report["passed_guards"])
        first, second = report["segments"]
        self.assertEqual(first["gesture"], "挥手")
        self.assertEqual(first["emotion"], "frustrated")
        self.assertEqual(first["speaker"], "女销售")
        self.assertEqual(second["speech_mode"], "off_screen")
        self.assertEqual(second["characters"], [])


if __name__ == "__main__":
    unittest.main()
