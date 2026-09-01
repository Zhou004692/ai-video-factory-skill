"""Offline perceptual media calibration for Flow Guard."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.flow_guard.perceptual_media import (  # noqa: E402
    parse_blackdetect,
    parse_freezedetect,
    run_perceptual_media_golden,
)


class FlowGuardPerceptualMediaTests(unittest.TestCase):
    def test_parses_black_and_freeze_filter_evidence(self):
        black = parse_blackdetect(
            "[blackdetect] black_start:1 black_end:1.8 black_duration:0.8"
        )
        frozen = parse_freezedetect(
            "freeze_start: 0.96\nfreeze_duration: 1.04\nfreeze_end: 2"
        )

        self.assertEqual(black, ({"start": 1.0, "end": 1.8, "duration": 0.8},))
        self.assertEqual(
            frozen,
            ({"start": 0.96, "end": 2.0, "duration": 1.04},),
        )

    def test_real_local_corpus_accepts_clean_media_and_kills_all_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_perceptual_media_golden(Path(directory))

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["paid_provider_calls"], 0)
        self.assertEqual(result["clean_findings"], [])
        self.assertEqual(result["mutation_count"], 4)
        self.assertEqual(result["killed"], 4)
        self.assertEqual(result["survived"], 0)
        detected = {
            item["mutation_id"]: set(item["observed_codes"])
            for item in result["results"]
        }
        self.assertIn("final_media_freeze_segment", detected["freeze_segment"])
        self.assertIn("final_media_black_segment", detected["black_segment"])
        self.assertIn("final_media_frame_cadence_gap", detected["timestamp_gap"])
        self.assertIn("final_media_av_duration_mismatch", detected["av_mismatch"])


if __name__ == "__main__":
    unittest.main()
