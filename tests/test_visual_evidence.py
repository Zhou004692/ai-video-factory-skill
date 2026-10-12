"""Offline content proof: authored intent cannot become observed evidence."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from factory import visual_contract
from factory.clients import video_qa
from factory.steps import s3_shots, s6_compose
from factory.video_routing import VideoRoute

REQS = [{"id": "hand", "description": "Raised hand is visible", "priority": "critical"}]
SHA = hashlib.sha256(b"candidate").hexdigest()

def observation():
    return {"visual_comparison": {"alignment": .95, "requirement_evidence": [
        {"id": "hand", "status": "satisfied", "event_refs": ["e1"]}]},
        "observed_visual_events": [{"id": "e1", "start_sec": 1., "end_sec": 2.,
            "observed_description": "Alice raises her hand", "confidence": .9}],
        "transcript": "Ready", "confidence": 1., "lip_sync": True}

class EvidenceTests(unittest.TestCase):
    def report(self, raw=None):
        from factory import visual_evidence
        proof = visual_evidence.bind_observations(REQS, raw or observation(), asset_sha256=SHA, duration_sec=4)
        return visual_evidence.evaluate(REQS, proof, asset_sha256=SHA, duration_sec=4)

    def test_valid_observed_action(self):
        report = self.report()
        self.assertTrue(report["passed"])
        self.assertEqual(report["items"][0]["start_sec"], 1.)

    def test_alignment_and_correct_dialogue_cannot_prove_missing_action(self):
        raw = observation(); raw["observed_visual_events"] = []
        decision = video_qa.evaluate(raw, expect_speech=True, expected_text="Ready",
            expected_visual="hand", visual_requirements=REQS, actual_duration_sec=4)
        self.assertFalse(decision.passed)
        self.assertIn("content_insufficient_evidence", decision.reasons)

    def test_invalid_events_and_references_fail_closed(self):
        cases = [{"start_sec": float("nan")}, {"end_sec": 5}, {"start_sec": -1},
                 {"confidence": .4}, {"observed_description": ""}]
        for changes in cases:
            with self.subTest(changes=changes):
                raw = observation(); raw["observed_visual_events"][0].update(changes)
                self.assertFalse(self.report(raw)["passed"])
        raw = observation(); raw["visual_comparison"]["requirement_evidence"][0]["event_refs"] = ["invented"]
        self.assertFalse(self.report(raw)["passed"])

    def test_conflict_and_automatic_not_applicable_are_rejected(self):
        for status in ("conflicting", "not_applicable", "unknown"):
            raw = observation(); raw["visual_comparison"]["requirement_evidence"][0]["status"] = status
            self.assertFalse(self.report(raw)["passed"])

    def test_malformed_mapping_cannot_disappear_beside_valid_mapping(self):
        from factory import visual_evidence
        for malformed in (None, "invalid", 7, float("nan")):
            with self.subTest(malformed=malformed):
                raw = observation(); raw["visual_comparison"]["requirement_evidence"].append(malformed)
                proof = visual_evidence.bind_observations(REQS, raw, asset_sha256=SHA, duration_sec=4)
                result = visual_evidence.evaluate(REQS, proof, asset_sha256=SHA, duration_sec=4)
                self.assertFalse(result["passed"])
                self.assertIn("content_invalid_evidence", result["reasons"])
                json.dumps(proof, allow_nan=False)
                json.dumps(result, allow_nan=False)

    def test_nonfinite_mapping_refs_remain_rejected_and_json_safe(self):
        from factory import visual_evidence
        raw = observation()
        raw["visual_comparison"]["requirement_evidence"][0]["event_refs"] = [float("nan")]
        proof = visual_evidence.bind_observations(REQS, raw, asset_sha256=SHA, duration_sec=4)
        result = visual_evidence.evaluate(REQS, proof, asset_sha256=SHA, duration_sec=4)
        self.assertFalse(result["passed"])
        json.dumps(proof, allow_nan=False)
        json.dumps(result, allow_nan=False)

    def test_action_retention_requires_output_adjacent_ascending_source(self):
        from factory import visual_evidence
        raw = observation(); raw["observed_visual_events"][0]["end_sec"] = 3
        proof = visual_evidence.bind_observations(REQS, raw, asset_sha256=SHA, duration_sec=4)
        board = {"visual_requirements": REQS, "masters": [{"idx": 1, "visual_requirements": REQS}]}
        masters = {1: {"asset_sha256": SHA, "duration_sec": 4, "evidence": proof},
                   2: {"asset_sha256": "other", "duration_sec": 4}}
        def edit(master, start, duration=1):
            return {"video_master_idx": master, "video_start": start, "duration": duration}
        for shots, expected in (
            ([edit(1, 1, 2)], True),
            ([edit(1, 1), edit(1, 2)], True),
            ([edit(1, 2), edit(1, 1)], False),
            ([edit(1, 1), edit(2, 0), edit(1, 2)], False),
            ([edit(1, 1), edit(1, 1.5, 1.5)], False),
        ):
            with self.subTest(shots=shots):
                board["shots"] = shots
                report = visual_evidence.retention_report(board, masters)
                self.assertEqual(report["passed"], expected)

    def test_fades_exclude_cross_cut_and_single_clip_boundary_actions(self):
        from factory import visual_evidence
        board = {"visual_requirements": REQS, "masters": [{"idx": 1, "visual_requirements": REQS}],
            "shots": [{"idx": 1, "video_master_idx": 1, "video_start": 1, "duration": 1},
                      {"idx": 2, "video_master_idx": 1, "video_start": 2, "duration": 1,
                       "transition_type": "dip_to_black", "transition_duration_sec": .25}]}
        for start, end, second_master, transition, expected in (
            (1.95, 2.05, 1, "dip_to_black", False),
            (1.90, 1.98, 2, "dip_to_black", False),
            (1.2, 1.4, 2, "dip_to_black", True),
            (1.95, 2.05, 1, "none", True),
        ):
            with self.subTest(start=start, second_master=second_master, transition=transition):
                raw = observation(); raw["observed_visual_events"][0].update(start_sec=start, end_sec=end)
                proof = visual_evidence.bind_observations(REQS, raw, asset_sha256=SHA, duration_sec=4)
                masters = {1: {"asset_sha256": SHA, "duration_sec": 4, "evidence": proof},
                           2: {"asset_sha256": "other", "duration_sec": 4}}
                board["shots"][1].update(video_master_idx=second_master, transition_type=transition)
                report = visual_evidence.retention_report(board, masters)
                self.assertEqual(report["passed"], expected)
                self.assertEqual(report["kept_intervals"][0]["end_sec"], 2)
                visible_end = 1.75 if transition == "dip_to_black" else 2
                self.assertEqual(report["visible_intervals"][0]["end_sec"], visible_end)

    def test_gate_and_renderer_share_capped_transition_projection(self):
        from factory import visual_evidence
        rows = [{"idx": 1, "video_path": "one.mp4"}]
        for second_duration, expected in ((1, .25), (.1, .05)):
            shots = [{"idx": 1, "video_master_idx": 1, "duration": 1, "video_start": 0},
                     {"idx": 2, "video_master_idx": 1, "duration": second_duration, "video_start": 1,
                      "transition_type": "dip_to_black", "transition_duration_sec": 5}]
            projected = visual_evidence.picture_transitions(shots)
            rendered = s6_compose._director_segments({"shots": shots}, rows)
            self.assertEqual(projected[0]["video_fade_out_sec"], expected)
            self.assertEqual(projected[1]["video_fade_in_sec"], expected)
            for index in range(2):
                self.assertEqual(projected[index]["video_fade_in_sec"], rendered[index]["video_fade_in_sec"])
                self.assertEqual(projected[index]["video_fade_out_sec"], rendered[index]["video_fade_out_sec"])

    def test_remote_edge_fades_do_not_block_unfaded_continuous_action(self):
        from factory import visual_evidence
        raw = observation(); raw["observed_visual_events"][0].update(start_sec=1.8, end_sec=2.2)
        proof = visual_evidence.bind_observations(REQS, raw, asset_sha256=SHA, duration_sec=4)
        masters = {1: {"asset_sha256": SHA, "duration_sec": 4, "evidence": proof},
                   2: {"asset_sha256": "other", "duration_sec": 4}}
        board = {"visual_requirements": REQS, "masters": [{"idx": 1, "visual_requirements": REQS}]}
        # Entry fade finishes before the action; the later connection is clear.
        board["shots"] = [{"idx": 1, "video_master_idx": 2, "video_start": 0, "duration": 1},
            {"idx": 2, "video_master_idx": 1, "video_start": 1, "duration": 1,
             "transition_type": "dip_to_black", "transition_duration_sec": .25},
            {"idx": 3, "video_master_idx": 1, "video_start": 2, "duration": 1}]
        report = visual_evidence.retention_report(board, masters)
        with self.subTest(edge="entry"):
            self.assertTrue(report["passed"])
            self.assertEqual(report["visible_intervals"][1]["start_sec"], 1.25)
        # Exit fade starts after the action; it must still trim the outer end.
        board["shots"] = [{"idx": 1, "video_master_idx": 1, "video_start": 1, "duration": 1},
            {"idx": 2, "video_master_idx": 1, "video_start": 2, "duration": 1},
            {"idx": 3, "video_master_idx": 2, "video_start": 0, "duration": 1,
             "transition_type": "dip_to_black", "transition_duration_sec": .25}]
        report = visual_evidence.retention_report(board, masters)
        with self.subTest(edge="exit"):
            self.assertTrue(report["passed"])
            self.assertEqual(report["visible_intervals"][1]["end_sec"], 2.75)

    def test_invalid_transition_duration_cannot_reach_renderer_or_pass_gate(self):
        from factory import visual_evidence
        proof = visual_evidence.bind_observations(REQS, observation(), asset_sha256=SHA, duration_sec=4)
        for invalid in (float("nan"), float("inf"), -.1, "invalid"):
            shots = [{"idx": 1, "video_master_idx": 1, "duration": 1, "video_start": 0},
                     {"idx": 2, "video_master_idx": 1, "duration": 1, "video_start": 1,
                      "transition_type": "dip_to_black", "transition_duration_sec": invalid}]
            board = {"visual_requirements": REQS, "masters": [{"idx": 1, "visual_requirements": REQS}], "shots": shots}
            with self.subTest(invalid=invalid):
                self.assertFalse(visual_evidence.retention_report(board,
                    {1: {"asset_sha256": SHA, "duration_sec": 4, "evidence": proof}})["passed"])
                with self.assertRaises(visual_evidence.ContentReviewRequired):
                    s6_compose._director_segments(board, [{"idx": 1, "video_path": "one.mp4"}])

    def test_stale_and_forged_human_proof_are_rejected(self):
        from factory import visual_evidence
        proof = visual_evidence.bind_observations(REQS, observation(), asset_sha256=SHA, duration_sec=4)
        for key, value in (("asset_sha256", "wrong"), ("requirement_set_hash", "old"), ("provenance", "human")):
            bad = deepcopy(proof); bad[key] = value
            self.assertFalse(visual_evidence.evaluate(REQS, bad, asset_sha256=SHA, duration_sec=4)["passed"])
        bad = deepcopy(proof); bad["items"].append({"id": "unknown", "status": "satisfied"})
        self.assertFalse(visual_evidence.evaluate(REQS, bad, asset_sha256=SHA, duration_sec=4)["passed"])

    def test_root_requirement_changed_without_updating_master_is_stale(self):
        from factory import visual_evidence
        proof = visual_evidence.bind_observations(REQS, observation(), asset_sha256=SHA, duration_sec=4)
        board = {"visual_requirements": [{**REQS[0], "description": "Both hands raised"}],
            "masters": [{"idx": 1, "visual_requirements": REQS}],
            "shots": [{"video_master_idx": 1, "video_start": 0, "duration": 3}]}
        self.assertFalse(visual_evidence.retention_report(board, {1: {"asset_sha256": SHA,
            "duration_sec": 4, "evidence": proof}})["passed"])

    def test_human_validator_is_explicit_and_accepts_reasoned_waiver(self):
        from factory import visual_evidence
        evidence = {"asset_sha256": SHA, "requirement_set_hash": visual_contract.requirement_set_hash(REQS),
            "provenance": "human", "items": [{"id": "hand", "status": "not_applicable", "reason": "Approved revised action"}]}
        self.assertFalse(visual_evidence.evaluate(REQS, evidence, asset_sha256=SHA, duration_sec=4)["passed"])
        self.assertTrue(visual_evidence.evaluate_human(REQS, evidence, asset_sha256=SHA, duration_sec=4)["passed"])
        evidence["items"][0]["reason"] = ""
        self.assertFalse(visual_evidence.evaluate_human(REQS, evidence, asset_sha256=SHA, duration_sec=4)["passed"])

    def test_mixed_repeated_words_cannot_reuse_one_observation(self):
        plan = {"beats": [{"speech_mode": "on_screen", "dialogue": "Ready"},
            {"speech_mode": "off_screen", "dialogue": "Go"}, {"speech_mode": "on_screen", "dialogue": "Ready"}]}
        raw = observation(); raw["observed_mouth_intervals"] = [{"start_sec": 0, "end_sec": 1,
            "dialogue": "Ready", "alignment": .95, "confidence": .9, "visible_speaking_mouth": True}]
        self.assertFalse(video_qa.evaluate(raw, expect_speech=True, visual_plan=plan, actual_duration_sec=4).passed)

    def test_same_words_across_onscreen_and_offscreen_turns_are_ambiguous(self):
        plan = {"beats": [{"speech_mode": "on_screen", "dialogue": "Ready"},
                          {"speech_mode": "off_screen", "dialogue": "Ready!"}]}
        raw = observation(); raw.update(transcript="Ready Ready", lip_sync=False,
            lip_sync_evidence={"overall_alignment": .1, "confidence": .99, "segments": []})
        raw["observed_mouth_intervals"] = [{"start_sec": 2, "end_sec": 3, "dialogue": "Ready",
            "alignment": .99, "confidence": .99, "visible_speaking_mouth": True}]
        decision = video_qa.evaluate(raw, expect_speech=True, visual_plan=plan, actual_duration_sec=4)
        self.assertFalse(decision.passed)
        self.assertIn("content_mouth_evidence_missing", decision.reasons)

    def test_retention_uses_picture_intervals_and_current_hashes(self):
        from factory import visual_evidence
        proof = visual_evidence.bind_observations(REQS, observation(), asset_sha256=SHA, duration_sec=4)
        board = {"visual_requirements": REQS, "masters": [{"idx": 1, "visual_requirements": REQS}],
            "shots": [{"video_master_idx": 1, "audio_master_idx": 2, "video_start": 0, "audio_start": 3, "duration": 3}]}
        masters = {1: {"asset_sha256": SHA, "duration_sec": 4, "evidence": proof}}
        self.assertTrue(visual_evidence.retention_report(board, masters)["passed"])
        board["shots"][0].update(video_start=2.1, audio_start=0, duration=1)
        report = visual_evidence.retention_report(board, masters)
        self.assertFalse(report["passed"])
        self.assertIn("content_event_cropped", report["reasons"])
        board["shots"][0].update(video_master_idx=2, audio_master_idx=1, video_start=0, duration=3)
        self.assertFalse(visual_evidence.retention_report(board, masters)["passed"])
        masters[1]["asset_sha256"] = "changed"
        self.assertFalse(visual_evidence.retention_report(board, masters)["passed"])

    def test_mixed_mouth_requires_scoped_observation(self):
        plan = {"beats": [{"speech_mode": "on_screen", "dialogue": "Ready", "start": 0, "duration": 2},
                          {"speech_mode": "off_screen", "dialogue": "Go", "start": 2, "duration": 2}]}
        raw = observation(); raw["lip_sync"] = False
        decision = video_qa.evaluate(raw, expect_speech=True, visual_plan=plan, actual_duration_sec=4)
        self.assertIn("content_mouth_evidence_missing", decision.reasons)
        raw["observed_mouth_intervals"] = [{"start_sec": 0, "end_sec": 2, "dialogue": "Ready",
             "alignment": .95, "confidence": .9, "visible_speaking_mouth": True}]
        self.assertTrue(video_qa.evaluate(raw, expect_speech=True, visual_plan=plan, actual_duration_sec=4).passed)
        raw["observed_mouth_intervals"][0]["dialogue"] = "unrelated"
        self.assertFalse(video_qa.evaluate(raw, expect_speech=True, visual_plan=plan, actual_duration_sec=4).passed)

class GenerationEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_uncertain_content_keeps_candidate_and_never_retries(self):
        raw = observation(); raw["observed_visual_events"] = []
        route = VideoRoute(model="seedance25", tier="seedance25", generate_audio=False, qa_required=True)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/"shot.mp4"; target.write_bytes(b"accepted")
            with patch.object(s3_shots.seedance, "text_to_video", new=AsyncMock(return_value=b"candidate")) as generate, \
                 patch.object(s3_shots.tos, "upload", return_value="https://qa/clip"), \
                 patch.object(video_qa, "analyze", return_value=(raw, video_qa.QADecision(True, ()))), \
                 patch.object(s3_shots.ffmpeg, "probe_duration", new=AsyncMock(return_value=4)):
                with self.assertRaises(s3_shots.SeedanceQualityError) as raised:
                    await s3_shots._generate_seedance_video(prompt="hand", route=route,
                        request_kwargs={"duration": 4}, out_path=target, key_hint="offline", visual_requirements=REQS)
            self.assertEqual(generate.await_count, 1)
            self.assertEqual(target.read_bytes(), b"accepted")
            self.assertEqual(raised.exception.candidate_path.read_bytes(), b"candidate")
            self.assertEqual(raised.exception.stable_code, "content_review_required")
            self.assertEqual(raised.exception.qa_evidence["visual_evidence"]["asset_sha256"], SHA)

    async def test_valid_observation_binds_to_measured_asset_and_forces_qa(self):
        route = VideoRoute(model="seedance25", tier="seedance25", generate_audio=False, qa_required=False)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(s3_shots.seedance, "text_to_video", new=AsyncMock(return_value=b"candidate")), \
             patch.object(s3_shots.tos, "upload", return_value="https://qa/clip"), \
             patch.object(video_qa, "analyze", return_value=(observation(), video_qa.QADecision(True, ()))) as analyze, \
             patch.object(s3_shots.ffmpeg, "probe_duration", new=AsyncMock(return_value=4)):
            result = await s3_shots._generate_seedance_video(prompt="hand", route=route,
                request_kwargs={"duration": 10}, out_path=Path(tmp)/"shot.mp4", key_hint="offline", visual_requirements=REQS)
            proof = json.loads(result.qa_note)["result"]["visual_evidence"]
            self.assertEqual(proof["duration_sec"], 4)
            self.assertEqual(proof["asset_sha256"], SHA)
            self.assertEqual(analyze.call_args.kwargs["actual_duration_sec"], 4)

    async def test_disabling_asr_cannot_disable_mixed_onscreen_mouth_gate(self):
        route = VideoRoute(model="seedance25", tier="seedance25", generate_audio=False, qa_required=True)
        plan = {"beats": [{"speech_mode": "on_screen", "dialogue": "Ready"}, {"speech_mode": "off_screen", "dialogue": "Go"}]}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(s3_shots.seedance, "text_to_video", new=AsyncMock(return_value=b"candidate")), \
             patch.object(s3_shots.tos, "upload", return_value="https://qa/clip"), \
             patch.object(video_qa, "analyze", return_value=(observation(), video_qa.QADecision(True, ()))), \
             patch.object(s3_shots.ffmpeg, "probe_duration", new=AsyncMock(return_value=4)):
            with self.assertRaises(s3_shots.SeedanceQualityError) as raised:
                await s3_shots._generate_seedance_video(prompt="hand", route=route, request_kwargs={"duration": 4},
                    out_path=Path(tmp)/"shot.mp4", key_hint="offline", expected_text="Ready Go", asr_enabled=False,
                    visual_requirements=REQS, visual_plan=plan)
            self.assertIn("content_mouth_evidence_missing", raised.exception.reasons)

class ComposeEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_rollback_failure_retains_backups_and_reports_recovery_paths(self):
        from factory import visual_evidence
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); final = root/"final.mp4"; report = root/"content_retention.json"
            candidate = root/"candidate.mp4"; staged = root/"staged.json"
            final.write_bytes(b"old"); report.write_text("old-proof", encoding="utf-8")
            candidate.write_bytes(b"new"); staged.write_text("new-proof", encoding="utf-8")
            original_replace = Path.replace; original_copy = s6_compose.shutil.copy2
            def fail_report(path, target):
                if path == staged:
                    raise OSError("injected report replacement failure")
                return original_replace(path, target)
            def fail_final_restore(source, target):
                if Path(source).name == ".final.mp4.content-backup":
                    raise OSError("injected rollback failure")
                return original_copy(source, target)
            with patch.object(Path, "replace", fail_report), patch.object(s6_compose.shutil, "copy2", fail_final_restore):
                with self.assertRaises(visual_evidence.ContentReviewRequired) as raised:
                    s6_compose._commit_content_final(final, candidate, report, staged)
            paths = raised.exception.report["recovery_paths"]
            self.assertEqual(Path(paths[str(final)]).read_bytes(), b"old")
            self.assertEqual(Path(paths[str(report)]).read_text(encoding="utf-8"), "old-proof")
            self.assertEqual(report.read_text(encoding="utf-8"), "old-proof")
            self.assertIn("content_commit_recovery_required", raised.exception.report["reasons"])
            # Reentry must preserve the original recovery backup rather than
            # copying the partially committed new final over it.
            with self.assertRaises(visual_evidence.ContentReviewRequired):
                s6_compose._commit_content_final(final, candidate, report, staged)
            self.assertEqual(Path(paths[str(final)]).read_bytes(), b"old")

    async def test_missing_master_has_actionable_content_review_signal(self):
        from factory import visual_evidence
        with self.assertRaises(visual_evidence.ContentReviewRequired) as raised:
            await s6_compose._content_retention({"visual_requirements": REQS},
                [{"idx": 1, "video_path": "missing-content-master.mp4", "video_qa": "{}"}])
        self.assertEqual(raised.exception.stable_code, "content_review_required")

    async def test_final_report_commit_failure_restores_both_previous_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); final=root/"final.mp4"; report=root/"content_retention.json"
            candidate=root/"candidate.mp4"; staged=root/"staged.json"
            final.write_bytes(b"old"); report.write_text("old-proof", encoding="utf-8")
            candidate.write_bytes(b"new"); staged.write_text("new-proof", encoding="utf-8")
            original = Path.replace
            def fail_report(path, target):
                if path == staged:
                    raise OSError("injected report replacement failure")
                return original(path, target)
            with patch.object(Path, "replace", fail_report), self.assertRaises(OSError):
                s6_compose._commit_content_final(final, candidate, report, staged)
            self.assertEqual(final.read_bytes(), b"old")
            self.assertEqual(report.read_text(encoding="utf-8"), "old-proof")

    async def compose(self, tmp, *, crop=False, legacy=False, refresh=None):
        from factory import visual_evidence
        base = Path(tmp)/"job"; base.mkdir()
        source = base/"master.mp4"; source.write_bytes(b"candidate")
        old = base/"final.mp4"; old.write_bytes(b"old-final")
        proof = visual_evidence.bind_observations(REQS, observation(), asset_sha256=SHA, duration_sec=4)
        board = {"production_mode": "native_master_local_director", "masters": [{"idx": 1, "visual_requirements": REQS}],
            "visual_requirements": [] if legacy else REQS, "shots": [{"idx": 1, "video_master_idx": 1,
                "audio_master_idx": 1, "video_start": 2.1 if crop else 0, "duration": 1 if crop else 3}]}
        database=Mock(); row = {"idx": 1, "video_path": str(source), "video_qa": json.dumps({"result": {"visual_evidence": proof}})}
        database.execute.return_value.fetchone.return_value = {"storyboard": json.dumps(board)}
        database.execute.return_value.fetchall.return_value = [row]
        async def mix(_concat, _bgm, output, **_kwargs):
            Path(output).write_bytes(b"new-final")
        with patch.object(s6_compose, "ASSETS_DIR", Path(tmp)), patch.object(s6_compose, "conn", return_value=database), \
             patch.object(s6_compose, "_refresh_director_speech_timeline", new=AsyncMock(side_effect=refresh)), \
             patch.object(s6_compose.ffmpeg, "probe_duration", new=AsyncMock(return_value=4)), \
             patch.object(s6_compose, "_prep_director_segment", new=AsyncMock(return_value=base/"prep.mp4")) as prep, \
             patch.object(s6_compose.ffmpeg, "concat_shots", new=AsyncMock()), \
             patch.object(s6_compose.ffmpeg, "mix_bgm", new=AsyncMock(side_effect=mix)), \
             patch.object(s6_compose, "finalize_accepted_candidates"), \
             patch.dict(os.environ, {"APPEND_CTA": "false", "GEN_THUMBNAIL": "false", "GEN_MARKETING_THUMBNAIL": "false"}):
            try:
                await s6_compose.run("job")
            except visual_evidence.ContentReviewRequired as exc:
                self.assertEqual(old.read_bytes(), b"old-final")
                self.assertEqual(prep.await_count, 0)
                self.assertEqual(exc.stable_code, "content_review_required")
                self.assertTrue((base/"content_retention.review.json").is_file())
                self.assertIs(exc.report, exc.content_result)
                raise
            finally:
                database.close.assert_called_once()
        return base

    async def test_retained_action_persists_final_and_master_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = await self.compose(tmp)
            report = json.loads((base/"content_retention.json").read_text(encoding="utf-8"))
            self.assertTrue(report["passed"])
            self.assertEqual(report["final_sha256"], hashlib.sha256(b"new-final").hexdigest())
            self.assertEqual(report["master_hashes"]["1"], SHA)

    async def test_cropped_action_stops_before_old_final_is_overwritten(self):
        from factory import visual_evidence
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(visual_evidence.ContentReviewRequired):
            await self.compose(tmp, crop=True)

    async def test_asr_picture_shift_is_checked_after_refresh(self):
        from factory import visual_evidence
        async def refresh(_job, board, _rows, _db):
            board["shots"][0].update(video_start=2.1, duration=1)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(visual_evidence.ContentReviewRequired):
            await self.compose(tmp, refresh=refresh)

    async def test_empty_root_contract_keeps_legacy_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = await self.compose(tmp, crop=True, legacy=True)
            self.assertEqual((base/"final.mp4").read_bytes(), b"new-final")
            self.assertFalse((base/"content_retention.json").exists())

class QATransportEvidenceTests(unittest.TestCase):
    def test_existing_two_calls_map_requirements_to_blind_event_refs(self):
        raw = observation()
        response = Mock(status_code=200); response.json.return_value = {"choices": [{"message": {"content": json.dumps(raw)}}]}
        client = Mock(); client.__enter__ = Mock(return_value=client); client.__exit__ = Mock(return_value=False); client.post.return_value=response
        with patch.dict(os.environ, {"ARK_API_KEY": "offline"}), patch.object(video_qa.httpx, "Client", return_value=client), \
             patch.object(video_qa.doubao_llm, "complete_json", return_value=(raw["visual_comparison"], {})) as compare:
            _, decision = video_qa.analyze("https://qa/clip", expect_speech=True, expected_text="Ready",
                expected_visual="hand", visual_requirements=REQS, actual_duration_sec=4)
        self.assertTrue(decision.passed)
        self.assertEqual(client.post.call_count, 1); self.assertEqual(compare.call_count, 1)
        self.assertIn("observed_visual_events", client.post.call_args.kwargs["json"]["messages"][0]["content"][1]["text"])
        self.assertIn("event_refs", compare.call_args.args[0])

