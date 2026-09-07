"""Formal one-master replay through real QA, ASR, compose and human review.

This is transport/pipeline evidence only.  The authored 64px media is not a
content-quality golden and this journey intentionally has no identity/scene
anchors; those remain covered by the independent character-anchor journey.
"""
from __future__ import annotations

import hashlib
import base64
import asyncio
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory import (  # noqa: E402
    creative_ir,
    production_adapters,
    production_compiler,
    production_config,
    production_graph,
    production_graph_runner,
)
from factory.management_api import ManagementService  # noqa: E402
from tests.strict_handler_support import StrictMasterComposeReplay  # noqa: E402
from tests.strict_replay_support import StrictReplayViolation  # noqa: E402


JOB_ID = "J-strict-master"
REVIEW_CHECKS = {
    "identity": True,
    "lip_sync": True,
    "subtitle_sync": True,
    "visual_integrity": True,
    "clean_frame": True,
    "editing_rhythm": True,
}


class StrictMasterComposeJourneyTests(unittest.IsolatedAsyncioTestCase):
    def contract(self):
        return json.loads(
            (ROOT / "tests/fixtures/strict_transport/master_compose.json")
            .read_text(encoding="utf-8")
        )

    def empty_asr_contract(self):
        contract = self.contract()
        contract["steps"][10]["response"]["json"] = {
            "result": {"text": "", "utterances": []},
        }
        return contract

    def prepare(self, replay: StrictMasterComposeReplay):
        snapshot = production_config.freeze_generation_snapshot({
            "audio_mode": "seedance_native",
            "resolution": "480p",
            "ratio": "1:1",
            "video_model": "doubao-seedance-2-5-260628",
            "qa_enabled": True,
            "max_auto_retries": 0,
            "auto_retry_budget_cents": 0,
            "budget_cents": 10000,
        })
        source = {
            "title": "Strict master compose",
            "target_duration_sec": 4,
            "production_mode": "native_master_local_director",
            "characters": [],
            "production_settings": {
                "music_provider": "silent",
                "asr_enabled": True,
                "budget_cents": 10000,
                "generation_snapshot": snapshot,
            },
            "beats": [{
                "id": "voice-1",
                "scene": "A boom lift inspection in daylight",
                "duration_sec": 4,
                "speaker": "narrator",
                "speech_mode": "narration",
                "dialogue": "Ready for delivery.",
                "action": "A boom lift inspection in daylight",
                "camera": "medium-wide",
            }],
        }
        compilation = production_compiler.compile_ir(
            creative_ir.from_storyboard(source),
            job_id=JOB_ID,
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
            runtime_storyboard=source,
        )
        self.assertTrue(compilation.compilable, compilation.issues)
        spec = compilation.spec
        self.assertEqual(len(spec.masters), 1)
        self.assertFalse(any(
            node.node_type in {"character_anchor", "scene_anchor"}
            for node in spec.nodes
        ))
        replay.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,"
            "production_spec_json,production_spec_hash,production_spec_version,"
            "execution_engine) VALUES(?,?,?,'PRODUCTION_RUNNING',?,?,?,2,'graph_v2')",
            (
                JOB_ID,
                "T-strict-master",
                source["title"],
                json.dumps(spec.runtime_storyboard, ensure_ascii=False),
                json.dumps(spec.as_dict(), ensure_ascii=False),
                spec.spec_hash,
            ),
        )
        production_adapters.materialize_frozen_execution_inputs(
            replay.database, spec
        )
        production_graph.persist_graph(replay.database, spec)
        replay.database.commit()
        return spec

    async def run_job(self, replay):
        return await production_graph_runner.run_graph_job(
            replay.database,
            JOB_ID,
            configured_limit=1,
            handlers=production_adapters.build_handlers(replay.database),
        )

    def assert_single_paid_master_submission(self, replay):
        calls = replay.database.execute(
            "SELECT task_id,cost_cents FROM provider_calls WHERE job_id=?",
            (JOB_ID,),
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in calls], [("strict-master-task-1", 7)]
        )
        self.assertEqual(replay.database.execute(
            "SELECT cost_cents FROM jobs WHERE job_id=?", (JOB_ID,),
        ).fetchone()[0], 7)
        self.assertEqual(sum(
            request["method"] == "POST"
            and request["route"].endswith("/contents/generations/tasks")
            for request in replay.requests
        ), 1)

    def asr_submit_count(self, replay):
        return sum(
            request["method"] == "POST"
            and request["route"].endswith("/auc/bigmodel/submit")
            for request in replay.requests
        )

    def frozen_graph_snapshot(self, database):
        """Capture only immutable graph identity and materialized inputs.

        Execution state/output columns must change when review accepts and the
        graph resumes, so they are asserted separately instead of being
        mislabeled as part of the frozen contract.
        """
        spec_fields = (
            "production_spec_hash", "production_spec_json",
            "production_spec_version", "execution_engine",
        )
        job = database.execute(
            "SELECT production_spec_hash,production_spec_json,"
            "production_spec_version,execution_engine FROM jobs WHERE job_id=?",
            (JOB_ID,),
        ).fetchone()
        materialized_fields = (
            "idx", "duration_sec", "shot_type", "camera", "prompt",
            "voiceover", "subtitle", "narrative_type", "speaker_id",
            "anchor_character",
        )
        shot_columns = {
            str(row["name"])
            for row in database.execute("PRAGMA table_info(shots)").fetchall()
        }
        self.assertTrue(set(materialized_fields).issubset(shot_columns))
        return {
            "job_spec": {field: job[field] for field in spec_fields},
            "production_nodes": [tuple(row) for row in database.execute(
                "SELECT node_id,node_type,paid,spec_hash,input_json,"
                "idempotency_key,estimated_cost_cents FROM production_nodes "
                "WHERE job_id=? ORDER BY node_id",
                (JOB_ID,),
            ).fetchall()],
            "production_edges": [tuple(row) for row in database.execute(
                "SELECT from_node_id,to_node_id,edge_type FROM production_edges "
                "WHERE job_id=? ORDER BY from_node_id,to_node_id,edge_type",
                (JOB_ID,),
            ).fetchall()],
            "materialized_frozen_inputs": [tuple(row) for row in database.execute(
                f"SELECT {','.join(materialized_fields)} FROM shots "
                "WHERE job_id=? ORDER BY idx",
                (JOB_ID,),
            ).fetchall()],
        }

    async def test_formal_master_composes_and_review_immediately_unlocks_publish(self):
        with StrictMasterComposeReplay() as replay:
            self.prepare(replay)
            result = await self.run_job(replay)

            self.assertEqual(
                result.started_nodes,
                ("master:1", "bgm", "local_compose", "final_review"),
            )
            self.assertEqual(result.job_state, "COMPOSED")
            shot = replay.database.execute(
                "SELECT * FROM shots WHERE job_id=? AND idx=1", (JOB_ID,)
            ).fetchone()
            self.assertEqual(shot["state"], "OK")
            master = Path(shot["video_path"])
            self.assertEqual(
                hashlib.sha256(master.read_bytes()).hexdigest(),
                replay.media_sha256,
            )
            self.assertTrue(master.with_suffix(".wav").is_file())
            qa = json.loads(shot["video_qa"])
            self.assertNotEqual(qa.get("status"), "manual_review_required")
            self.assertEqual(qa["result"]["transcript"], "Ready for delivery.")
            self.assertEqual(
                qa["result"]["visual_comparison"]["alignment"], 0.98
            )
            derived = replay.database.execute(
                "SELECT media_sha256 FROM master_transcriptions "
                "WHERE job_id=? AND master_idx=1", (JOB_ID,),
            ).fetchone()
            self.assertEqual(derived["media_sha256"], replay.media_sha256)
            self.assertEqual(replay.database.execute(
                "SELECT COUNT(*) FROM character_identity_anchors WHERE job_id=?",
                (JOB_ID,),
            ).fetchone()[0], 0)
            self.assertEqual(replay.database.execute(
                "SELECT COUNT(*) FROM scene_visual_anchors WHERE job_id=?",
                (JOB_ID,),
            ).fetchone()[0], 0)

            job = replay.database.execute(
                "SELECT * FROM jobs WHERE job_id=?", (JOB_ID,)
            ).fetchone()
            self.assertTrue(Path(job["bgm_path"]).is_file())
            final = Path(job["final_path"])
            self.assertTrue(final.is_file())
            media = await replay.probe(final)
            self.assertTrue(any(s["codec_type"] == "video" for s in media["streams"]))
            self.assertTrue(any(s["codec_type"] == "audio" for s in media["streams"]))

            self.assert_single_paid_master_submission(replay)
            self.assertEqual(job["cost_cents"], 7)
            self.assertEqual(len(replay.requests), 11)
            self.assertEqual(self.asr_submit_count(replay), 1)
            director_processes = [
                process for process in replay.processes
                if process["executable"].lower().startswith("ffmpeg")
                and process["argv"][4:5] == ["-ss"]
            ]
            self.assertEqual(len(director_processes), 1)
            self.assertIn("0:v:0", director_processes[0]["argv"])
            self.assertIn("1:a:0", director_processes[0]["argv"])
            self.assertEqual(
                [request["method"] for request in replay.requests].count("POST"),
                6,
            )
            before = (len(replay.requests), len(replay.processes), job["cost_cents"])
            second = await self.run_job(replay)
            self.assertEqual(second.started_nodes, ())
            current_cost = replay.database.execute(
                "SELECT cost_cents FROM jobs WHERE job_id=?", (JOB_ID,)
            ).fetchone()[0]
            self.assertEqual(
                (len(replay.requests), len(replay.processes), current_cost), before
            )

            service = ManagementService(
                replay.database, replay.root, worker_mode="external"
            )
            review = service.review_video(
                JOB_ID,
                decision="approved",
                checks=REVIEW_CHECKS,
                first_pass=True,
            )
            self.assertTrue(review["publish_unlocked"])
            final_review = replay.database.execute(
                "SELECT state FROM production_nodes WHERE job_id=? "
                "AND node_id='final_review'",
                (JOB_ID,),
            ).fetchone()[0]
            self.assertEqual(final_review, "SUCCEEDED")
            # This direct assertion intentionally forbids an extra runner pass
            # from hiding a management-console state projection defect.
            state = replay.database.execute(
                "SELECT state FROM jobs WHERE job_id=?", (JOB_ID,)
            ).fetchone()[0]
            self.assertEqual(state, "READY_TO_PUBLISH")

        evidence = replay.evidence()
        self.assertEqual(evidence["external_provider_fees_cents"], 0)
        self.assertTrue(evidence["simulated_accounting"])
        self.assertFalse(evidence["content_quality_verified"])
        self.assertFalse(evidence["process_tree_verified"])

    async def test_successful_empty_asr_is_reused_without_second_request(self):
        with StrictMasterComposeReplay(
            contract=self.empty_asr_contract()
        ) as replay:
            self.prepare(replay)
            result = await self.run_job(replay)

            self.assertEqual(result.job_state, "COMPOSED")
            self.assertEqual(len(replay.requests), 11)
            self.assertEqual(self.asr_submit_count(replay), 1)
            self.assert_single_paid_master_submission(replay)
            artifact = replay.database.execute(
                "SELECT transcription_json FROM master_transcriptions "
                "WHERE job_id=? AND master_idx=1", (JOB_ID,),
            ).fetchone()
            payload = json.loads(artifact["transcription_json"])
            self.assertEqual(payload["text"], "")
            self.assertEqual(payload["utterances"], [])
            current = replay.database.execute(
                "SELECT storyboard FROM jobs WHERE job_id=?", (JOB_ID,),
            ).fetchone()
            storyboard = json.loads(current["storyboard"])
            edit = storyboard["shots"][0]
            self.assertEqual(edit.get("subtitle_cues") or [], [])
            self.assertEqual(edit.get("speech_timeline_version"), 3)

            before = (len(replay.requests), len(replay.processes), 7)
            second = await self.run_job(replay)
            self.assertEqual(second.started_nodes, ())
            self.assertEqual((
                len(replay.requests), len(replay.processes),
                replay.database.execute(
                    "SELECT cost_cents FROM jobs WHERE job_id=?", (JOB_ID,),
                ).fetchone()[0],
            ), before)

    async def test_qa_transport_error_never_becomes_quality_success(self):
        contract = self.contract()
        contract["steps"] = contract["steps"][:7]
        contract["steps"][6]["response"] = {
            "status_code": 503,
            "json": {"error": {"message": "authored QA outage"}},
        }
        with StrictMasterComposeReplay(contract=contract) as replay:
            self.prepare(replay)
            result = await self.run_job(replay)
            shot = replay.database.execute(
                "SELECT state,video_qa FROM shots WHERE job_id=? AND idx=1",
                (JOB_ID,),
            ).fetchone()
            self.assertEqual(shot["state"], "REVIEW_REQUIRED")
            self.assertEqual(
                json.loads(shot["video_qa"])["reasons"], ["qa_error"]
            )
            self.assertEqual(result.job_state, "PRODUCTION_REVIEW_REQUIRED")
            self.assertIsNone(replay.database.execute(
                "SELECT final_path FROM jobs WHERE job_id=?", (JOB_ID,)
            ).fetchone()[0])
            self.assert_single_paid_master_submission(replay)
            self.assertEqual(len(replay.requests), 7)
            self.assertEqual(self.asr_submit_count(replay), 0)
            self.assertEqual(replay.database.execute(
                "SELECT COUNT(*) FROM master_transcriptions WHERE job_id=?",
                (JOB_ID,),
            ).fetchone()[0], 0)
            before = (len(replay.requests), len(replay.processes), 7)
            second = await self.run_job(replay)
            self.assertEqual(second.started_nodes, ())
            self.assertEqual((
                len(replay.requests), len(replay.processes),
                replay.database.execute(
                    "SELECT cost_cents FROM jobs WHERE job_id=?", (JOB_ID,),
                ).fetchone()[0],
            ), before)

    async def test_quality_candidate_acceptance_resumes_same_frozen_graph(self):
        contract = self.empty_asr_contract()
        comparison = contract["steps"][7]["response"]["json"]
        comparison["choices"][0]["message"]["content"] = json.dumps({
            "alignment": 0.1,
            "matches": [],
            "mismatches": ["authored semantic mismatch"],
            "reason": "manual decision required",
        })
        with StrictMasterComposeReplay(contract=contract) as replay:
            spec = self.prepare(replay)
            paused = await self.run_job(replay)
            self.assertEqual(paused.started_nodes, ("master:1",))
            self.assertEqual(paused.job_state, "PRODUCTION_REVIEW_REQUIRED")
            shot = replay.database.execute(
                "SELECT state,video_qa FROM shots WHERE job_id=? AND idx=1",
                (JOB_ID,),
            ).fetchone()
            qa = json.loads(shot["video_qa"])
            self.assertEqual(shot["state"], "REVIEW_REQUIRED")
            candidate = Path(qa["candidate_path"])
            self.assertTrue(candidate.is_file())
            candidate_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
            self.assertEqual(candidate_sha, replay.media_sha256)
            self.assertEqual(replay.database.execute(
                "SELECT media_sha256 FROM master_transcriptions "
                "WHERE job_id=? AND master_idx=1", (JOB_ID,),
            ).fetchone()[0], candidate_sha)
            empty_candidate = json.loads(replay.database.execute(
                "SELECT transcription_json FROM master_transcriptions "
                "WHERE job_id=? AND master_idx=1 AND media_sha256=?",
                (JOB_ID, candidate_sha),
            ).fetchone()[0])
            self.assertEqual(empty_candidate["text"], "")
            self.assertEqual(empty_candidate["utterances"], [])
            request_count = len(replay.requests)
            cost = replay.database.execute(
                "SELECT cost_cents FROM jobs WHERE job_id=?", (JOB_ID,)
            ).fetchone()[0]
            before_accept = self.frozen_graph_snapshot(replay.database)
            self.assertEqual(
                before_accept["job_spec"]["production_spec_hash"],
                spec.spec_hash,
            )
            self.assertEqual(
                before_accept["job_spec"]["production_spec_json"],
                json.dumps(spec.as_dict(), ensure_ascii=False),
            )
            self.assertEqual(
                replay.database.execute(
                    "SELECT state FROM production_nodes WHERE job_id=? "
                    "AND node_id='master:1'", (JOB_ID,),
                ).fetchone()[0],
                "REVIEW_REQUIRED",
            )

            service = ManagementService(
                replay.database, replay.root, worker_mode="external"
            )
            accepted = service.review_master_candidate(
                JOB_ID, 1, decision="accept_candidate"
            )
            self.assertEqual(accepted["remaining_review_count"], 0)
            after_accept = self.frozen_graph_snapshot(replay.database)
            self.assertEqual(after_accept, before_accept)
            self.assertEqual(
                replay.database.execute(
                    "SELECT state FROM production_nodes WHERE job_id=? "
                    "AND node_id='master:1'", (JOB_ID,),
                ).fetchone()[0],
                "SUCCEEDED",
            )
            active_master = Path(replay.database.execute(
                "SELECT video_path FROM shots WHERE job_id=? AND idx=1",
                (JOB_ID,),
            ).fetchone()[0])
            self.assertEqual(
                hashlib.sha256(active_master.read_bytes()).hexdigest(),
                candidate_sha,
            )
            self.assertEqual(replay.database.execute(
                "SELECT media_sha256 FROM master_transcriptions "
                "WHERE job_id=? AND master_idx=1", (JOB_ID,),
            ).fetchone()[0], candidate_sha)
            resumed = await self.run_job(replay)
            self.assertEqual(
                resumed.started_nodes, ("bgm", "local_compose", "final_review")
            )
            self.assertEqual(resumed.job_state, "COMPOSED")
            self.assertEqual(
                self.frozen_graph_snapshot(replay.database), before_accept
            )
            self.assertEqual(len(replay.requests), request_count)
            self.assertEqual(self.asr_submit_count(replay), 1)
            self.assertEqual(replay.database.execute(
                "SELECT media_sha256 FROM master_transcriptions "
                "WHERE job_id=? AND master_idx=1", (JOB_ID,),
            ).fetchone()[0], replay.media_sha256)
            self.assert_single_paid_master_submission(replay)
            self.assertEqual(replay.database.execute(
                "SELECT cost_cents FROM jobs WHERE job_id=?", (JOB_ID,)
            ).fetchone()[0], cost)
            service.review_video(
                JOB_ID, decision="approved", checks=REVIEW_CHECKS
            )
            self.assertEqual(replay.database.execute(
                "SELECT state FROM jobs WHERE job_id=?", (JOB_ID,)
            ).fetchone()[0], "READY_TO_PUBLISH")

    async def test_asr_transport_error_requires_review_without_composing(self):
        contract = self.contract()
        contract["steps"] = contract["steps"][:9]
        contract["steps"][8]["response"] = {
            "status_code": 503,
            "headers": {"X-Api-Status-Code": "50000000"},
            "json": {"message": "authored ASR outage"},
        }
        with StrictMasterComposeReplay(contract=contract) as replay:
            self.prepare(replay)
            result = await self.run_job(replay)
            shot = replay.database.execute(
                "SELECT state,video_qa FROM shots WHERE job_id=? AND idx=1",
                (JOB_ID,),
            ).fetchone()
            self.assertEqual(shot["state"], "REVIEW_REQUIRED")
            self.assertEqual(
                json.loads(shot["video_qa"])["reasons"], ["qa_error"]
            )
            self.assertEqual(result.job_state, "PRODUCTION_REVIEW_REQUIRED")
            self.assertIsNone(replay.database.execute(
                "SELECT final_path FROM jobs WHERE job_id=?", (JOB_ID,)
            ).fetchone()[0])
            self.assert_single_paid_master_submission(replay)
            self.assertEqual(len(replay.requests), 9)
            self.assertEqual(self.asr_submit_count(replay), 1)
            self.assertEqual(replay.database.execute(
                "SELECT COUNT(*) FROM master_transcriptions WHERE job_id=?",
                (JOB_ID,),
            ).fetchone()[0], 0)
            before = (len(replay.requests), len(replay.processes), 7)
            second = await self.run_job(replay)
            self.assertEqual(second.started_nodes, ())
            self.assertEqual((
                len(replay.requests), len(replay.processes),
                replay.database.execute(
                    "SELECT cost_cents FROM jobs WHERE job_id=?", (JOB_ID,),
                ).fetchone()[0],
            ), before)

    async def test_corrupt_download_fails_after_accounting_without_repost(self):
        from tos.utils import Crc64

        contract = self.contract()
        corrupt = b"authored corrupt master media"
        sha256 = hashlib.sha256(corrupt).hexdigest()
        sha1 = hashlib.sha1(corrupt).hexdigest()[:8]
        response = contract["steps"][3]["response"]
        response.pop("media_fixture")
        response.update(
            body_base64=base64.b64encode(corrupt).decode("ascii"),
            body_sha256=sha256,
        )
        for step in contract["steps"][4:6]:
            step["request"]["url"] = step["request"]["url"].replace(
                "302721f6", sha1
            )
        contract["steps"][5]["request"]["body_sha256"] = sha256
        crc = Crc64()
        crc.update(corrupt)
        contract["steps"][5]["response"]["headers"][
            "x-tos-hash-crc64ecma"
        ] = str(crc.crc)
        # Audio extraction validates the downloaded master before any durable
        # object-store upload, so corrupt media must stop at the GET boundary.
        contract["steps"] = contract["steps"][:4]

        with StrictMasterComposeReplay(contract=contract) as replay:
            self.prepare(replay)
            first = await self.run_job(replay)
            shot = replay.database.execute(
                "SELECT state,cost_cents,error FROM shots WHERE job_id=? AND idx=1",
                (JOB_ID,),
            ).fetchone()
            self.assertEqual(shot["state"], "FAILED")
            self.assertIn("ffmpeg", shot["error"].lower())
            self.assertEqual(shot["cost_cents"], 7)
            self.assertEqual(first.job_state, "FAILED")
            self.assertIsNone(replay.database.execute(
                "SELECT final_path FROM jobs WHERE job_id=?", (JOB_ID,)
            ).fetchone()[0])
            before = (
                len(replay.requests),
                len(replay.processes),
                replay.database.execute(
                    "SELECT cost_cents FROM jobs WHERE job_id=?", (JOB_ID,)
                ).fetchone()[0],
            )
            second = await self.run_job(replay)
            self.assertEqual(second.started_nodes, ())
            self.assertEqual((
                len(replay.requests),
                len(replay.processes),
                replay.database.execute(
                    "SELECT cost_cents FROM jobs WHERE job_id=?", (JOB_ID,)
                ).fetchone()[0],
            ), before)
            self.assertEqual(
                [request["method"] for request in replay.requests].count("POST"),
                1,
            )

    async def test_compose_failure_reentry_never_reposts_or_rebills(self):
        with StrictMasterComposeReplay(fail_operation="mix") as replay:
            self.prepare(replay)
            first = await self.run_job(replay)
            self.assertEqual(first.job_state, "FAILED")
            self.assertEqual(replay.database.execute(
                "SELECT state FROM production_nodes WHERE job_id=? "
                "AND node_id='local_compose'", (JOB_ID,)
            ).fetchone()[0], "FAILED")
            self.assertIsNone(replay.database.execute(
                "SELECT final_path FROM jobs WHERE job_id=?", (JOB_ID,)
            ).fetchone()[0])
            before = (
                len(replay.requests),
                len(replay.processes),
                replay.database.execute(
                    "SELECT cost_cents FROM jobs WHERE job_id=?", (JOB_ID,)
                ).fetchone()[0],
            )
            second = await self.run_job(replay)
            self.assertEqual(second.started_nodes, ())
            self.assertEqual((
                len(replay.requests),
                len(replay.processes),
                replay.database.execute(
                    "SELECT cost_cents FROM jobs WHERE job_id=?", (JOB_ID,)
                ).fetchone()[0],
            ), before)
            self.assertEqual(
                [request["method"] for request in replay.requests].count("POST"),
                6,
            )

    async def test_media_process_allowlist_rejects_remote_or_extra_arguments(self):
        for attack in ("remote", "extra"):
            with self.subTest(attack=attack):
                with self.assertRaisesRegex(
                    StrictReplayViolation,
                    "media path|normalization|unapproved ffmpeg arguments",
                ):
                    with StrictMasterComposeReplay() as replay:
                        if attack == "remote":
                            command = [
                                str(replay.ffmpeg), "-y", "-hide_banner",
                                "-loglevel", "error", "-i",
                                "https://example.invalid/master.mp4",
                                "-vn", "-ac", "1", "-ar", "16000",
                                str(replay.root / "output.wav"),
                            ]
                        else:
                            # Exact approved silent-BGM command plus one extra
                            # otherwise-valid FFmpeg progress option.
                            command = [
                                str(replay.ffmpeg), "-y", "-hide_banner",
                                "-loglevel", "error", "-f", "lavfi", "-i",
                                "anullsrc=r=44100:cl=stereo", "-t", "60",
                                "-q:a", "9", "-acodec", "libmp3lame",
                                "-progress", str(replay.root / "progress.txt"),
                                str(replay.root / "output.mp3"),
                            ]
                        try:
                            await asyncio.create_subprocess_exec(*command)
                        except StrictReplayViolation:
                            pass
                        self.assertEqual(replay.processes, [])


if __name__ == "__main__":
    unittest.main()
