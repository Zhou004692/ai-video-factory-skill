"""Real provider request/response code under in-process HTTP transport replay.

These authored contracts do not claim real video or recognition quality, nor
whole-Worker/browser subprocess isolation. No fixture is built from a current
production request: expected request bodies are versioned JSON files.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from uuid import UUID
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.clients import bigasr, doubao_llm, seedance  # noqa: E402
from tests.strict_replay_support import (  # noqa: E402
    StrictReplayViolation, StrictTransportReplay,
)


FIXTURES = ROOT / "tests" / "fixtures" / "strict_transport"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class StrictTransportGuardTests(unittest.TestCase):
    def assert_boundary_rejected_before_delegate(self, owner, name):
        rejected_at_call = False
        # Install a harmless sentinel BEFORE entering the guard. Missing guards
        # can fail this test without executing DNS or a native process.
        with mock.patch.object(owner, name, return_value=None) as delegate:
            with self.assertRaises(StrictReplayViolation):
                with StrictTransportReplay(fixture("doubao_brief.json")):
                    try:
                        getattr(owner, name)("never-execute")
                    except StrictReplayViolation:
                        rejected_at_call = True
            self.assertTrue(rejected_at_call, f"{name} must be rejected at its call boundary")
            delegate.assert_not_called()

    def test_all_standard_dns_lookups_are_guarded(self):
        for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
            with self.subTest(name=name):
                self.assert_boundary_rejected_before_delegate(socket, name)

    def test_native_process_entry_points_are_guarded(self):
        names = [name for name in dir(os) if (
            name.startswith(("spawn", "exec")) or name in {"startfile", "popen"}
        ) and callable(getattr(os, name))]
        for name in names:
            with self.subTest(name=name):
                self.assert_boundary_rejected_before_delegate(os, name)

    def test_existing_sockets_cannot_send_through_guard(self):
        for name in ("send", "sendall"):
            with self.subTest(name=name):
                self.assert_boundary_rejected_before_delegate(socket.socket, name)

    def test_unknown_http_request_is_blocked_without_network(self):
        session = StrictTransportReplay(fixture("doubao_brief.json"))
        with self.assertRaisesRegex(StrictReplayViolation, "request mismatch"):
            with session:
                httpx.get("https://example.invalid/unknown")
        self.assertEqual(session.evidence()["http_requests_matched"], 0)
        self.assertEqual(session.evidence()["violations"], 1)

    def test_raw_socket_attempt_is_rejected(self):
        session = StrictTransportReplay(fixture("doubao_brief.json"))
        with self.assertRaisesRegex(StrictReplayViolation, "socket.connect"):
            with session, socket.socket() as connection:
                connection.connect(("198.51.100.10", 443))
        self.assertEqual(session.evidence()["violations"], 1)

    def test_subprocess_cannot_escape_in_process_scope(self):
        with self.assertRaisesRegex(StrictReplayViolation, "subprocess"):
            with StrictTransportReplay(fixture("doubao_brief.json")):
                subprocess.run([sys.executable, "-c", "print('must not run')"])

    def test_swallowed_mismatch_still_fails_at_context_exit(self):
        with self.assertRaisesRegex(StrictReplayViolation, "request mismatch"):
            with StrictTransportReplay(fixture("doubao_brief.json")):
                try:
                    httpx.get("https://example.invalid/unknown")
                except StrictReplayViolation:
                    pass

    def test_unused_expected_requests_fail_closed(self):
        with self.assertRaisesRegex(StrictReplayViolation, "unconsumed"):
            with StrictTransportReplay(fixture("doubao_brief.json")):
                pass

    def test_application_error_cannot_hide_unconsumed_requests(self):
        with self.assertRaises(Exception) as caught:
            with StrictTransportReplay(fixture("doubao_brief.json")):
                raise ValueError("application exited before issuing recorded request")
        self.assertIsInstance(caught.exception, StrictReplayViolation)
        self.assertIn("unconsumed", str(caught.exception))

    def test_mismatch_diagnostic_does_not_disclose_prompt(self):
        sensitive_prompt = "private-unrecorded-content"
        with self.assertRaises(StrictReplayViolation) as caught:
            with StrictTransportReplay(fixture("doubao_brief.json")):
                doubao_llm.complete_json(
                    sensitive_prompt, system="Return JSON only.",
                    max_tokens=3000, model="strict-llm-model",
                )
        self.assertNotIn(sensitive_prompt, str(caught.exception))

    def test_dns_cannot_leave_fixture_boundary(self):
        with self.assertRaisesRegex(StrictReplayViolation, "DNS"):
            with StrictTransportReplay(fixture("doubao_brief.json")):
                socket.getaddrinfo("example.invalid", 443)

    def test_environment_credentials_are_replaced_and_restored(self):
        sample = fixture("doubao_brief.json")
        with mock.patch.dict(os.environ, {"UNRELATED_SECRET": "not-a-real-key"}):
            with StrictTransportReplay(sample):
                self.assertNotIn("UNRELATED_SECRET", os.environ)
                self.assertEqual(os.environ["FACTORY_PROVIDER_MODE"], "live")
                self.assertEqual(os.environ["ARK_API_KEY"], "strict-test-key")
                doubao_llm.complete_json(
                    "保留客户画外对白，工人独自检查设备。", system="Return JSON only.",
                    max_tokens=3000, model="strict-llm-model",
                )
            self.assertEqual(os.environ["UNRELATED_SECRET"], "not-a-real-key")


class StrictLlmBoundaryTests(unittest.TestCase):
    def test_missing_authentication_header_cannot_match_fixture(self):
        original = httpx.Client.build_request

        def missing_auth(client, *args, **kwargs):
            request = original(client, *args, **kwargs)
            request.headers.pop("authorization", None)
            return request

        with self.assertRaisesRegex(StrictReplayViolation, "request mismatch"):
            with StrictTransportReplay(fixture("doubao_brief.json")), mock.patch.object(httpx.Client, "build_request", missing_auth):
                doubao_llm.complete_json(
                    "保留客户画外对白，工人独自检查设备。", system="Return JSON only.",
                    max_tokens=3000, model="strict-llm-model",
                )

    def test_real_client_extracts_response_and_usage(self):
        session = StrictTransportReplay(fixture("doubao_brief.json"))
        with session:
            data, usage = doubao_llm.complete_json(
                "保留客户画外对白，工人独自检查设备。", system="Return JSON only.",
                max_tokens=3000, model="strict-llm-model",
            )
        self.assertEqual(data, {"speech_mode": "off_screen", "characters": ["worker"]})
        self.assertEqual(usage, {"input_tokens": 120, "output_tokens": 40})
        self.assertEqual(session.evidence()["http_requests_matched"], 1)
        self.assertEqual(session.evidence()["scope"], "in_process_http_transport")

    def test_prompt_change_cannot_silently_regenerate_expected_answer(self):
        with self.assertRaisesRegex(StrictReplayViolation, "request mismatch"):
            with StrictTransportReplay(fixture("doubao_brief.json")):
                doubao_llm.complete_json(
                    "工人和客户都出镜。", system="Return JSON only.",
                    max_tokens=3000, model="strict-llm-model",
                )

    def test_real_client_rejects_truncated_json_envelope(self):
        sample = fixture("doubao_brief.json")
        sample["steps"][0]["response"]["json"]["choices"][0]["finish_reason"] = "length"
        with self.assertRaisesRegex(json.JSONDecodeError, "truncated"):
            with StrictTransportReplay(sample):
                doubao_llm.complete_json(
                    "保留客户画外对白，工人独自检查设备。", system="Return JSON only.",
                    max_tokens=3000, model="strict-llm-model",
                )


class StrictSeedanceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def generate(self, **kwargs):
        return await seedance.text_to_video(
            "工人在检查设备，客户在画外说：可以交付了。",
            model="doubao-seedance-2-5-pro", duration=4,
            resolution="480p", generate_audio=True, **kwargs,
        )

    async def test_real_submit_poll_download_and_callbacks_execute(self):
        session = StrictTransportReplay(fixture("seedance_dialogue.json"))
        created, updated, metadata = [], [], {}
        with session, mock.patch.object(seedance.asyncio, "sleep", new=mock.AsyncMock()):
            video = await self.generate(
                metadata_out=metadata, on_task_created=created.append,
                on_task_updated=lambda task_id, status, data: updated.append((task_id, status)),
            )
        self.assertEqual(video, b"synthetic-transport-bytes-not-a-quality-golden")
        self.assertEqual(created, ["strict-task-1"])
        self.assertEqual(updated, [("strict-task-1", "completed")])
        self.assertEqual(metadata["usage"], {"completion_tokens": 100})
        self.assertEqual(session.evidence()["http_requests_matched"], 4)
        self.assertEqual(session.evidence()["violations"], 0)
        self.assertNotIn("mode", metadata)  # Did not take the high-level replay branch.
        self.assertNotIn("authorization", session.requests[-1]["header_names"])

    async def test_ambiguous_submit_does_not_poll_or_resubmit(self):
        sample = fixture("seedance_dialogue.json")
        sample["steps"] = sample["steps"][:1]
        sample["steps"][0]["response"]["json"] = {}
        session = StrictTransportReplay(sample)
        with self.assertRaises(Exception) as caught:
            with session:
                await self.generate()
        self.assertNotIsInstance(caught.exception, StrictReplayViolation)
        self.assertTrue(getattr(caught.exception, "submit_result_unknown", False))
        self.assertFalse(getattr(caught.exception, "retryable", True))
        self.assertEqual(getattr(caught.exception, "pause_state", None), "PROVIDER_ACTION_REQUIRED")
        self.assertEqual(session.evidence()["http_requests_matched"], 1)

    async def test_download_failure_keeps_remote_task_for_recovery(self):
        sample = fixture("seedance_dialogue.json")
        sample["steps"][-1]["response"] = {"status_code": 503, "text": "fixture unavailable"}
        session = StrictTransportReplay(sample)
        updates = []
        with self.assertRaises(Exception) as caught:
            with session, mock.patch.object(seedance.asyncio, "sleep", new=mock.AsyncMock()):
                await self.generate(on_task_updated=lambda task_id, status, data: updates.append((task_id, status)))
        self.assertNotIsInstance(caught.exception, StrictReplayViolation)
        self.assertEqual(getattr(caught.exception, "task_id", None), "strict-task-1")
        self.assertEqual(updates, [("strict-task-1", "action_required")])
        self.assertEqual(session.evidence()["http_requests_matched"], 4)

    async def test_poll_transport_error_reuses_original_task(self):
        sample = fixture("seedance_dialogue.json")
        sample["steps"][1]["response"] = {"transport_error": "ReadTimeout"}
        try:
            session = StrictTransportReplay(sample)
        except ValueError as error:
            self.fail(f"Recorded transport failures must be replayable: {error}")
        created = []
        with session, mock.patch.object(seedance.asyncio, "sleep", new=mock.AsyncMock()):
            await self.generate(on_task_created=created.append)
        self.assertEqual(created, ["strict-task-1"])
        self.assertEqual([item["method"] for item in session.requests], ["POST", "GET", "GET", "GET"])

    async def test_private_download_redirect_is_rejected_by_real_validator(self):
        sample = fixture("seedance_dialogue.json")
        sample["steps"][-1]["response"] = {
            "status_code": 302, "headers": {"location": "https://127.0.0.1/private"}, "text": "",
        }
        with self.assertRaisesRegex(ValueError, "public HTTPS"):
            with StrictTransportReplay(sample), mock.patch.object(seedance.asyncio, "sleep", new=mock.AsyncMock()):
                await self.generate()

    async def test_insecure_download_redirect_is_rejected_by_real_validator(self):
        sample = fixture("seedance_dialogue.json")
        sample["steps"][-1]["response"] = {
            "status_code": 302, "headers": {"location": "http://93.184.216.34/master"}, "text": "",
        }
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            with StrictTransportReplay(sample), mock.patch.object(seedance.asyncio, "sleep", new=mock.AsyncMock()):
                await self.generate()


class StrictAsrBoundaryTests(unittest.TestCase):
    def setUp(self):
        # Seed only external entropy, not request construction or handlers.
        entropy = mock.patch.object(bigasr.uuid, "uuid4", return_value=UUID("11111111-1111-4111-8111-111111111111"))
        self.uuid_source = entropy.start()
        self.addCleanup(entropy.stop)

    def test_missing_protocol_headers_cannot_match_fixture(self):
        original = httpx.Client.build_request
        for header in ("x-api-key", "x-api-resource-id", "x-api-request-id", "x-api-sequence"):
            with self.subTest(header=header), tempfile.TemporaryDirectory() as directory:
                audio = Path(directory) / "source.wav"
                audio.write_bytes(b"strict-audio")

                def missing_header(client, *args, **kwargs):
                    request = original(client, *args, **kwargs)
                    request.headers.pop(header, None)
                    return request

                with self.assertRaisesRegex(StrictReplayViolation, "request mismatch"):
                    with StrictTransportReplay(fixture("asr_performance.json")), mock.patch.object(httpx.Client, "build_request", missing_header):
                        bigasr.transcribe_detailed(audio, poll_interval=0)

    def test_query_for_different_asr_request_cannot_match_fixture(self):
        original = httpx.Client.build_request

        def changed_identity(client, *args, **kwargs):
            request = original(client, *args, **kwargs)
            if request.url.path.endswith("/query"):
                request.headers["x-api-request-id"] = "22222222-2222-4222-8222-222222222222"
            return request

        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "source.wav"
            audio.write_bytes(b"strict-audio")
            with self.assertRaisesRegex(StrictReplayViolation, "request mismatch"):
                with StrictTransportReplay(fixture("asr_performance.json")), mock.patch.object(httpx.Client, "build_request", changed_identity):
                    bigasr.transcribe_detailed(audio, poll_interval=0)

    def test_real_submit_poll_and_performance_parser_execute(self):
        sample = fixture("asr_performance.json")
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "source.wav"
            audio.write_bytes(b"strict-audio")
            session = StrictTransportReplay(sample)
            with session:
                transcript = bigasr.transcribe_detailed(audio, poll_interval=0)
        self.assertEqual(transcript.text, "可以交付了。")
        self.assertEqual(transcript.utterances[0].speaker_id, "voice-1")
        self.assertEqual(transcript.utterances[0].emotion, "calm")
        self.assertEqual(transcript.utterances[0].tone, "firm")
        self.assertEqual(transcript.speech_start_ms, 0)
        self.assertEqual(transcript.speech_end_ms, 1200)
        self.assertEqual(session.evidence()["http_requests_matched"], 3)
        self.uuid_source.assert_called_once()  # Query must reuse the submit ID.
        self.assertFalse(session.evidence()["content_quality_verified"])

    def test_asr_provider_rejection_does_not_become_empty_success(self):
        sample = fixture("asr_performance.json")
        sample["steps"] = sample["steps"][:1]
        sample["steps"][0]["response"]["headers"]["X-Api-Status-Code"] = "45000001"
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "source.wav"
            audio.write_bytes(b"strict-audio")
            with self.assertRaisesRegex(RuntimeError, "submit failed"):
                with StrictTransportReplay(sample):
                    bigasr.transcribe_detailed(audio, poll_interval=0)


if __name__ == "__main__":
    unittest.main()
