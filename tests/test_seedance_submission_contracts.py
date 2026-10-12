"""Offline submission contracts: validation must precede every paid POST."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from factory import character_assets, db, reference_attention
from factory.clients import seedance, video_qa
from factory.steps import s3_shots
from factory.video_routing import VideoRoute

MODEL = "doubao-seedance-2-5-260628"
LEGACY = "doubao-seedance-2-0-260128"


def payload(roles=(), **overrides):
    kwargs = dict(model=MODEL, content=[{"role": role} for role in roles],
                  duration=8, resolution="720p", ratio="9:16",
                  camera_fixed=False, last_frame=None, seed=None,
                  motion_strength=0.4, generate_audio=False)
    kwargs.update(overrides)
    return seedance._video_task_payload(**kwargs)


def media(kind, seconds, index=0, **kwargs):
    return reference_attention.candidate(
        f"https://example.invalid/{kind}-{index}?signature=private",
        kind=kind, duty="voice_continuity" if kind == "audio" else "scene_continuity",
        duration_sec=seconds, **kwargs)


class PayloadContractTests(unittest.TestCase):
    def test_scene_voiceover_keeps_implicit_composition_image_with_audio(self):
        request = {"first_frame": "https://example.invalid/composition.png",
                   "reference_audios": ["https://example.invalid/voice.wav"]}
        prompt = s3_shots._scene_voiceover_reference_mode(request, "scene prompt")
        self.assertIsNone(request["first_frame"])
        self.assertEqual(request["references"], ["https://example.invalid/composition.png"])
        self.assertEqual(request["reference_audios"], ["https://example.invalid/voice.wav"])
        self.assertIn("图片1", prompt)
        self.assertIn("构图参考", prompt)
        self.assertIn("非精确锁帧", prompt)
        self.assertEqual(request["reference_manifest"][0]["duty"], "scene_continuity")

    def test_scene_voiceover_without_audio_preserves_strict_frame_mode(self):
        request = {"first_frame": "https://example.invalid/frame.png"}
        self.assertEqual(s3_shots._scene_voiceover_reference_mode(request, "original"), "original")
        self.assertEqual(request["first_frame"], "https://example.invalid/frame.png")
        self.assertNotIn("references", request)

    def test_explicit_last_frame_is_not_silently_converted(self):
        request = {"first_frame": "https://example.invalid/frame.png", "last_frame": "end.png",
                   "reference_audios": ["https://example.invalid/voice.wav"]}
        s3_shots._scene_voiceover_reference_mode(request, "original")
        self.assertEqual(request["first_frame"], "https://example.invalid/frame.png")
        self.assertEqual(request["last_frame"], "end.png")

    def test_strict_frames_cannot_mix_with_any_reference_role(self):
        for frame in ("first_frame", "last_frame"):
            for reference in ("reference_image", "reference_video", "reference_audio"):
                with self.subTest(frame=frame, reference=reference):
                    with self.assertRaisesRegex(seedance.SeedanceParameterError, "mutually exclusive"):
                        payload([frame, reference])

    def test_edit_requires_video_even_when_audio_present(self):
        with self.assertRaisesRegex(seedance.SeedanceParameterError, "require reference_video"):
            payload(["reference_audio"], task_intent="edit", duration=-1, ratio="adaptive")

    def test_legal_neighbor_payloads_preserve_aspect_and_supported_controls(self):
        for roles in ([], ["reference_image"], ["reference_video", "reference_audio"]):
            result = payload(roles)
            self.assertEqual(result["ratio"], "9:16")
            self.assertNotIn("motion_strength", result)
        frames = payload(["first_frame", "last_frame"])
        self.assertNotIn("ratio", frames)
        self.assertEqual(payload(["reference_video"], task_intent="edit", duration=-1,
                                 ratio="adaptive")["duration"], -1)
        legacy = payload(["first_frame", "reference_image"], model=LEGACY)
        self.assertEqual(legacy["motion_strength"], 0.4)

    def test_edit_still_requires_adaptive_ratio_and_minus_one_duration(self):
        for change, message in (({"ratio": "9:16"}, "adaptive"),
                                ({"duration": 8}, "duration=-1")):
            kwargs = dict(task_intent="edit", ratio="adaptive", duration=-1)
            kwargs.update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                payload(["reference_video"], **kwargs)


class DurationAttentionTests(unittest.TestCase):
    def test_aggregate_video_limit_rejects_three_twelve_second_clips(self):
        with self.assertRaisesRegex(ValueError, "video.*36.*30"):
            reference_attention.select_manifest([media("video", 12, i) for i in range(3)],
                                                max_video_references=3)

    def test_aggregate_audio_limit(self):
        with self.assertRaisesRegex(ValueError, "audio.*32.*30"):
            reference_attention.select_manifest([media("audio", 16, i) for i in range(2)],
                                                max_video_references=3)

    def test_four_short_references_and_duplicates_are_retained(self):
        items = [media("video", 7.5, i) for i in range(4)]
        result = reference_attention.select_manifest(items + [items[0]], max_video_references=4)
        self.assertEqual(len(result), 4)
        self.assertEqual(sum(item["duration_sec"] for item in result), 30)

    def test_unknown_duration_is_not_zero_evidence(self):
        self.assertIsNone(media("video", None)["duration_sec"])
        self.assertIsNone(media("audio", 0)["duration_sec"])

    def test_duplicate_cannot_hide_required_flag_or_known_duration(self):
        first = media("video", None, required=False)
        later = media("video", 31)
        with self.assertRaisesRegex(ValueError, "video.*31.*30"):
            reference_attention.select_manifest([first, later], max_video_references=3)

    def test_same_uri_preserves_required_video_and_audio_roles(self):
        video = media("video", 5)
        audio = reference_attention.candidate(video["uri"], kind="audio", duty="voice_continuity", duration_sec=5)
        manifest = reference_attention.select_manifest([video, audio], max_video_references=3)
        self.assertEqual([item["kind"] for item in manifest], ["video", "audio"])
        self.assertTrue(all(item["required"] for item in manifest))


class SubmissionPreflightTests(unittest.IsolatedAsyncioTestCase):
    def _overlong_builder(self, kind):
        members = [{"id": str(i), "generation_mode": "ai_generated"} for i in range(3)]
        anchors = {character_assets.character_identity_key(member): s3_shots.DialogueAnchor(
            video_url=f"https://example.invalid/old-{member['id']}.mp4", duration_sec=12)
            for member in members}
        if kind == "dialogue":
            return s3_shots._dialogue_request_kwargs(
                {"duration_sec": 8}, None, character=members[0], cast_characters=members[1:], identity_anchors=anchors)
        if kind == "broll":
            return s3_shots._broll_request_kwargs({"duration_sec": 8}, cast_characters=members, identity_anchors=anchors)
        return s3_shots._extend_request_kwargs({"duration_sec": 36, "extend_duration_sec": 8,
                                                "repair_source_path": "https://example.invalid/old.mp4"})

    def test_production_builders_preserve_old_paid_overlong_materials_until_ledger_check(self):
        for kind in ("dialogue", "broll", "extend"):
            with self.subTest(kind=kind):
                request = self._overlong_builder(kind)
                self.assertEqual(sum(item["duration_sec"] for item in request["reference_manifest"]), 36)

    async def test_matching_paid_36_second_ledger_recovers_with_real_get_and_no_post(self):
        database = self._budget_database(1)
        database.execute("UPDATE jobs SET production_spec_hash='old-spec' WHERE job_id='J-REFERENCE-BUDGET'")
        database.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,usage_json,cost_cents) "
                         "VALUES('old-paid-task','J-REFERENCE-BUDGET',1,'seedance','generate','{}',0)")
        database.execute("INSERT INTO production_nodes(job_id,node_id,node_type,state,spec_hash,output_json,idempotency_key,provider_task_id) "
                         "VALUES('J-REFERENCE-BUDGET','master:1','master','RUNNING','old-spec',?, 'old-master-key','old-paid-task')",
                         (json.dumps({"resume_provider_task_id": "old-paid-task"}),))
        database.commit()
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"status": "completed", "content": {"video_url": "https://example.invalid/old.mp4"}})
        real_client = httpx.AsyncClient
        with tempfile.TemporaryDirectory() as temp, patch.object(seedance, "_headers", return_value={}), patch.object(
            seedance.httpx, "AsyncClient", side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler))), patch.object(
            seedance.provider_downloads, "download_bytes", new=AsyncMock(return_value=(b"old-paid-video", "https://example.invalid/old.mp4"))), patch.object(
            seedance, "text_to_video", new=AsyncMock()) as submit, patch.object(
            s3_shots.tos, "upload", return_value="https://example.invalid/recovered.mp4"):
            result = await s3_shots._generate_seedance_video(
                prompt="old generation", route=VideoRoute(MODEL, "seedance25", False, False),
                request_kwargs=self._overlong_builder("broll"), out_path=Path(temp) / "out.mp4", key_hint="offline",
                job_id="J-REFERENCE-BUDGET", shot_idx=1, database=database)
        self.assertEqual(result.video, b"old-paid-video")
        self.assertEqual([request.method for request in requests], ["GET"])
        self.assertTrue(requests[0].url.path.endswith("/tasks/old-paid-task"))
        submit.assert_not_awaited()

    async def test_unmatched_or_forged_resume_does_not_bypass_new_submission_preflight(self):
        database = self._budget_database(5000)
        with tempfile.TemporaryDirectory() as temp, patch.object(seedance, "text_to_video", new=AsyncMock()) as submit, patch.object(
            seedance, "inspect_task", new=AsyncMock()) as inspect:
            for kind in ("dialogue", "broll", "extend"):
                with self.subTest(kind=kind):
                    request = self._overlong_builder(kind)
                    request["resume_provider_task_id"] = "forged-paid-task"
                    with self.assertRaisesRegex(seedance.SeedanceParameterError, "video.*36.*30"):
                        await s3_shots._generate_seedance_video(
                            prompt="new generation", route=VideoRoute(MODEL, "seedance25", False, False),
                            request_kwargs=request, out_path=Path(temp) / "out.mp4", key_hint="offline",
                            job_id="J-REFERENCE-BUDGET", shot_idx=1, database=database)
        submit.assert_not_awaited()
        inspect.assert_not_awaited()

    async def test_old_model_required_unknown_duration_has_clear_preflight_diagnostic(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(seedance, "text_to_video", new=AsyncMock()) as submit:
            with self.assertRaisesRegex(seedance.SeedanceParameterError, "duration unavailable"):
                await s3_shots._generate_seedance_video(
                    prompt="test", route=VideoRoute(LEGACY, "legacy", False, False),
                    request_kwargs={"duration": 8, "reference_videos": ["https://example.invalid/unknown.mp4"]},
                    out_path=Path(temp) / "out.mp4", key_hint="offline")
            submit.assert_not_awaited()

    def _budget_database(self, budget_cents):
        database = sqlite3.connect(":memory:")
        self.addCleanup(database.close)
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        storyboard = json.dumps({"production_settings": {"budget_cents": budget_cents}})
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-REFERENCE-BUDGET','T-1','offline budget','PRODUCTION_RUNNING',?,0)",
            (storyboard,))
        database.execute("INSERT INTO shots(job_id,idx,state,cost_cents) VALUES('J-REFERENCE-BUDGET',1,'GENERATING',0)")
        database.commit()
        return database

    async def test_canonical_reference_duration_blocks_underfunded_production_before_client(self):
        database = self._budget_database(2500)
        item = media("video", 20)
        request = {"duration": 8, "resolution": "720p", "reference_videos": [item["uri"]],
                   "reference_manifest": [item]}
        with tempfile.TemporaryDirectory() as temp, patch.object(s3_shots, "conn", return_value=database), patch.object(
            seedance, "text_to_video", new=AsyncMock(return_value=b"offline")) as submit, patch.object(
            s3_shots.tos, "upload", return_value="https://example.invalid/offline.mp4"):
            with self.assertRaisesRegex(s3_shots.SeedanceBudgetError, "next_estimate=4284"):
                await s3_shots._generate_seedance_video(
                    prompt="test", route=VideoRoute(MODEL, "seedance25", False, False),
                    request_kwargs=request, out_path=Path(temp) / "out.mp4", key_hint="offline",
                    job_id="J-REFERENCE-BUDGET", shot_idx=1, database=database)
            submit.assert_not_awaited()

    async def test_budget_reservation_client_and_audit_share_canonical_reference_durations(self):
        database = self._budget_database(5000)
        video, audio = media("video", 20), media("audio", 3)
        request = {"duration": 8, "resolution": "720p", "reference_videos": [video["uri"]],
                   "reference_audios": [audio["uri"]], "reference_manifest": [video, audio],
                   "reference_video_duration_sec": 1, "reference_audio_duration_sec": 1}
        async def generated(**kwargs):
            kwargs["on_task_created"]("offline-budget-task")
            kwargs["metadata_out"].update(task_id="offline-budget-task", usage={"cost_cents": 1000})
            return b"offline-video"
        original_reserve = s3_shots.provider_tasks.reserve_paid_submit
        original_budget = s3_shots._assert_paid_call_within_job_budget
        with tempfile.TemporaryDirectory() as temp, patch.object(s3_shots, "conn", return_value=database), patch.object(
            seedance, "text_to_video", new=AsyncMock(side_effect=generated)) as submit, patch.object(
            s3_shots.tos, "upload", return_value="https://example.invalid/offline.mp4"), patch.object(
            s3_shots.provider_tasks, "reserve_paid_submit", wraps=original_reserve) as reserve, patch.object(
            s3_shots, "_assert_paid_call_within_job_budget", wraps=original_budget) as budget:
            result = await s3_shots._generate_seedance_video(
                prompt="test", route=VideoRoute(MODEL, "seedance25", False, False),
                request_kwargs=request, out_path=Path(temp) / "out.mp4", key_hint="offline",
                job_id="J-REFERENCE-BUDGET", shot_idx=1, database=database)
        checked = budget.call_args.kwargs["request_kwargs"]
        self.assertEqual(checked["reference_video_duration_sec"], 20)
        self.assertEqual(checked["reference_audio_duration_sec"], 3)
        self.assertEqual(reserve.call_args.kwargs["estimated_cost_cents"], 4284)
        self.assertEqual(submit.call_args.kwargs["reference_video_durations"], [20])
        self.assertEqual(submit.call_args.kwargs["reference_audio_durations"], [3])
        self.assertNotIn("reference_video_duration_sec", submit.call_args.kwargs)
        audit = result.provider_calls[-1]["usage"]["reference_manifest"]
        self.assertEqual([item["duration_sec"] for item in audit], [20, 3])

    async def test_generation_wrapper_duplicate_metadata_cannot_hide_31_seconds(self):
        long = media("video", 31)
        short = media("video", 5)
        request = {"duration": 8, "reference_videos": [long["uri"]],
                   "reference_manifest": [long, short]}
        with tempfile.TemporaryDirectory() as temp, patch.object(
            seedance, "text_to_video", new=AsyncMock(return_value=b"mock")) as submit, patch.object(
            s3_shots.tos, "upload", return_value="https://example.invalid/mock.mp4"):
            with self.assertRaisesRegex(seedance.SeedanceParameterError, "video.*31.*30"):
                await s3_shots._generate_seedance_video(
                    prompt="test", route=VideoRoute(MODEL, "seedance25", False, False),
                    request_kwargs=request, out_path=Path(temp) / "out.mp4", key_hint="offline")
            submit.assert_not_awaited()

    async def test_legal_client_neighbors_reach_only_mock_transport(self):
        posted = []
        def handler(request):
            posted.append(json.loads(request.content))
            return httpx.Response(400, json={"error": {"message": "mock-only sentinel"}})
        real_client = httpx.AsyncClient
        with patch.object(seedance, "_headers", return_value={}), patch.object(
            seedance.httpx, "AsyncClient",
            side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler))):
            cases = [
                {},
                dict(first_frame="https://example.invalid/frame.png", last_frame="https://example.invalid/end.png"),
                dict(references=["https://example.invalid/ref.png"]),
                dict(reference_videos=[f"https://example.invalid/{i}.mp4" for i in range(4)],
                     reference_video_durations=[7.5] * 4),
                dict(model=LEGACY, reference_videos=["https://example.invalid/old.mp4"],
                     reference_video_durations=[16]),
                dict(reference_videos=["https://example.invalid/same.mp4"] * 2,
                     reference_video_durations=[20, 20]),
            ]
            for case in cases:
                with self.subTest(case=case):
                    previous_posts = len(posted)
                    with self.assertRaises(seedance.SeedanceParameterError):
                        await seedance.text_to_video("test", **{"model": MODEL, **case})
                    self.assertEqual(len(posted), previous_posts + 1)
        self.assertEqual(len(posted), len(cases))
        self.assertNotIn("ratio", posted[1])
        self.assertEqual(posted[3]["ratio"], "9:16")
        self.assertEqual(len(posted[-1]["content"]), 2)
        self.assertTrue(all("reference_video_durations" not in item for item in posted))

    async def test_generation_wrapper_rejects_unknown_or_overlong_references_before_client(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            seedance, "text_to_video", new=AsyncMock()) as submit:
            for seconds in (None, 12):
                items = [media("video", seconds, i) for i in range(3)]
                request = {"duration": 8, "reference_videos": [i["uri"] for i in items],
                           "reference_manifest": items}
                with self.subTest(seconds=seconds), self.assertRaises(seedance.SeedanceParameterError):
                    await s3_shots._generate_seedance_video(
                        prompt="test", route=VideoRoute(MODEL, "seedance25", False, False),
                        request_kwargs=request, out_path=Path(temp) / "out.mp4", key_hint="offline")
            submit.assert_not_awaited()

    async def test_generation_result_persists_chosen_reference_durations_in_audit(self):
        items = [media("video", 5, i) for i in range(2)]
        request = {"duration": 8, "reference_videos": [i["uri"] for i in items],
                   "reference_manifest": reference_attention.select_manifest(items, max_video_references=3)}
        async def generated(**kwargs):
            kwargs["on_task_created"]("offline-task")
            kwargs["on_task_updated"]("offline-task", "completed", {"usage": {}})
            kwargs["metadata_out"].update(task_id="offline-task", usage={})
            return b"offline-video"
        decision = video_qa.QADecision(True, ())
        with tempfile.TemporaryDirectory() as temp, patch.object(
            seedance, "text_to_video", new=AsyncMock(side_effect=generated)) as submit, patch.object(
            s3_shots.tos, "upload", return_value="https://example.invalid/output.mp4"), patch.object(
            video_qa, "analyze", return_value=({}, decision)), patch.object(video_qa, "evaluate", return_value=decision):
            result = await s3_shots._generate_seedance_video(
                prompt="test", route=VideoRoute(MODEL, "seedance25", False, False),
                request_kwargs=request, out_path=Path(temp) / "out.mp4", key_hint="offline")
        self.assertEqual(submit.call_args.kwargs["reference_video_durations"], [5, 5])
        audit = result.provider_calls[-1]["usage"]["reference_manifest"]
        self.assertEqual([item["duration_sec"] for item in audit], [5, 5])
        self.assertNotIn("signature", json.dumps(audit))

    async def test_invalid_client_requests_fail_before_mocked_http_post(self):
        posted = []
        def handler(request):
            posted.append(request)
            return httpx.Response(400, json={"error": {"message": "sent"}})
        real_client = httpx.AsyncClient
        with patch.object(seedance, "_headers", return_value={}), patch.object(
            seedance.httpx, "AsyncClient",
            side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler))):
            cases = [
                dict(first_frame="https://example.invalid/frame.png",
                     references=["https://example.invalid/ref.png"]),
                dict(task_intent="edit", duration=-1, ratio="adaptive",
                     reference_audios=["https://example.invalid/voice.wav"]),
                dict(reference_videos=[f"https://example.invalid/{i}.mp4" for i in range(3)],
                     reference_video_durations=[12, 12, 12]),
                dict(reference_audios=[f"https://example.invalid/{i}.wav" for i in range(2)],
                     reference_audio_durations=[16, 16]),
            ]
            for case in cases:
                with self.subTest(case=case):
                    with self.assertRaises(seedance.SeedanceParameterError):
                        await seedance.text_to_video("test", **{ "model": MODEL, **case})
        self.assertEqual(posted, [])

    async def test_production_preflight_rejects_unknown_required_remote_media_without_probe(self):
        request = {"reference_videos": ["https://example.invalid/v.mp4"]}
        with patch.object(s3_shots.ffmpeg, "probe_duration", new=AsyncMock()) as probe:
            with self.assertRaisesRegex(seedance.SeedanceParameterError, "duration unavailable.*video.*1"):
                await s3_shots._preflight_reference_manifest(request, model=MODEL)
        probe.assert_not_awaited()

    async def test_local_controlled_audio_duration_is_inspected_and_audit_is_sanitized(self):
        with tempfile.TemporaryDirectory() as temp:
            audio = Path(temp) / "voice.wav"
            audio.write_bytes(b"controlled fixture")
            request = {"reference_audios": [str(audio)]}
            with patch.object(s3_shots.ffmpeg, "probe_duration", new=AsyncMock(return_value=8.25)) as probe:
                manifest = await s3_shots._preflight_reference_manifest(request, model=MODEL)
            probe.assert_awaited_once_with(audio.resolve())
            audit = reference_attention.sanitized_manifest(manifest)
            self.assertEqual(audit[0]["duration_sec"], 8.25)
            self.assertNotIn(str(audio), json.dumps(audit))
            self.assertEqual(set(audit[0]), {"kind", "duty", "material_index", "duration_sec"})

    async def test_failed_local_probe_has_clear_diagnostic_without_private_uri(self):
        with tempfile.TemporaryDirectory() as temp:
            audio = Path(temp) / "private.wav"
            audio.write_bytes(b"broken")
            with patch.object(s3_shots.ffmpeg, "probe_duration", new=AsyncMock(side_effect=ValueError("private"))):
                with self.assertRaisesRegex(ValueError, "duration unavailable") as error:
                    await s3_shots._preflight_reference_manifest({"reference_audios": [str(audio)]}, model=MODEL)
            self.assertNotIn(str(audio), str(error.exception))

    async def test_production_does_not_invent_a_legacy_duration_cap(self):
        item = media("video", 16)
        manifest = await s3_shots._preflight_reference_manifest(
            {"reference_videos": [item["uri"]], "reference_manifest": [item]}, model=LEGACY)
        self.assertEqual(manifest[0]["duration_sec"], 16)

    async def test_each_selected_duration_reaches_sanitized_audit(self):
        items = [media("video", 7.5, i) for i in range(4)] + [media("audio", 3)]
        manifest = reference_attention.select_manifest(items, max_video_references=4)
        request = {"reference_videos": [i["uri"] for i in items[:4]],
                   "reference_audios": [items[-1]["uri"]], "reference_manifest": manifest}
        result = await s3_shots._preflight_reference_manifest(request, model=MODEL)
        audit = reference_attention.sanitized_manifest(result)
        self.assertEqual([i["duration_sec"] for i in audit], [7.5] * 4 + [3])
        self.assertNotIn("signature", json.dumps(audit))
        self.assertNotIn("https:", json.dumps(audit))


if __name__ == "__main__":
    unittest.main()
