"""Independent semantic oracles for Flow Guard production artifacts."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.flow_guard import invariants  # noqa: E402
from factory import full_flow_regression  # noqa: E402


class FlowGuardSemanticInvariantTests(unittest.TestCase):
    def test_semantic_evaluator_is_available(self):
        evaluator = getattr(invariants, "evaluate_production_spec", None)

        self.assertTrue(callable(evaluator))

    def test_media_artifact_evaluator_is_available(self):
        evaluator = getattr(invariants, "evaluate_media_artifact", None)

        self.assertTrue(callable(evaluator))

    def test_media_artifact_must_exist_and_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = invariants.evaluate_media_artifact(root / "missing.mp4")
            invalid_path = root / "invalid.mp4"
            invalid_path.write_text("not video", encoding="utf-8")
            invalid = invariants.evaluate_media_artifact(invalid_path)

        self.assertIn(
            "final_media_missing",
            {item.code for item in missing["findings"]},
        )
        self.assertIn(
            "final_media_decode_failed",
            {item.code for item in invalid["findings"]},
        )

    def test_valid_media_reports_duration_fps_and_streams(self):
        with tempfile.TemporaryDirectory() as directory:
            video = full_flow_regression._fixture_video(
                Path(directory) / "valid.mp4", duration_sec=1.0, index=1
            )
            result = invariants.evaluate_media_artifact(video, perceptual=True)

        self.assertEqual(result["findings"], ())
        metrics = result.get("metrics") or {}
        self.assertGreaterEqual(metrics.get("duration_sec", 0), 0.9)
        self.assertGreater(metrics.get("fps", 0), 0)
        self.assertTrue(metrics.get("has_video", False))
        self.assertTrue(metrics.get("has_audio", False))
        self.assertIn("perceptual", metrics)

    def _valid_spec(self):
        return {
            "characters": [{"id": "linxia"}],
            "masters": [{
                "idx": 1,
                "character_ids": ["linxia"],
                "speaker_ids": ["linxia"],
                "dialogue": "设备已经完成复检",
                "editorial_duration_sec": 8.0,
            }],
            "nodes": [
                {"node_id": "master:1", "node_type": "master"},
                {"node_id": "local_compose", "node_type": "local_compose"},
            ],
            "edges": [{
                "from_node_id": "master:1",
                "to_node_id": "local_compose",
                "edge_type": "media_input",
            }],
            "runtime_storyboard": {
                "masters": [{
                    "idx": 1,
                    "cast": ["linxia"],
                    "speakers": ["linxia"],
                    "speaker": "linxia",
                    "narrative_type": "dialogue",
                    "voiceover": "设备已经完成复检",
                    "duration": 8.0,
                }],
                "shots": [{
                    "idx": 1,
                    "video_master_idx": 1,
                    "audio_master_idx": 1,
                    "video_start": 0.0,
                    "audio_start": 0.0,
                    "duration": 8.0,
                    "speaker": "linxia",
                    "characters": ["linxia"],
                    "dialogue": "设备已经完成复检",
                }],
                "master_dependency_graph": {
                    "nodes": [{"idx": 1, "depends_on": []}],
                    "waves": [[1]],
                },
            },
        }

    def _codes(self, spec):
        return {
            finding.code
            for finding in invariants.evaluate_production_spec(spec)
        }

    def test_valid_spec_has_no_semantic_findings(self):
        self.assertEqual(self._codes(self._valid_spec()), set())

    def test_detects_extra_or_missing_master_projection(self):
        spec = self._valid_spec()
        spec["masters"].append({
            "idx": 2,
            "character_ids": [],
            "speaker_ids": [],
            "dialogue": "",
            "editorial_duration_sec": 3.0,
        })

        self.assertIn("master_projection_mismatch", self._codes(spec))

    def test_detects_shot_referencing_missing_master(self):
        spec = self._valid_spec()
        spec["runtime_storyboard"]["shots"][0]["video_master_idx"] = 99

        self.assertIn("shot_master_missing", self._codes(spec))

    def test_detects_empty_dialogue_voiceover(self):
        spec = self._valid_spec()
        spec["runtime_storyboard"]["masters"][0]["voiceover"] = ""

        self.assertIn("dialogue_voiceover_empty", self._codes(spec))

    def test_detects_unknown_character_and_speaker(self):
        spec = self._valid_spec()
        runtime_master = spec["runtime_storyboard"]["masters"][0]
        runtime_master["cast"] = ["ghost"]
        runtime_master["speaker"] = "ghost"
        runtime_master["speakers"] = ["ghost"]

        codes = self._codes(spec)
        self.assertIn("character_unknown", codes)
        self.assertIn("speaker_unknown", codes)

    def test_offscreen_voiceover_does_not_require_an_onscreen_character(self):
        spec = self._valid_spec()
        runtime_master = spec["runtime_storyboard"]["masters"][0]
        runtime_master["cast"] = []
        runtime_master["narrative_type"] = "voiceover"
        runtime_master["speaker"] = "narrator"
        runtime_master["speakers"] = ["narrator"]

        self.assertNotIn("speaker_unknown", self._codes(spec))

    def test_detects_shot_exceeding_master_duration(self):
        spec = self._valid_spec()
        spec["runtime_storyboard"]["shots"][0]["video_start"] = 4.0
        spec["runtime_storyboard"]["shots"][0]["duration"] = 5.0

        self.assertIn("shot_exceeds_master", self._codes(spec))

    def test_detects_dependency_referencing_missing_master(self):
        spec = self._valid_spec()
        dependency = spec["runtime_storyboard"]["master_dependency_graph"]
        dependency["nodes"][0]["depends_on"] = [2]

        self.assertIn("master_dependency_missing", self._codes(spec))

    def test_detects_graph_edge_referencing_missing_node(self):
        spec = deepcopy(self._valid_spec())
        spec["edges"][0]["from_node_id"] = "master:404"

        self.assertIn("edge_node_missing", self._codes(spec))


if __name__ == "__main__":
    unittest.main()
