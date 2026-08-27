"""Completeness and media checks for zero-cost full-flow scenarios."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "full_flow"


class FullFlowFixtureTests(unittest.TestCase):
    SCENARIOS = (
        "text_three_person_multiscene",
        "local_video_performance",
        "broll_before_cta",
        "restart_and_reconcile",
    )

    def test_all_representative_manifests_are_complete_and_sanitized(self):
        for scenario in self.SCENARIOS:
            with self.subTest(scenario=scenario):
                path = FIXTURE_ROOT / scenario / "manifest.json"
                manifest = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(manifest["fixture_schema_version"], 1)
                self.assertEqual(manifest["scenario_id"], scenario)
                self.assertTrue(manifest["source"]["content"])
                self.assertEqual(len(manifest["brief_result"]["angles"]), 3)
                self.assertTrue(manifest["script_result"]["variants"])
                self.assertTrue(manifest["masters"])
                self.assertTrue(manifest["provider_sequences"])
                self.assertIn("director_timeline", manifest["expected"])
                self.assertIn("final_duration_sec", manifest["expected"])
                self.assertIn("recovery_actions", manifest["expected"])
                encoded = json.dumps(manifest, ensure_ascii=False).casefold()
                for forbidden in (
                    "api_key", "authorization", "secret_access_key",
                    "x-api-key", "https://ark.cn-beijing",
                ):
                    self.assertNotIn(forbidden, encoded)

    def test_synthetic_media_factory_creates_probeable_local_assets(self):
        from tests.fixtures.full_flow.media_factory import build_media_set

        with tempfile.TemporaryDirectory() as directory:
            media = build_media_set(Path(directory), duration_sec=2)
            self.assertEqual(set(media), {"video", "audio", "image"})
            for path in media.values():
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries",
                    "format=duration", "-of", "default=nw=1:nk=1",
                    str(media["video"]),
                ],
                check=True, capture_output=True, text=True,
            )
            self.assertAlmostEqual(float(probe.stdout.strip()), 2.0, delta=0.2)


if __name__ == "__main__":
    unittest.main()
