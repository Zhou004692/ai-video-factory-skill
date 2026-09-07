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

            calls = replay.database.execute(
                "SELECT task_id,cost_cents FROM provider_calls WHERE job_id=?",
                (JOB_ID,),
            ).fetchall()
            self.assertEqual([tuple(row) for row in calls], [("strict-master-task-1", 7)])
            self.assertEqual(job["cost_cents"], 7)
            self.assertEqual(len(replay.requests), 11)
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

    async def test_quality_candidate_acceptance_resumes_same_frozen_graph(self):
        contract = self.contract()
        comparison = contract["steps"][7]["response"]["json"]
        comparison["choices"][0]["message"]["content"] = json.dumps({
            "alignment": 0.1,
            "matches": [],
            "mismatches": ["authored semantic mismatch"],
            "reason": "manual decision required",
        })
        with StrictMasterComposeReplay(contract=contract) as replay:
            self.prepare(replay)
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
            request_count = len(replay.requests)
            cost = replay.database.execute(
                "SELECT cost_cents FROM jobs WHERE job_id=?", (JOB_ID,)
            ).fetchone()[0]

            service = ManagementService(
                replay.database, replay.root, worker_mode="external"
            )
            accepted = service.review_master_candidate(
                JOB_ID, 1, decision="accept_candidate"
            )
            self.assertEqual(accepted["remaining_review_count"], 0)
            resumed = await self.run_job(replay)
            self.assertEqual(
                resumed.started_nodes, ("bgm", "local_compose", "final_review")
            )
            self.assertEqual(resumed.job_state, "COMPOSED")
            self.assertEqual(len(replay.requests), request_count)
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
                    "media path|normalization",
                ):
                    with StrictMasterComposeReplay() as replay:
                        source = (
                            "https://example.invalid/master.mp4"
                            if attack == "remote"
                            else str(replay.root / "input.mp4")
                        )
                        command = [
                            str(replay.ffmpeg), "-y", "-hide_banner",
                            "-loglevel", "error", "-i", source,
                            "-vf", "not-an-authored-filter",
                            "-c:v", "libx264", "-preset", "medium",
                            "-crf", "20", "-c:a", "aac", "-b:a", "128k",
                            "-ar", "44100", "-ac", "2", "-pix_fmt", "yuv420p",
                            str(replay.root / "output.mp4"),
                        ]
                        if attack == "extra":
                            command.extend(["-progress", str(replay.root / "progress")])
                        try:
                            await asyncio.create_subprocess_exec(*command)
                        except StrictReplayViolation:
                            pass
                        self.assertEqual(replay.processes, [])


if __name__ == "__main__":
    unittest.main()
