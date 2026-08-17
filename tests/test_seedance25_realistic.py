import json
import sys
import unittest
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.experiments.seedance25_realistic import (  # noqa: E402
    build_payload,
    completed_result,
    run_case,
)


class Seedance25RealisticExperimentTests(unittest.TestCase):
    def test_payload_contains_realistic_reference_and_native_audio_controls(self):
        import tempfile

        case = {
            "prompt": "测试真人对白",
            "duration": 4,
            "resolution": "480p",
            "ratio": "9:16",
            "first_frame": "portrait.png",
            "motion_strength": 0.35,
        }

        with tempfile.TemporaryDirectory() as temp:
            base_dir = Path(temp)
            (base_dir / "portrait.png").write_bytes(b"synthetic portrait")
            payload = build_payload(case, base_dir=base_dir)

        self.assertEqual(payload["model"], "doubao-seedance-2-5-260628")
        self.assertTrue(payload["generate_audio"])
        self.assertEqual(payload["resolution"], "480p")
        self.assertEqual(payload["duration"], 4)
        self.assertEqual(payload["motion_strength"], 0.35)
        self.assertNotIn("ratio", payload)
        self.assertEqual(payload["content"][0], {"type": "text", "text": "测试真人对白"})
        self.assertEqual(payload["content"][1]["role"], "first_frame")
        self.assertTrue(payload["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_completed_result_is_reused_only_when_output_exists(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "smoke.mp4"
            result_path = root / "generation_results.json"
            result_path.write_text(
                json.dumps({"smoke": {"terminal": "succeeded", "output_file": output.name}}),
                encoding="utf-8",
            )
            self.assertIsNone(completed_result("smoke", result_path, root))
            output.write_bytes(b"video")
            self.assertEqual(completed_result("smoke", result_path, root)["terminal"], "succeeded")

    def test_payload_supports_video_and_audio_reference_modalities(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            base_dir = Path(temp)
            (base_dir / "anchor.mp4").write_bytes(b"video")
            (base_dir / "anchor.wav").write_bytes(b"audio")
            payload = build_payload(
                {
                    "prompt": "说新台词",
                    "duration": 6,
                    "resolution": "720p",
                    "reference_video": "anchor.mp4",
                    "reference_audio": "anchor.wav",
                },
                base_dir=base_dir,
            )

        self.assertEqual([part.get("role") for part in payload["content"]], [None, "reference_video", "reference_audio"])
        self.assertTrue(payload["content"][1]["video_url"]["url"].startswith("data:video/mp4;base64,"))
        self.assertTrue(payload["content"][2]["audio_url"]["url"].startswith("data:audio/wav;base64,"))

    def test_payload_preserves_public_reference_video_url(self):
        payload = build_payload(
            {
                "prompt": "说新台词",
                "duration": 6,
                "resolution": "720p",
                "reference_video": "https://media.example.com/anchor.mp4?signature=abc",
            },
            base_dir=Path("C:/unused"),
        )

        self.assertEqual(
            payload["content"][1]["video_url"]["url"],
            "https://media.example.com/anchor.mp4?signature=abc",
        )


class Seedance25RealisticRunCaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_case_polls_downloads_and_records_provider_usage(self):
        import tempfile

        poll_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal poll_count
            if request.method == "POST":
                return httpx.Response(200, json={"id": "task-1"})
            if request.url.host == "media.example.com":
                return httpx.Response(200, content=b"mp4-bytes")
            poll_count += 1
            if poll_count == 1:
                return httpx.Response(200, json={"status": "running"})
            return httpx.Response(
                200,
                json={
                    "status": "succeeded",
                    "content": {"video_url": "https://media.example.com/result.mp4"},
                    "usage": {"total_tokens": 1234},
                },
            )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "portrait.png").write_bytes(b"portrait")
            case = {
                "prompt": "测试",
                "duration": 4,
                "resolution": "480p",
                "first_frame": "portrait.png",
            }
            result = await run_case(
                "smoke",
                case,
                base_dir=root,
                output_dir=root,
                result_path=root / "generation_results.json",
                api_key="test-key",
                transport=httpx.MockTransport(handler),
                poll_seconds=0,
            )

            self.assertEqual(result["terminal"], "succeeded")
            self.assertEqual(result["task_id"], "task-1")
            self.assertEqual(result["usage"]["total_tokens"], 1234)
            self.assertEqual((root / "smoke.mp4").read_bytes(), b"mp4-bytes")
            saved = json.loads((root / "generation_results.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["smoke"]["terminal"], "succeeded")

    async def test_run_case_records_provider_rejection_body(self):
        import tempfile

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error": {"code": "InputImageSensitiveContentDetected.PrivacyInformation"}},
            )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            case = {"prompt": "测试", "duration": 4, "resolution": "480p"}
            result = await run_case(
                "blocked",
                case,
                base_dir=root,
                output_dir=root,
                result_path=root / "generation_results.json",
                api_key="test-key",
                transport=httpx.MockTransport(handler),
                poll_seconds=0,
            )

            self.assertEqual(result["terminal"], "failed")
            self.assertEqual(result["http_status"], 400)
            self.assertIn("PrivacyInformation", result["provider_error"])
            saved = json.loads((root / "generation_results.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["blocked"]["terminal"], "failed")


if __name__ == "__main__":
    unittest.main()
