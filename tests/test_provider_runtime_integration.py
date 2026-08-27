"""Production provider clients honor deterministic zero-cost replay mode."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import provider_replay  # noqa: E402
from factory.clients import bigasr, doubao_llm, image_vision, seedance  # noqa: E402


class DoubaoReplayIntegrationTests(unittest.TestCase):
    def test_structured_generation_replays_json_without_http_or_api_key(self):
        prompt = "generate a topic brief"
        system = "return strict json"
        payload = {
            "model": doubao_llm.DEFAULT_MODEL,
            "max_tokens": 3000,
            "temperature": 0.7,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            SeedanceReplayIntegrationTests._write_manifest(root, [{
                "operation": "doubao.llm.complete_json",
                "request_hash": provider_replay.fingerprint(
                    "doubao.llm.complete_json", payload
                ),
                "responses": [{
                    "status": "succeeded",
                    "result": {"angles": [{"title": "replayed"}]},
                    "usage": {"input_tokens": 120, "output_tokens": 40},
                }],
            }])
            with mock.patch.dict(os.environ, {
                "FACTORY_PROVIDER_MODE": "replay",
                "FACTORY_REPLAY_FIXTURE_ROOT": str(root),
            }, clear=False), mock.patch.object(
                doubao_llm.httpx,
                "Client",
                side_effect=AssertionError("replay attempted live HTTP"),
            ):
                result, usage = doubao_llm.complete_json(
                    prompt, system=system, max_tokens=3000
                )

        self.assertEqual(result["angles"][0]["title"], "replayed")
        self.assertEqual(usage, {"input_tokens": 120, "output_tokens": 40})


class BigAsrReplayIntegrationTests(unittest.TestCase):
    def test_transcription_replays_speakers_and_performance_without_http(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "source.wav"
            audio.write_bytes(b"fixture-audio")
            request = bigasr.replay_request(audio)
            SeedanceReplayIntegrationTests._write_manifest(root, [{
                "operation": "volc.asr.transcribe",
                "request_hash": provider_replay.fingerprint(
                    "volc.asr.transcribe", request
                ),
                "responses": [{
                    "status": "succeeded",
                    "result": {
                        "text": "设备租赁，狗都不干！",
                        "utterances": [{
                            "text": "设备租赁，狗都不干！",
                            "start_time": 0,
                            "end_time": 2100,
                            "speaker_id": "speaker-1",
                            "emotion": "frustrated",
                            "tone": "emphatic",
                            "pace": "fast",
                            "volume": "loud",
                        }],
                    },
                }],
            }])
            with mock.patch.dict(os.environ, {
                "FACTORY_PROVIDER_MODE": "replay",
                "FACTORY_REPLAY_FIXTURE_ROOT": str(root),
            }, clear=False), mock.patch.object(
                bigasr.httpx,
                "Client",
                side_effect=AssertionError("replay attempted live HTTP"),
            ):
                transcript = bigasr.transcribe_detailed(audio)

        self.assertEqual(transcript.text, "设备租赁，狗都不干！")
        self.assertEqual(transcript.utterances[0].speaker_id, "speaker-1")
        self.assertEqual(transcript.utterances[0].emotion, "frustrated")
        self.assertEqual(transcript.utterances[0].tone, "emphatic")


class VisionReplayIntegrationTests(unittest.TestCase):
    def test_frame_analysis_replays_structured_evidence_without_http(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame = root / "frame.jpg"
            frame.write_bytes(b"fixture-frame")
            payload = image_vision.request_payload(
                "describe gesture", image_path=frame, max_tokens=1200,
                model="vision-test",
            )
            SeedanceReplayIntegrationTests._write_manifest(root, [{
                "operation": "doubao.vision.analyze_json",
                "request_hash": provider_replay.fingerprint(
                    "doubao.vision.analyze_json", payload
                ),
                "responses": [{
                    "status": "succeeded",
                    "result": {
                        "gesture": "右手抬起并向镜头挥手",
                        "facial_expression": "眉毛上扬，面带微笑",
                    },
                    "usage": {"input_tokens": 60, "output_tokens": 20},
                }],
            }])
            with mock.patch.dict(os.environ, {
                "FACTORY_PROVIDER_MODE": "replay",
                "FACTORY_REPLAY_FIXTURE_ROOT": str(root),
            }, clear=False), mock.patch.object(
                image_vision.httpx,
                "Client",
                side_effect=AssertionError("replay attempted live HTTP"),
            ):
                result, usage = image_vision.analyze_json(
                    "describe gesture", image_path=frame,
                    max_tokens=1200, model="vision-test",
                )

        self.assertIn("挥手", result["gesture"])
        self.assertEqual(usage, {"input_tokens": 60, "output_tokens": 20})


class SeedanceReplayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_generation_replays_media_and_callbacks_without_http(self):
        request = seedance._video_task_payload(
            model=seedance.DEFAULT_MODEL,
            content=[{"type": "text", "text": "equipment rescue"}],
            duration=4,
            resolution="480p",
            ratio="9:16",
            camera_fixed=False,
            last_frame=None,
            seed=None,
            motion_strength=None,
            generate_audio=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "media" / "master.mp4"
            media.parent.mkdir()
            media.write_bytes(b"replayed-video")
            self._write_manifest(root, [{
                "operation": "seedance.video.generate",
                "request_hash": provider_replay.fingerprint(
                    "seedance.video.generate", request
                ),
                "responses": [{
                    "status": "succeeded",
                    "task_id": "replay:seedance:master-1",
                    "usage": {"output_tokens": 100},
                    "media": "media/master.mp4",
                }],
            }])
            metadata = {}
            created = []
            updated = []
            with self._replay_environment(root), mock.patch.object(
                seedance.httpx,
                "AsyncClient",
                side_effect=AssertionError("replay attempted live HTTP"),
            ):
                video = await seedance.text_to_video(
                    "equipment rescue",
                    duration=4,
                    resolution="480p",
                    generate_audio=True,
                    metadata_out=metadata,
                    on_task_created=created.append,
                    on_task_updated=lambda task_id, status, data: updated.append(
                        (task_id, status, data["paid"])
                    ),
                )

        self.assertEqual(video, b"replayed-video")
        self.assertEqual(created, ["replay:seedance:master-1"])
        self.assertEqual(
            updated,
            [("replay:seedance:master-1", "completed", False)],
        )
        self.assertEqual(metadata["task_id"], "replay:seedance:master-1")
        self.assertEqual(metadata["mode"], "replay")
        self.assertFalse(metadata["paid"])

    async def test_task_inspection_replays_state_sequence_and_terminal_media(self):
        request = {"task_id": "replay:seedance:master-2"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "media" / "master-2.mp4"
            media.parent.mkdir()
            media.write_bytes(b"inspected-video")
            self._write_manifest(root, [{
                "operation": "seedance.video.inspect",
                "request_hash": provider_replay.fingerprint(
                    "seedance.video.inspect", request
                ),
                "responses": [
                    {"status": "queued", "task_id": request["task_id"]},
                    {
                        "status": "succeeded",
                        "task_id": request["task_id"],
                        "media": "media/master-2.mp4",
                    },
                ],
            }])
            with self._replay_environment(root), mock.patch.object(
                seedance.httpx,
                "AsyncClient",
                side_effect=AssertionError("replay attempted live HTTP"),
            ):
                queued = await seedance.inspect_task(request["task_id"])
                completed = await seedance.inspect_task(request["task_id"])

        self.assertEqual(queued["status"], "pending")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["video"], b"inspected-video")
        self.assertEqual(completed["cost_cents"], 0)
        self.assertFalse(completed["paid"])

    async def test_image_generation_replays_media_without_http(self):
        request = {
            "model": "doubao-seedream-5-0-260128",
            "prompt": "daylight equipment yard",
            "size": "1024x1536",
            "watermark": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "media" / "anchor.png"
            media.parent.mkdir()
            media.write_bytes(b"replayed-image")
            self._write_manifest(root, [{
                "operation": "seedance.image.generate",
                "request_hash": provider_replay.fingerprint(
                    "seedance.image.generate", request
                ),
                "responses": [{
                    "status": "succeeded",
                    "media": "media/anchor.png",
                }],
            }])
            with self._replay_environment(root), mock.patch.object(
                seedance.httpx,
                "AsyncClient",
                side_effect=AssertionError("replay attempted live HTTP"),
            ):
                image = await seedance.text_to_image(
                    "daylight equipment yard",
                    model="doubao-seedream-5-0-260128",
                )

        self.assertEqual(image, b"replayed-image")

    @staticmethod
    def _replay_environment(root: Path):
        return mock.patch.dict(os.environ, {
            "FACTORY_PROVIDER_MODE": "replay",
            "FACTORY_REPLAY_FIXTURE_ROOT": str(root),
        }, clear=False)

    @staticmethod
    def _write_manifest(root: Path, operations: list[dict]) -> None:
        (root / "manifest.json").write_text(json.dumps({
            "fixture_schema_version": 1,
            "fixture_id": "seedance-integration",
            "operations": operations,
        }), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
