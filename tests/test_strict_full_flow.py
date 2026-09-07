"""Bounded real character-anchor journey, NOT full E2E or quality evidence."""
from __future__ import annotations

import hashlib
import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import subprocess
import sqlite3
import threading
import unittest
from unittest import mock

from urllib3.util import connection as urllib3_connection
import httpx
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory import (  # noqa: E402
    creative_ir, production_adapters, production_compiler,
    production_config, production_executor, production_graph, production_graph_runner,
)
from tests.strict_replay_support import StrictReplayViolation  # noqa: E402

from tests.strict_handler_support import StrictHandlerReplay  # noqa: E402


class StrictCharacterAnchorJourneyTests(unittest.IsolatedAsyncioTestCase):
    def contract(self):
        return json.loads((ROOT / "tests/fixtures/strict_transport/character_anchor.json").read_text(encoding="utf-8"))

    def replay(self, **kwargs):
        return StrictHandlerReplay(**kwargs)

    def prepare(self, replay, *, budget=10000):
        source = {
            "characters": [{"id": "worker", "name": "Strict Worker",
                            "look": "navy coveralls", "generation_mode": "ai_generated"}],
            "production_settings": {
                "character_anchor_generator": "seedance_2_5",
                "generation_snapshot": production_config.freeze_generation_snapshot({
                    "audio_mode": "seedance_native", "resolution": "480p", "ratio": "1:1",
                    "video_model": "doubao-seedance-2-5-260628", "qa_enabled": True,
                }),
                "budget_cents": budget,
            },
            "beats": [{"id": "yard-1", "scene": "yard", "duration_sec": 4,
                       "cast": ["worker"], "speech_mode": "off_screen",
                       "action": "worker checks machine"}],
        }
        result = production_compiler.compile_ir(
            creative_ir.from_storyboard(source), job_id="J-strict-anchor",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000, runtime_storyboard=source,
        )
        self.assertTrue(result.compilable, result.issues)
        spec = result.spec
        database = replay.database
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,production_spec_json,"
            "production_spec_hash,production_spec_version,execution_engine) "
            "VALUES(?,?,?,'PRODUCTION_RUNNING',?,?,?,2,'graph_v2')",
            (spec.job_id, "T-strict", "Strict anchor", json.dumps(spec.runtime_storyboard),
             json.dumps(spec.as_dict()), spec.spec_hash),
        )
        production_adapters.materialize_frozen_execution_inputs(database, spec)
        production_graph.persist_graph(database, spec)
        database.commit()
        self.assertEqual(database.execute(
            "SELECT count(*) FROM character_identity_anchors"
        ).fetchone()[0], 0)

    async def run_job(self, replay):
        return await production_graph_runner.run_graph_job(
            replay.database, "J-strict-anchor", configured_limit=1,
            handlers=production_adapters.build_handlers(replay.database),
        )

    async def assert_delivered(self, replay, result):
        database = replay.database
        anchor = database.execute("SELECT * FROM character_identity_anchors").fetchone()
        self.assertIsNotNone(anchor, "real handler must persist an anchor")
        self.assertEqual((anchor["state"], anchor["approval_state"]), ("READY", "PENDING_REVIEW"), anchor["error"])
        self.assertEqual(anchor["source_type"], "seedance_identity_anchor")
        video, preview = Path(anchor["video_path"]), Path(anchor["preview_path"] or "")
        self.assertEqual(hashlib.sha256(video.read_bytes()).hexdigest(), replay.media_sha256)
        self.assertTrue(preview.is_file(), "READY requires a real extracted review preview")
        self.assertTrue(preview.read_bytes().startswith(b"\xff\xd8\xff"))
        media = await replay.probe(video)
        self.assertEqual((media["streams"][0]["width"], media["streams"][0]["height"]), (64, 64))
        self.assertEqual(media["streams"][0]["codec_name"], "mpeg4")
        self.assertEqual(len(media["streams"]), 1)
        self.assertAlmostEqual(float(media["format"]["duration"]), 4.0, places=2)
        frame = await replay.probe(preview)
        self.assertEqual((frame["streams"][0]["width"], frame["streams"][0]["height"]), (64, 64))
        self.assertEqual(anchor["cost_cents"], 7)
        self.assertEqual(database.execute("SELECT cost_cents FROM jobs").fetchone()[0], 7)
        calls = database.execute("SELECT * FROM provider_calls").fetchall()
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["task_id"], calls[0]["cost_cents"]), ("strict-anchor-task-1", 7))
        self.assertEqual(json.loads(calls[0]["usage_json"])["status"], "completed")
        node = database.execute(
            "SELECT * FROM production_nodes WHERE node_id='character_anchor:worker'"
        ).fetchone()
        self.assertEqual(node["state"], "REVIEW_REQUIRED")
        self.assertIsNone(node["lease_id"])
        self.assertEqual(json.loads(node["output_json"])["video_path"], str(video))
        events = database.execute(
            "SELECT event,from_state,to_state FROM production_node_events "
            "WHERE node_id='character_anchor:worker' ORDER BY rowid"
        ).fetchall()
        self.assertIn(("start", "READY", "RUNNING"), [tuple(row) for row in events])
        self.assertEqual([row[0] for row in events].count("require_review"), 1)
        self.assertEqual(result.started_nodes, ("character_anchor:worker",))
        self.assertEqual([request["method"] for request in replay.requests],
                         ["POST", "GET", "GET", "GET", "HEAD", "PUT"])
        return (dict(anchor), [tuple(row) for row in calls])

    def assert_no_ready_anchor(self, replay):
        self.assertEqual(replay.database.execute(
            "SELECT count(*) FROM character_identity_anchors WHERE state='READY'"
        ).fetchone()[0], 0)

    async def test_changed_prompt_reference_duration_or_auth_cannot_match(self):
        original = httpx.AsyncClient.build_request
        for mutation in ("prompt", "reference", "duration", "auth"):
            with self.subTest(mutation=mutation):
                def changed(client, *args, **kwargs):
                    request = original(client, *args, **kwargs)
                    if request.method != "POST":
                        return request
                    payload = json.loads(request.content)
                    headers = dict(request.headers)
                    headers.pop("content-length", None)
                    if mutation == "prompt":
                        payload["content"][0]["text"] += " Changed identity."
                    elif mutation == "reference":
                        payload["content"].append({"type": "image_url", "role": "reference_image",
                                                   "image_url": {"url": "https://example.invalid/unapproved.png"}})
                    elif mutation == "duration":
                        payload["duration"] = 8
                    else:
                        headers.pop("authorization", None)
                    return httpx.Request(request.method, request.url, headers=headers, json=payload)

                with self.assertRaisesRegex(StrictReplayViolation, "request mismatch"):
                    with self.replay() as replay, mock.patch.object(httpx.AsyncClient, "build_request", changed):
                        self.prepare(replay)
                        await self.run_job(replay)
                        self.assert_no_ready_anchor(replay)
                        self.assertEqual(replay.requests, [])

    async def test_http_failures_cannot_publish_review_or_repeat_submit(self):
        for stage in ("submit", "download"):
            with self.subTest(stage=stage):
                contract = self.contract()
                index = 0 if stage == "submit" else 3
                contract["steps"] = contract["steps"][:index + 1]
                contract["steps"][index]["response"] = {
                    "status_code": 401 if stage == "submit" else 503,
                    "json": {"error": {"code": "AuthenticationFailed", "message": "authored failure"}},
                }
                with self.replay(contract=contract) as replay:
                    self.prepare(replay)
                    result = await self.run_job(replay)
                    self.assert_no_ready_anchor(replay)
                    self.assertNotEqual(result.job_state, "ANCHOR_REVIEW_REQUIRED")
                    before = len(replay.requests)
                    second = await self.run_job(replay)
                    self.assertEqual(second.started_nodes, ())
                    self.assertEqual(len(replay.requests), before)
                    self.assertEqual([r["method"] for r in replay.requests].count("POST"), 1)
                    if stage == "submit":
                        self.assertEqual(replay.database.execute("SELECT cost_cents FROM jobs").fetchone()[0], 0)

    async def test_budget_or_missing_credentials_block_before_transport(self):
        for reason in ("budget", "credentials"):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(StrictReplayViolation, "unconsumed"):
                    with self.replay() as replay:
                        self.prepare(replay, budget=100 if reason == "budget" else 10000)
                        if reason == "credentials":
                            os.environ.pop("ARK_API_KEY")
                        result = await self.run_job(replay)
                        self.assert_no_ready_anchor(replay)
                        self.assertNotEqual(result.job_state, "ANCHOR_REVIEW_REQUIRED")
                        self.assertEqual(replay.requests, [])
                        self.assertEqual(replay.database.execute("SELECT cost_cents FROM jobs").fetchone()[0], 0)

    async def test_changed_poll_sequence_fails_closed(self):
        contract = self.contract()
        contract["steps"].pop(2)
        with self.assertRaisesRegex(StrictReplayViolation, "request mismatch"):
            with self.replay(contract=contract) as replay:
                self.prepare(replay)
                await self.run_job(replay)
                self.assert_no_ready_anchor(replay)

    async def test_unknown_requests_boundary_is_sticky_after_caller_swallows_it(self):
        with self.assertRaisesRegex(StrictReplayViolation, "TOS authentication mismatch"):
            with self.replay() as replay:
                self.prepare(replay)
                result = await self.run_job(replay)
                await self.assert_delivered(replay, result)
                try:
                    requests.get("https://example.invalid/unrecorded", timeout=1)
                except StrictReplayViolation:
                    pass

    async def test_database_media_paths_and_credentials_are_isolated(self):
        with mock.patch.dict(os.environ, {"UNRELATED_SECRET": "not-a-real-secret"}):
            with self.assertRaisesRegex(StrictReplayViolation, "database outside"):
                with self.replay() as replay:
                    self.assertNotIn("UNRELATED_SECRET", os.environ)
                    self.assertEqual(os.environ["VOLC_AK"], "strict-test-ak")
                    actual_db = Path(replay.database.execute("PRAGMA database_list").fetchone()[2]).resolve()
                    self.assertEqual(actual_db, replay.db_path)
                    self.assertEqual(os.environ["FACTORY_DB_PATH"], str(replay.db_path))
                    outside = replay.root.parent / (replay.root.name + "-outside.db")
                    try:
                        sqlite3.connect(outside)
                    except StrictReplayViolation:
                        pass
                    self.assertFalse(outside.exists())
            self.assertEqual(os.environ["UNRELATED_SECRET"], "not-a-real-secret")

    def test_corrupted_fixture_manifest_is_rejected_before_runtime(self):
        contract = self.contract()
        contract["steps"][3]["response"] = {
            "status_code": 200, "body_base64": "YmFk", "body_sha256": "wrong",
        }
        with self.assertRaisesRegex(ValueError, "pinned SHA256"):
            self.replay(contract=contract)

    async def test_real_graph_anchor_media_cost_review_and_reentry(self):
        with self.replay() as replay:
            self.prepare(replay)
            result = await self.run_job(replay)
            before = await self.assert_delivered(replay, result)
            request_count = len(replay.requests)
            second = await self.run_job(replay)
            self.assertEqual(second.started_nodes, ())
            self.assertEqual(second.job_state, result.job_state)
            self.assertEqual(len(replay.requests), request_count)
            after = (dict(replay.database.execute("SELECT * FROM character_identity_anchors").fetchone()),
                     [tuple(row) for row in replay.database.execute("SELECT * FROM provider_calls")])
            self.assertEqual(before, after)
        self.assertFalse(replay.evidence()["process_tree_verified"])
        self.assertEqual(replay.evidence()["violations"], 0)

    async def test_corrupt_download_cannot_publish_ready_review_anchor(self):
        contract = self.contract()
        corrupt = b"strict authored corrupt media, not an mp4"
        sha256 = hashlib.sha256(corrupt).hexdigest()
        sha1 = hashlib.sha1(corrupt).hexdigest()[:8]
        response = contract["steps"][3]["response"]
        response.pop("media_fixture")
        response.update(body_base64=base64.b64encode(corrupt).decode(), body_sha256=sha256)
        for step in contract["steps"][4:]:
            step["request"]["url"] = step["request"]["url"].replace("fcc3efc2", sha1)
        contract["steps"][5]["request"]["body_sha256"] = sha256
        # CRC64/XZ for the authored corrupt bytes, independently computed.
        contract["steps"][5]["response"]["headers"]["x-tos-hash-crc64ecma"] = "15732344268581664207"
        with self.replay(contract=contract) as replay:
            self.prepare(replay)
            result = await self.run_job(replay)
            anchor = dict(replay.database.execute("SELECT * FROM character_identity_anchors").fetchone())
            calls = [dict(row) for row in replay.database.execute("SELECT * FROM provider_calls")]
            self.assertTrue(replay.processes, anchor["error"])
            self.assertNotEqual(anchor["state"], "READY", {
                "anchor": anchor, "provider_calls": calls, "job_state": result.job_state,
            })
            self.assertEqual(anchor["state"], "FAILED")
            self.assertIn("preview", anchor["error"])
            self.assertNotEqual(result.job_state, "ANCHOR_REVIEW_REQUIRED")
            self.assertNotEqual(replay.database.execute(
                "SELECT state FROM production_nodes WHERE node_id='character_anchor:worker'"
            ).fetchone()[0], "REVIEW_REQUIRED")
            # The remote task was billed: failure must not erase or double it.
            self.assertEqual(anchor["cost_cents"], 7)
            self.assertEqual(calls[0]["cost_cents"], 7)
            self.assertEqual(replay.database.execute("SELECT cost_cents FROM jobs").fetchone()[0], 7)

    async def test_tos_does_not_start_background_dns_or_rewrite_global_transport(self):
        before = urllib3_connection.create_connection
        with mock.patch.object(threading.Thread, "start", side_effect=AssertionError("background thread forbidden")) as start:
            with self.replay() as replay:
                self.prepare(replay)
                result = await self.run_job(replay)
                await self.assert_delivered(replay, result)
                start.assert_not_called()
                self.assertIs(urllib3_connection.create_connection, before)
        self.assertIs(urllib3_connection.create_connection, before)

    async def test_fake_handler_success_is_rejected_by_journey_oracle(self):
        with self.assertRaisesRegex(StrictReplayViolation, "unconsumed"):
            with self.replay() as replay:
                self.prepare(replay)
                handlers = production_adapters.build_handlers(replay.database)
                handlers["character_anchor"] = lambda node: production_executor.NodeOutcome.require_review({
                    "character_id": "worker", "video_path": "pretend.mp4",
                })
                result = await production_graph_runner.run_graph_job(
                    replay.database, "J-strict-anchor", configured_limit=1, handlers=handlers,
                )
                with self.assertRaisesRegex(AssertionError, "real handler must persist an anchor"):
                    await self.assert_delivered(replay, result)
                self.assertEqual(len(replay.requests), 0)

    async def test_missing_contract_request_cannot_be_swallowed_into_success(self):
        contract = self.contract()
        contract["steps"].pop()
        with self.assertRaisesRegex(StrictReplayViolation, "extra request"):
            with self.replay(contract=contract) as replay:
                self.prepare(replay)
                await self.run_job(replay)
                self.assertEqual(replay.database.execute(
                    "SELECT count(*) FROM character_identity_anchors WHERE state='READY'"
                ).fetchone()[0], 0)

    async def test_unknown_async_subprocess_rejected_before_captured_constructor(self):
        original_class = subprocess.Popen
        with mock.patch.object(original_class, "__init__", side_effect=AssertionError("must not launch")) as constructor:
            with self.assertRaisesRegex(StrictReplayViolation, "unknown subprocess executable"):
                with self.replay() as replay:
                    try:
                        await asyncio.create_subprocess_exec(str(replay.root / "not-allowed.exe"))
                    except StrictReplayViolation:
                        pass
            constructor.assert_not_called()

    async def test_remote_media_and_shell_override_are_rejected_before_launch(self):
        for attack in ("remote", "shell", "relative", "environment", "outside"):
            with self.subTest(attack=attack):
                with mock.patch.object(subprocess.Popen, "__init__", side_effect=AssertionError("must not launch")) as constructor:
                    with self.assertRaises(StrictReplayViolation):
                        with self.replay() as replay:
                            args = [str(replay.ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                                    "-ss", "1.000", "-i", str(replay.root / "input.mp4"),
                                    "-frames:v", "1", "-q:v", "2", str(replay.root / "frame.jpg")]
                            kwargs = {}
                            if attack == "remote":
                                args[8] = "https://example.invalid/video.mp4"
                            elif attack == "shell":
                                kwargs["shell"] = True
                            elif attack == "relative":
                                args[0] = "ffmpeg"
                            elif attack == "environment":
                                kwargs["env"] = {"MALICIOUS": "1"}
                            else:
                                args[8] = str(replay.root.parent / "outside.mp4")
                            try:
                                subprocess.Popen(args, **kwargs)
                            except StrictReplayViolation:
                                pass
                    constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
