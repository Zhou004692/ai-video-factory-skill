import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = (
    ROOT
    / "data"
    / "seedance25_lipsync_validation"
    / "production_benchmarks"
    / "manifest.json"
)


@unittest.skipUnless(
    MANIFEST.is_file(),
    "local production benchmark assets are not present in this checkout",
)
class ProductionBenchmarkManifestTests(unittest.TestCase):
    def test_accepted_media_exists_and_duration_matches_ffprobe(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for name, benchmark in manifest["benchmarks"].items():
            with self.subTest(benchmark=name):
                media_path = (MANIFEST.parent / benchmark["file"]).resolve()
                self.assertTrue(media_path.is_file(), media_path)

                probe = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "json",
                        str(media_path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                actual = float(json.loads(probe.stdout)["format"]["duration"])
                self.assertAlmostEqual(
                    actual,
                    float(benchmark["duration_seconds"]),
                    delta=0.01,
                )

                if contact_sheet := benchmark.get("contact_sheet"):
                    sheet_path = (MANIFEST.parent / contact_sheet).resolve()
                    self.assertTrue(sheet_path.is_file(), sheet_path)


if __name__ == "__main__":
    unittest.main()
