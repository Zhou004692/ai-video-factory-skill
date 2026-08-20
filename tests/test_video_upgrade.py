"""P0 model-routing and native-audio upgrade tests."""
from pathlib import Path
import asyncio
import inspect
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


from factory import (  # noqa: E402
    character_assets,
    db,
    models,
    orchestrator,
    provider_tasks,
    production_planner,
    prompts,
    scene_assets,
    video_routing,
)
from factory.clients import bigasr, ffmpeg, music, seedance, video_qa  # noqa: E402
from factory.steps import s1_storyboard, s3_shots, s5_bgm, s6_compose  # noqa: E402


class _InjectedFinalConnection:
    def __init__(self, connection, *, fail_sql: str = "", fail_commit: bool = False):
        self.connection = connection
        self.fail_sql = fail_sql
        self.fail_commit = fail_commit
        self.rolled_back = False
        self.closed = False

    def execute(self, sql, parameters=()):
        if self.fail_sql and self.fail_sql in sql:
            raise sqlite3.OperationalError("injected final accounting failure")
        return self.connection.execute(sql, parameters)

    def commit(self):
        if self.fail_commit:
            raise sqlite3.OperationalError("injected final commit failure")
        return self.connection.commit()

    def rollback(self):
        self.rolled_back = True
        return self.connection.rollback()

    def close(self):
        self.closed = True
        return self.connection.close()

    def __getattr__(self, name):
        return getattr(self.connection, name)


class _NonClosingConnection:
    """Keep an in-memory fixture readable after production closes its handle."""

    def __init__(self, connection):
        self.connection = connection

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self.connection, name)


class SeedancePayloadTests(unittest.TestCase):
    def test_account_overdue_submission_is_non_retryable_and_actionable(self):
        response = Mock(
            status_code=403,
            text=(
                '{"error":{"code":"AccountOverdueError","message":'
                '"The account has an overdue balance"}}'
            ),
        )

        error = seedance._submission_error(
            response,
            endpoint="https://ark.example/tasks",
            model="doubao-seedance-2-5-260628",
            content_roles=[],
        )

        self.assertIsInstance(error, seedance.SeedanceAccountUnavailableError)
        self.assertTrue(error.non_retryable)
        self.assertEqual(error.pause_state, "PROVIDER_ACTION_REQUIRED")
        self.assertIn("欠费", str(error))
        self.assertIn("充值", str(error))

    def test_invalid_parameter_submission_is_non_retryable(self):
        response = Mock(
            status_code=400,
            text=(
                '{"error":{"code":"InvalidParameter","message":'
                '"the duration specified in the request is not valid"}}'
            ),
        )

        error = seedance._submission_error(
            response,
            endpoint="https://ark.example/tasks",
            model="doubao-seedance-2-5-260628",
            content_roles=["reference_image"],
        )

        self.assertIsInstance(error, seedance.SeedanceParameterError)
        self.assertTrue(error.non_retryable)
        self.assertIn("请求参数", str(error))

    def test_generation_duration_rejects_invalid_values_before_submission(self):
        for invalid in (0, -1, float("nan"), 30.1):
            with self.subTest(invalid=invalid):
                with self.assertRaises(seedance.SeedanceParameterError):
                    seedance.normalize_generation_duration(invalid)

    def test_transport_disconnect_after_submit_requires_manual_reconciliation(self):
        error = seedance._submission_transport_error(
            seedance.httpx.RemoteProtocolError(
                "Server disconnected without sending a response"
            )
        )

        self.assertIsInstance(error, seedance.SeedanceSubmissionUnknownError)
        self.assertTrue(error.non_retryable)
        self.assertFalse(error.retryable)
        self.assertEqual(error.code, "transient_network")
        self.assertEqual(error.pause_state, "PROVIDER_ACTION_REQUIRED")
        self.assertIn("提交结果未知", str(error))

    def test_output_copyright_policy_error_is_non_retryable_and_readable(self):
        error = seedance._task_failure_error(
            "task-policy",
            {
                "status": "failed",
                "error": {
                    "code": (
                        "OutputVideoSensitiveContentDetected.PolicyViolation"
                    ),
                    "message": "output may be related to copyright restrictions",
                },
            },
        )

        self.assertIsInstance(error, seedance.SeedanceOutputPolicyError)
        self.assertTrue(error.non_retryable)
        self.assertIn("版权或敏感内容策略", str(error))
        self.assertNotIn("{'status'", str(error))

    def test_usage_cost_uses_provider_completion_tokens(self):
        self.assertEqual(
            seedance.usage_cost_cents({"completion_tokens": 260100}),
            1821,
        )

    def test_edit_payload_uses_adaptive_duration_and_mov(self):
        payload = seedance._video_task_payload(
            model="doubao-seedance-2-5-260628",
            content=[{
                "type": "video_url",
                "video_url": {"url": "https://cdn/source.mp4"},
                "role": "reference_video",
            }],
            duration=-1,
            resolution="720p",
            ratio="adaptive",
            camera_fixed=False,
            last_frame=None,
            seed=None,
            motion_strength=None,
            generate_audio=True,
            output_format="mov",
            task_intent="edit",
        )

        self.assertEqual(payload["duration"], -1)
        self.assertEqual(payload["ratio"], "adaptive")
        self.assertEqual(payload["output_format"], "mov")


class MusicFallbackTests(unittest.IsolatedAsyncioTestCase):
    def test_unknown_provider_and_unimplemented_haimian_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "unknown-music"):
            music.resolve_provider(explicit="unknown-music")
        with self.assertRaisesRegex(ValueError, "haimian|海绵"):
            music.estimate_cost_cents(60, provider="haimian")
        self.assertFalse(music.provider_registry()["haimian"]["supported"])
        self.assertIsNone(music.provider_registry()["haimian"]["executor"])

    async def test_suno_task_first_ambiguity_is_persisted_and_never_reposted(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-SUNO-TASK','T-1','suno task','SHOTS_OK','{}')"
        )
        database.commit()
        calls = 0

        async def ambiguous_generate(*args, **kwargs):
            nonlocal calls
            calls += 1
            kwargs["on_task_created"]("suno-remote-1")
            raise music.MusicTaskUnknownError(
                "suno task status unknown", task_id="suno-remote-1"
            )

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            s5_bgm, "conn", return_value=database
        ), patch.object(s5_bgm, "ASSETS_DIR", Path(tmp)), patch.object(
            music, "generate", new=ambiguous_generate
        ):
            with self.assertRaises(music.MusicTaskUnknownError):
                await s5_bgm.run("J-SUNO-TASK", provider="suno")
            with self.assertRaisesRegex(Exception, "核对|task"):
                await s5_bgm.run("J-SUNO-TASK", provider="suno")

        row = database.execute(
            "SELECT task_id,provider,intent,usage_json,cost_cents "
            "FROM provider_calls WHERE task_id='suno-remote-1'"
        ).fetchone()
        self.assertEqual(calls, 1)
        self.assertEqual((row["provider"], row["intent"]), ("suno", "bgm"))
        self.assertEqual(json.loads(row["usage_json"])["status"], "action_required")
        self.assertEqual(row["cost_cents"], 0)
        database.close()
    def test_music_provider_resolver_has_one_explicit_env_file_precedence(self):
        with patch.dict(
            os.environ, {"DEFAULT_MUSIC_PROVIDER": "suno"}, clear=False
        ):
            self.assertEqual(
                music.resolve_provider({"DEFAULT_MUSIC_PROVIDER": "silent"}),
                "suno",
            )
            self.assertEqual(
                music.resolve_provider(
                    {"DEFAULT_MUSIC_PROVIDER": "silent"}, explicit="library"
                ),
                "library",
            )

    async def test_explicit_library_missing_fails_closed_with_actionable_error(self):
        with patch.dict(os.environ, {"DEFAULT_MUSIC_PROVIDER": "library"}), \
                patch.object(
                    music, "_via_library",
                    side_effect=RuntimeError("no bgm in local library"),
                ), patch.object(
                    music, "_via_silent", new=AsyncMock(return_value=b"silent")
                ) as silent:
            with self.assertRaisesRegex(
                music.MusicProviderConfigError, "library|音乐库|mp3"
            ):
                await music.generate("business", 30)

        silent.assert_not_awaited()

    async def test_suno_posts_once_then_polls_and_downloads_the_same_task(self):
        counts = {"post": 0, "poll": 0, "download": 0}
        polled_paths = []

        def handler(request):
            if request.method == "POST":
                counts["post"] += 1
                return httpx.Response(200, json={"id": "suno-task-001"})
            if request.url.host == "api.sunoapi.com":
                counts["poll"] += 1
                polled_paths.append(request.url.path)
                if counts["poll"] == 1:
                    return httpx.Response(200, json={"status": "running"})
                return httpx.Response(200, json={
                    "status": "completed",
                    "data": [{"audio_url": "https://media.example/bgm.mp3"}],
                    "usage": {"credits": 1},
                })
            counts["download"] += 1
            return httpx.Response(200, content=b"paid-bgm")

        real_async_client = httpx.AsyncClient

        def client_factory(*_args, **kwargs):
            return real_async_client(
                transport=httpx.MockTransport(handler),
                headers=kwargs.get("headers"),
            )
        created = []
        updated = []
        with patch.dict(os.environ, {
                    "SUNO_API_KEY": "test-key",
                    "PROVIDER_DOWNLOAD_TRUSTED_HOSTS": "media.example",
                }), \
                patch(
                    "socket.getaddrinfo",
                    return_value=[(2, 1, 6, "", ("8.8.8.8", 443))],
                ), \
                patch.object(
                    music.httpx, "AsyncClient", side_effect=client_factory
                ), \
                patch.object(music.asyncio, "sleep", new=AsyncMock()):
            audio = await music.generate(
                "business", 60, provider="suno",
                on_task_created=created.append,
                on_task_updated=lambda task_id, status, data: updated.append(
                    (task_id, status, data)
                ),
            )

        self.assertEqual(audio, b"paid-bgm")
        self.assertEqual(counts, {"post": 1, "poll": 2, "download": 1})
        self.assertEqual(created, ["suno-task-001"])
        self.assertEqual(
            polled_paths,
            ["/api/v1/suno/task/suno-task-001"] * 2,
        )
        self.assertEqual(updated[-1][0:2], ("suno-task-001", "completed"))

    async def test_suno_post_ambiguity_keeps_gate_closed_for_second_run(self):
        cases = {
            "http_5xx": lambda _request: httpx.Response(503, text="busy"),
            "malformed_2xx": lambda _request: httpx.Response(
                200, content=b"not-json"
            ),
            "missing_task_id": lambda _request: httpx.Response(
                200, json={"data": [{}]}
            ),
            "transport": lambda request: (_ for _ in ()).throw(
                httpx.ConnectError("connection reset", request=request)
            ),
            "cancelled": lambda _request: (_ for _ in ()).throw(
                asyncio.CancelledError()
            ),
        }
        for suffix, response in cases.items():
            with self.subTest(case=suffix):
                database = sqlite3.connect(":memory:")
                database.row_factory = sqlite3.Row
                database.executescript(db.DDL)
                job_id = f"J-SUNO-{suffix.upper()}"
                database.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
                    "VALUES(?, 'T-1', 'suno ambiguity', 'SHOTS_OK', '{}')",
                    (job_id,),
                )
                database.commit()
                posts = 0

                def handler(request):
                    nonlocal posts
                    posts += 1
                    return response(request)

                real_async_client = httpx.AsyncClient

                def client_factory(*_args, **_kwargs):
                    return real_async_client(
                        transport=httpx.MockTransport(handler)
                    )

                with tempfile.TemporaryDirectory() as tmp, patch.dict(
                    os.environ, {"SUNO_API_KEY": "test-key"}
                ), patch.object(
                    s5_bgm, "conn", return_value=database
                ), patch.object(
                    s5_bgm, "ASSETS_DIR", Path(tmp)
                ), patch.object(
                    music.httpx, "AsyncClient", side_effect=client_factory
                ):
                    with self.assertRaises(BaseException):
                        await s5_bgm.run(job_id, provider="suno")
                    with self.assertRaises(provider_tasks.ProviderTaskGateError):
                        await s5_bgm.run(job_id, provider="suno")

                row = database.execute(
                    "SELECT task_id,usage_json FROM provider_calls"
                ).fetchone()
                self.assertEqual(posts, 1)
                self.assertTrue(row["task_id"].startswith("local-submit:"))
                self.assertEqual(
                    json.loads(row["usage_json"])["status"],
                    "action_required",
                )
                database.close()

    async def test_suno_pre_submit_configuration_failure_releases_reservation(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-SUNO-PREFLIGHT','T-1','preflight','SHOTS_OK','{}')"
        )
        database.commit()
        with patch.dict(os.environ, {}, clear=True), patch.object(
            s5_bgm, "conn", return_value=database
        ):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "SUNO_API_KEY"):
                    await s5_bgm.run("J-SUNO-PREFLIGHT", provider="suno")

        self.assertEqual(
            database.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0],
            0,
        )
        database.close()

    async def test_s5_completed_bgm_cost_and_artifact_are_idempotent(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-SUNO-DONE','T-1','done','SHOTS_OK','{}',0)"
        )
        database.commit()
        counts = {"post": 0, "poll": 0, "download": 0}

        def handler(request):
            if request.method == "POST":
                counts["post"] += 1
                return httpx.Response(200, json={"id": "suno-done-1"})
            if request.url.host == "api.sunoapi.com":
                counts["poll"] += 1
                return httpx.Response(200, json={
                    "status": "completed",
                    "data": [{"audio_url": "https://media.example/done.mp3"}],
                    "usage": {"credits": 1},
                })
            counts["download"] += 1
            return httpx.Response(200, content=b"completed-bgm")

        real_async_client = httpx.AsyncClient

        def client_factory(*_args, **_kwargs):
            return real_async_client(transport=httpx.MockTransport(handler))

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {
                "SUNO_API_KEY": "test-key",
                "PROVIDER_DOWNLOAD_TRUSTED_HOSTS": "media.example",
            }
        ), patch(
            "socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("8.8.8.8", 443))],
        ), patch.object(
            s5_bgm, "conn", return_value=database
        ), patch.object(
            s5_bgm, "ASSETS_DIR", Path(tmp)
        ), patch.object(
            music.httpx, "AsyncClient", side_effect=client_factory
        ), patch.object(music.asyncio, "sleep", new=AsyncMock()):
            await s5_bgm.run("J-SUNO-DONE", provider="suno")
            await s5_bgm.run("J-SUNO-DONE", provider="suno")

            bgm = Path(tmp) / "J-SUNO-DONE" / "bgm.mp3"
            self.assertEqual(bgm.read_bytes(), b"completed-bgm")

        call = database.execute(
            "SELECT cost_cents,usage_json FROM provider_calls "
            "WHERE task_id='suno-done-1'"
        ).fetchone()
        job = database.execute(
            "SELECT cost_cents,bgm_path FROM jobs WHERE job_id='J-SUNO-DONE'"
        ).fetchone()
        self.assertEqual(counts, {"post": 1, "poll": 1, "download": 1})
        self.assertEqual(call["cost_cents"], 20)
        self.assertEqual(json.loads(call["usage_json"])["status"], "completed")
        self.assertEqual(job["cost_cents"], 20)
        self.assertEqual(job["bgm_path"], str(Path(tmp) / "J-SUNO-DONE" / "bgm.mp3"))
        database.close()

    async def test_suno_known_task_poll_or_download_failure_never_reposts(self):
        for phase in ("poll", "download"):
            with self.subTest(phase=phase):
                database = sqlite3.connect(":memory:")
                database.row_factory = sqlite3.Row
                database.executescript(db.DDL)
                job_id = f"J-SUNO-{phase.upper()}-FAIL"
                database.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
                    "VALUES(?, 'T-1', 'known task', 'SHOTS_OK', '{}')",
                    (job_id,),
                )
                database.commit()
                counts = {"post": 0, "poll": 0, "download": 0}

                def handler(request):
                    if request.method == "POST":
                        counts["post"] += 1
                        return httpx.Response(200, json={"id": f"task-{phase}"})
                    if request.url.host == "api.sunoapi.com":
                        counts["poll"] += 1
                        if phase == "poll":
                            return httpx.Response(503, text="poll unavailable")
                        return httpx.Response(200, json={
                            "status": "completed",
                            "data": [{
                                "audio_url": "https://media.example/fail.mp3"
                            }],
                        })
                    counts["download"] += 1
                    return httpx.Response(503, text="download unavailable")

                real_async_client = httpx.AsyncClient

                def client_factory(*_args, **_kwargs):
                    return real_async_client(
                        transport=httpx.MockTransport(handler)
                    )

                with tempfile.TemporaryDirectory() as tmp, patch.dict(
                    os.environ, {
                        "SUNO_API_KEY": "test-key",
                        "PROVIDER_DOWNLOAD_TRUSTED_HOSTS": "media.example",
                    }
                ), patch(
                    "socket.getaddrinfo",
                    return_value=[(2, 1, 6, "", ("8.8.8.8", 443))],
                ), patch.object(
                    s5_bgm, "conn", return_value=database
                ), patch.object(
                    s5_bgm, "ASSETS_DIR", Path(tmp)
                ), patch.object(
                    music.httpx, "AsyncClient", side_effect=client_factory
                ):
                    with self.assertRaises(music.MusicTaskUnknownError):
                        await s5_bgm.run(job_id, provider="suno")
                    with self.assertRaises(provider_tasks.ProviderTaskGateError):
                        await s5_bgm.run(job_id, provider="suno")

                row = database.execute(
                    "SELECT task_id,usage_json FROM provider_calls"
                ).fetchone()
                self.assertEqual(counts["post"], 1)
                self.assertEqual(row["task_id"], f"task-{phase}")
                self.assertEqual(
                    json.loads(row["usage_json"])["status"],
                    "action_required",
                )
                database.close()

    async def test_s5_publish_db_failure_keeps_cost_journal_and_blocks_repost(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-SUNO-PUBLISH-DB','T-1','publish db','SHOTS_OK','{}',0)"
        )
        database.execute(
            "CREATE TRIGGER fail_suno_publish_complete "
            "BEFORE UPDATE OF usage_json ON provider_calls "
            "WHEN json_extract(NEW.usage_json, '$.status')='completed' "
            "BEGIN SELECT RAISE(ABORT,'suno publish finalize failed'); END"
        )
        database.commit()
        posts = 0

        def handler(request):
            nonlocal posts
            if request.method == "POST":
                posts += 1
                return httpx.Response(200, json={"id": "suno-publish-db-1"})
            if request.url.host == "api.sunoapi.com":
                return httpx.Response(200, json={
                    "status": "completed",
                    "data": [{"audio_url": "https://media.example/db.mp3"}],
                    "usage": {"credits": 1},
                })
            return httpx.Response(200, content=b"recoverable-bgm")

        real_async_client = httpx.AsyncClient

        def client_factory(*_args, **_kwargs):
            return real_async_client(transport=httpx.MockTransport(handler))

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {
                "SUNO_API_KEY": "test-key",
                "PROVIDER_DOWNLOAD_TRUSTED_HOSTS": "media.example",
            }
        ), patch(
            "socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("8.8.8.8", 443))],
        ), patch.object(
            s5_bgm, "conn", return_value=database
        ), patch.object(
            s5_bgm, "ASSETS_DIR", Path(tmp)
        ), patch.object(
            music.httpx, "AsyncClient", side_effect=client_factory
        ):
            with self.assertRaisesRegex(
                music.MusicTaskUnknownError, "暂存|禁止重新提交"
            ):
                await s5_bgm.run("J-SUNO-PUBLISH-DB", provider="suno")
            with self.assertRaises(provider_tasks.ProviderTaskGateError):
                await s5_bgm.run("J-SUNO-PUBLISH-DB", provider="suno")

            target = Path(tmp) / "J-SUNO-PUBLISH-DB" / "bgm.mp3"
            self.assertEqual(target.read_bytes(), b"recoverable-bgm")
            call = database.execute(
                "SELECT cost_cents,usage_json FROM provider_calls "
                "WHERE task_id='suno-publish-db-1'"
            ).fetchone()
            job = database.execute(
                "SELECT state,cost_cents FROM jobs "
                "WHERE job_id='J-SUNO-PUBLISH-DB'"
            ).fetchone()
            usage = json.loads(call["usage_json"])
            self.assertEqual(posts, 1)
            self.assertEqual(call["cost_cents"], 20)
            self.assertEqual(job["cost_cents"], 20)
            self.assertEqual(job["state"], "PROVIDER_ACTION_REQUIRED")
            self.assertEqual(usage["status"], "action_required")
            self.assertTrue(Path(usage["publish_staging_path"]).is_file())
        database.close()

    async def test_suno_promotion_failure_persists_remote_id_for_reconcile(self):
        from factory.management_api import ManagementService

        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        job_id = "J-SUNO-PROMOTE-FAIL"
        remote_id = "suno-promote-fail-1"
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES(?, 'T-1', 'promote fail', 'SHOTS_OK', '{}', 0)",
            (job_id,),
        )
        database.execute(
            "CREATE TRIGGER fail_remote_promotion BEFORE INSERT ON provider_calls "
            "WHEN NEW.task_id='suno-promote-fail-1' "
            "BEGIN SELECT RAISE(ABORT,'remote insert failed'); END"
        )
        database.commit()
        posts = 0

        def handler(request):
            nonlocal posts
            posts += 1
            return httpx.Response(200, json={"id": remote_id})

        real_async_client = httpx.AsyncClient

        def client_factory(*_args, **_kwargs):
            return real_async_client(transport=httpx.MockTransport(handler))

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"SUNO_API_KEY": "test-key"}
        ), patch.object(
            s5_bgm, "conn", return_value=database
        ), patch.object(
            s5_bgm, "ASSETS_DIR", Path(tmp)
        ), patch.object(
            music.httpx, "AsyncClient", side_effect=client_factory
        ):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "remote insert"):
                await s5_bgm.run(job_id, provider="suno")
            with self.assertRaises(provider_tasks.ProviderTaskGateError):
                await s5_bgm.run(job_id, provider="suno")

            row = database.execute(
                "SELECT task_id,provider,intent,usage_json FROM provider_calls"
            ).fetchone()
            usage = json.loads(row["usage_json"])
            self.assertEqual(posts, 1)
            self.assertTrue(row["task_id"].startswith("local-submit:"))
            self.assertEqual((row["provider"], row["intent"]), ("suno", "bgm"))
            self.assertEqual(usage["status"], "action_required")
            self.assertEqual(usage["remote_task_id"], remote_id)
            self.assertEqual(usage["provider"], "suno")
            self.assertEqual(usage["intent"], "bgm")

            database.execute("DROP TRIGGER fail_remote_promotion")
            database.execute(
                "UPDATE jobs SET state='PROVIDER_ACTION_REQUIRED' WHERE job_id=?",
                (job_id,),
            )
            database.commit()
            restarted = ManagementService(database, Path(tmp))
            unresolved = next(
                item for item in restarted.videos()["jobs"]
                if item["job_id"] == job_id
            )["unresolved_provider_tasks"]
            self.assertEqual(unresolved[0]["task_id"], remote_id)

            inspect_result = {
                "status": "failed", "usage": {}, "cost_cents": 20,
            }
            with patch(
                "factory.management_api.music.inspect_task",
                new=Mock(return_value=object()),
            ) as inspect, patch(
                "factory.management_api.asyncio.run",
                return_value=inspect_result,
            ):
                reconciled = restarted.reconcile_provider_task(job_id, remote_id)
            inspect.assert_called_once_with(remote_id, provider="suno")
            self.assertEqual(reconciled["remote_status"], "failed")
            self.assertIsNone(database.execute(
                "SELECT 1 FROM provider_calls WHERE task_id LIKE 'local-submit:%'"
            ).fetchone())
            self.assertEqual(database.execute(
                "SELECT cost_cents FROM provider_calls WHERE task_id=?",
                (remote_id,),
            ).fetchone()[0], 20)
        database.close()

    async def test_suno_download_clients_never_forward_provider_authorization(self):
        for operation in ("generate", "inspect"):
            with self.subTest(operation=operation):
                seen_download_headers = []

                def handler(request):
                    if request.url.host == "api.sunoapi.com":
                        return httpx.Response(200, json={
                            "id": "suno-headerless-1",
                            "status": "completed",
                            "data": [{
                                "audio_url": "https://evil.example/audio.mp3"
                            }],
                        })
                    seen_download_headers.append(dict(request.headers))
                    return httpx.Response(200, content=b"audio")

                real_async_client = httpx.AsyncClient

                def client_factory(*_args, **kwargs):
                    return real_async_client(
                        transport=httpx.MockTransport(handler),
                        headers=kwargs.get("headers"),
                    )

                with patch.dict(os.environ, {
                    "SUNO_API_KEY": "secret-key",
                    "PROVIDER_DOWNLOAD_TRUSTED_HOSTS": "evil.example",
                }), patch(
                    "socket.getaddrinfo",
                    return_value=[(2, 1, 6, "", ("8.8.8.8", 443))],
                ), patch.object(
                    music.httpx, "AsyncClient", side_effect=client_factory
                ):
                    if operation == "generate":
                        audio = await music.generate(
                            "business", 60, provider="suno"
                        )
                    else:
                        inspected = await music.inspect_task(
                            "suno-headerless-1", provider="suno"
                        )
                        audio = inspected["audio"]

                self.assertEqual(audio, b"audio")
                self.assertEqual(len(seen_download_headers), 1)
                lowered = {
                    str(key).casefold(): value
                    for key, value in seen_download_headers[0].items()
                }
                self.assertNotIn("authorization", lowered)

    async def test_suno_dual_stack_download_failover_never_reposts(self):
        counts = {"post": 0, "download": 0}
        attempted_hosts = []

        def handler(request):
            if request.url.host == "api.sunoapi.com":
                counts["post"] += int(request.method == "POST")
                return httpx.Response(200, json={
                    "id": "suno-dual-stack-1",
                    "status": "completed",
                    "data": [{
                        "audio_url": "https://dual.example/audio.mp3"
                    }],
                })
            counts["download"] += 1
            attempted_hosts.append(request.url.host)
            if ":" in request.url.host:
                raise httpx.ConnectError(
                    "ipv6 unavailable", request=request
                )
            return httpx.Response(200, content=b"audio-via-ipv4")

        real_async_client = httpx.AsyncClient

        def client_factory(*_args, **kwargs):
            return real_async_client(
                transport=httpx.MockTransport(handler),
                headers=kwargs.get("headers"),
            )

        with patch.dict(os.environ, {"SUNO_API_KEY": "test-key"}), patch(
            "socket.getaddrinfo",
            return_value=[
                (10, 1, 6, "", ("2001:4860:4860::8888", 443, 0, 0)),
                (2, 1, 6, "", ("8.8.8.8", 443)),
            ],
        ), patch.object(
            music.httpx, "AsyncClient", side_effect=client_factory
        ):
            audio = await music.generate(
                "business", 60, provider="suno"
            )

        self.assertEqual(audio, b"audio-via-ipv4")
        self.assertEqual(counts, {"post": 1, "download": 2})
        self.assertEqual(attempted_hosts, [
            "2001:4860:4860::8888", "8.8.8.8",
        ])

    async def test_suno_rejects_unsafe_download_urls_before_request(self):
        unsafe_cases = (
            "http://public.example/audio.mp3",
            "https://localhost/audio.mp3",
            "https://127.0.0.1/audio.mp3",
            "https://10.0.0.8/audio.mp3",
            "https://169.254.1.2/audio.mp3",
        )
        for unsafe_url in unsafe_cases:
            with self.subTest(url=unsafe_url):
                downloads = 0

                def handler(request):
                    nonlocal downloads
                    if request.url.host == "api.sunoapi.com":
                        return httpx.Response(200, json={
                            "id": "suno-unsafe-1",
                            "status": "completed",
                            "data": [{"audio_url": unsafe_url}],
                        })
                    downloads += 1
                    return httpx.Response(200, content=b"must-not-download")

                real_async_client = httpx.AsyncClient

                def client_factory(*_args, **kwargs):
                    return real_async_client(
                        transport=httpx.MockTransport(handler),
                        headers=kwargs.get("headers"),
                    )

                with patch.dict(
                    os.environ, {"SUNO_API_KEY": "secret-key"}, clear=True
                ), patch.object(
                    music.httpx, "AsyncClient", side_effect=client_factory
                ):
                    with self.assertRaisesRegex(ValueError, "HTTPS|public|安全"):
                        await music.generate("business", 60, provider="suno")
                self.assertEqual(downloads, 0)

    async def test_suno_rejects_private_dns_and_unsafe_redirect_hops(self):
        for mode in ("private_dns", "redirect"):
            with self.subTest(mode=mode):
                secret_requests = 0

                def handler(request):
                    nonlocal secret_requests
                    if request.url.host == "api.sunoapi.com":
                        url = (
                            "https://dns-private.example/audio.mp3"
                            if mode == "private_dns"
                            else "https://evil.example/start"
                        )
                        return httpx.Response(200, json={
                            "id": "suno-hop-1", "status": "completed",
                            "data": [{"audio_url": url}],
                        })
                    if request.url.host in {"127.0.0.1", "localhost"}:
                        secret_requests += 1
                        return httpx.Response(200, content=b"secret")
                    return httpx.Response(
                        302, headers={"location": "https://127.0.0.1/secret"}
                    )

                real_async_client = httpx.AsyncClient

                def client_factory(*_args, **kwargs):
                    return real_async_client(
                        transport=httpx.MockTransport(handler),
                        headers=kwargs.get("headers"),
                    )

                env = {"SUNO_API_KEY": "secret-key"}
                if mode == "redirect":
                    env["PROVIDER_DOWNLOAD_TRUSTED_HOSTS"] = "evil.example"
                def dns_result(host, port, **_kwargs):
                    address = (
                        "10.2.3.4" if host == "dns-private.example"
                        else "8.8.8.8"
                    )
                    return [(2, 1, 6, "", (address, port))]

                with patch.dict(os.environ, env, clear=True), patch(
                    "socket.getaddrinfo", side_effect=dns_result,
                ), patch.object(
                    music.httpx, "AsyncClient", side_effect=client_factory
                ):
                    with self.assertRaisesRegex(ValueError, "public|安全"):
                        await music.generate("business", 60, provider="suno")
                self.assertEqual(secret_requests, 0)

    def test_edit_contract_rejects_fixed_ratio_before_api_call(self):
        with self.assertRaisesRegex(ValueError, "ratio='adaptive'"):
            seedance._video_task_payload(
                model="doubao-seedance-2-5-260628",
                content=[{
                    "type": "video_url",
                    "video_url": {"url": "https://cdn/source.mp4"},
                    "role": "reference_video",
                }],
                duration=-1,
                resolution="720p",
                ratio="9:16",
                camera_fixed=False,
                last_frame=None,
                seed=None,
                motion_strength=None,
                generate_audio=True,
                output_format="mov",
                task_intent="edit",
            )

    def test_edit_contract_requires_reference_media_before_api_call(self):
        with self.assertRaisesRegex(ValueError, "reference_video or reference_audio"):
            seedance._video_task_payload(
                model="doubao-seedance-2-5-260628",
                content=[{"type": "text", "text": "edit this"}],
                duration=-1,
                resolution="720p",
                ratio="adaptive",
                camera_fixed=False,
                last_frame=None,
                seed=None,
                motion_strength=None,
                generate_audio=True,
                output_format="mov",
                task_intent="edit",
            )

    def test_seedance25_accepts_official_reference_limits(self):
        seedance._validate_reference_limits(
            "doubao-seedance-2-5-260628",
            image_count=30,
            video_count=10,
            audio_count=10,
        )

    def test_seedance25_rejects_more_than_fifty_references(self):
        with self.assertRaisesRegex(ValueError, "reference_image.*max 30"):
            seedance._validate_reference_limits(
                "doubao-seedance-2-5-260628",
                image_count=31,
                video_count=10,
                audio_count=10,
            )

    def test_seedance25_rejects_duration_over_live_api_maximum(self):
        with self.assertRaisesRegex(ValueError, "maximum is 30 seconds"):
            seedance._video_task_payload(
                model="doubao-seedance-2-5-260628",
                content=[{"type": "text", "text": "test"}],
                duration=36,
                resolution="720p",
                ratio="9:16",
                camera_fixed=False,
                last_frame=None,
                seed=None,
                motion_strength=None,
                generate_audio=True,
            )

    def test_seedance25_pads_short_editorial_duration_before_submission(self):
        payload = seedance._video_task_payload(
            model="doubao-seedance-2-5-260628",
            content=[{"type": "text", "text": "test"}],
            duration=0.4,
            resolution="720p",
            ratio="9:16",
            camera_fixed=False,
            last_frame=None,
            seed=None,
            motion_strength=None,
            generate_audio=True,
        )

        self.assertEqual(payload["duration"], 4)

    def test_seedance25_rounds_fractional_duration_up_not_down(self):
        payload = seedance._video_task_payload(
            model="doubao-seedance-2-5-260628",
            content=[{"type": "text", "text": "test"}],
            duration=7.33,
            resolution="720p",
            ratio="9:16",
            camera_fixed=False,
            last_frame=None,
            seed=None,
            motion_strength=None,
            generate_audio=True,
        )

        self.assertEqual(payload["duration"], 8)

    def test_seedance25_rejects_unsupported_1080p_output(self):
        with self.assertRaisesRegex(ValueError, "480p or 720p"):
            seedance._video_task_payload(
                model="doubao-seedance-2-5-260628",
                content=[{"type": "text", "text": "test"}],
                duration=8,
                resolution="1080p",
                ratio="9:16",
                camera_fixed=False,
                last_frame=None,
                seed=None,
                motion_strength=None,
                generate_audio=True,
            )

    def test_local_image_accepts_path_objects(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            frame = Path(tmp) / "frame.png"
            frame.write_bytes(b"png")

            encoded = seedance._to_image_arg(frame)

        self.assertTrue(encoded.startswith("data:image/png;base64,"))

    def test_native_audio_uses_official_generate_audio_field(self):
        self.assertTrue(hasattr(seedance, "_video_task_payload"))
        payload = seedance._video_task_payload(
            model="doubao-seedance-test",
            content=[{"type": "text", "text": "test"}],
            duration=4,
            resolution="480p",
            ratio="9:16",
            camera_fixed=False,
            last_frame=None,
            seed=None,
            motion_strength=0.4,
            generate_audio=True,
        )

        self.assertTrue(payload["generate_audio"])
        self.assertNotIn("audio_output", payload)
        self.assertEqual(payload["model"], "doubao-seedance-test")

    def test_reference_wav_uses_wav_data_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "anchor.wav"
            audio.write_bytes(b"wav")
            content = []

            seedance._push_audio(content, "reference_audio", str(audio))

        self.assertTrue(
            content[0]["audio_url"]["url"].startswith(
                "data:audio/wav;base64,"
            )
        )

    def test_fast_t2v_omits_unsupported_camera_fixed_parameter(self):
        payload = seedance._video_task_payload(
            model="doubao-seedance-2-0-fast-260128",
            content=[{"type": "text", "text": "test"}],
            duration=4,
            resolution="480p",
            ratio="9:16",
            camera_fixed=False,
            last_frame=None,
            seed=None,
            motion_strength=0.25,
            generate_audio=True,
        )

        self.assertNotIn("camera_fixed", payload)

    def test_full_t2v_omits_unsupported_camera_fixed_parameter(self):
        payload = seedance._video_task_payload(
            model="doubao-seedance-2-0-260128",
            content=[{"type": "text", "text": "test"}],
            duration=4,
            resolution="480p",
            ratio="9:16",
            camera_fixed=True,
            last_frame=None,
            seed=None,
            motion_strength=0.25,
            generate_audio=True,
        )

        self.assertNotIn("camera_fixed", payload)

    def test_full_i2v_omits_unsupported_camera_fixed_parameter(self):
        payload = seedance._video_task_payload(
            model="doubao-seedance-2-0-260128",
            content=[
                {"type": "text", "text": "test"},
                {
                    "type": "image_url",
                    "role": "first_frame",
                    "image_url": {"url": "https://example.com/frame.png"},
                },
            ],
            duration=4,
            resolution="480p",
            ratio="9:16",
            camera_fixed=True,
            last_frame=None,
            seed=None,
            motion_strength=0.25,
            generate_audio=True,
        )

        self.assertNotIn("camera_fixed", payload)

    def test_seedance25_first_frame_omits_ratio_and_camera_fixed(self):
        payload = seedance._video_task_payload(
            model="doubao-seedance-2-5-260628",
            content=[
                {"type": "text", "text": "test"},
                {
                    "type": "image_url",
                    "role": "first_frame",
                    "image_url": {"url": "https://example.com/frame.png"},
                },
            ],
            duration=4,
            resolution="720p",
            ratio="9:16",
            camera_fixed=True,
            last_frame=None,
            seed=None,
            motion_strength=0.4,
            generate_audio=True,
        )

        self.assertNotIn("ratio", payload)
        self.assertNotIn("camera_fixed", payload)


class ObservabilitySchemaTests(unittest.TestCase):
    def test_shot_schema_records_model_native_audio_and_qa(self):
        self.assertTrue(hasattr(db, "SHOT_OBSERVABILITY_COLUMNS"))
        names = {name for name, _ in db.SHOT_OBSERVABILITY_COLUMNS}
        self.assertEqual(
            {"video_model", "native_audio", "video_qa"} - names,
            set(),
        )

    def test_provider_call_cost_is_idempotent_by_task_id(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        calls = ({
            "task_id": "cgt-recovered-001",
            "intent": "generate",
            "cost_cents": 1482,
            "usage": {"completion_tokens": 211765},
        },)

        first = s3_shots._record_provider_calls(
            memory_db, "J-TEST", 2, "seedance", calls
        )
        second = s3_shots._record_provider_calls(
            memory_db, "J-TEST", 2, "seedance", calls
        )

        self.assertEqual(first, 1482)
        self.assertEqual(second, 0)
        rows = memory_db.execute(
            "SELECT task_id, cost_cents FROM provider_calls"
        ).fetchall()
        self.assertEqual(
            [(row["task_id"], row["cost_cents"]) for row in rows],
            [("cgt-recovered-001", 1482)],
        )

    def test_distinct_provider_tasks_are_billed_separately(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.executescript(db.DDL)

        first = s3_shots._record_provider_calls(
            memory_db,
            "J-TEST",
            1,
            "seedance",
            ({"task_id": "cgt-001", "cost_cents": 406},),
        )
        second = s3_shots._record_provider_calls(
            memory_db,
            "J-TEST",
            2,
            "seedance",
            ({"task_id": "cgt-002", "cost_cents": 1482},),
        )

        self.assertEqual(first + second, 1888)


class VideoRoutingTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "ARK_VIDEO_PRIMARY_MODEL": "doubao-seedance-2-5-260628",
        }

    def test_broll_keeps_seedance_native_ambient_audio(self):
        self.assertTrue(hasattr(video_routing, "select_route"))
        route = video_routing.select_route(
            {"narrative_type": "broll"}, {}, env=self.env
        )

        self.assertEqual(route.model, "doubao-seedance-2-5-260628")
        self.assertEqual(route.tier, "seedance25")
        self.assertTrue(route.generate_audio)
        self.assertTrue(route.qa_required)
        self.assertIsNone(route.fallback_model)

    def test_scene_dialogue_uses_external_fixed_voice(self):
        self.assertTrue(hasattr(video_routing, "select_route"))
        route = video_routing.select_route(
            {"narrative_type": "dialogue"},
            {"render_mode": "scene", "audio_mode": "fixed_tts"},
            env=self.env,
        )

        self.assertEqual(route.model, "doubao-seedance-2-5-260628")
        self.assertFalse(route.generate_audio)
        self.assertTrue(route.qa_required)

    def test_scene_dialogue_can_use_seedance_native_voice(self):
        route = video_routing.select_route(
            {"narrative_type": "dialogue"},
            {"render_mode": "scene", "audio_mode": "seedance_native"},
            env=self.env,
        )

        self.assertTrue(route.generate_audio)

    def test_digital_human_dialogue_uses_external_fixed_voice(self):
        self.assertTrue(hasattr(video_routing, "select_route"))
        route = video_routing.select_route(
            {"narrative_type": "dialogue"},
            {
                "render_mode": "digital_human",
                "native_audio": False,
                "audio_mode": "fixed_tts",
            },
            env=self.env,
        )

        self.assertEqual(route.model, "doubao-seedance-2-5-260628")
        self.assertFalse(route.generate_audio)

    def test_precision_gauge_motion_is_rejected_before_generation(self):
        reason = video_routing.precision_motion_reason({
            "scene": "施工机械液压压力表特写",
            "scene_dynamics": "红色指针逆时针回落到 2 MPa",
        })

        self.assertIsNotNone(reason)
        self.assertIn("指针", reason)

    def test_absolute_object_direction_is_rejected(self):
        reason = video_routing.precision_motion_reason({
            "scene": "三名工人抬着钢管",
            "scene_dynamics": "工人缓慢向画面右侧行走",
        })

        self.assertIsNotNone(reason)
        self.assertIn("方向", reason)

    def test_camera_pan_direction_remains_eligible(self):
        reason = video_routing.precision_motion_reason({
            "prompt": "镜头向右平移，工人自然经过施工现场",
        })

        self.assertIsNone(reason)

    def test_natural_broll_motion_remains_eligible(self):
        reason = video_routing.precision_motion_reason({
            "scene": "施工机械液压管路特写",
            "scene_dynamics": "管路轻微振动，浮尘缓慢飘动",
        })

        self.assertIsNone(reason)

    def test_static_numbers_do_not_block_camera_motion(self):
        reason = video_routing.precision_motion_reason({
            "scene": "设备铭牌上有静态数字",
            "prompt": "镜头从前景缓慢移到背景",
            "scene_dynamics": "浮尘缓慢飘动",
        })

        self.assertIsNone(reason)


class StoryboardPromptSafetyTests(unittest.TestCase):
    def test_roster_summary_exposes_fixed_character_reference_image(self):
        summary = prompts.character_roster_summary()

        self.assertIn("sales_xiaoneng", summary)
        self.assertIn(
            "references/characters/sales_xiaoneng/canonical-v1.png", summary
        )

    def test_storyboard_requires_visible_natural_motion(self):
        prompt = prompts.storyboard_prompt(
            title="设备场地日常",
            track="工程设备",
            duration=30,
        )

        self.assertIn("中等尺度、清晰可见的自然运动", prompt)
        self.assertIn("不能只写浮尘、光影、云层或树叶轻动", prompt)

    def test_storyboard_prioritizes_one_primary_motion(self):
        prompt = prompts.storyboard_prompt(
            title="设备场地日常",
            track="工程设备",
            duration=30,
        )

        self.assertIn("有且只有一个优先级最高的中等尺度主动态", prompt)
        self.assertIn("其他背景动作只作可选增强", prompt)

    def test_storyboard_avoids_precision_motion_for_generative_video(self):
        prompt = prompts.storyboard_prompt(
            title="液压设备检查",
            track="工程设备",
            duration=30,
        )

        self.assertIn("精密视觉动作", prompt)
        self.assertIn("不得把仪表指针", prompt)
        self.assertIn("人物和车辆不指定向左/向右", prompt)
        self.assertNotIn("仪表指针缓慢回落", prompt)

    def test_end_frame_is_optional_and_limited_to_natural_transitions(self):
        prompt = prompts.storyboard_prompt(
            title="设备巡检",
            track="工程设备",
            duration=30,
        )

        self.assertIn("`end_scene` 可选", prompt)
        self.assertIn("自然状态过渡", prompt)


class StoryboardLongSceneTests(unittest.TestCase):
    def test_asset_backed_virtual_portrait_does_not_require_duplicate_look(self):
        storyboard = self._storyboard(20)
        storyboard["characters"] = [{
            "id": "virtual_host",
            "name": "Virtual Host",
            "identity_type": "virtual_person",
            "seedance_asset_uri": "asset://asset-virtual-host-01",
        }]
        for shot in storyboard["shots"]:
            shot["speaker"] = "virtual_host"

        s1_storyboard._validate(storyboard)

    @staticmethod
    def _storyboard(duration: int) -> dict:
        return {
            "characters": [{
                "id": "host", "name": "主持人", "gender": "female",
                "look": "完全虚构的成年女性主持人",
            }],
            "hook": "完整讲清一个重点",
            "bgm": "克制的轻音乐",
            "shots": [
                {
                    "idx": 1, "type": "dialogue", "speaker": "host",
                    "render_mode": "presenter", "duration": duration,
                    "scene": "明亮工作室", "action": "自然讲解并轻微点头",
                    "voiceover": "这是一段用于验证长镜头能力的完整讲解台词。",
                },
                {
                    "idx": 2, "type": "dialogue", "speaker": "host",
                    "render_mode": "presenter", "duration": 4,
                    "scene": "同一工作室", "action": "自然收束",
                    "voiceover": "讲解到这里结束。",
                },
            ],
        }

    def test_long_dialogue_is_enabled_by_default_after_live_validation(self):
        with patch.dict("os.environ", {}, clear=True):
            s1_storyboard._validate(self._storyboard(20))

    def test_legacy_long_scene_toggle_does_not_restore_a_hard_limit(self):
        with patch.dict(
            "os.environ", {"SEEDANCE25_LONG_SCENE_ENABLED": "false"}, clear=True
        ):
            s1_storyboard._validate(self._storyboard(20))

    def test_long_dialogue_allows_up_to_thirty_seconds_when_enabled(self):
        with patch.dict(
            "os.environ", {"SEEDANCE25_LONG_SCENE_ENABLED": "true"}, clear=True
        ):
            s1_storyboard._validate(self._storyboard(20))

    def test_long_dialogue_over_thirty_seconds_is_not_locally_blocked(self):
        with patch.dict(
            "os.environ", {"SEEDANCE25_LONG_SCENE_ENABLED": "1"}, clear=True
        ):
            s1_storyboard._validate(self._storyboard(31))


class VideoQATests(unittest.TestCase):
    def test_continuity_analyzer_compares_current_video_to_canonical_and_previous_refs(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "choices": [{"message": {"content": json.dumps({
                "identity_checked": True, "identity_alignment": 0.93,
                "scene_checked": True, "scene_alignment": 0.90,
                "blocking_checked": True, "blocking_alignment": 0.88,
                "wardrobe_checked": True, "wardrobe_match": True,
                "prop_state_checked": True, "prop_state_match": True,
                "mismatches": [], "reason": "连续",
            }, ensure_ascii=False)}}],
        }
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.post.return_value = response

        with (
            patch.dict(os.environ, {"ARK_API_KEY": "test-key"}, clear=False),
            patch.object(video_qa.httpx, "Client", return_value=client),
        ):
            raw, decision = video_qa.analyze_continuity(
                "https://cdn/current.mp4",
                contract={"scene": {"key": "yard"}, "characters": []},
                identity_image_urls=["https://cdn/lead.png"],
                scene_image_url="https://cdn/yard.png",
                previous_video_url="https://cdn/previous.mp4",
            )

        self.assertTrue(decision.passed)
        self.assertEqual(raw["scene_alignment"], 0.90)
        payload = client.post.call_args.kwargs["json"]
        content = payload["messages"][0]["content"]
        self.assertEqual(
            [item["type"] for item in content],
            ["text", "video_url", "image_url", "image_url", "video_url"],
        )
        self.assertIn("continuity_contract", content[0]["text"])

    def test_continuity_gate_rejects_identity_scene_and_blocking_drift(self):
        decision = video_qa.evaluate_continuity({
            "identity_checked": True, "identity_alignment": 0.61,
            "scene_checked": True, "scene_alignment": 0.68,
            "blocking_checked": True, "blocking_alignment": 0.52,
            "wardrobe_match": False, "prop_state_match": False,
        })

        self.assertFalse(decision.passed)
        self.assertIn("identity_drift", decision.reasons)
        self.assertIn("scene_drift", decision.reasons)
        self.assertIn("blocking_drift", decision.reasons)
        self.assertIn("wardrobe_drift", decision.reasons)
        self.assertIn("prop_state_mismatch", decision.reasons)

    def test_continuity_gate_ignores_dimensions_without_reference_evidence(self):
        decision = video_qa.evaluate_continuity({
            "identity_checked": False, "identity_alignment": 0,
            "scene_checked": True, "scene_alignment": 0.91,
            "blocking_checked": False, "blocking_alignment": 0,
            "wardrobe_checked": False, "prop_state_checked": False,
        })

        self.assertTrue(decision.passed)
        self.assertEqual(decision.reasons, ())

    def test_broll_semantic_gate_requires_industry_subject_and_environment(self):
        decision = video_qa.evaluate(
            {
                "transcript": "",
                "confidence": 1.0,
                "broll_semantics": {
                    "industry_subject_visible": False,
                    "environment_context_visible": False,
                    "dominant_subject": "hands",
                    "hand_closeup_ratio": 0.82,
                },
            },
            expect_speech=False,
            require_broll_semantics=True,
        )

        self.assertFalse(decision.passed)
        self.assertIn("missing_industry_subject", decision.reasons)
        self.assertIn("missing_environment_context", decision.reasons)
        self.assertIn("meaningless_hand_closeup", decision.reasons)

    def test_broll_semantic_gate_accepts_scene_subject_and_action_evidence(self):
        decision = video_qa.evaluate(
            {
                "transcript": "",
                "confidence": 1.0,
                "broll_semantics": {
                    "industry_subject_visible": True,
                    "environment_context_visible": True,
                    "meaningful_action_visible": True,
                    "dominant_subject": "equipment",
                    "hand_closeup_ratio": 0.12,
                },
            },
            expect_speech=False,
            require_broll_semantics=True,
        )

        self.assertTrue(decision.passed)
        self.assertEqual(decision.reasons, ())

    def test_readable_generated_text_fails_when_forbidden(self):
        decision = video_qa.evaluate(
            {
                "transcript": "",
                "confidence": 1.0,
                "has_readable_text": True,
            },
            expect_speech=False,
            forbid_readable_text=True,
        )

        self.assertFalse(decision.passed)
        self.assertIn("unexpected_readable_text", decision.reasons)

    def test_transcript_normalization_equates_chinese_and_itn_numbers(self):
        spoken = "二零二六年八月十日，AI视频工厂升级到二点五，设备编号AI三一六。"
        itn = "2026年8月10日，AI 视频工厂升级到2.5，设备编号 AI 316。"

        self.assertEqual(
            video_qa.normalize_transcript_text(spoken),
            video_qa.normalize_transcript_text(itn),
        )

    def test_exact_dialogue_accepts_equivalent_itn_output(self):
        decision = video_qa.evaluate(
            {
                "transcript": "2026年8月10日，AI 视频工厂完成升级，设备编号 AI316。",
                "confidence": 1.0,
            },
            expect_speech=True,
            expected_text="二零二六年八月十日，AI视频工厂完成升级，设备编号AI三一六。",
        )

        self.assertTrue(decision.passed)
        self.assertEqual(decision.transcript_similarity, 1.0)

    def test_transcript_normalization_does_not_hide_semantic_changes(self):
        decision = video_qa.evaluate(
            {"transcript": "设备编号 AI316，复核已经驳回。", "confidence": 1.0},
            expect_speech=True,
            expected_text="设备编号AI三一六，复核已经通过。",
        )

        self.assertFalse(decision.passed)
        self.assertIn("transcript_mismatch", decision.reasons)

    def test_broll_rejects_unexpected_speech(self):
        self.assertTrue(hasattr(video_qa, "evaluate"))
        decision = video_qa.evaluate(
            {"transcript": "你好，我是豆包。", "confidence": 0.98},
            expect_speech=False,
        )

        self.assertFalse(decision.passed)
        self.assertIn("unexpected_speech", decision.reasons)

    def test_broll_accepts_speech_free_audio(self):
        self.assertTrue(hasattr(video_qa, "evaluate"))
        decision = video_qa.evaluate(
            {"transcript": "", "confidence": 0.95},
            expect_speech=False,
        )

        self.assertTrue(decision.passed)
        self.assertEqual(decision.reasons, ())

    def test_speech_free_broll_ignores_zero_transcript_confidence(self):
        decision = video_qa.evaluate(
            {"transcript": "", "confidence": 0.0},
            expect_speech=False,
        )

        self.assertTrue(decision.passed)
        self.assertNotIn("low_confidence", decision.reasons)

    def test_exact_dialogue_rejects_material_transcript_change(self):
        self.assertTrue(hasattr(video_qa, "evaluate"))
        decision = video_qa.evaluate(
            {"transcript": "你好，我是豆包。", "confidence": 0.98},
            expect_speech=True,
            expected_text="今天风很大，记得带伞。",
        )

        self.assertFalse(decision.passed)
        self.assertIn("transcript_mismatch", decision.reasons)

    def test_visual_prompt_mismatch_is_rejected(self):
        self.assertIn(
            "expected_visual", inspect.signature(video_qa.evaluate).parameters
        )
        decision = video_qa.evaluate(
            {
                "transcript": "",
                "confidence": 1.0,
                "visual_alignment": 0.2,
                "has_visible_person": True,
            },
            expect_speech=False,
            expected_visual="施工机械液压表，无人物",
            forbid_people=True,
        )

        self.assertFalse(decision.passed)
        self.assertIn("visual_mismatch", decision.reasons)
        self.assertIn("unexpected_person", decision.reasons)

    def test_independent_visual_comparison_overrides_self_score(self):
        decision = video_qa.evaluate(
            {
                "transcript": "",
                "confidence": 1.0,
                "visual_alignment": 1.0,
                "observed_visual": "手摇磨豆机中倒入咖啡豆并研磨",
                "visual_comparison": {
                    "alignment": 0.0,
                    "mismatches": ["目标是施工机械液压表，实际是咖啡磨豆机"],
                },
            },
            expect_speech=False,
            expected_visual="施工机械液压表，指针缓慢回落",
        )

        self.assertFalse(decision.passed)
        self.assertIn("visual_mismatch", decision.reasons)

    def test_visual_comparison_prompt_separates_expected_and_observed(self):
        self.assertTrue(hasattr(video_qa, "_visual_comparison_prompt"))
        prompt = video_qa._visual_comparison_prompt(
            "施工机械液压表",
            "手摇磨豆机和咖啡豆",
        )

        self.assertIn("施工机械液压表", prompt)
        self.assertIn("手摇磨豆机和咖啡豆", prompt)

    def test_visual_comparison_does_not_treat_omissions_as_mismatches(self):
        prompt = video_qa._visual_comparison_prompt(
            "生锈钢管、逆光薄雾和工人",
            "建筑工地里有钢管和工人",
        )

        self.assertIn("未提及不等于不匹配", prompt)
        self.assertIn("明确冲突", prompt)


class BigASRTests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_audio_builds_16k_mono_wav(self):
        with patch.object(ffmpeg, "_run", new=AsyncMock()) as run:
            await ffmpeg.extract_audio_wav(
                Path("input.mp4"), Path("output.wav")
            )

        args = run.await_args.args[0]
        self.assertIn("-vn", args)
        self.assertEqual(args[args.index("-ac") + 1], "1")
        self.assertEqual(args[args.index("-ar") + 1], "16000")
        self.assertEqual(args[-1], "output.wav")

    def test_bigasr_parses_flash_response(self):
        from factory.clients import bigasr

        self.assertEqual(
            bigasr.parse_transcript({"result": {"text": "新台词"}}),
            "新台词",
        )

    def test_bigasr_parses_transcript_with_speech_bounds(self):
        from factory.clients import bigasr

        result = bigasr.parse_transcription({
            "result": {
                "text": "新台词",
                "utterances": [
                    {"text": "新", "start_time": 620, "end_time": 980},
                    {"text": "台词", "start_time": 990, "end_time": 2860},
                ],
            }
        })

        self.assertEqual(result.text, "新台词")
        self.assertEqual(result.speech_start_ms, 620)
        self.assertEqual(result.speech_end_ms, 2860)

    def test_bigasr_preserves_timed_utterances_for_post_generation_subtitles(self):
        result = bigasr.parse_transcription({
            "result": {
                "text": "狗不干？我干。拼的不是腿，是信息和效率。",
                "utterances": [
                    {"text": "狗不干？我干。", "start_time": 2370,
                     "end_time": 3650},
                    {"text": "拼的不是腿，是信息和效率。", "start_time": 4010,
                     "end_time": 6830},
                ],
            }
        })

        self.assertEqual(
            [(item.text, item.start_ms, item.end_ms)
             for item in result.utterances],
            [
                ("狗不干？我干。", 2370, 3650),
                ("拼的不是腿，是信息和效率。", 4010, 6830),
            ],
        )

    def test_bigasr_handles_response_without_word_timings(self):
        from factory.clients import bigasr

        result = bigasr.parse_transcription({"result": {"text": "仅文本"}})

        self.assertEqual(result.text, "仅文本")
        self.assertIsNone(result.speech_start_ms)
        self.assertIsNone(result.speech_end_ms)

    def test_bigasr_sends_audio_to_official_flash_endpoint(self):
        import httpx
        from factory.clients import bigasr

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["resource"] = request.headers["X-Api-Resource-Id"]
            return httpx.Response(
                200,
                headers={"X-Api-Status-Code": "20000000"},
                json={"result": {"text": "原生台词"}},
            )

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "voice.wav"
            audio.write_bytes(b"wav")
            text = bigasr.transcribe(
                audio,
                app_id="app",
                access_token="token",
                transport=httpx.MockTransport(handler),
            )

        self.assertEqual(text, "原生台词")
        self.assertTrue(captured["url"].endswith("/recognize/flash"))
        self.assertEqual(captured["resource"], "volc.bigasr.auc_turbo")

    def test_bigasr_uses_asr_specific_environment_credentials(self):
        import httpx
        from factory.clients import bigasr

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["app"] = request.headers["X-Api-App-Key"]
            captured["token"] = request.headers["X-Api-Access-Key"]
            return httpx.Response(
                200,
                headers={"X-Api-Status-Code": "20000000"},
                json={"result": {"text": "ok"}},
            )

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"VOLC_ASR_APPID": "asr-app", "VOLC_ASR_TOKEN": "asr-token"},
            clear=True,
        ):
            audio = Path(tmp) / "voice.wav"
            audio.write_bytes(b"wav")
            bigasr.transcribe(
                audio, transport=httpx.MockTransport(handler)
            )

        self.assertEqual(captured, {"app": "asr-app", "token": "asr-token"})

    def test_bigasr_prefers_new_console_api_key(self):
        import httpx

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["api_key"] = request.headers.get("X-Api-Key")
            captured["legacy_app"] = request.headers.get("X-Api-App-Key")
            captured["legacy_token"] = request.headers.get("X-Api-Access-Key")
            return httpx.Response(
                200,
                headers={"X-Api-Status-Code": "20000000"},
                json={"result": {"text": "ok"}},
            )

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {
                "VOLC_ASR_API_KEY": "new-api-key",
                "VOLC_ASR_APPID": "legacy-app",
                "VOLC_ASR_TOKEN": "legacy-token",
            },
            clear=True,
        ):
            audio = Path(tmp) / "voice.wav"
            audio.write_bytes(b"wav")
            bigasr.transcribe(audio, transport=httpx.MockTransport(handler))

        self.assertEqual(captured["api_key"], "new-api-key")
        self.assertIsNone(captured["legacy_app"])
        self.assertIsNone(captured["legacy_token"])

    def test_bigasr_new_console_uses_seed_asr_2_standard_submit_and_query(self):
        import httpx

        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((str(request.url), request.headers["X-Api-Resource-Id"]))
            if str(request.url).endswith("/submit"):
                return httpx.Response(
                    200, headers={"X-Api-Status-Code": "20000000"}, json={}
                )
            return httpx.Response(
                200,
                headers={"X-Api-Status-Code": "20000000"},
                json={"result": {"text": "新版识别"}},
            )

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "voice.wav"
            audio.write_bytes(b"wav")
            result = bigasr.transcribe(
                audio, api_key="new-api-key",
                transport=httpx.MockTransport(handler), poll_interval=0,
            )

        self.assertEqual(result, "新版识别")
        self.assertEqual(
            [url.rsplit("/", 1)[-1] for url, _ in requests],
            ["submit", "query"],
        )
        self.assertTrue(all(resource == "volc.seedasr.auc" for _, resource in requests))


class Seedance25DialogueTests(unittest.TestCase):
    def test_dialogue_master_is_marked_generating_before_provider_call(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        db._add_cols_if_missing(memory_db, "shots", [
            ("narrative_type", "TEXT"),
            ("speaker_id", "TEXT"),
            ("audio_path", "TEXT"),
            ("audio_url", "TEXT"),
            ("video_url", "TEXT"),
            ("anchor_character", "TEXT"),
            ("character_in_scene_path", "TEXT"),
            ("character_in_scene_url", "TEXT"),
        ] + db.SHOT_OBSERVABILITY_COLUMNS)
        memory_db.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,updated_at) "
            "VALUES('J-ACTIVE','T-ACTIVE','Active','PRODUCTION_RUNNING',"
            "'2000-01-01 00:00:00')"
        )
        memory_db.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,state,narrative_type,"
            "speaker_id,voiceover) VALUES('J-ACTIVE',1,4,'PENDING',"
            "'dialogue','lead','你好')"
        )
        memory_db.commit()
        observed_states = []

        with tempfile.TemporaryDirectory() as tmp:
            audio_path = Path(tmp) / "01.wav"
            audio_path.write_bytes(b"audio")
            video_path = Path(tmp) / "01.mp4"
            video_path.write_bytes(b"video")

            async def generate(**_kwargs):
                row = memory_db.execute(
                    "SELECT state,provider,error FROM shots "
                    "WHERE job_id='J-ACTIVE' AND idx=1"
                ).fetchone()
                observed_states.append(dict(row))
                return s3_shots.SeedanceGenerationResult(
                    video=b"video", video_path=video_path,
                    video_url="https://cdn/01.mp4", qa_note="{}",
                    native_audio=True,
                    model="doubao-seedance-2-5-260628",
                    audio_path=audio_path, cost_cents=0,
                    provider_calls=(),
                )

            shot = dict(memory_db.execute(
                "SELECT * FROM shots WHERE job_id='J-ACTIVE' AND idx=1"
            ).fetchone())
            character = {
                "id": "lead", "name": "主角",
                "generation_mode": "ai_generated", "look": "年轻主角",
            }
            with (
                patch.object(
                    s3_shots, "conn",
                    return_value=_NonClosingConnection(memory_db),
                ),
                patch.object(s3_shots, "ASSETS_DIR", Path(tmp)),
                patch.object(
                    s3_shots, "_generate_seedance_video",
                    new=AsyncMock(side_effect=generate),
                ),
            ):
                idx, error, _anchor = asyncio.run(s3_shots._gen_dialogue(
                    "J-ACTIVE", shot, character,
                    {
                        "idx": 1, "speaker": "lead", "duration": 4,
                        "voiceover": "你好", "render_mode": "presenter",
                    },
                    asyncio.Semaphore(1),
                ))

        self.assertEqual(idx, 1)
        self.assertIsNone(error)
        self.assertEqual(observed_states, [{
            "state": "GENERATING", "provider": "seedance", "error": None,
        }])
        row = memory_db.execute(
            "SELECT state FROM shots WHERE job_id='J-ACTIVE' AND idx=1"
        ).fetchone()
        job = memory_db.execute(
            "SELECT updated_at FROM jobs WHERE job_id='J-ACTIVE'"
        ).fetchone()
        self.assertEqual(row["state"], "OK")
        self.assertNotEqual(job["updated_at"], "2000-01-01 00:00:00")

    def test_planner_uses_selected_seedance_for_character_and_scene_anchors(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        storyboard = {
            "production_settings": {
                "character_anchor_generator": "seedance_2_5",
                "scene_anchor_generator": "seedance_2_5",
            },
            "characters": [{
                "id": "customer", "name": "Customer",
                "generation_mode": "ai_generated",
            }],
            "masters": [
                {
                    "idx": 1, "type": "dialogue", "speaker": "customer",
                    "duration": 4, "scene": "repair bay",
                    "scene_anchor_key": "repair-bay-a",
                },
                {
                    "idx": 2, "type": "dialogue", "speaker": "customer",
                    "duration": 4, "scene": "repair bay",
                    "scene_anchor_key": "repair-bay-a",
                },
            ],
        }

        plan = production_planner.analyze_storyboard(
            memory_db, storyboard, budget_cents=10000, resolution="720p"
        )

        identity = plan["paid_identity_anchors"][0]
        scene = plan["paid_scene_anchors"][0]
        self.assertEqual(identity["generator"], "seedance_2_5")
        self.assertEqual(identity["anchor_mode"], "dynamic_video")
        self.assertEqual(identity["estimated_cost_cents"], 408)
        self.assertEqual(scene["generator"], "seedance_2_5")
        self.assertEqual(scene["estimated_cost_cents"], 408)

    def test_character_makeup_plan_deduplicates_fixed_aliases_and_marks_ai(self):
        characters = [
            {
                "id": "sales-wide-shot",
                "name": "Sales",
                "generation_mode": "fixed_asset",
                "identity_type": "virtual_person",
                "seedance_asset_uri": "asset://asset-sales",
            },
            {
                "id": "sales-close-up",
                "name": "Sales",
                "generation_mode": "fixed_asset",
                "identity_type": "virtual_person",
                "seedance_asset_uri": "asset://asset-sales",
            },
            {
                "id": "customer",
                "name": "Customer",
                "generation_mode": "ai_generated",
                "look": "A young customer in a grey sweater",
            },
            {
                "id": "custom-expert",
                "name": "Custom Expert",
                "generation_mode": "fixed_asset",
                "identity_type": "custom",
                "asset_character_id": "custom-expert",
            },
        ]

        plan = character_assets.character_makeup_plan(characters)

        self.assertEqual(len(plan), 3)
        fixed = next(item for item in plan if item["source"] == "volc_asset")
        generated = next(
            item for item in plan if item["source"] == "seedream_identity_image"
        )
        custom = next(
            item for item in plan if item["source"] == "seedance_identity_master"
        )
        self.assertEqual(
            fixed["character_ids"], ["sales-wide-shot", "sales-close-up"]
        )
        self.assertEqual(fixed["status"], "ready")
        self.assertEqual(generated["status"], "pending")
        self.assertTrue(generated["requires_paid_call"])
        self.assertEqual(generated["anchor_mode"], "static_image")
        self.assertEqual(generated["duration_sec"], 0)
        self.assertEqual(custom["anchor_mode"], "dynamic_video")
        self.assertEqual(custom["duration_sec"], 4)

    def test_dialogue_request_carries_primary_and_cast_identity_references(self):
        primary = {
            "id": "customer",
            "name": "Customer",
            "generation_mode": "ai_generated",
        }
        fixed_cast = {
            "id": "sales",
            "name": "Sales",
            "generation_mode": "fixed_asset",
            "identity_type": "virtual_person",
            "seedance_asset_uri": "asset://asset-sales",
        }
        ai_cast = {
            "id": "assistant",
            "name": "Assistant",
            "generation_mode": "ai_generated",
        }
        anchors = {
            character_assets.character_identity_key(primary):
                s3_shots.DialogueAnchor("https://cdn/identity-customer.mp4", 4),
            character_assets.character_identity_key(ai_cast):
                s3_shots.DialogueAnchor("https://cdn/identity-assistant.mp4", 4),
        }

        request = s3_shots._dialogue_request_kwargs(
            {"duration_sec": 8},
            None,
            character=primary,
            cast_characters=[fixed_cast, ai_cast],
            identity_anchors=anchors,
        )

        self.assertEqual(request["references"], ["asset://asset-sales"])
        self.assertEqual(
            request["reference_videos"],
            [
                "https://cdn/identity-customer.mp4",
                "https://cdn/identity-assistant.mp4",
            ],
        )
        self.assertEqual(request["reference_video_duration_sec"], 8)

    def test_dialogue_request_reuses_static_identity_with_previous_master(self):
        primary = {
            "id": "customer",
            "name": "Customer",
            "generation_mode": "ai_generated",
        }
        static_identity = s3_shots.DialogueAnchor(
            image_reference="https://cdn/customer-identity.png",
            anchor_mode="static_image",
        )
        previous_master = s3_shots.DialogueAnchor(
            "https://cdn/previous-master.mp4", 8
        )

        request = s3_shots._dialogue_request_kwargs(
            {"duration_sec": 8},
            previous_master,
            character=primary,
            identity_anchors={
                character_assets.character_identity_key(primary):
                    static_identity,
            },
        )

        self.assertEqual(
            request["references"], ["https://cdn/customer-identity.png"]
        )
        self.assertEqual(
            request["reference_videos"],
            ["https://cdn/previous-master.mp4"],
        )
        self.assertEqual(request["reference_video_duration_sec"], 8)

    def test_scene_cast_is_limited_to_characters_named_in_the_scene(self):
        characters = {
            "sales": {"id": "sales", "name": "Sales"},
            "customer": {"id": "customer", "name": "Customer"},
            "unrelated": {"id": "unrelated", "name": "Unrelated"},
        }
        shot = {
            "scene": "Sales and Customer stand beside a rented excavator",
            "action": "Customer gives a thumbs-up",
        }

        cast = s3_shots._scene_cast_characters(shot, characters, "sales")

        self.assertEqual([item["id"] for item in cast], ["customer"])

    def test_identity_anchor_stage_generates_ai_once_and_reuses_it(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        memory_db.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES(?,?,?,?,?,?)",
            ("J-ID", "T-ID", "Identity", "PRODUCTION_RUNNING", "{}", 0),
        )
        memory_db.commit()
        characters = [{
            "id": "customer",
            "name": "Customer",
            "generation_mode": "ai_generated",
            "look": "A young customer in a grey sweater",
        }]
        with tempfile.TemporaryDirectory() as tmp:
            video_path = Path(tmp) / "identity.mp4"
            video_path.write_bytes(b"video")
            preview_path = Path(tmp) / "frame-001.jpg"
            preview_path.write_bytes(b"jpg")
            result = s3_shots.SeedanceGenerationResult(
                video=b"video",
                video_path=video_path,
                video_url="https://cdn/identity-customer.mp4",
                qa_note="{}",
                native_audio=False,
                model="doubao-seedance-2-5-260628",
                audio_path=None,
                cost_cents=272,
                provider_calls=({
                    "task_id": "identity-task-1",
                    "intent": "character_identity_anchor",
                    "usage": {},
                    "cost_cents": 272,
                },),
            )
            with (
                patch.object(s3_shots, "conn", return_value=memory_db),
                patch.object(s3_shots, "ASSETS_DIR", Path(tmp)),
                patch.object(
                    s3_shots.seedance, "text_to_image",
                    AsyncMock(return_value=b"png"),
                ) as text_to_image,
                patch.object(
                    s3_shots.tos, "upload",
                    return_value="https://cdn/identity-customer.png",
                ) as upload,
                patch.object(
                    s3_shots, "_generate_seedance_video",
                    AsyncMock(return_value=result),
                ) as generate,
                patch.object(
                    s3_shots.ffmpeg, "extract_frames",
                    AsyncMock(return_value=[preview_path]),
                ),
            ):
                first = asyncio.run(
                    s3_shots._ensure_character_identity_anchors(
                        "J-ID", characters
                    )
                )
                second = asyncio.run(
                    s3_shots._ensure_character_identity_anchors(
                        "J-ID", characters
                    )
                )

        identity_key = character_assets.character_identity_key(characters[0])
        self.assertEqual(text_to_image.await_count, 1)
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(generate.await_count, 0)
        self.assertEqual(
            first[identity_key].image_reference,
            "https://cdn/identity-customer.png",
        )
        self.assertEqual(
            second[identity_key].image_reference,
            "https://cdn/identity-customer.png",
        )
        row = memory_db.execute(
            "SELECT state,image_url,cost_cents FROM character_identity_anchors "
            "WHERE job_id=? AND identity_key=?",
            ("J-ID", identity_key),
        ).fetchone()
        self.assertEqual(dict(row), {
            "state": "READY",
            "image_url": "https://cdn/identity-customer.png",
            "cost_cents": 29,
        })
        self.assertEqual(
            memory_db.execute(
                "SELECT cost_cents FROM jobs WHERE job_id='J-ID'"
            ).fetchone()[0],
            29,
        )

    def test_identity_anchor_stage_can_use_selected_seedance_generator(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        storyboard = {
            "production_settings": {
                "character_anchor_generator": "seedance_2_5",
                "budget_cents": 5000,
            }
        }
        memory_db.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES(?,?,?,?,?,?)",
            ("J-ID-S25", "T", "Identity S25", "PRODUCTION_RUNNING",
             json.dumps(storyboard), 0),
        )
        memory_db.commit()
        characters = [{
            "id": "customer", "name": "Customer",
            "generation_mode": "ai_generated", "look": "young customer",
        }]
        with tempfile.TemporaryDirectory() as tmp:
            video_path = Path(tmp) / "identity.mp4"
            video_path.write_bytes(b"video")
            preview_path = Path(tmp) / "frame.jpg"
            preview_path.write_bytes(b"jpg")
            result = s3_shots.SeedanceGenerationResult(
                video=b"video", video_path=video_path,
                video_url="https://cdn/identity.mp4", qa_note="{}",
                native_audio=False, model="doubao-seedance-2-5-260628",
                audio_path=None, cost_cents=408,
                provider_calls=({
                    "task_id": "identity-s25", "intent": "identity",
                    "usage": {}, "cost_cents": 408,
                },),
            )
            with (
                patch.object(s3_shots, "conn", return_value=memory_db),
                patch.object(s3_shots, "ASSETS_DIR", Path(tmp)),
                patch.object(
                    s3_shots.seedance, "text_to_image", new=AsyncMock()
                ) as image_generate,
                patch.object(
                    s3_shots, "_generate_seedance_video",
                    new=AsyncMock(return_value=result),
                ) as video_generate,
                patch.object(
                    s3_shots.ffmpeg, "extract_frames",
                    new=AsyncMock(return_value=[preview_path]),
                ),
            ):
                anchors = asyncio.run(
                    s3_shots._ensure_character_identity_anchors(
                        "J-ID-S25", characters, storyboard=storyboard
                    )
                )

        identity_key = character_assets.character_identity_key(characters[0])
        self.assertEqual(image_generate.await_count, 0)
        self.assertEqual(video_generate.await_count, 1)
        self.assertEqual(anchors[identity_key].anchor_mode, "dynamic_video")
        row = memory_db.execute(
            "SELECT generator,model,preview_path,cost_cents "
            "FROM character_identity_anchors WHERE job_id='J-ID-S25'"
        ).fetchone()
        self.assertEqual(row["generator"], "seedance_2_5")
        self.assertEqual(row["model"], "doubao-seedance-2-5-260628")
        self.assertEqual(row["preview_path"], str(preview_path))
        self.assertEqual(row["cost_cents"], 408)

    def test_dynamic_identity_uses_one_provider_owner_from_submit_to_final(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        storyboard = {
            "production_settings": {
                "character_anchor_generator": "seedance_2_5",
                "budget_cents": 5000,
            }
        }
        memory_db.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES(?,?,?,?,?,?)",
            (
                "J-ID-OWNER", "T", "Identity owner",
                "PRODUCTION_RUNNING", json.dumps(storyboard), 0,
            ),
        )
        memory_db.commit()
        characters = [{
            "id": "customer", "name": "Customer",
            "generation_mode": "ai_generated", "look": "young customer",
        }]

        async def generate_video(**kwargs):
            task_id = "identity-owner-task"
            usage = {"completion_tokens": 38830, "total_tokens": 38830}
            kwargs["metadata_out"].update({"task_id": task_id, "usage": usage})
            kwargs["on_task_created"](task_id)
            kwargs["on_task_updated"](
                task_id, "completed", {"usage": usage}
            )
            return b"video"

        with tempfile.TemporaryDirectory() as tmp:
            preview_path = Path(tmp) / "frame.jpg"
            preview_path.write_bytes(b"jpg")
            with (
                patch.object(s3_shots, "conn", return_value=memory_db),
                patch.object(s3_shots, "ASSETS_DIR", Path(tmp)),
                patch.object(
                    s3_shots.seedance,
                    "text_to_video",
                    new=AsyncMock(side_effect=generate_video),
                ),
                patch.object(
                    s3_shots.tos,
                    "upload",
                    return_value="https://cdn/identity-owner.mp4",
                ),
                patch.object(
                    s3_shots.ffmpeg,
                    "extract_frames",
                    new=AsyncMock(return_value=[preview_path]),
                ),
            ):
                asyncio.run(
                    s3_shots._ensure_character_identity_anchors(
                        "J-ID-OWNER", characters, storyboard=storyboard
                    )
                )

        call = memory_db.execute(
            "SELECT job_id,shot_idx,provider,intent,cost_cents,usage_json "
            "FROM provider_calls WHERE task_id='identity-owner-task'"
        ).fetchone()
        identity = memory_db.execute(
            "SELECT state,cost_cents FROM character_identity_anchors "
            "WHERE job_id='J-ID-OWNER'"
        ).fetchone()
        job_cost = memory_db.execute(
            "SELECT cost_cents FROM jobs WHERE job_id='J-ID-OWNER'"
        ).fetchone()[0]
        self.assertEqual(
            tuple(call[:4]),
            ("J-ID-OWNER", -1001, "seedance_identity", "generate"),
        )
        self.assertEqual(json.loads(call[5])["status"], "completed")
        self.assertEqual(identity["state"], "READY")
        self.assertGreater(call["cost_cents"], 0)
        self.assertEqual(identity["cost_cents"], call["cost_cents"])
        self.assertEqual(job_cost, call["cost_cents"])

    def test_dialogue_privacy_rejection_falls_back_to_dynamic_identity(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        memory_db.execute("ALTER TABLE shots ADD COLUMN video_url TEXT")
        memory_db.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES(?,?,?,?,?,?)",
            ("J-FALLBACK", "T", "Fallback", "PRODUCTION_RUNNING", "{}", 29),
        )
        memory_db.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,voiceover,state) "
            "VALUES(?,?,?,?,?)",
            ("J-FALLBACK", 1, 8, "Hello", "PENDING"),
        )
        character = {
            "id": "customer",
            "name": "Customer",
            "generation_mode": "ai_generated",
            "look": "A young customer in a grey sweater",
        }
        identity_key = character_assets.character_identity_key(character)
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "identity.png"
            image_path.write_bytes(b"png")
            memory_db.execute(
                "INSERT INTO character_identity_anchors("
                "job_id,identity_key,character_id,display_name,source_type,"
                "state,anchor_mode,image_path,image_url,cost_cents) "
                "VALUES(?,?,?,?,?,'READY','static_image',?,?,29)",
                (
                    "J-FALLBACK", identity_key, "customer", "Customer",
                    "seedream_identity_image", str(image_path),
                    "https://cdn/customer.png",
                ),
            )
            memory_db.commit()
            video_path = Path(tmp) / "result.mp4"
            video_path.write_bytes(b"video")
            audio_path = Path(tmp) / "result.wav"
            audio_path.write_bytes(b"wav")
            result = s3_shots.SeedanceGenerationResult(
                video=b"video",
                video_path=video_path,
                video_url="https://cdn/customer-dynamic.mp4",
                qa_note="{}",
                native_audio=True,
                model="doubao-seedance-2-5-260628",
                audio_path=audio_path,
                cost_cents=272,
                provider_calls=(),
            )
            anchors = {
                identity_key: s3_shots.DialogueAnchor(
                    image_reference="https://cdn/customer.png",
                    anchor_mode="static_image",
                )
            }
            with (
                patch.object(s3_shots, "conn", return_value=memory_db),
                patch.object(s3_shots, "ASSETS_DIR", Path(tmp)),
                patch.object(
                    s3_shots, "_generate_seedance_video",
                    AsyncMock(side_effect=[
                        s3_shots.seedance.SeedanceInputPrivacyError("privacy"),
                        result,
                        result,
                    ]),
                ) as generate,
                patch.object(
                    s3_shots.ffmpeg, "extract_frames",
                    AsyncMock(return_value=[]),
                ),
                patch.object(s3_shots, "_persist_master_transcription"),
            ):
                idx, error, returned_anchor = asyncio.run(
                    s3_shots._gen_dialogue(
                        "J-FALLBACK",
                        {
                            "idx": 1,
                            "duration_sec": 8,
                            "voiceover": "Hello",
                            "shot_type": "dialogue",
                        },
                        character,
                        {"voiceover": "Hello"},
                        asyncio.Semaphore(1),
                        identity_anchors=anchors,
                    )
                )

        self.assertEqual(idx, 1)
        self.assertIsNone(error)
        self.assertIsNotNone(returned_anchor)
        self.assertEqual(generate.await_count, 3)
        self.assertEqual(anchors[identity_key].anchor_mode, "dynamic_video")
        self.assertEqual(
            generate.await_args_list[-1].kwargs["request_kwargs"][
                "reference_videos"
            ],
            ["https://cdn/customer-dynamic.mp4"],
        )

    def test_identity_anchor_failure_blocks_all_scene_generation(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        memory_db.execute("ALTER TABLE jobs ADD COLUMN characters_json TEXT")
        memory_db.execute("ALTER TABLE shots ADD COLUMN narrative_type TEXT")
        memory_db.execute("ALTER TABLE shots ADD COLUMN speaker_id TEXT")
        storyboard = {
            "characters": [{
                "id": "customer",
                "name": "Customer",
                "generation_mode": "ai_generated",
                "look": "A young customer in a grey sweater",
            }],
            "masters": [{
                "idx": 1,
                "type": "dialogue",
                "speaker": "customer",
                "duration": 4,
                "render_mode": "presenter",
            }],
        }
        memory_db.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,characters_json) "
            "VALUES(?,?,?,?,?,?)",
            (
                "J-GATE", "T-GATE", "Gate", "PRODUCTION_RUNNING",
                json.dumps(storyboard),
                json.dumps(storyboard["characters"]),
            ),
        )
        memory_db.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,state,narrative_type,"
            "speaker_id) VALUES(?,?,?,?,?,?)",
            ("J-GATE", 1, 4, "PENDING", "dialogue", "customer"),
        )
        memory_db.commit()

        with (
            patch.object(s3_shots, "conn", return_value=memory_db),
            patch.object(
                s3_shots, "_resolve_speaking_voice_bindings",
                return_value=False,
            ),
            patch.object(
                s3_shots, "_ensure_character_identity_anchors",
                AsyncMock(side_effect=RuntimeError("identity anchor failed")),
            ),
            patch.object(s3_shots, "_gen_dialogue", AsyncMock()) as generate,
        ):
            with self.assertRaisesRegex(RuntimeError, "identity anchor failed"):
                asyncio.run(s3_shots.run("J-GATE"))

        generate.assert_not_awaited()

    def test_run_passes_previous_same_scene_master_across_speakers(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        memory_db.execute("ALTER TABLE jobs ADD COLUMN characters_json TEXT")
        memory_db.execute("ALTER TABLE shots ADD COLUMN narrative_type TEXT")
        memory_db.execute("ALTER TABLE shots ADD COLUMN speaker_id TEXT")
        memory_db.execute("ALTER TABLE shots ADD COLUMN anchor_character TEXT")
        memory_db.execute("ALTER TABLE shots ADD COLUMN video_url TEXT")
        characters = [
            {
                "id": "sales", "name": "Sales", "gender": "female",
                "generation_mode": "ai_generated", "look": "young sales",
            },
            {
                "id": "customer", "name": "Customer", "gender": "male",
                "generation_mode": "ai_generated", "look": "young customer",
            },
        ]
        masters = [
            {
                "idx": 1, "type": "dialogue", "speaker": "sales",
                "render_mode": "cinematic_character", "duration": 4,
                "scene_anchor_key": "yard-a",
            },
            {
                "idx": 2, "type": "dialogue", "speaker": "sales",
                "render_mode": "cinematic_character", "duration": 4,
                "scene_anchor_key": "yard-a",
            },
            {
                "idx": 3, "type": "dialogue", "speaker": "customer",
                "render_mode": "cinematic_character", "duration": 4,
                "scene_anchor_key": "yard-a",
            },
        ]
        storyboard = {"characters": characters, "masters": masters, "shots": []}
        memory_db.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,characters_json) "
            "VALUES(?,?,?,?,?,?)",
            (
                "J-SCENE-CHAIN", "T-SCENE-CHAIN", "Scene chain",
                "PRODUCTION_RUNNING", json.dumps(storyboard),
                json.dumps(characters),
            ),
        )
        for master in masters:
            memory_db.execute(
                "INSERT INTO shots(job_id,idx,duration_sec,state,narrative_type,"
                "speaker_id) VALUES(?,?,?,?,?,?)",
                (
                    "J-SCENE-CHAIN", master["idx"], 4, "PENDING",
                    "dialogue", master["speaker"],
                ),
            )
        memory_db.commit()
        received_scene_anchors = []

        async def generate(job_id, shot, char, sb_shot, sem, anchor=None, **kwargs):
            scene_anchor = kwargs.get("scene_video_anchor")
            received_scene_anchors.append(
                scene_anchor.video_url if scene_anchor else None
            )
            return (
                shot["idx"], None,
                s3_shots.DialogueAnchor(
                    f"https://cdn/master-{shot['idx']}.mp4", 4
                ),
            )

        with (
            patch.object(s3_shots, "conn", return_value=memory_db),
            patch.object(
                s3_shots, "_resolve_speaking_voice_bindings",
                return_value=False,
            ),
            patch.object(
                s3_shots, "_synthesize_fixed_voice_track",
                AsyncMock(return_value=s3_shots.FixedVoiceTrack(
                    Path("fixed.wav"), bigasr.Transcription(""), (), 4
                )),
            ),
            patch.object(
                s3_shots, "_ensure_character_identity_anchors",
                AsyncMock(return_value={}),
            ),
            patch.object(
                s3_shots, "_ensure_scene_visual_anchors",
                AsyncMock(return_value={"yard-a": "https://cdn/yard.png"}),
            ),
            patch.object(s3_shots, "_gen_dialogue", side_effect=generate),
            patch.object(s3_shots.asset_library, "reuse_broll_assets"),
        ):
            asyncio.run(s3_shots.run("J-SCENE-CHAIN"))

        self.assertEqual(
            received_scene_anchors,
            [None, "https://cdn/master-1.mp4", "https://cdn/master-2.mp4"],
        )

    def test_static_makeup_reuses_existing_image_without_rebuy(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        memory_db.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-PREVIEW','T-PREVIEW','Preview','PRODUCTION_RUNNING','{}',0)"
        )
        memory_db.commit()
        character = {
            "id": "customer",
            "name": "Customer",
            "generation_mode": "ai_generated",
            "look": "A young fictional customer",
        }
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(s3_shots, "conn", return_value=memory_db),
                patch.object(s3_shots, "ASSETS_DIR", Path(tmp)),
                patch.object(
                    s3_shots.seedance, "text_to_image",
                    AsyncMock(return_value=b"png"),
                ) as generate,
                patch.object(
                    s3_shots.tos, "upload",
                    return_value="https://cdn/identity-preview.png",
                ),
            ):
                first = asyncio.run(
                    s3_shots._ensure_character_identity_anchors(
                        "J-PREVIEW", [character]
                    )
                )
                second = asyncio.run(
                    s3_shots._ensure_character_identity_anchors(
                        "J-PREVIEW", [character]
                    )
                )

        identity_key = character_assets.character_identity_key(character)
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(
            first[identity_key].image_reference,
            "https://cdn/identity-preview.png",
        )
        self.assertEqual(
            second[identity_key].image_reference,
            "https://cdn/identity-preview.png",
        )
        row = memory_db.execute(
            "SELECT state,preview_path,cost_cents,error "
            "FROM character_identity_anchors WHERE job_id='J-PREVIEW'"
        ).fetchone()
        self.assertEqual(row["state"], "READY")
        self.assertTrue(row["preview_path"])
        self.assertEqual(row["cost_cents"], 29)
        self.assertIsNone(row["error"])

    def test_static_makeup_upload_retry_does_not_rebuy_image(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        memory_db.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-UPLOAD','T-UPLOAD','Upload','PRODUCTION_RUNNING','{}',0)"
        )
        memory_db.commit()
        character = {
            "id": "customer",
            "name": "Customer",
            "generation_mode": "ai_generated",
            "look": "A young fictional customer",
        }
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(s3_shots, "conn", return_value=memory_db),
                patch.object(s3_shots, "ASSETS_DIR", Path(tmp)),
                patch.object(
                    s3_shots.seedance, "text_to_image",
                    AsyncMock(return_value=b"png"),
                ) as generate,
                patch.object(
                    s3_shots.tos, "upload",
                    side_effect=[
                        RuntimeError("upload failed"),
                        "https://cdn/identity-upload.png",
                    ],
                ) as upload,
            ):
                with self.assertRaisesRegex(RuntimeError, "upload failed"):
                    asyncio.run(
                        s3_shots._ensure_character_identity_anchors(
                            "J-UPLOAD", [character]
                        )
                    )
                anchors = asyncio.run(
                    s3_shots._ensure_character_identity_anchors(
                        "J-UPLOAD", [character]
                    )
                )

        identity_key = character_assets.character_identity_key(character)
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(upload.call_count, 2)
        self.assertEqual(
            anchors[identity_key].image_reference,
            "https://cdn/identity-upload.png",
        )
        self.assertEqual(
            memory_db.execute(
                "SELECT cost_cents FROM jobs WHERE job_id='J-UPLOAD'"
            ).fetchone()[0],
            29,
        )

    def test_fixed_custom_asset_uses_script_role_and_appearance_only(self):
        shot = {
            "idx": 1,
            "duration_sec": 8,
            "render_mode": "cinematic_character",
            "scene": "海外设备租赁项目现场",
            "voiceover": "项目交付按计划推进。",
            "cast": [{
                "id": "主管",
                "name": "奥黛丽",
                "script_character_name": "主管",
                "generation_mode": "fixed_asset",
                "identity_type": "custom",
                "seedance_reference_mode": "description_only",
                "look": "《诡秘之主》塔罗会成员",
                "background_description": (
                    "《诡秘之主》塔罗会代号正义。"
                    "真人形象设定为18岁的欧洲年轻女性，金色长卷发、绿色眼睛。"
                ),
            }],
        }

        prompt = s3_shots._build_native_dialogue_prompt(
            shot,
            {
                "id": "员工A",
                "name": "老大",
                "script_character_name": "员工A",
                "generation_mode": "fixed_asset",
                "identity_type": "virtual_person",
                "seedance_asset_uri": "asset://employee-a",
                "look": "古代戏楼名伶",
                "background_description": "先秦战乱中的后宅名伶",
            },
        )

        self.assertIn("人物姓名与设定：员工A", prompt)
        self.assertIn("主管：18岁的欧洲年轻女性", prompt)
        self.assertNotIn("奥黛丽", prompt)
        self.assertNotIn("诡秘之主", prompt)
        self.assertNotIn("塔罗会", prompt)
        self.assertNotIn("先秦战乱", prompt)

    def test_cinematic_character_prompt_uses_story_action_and_dynamic_camera(self):
        shot = {
            "idx": 1,
            "duration_sec": 12,
            "render_mode": "cinematic_character",
            "scene": "午夜雾港的废弃蒸汽船甲板",
            "camera": "低机位跟拍后环绕人物",
            "action": "人物弹起硬币，躲过裂开的甲板",
            "voiceover": "罗盘是诱饵。真正的危险，在我们脚下。",
            "performance_beats": [
                {
                    "duration": 5,
                    "visual_intent": "人物登上甲板，雕像突然睁眼",
                    "dialogue": "罗盘是诱饵。",
                },
                {
                    "duration": 7,
                    "visual_intent": "人物弹起硬币，甲板裂开",
                    "dialogue": "真正的危险，在我们脚下。",
                },
            ],
            "cast": [
                {"name": "奥黛丽", "look": "金发绿眼的欧洲贵族少女"},
                {"name": "阿尔杰", "look": "深蓝卷发的年轻航海者"},
            ],
        }

        prompt = s3_shots._build_native_dialogue_prompt(
            shot,
            {"name": "克莱恩", "look": "年轻的东亚神秘学调查者"},
        )

        self.assertIn("电影叙事情境表演", prompt)
        self.assertIn("不得持续正对镜头站桩讲解", prompt)
        self.assertIn("人物登上甲板，雕像突然睁眼", prompt)
        self.assertIn("人物弹起硬币，甲板裂开", prompt)
        self.assertIn("低机位跟拍后环绕人物", prompt)
        self.assertIn("奥黛丽：金发绿眼的欧洲贵族少女", prompt)
        self.assertIn("阿尔杰：深蓝卷发的年轻航海者", prompt)
        self.assertIn("只有克莱恩说出台词", prompt)
        self.assertNotIn("建立场景、人物或物件关系", prompt)

    def test_cinematic_generation_resolves_only_scene_cast(self):
        source = inspect.getsource(s3_shots.run)

        self.assertIn('sb_shot["cast"]', source)
        self.assertIn("_scene_cast_characters", source)
        self.assertNotIn("if character_id != shot[\"speaker_id\"]", source)

    def test_cinematic_ensemble_prompt_assigns_each_line_to_its_speaker(self):
        shot = {
            "idx": 1,
            "speaker": "klein",
            "speakers": ["klein", "audrey"],
            "duration_sec": 12,
            "render_mode": "cinematic_character",
            "scene": "foggy hall",
            "camera": "continuous moving camera",
            "action": "the two characters investigate the room",
            "voiceover": "First line.Second line.",
            "performance_beats": [
                {
                    "speaker": "klein",
                    "start": 0,
                    "duration": 6,
                    "visual_intent": "Klein examines the clue",
                    "dialogue": "First line.",
                },
                {
                    "speaker": "audrey",
                    "start": 6,
                    "duration": 6,
                    "visual_intent": "Audrey reacts",
                    "dialogue": "Second line.",
                },
            ],
            "cast": [{"id": "audrey", "name": "Audrey", "look": "young woman"}],
        }

        prompt = s3_shots._build_native_dialogue_prompt(
            shot, {"id": "klein", "name": "Klein", "look": "young man"}
        )

        self.assertIn("角色 klein", prompt)
        self.assertIn("角色 audrey", prompt)
        self.assertIn("多人对白必须严格按照", prompt)
        self.assertNotIn("其他同场人物只能做动作和反应，不得开口", prompt)

    def test_cinematic_prompt_uses_continuous_integer_timeline_and_unique_dialogue(self):
        shot = {
            "idx": 1,
            "speaker": "linxia",
            "duration_sec": 12,
            "render_mode": "cinematic_character",
            "scene": "暴雨中的高空设备抢修现场",
            "camera": "跟随人物走向设备后缓慢推近",
            "action": "林夏观察臂架并指向曲臂高空作业平台",
            "voiceover": "关键不是举得高。还要保留水平作业半径。",
            "performance_beats": [
                {
                    "speaker": "linxia",
                    "start": 0.4,
                    "duration": 4.6,
                    "visual_intent": "林夏走向设备并观察臂架",
                    "dialogue": "关键不是举得高。",
                },
                {
                    "speaker": "linxia",
                    "start": 5,
                    "duration": 7,
                    "visual_intent": "镜头推近，林夏指向作业半径",
                    "dialogue": "还要保留水平作业半径。",
                },
            ],
        }

        prompt = s3_shots._build_native_dialogue_prompt(
            shot,
            {"id": "linxia", "name": "林夏", "look": "年轻的设备调度工程师"},
        )

        self.assertIn("连续整数秒导演时间轴", prompt)
        self.assertIn("0-5秒", prompt)
        self.assertIn("5-12秒", prompt)
        self.assertEqual(prompt.count("关键不是举得高。"), 1)
        self.assertEqual(prompt.count("还要保留水平作业半径。"), 1)
        self.assertIn("嘴部保持清晰可见", prompt)
        self.assertIn("运镜服务剧情", prompt)

    def test_cinematic_prompt_without_performance_beats_still_has_one_canonical_line(self):
        dialogue = "两小时内安排设备到场。"

        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 1,
                "speaker": "linxia",
                "duration_sec": 8,
                "render_mode": "cinematic_character",
                "scene": "暴雨抢修现场",
                "camera": "从设备全景跟移到人物中近景",
                "action": "林夏走到控制台前确认调度信息",
                "voiceover": dialogue,
            },
            {"id": "linxia", "name": "林夏", "look": "年轻的设备调度工程师"},
        )

        self.assertIn("0-8秒", prompt)
        self.assertEqual(prompt.count(dialogue), 1)
        self.assertIn("{台词：两小时内安排设备到场。}", prompt)

    def test_lip_sync_edit_uses_original_master_and_preserves_cinematic_dimensions(self):
        shot = {
            "idx": 3,
            "duration_sec": 20,
            "voiceover": "我选一台二十八米级曲臂高空作业平台。",
            "repair_source_path": "C:/jobs/J-1/shots/03.mp4",
            "repair_start_sec": 3,
            "repair_end_sec": 15,
        }

        request = s3_shots._lip_sync_edit_request_kwargs(shot)
        prompt = s3_shots._build_lip_sync_edit_prompt(shot)

        self.assertEqual(request["task_intent"], "edit")
        self.assertEqual(request["duration"], -1)
        self.assertEqual(request["ratio"], "adaptive")
        self.assertEqual(
            request["reference_videos"], ["C:/jobs/J-1/shots/03.mp4"]
        )
        self.assertEqual(request["reference_video_duration_sec"], 20)
        self.assertIn("仅编辑视频1中3-15秒", prompt)
        self.assertIn("嘴部动作", prompt)
        self.assertIn("原始声音、音色、语速、表情、身体动作、运镜", prompt)
        self.assertIn("其余均保持不变", prompt)
        self.assertEqual(
            prompt.count("我选一台二十八米级曲臂高空作业平台。"), 1
        )

    def test_lip_sync_edit_never_remuxes_a_new_fixed_voice_track(self):
        source = inspect.getsource(s3_shots._gen_dialogue)

        self.assertIn("fixed_voice_track and not lip_sync_edit", source)

    def test_ai_generated_character_ignores_cached_roster_reference(self):
        character = {
            "id": "sales_xiaoneng",
            "name": "芳白",
            "look": "年轻、理性的设备租赁顾问",
            "generation_mode": "ai_generated",
        }

        with patch.object(
            s3_shots,
            "_roster_character",
            return_value={
                "identity_type": "real_person",
                "seedance_asset_uri": "asset://asset-old-fixed-person",
            },
        ):
            request = s3_shots._dialogue_request_kwargs(
                {"duration_sec": 8}, None, character=character
            )
            prompt = s3_shots._build_native_dialogue_prompt(
                {"idx": 1, "duration_sec": 8, "voiceover": "你好"},
                character,
            )

        self.assertNotIn("references", request)
        self.assertIn("完全虚构", prompt)

    def test_dialogue_prompt_contains_exact_native_line(self):
        shot = {
            "idx": 1,
            "scene": "明亮设备交付工作室",
            "camera": "镜头缓慢推近",
            "action": "说到交付时轻微点头",
            "voiceover": "设备编号AI三一六，确认无误，可以交付。",
            "voice_directive": "专业沉稳的成年男性普通话",
            "expression_arc": "认真核对后确认完成",
        }
        character = {
            "name": "老周",
            "look": "四十岁中国男性，短黑发，深灰西装",
        }

        prompt = s3_shots._build_native_dialogue_prompt(shot, character)

        self.assertIn(character["look"], prompt)
        self.assertIn(shot["voiceover"], prompt)
        self.assertIn("完整说出且只能说出", prompt)
        self.assertIn(shot["voice_directive"], prompt)
        self.assertNotIn("SeedTTS", prompt)
        self.assertNotIn("OmniHuman", prompt)

    def test_dialogue_prompt_uses_character_background_context(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 1,
                "scene": "设备租赁办公室",
                "voiceover": "先把设备利用率算清楚。",
            },
            {
                "name": "小玲",
                "look": "固定的设备租赁顾问",
                "background_description": "设备租赁行业一线销售，表达干练可信。",
            },
        )

        self.assertIn("角色背景：设备租赁行业一线销售，表达干练可信。", prompt)

    def test_dialogue_prompt_uses_director_timeline_and_audio_grammar(self):
        shot = {
            "idx": 1,
            "duration_sec": 8,
            "scene": "明亮设备交付工作室",
            "camera": "镜头缓慢推近",
            "action": "说到交付时轻微点头",
            "voiceover": "设备编号AI三一六，确认无误，可以交付。",
            "expression_arc": "认真核对后确认完成",
            "sound_design": "纸张轻响与安静室内底噪",
        }

        prompt = s3_shots._build_native_dialogue_prompt(
            shot, {"name": "老周", "look": "虚构的中年工程师"}
        )

        self.assertIn("创作意图：", prompt)
        self.assertIn("时间轴：", prompt)
        self.assertIn("0.0-", prompt)
        self.assertIn("-8.0秒：", prompt)
        self.assertIn(f"{{台词：{shot['voiceover']}}}", prompt)
        self.assertIn(f"<环境音：{shot['sound_design']}>", prompt)
        self.assertIn("无 BGM", prompt)

    def test_default_sound_design_is_derived_from_scene_dynamics(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 1,
                "duration_sec": 8,
                "scene": "设备间",
                "scene_dynamics": "纸页被风轻轻带动",
                "voiceover": "请再次核对设备记录。",
            },
            {"name": "老周", "look": "虚构的中年工程师"},
        )

        self.assertIn("由“纸页被风轻轻带动”产生的轻微环境声与物件声", prompt)

    def test_anchored_dialogue_uses_video_for_identity_and_native_voice(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 2,
                "duration_sec": 8,
                "scene": "同一工作室",
                "voiceover": "现在进入第二项检查。",
            },
            {"name": "老周", "look": "虚构的中年工程师"},
            anchored=True,
        )

        self.assertIn("素材职责：", prompt)
        self.assertIn("身份参考图只锁定人物外貌", prompt)
        self.assertIn("前序视频只延续原生声线", prompt)
        self.assertIn("不得复述参考素材中的旧台词", prompt)
        self.assertNotIn("音频1", prompt)

    def test_fixed_identity_anchor_does_not_lock_scene_wardrobe(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 1,
                "duration_sec": 8,
                "scene": "户外设备停车场，工作人员佩戴白色安全帽和反光背心",
                "voiceover": "这批设备今天完成交付。",
            },
            {
                "id": "staff",
                "name": "老大",
                "script_character_name": "女工作人员",
                "generation_mode": "fixed_asset",
                "identity_type": "virtual_person",
                "seedance_asset_uri": "asset://staff",
                "look": "古装戏楼名伶",
            },
            anchored=True,
        )

        self.assertIn("身份参考图只锁定人物外貌中的不可变身份特征", prompt)
        self.assertIn("服装、PPE、防护装备和职业状态严格服从本镜场景设定", prompt)
        self.assertNotIn("体型与服装", prompt)

    def test_dialogue_prompt_blocks_text_on_equipment_and_ppe(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 1,
                "duration_sec": 8,
                "scene": "户外设备停车场，工作人员佩戴安全帽和反光背心",
                "voiceover": "设备已经完成交付。",
            },
            {"id": "staff", "name": "工作人员", "generation_mode": "ai_generated"},
        )

        self.assertIn("安全帽、反光背心、工服、工程机械、设备铭牌", prompt)
        self.assertIn("禁止企业名称、品牌、设备编号、车牌、字母、数字、二维码", prompt)

    def test_fixed_character_prompt_does_not_claim_the_person_is_fictional(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 1,
                "duration_sec": 8,
                "scene": "设备租赁办公室",
                "voiceover": "现在做租赁，拼的是信息和效率。",
            },
            {
                "id": "sales_xiaoneng",
                "name": "小玲",
                "look": "固定的设备租赁顾问",
                "reference_image": (
                    "references/characters/sales_xiaoneng/canonical-v1.png"
                ),
            },
        )

        self.assertIn("固定角色身份参考", prompt)
        self.assertNotIn("完全虚构", prompt)

    def test_first_dialogue_is_prompt_only(self):
        shot = {"duration_sec": 8}

        request = s3_shots._dialogue_request_kwargs(shot, None)

        self.assertNotIn("first_frame", request)
        self.assertNotIn("reference_videos", request)
        self.assertNotIn("reference_audios", request)

    def test_short_editorial_dialogue_uses_provider_safe_duration(self):
        request = s3_shots._dialogue_request_kwargs(
            {"duration_sec": 0.4}, None
        )

        self.assertEqual(request["duration"], 4)

    def test_real_person_character_requires_authorized_seedance_asset(self):
        shot = {"duration_sec": 8}
        character = {"id": "sales_xiaoneng"}

        with patch.object(
            s3_shots,
            "_roster_character",
            return_value={
                "identity_type": "real_person",
                "reference_image": "references/characters/person.png",
            },
        ):
            with self.assertRaisesRegex(
                s3_shots.SeedanceIdentityAuthorizationError,
                "authorized.*asset",
            ):
                s3_shots._dialogue_request_kwargs(
                    shot, None, character=character
                )

    def test_real_person_character_uses_authorized_seedance_asset_uri(self):
        shot = {"duration_sec": 8}
        character = {"id": "sales_xiaoneng"}

        with patch.object(
            s3_shots,
            "_roster_character",
            return_value={
                "identity_type": "real_person",
                "seedance_asset_uri": "asset://asset-xiaoling-authorized",
            },
        ):
            request = s3_shots._dialogue_request_kwargs(
                shot, None, character=character
            )

        self.assertEqual(
            request["references"], ["asset://asset-xiaoling-authorized"]
        )
        self.assertNotIn("reference_videos", request)

    def test_virtual_portrait_character_uses_volc_asset_uri(self):
        shot = {"duration_sec": 8}
        character = {
            "id": "virtual_host",
            "identity_type": "virtual_person",
            "seedance_asset_uri": "asset://asset-virtual-host-01",
        }

        request = s3_shots._dialogue_request_kwargs(
            shot, None, character=character
        )

        self.assertEqual(
            request["references"], ["asset://asset-virtual-host-01"]
        )

    def test_virtual_portrait_character_requires_virtual_portrait_asset(self):
        shot = {"duration_sec": 8}
        character = {"id": "virtual_host", "identity_type": "virtual_person"}

        with self.assertRaisesRegex(
            s3_shots.SeedanceIdentityAuthorizationError,
            "virtual-portrait.*asset",
        ):
            s3_shots._dialogue_request_kwargs(shot, None, character=character)

    def test_custom_character_uses_description_only_for_first_seedance_master(self):
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "klein-1.png"
            second = Path(temp) / "klein-2.png"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            shot = {"duration_sec": 8}
            character = {
                "id": "custom_klein",
                "identity_type": "custom",
                "generation_mode": "fixed_asset",
                "image_path": str(first),
                "reference_images": [str(first), str(second)],
                "background_description": (
                    "A fictional young Asian occult detective"
                ),
            }

            request = s3_shots._dialogue_request_kwargs(
                shot, None, character=character
            )

        self.assertNotIn("references", request)

    def test_custom_character_prompt_does_not_claim_an_image_reference(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 1,
                "duration_sec": 8,
                "scene": "A Victorian harbor",
                "voiceover": "The ritual has already begun.",
            },
            {
                "id": "custom_klein",
                "name": "Klein",
                "identity_type": "custom",
                "generation_mode": "fixed_asset",
                "image_path": "C:/characters/klein.png",
                "look": "A fictional young Asian occult detective",
            },
        )

        self.assertNotIn("固定角色身份参考", prompt)
        self.assertIn("完全虚构", prompt)

    def test_seedance_privacy_rejection_is_non_retryable(self):
        response = Mock(
            status_code=400,
            text=(
                '{"error":{"code":"InputImageSensitiveContentDetected.'
                'PrivacyInformation","message":"input may contain real person"}}'
            ),
        )

        error = seedance._submission_error(
            response,
            endpoint="https://ark.example/tasks",
            model="doubao-seedance-2-5-260628",
            content_roles=["reference_image"],
        )

        self.assertIsInstance(error, seedance.SeedanceInputPrivacyError)
        self.assertTrue(error.non_retryable)
        self.assertIn("authorized asset://", str(error))

    def test_seedance_preserves_authorized_asset_uri(self):
        self.assertEqual(
            seedance._to_image_arg("asset://asset-xiaoling-authorized"),
            "asset://asset-xiaoling-authorized",
        )

    def test_s3_resolves_real_person_asset_before_paid_generation(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        characters = [{
            "id": "sales_person",
            "name": "销售顾问",
            "identity_type": "real_person",
        }]
        resolved = Mock(
            status="resolved",
            asset_uri="asset://asset-authorized-example",
        )

        with patch.object(
            s3_shots.volc_assets, "from_environment", return_value=Mock()
        ), patch.object(
            s3_shots.character_assets,
            "resolve_character_asset",
            return_value=resolved,
        ):
            changed = s3_shots._resolve_real_person_bindings(
                database, characters
            )

        self.assertTrue(changed)
        self.assertEqual(
            characters[0]["seedance_asset_uri"],
            "asset://asset-authorized-example",
        )

    def test_followup_dialogue_reuses_native_video_only(self):
        shot = {"duration_sec": 8}
        anchor = s3_shots.DialogueAnchor(
            video_url="https://cdn/anchor.mp4",
            duration_sec=30,
        )

        request = s3_shots._dialogue_request_kwargs(shot, anchor)

        self.assertEqual(request["reference_videos"], [anchor.video_url])
        self.assertEqual(request["reference_video_duration_sec"], 30)
        self.assertNotIn("reference_audios", request)

    def test_reference_video_cost_includes_input_and_output_duration(self):
        estimate = s3_shots._estimated_request_cost_cents({
            "duration": 6,
            "resolution": "480p",
            "reference_videos": ["https://cdn/anchor.mp4"],
            "reference_video_duration_sec": 30,
        })

        self.assertEqual(estimate, 2448)

    def test_runtime_has_no_omnihuman_dependency(self):
        source = inspect.getsource(s3_shots._gen_dialogue)

        self.assertNotIn("omnihuman", source.lower())
        self.assertNotIn("audio_url", source)

    def test_generated_dialogue_returns_video_only_anchor(self):
        source = inspect.getsource(s3_shots._gen_dialogue)

        self.assertIn("seedance.normalize_generation_duration", source)
        self.assertNotIn(
            "DialogueAnchor(result.video_url, result.audio_path)", source
        )

    def test_completed_dialogue_video_can_seed_a_later_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "01.mp4"
            video.write_bytes(b"video")

            anchor = s3_shots._dialogue_anchor_from_completed_shot({
                "video_path": str(video),
                "video_url": "https://cdn/01.mp4",
                "duration_sec": 12,
            })

        self.assertEqual(anchor.video_url, "https://cdn/01.mp4")
        self.assertEqual(anchor.duration_sec, 12)

    def test_completed_short_dialogue_anchor_uses_generated_clip_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "01.mp4"
            video.write_bytes(b"video")

            anchor = s3_shots._dialogue_anchor_from_completed_shot({
                "video_path": str(video),
                "video_url": "https://cdn/01.mp4",
                "duration_sec": 0.4,
            })

        self.assertEqual(anchor.duration_sec, 4)

    def test_short_dialogue_prompt_explicitly_pads_after_editorial_end(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 1,
                "duration_sec": 0.4,
                "scene": "设备停放场",
                "voiceover": "啊！",
            },
            {
                "id": "ai-character",
                "name": "员工甲",
                "identity_type": "custom",
                "generation_mode": "ai_generated",
            },
        )

        self.assertIn("0.4-4.0秒", prompt)
        self.assertIn("供本地剪辑裁切", prompt)

    def test_native_dialogue_prompt_drops_source_on_screen_text_instructions(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 1,
                "duration_sec": 4,
                "render_mode": "cinematic_character",
                "scene": (
                    "户外设备场；人物竖起拇指；画面文字：XCMG XG10.4HD\n"
                    "众能联合 400 805 8558"
                ),
                "action": "自然交流；画面文字：订10000台设备大客户？",
                "voiceover": "这是来自非洲的客户",
                "performance_beats": [{
                    "start": 0,
                    "duration": 4,
                    "dialogue": "这是来自非洲的客户",
                    "visual_intent": (
                        "人物在设备场交流；画面文字：众能联合 400 805 8558"
                    ),
                }],
            },
            {
                "id": "ai-character",
                "name": "员工甲",
                "identity_type": "custom",
                "generation_mode": "ai_generated",
            },
        )

        self.assertIn("户外设备场", prompt)
        self.assertNotIn("XCMG", prompt)
        self.assertNotIn("众能联合 400", prompt)
        self.assertNotIn("订10000台", prompt)

    def test_short_broll_request_uses_provider_safe_duration(self):
        request = s3_shots._broll_request_kwargs({"duration_sec": 3.33})

        self.assertEqual(request["duration"], 4)


class SeedanceGenerationGateTests(unittest.IsolatedAsyncioTestCase):
    async def _measure_broll_concurrency(
        self, *, masters: list[dict], characters: list[dict] | None = None
    ) -> tuple[int, list[str]]:
        characters = list(characters or [])
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute("ALTER TABLE jobs ADD COLUMN characters_json TEXT")
        database.execute("ALTER TABLE shots ADD COLUMN narrative_type TEXT")
        database.execute("ALTER TABLE shots ADD COLUMN speaker_id TEXT")
        database.execute("ALTER TABLE shots ADD COLUMN anchor_character TEXT")
        database.execute("ALTER TABLE shots ADD COLUMN video_url TEXT")
        storyboard = {
            "masters": masters,
            "shots": [],
            "characters": characters,
        }
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,"
            "characters_json) VALUES(?,?,?,?,?,?)",
            (
                "J-BROLL-DEPS", "T-1", "broll dependencies",
                "PRODUCTION_RUNNING", json.dumps(storyboard),
                json.dumps(characters),
            ),
        )
        for master in masters:
            database.execute(
                "INSERT INTO shots(job_id,idx,state,duration_sec,shot_type,"
                "prompt,narrative_type,anchor_character) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    "J-BROLL-DEPS", master["idx"], "PENDING", 4,
                    "broll", f"scene {master['idx']}", "broll",
                    master.get("anchor_character"),
                ),
            )
        database.commit()
        active = 0
        maximum_active = 0
        paid_batch_ids: list[str] = []

        async def generate(
            _job_id, shot, _anchor_path, _sb_shot, _sem, **_kwargs
        ):
            nonlocal active, maximum_active
            paid_batch_ids.append(str(_kwargs.get("paid_batch_id") or ""))
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                await asyncio.sleep(0.04)
                return shot["idx"], None
            finally:
                active -= 1

        with (
            patch.object(s3_shots, "MAX_CONCURRENT_SHOTS", 2),
            patch.dict(
                os.environ, {"SEEDANCE_MAX_CONCURRENT_MASTERS": "2"}
            ),
            patch.object(s3_shots, "conn", return_value=database),
            patch.object(
                s3_shots.character_assets, "apply_cached_bindings",
                return_value=False,
            ),
            patch.object(
                s3_shots, "_resolve_real_person_bindings",
                return_value=False,
            ),
            patch.object(
                s3_shots.scene_assets, "apply_continuity_metadata",
                return_value=False,
            ),
            patch.object(
                s3_shots, "_ensure_character_identity_anchors",
                AsyncMock(return_value={}),
            ),
            patch.object(
                s3_shots, "_ensure_scene_visual_anchors",
                AsyncMock(return_value={}),
            ),
            patch.object(s3_shots, "_assert_continuity_anchors_approved"),
            patch.object(s3_shots, "_gen_broll", side_effect=generate),
        ):
            await s3_shots.run("J-BROLL-DEPS")
        database.close()
        return maximum_active, paid_batch_ids

    async def test_same_scene_broll_masters_stay_sequential(self):
        maximum_active, batch_ids = await self._measure_broll_concurrency(masters=[
            {
                "idx": 1, "type": "broll", "duration": 4,
                "scene_anchor_key": "yard-a",
            },
            {
                "idx": 2, "type": "broll", "duration": 4,
                "scene_anchor_key": "yard-a",
            },
        ])

        self.assertEqual(maximum_active, 1)
        self.assertEqual(len(set(batch_ids)), 2)

    async def test_broll_anchor_character_and_cast_identity_stay_sequential(self):
        lead = {
            "id": "lead", "name": "Lead", "generation_mode": "ai_generated",
            "look": "young lead",
        }
        maximum_active, batch_ids = await self._measure_broll_concurrency(
            characters=[lead],
            masters=[
                {
                    "idx": 1, "type": "broll", "duration": 4,
                    "scene_anchor_key": "yard-a", "anchor_character": "lead",
                },
                {
                    "idx": 2, "type": "broll", "duration": 4,
                    "scene_anchor_key": "office-b",
                    "cast": [{"id": "lead", "name": "Lead"}],
                },
            ],
        )

        self.assertEqual(maximum_active, 1)
        self.assertEqual(len(set(batch_ids)), 2)

    async def test_unknown_in_first_wave_blocks_dependent_wave_before_post(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute("ALTER TABLE jobs ADD COLUMN characters_json TEXT")
        database.execute("ALTER TABLE shots ADD COLUMN narrative_type TEXT")
        database.execute("ALTER TABLE shots ADD COLUMN speaker_id TEXT")
        database.execute("ALTER TABLE shots ADD COLUMN anchor_character TEXT")
        database.execute("ALTER TABLE shots ADD COLUMN video_url TEXT")
        masters = [
            {
                "idx": 1, "type": "broll", "duration": 4,
                "scene_anchor_key": "yard-a",
            },
            {
                "idx": 2, "type": "broll", "duration": 4,
                "scene_anchor_key": "yard-a",
            },
        ]
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,"
            "characters_json) VALUES(?,?,?,?,?,?)",
            (
                "J-WAVE-UNKNOWN", "T-1", "wave unknown",
                "PRODUCTION_RUNNING",
                json.dumps({"masters": masters, "shots": []}), "[]",
            ),
        )
        for master in masters:
            database.execute(
                "INSERT INTO shots(job_id,idx,state,duration_sec,shot_type,"
                "prompt,narrative_type) VALUES(?,?,?,?,?,?,?)",
                (
                    "J-WAVE-UNKNOWN", master["idx"], "PENDING", 4,
                    "broll", f"scene {master['idx']}", "broll",
                ),
            )
        database.commit()
        posts = 0
        batch_ids: list[str] = []
        submit_lock = asyncio.Lock()

        async def generate(
            job_id, shot, _anchor_path, _sb_shot, _sem, **kwargs
        ):
            nonlocal posts
            async with submit_lock:
                posts += 1
                batch_id = str(kwargs.get("paid_batch_id") or "")
                batch_ids.append(batch_id)
                reservation = provider_tasks.reserve_paid_submit(
                    database,
                    job_id=job_id,
                    shot_idx=shot["idx"],
                    provider="seedance",
                    intent="generate",
                    authorized_batch_id=batch_id,
                )
                provider_tasks.mark_action_required(
                    database, reservation, message="submit result unknown"
                )
                return (
                    shot["idx"],
                    seedance.SeedanceSubmissionUnknownError(
                        "submit result unknown"
                    ),
                )

        with (
            patch.object(s3_shots, "MAX_CONCURRENT_SHOTS", 2),
            patch.object(s3_shots, "conn", return_value=database),
            patch.object(
                s3_shots.character_assets, "apply_cached_bindings",
                return_value=False,
            ),
            patch.object(
                s3_shots, "_resolve_real_person_bindings",
                return_value=False,
            ),
            patch.object(
                s3_shots.scene_assets, "apply_continuity_metadata",
                return_value=False,
            ),
            patch.object(
                s3_shots, "_ensure_character_identity_anchors",
                AsyncMock(return_value={}),
            ),
            patch.object(
                s3_shots, "_ensure_scene_visual_anchors",
                AsyncMock(return_value={}),
            ),
            patch.object(s3_shots, "_assert_continuity_anchors_approved"),
            patch.object(s3_shots, "_gen_broll", side_effect=generate),
        ):
            with self.assertRaisesRegex(RuntimeError, "shots failed"):
                await s3_shots.run("J-WAVE-UNKNOWN")

        self.assertEqual(posts, 1)
        self.assertEqual(len(batch_ids), 1)
        with self.assertRaises(provider_tasks.ProviderTaskGateError):
            provider_tasks.begin_paid_submit_batch(database, "J-WAVE-UNKNOWN")
        self.assertEqual(
            len(provider_tasks.unresolved_task_ids(
                database, "J-WAVE-UNKNOWN"
            )),
            1,
        )
        database.close()

    async def test_run_overlaps_two_independent_masters_at_configured_concurrency(self):
        """The shot semaphore must limit real tasks, not a serial for-loop."""
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute("ALTER TABLE jobs ADD COLUMN characters_json TEXT")
        database.execute("ALTER TABLE shots ADD COLUMN narrative_type TEXT")
        database.execute("ALTER TABLE shots ADD COLUMN speaker_id TEXT")
        database.execute("ALTER TABLE shots ADD COLUMN anchor_character TEXT")
        database.execute("ALTER TABLE shots ADD COLUMN video_url TEXT")
        storyboard = {
            "masters": [
                {
                    "idx": 1, "type": "broll", "duration": 4,
                    "scene_anchor_key": "yard-a",
                },
                {
                    "idx": 2, "type": "broll", "duration": 4,
                    "scene_anchor_key": "office-b",
                },
            ],
            "shots": [],
        }
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,"
            "characters_json) VALUES(?,?,?,?,?,?)",
            (
                "J-CONCURRENT-RUN", "T-1", "concurrency",
                "PRODUCTION_RUNNING", json.dumps(storyboard), "[]",
            ),
        )
        for idx in (1, 2):
            database.execute(
                "INSERT INTO shots(job_id,idx,state,duration_sec,"
                "shot_type,prompt,narrative_type) VALUES(?,?,?,?,?,?,?)",
                (
                    "J-CONCURRENT-RUN", idx, "PENDING", 4,
                    "broll", f"scene {idx}", "broll",
                ),
            )
        database.commit()

        active = 0
        maximum_active = 0
        both_started = asyncio.Event()

        async def generate(
            _job_id, shot, _anchor_path, _sb_shot, _sem, **_kwargs
        ):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            if active == 2:
                both_started.set()
            try:
                await asyncio.wait_for(both_started.wait(), timeout=0.5)
                await asyncio.sleep(0)
                return shot["idx"], None
            finally:
                active -= 1

        with (
            patch.object(s3_shots, "MAX_CONCURRENT_SHOTS", 2),
            patch.object(s3_shots, "conn", return_value=database),
            patch.object(
                s3_shots.character_assets, "apply_cached_bindings",
                return_value=False,
            ),
            patch.object(
                s3_shots, "_resolve_real_person_bindings",
                return_value=False,
            ),
            patch.object(
                s3_shots.scene_assets, "apply_continuity_metadata",
                return_value=False,
            ),
            patch.object(
                s3_shots, "_ensure_character_identity_anchors",
                AsyncMock(return_value={}),
            ),
            patch.object(
                s3_shots, "_ensure_scene_visual_anchors",
                AsyncMock(return_value={}),
            ),
            patch.object(s3_shots, "_assert_continuity_anchors_approved"),
            patch.object(s3_shots, "_gen_broll", side_effect=generate),
        ):
            await asyncio.wait_for(
                s3_shots.run("J-CONCURRENT-RUN"), timeout=1.0
            )

        self.assertEqual(maximum_active, 2)
        database.close()

    async def test_paid_batch_allows_two_sibling_reservations_concurrently(self):
        """One batch may submit siblings without authorizing old unknown work."""
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "factory.db"
            with patch.dict(
                os.environ, {"FACTORY_DB_PATH": str(database_path)}
            ):
                setup = db.conn()
                setup.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
                    "VALUES('J-BATCH','T-1','batch','PRODUCTION_RUNNING','{}')"
                )
                setup.commit()
                try:
                    batch_id = provider_tasks.begin_paid_submit_batch(
                        setup, "J-BATCH"
                    )
                finally:
                    setup.close()

                start = asyncio.Event()

                async def reserve(idx):
                    await start.wait()

                    def blocking_reserve():
                        database = db.conn()
                        try:
                            return provider_tasks.reserve_paid_submit(
                                database,
                                job_id="J-BATCH",
                                shot_idx=idx,
                                provider="seedance",
                                intent="generate",
                                authorized_batch_id=batch_id,
                            )
                        finally:
                            database.close()

                    return await asyncio.to_thread(blocking_reserve)

                pending = [
                    asyncio.create_task(reserve(1)),
                    asyncio.create_task(reserve(2)),
                ]
                start.set()
                reservations = await asyncio.gather(*pending)

                verify = db.conn()
                rows = verify.execute(
                    "SELECT task_id,usage_json FROM provider_calls "
                    "WHERE job_id='J-BATCH' ORDER BY shot_idx"
                ).fetchall()
                verify.close()

        self.assertEqual(len(set(reservations)), 2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(
            json.loads(row["usage_json"])["batch_id"] == batch_id
            for row in rows
        ))

    async def test_concurrent_batch_reservations_cannot_oversubscribe_job_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "factory.db"
            with patch.dict(
                os.environ, {"FACTORY_DB_PATH": str(database_path)}
            ):
                setup = db.conn()
                setup.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,"
                    "cost_cents) VALUES(?,?,?,?,?,0)",
                    (
                        "J-BATCH-BUDGET", "T-1", "batch budget",
                        "PRODUCTION_RUNNING",
                        json.dumps({"production_settings": {
                            "budget_cents": 100,
                        }}),
                    ),
                )
                setup.commit()
                batch_id = provider_tasks.begin_paid_submit_batch(
                    setup, "J-BATCH-BUDGET"
                )
                setup.close()
                start = asyncio.Event()

                async def reserve(idx):
                    await start.wait()

                    def blocking_reserve():
                        database = db.conn()
                        try:
                            return provider_tasks.reserve_paid_submit(
                                database,
                                job_id="J-BATCH-BUDGET",
                                shot_idx=idx,
                                provider="seedance",
                                intent="generate",
                                authorized_batch_id=batch_id,
                                estimated_cost_cents=80,
                                budget_limit_cents=100,
                            )
                        finally:
                            database.close()

                    try:
                        return "ok", await asyncio.to_thread(blocking_reserve)
                    except Exception as exc:
                        return "error", exc

                tasks = [
                    asyncio.create_task(reserve(1)),
                    asyncio.create_task(reserve(2)),
                ]
                start.set()
                outcomes = await asyncio.gather(*tasks)
                verify = db.conn()
                reservations = verify.execute(
                    "SELECT COUNT(*) FROM provider_calls "
                    "WHERE job_id='J-BATCH-BUDGET'"
                ).fetchone()[0]
                verify.close()

        self.assertEqual([item[0] for item in outcomes].count("ok"), 1)
        failure = next(item[1] for item in outcomes if item[0] == "error")
        self.assertIsInstance(
            failure, provider_tasks.ProviderTaskBudgetError
        )
        self.assertEqual(reservations, 1)

    async def test_cancelled_broll_does_not_leave_shot_generating(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-CANCEL-WAVE','T-1','cancel','PRODUCTION_RUNNING','{}')"
        )
        database.execute(
            "INSERT INTO shots(job_id,idx,state,duration_sec,shot_type,prompt) "
            "VALUES('J-CANCEL-WAVE',1,'PENDING',4,'broll','equipment')"
        )
        database.commit()
        entered = asyncio.Event()

        async def wait_forever(**_kwargs):
            entered.set()
            await asyncio.Event().wait()

        with (
            patch.object(
                s3_shots, "conn", return_value=_NonClosingConnection(database)
            ),
            patch.object(
                s3_shots, "_generate_seedance_video",
                side_effect=wait_forever,
            ),
        ):
            task = asyncio.create_task(s3_shots._gen_broll(
                "J-CANCEL-WAVE",
                {
                    "idx": 1, "duration_sec": 4,
                    "prompt": "equipment", "camera": "fixed",
                },
                None,
                {},
                asyncio.Semaphore(1),
            ))
            await asyncio.wait_for(entered.wait(), timeout=0.5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        state = database.execute(
            "SELECT state FROM shots WHERE job_id='J-CANCEL-WAVE' AND idx=1"
        ).fetchone()[0]
        self.assertNotEqual(state, "GENERATING")
        database.close()

    async def test_batch_never_authorizes_an_older_unresolved_task(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,"
            "intent,usage_json) VALUES(?,?,?,?,?,?)",
            (
                "task-old", "J-HISTORY", 9, "seedance", "generate",
                json.dumps({"status": "unknown", "batch_id": "old-batch"}),
            ),
        )
        database.commit()

        with self.assertRaises(provider_tasks.ProviderTaskGateError):
            provider_tasks.begin_paid_submit_batch(database, "J-HISTORY")

        self.assertEqual(
            provider_tasks.unresolved_task_ids(database, "J-HISTORY"),
            ["task-old"],
        )
        database.close()

    async def test_account_failure_blocks_a_waiting_sibling_before_post(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            database_path = Path(tmp) / "factory.db"
            storyboard = {
                "masters": [
                    {"idx": 1, "type": "broll", "duration": 4},
                    {"idx": 2, "type": "broll", "duration": 4},
                ],
                "shots": [],
            }
            with patch.dict(
                os.environ, {"FACTORY_DB_PATH": str(database_path)}
            ):
                setup = db.conn()
                setup.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,"
                    "characters_json) VALUES(?,?,?,?,?,?)",
                    (
                        "J-ACCOUNT-WAVE", "T-1", "account wave",
                        "PRODUCTION_RUNNING", json.dumps(storyboard), "[]",
                    ),
                )
                for idx in (1, 2):
                    setup.execute(
                        "INSERT INTO shots(job_id,idx,state,duration_sec,"
                        "shot_type,prompt,narrative_type) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            "J-ACCOUNT-WAVE", idx, "PENDING", 4,
                            "broll", f"equipment {idx}", "broll",
                        ),
                    )
                setup.commit()
                setup.close()
                posts = 0

                async def account_failure(*_args, **_kwargs):
                    nonlocal posts
                    posts += 1
                    raise seedance.SeedanceAccountUnavailableError(
                        "account overdue"
                    )

                route = video_routing.VideoRoute(
                    model="seedance-test", tier="seedance25",
                    generate_audio=False, qa_required=False,
                    fallback_model=None,
                )
                with (
                    patch.object(s3_shots, "MAX_CONCURRENT_SHOTS", 1),
                    patch.object(
                        s3_shots.character_assets, "apply_cached_bindings",
                        return_value=False,
                    ),
                    patch.object(
                        s3_shots, "_resolve_real_person_bindings",
                        return_value=False,
                    ),
                    patch.object(
                        s3_shots.scene_assets, "apply_continuity_metadata",
                        return_value=False,
                    ),
                    patch.object(
                        s3_shots, "_ensure_character_identity_anchors",
                        AsyncMock(return_value={}),
                    ),
                    patch.object(
                        s3_shots, "_ensure_scene_visual_anchors",
                        AsyncMock(return_value={}),
                    ),
                    patch.object(
                        s3_shots, "_assert_continuity_anchors_approved",
                    ),
                    patch.object(s3_shots, "select_route", return_value=route),
                    patch.object(
                        s3_shots.seedance, "text_to_video",
                        side_effect=account_failure,
                    ),
                    patch.object(s3_shots, "ASSETS_DIR", Path(tmp)),
                ):
                    with self.assertRaisesRegex(RuntimeError, "shots failed"):
                        await s3_shots.run("J-ACCOUNT-WAVE")

                verify = db.conn()
                states = [
                    row[0] for row in verify.execute(
                        "SELECT state FROM shots WHERE job_id='J-ACCOUNT-WAVE' "
                        "ORDER BY idx"
                    ).fetchall()
                ]
                runtime = verify.execute(
                    "SELECT status FROM provider_runtime "
                    "WHERE provider='seedance'"
                ).fetchone()[0]
                verify.close()

        self.assertEqual(posts, 1)
        self.assertEqual(states, ["FAILED", "FAILED"])
        self.assertEqual(runtime, "action_required")

    async def test_dialogue_final_update_failure_rolls_back_before_failure_record(self):
        await self._assert_final_accounting_failure_is_recoverable(
            dialogue=True, fail_sql="UPDATE shots SET video_path="
        )

    async def test_broll_final_commit_failure_rolls_back_before_failure_record(self):
        await self._assert_final_accounting_failure_is_recoverable(
            dialogue=False, fail_commit=True
        )

    async def _assert_final_accounting_failure_is_recoverable(
        self, *, dialogue: bool, fail_sql: str = "", fail_commit: bool = False
    ):
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "factory.db"
            with patch.dict(os.environ, {"FACTORY_DB_PATH": str(database_path)}):
                setup = db.conn()
                setup.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
                    "VALUES('J-FINAL-TX','T-1','final tx','PRODUCTION_RUNNING','{}',0)"
                )
                setup.execute(
                    "INSERT INTO shots(job_id,idx,state,cost_cents,duration_sec,prompt) "
                    "VALUES('J-FINAL-TX',1,'GENERATING',0,4,'equipment')"
                )
                setup.execute(
                    "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
                    "usage_json,cost_cents) VALUES('task-staged','J-FINAL-TX',1,"
                    "'seedance','generate',?,0)",
                    (json.dumps({
                        "status": "staged",
                        "artifact_path": str(Path(tmp) / "candidate.mp4"),
                    }),),
                )
                setup.commit()
                setup.close()

                candidate = Path(tmp) / "candidate.mp4"
                candidate.write_bytes(b"video")
                audio = Path(tmp) / "candidate.wav"
                audio.write_bytes(b"audio")
                result = s3_shots.SeedanceGenerationResult(
                    video=b"video",
                    video_path=candidate,
                    video_url="https://assets/video.mp4",
                    qa_note="{}",
                    native_audio=dialogue,
                    model="seedance-test",
                    audio_path=audio if dialogue else None,
                    cost_cents=29,
                    provider_calls=({
                        "task_id": "task-staged",
                        "intent": "generate",
                        "usage": {"status": "completed"},
                        "cost_cents": 29,
                    },),
                )

                final_raw = sqlite3.connect(database_path, timeout=0.02)
                final_raw.row_factory = sqlite3.Row
                failure_raw = sqlite3.connect(database_path, timeout=0.02)
                failure_raw.row_factory = sqlite3.Row
                final_connection = _InjectedFinalConnection(
                    final_raw, fail_sql=fail_sql, fail_commit=fail_commit
                )
                failure_connection = _InjectedFinalConnection(failure_raw)
                original = (
                    "injected final commit failure" if fail_commit
                    else "injected final accounting failure"
                )
                route = video_routing.VideoRoute(
                    model="seedance-test", tier="seedance25",
                    generate_audio=dialogue, qa_required=False,
                    fallback_model=None,
                )
                patches = (
                    patch.object(s3_shots, "conn", side_effect=[
                        final_connection, failure_connection,
                    ]),
                    patch.object(s3_shots, "_mark_shot_generating"),
                    patch.object(
                        s3_shots, "_generate_seedance_video",
                        new=AsyncMock(return_value=result),
                    ),
                    patch.object(s3_shots, "select_route", return_value=route),
                    patch.object(s3_shots, "_persist_master_transcription"),
                )
                with patches[0], patches[1], patches[2], patches[3], patches[4]:
                    if dialogue:
                        idx, caught, _anchor = await s3_shots._gen_dialogue(
                            "J-FINAL-TX",
                            {"idx": 1, "duration_sec": 4, "voiceover": "line"},
                            {"id": "lead", "name": "Lead"},
                            {"voiceover": "line"},
                            asyncio.Semaphore(1),
                        )
                    else:
                        idx, caught = await s3_shots._gen_broll(
                            "J-FINAL-TX",
                            {
                                "idx": 1, "duration_sec": 4,
                                "prompt": "equipment", "camera": "fixed",
                            },
                            None,
                            {},
                            asyncio.Semaphore(1),
                        )

                self.assertEqual(idx, 1)
                self.assertIsInstance(caught, sqlite3.OperationalError)
                self.assertIn(original, str(caught))
                self.assertNotIn("locked", str(caught).casefold())
                self.assertTrue(final_connection.rolled_back)
                self.assertTrue(final_connection.closed)
                self.assertTrue(failure_connection.closed)

                verify = db.conn()
                shot_row = verify.execute(
                    "SELECT state,cost_cents,error FROM shots "
                    "WHERE job_id='J-FINAL-TX' AND idx=1"
                ).fetchone()
                provider_row = verify.execute(
                    "SELECT cost_cents,usage_json FROM provider_calls "
                    "WHERE task_id='task-staged'"
                ).fetchone()
                self.assertEqual(shot_row["state"], "FAILED")
                self.assertEqual(shot_row["cost_cents"], 0)
                self.assertIn(original, shot_row["error"])
                self.assertEqual(provider_row["cost_cents"], 0)
                self.assertEqual(
                    json.loads(provider_row["usage_json"])["status"], "staged"
                )
                with self.assertRaises(provider_tasks.ProviderTaskGateError):
                    provider_tasks.reserve_paid_submit(
                        verify,
                        job_id="J-FINAL-TX",
                        shot_idx=1,
                        provider="seedance",
                        intent="generate",
                    )
                verify.close()

    async def test_real_client_completed_callback_allows_authorized_qa_retry_once(self):
        import httpx

        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-REAL-RETRY','T-1','real retry','PRODUCTION_RUNNING','{}',0)"
        )
        database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents) "
            "VALUES('J-REAL-RETRY',1,'GENERATING',0)"
        )
        database.commit()
        counts = {"post": 0, "poll": 0, "download": 0}

        def handler(provider_request):
            if provider_request.method == "POST":
                counts["post"] += 1
                return httpx.Response(200, json={
                    "id": f"task-real-{counts['post']}"
                })
            if "/tasks/task-real-" in provider_request.url.path:
                counts["poll"] += 1
                task_id = provider_request.url.path.rsplit("/", 1)[-1]
                return httpx.Response(200, json={
                    "status": "completed",
                    "content": {
                        "video_url": f"https://media.example/{task_id}.mp4"
                    },
                    "usage": {"completion_tokens": 1000},
                })
            counts["download"] += 1
            return httpx.Response(200, content=b"video")

        real_async_client = httpx.AsyncClient

        def client_factory(*_args, **kwargs):
            return real_async_client(
                transport=httpx.MockTransport(handler),
                headers=kwargs.get("headers"),
            )
        failed = video_qa.QADecision(False, ("unexpected_readable_text",))
        passed = video_qa.QADecision(True, ())
        route = video_routing.VideoRoute(
            model="seedance-test", tier="seedance25",
            generate_audio=False, qa_required=True, fallback_model=None,
        )
        temp = tempfile.TemporaryDirectory()
        tmp = temp.name
        with (
            patch.dict(os.environ, {
                "ARK_API_KEY": "test-key", "SEEDANCE_MAX_AUTO_RETRIES": "1",
                "PROVIDER_DOWNLOAD_TRUSTED_HOSTS": "media.example",
            }),
            patch(
                "socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("8.8.8.8", 443))],
            ),
            patch.object(
                seedance.httpx, "AsyncClient", side_effect=client_factory
            ),
            patch.object(seedance.asyncio, "sleep", new=AsyncMock()),
            patch.object(s3_shots.tos, "upload", return_value="https://qa/video"),
            patch.object(
                s3_shots.video_qa, "analyze",
                side_effect=[({"has_readable_text": True}, failed), ({}, passed)],
            ),
            patch.object(
                s3_shots.video_qa, "evaluate", side_effect=[failed, passed]
            ),
        ):
            result = await s3_shots._generate_seedance_video(
                prompt="equipment",
                route=route,
                request_kwargs={"duration": 4, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="real-retry",
                job_id="J-REAL-RETRY",
                shot_idx=1,
                database=database,
            )

        self.assertEqual(counts, {"post": 2, "poll": 2, "download": 2})
        self.assertEqual(result.cost_cents, 14)
        staged_rows = database.execute(
            "SELECT task_id,cost_cents,usage_json FROM provider_calls "
            "ORDER BY task_id"
        ).fetchall()
        self.assertEqual(
            [json.loads(row["usage_json"])["status"] for row in staged_rows],
            ["staged", "staged"],
        )
        self.assertEqual([row["cost_cents"] for row in staged_rows], [0, 0])
        for row in staged_rows:
            usage = json.loads(row["usage_json"])
            self.assertEqual(usage["remote_status"], "completed")
            self.assertTrue(Path(usage["artifact_path"]).is_file())
        blocked_submit = AsyncMock(return_value=b"must-not-submit")
        with patch.object(
            s3_shots.seedance, "text_to_video", new=blocked_submit
        ):
            with self.assertRaises(provider_tasks.ProviderTaskGateError):
                await s3_shots._generate_seedance_video(
                    prompt="equipment",
                    route=route,
                    request_kwargs={"duration": 4, "resolution": "480p"},
                    out_path=result.video_path,
                    key_hint="crash-recovery",
                    job_id="J-REAL-RETRY",
                    shot_idx=1,
                    database=database,
                )
        blocked_submit.assert_not_awaited()

        first_delta = s3_shots._record_provider_calls(
            database, "J-REAL-RETRY", 1, "seedance", result.provider_calls
        )
        database.execute(
            "UPDATE shots SET state='OK',video_path=?,"
            "cost_cents=cost_cents+? WHERE job_id='J-REAL-RETRY' AND idx=1",
            (str(result.video_path), first_delta),
        )
        database.execute(
            "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id='J-REAL-RETRY'",
            (first_delta,),
        )
        database.commit()
        second_delta = s3_shots._record_provider_calls(
            database, "J-REAL-RETRY", 1, "seedance", result.provider_calls
        )
        rows = database.execute(
            "SELECT task_id,cost_cents,usage_json FROM provider_calls "
            "ORDER BY task_id"
        ).fetchall()
        self.assertEqual([row["task_id"] for row in rows], [
            "task-real-1", "task-real-2",
        ])
        self.assertEqual((first_delta, second_delta), (14, 0))
        self.assertEqual(sum(row["cost_cents"] for row in rows), 14)
        self.assertEqual(
            [json.loads(row["usage_json"])["status"] for row in rows],
            ["completed", "completed"],
        )
        self.assertEqual(database.execute(
            "SELECT cost_cents FROM shots WHERE job_id='J-REAL-RETRY' AND idx=1"
        ).fetchone()[0], 14)
        self.assertEqual(database.execute(
            "SELECT cost_cents FROM jobs WHERE job_id='J-REAL-RETRY'"
        ).fetchone()[0], 14)
        temp.cleanup()
        database.close()

    async def test_real_client_single_attempt_has_one_cost_landing(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-REAL-SINGLE','T-1','real single',"
            "'PRODUCTION_RUNNING','{}',0)"
        )
        database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents) "
            "VALUES('J-REAL-SINGLE',1,'GENERATING',0)"
        )
        database.commit()
        counts = {"post": 0, "poll": 0, "download": 0}

        def handler(request):
            if request.method == "POST":
                counts["post"] += 1
                return httpx.Response(200, json={"id": "task-real-single"})
            if request.url.path.endswith("/tasks/task-real-single"):
                counts["poll"] += 1
                return httpx.Response(200, json={
                    "status": "completed",
                    "content": {
                        "video_url": "https://media.example/single.mp4"
                    },
                    "usage": {"completion_tokens": 1000},
                })
            counts["download"] += 1
            return httpx.Response(200, content=b"single-video")

        real_async_client = httpx.AsyncClient

        def client_factory(*_args, **kwargs):
            return real_async_client(
                transport=httpx.MockTransport(handler),
                headers=kwargs.get("headers"),
            )
        route = video_routing.VideoRoute(
            model="seedance-test", tier="seedance25",
            generate_audio=False, qa_required=False, fallback_model=None,
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {
                "ARK_API_KEY": "test-key",
                "PROVIDER_DOWNLOAD_TRUSTED_HOSTS": "media.example",
            }
        ), patch(
            "socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("8.8.8.8", 443))],
        ), patch.object(
            seedance.httpx, "AsyncClient", side_effect=client_factory
        ), patch.object(
            seedance.asyncio, "sleep", new=AsyncMock()
        ), patch.object(
            s3_shots.tos, "upload", return_value="https://qa/single"
        ):
            result = await s3_shots._generate_seedance_video(
                prompt="equipment",
                route=route,
                request_kwargs={"duration": 4, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="real-single",
                job_id="J-REAL-SINGLE",
                shot_idx=1,
                database=database,
            )

        first_delta = s3_shots._record_provider_calls(
            database, "J-REAL-SINGLE", 1, "seedance", result.provider_calls
        )
        database.execute(
            "UPDATE shots SET state='OK',cost_cents=cost_cents+? "
            "WHERE job_id='J-REAL-SINGLE' AND idx=1",
            (first_delta,),
        )
        database.execute(
            "UPDATE jobs SET cost_cents=cost_cents+? "
            "WHERE job_id='J-REAL-SINGLE'",
            (first_delta,),
        )
        database.commit()
        second_delta = s3_shots._record_provider_calls(
            database, "J-REAL-SINGLE", 1, "seedance", result.provider_calls
        )
        provider_sum = database.execute(
            "SELECT COALESCE(SUM(cost_cents),0) FROM provider_calls "
            "WHERE job_id='J-REAL-SINGLE'"
        ).fetchone()[0]
        shot_cost = database.execute(
            "SELECT cost_cents FROM shots "
            "WHERE job_id='J-REAL-SINGLE' AND idx=1"
        ).fetchone()[0]
        job_cost = database.execute(
            "SELECT cost_cents FROM jobs WHERE job_id='J-REAL-SINGLE'"
        ).fetchone()[0]

        self.assertEqual(counts, {"post": 1, "poll": 1, "download": 1})
        self.assertEqual(len(result.provider_calls), 1)
        self.assertEqual(result.cost_cents, 7)
        self.assertEqual(
            (result.cost_cents, provider_sum, shot_cost, job_cost),
            (7, 7, 7, 7),
        )
        self.assertEqual((first_delta, second_delta), (7, 0))
        database.close()

    async def test_s3_promotion_failure_keeps_remote_alias_and_blocks_repost(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        job_id = "J-S3-PROMOTE-FAIL"
        remote_id = "seedance-promote-fail-1"
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES(?, 'T-1', 's3 promote fail', 'PRODUCTION_RUNNING', '{}', 0)",
            (job_id,),
        )
        database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents) "
            "VALUES(?,1,'GENERATING',0)",
            (job_id,),
        )
        database.execute(
            "CREATE TRIGGER fail_s3_remote_promotion "
            "BEFORE INSERT ON provider_calls "
            "WHEN NEW.task_id='seedance-promote-fail-1' "
            "BEGIN SELECT RAISE(ABORT,'s3 remote insert failed'); END"
        )
        database.commit()
        posts = 0

        def handler(request):
            nonlocal posts
            posts += 1
            return httpx.Response(200, json={"id": remote_id})

        real_async_client = httpx.AsyncClient

        def client_factory(*_args, **kwargs):
            return real_async_client(
                transport=httpx.MockTransport(handler),
                headers=kwargs.get("headers"),
            )

        route = video_routing.VideoRoute(
            model="seedance-test", tier="seedance25",
            generate_audio=False, qa_required=False, fallback_model=None,
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"ARK_API_KEY": "test-key"}
        ), patch.object(
            seedance.httpx, "AsyncClient", side_effect=client_factory
        ):
            kwargs = dict(
                prompt="equipment",
                route=route,
                request_kwargs={"duration": 4, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="s3-promote-fail",
                job_id=job_id,
                shot_idx=1,
                database=database,
            )
            with self.assertRaises(seedance.SeedanceSubmissionUnknownError):
                await s3_shots._generate_seedance_video(**kwargs)
            with self.assertRaises(provider_tasks.ProviderTaskGateError):
                await s3_shots._generate_seedance_video(**kwargs)

        row = database.execute(
            "SELECT task_id,provider,intent,usage_json FROM provider_calls"
        ).fetchone()
        usage = json.loads(row["usage_json"])
        self.assertEqual(posts, 1)
        self.assertTrue(row["task_id"].startswith("local-submit:"))
        self.assertEqual((row["provider"], row["intent"]), (
            "seedance", "generate",
        ))
        self.assertEqual(usage["status"], "action_required")
        self.assertEqual(usage["remote_task_id"], remote_id)
        self.assertEqual(usage["provider"], "seedance")
        self.assertEqual(usage["intent"], "generate")
        database.close()

    async def test_s3_rejects_remote_id_equal_to_local_reservation(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        job_id = "J-S3-SELF-PROMOTE"
        reservation_id = f"local-submit:seedance:{job_id}:1:generate"
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES(?, 'T-1', 'self promote', 'PRODUCTION_RUNNING', '{}', 0)",
            (job_id,),
        )
        database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents) "
            "VALUES(?,1,'GENERATING',0)",
            (job_id,),
        )
        database.commit()
        posts = 0

        def handler(request):
            nonlocal posts
            if request.method == "POST":
                posts += 1
            return httpx.Response(200, json={"id": reservation_id})

        real_async_client = httpx.AsyncClient

        def client_factory(*_args, **kwargs):
            return real_async_client(
                transport=httpx.MockTransport(handler),
                headers=kwargs.get("headers"),
            )

        route = video_routing.VideoRoute(
            model="seedance-test", tier="seedance25",
            generate_audio=False, qa_required=False, fallback_model=None,
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"ARK_API_KEY": "test-key"}
        ), patch.object(
            seedance.httpx, "AsyncClient", side_effect=client_factory
        ), patch.object(seedance.asyncio, "sleep", new=AsyncMock()):
            kwargs = dict(
                prompt="equipment",
                route=route,
                request_kwargs={
                    "duration": 4, "resolution": "480p", "timeout": 5,
                },
                out_path=Path(tmp) / "shot.mp4",
                key_hint="s3-self-promote",
                job_id=job_id,
                shot_idx=1,
                database=database,
            )
            with self.assertRaises(seedance.SeedanceSubmissionUnknownError):
                await s3_shots._generate_seedance_video(**kwargs)
            with self.assertRaises(provider_tasks.ProviderTaskGateError):
                await s3_shots._generate_seedance_video(**kwargs)

        row = database.execute(
            "SELECT task_id,usage_json FROM provider_calls WHERE task_id=?",
            (reservation_id,),
        ).fetchone()
        self.assertEqual(posts, 1)
        self.assertIsNotNone(row)
        usage = json.loads(row["usage_json"])
        self.assertEqual(usage["remote_task_id"], reservation_id)
        self.assertEqual(usage["status"], "action_required")
        database.close()

    async def test_real_video_client_cancellation_keeps_gate_closed(self):
        import httpx

        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-VIDEO-CANCEL','T-1','cancel','PRODUCTION_RUNNING','{}')"
        )
        database.commit()
        posts = 0

        def handler(_provider_request):
            nonlocal posts
            posts += 1
            raise asyncio.CancelledError()

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        route = video_routing.VideoRoute(
            model="seedance-test", tier="seedance25",
            generate_audio=False, qa_required=False, fallback_model=None,
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"ARK_API_KEY": "test-key"}),
            patch.object(seedance.httpx, "AsyncClient", return_value=client),
        ):
            kwargs = dict(
                prompt="equipment",
                route=route,
                request_kwargs={"duration": 4, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="cancel",
                job_id="J-VIDEO-CANCEL",
                shot_idx=1,
                database=database,
            )
            with self.assertRaises(asyncio.CancelledError):
                await s3_shots._generate_seedance_video(**kwargs)
            with self.assertRaises(provider_tasks.ProviderTaskGateError):
                await s3_shots._generate_seedance_video(**kwargs)

        self.assertEqual(posts, 1)
        row = database.execute("SELECT task_id,usage_json FROM provider_calls").fetchone()
        self.assertTrue(row["task_id"].startswith("local-submit:"))
        self.assertEqual(json.loads(row["usage_json"])["status"], "action_required")
        database.close()

    async def test_static_identity_disk_failure_keeps_staging_and_never_reposts(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-IMAGE-DISK','T-1','image disk','PRODUCTION_RUNNING','{}',0)"
        )
        database.commit()
        character = {
            "id": "lead", "name": "Lead", "generation_mode": "ai_generated",
            "look": "worker in a blue jacket",
        }
        submit = AsyncMock(return_value=b"png")

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(s3_shots, "conn", return_value=database),
            patch.object(s3_shots, "ASSETS_DIR", Path(tmp)),
            patch.object(s3_shots.seedance, "text_to_image", new=submit),
            patch.object(Path, "write_bytes", side_effect=OSError("disk full")),
        ):
            with self.assertRaises(OSError):
                await s3_shots._ensure_character_identity_anchors(
                    "J-IMAGE-DISK", [character]
                )
            with self.assertRaises(provider_tasks.ProviderTaskGateError):
                await s3_shots._ensure_character_identity_anchors(
                    "J-IMAGE-DISK", [character]
                )

        self.assertEqual(submit.await_count, 1)
        row = database.execute(
            "SELECT usage_json FROM provider_calls"
        ).fetchone()
        self.assertEqual(json.loads(row["usage_json"])["status"], "action_required")
        self.assertEqual(database.execute(
            "SELECT cost_cents FROM jobs WHERE job_id='J-IMAGE-DISK'"
        ).fetchone()[0], 0)
        database.close()

    async def test_static_identity_db_failure_keeps_staging_and_never_reposts(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-IMAGE-DB','T-1','image db','PRODUCTION_RUNNING','{}',0)"
        )
        database.commit()
        character = {
            "id": "lead", "name": "Lead", "generation_mode": "ai_generated",
            "look": "worker in a blue jacket",
        }
        submit = AsyncMock(return_value=b"png")

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(s3_shots, "conn", return_value=database),
            patch.object(s3_shots, "ASSETS_DIR", Path(tmp)),
            patch.object(s3_shots.seedance, "text_to_image", new=submit),
            patch.object(
                s3_shots.provider_tasks, "upsert_final_call",
                side_effect=sqlite3.OperationalError("db unavailable"),
            ),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                await s3_shots._ensure_character_identity_anchors(
                    "J-IMAGE-DB", [character]
                )
            with self.assertRaises(provider_tasks.ProviderTaskGateError):
                await s3_shots._ensure_character_identity_anchors(
                    "J-IMAGE-DB", [character]
                )

        self.assertEqual(submit.await_count, 1)
        row = database.execute("SELECT usage_json FROM provider_calls").fetchone()
        self.assertEqual(json.loads(row["usage_json"])["status"], "action_required")
        self.assertEqual(database.execute(
            "SELECT cost_cents FROM jobs WHERE job_id='J-IMAGE-DB'"
        ).fetchone()[0], 0)
        database.close()

    async def test_real_image_client_2xx_contract_failures_are_not_reposted(self):
        import httpx

        responses = (
            ("malformed-json", lambda: httpx.Response(200, text="not-json")),
            ("missing-url", lambda: httpx.Response(200, json={"data": []})),
        )
        for label, response_factory in responses:
            with self.subTest(label=label):
                database = sqlite3.connect(":memory:")
                database.row_factory = sqlite3.Row
                database.executescript(db.DDL)
                database.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
                    "VALUES('J-IMAGE-CONTRACT','T-1','image contract','PRODUCTION_RUNNING','{}')"
                )
                database.commit()
                posts = 0

                def handler(provider_request):
                    nonlocal posts
                    self.assertEqual(provider_request.method, "POST")
                    posts += 1
                    return response_factory()

                client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                kwargs = dict(
                    job_id="J-IMAGE-CONTRACT",
                    shot_idx=-2001,
                    provider="seedream_scene",
                    intent="scene_visual_anchor",
                    task_id="seedream-scene:J-IMAGE-CONTRACT:yard",
                    prompt="empty yard",
                    resolution="1440x2560",
                    model="seedream-test",
                    usage={},
                    cost_cents=29,
                )
                with (
                    patch.dict(os.environ, {"ARK_API_KEY": "test-key"}),
                    patch.object(seedance.httpx, "AsyncClient", return_value=client),
                ):
                    with self.assertRaises(seedance.SeedanceSubmissionUnknownError):
                        await s3_shots._submit_paid_image(database, **kwargs)
                    with self.assertRaisesRegex(Exception, "local-submit|核对"):
                        await s3_shots._submit_paid_image(database, **kwargs)

                self.assertEqual(posts, 1)
                row = database.execute(
                    "SELECT task_id,usage_json FROM provider_calls"
                ).fetchone()
                self.assertTrue(row["task_id"].startswith("local-submit:"))
                self.assertEqual(
                    json.loads(row["usage_json"])["status"], "action_required"
                )
                database.close()

    async def test_real_image_client_cancellation_is_not_reposted(self):
        import httpx

        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-IMAGE-CANCEL','T-1','image cancel','PRODUCTION_RUNNING','{}')"
        )
        database.commit()
        posts = 0

        def handler(_provider_request):
            nonlocal posts
            posts += 1
            raise asyncio.CancelledError()

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        kwargs = dict(
            job_id="J-IMAGE-CANCEL",
            shot_idx=-1001,
            provider="seedream_identity",
            intent="character_identity_anchor",
            task_id="seedream-identity:J-IMAGE-CANCEL:lead",
            prompt="portrait",
            resolution="1440x2560",
            model="seedream-test",
            usage={},
            cost_cents=29,
        )
        with (
            patch.dict(os.environ, {"ARK_API_KEY": "test-key"}),
            patch.object(seedance.httpx, "AsyncClient", return_value=client),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await s3_shots._submit_paid_image(database, **kwargs)
            with self.assertRaisesRegex(Exception, "local-submit|核对"):
                await s3_shots._submit_paid_image(database, **kwargs)

        self.assertEqual(posts, 1)
        row = database.execute("SELECT task_id,usage_json FROM provider_calls").fetchone()
        self.assertTrue(row["task_id"].startswith("local-submit:"))
        self.assertEqual(json.loads(row["usage_json"])["status"], "action_required")
        database.close()

    async def test_real_image_client_connect_failure_releases_reservation(self):
        import httpx

        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-IMAGE-CONNECT','T-1','image connect','PRODUCTION_RUNNING','{}')"
        )
        database.commit()

        def handler(provider_request):
            raise httpx.ConnectError("connection refused", request=provider_request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with (
            patch.dict(os.environ, {"ARK_API_KEY": "test-key"}),
            patch.object(seedance.httpx, "AsyncClient", return_value=client),
        ):
            with self.assertRaises(seedance.SeedanceSubmissionUnknownError) as raised:
                await s3_shots._submit_paid_image(
                    database,
                    job_id="J-IMAGE-CONNECT",
                    shot_idx=-1001,
                    provider="seedream_identity",
                    intent="character_identity_anchor",
                    task_id="seedream-identity:J-IMAGE-CONNECT:lead",
                    prompt="portrait",
                    resolution="1440x2560",
                    model="seedream-test",
                    usage={},
                    cost_cents=29,
                )

        self.assertTrue(raised.exception.retryable)
        self.assertEqual(
            database.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0], 0
        )
        database.close()

    async def test_shared_paid_image_gate_blocks_pending_and_action_required_before_post(self):
        for status in ("pending", "action_required"):
            with self.subTest(status=status):
                database = sqlite3.connect(":memory:")
                database.row_factory = sqlite3.Row
                database.executescript(db.DDL)
                database.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
                    "VALUES('J-IMAGE-GATE','T-1','image gate','PRODUCTION_RUNNING','{}')"
                )
                database.execute(
                    "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,usage_json) "
                    "VALUES('remote-image','J-IMAGE-GATE',1,'seedream_scene','generate',?)",
                    (json.dumps({"status": status}),),
                )
                database.commit()
                submit = AsyncMock(return_value=b"must-not-submit")

                with patch.object(s3_shots.seedance, "text_to_image", new=submit):
                    with self.assertRaisesRegex(Exception, "remote-image|核对") as raised:
                        await s3_shots._submit_paid_image(
                            database,
                            job_id="J-IMAGE-GATE",
                            shot_idx=-2001,
                            provider="seedream_scene",
                            intent="scene_visual_anchor",
                            task_id="seedream-scene:J-IMAGE-GATE:yard",
                            prompt="empty equipment yard",
                            resolution="1440x2560",
                            model="seedream-test",
                            usage={"anchor_key": "yard"},
                            cost_cents=29,
                        )

                submit.assert_not_awaited()
                self.assertEqual(
                    getattr(raised.exception, "pause_state", None),
                    "PROVIDER_ACTION_REQUIRED",
                )
                database.close()

    async def test_paid_image_success_remains_staged_until_asset_commit(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-IMAGE-OK','T-1','image ok','PRODUCTION_RUNNING','{}')"
        )
        database.commit()

        with patch.object(
            s3_shots.seedance, "text_to_image", new=AsyncMock(return_value=b"png")
        ) as submit:
            image, reservation_id = await s3_shots._submit_paid_image(
                database,
                job_id="J-IMAGE-OK",
                shot_idx=-1001,
                provider="seedream_identity",
                intent="character_identity_anchor",
                task_id="seedream-identity:J-IMAGE-OK:lead",
                prompt="portrait",
                resolution="1440x2560",
                model="seedream-test",
                usage={"anchor_mode": "static_image"},
                cost_cents=29,
            )

        self.assertEqual(image, b"png")
        self.assertTrue(reservation_id.startswith("local-submit:"))
        submit.assert_awaited_once()
        rows = database.execute(
            "SELECT task_id,cost_cents,usage_json FROM provider_calls ORDER BY task_id"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], reservation_id)
        self.assertEqual(rows[0]["cost_cents"], 0)
        self.assertEqual(
            json.loads(rows[0]["usage_json"])["status"], "action_required"
        )
        database.close()

    async def test_paid_image_definite_failure_releases_reservation(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-IMAGE-FAIL','T-1','image fail','PRODUCTION_RUNNING','{}')"
        )
        database.commit()

        with patch.object(
            s3_shots.seedance, "text_to_image",
            new=AsyncMock(side_effect=seedance.SeedanceParameterError("bad request")),
        ):
            with self.assertRaises(seedance.SeedanceParameterError):
                await s3_shots._submit_paid_image(
                    database,
                    job_id="J-IMAGE-FAIL",
                    shot_idx=-1001,
                    provider="seedream_identity",
                    intent="character_identity_anchor",
                    task_id="seedream-identity:J-IMAGE-FAIL:lead",
                    prompt="portrait",
                    resolution="1440x2560",
                    model="seedream-test",
                    usage={},
                    cost_cents=29,
                )

        self.assertEqual(
            database.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0], 0
        )
        database.close()

    async def test_paid_image_unknown_submit_stays_action_required_and_is_not_reposted(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-IMAGE-UNKNOWN','T-1','image unknown','PRODUCTION_RUNNING','{}')"
        )
        database.commit()
        unknown = seedance.SeedanceSubmissionUnknownError("submit result unknown")
        submit = AsyncMock(side_effect=unknown)
        kwargs = dict(
            job_id="J-IMAGE-UNKNOWN",
            shot_idx=-2001,
            provider="seedream_scene",
            intent="scene_visual_anchor",
            task_id="seedream-scene:J-IMAGE-UNKNOWN:yard",
            prompt="empty yard",
            resolution="1440x2560",
            model="seedream-test",
            usage={},
            cost_cents=29,
        )

        with patch.object(s3_shots.seedance, "text_to_image", new=submit):
            with self.assertRaises(seedance.SeedanceSubmissionUnknownError):
                await s3_shots._submit_paid_image(database, **kwargs)
            with self.assertRaisesRegex(Exception, "local-submit|核对"):
                await s3_shots._submit_paid_image(database, **kwargs)

        self.assertEqual(submit.await_count, 1)
        row = database.execute(
            "SELECT task_id,usage_json FROM provider_calls"
        ).fetchone()
        self.assertTrue(row["task_id"].startswith("local-submit:"))
        self.assertEqual(json.loads(row["usage_json"])["status"], "action_required")
        database.close()

    async def test_shared_paid_submit_gate_blocks_cli_bypass_before_provider_post(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-CLI-GATE','T-1','cli gate','PRODUCTION_RUNNING','{}')"
        )
        database.execute(
            "INSERT INTO shots(job_id,idx,state) "
            "VALUES('J-CLI-GATE',1,'PENDING')"
        )
        database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,usage_json) "
            "VALUES('remote-pending','J-CLI-GATE',1,'seedance','generate',?)",
            (json.dumps({"status": "pending"}),),
        )
        database.commit()
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=False, qa_required=False,
        )
        submit = AsyncMock(return_value=b"must-not-submit")

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            s3_shots.seedance, "text_to_video", new=submit
        ):
            with self.assertRaisesRegex(Exception, "remote-pending|核对") as raised:
                await s3_shots._generate_seedance_video(
                    prompt="equipment",
                    route=route,
                    request_kwargs={"duration": 4, "resolution": "480p"},
                    out_path=Path(tmp) / "shot.mp4",
                    key_hint="cli-bypass",
                    job_id="J-CLI-GATE",
                    shot_idx=1,
                    database=database,
                )

        submit.assert_not_awaited()
        self.assertEqual(
            getattr(raised.exception, "pause_state", None),
            "PROVIDER_ACTION_REQUIRED",
        )
        database.close()
    async def test_continuity_drift_is_merged_into_master_quality_gate(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=False, qa_required=True,
        )
        continuity_failed = video_qa.QADecision(False, ("identity_drift",))
        with tempfile.TemporaryDirectory() as tmp, \
            patch.dict(os.environ, {"SEEDANCE_MAX_AUTO_RETRIES": "0"}, clear=False), \
            patch.object(
                s3_shots.seedance, "text_to_video",
                new=AsyncMock(return_value=b"candidate"),
            ) as generate, \
            patch.object(
                s3_shots.tos, "upload",
                new=Mock(return_value="https://qa/current.mp4"),
            ), \
            patch.object(
                s3_shots.video_qa, "analyze",
                new=Mock(return_value=({
                    "visual_comparison": {"alignment": 1.0},
                    "transcript": "", "confidence": 1.0,
                }, video_qa.QADecision(True, ()))),
            ), \
            patch.object(
                s3_shots.video_qa, "analyze_continuity",
                new=Mock(return_value=({
                    "identity_checked": True, "identity_alignment": 0.4,
                }, continuity_failed)),
            ) as continuity:
            with self.assertRaises(s3_shots.SeedanceQualityError) as raised:
                await s3_shots._generate_seedance_video(
                    prompt="cinematic scene",
                    route=route,
                    request_kwargs={"duration": 4, "resolution": "480p"},
                    out_path=Path(tmp) / "shot.mp4",
                    key_hint="continuity-shot",
                    continuity_context={
                        "contract": {"scene": {"key": "yard"}},
                        "identity_image_urls": ["https://qa/lead.png"],
                        "scene_image_url": "https://qa/yard.png",
                        "previous_video_url": "https://qa/previous.mp4",
                    },
                )

        self.assertEqual(generate.await_count, 1)
        self.assertEqual(continuity.call_count, 1)
        self.assertEqual(raised.exception.reasons, ("identity_drift",))

    async def test_broll_semantic_failure_never_auto_retries_or_overwrites_master(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=False, qa_required=True,
        )
        failed = video_qa.QADecision(False, (
            "missing_industry_subject", "meaningless_hand_closeup",
        ))
        with tempfile.TemporaryDirectory() as tmp:
            master = Path(tmp) / "J-REVIEW" / "shots" / "02.mp4"
            master.parent.mkdir(parents=True)
            master.write_bytes(b"accepted-master")
            with patch.object(s3_shots, "ASSETS_DIR", Path(tmp)), patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(return_value=b"bad-candidate"),
                ) as generate, patch.object(
                    s3_shots.tos, "upload",
                    new=Mock(return_value="https://qa/candidate"),
                ), patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(return_value=({
                        "visual_comparison": {"alignment": 1.0},
                        "broll_semantics": {
                            "industry_subject_visible": False,
                            "environment_context_visible": True,
                            "meaningful_action_visible": True,
                            "dominant_subject": "hands",
                            "hand_closeup_ratio": 0.9,
                        },
                    }, failed)),
                ):
                with self.assertRaisesRegex(
                    s3_shots.SeedanceQualityError,
                    "manual review",
                ) as raised:
                    await s3_shots._generate_seedance_video(
                        prompt="equipment rental yard",
                        route=route,
                        request_kwargs={"duration": 6, "resolution": "480p"},
                        out_path=master,
                        key_hint="job-shot-02",
                        require_broll_semantics=True,
                    )

            self.assertEqual(generate.await_count, 1)
            self.assertEqual(master.read_bytes(), b"accepted-master")
            self.assertEqual(raised.exception.reasons, (
                "missing_industry_subject", "meaningless_hand_closeup",
            ))
            self.assertTrue(raised.exception.candidate_path.is_file())
            self.assertEqual(
                raised.exception.candidate_path.read_bytes(), b"bad-candidate"
            )
            database = sqlite3.connect(":memory:")
            database.row_factory = sqlite3.Row
            database.executescript(db.DDL)
            database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state) "
                "VALUES('J-REVIEW','T-1','review','PRODUCTION_RUNNING')"
            )
            database.execute(
                "INSERT INTO shots(job_id,idx,state) "
                "VALUES('J-REVIEW',2,'PENDING')"
            )
            with patch.object(s3_shots, "ASSETS_DIR", Path(tmp)):
                s3_shots._record_failed_generation(
                    database, "J-REVIEW", 2, raised.exception
                )
            recorded = database.execute(
                "SELECT state,video_qa FROM shots "
                "WHERE job_id='J-REVIEW' AND idx=2"
            ).fetchone()
            qa = json.loads(recorded["video_qa"])
            database.close()
            self.assertEqual(recorded["state"], "REVIEW_REQUIRED")
            self.assertTrue(os.path.samefile(
                qa["candidate_path"], raised.exception.candidate_path
            ))
            self.assertEqual(qa["candidate_url"], "https://qa/candidate")

    def test_quality_error_without_candidate_is_a_real_failure(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) "
            "VALUES('J-NO-CANDIDATE','T-1','no candidate','PRODUCTION_RUNNING')"
        )
        database.execute(
            "INSERT INTO shots(job_id,idx,state) "
            "VALUES('J-NO-CANDIDATE',1,'GENERATING')"
        )
        quality_error = s3_shots.SeedanceQualityError(
            "quality analysis failed before preview was saved",
            cost_cents=0,
            provider_calls=(),
            reasons=("qa_error",),
            candidate_path=Path("definitely-missing-candidate.mp4"),
        )
        self.assertFalse(hasattr(quality_error, "pause_state"))
        try:
            s3_shots._record_failed_generation(
                database,
                "J-NO-CANDIDATE",
                1,
                quality_error,
            )
            shot = database.execute(
                "SELECT state,video_qa,error FROM shots "
                "WHERE job_id='J-NO-CANDIDATE' AND idx=1"
            ).fetchone()
        finally:
            database.close()

        self.assertEqual(shot["state"], "FAILED")
        self.assertIsNone(shot["video_qa"])
        self.assertIn("SeedanceQualityError", shot["error"])

    def test_quality_error_rejects_url_only_and_outside_asset_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp) / "assets"
            controlled = assets / "J-CANDIDATE-RULE" / "shots" / "review"
            controlled.mkdir(parents=True)
            outside = Path(tmp) / "outside-candidate.mp4"
            outside.write_bytes(b"paid-but-uncontrolled")
            cases = (
                (None, "https://cdn.example/url-only.mp4"),
                (outside, "https://cdn.example/outside.mp4"),
            )
            for index, (candidate_path, candidate_url) in enumerate(cases, 1):
                with self.subTest(candidate_path=candidate_path):
                    database = sqlite3.connect(":memory:")
                    database.row_factory = sqlite3.Row
                    database.executescript(db.DDL)
                    database.execute(
                        "INSERT INTO jobs(job_id,topic_id,title,state) "
                        "VALUES('J-CANDIDATE-RULE','T-1','candidate rule',"
                        "'PRODUCTION_RUNNING')"
                    )
                    database.execute(
                        "INSERT INTO shots(job_id,idx,state) "
                        "VALUES('J-CANDIDATE-RULE',?,'GENERATING')",
                        (index,),
                    )
                    quality_error = s3_shots.SeedanceQualityError(
                        "soft QA without a controlled preview",
                        cost_cents=600,
                        provider_calls=(),
                        reasons=("identity_drift",),
                        candidate_path=candidate_path,
                        candidate_url=candidate_url,
                    )
                    with patch.object(s3_shots, "ASSETS_DIR", assets):
                        s3_shots._record_failed_generation(
                            database, "J-CANDIDATE-RULE", index, quality_error
                        )
                    shot = database.execute(
                        "SELECT state,video_qa FROM shots "
                        "WHERE job_id='J-CANDIDATE-RULE' AND idx=?",
                        (index,),
                    ).fetchone()
                    database.close()
                    self.assertEqual(shot["state"], "FAILED")
                    self.assertIsNone(shot["video_qa"])
                    self.assertFalse(hasattr(quality_error, "pause_state"))

    def test_quality_error_accepts_only_real_candidate_under_job_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp) / "assets"
            candidate = (
                assets / "J-CONTROLLED-CANDIDATE" / "shots" / "review"
                / "01-candidate.mp4"
            )
            candidate.parent.mkdir(parents=True)
            candidate.write_bytes(b"controlled-preview")
            database = sqlite3.connect(":memory:")
            database.row_factory = sqlite3.Row
            database.executescript(db.DDL)
            database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state) "
                "VALUES('J-CONTROLLED-CANDIDATE','T-1','controlled',"
                "'PRODUCTION_RUNNING')"
            )
            database.execute(
                "INSERT INTO shots(job_id,idx,state) "
                "VALUES('J-CONTROLLED-CANDIDATE',1,'GENERATING')"
            )
            quality_error = s3_shots.SeedanceQualityError(
                "soft QA with controlled preview",
                cost_cents=600,
                provider_calls=(),
                reasons=("identity_drift",),
                candidate_path=candidate,
                candidate_url="https://cdn.example/controlled.mp4",
            )
            with patch.object(s3_shots, "ASSETS_DIR", assets):
                s3_shots._record_failed_generation(
                    database, "J-CONTROLLED-CANDIDATE", 1, quality_error
                )
            shot = database.execute(
                "SELECT state,video_qa FROM shots "
                "WHERE job_id='J-CONTROLLED-CANDIDATE' AND idx=1"
            ).fetchone()
            database.close()

        self.assertEqual(shot["state"], "REVIEW_REQUIRED")
        self.assertEqual(
            Path(json.loads(shot["video_qa"])["candidate_path"]).resolve(),
            candidate.resolve(),
        )
        self.assertEqual(quality_error.pause_state, "PRODUCTION_REVIEW_REQUIRED")

    async def test_generation_qa_always_forbids_generated_readable_text(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=True, qa_required=True,
        )
        passed = video_qa.QADecision(True, ())
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(return_value=b"video"),
                ), \
                patch.object(
                    s3_shots.tos, "upload", new=Mock(return_value="https://qa/video")
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(return_value=({"visual_alignment": 0.95}, passed)),
                ) as analyze, \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ), \
                patch.object(
                    s3_shots.bigasr, "transcribe_detailed",
                    new=Mock(return_value=bigasr.Transcription("")),
                ):
            await s3_shots._generate_seedance_video(
                prompt="machine close-up",
                route=route,
                request_kwargs={"duration": 4, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-01",
            )

        self.assertTrue(analyze.call_args.kwargs["forbid_readable_text"])

    async def test_generation_can_skip_optional_asr_without_skipping_visual_qa(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=True, qa_required=True,
        )
        passed = video_qa.QADecision(True, ())
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(return_value=b"video"),
                ), \
                patch.object(
                    s3_shots.tos, "upload", new=Mock(return_value="https://qa/video")
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(return_value=({"visual_alignment": 0.95}, passed)),
                ) as analyze, \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ), \
                patch.object(
                    s3_shots.bigasr, "transcribe_detailed", new=Mock()
                ) as transcribe:
            result = await s3_shots._generate_seedance_video(
                prompt="presenter speaking",
                route=route,
                request_kwargs={"duration": 4, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-asr-off",
                expected_text="a line that remains subtitle guidance",
                asr_enabled=False,
            )

        self.assertEqual(analyze.call_count, 1)
        transcribe.assert_not_called()
        self.assertEqual(result.transcription.text, "")

    async def test_provider_usage_is_recorded_on_generation_result(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=True, qa_required=False,
        )

        async def generate_with_usage(**kwargs):
            kwargs["metadata_out"].update({
                "usage": {"completion_tokens": 260100}
            })
            return b"video"

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(side_effect=generate_with_usage),
                ), \
                patch.object(
                    s3_shots.tos, "upload", new=Mock(return_value="https://qa/video")
                ), \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ):
            result = await s3_shots._generate_seedance_video(
                prompt="presenter",
                route=route,
                request_kwargs={"duration": 12, "resolution": "720p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-01",
                expected_text="exact line",
            )

        self.assertEqual(result.cost_cents, 1821)
        self.assertEqual(result.provider_calls[0]["task_id"], None)
        self.assertEqual(
            result.provider_calls[0]["usage"]["completion_tokens"], 260100
        )

    async def test_retry_cost_and_provider_calls_are_cumulative(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=True, qa_required=True,
        )
        generated = iter([
            (b"first", "task-first", 100000),
            (b"retry", "task-retry", 200000),
        ])
        submitted_prompts = []

        async def generate_with_usage(**kwargs):
            submitted_prompts.append(kwargs["prompt"])
            video, task_id, tokens = next(generated)
            kwargs["metadata_out"].update({
                "task_id": task_id,
                "usage": {"completion_tokens": tokens},
            })
            return video

        failed = video_qa.QADecision(False, ("unexpected_readable_text",))
        passed = video_qa.QADecision(True, ())
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(side_effect=generate_with_usage),
                ), \
                patch.object(
                    s3_shots.tos, "upload",
                    new=Mock(side_effect=["https://qa/first", "https://qa/retry"]),
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(side_effect=[
                        ({"visual_alignment": 0.95,
                          "has_readable_text": True}, failed),
                        ({"visual_alignment": 0.95}, passed),
                    ]),
                ), \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ), \
                patch.object(
                    s3_shots.bigasr, "transcribe_detailed",
                    new=Mock(return_value=bigasr.Transcription("")),
                ):
            result = await s3_shots._generate_seedance_video(
                prompt="machine close-up",
                route=route,
                request_kwargs={"duration": 4, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-01",
            )

        self.assertEqual(result.cost_cents, 2100)
        self.assertEqual(
            [call["task_id"] for call in result.provider_calls],
            ["task-first", "task-retry"],
        )
        self.assertTrue(submitted_prompts[0].startswith("machine close-up"))
        self.assertIn("上一次画面出现了不应存在的可读文字", submitted_prompts[1])
        self.assertIn("纯色无字", submitted_prompts[1])
        self.assertIn("task-first", result.qa_note)
        self.assertIn("task-retry", result.qa_note)

    async def test_retry_soft_visual_mismatch_preserves_paid_candidate_for_review(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=False, qa_required=True,
        )
        generated = iter([
            (b"hard-failure", "task-hard", 100000),
            (b"soft-candidate", "task-soft", 200000),
        ])

        async def generate_with_usage(**kwargs):
            video, task_id, tokens = next(generated)
            kwargs["metadata_out"].update({
                "task_id": task_id,
                "usage": {"completion_tokens": tokens},
            })
            return video

        hard = video_qa.QADecision(False, ("unexpected_readable_text",))
        soft = video_qa.QADecision(False, ("visual_mismatch",))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            s3_shots.seedance, "text_to_video",
            new=AsyncMock(side_effect=generate_with_usage),
        ) as generate, patch.object(
            s3_shots.tos, "upload",
            new=Mock(side_effect=["https://qa/hard", "https://qa/soft"]),
        ), patch.object(
            s3_shots.video_qa, "analyze",
            new=Mock(side_effect=[
                ({"has_readable_text": True}, hard),
                ({"visual_alignment": 0.2}, soft),
            ]),
        ):
            with self.assertRaises(s3_shots.SeedanceQualityError) as raised:
                await s3_shots._generate_seedance_video(
                    prompt="equipment yard",
                    route=route,
                    request_kwargs={"duration": 4, "resolution": "480p"},
                    out_path=Path(tmp) / "shot.mp4",
                    key_hint="job-shot-soft-retry",
                )

            error = raised.exception
            self.assertEqual(generate.await_count, 2)
            self.assertEqual(error.pause_state, "PRODUCTION_REVIEW_REQUIRED")
            self.assertEqual(error.reasons, ("visual_mismatch",))
            self.assertEqual(error.cost_cents, 2100)
            self.assertEqual(
                [call["task_id"] for call in error.provider_calls],
                ["task-hard", "task-soft"],
            )
            self.assertEqual(error.candidate_url, "https://qa/soft")
            self.assertTrue(error.candidate_path.is_file())
            self.assertEqual(error.candidate_path.read_bytes(), b"soft-candidate")

    async def test_retry_account_error_keeps_first_paid_call_for_idempotent_billing(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=False, qa_required=True,
        )

        generation_count = 0

        async def first_then_account_error(**kwargs):
            nonlocal generation_count
            generation_count += 1
            if generation_count == 1:
                kwargs["metadata_out"].update({
                    "task_id": "task-first-paid",
                    "usage": {"completion_tokens": 100000},
                })
                return b"first-paid"
            raise seedance.SeedanceAccountUnavailableError("account overdue")

        generate = AsyncMock(side_effect=first_then_account_error)
        hard = video_qa.QADecision(False, ("unexpected_readable_text",))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            s3_shots.seedance, "text_to_video", new=generate,
        ), patch.object(
            s3_shots.tos, "upload", return_value="https://qa/first-paid",
        ), patch.object(
            s3_shots.video_qa, "analyze",
            return_value=({"has_readable_text": True}, hard),
        ):
            with self.assertRaises(
                seedance.SeedanceAccountUnavailableError
            ) as raised:
                await s3_shots._generate_seedance_video(
                    prompt="equipment yard",
                    route=route,
                    request_kwargs={"duration": 4, "resolution": "480p"},
                    out_path=Path(tmp) / "shot.mp4",
                    key_hint="job-shot-account-retry",
                )

        error = raised.exception
        self.assertEqual(error.pause_state, "PROVIDER_ACTION_REQUIRED")
        self.assertEqual(error.cost_cents, 700)
        self.assertEqual(
            [call["task_id"] for call in error.provider_calls],
            ["task-first-paid"],
        )
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) "
            "VALUES('J-ACCOUNT-RETRY','T-1','account retry','PRODUCTION_RUNNING')"
        )
        database.execute(
            "INSERT INTO shots(job_id,idx,state) "
            "VALUES('J-ACCOUNT-RETRY',1,'GENERATING')"
        )
        try:
            s3_shots._record_failed_generation(
                database, "J-ACCOUNT-RETRY", 1, error
            )
            s3_shots._record_failed_generation(
                database, "J-ACCOUNT-RETRY", 1, error
            )
            shot = database.execute(
                "SELECT cost_cents FROM shots WHERE job_id='J-ACCOUNT-RETRY'"
            ).fetchone()
            job = database.execute(
                "SELECT cost_cents FROM jobs WHERE job_id='J-ACCOUNT-RETRY'"
            ).fetchone()
            calls = database.execute(
                "SELECT task_id,cost_cents FROM provider_calls"
            ).fetchall()
        finally:
            database.close()
        self.assertEqual(shot["cost_cents"], 700)
        self.assertEqual(job["cost_cents"], 700)
        self.assertEqual(
            [(row["task_id"], row["cost_cents"]) for row in calls],
            [("task-first-paid", 700)],
        )

    async def test_visual_mismatch_requires_manual_review_without_retry(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=False, qa_required=True,
        )

        async def generate_with_usage(**kwargs):
            kwargs["metadata_out"].update({
                "task_id": "task-visual",
                "usage": {"completion_tokens": 58045},
            })
            return b"video"

        failed = video_qa.QADecision(False, ("visual_mismatch",))
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(side_effect=generate_with_usage),
                ) as generate, \
                patch.object(
                    s3_shots.tos, "upload", new=Mock(return_value="https://qa/video")
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(return_value=(
                        {"visual_alignment": 0.2}, failed
                    )),
                ):
            with self.assertRaisesRegex(
                s3_shots.SeedanceQualityError,
                "manual review",
            ) as raised:
                await s3_shots._generate_seedance_video(
                    prompt="modern office entrance",
                    route=route,
                    request_kwargs={"duration": 6, "resolution": "480p"},
                    out_path=Path(tmp) / "shot.mp4",
                    key_hint="job-shot-02",
                )
            self.assertEqual(generate.await_count, 1)
            self.assertEqual(raised.exception.cost_cents, 406)
            self.assertIsNotNone(raised.exception.candidate_path)
            self.assertTrue(raised.exception.candidate_path.is_file())
            self.assertEqual(
                raised.exception.candidate_path.read_bytes(), b"video"
            )
            self.assertEqual(raised.exception.candidate_url, "https://qa/video")

    async def test_retry_budget_keeps_transcript_mismatch_for_manual_review(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=True, qa_required=True,
        )

        async def generate_with_usage(**kwargs):
            kwargs["metadata_out"].update({
                "task_id": "task-first",
                "usage": {"completion_tokens": 58045},
            })
            return b"video"

        visual_pass = video_qa.QADecision(True, ())
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {
                    "SEEDANCE_AUTO_RETRY_BUDGET_CENTS": "300",
                }), \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(side_effect=generate_with_usage),
                ) as generate, \
                patch.object(
                    s3_shots.tos, "upload", new=Mock(return_value="https://qa/video")
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(return_value=(
                        {"visual_alignment": 0.95}, visual_pass
                    )),
                ), \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ), \
                patch.object(
                    s3_shots.bigasr, "transcribe_detailed",
                    new=Mock(return_value=bigasr.Transcription("wrong line")),
                ):
            result = await s3_shots._generate_seedance_video(
                prompt="presenter",
                route=route,
                request_kwargs={"duration": 6, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-02",
                expected_text="exact line",
            )

        self.assertEqual(generate.await_count, 1)
        self.assertEqual(result.cost_cents, 406)
        self.assertEqual(result.transcription.text, "wrong line")
        qa_note = json.loads(result.qa_note)
        self.assertEqual(qa_note["status"], "manual_review_required")
        self.assertEqual(qa_note["reasons"], ["transcript_mismatch"])
        self.assertIn("retry budget exceeded", qa_note["auto_retry_skipped"])

    async def test_qa_service_error_keeps_paid_master_for_manual_review(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=False, qa_required=True,
        )

        async def generate_with_usage(**kwargs):
            kwargs["metadata_out"].update({
                "task_id": "task-qa-unavailable",
                "usage": {"completion_tokens": 58045},
            })
            return b"video"

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(side_effect=generate_with_usage),
                ) as generate, patch.object(
                    s3_shots.tos, "upload", new=Mock(return_value="https://qa/video")
                ), patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(side_effect=RuntimeError("qa endpoint unavailable")),
                ):
            result = await s3_shots._generate_seedance_video(
                prompt="presenter in office",
                route=route,
                request_kwargs={"duration": 6, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-qa-error",
            )

        self.assertEqual(generate.await_count, 1)
        self.assertEqual(result.video, b"video")
        self.assertIn("manual_review_required", result.qa_note)
        self.assertIn("qa_error", result.qa_note)

    async def test_job_budget_blocks_paid_call_before_provider_submission(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=False, qa_required=False,
        )
        budget_db = Mock()
        budget_db.execute.return_value.fetchone.return_value = {
            "cost_cents": 1700,
            "storyboard": json.dumps({
                "production_settings": {"job_budget_cents": 1800}
            }),
        }

        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {
                    "SEEDANCE_JOB_BUDGET_CENTS": "1800",
                }), \
                patch.object(s3_shots, "conn", return_value=budget_db), \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(return_value=b"video"),
                ) as generate, patch.object(
                    s3_shots.tos, "upload", return_value="https://video"
                ):
            with self.assertRaisesRegex(
                s3_shots.SeedanceBudgetError,
                "exceeded before paid call",
            ):
                await s3_shots._generate_seedance_video(
                    prompt="presenter",
                    route=route,
                    request_kwargs={"duration": 4, "resolution": "480p"},
                    out_path=Path(tmp) / "shot.mp4",
                    key_hint="budget-shot",
                    job_id="J-BUDGET",
                )

        generate.assert_not_awaited()

    def test_production_route_has_no_paid_local_edit_policy(self):
        self.assertFalse(hasattr(s3_shots, "_should_try_local_dialogue_repair"))
        self.assertFalse(hasattr(s3_shots, "_build_dialogue_repair_prompt"))

    async def test_failed_native_dialogue_retries_full_generation(self):
        self.assertTrue(hasattr(s3_shots, "_generate_seedance_video"))
        route = video_routing.VideoRoute(
            model="seedance-fast", tier="fast",
            generate_audio=True, qa_required=True,
        )
        passed = video_qa.QADecision(True, ())

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(side_effect=[b"wrong", b"correct"]),
                ) as generate, \
                patch.object(
                    s3_shots.tos, "upload",
                    new=Mock(side_effect=["https://qa/first", "https://qa/final"]),
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(return_value=(
                        {"visual_alignment": 0.95}, passed
                    )),
                ), \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ), \
                patch.object(
                    s3_shots.bigasr, "transcribe_detailed",
                    new=Mock(side_effect=[
                        bigasr.Transcription("exact lime", 700, 3100),
                        bigasr.Transcription("exact line", 650, 3050),
                    ]),
                ):
            result = await s3_shots._generate_seedance_video(
                prompt="machine close-up",
                route=route,
                request_kwargs={"duration": 4, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-01",
                expected_text="exact line",
            )

        self.assertEqual(generate.await_count, 2)
        self.assertTrue(generate.await_args_list[0].kwargs["generate_audio"])
        self.assertTrue(generate.await_args_list[1].kwargs["generate_audio"])
        self.assertEqual(generate.await_args_list[1].kwargs["duration"], 4)
        self.assertNotIn("ratio", generate.await_args_list[1].kwargs)
        self.assertNotIn("output_format", generate.await_args_list[1].kwargs)
        self.assertNotIn("task_intent", generate.await_args_list[1].kwargs)
        self.assertNotIn("reference_videos", generate.await_args_list[1].kwargs)
        retry_prompt = generate.await_args_list[1].kwargs["prompt"]
        self.assertTrue(retry_prompt.startswith("machine close-up"))
        self.assertIn("画面文字通道关闭", retry_prompt)
        self.assertTrue(result.native_audio)
        self.assertEqual(result.video, b"correct")
        self.assertEqual(result.video_path.suffix, ".mp4")
        self.assertIn("transcript_mismatch", result.qa_note)

    async def test_passing_native_audio_qa_keeps_first_generation(self):
        self.assertTrue(hasattr(s3_shots, "_generate_seedance_video"))
        route = video_routing.VideoRoute(
            model="seedance-fast", tier="fast",
            generate_audio=True, qa_required=True,
        )
        passed = video_qa.QADecision(True, ())

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(return_value=b"native"),
                ) as generate, \
                patch.object(
                    s3_shots.tos, "upload", new=Mock(return_value="https://qa/video")
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(return_value=(
                        {"transcript": "", "visual_alignment": 0.95}, passed
                    )),
                ), \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ), \
                patch.object(
                    s3_shots.bigasr, "transcribe_detailed",
                    new=Mock(return_value=bigasr.Transcription("")),
                ):
            result = await s3_shots._generate_seedance_video(
                prompt="machine close-up",
                route=route,
                request_kwargs={"duration": 4, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-01",
            )

        self.assertEqual(generate.await_count, 1)
        self.assertTrue(result.native_audio)
        self.assertEqual(result.video_url, "https://qa/video")

    async def test_hard_visual_failure_retries_same_seedance25_model(self):
        self.assertTrue(hasattr(s3_shots, "_generate_seedance_video"))
        self.assertIn(
            "fallback_model", video_routing.VideoRoute.__dataclass_fields__
        )
        route = video_routing.VideoRoute(
            model="seedance-fast", tier="fast",
            generate_audio=True, qa_required=True,
            fallback_model="seedance-full",
        )
        failed = video_qa.QADecision(False, ("unexpected_person",))
        passed = video_qa.QADecision(True, ())

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(side_effect=[b"wrong", b"full"]),
                ) as generate, \
                patch.object(
                    s3_shots.tos, "upload",
                    new=Mock(side_effect=["https://qa/first", "https://qa/final"]),
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(side_effect=[
                        ({"visual_alignment": 0.95,
                          "has_visible_person": True}, failed),
                        ({"visual_alignment": 0.95}, passed),
                    ]),
                ), \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ), \
                patch.object(
                    s3_shots.bigasr, "transcribe_detailed",
                    new=Mock(return_value=bigasr.Transcription("")),
                ):
            result = await s3_shots._generate_seedance_video(
                prompt="machine gauge, no people",
                route=route,
                request_kwargs={"duration": 4, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-01",
                forbid_people=True,
            )

        self.assertEqual(
            generate.await_args_list[1].kwargs["model"], "seedance-fast"
        )
        self.assertEqual(generate.await_args_list[1].kwargs["duration"], 4)
        self.assertNotIn("task_intent", generate.await_args_list[1].kwargs)
        self.assertEqual(result.model, "seedance-fast")
        self.assertTrue(result.native_audio)

    async def test_bigasr_transcript_overrides_visual_model_transcript(self):
        route = video_routing.VideoRoute(
            model="seedance25", tier="seedance25",
            generate_audio=True, qa_required=True,
        )
        visual_pass = video_qa.QADecision(True, ())
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(return_value=b"video"),
                ), \
                patch.object(
                    s3_shots.tos, "upload", new=Mock(return_value="https://qa/video")
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(return_value=(
                        {"transcript": "hallucinated", "visual_alignment": 0.95},
                        visual_pass,
                    )),
                ), \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ), \
                patch.object(
                    s3_shots.bigasr, "transcribe_detailed",
                    new=Mock(return_value=bigasr.Transcription("exact", 500, 2800)),
                ):
            result = await s3_shots._generate_seedance_video(
                prompt="presenter",
                route=route,
                request_kwargs={"duration": 4, "resolution": "480p"},
                out_path=Path(tmp) / "shot.mp4",
                key_hint="job-shot-01",
                expected_text="exact",
            )

        self.assertTrue(result.native_audio)
        self.assertIn('"transcript": "exact"', result.qa_note)
        self.assertNotIn("hallucinated", result.qa_note)

    async def test_failed_retry_stops_after_two_full_generations(self):
        route = video_routing.VideoRoute(
            model="seedance-fast", tier="fast",
            generate_audio=True, qa_required=True,
        )
        visual_pass = video_qa.QADecision(True, ())
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s3_shots.seedance, "text_to_video",
                    new=AsyncMock(side_effect=[b"wrong", b"still-wrong"]),
                ) as generate, \
                patch.object(
                    s3_shots.tos, "upload",
                    new=Mock(side_effect=["https://qa/first", "https://qa/retry"]),
                ), \
                patch.object(
                    s3_shots.video_qa, "analyze",
                    new=Mock(return_value=({"visual_alignment": 0.95}, visual_pass)),
                ), \
                patch.object(
                    s3_shots.ffmpeg, "extract_audio_wav", new=AsyncMock()
                ), \
                patch.object(
                    s3_shots.bigasr, "transcribe_detailed",
                    new=Mock(side_effect=[
                        bigasr.Transcription("exact lime", 600, 2600),
                        bigasr.Transcription("exact pine", 600, 2600),
                    ]),
                ):
            with self.assertRaisesRegex(
                RuntimeError, "failed after retry"
            ) as raised:
                await s3_shots._generate_seedance_video(
                    prompt="presenter",
                    route=route,
                    request_kwargs={"duration": 4, "resolution": "480p"},
                    out_path=Path(tmp) / "shot.mp4",
                    key_hint="job-shot-01",
                    expected_text="exact line",
                )

        self.assertEqual(generate.await_count, 2)
        self.assertEqual(generate.await_args_list[1].kwargs["duration"], 4)
        self.assertNotIn("task_intent", generate.await_args_list[1].kwargs)
        self.assertEqual(raised.exception.cost_cents, 544)
        self.assertEqual(len(raised.exception.provider_calls), 2)


class LegacyRuntimeRemovalTests(unittest.TestCase):
    def test_tts_and_digital_human_runtime_modules_are_deleted(self):
        removed = [
            "scripts/factory/steps/s4_voice.py",
            "scripts/factory/clients/doubao_tts.py",
            "scripts/factory/clients/seedtts_v2.py",
            "scripts/factory/clients/seedtts_clone.py",
            "scripts/factory/clients/omnihuman.py",
            "scripts/factory/banned_words.py",
            "scripts/svg_diagrams.py",
        ]

        self.assertEqual(
            [path for path in removed if (PROJECT_ROOT / path).exists()],
            [],
        )

    def test_compose_reads_only_seedance_video_with_native_audio(self):
        source = inspect.getsource(s6_compose)

        self.assertFalse(hasattr(s6_compose, "_audio_strategy"))
        self.assertNotIn("audio_path", source)
        self.assertNotIn("provider", source)

    def test_smoke_check_contains_seedance_and_no_legacy_service(self):
        source = (PROJECT_ROOT / "scripts/smoke_check.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("ARK_VIDEO", source)
        self.assertNotIn("SeedTTS", source)
        self.assertNotIn("OmniHuman", source)

    def test_prompt_loader_has_no_voice_catalog_api(self):
        self.assertFalse(hasattr(prompts, "load_voice_catalog"))
        self.assertFalse(hasattr(prompts, "voice_catalog_table"))

    def test_requirements_drop_legacy_visual_service_sdk(self):
        requirements = (PROJECT_ROOT / "scripts/requirements.txt").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("volcengine", requirements.lower())


class Seedance25StateMachineTests(unittest.TestCase):
    def test_batch_query_excludes_all_terminal_and_human_pause_states(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        states = [
            "PENDING", "REJECTED", "PRODUCTION_REVIEW_REQUIRED",
            "PROVIDER_ACTION_REQUIRED", "FAILED", "BLOCKED", "PUBLISHED",
        ]
        for index, state in enumerate(states):
            database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state,priority) "
                "VALUES(?,?,?,?,1)",
                (f"J-BATCH-{index}", f"T-{index}", state, state),
            )
        database.commit()
        processed = []

        async def capture(job_id, semaphore):
            processed.append(job_id)

        with patch.object(orchestrator, "conn", return_value=database), \
                patch.object(orchestrator, "_import_topics", return_value=0), \
                patch.object(orchestrator, "_process_job", new=capture):
            asyncio.run(orchestrator.run_batch("ignored.csv"))

        self.assertEqual(processed, ["J-BATCH-0"])
        self.assertIs(orchestrator.BATCH_EXCLUDED_STATES, models.TERMINAL_STATES)
        database.close()
    def test_business_pause_states_are_terminal_without_being_failures(self):
        self.assertEqual(
            models.State.PRODUCTION_REVIEW_REQUIRED.value,
            "PRODUCTION_REVIEW_REQUIRED",
        )
        self.assertEqual(
            models.State.PROVIDER_ACTION_REQUIRED.value,
            "PROVIDER_ACTION_REQUIRED",
        )
        self.assertIn("PRODUCTION_REVIEW_REQUIRED", models.TERMINAL_STATES)
        self.assertIn("PROVIDER_ACTION_REQUIRED", models.TERMINAL_STATES)
        self.assertNotEqual(
            models.State.PRODUCTION_REVIEW_REQUIRED, models.State.FAILED
        )
        self.assertNotEqual(
            models.State.PROVIDER_ACTION_REQUIRED, models.State.BLOCKED
        )

    def test_anchor_review_pause_is_not_recorded_as_generation_failure(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-PAUSE','T-PAUSE','锚点审核暂停','PRODUCTION_RUNNING','{}')"
        )
        database.commit()
        try:
            with (
                patch.object(orchestrator, "conn", return_value=database),
                patch.object(
                    orchestrator, "_run_step", new=AsyncMock(
                        side_effect=s3_shots.ContinuityAnchorReviewPendingError(
                            "人物与场景锚点等待审核"
                        )
                    ),
                ),
            ):
                asyncio.run(
                    orchestrator._process_job(
                        "J-PAUSE", asyncio.Semaphore(1)
                    )
                )
            row = database.execute(
                "SELECT state,error FROM jobs WHERE job_id='J-PAUSE'"
            ).fetchone()
            self.assertEqual(row["state"], "ANCHOR_REVIEW_REQUIRED")
            self.assertIsNone(row["error"])
        finally:
            database.close()

    def test_paid_quality_guard_stops_step_level_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "candidate.mp4"
            candidate.write_bytes(b"candidate")
            quality_error = s3_shots.SeedanceQualityError(
                "manual review",
                cost_cents=406,
                provider_calls=(),
                reasons=("visual_mismatch",),
                candidate_path=candidate,
                candidate_url="https://assets/candidate.mp4",
            )
            aggregate = s3_shots._aggregate_shot_failures([
                (2, quality_error),
            ])

        self.assertFalse(orchestrator._should_retry_step(aggregate))
        self.assertEqual(
            aggregate.pause_state, "PRODUCTION_REVIEW_REQUIRED"
        )

    def test_account_overdue_failure_stops_step_level_retry(self):
        aggregate = s3_shots._aggregate_shot_failures([
            (3, seedance.SeedanceAccountUnavailableError("账号欠费")),
        ])

        self.assertFalse(orchestrator._should_retry_step(aggregate))
        self.assertEqual(
            aggregate.pause_state, "PROVIDER_ACTION_REQUIRED"
        )

    def test_technical_failure_prevents_soft_review_pause(self):
        quality_error = s3_shots.SeedanceQualityError(
            "manual review",
            cost_cents=406,
            provider_calls=(),
            reasons=("visual_mismatch",),
            candidate_path=Path("candidate.mp4"),
            candidate_url="https://assets/candidate.mp4",
        )

        aggregate = s3_shots._aggregate_shot_failures([
            (1, quality_error),
            (2, RuntimeError("provider timeout")),
        ])

        self.assertFalse(hasattr(aggregate, "pause_state"))

    def test_provider_action_has_priority_in_mixed_failures(self):
        aggregate = s3_shots._aggregate_shot_failures([
            (1, RuntimeError("provider timeout")),
            (2, seedance.SeedanceAccountUnavailableError("AccountOverdueError")),
        ])

        self.assertEqual(aggregate.pause_state, "PROVIDER_ACTION_REQUIRED")

    def test_soft_quality_job_pauses_without_incrementing_step_retry(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,retries) "
            "VALUES('J-QUALITY-PAUSE','T-Q','quality review',"
            "'PRODUCTION_RUNNING','{}',2)"
        )
        database.commit()
        try:
            temp = tempfile.TemporaryDirectory()
            candidate = Path(temp.name) / "candidate.mp4"
            candidate.write_bytes(b"candidate")
            error = s3_shots._aggregate_shot_failures([(
                1,
                s3_shots.SeedanceQualityError(
                    "visual_mismatch candidate requires review",
                    cost_cents=406,
                    provider_calls=(),
                    reasons=("visual_mismatch",),
                    candidate_path=candidate,
                    candidate_url="https://assets/candidate.mp4",
                ),
            )])
            with (
                patch.object(orchestrator, "conn", return_value=database),
                patch.object(
                    orchestrator, "_run_step", new=AsyncMock(side_effect=error)
                ),
            ):
                asyncio.run(orchestrator._process_job(
                    "J-QUALITY-PAUSE", asyncio.Semaphore(1)
                ))
            row = database.execute(
                "SELECT state,retries,error FROM jobs "
                "WHERE job_id='J-QUALITY-PAUSE'"
            ).fetchone()
            self.assertEqual(row["state"], "PRODUCTION_REVIEW_REQUIRED")
            self.assertEqual(row["retries"], 2)
            self.assertIn("visual_mismatch", row["error"])
        finally:
            temp.cleanup()
            database.close()

    def test_account_overdue_job_pauses_without_incrementing_step_retry(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,retries) "
            "VALUES('J-ACCOUNT-PAUSE','T-A','account action',"
            "'PRODUCTION_RUNNING','{}',1)"
        )
        database.commit()
        error = s3_shots._aggregate_shot_failures([(
            1,
            seedance.SeedanceAccountUnavailableError(
                "AccountOverdueError: recharge the Ark account"
            ),
        )])
        try:
            with (
                patch.object(orchestrator, "conn", return_value=database),
                patch.object(
                    orchestrator, "_run_step", new=AsyncMock(side_effect=error)
                ),
            ):
                asyncio.run(orchestrator._process_job(
                    "J-ACCOUNT-PAUSE", asyncio.Semaphore(1)
                ))
            row = database.execute(
                "SELECT state,retries,error FROM jobs "
                "WHERE job_id='J-ACCOUNT-PAUSE'"
            ).fetchone()
            self.assertEqual(row["state"], "PROVIDER_ACTION_REQUIRED")
            self.assertEqual(row["retries"], 1)
            self.assertIn("AccountOverdueError", row["error"])
        finally:
            database.close()

    def test_transient_step_error_remains_retryable(self):
        self.assertTrue(
            orchestrator._should_retry_step(RuntimeError("temporary timeout"))
        )

    def test_successful_steps_clear_stale_retry_errors(self):
        shot_commit_source = inspect.getsource(s3_shots._commit_generated_shot)
        orchestrator_source = inspect.getsource(orchestrator._process_job)

        self.assertIn("error=NULL", shot_commit_source)
        self.assertIn("error=NULL", orchestrator_source)

    def test_state_machine_skips_voice_generation_step(self):
        step_names = [step for _, step, _ in models.TRANSITIONS]

        self.assertNotIn("s4_voice", step_names)
        self.assertNotIn("s2_character", step_names)
        self.assertNotIn("s4_voice", orchestrator.STEP_MODULES)
        self.assertNotIn("s2_character", orchestrator.STEP_MODULES)

    def test_character_step_flows_directly_to_seedance_shots(self):
        transition = next(
            item for item in models.TRANSITIONS
            if item[0] is models.State.CHARACTER_OK
        )

        self.assertEqual(transition[1], "s3_shots")
        self.assertIs(transition[2], models.State.SHOTS_OK)

    def test_storyboard_flows_directly_to_seedance_shots(self):
        transition = next(
            item for item in models.TRANSITIONS
            if item[0] is models.State.STORYBOARDED
        )

        self.assertEqual(transition[1], "s3_shots")
        self.assertIs(transition[2], models.State.SHOTS_OK)


class HookOverlaySizingTests(unittest.TestCase):
    def test_long_hook_font_size_fits_single_line(self):
        hook = "设备租赁？狗都不干！狗不干，我干。"
        size = ffmpeg._hook_font_size(hook, 1080)

        self.assertLess(size, int(1080 * 0.072))
        self.assertLessEqual(size * len(hook), int(1080 * 0.90))

    def test_short_hook_keeps_large_display_size(self):
        self.assertEqual(
            ffmpeg._hook_font_size("我干", 1080),
            int(1080 * 0.072),
        )


    def test_thumbnail_long_hook_fits_inside_safe_width(self):
        hook = "设备租赁？狗都不干！狗不干，我干。"
        size = ffmpeg._thumbnail_hook_font_size(hook, 720)

        self.assertLessEqual(size * len(hook), 720 - 80)

    def test_thumbnail_short_hook_keeps_large_display_size(self):
        self.assertEqual(
            ffmpeg._thumbnail_hook_font_size("我干", 720),
            int(720 * 0.13),
        )


class ThumbnailEncodingTests(unittest.IsolatedAsyncioTestCase):
    async def test_continuity_cover_uses_first_stable_frame_without_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            out = base / "thumbnail.jpg"

            async def render(_args):
                out.write_bytes(b"jpeg")

            with patch.object(
                ffmpeg, "_run", new=AsyncMock(side_effect=render)
            ) as run:
                await ffmpeg.generate_continuity_cover(
                    base / "final.mp4", out
                )

        args = run.await_args.args[0]
        self.assertEqual(float(args[args.index("-ss") + 1]), 0.04)
        self.assertNotIn("-vf", args)
        self.assertEqual(args[args.index("-frames:v") + 1], "1")

    async def test_thumbnail_uses_full_range_single_thread_mjpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            out = base / "thumbnail.jpg"

            async def render(_args):
                out.write_bytes(b"jpeg")

            with (
                patch.object(ffmpeg, "probe_duration", new=AsyncMock(return_value=8.0)),
                patch.object(ffmpeg, "_run", new=AsyncMock(side_effect=render)) as run,
            ):
                await ffmpeg.generate_thumbnail(
                    base / "final.mp4",
                    "验收封面",
                    out,
                )

        args = run.await_args.args[0]
        self.assertEqual(args[args.index("-pix_fmt") + 1], "yuvj420p")
        self.assertEqual(args[args.index("-threads") + 1], "1")

    async def test_thumbnail_seek_is_clamped_inside_short_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            out = base / "thumbnail.jpg"

            async def render(_args):
                out.write_bytes(b"jpeg")

            with (
                patch.object(ffmpeg, "probe_duration", new=AsyncMock(return_value=4.04)),
                patch.object(ffmpeg, "_run", new=AsyncMock(side_effect=render)) as run,
            ):
                await ffmpeg.generate_thumbnail(
                    base / "final.mp4", "短片封面", out, ts=4.5
                )

        args = run.await_args.args[0]
        self.assertLess(float(args[args.index("-ss") + 1]), 4.04)

    async def test_thumbnail_fails_when_ffmpeg_writes_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with (
                patch.object(ffmpeg, "probe_duration", new=AsyncMock(return_value=4.04)),
                patch.object(ffmpeg, "_run", new=AsyncMock()),
            ):
                with self.assertRaisesRegex(ffmpeg.FFmpegError, "no output"):
                    await ffmpeg.generate_thumbnail(
                        base / "final.mp4", "短片封面", base / "thumbnail.jpg"
                    )


class SceneVisualAnchorTests(unittest.IsolatedAsyncioTestCase):
    def test_auto_review_mode_approves_ready_generated_anchors(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO character_identity_anchors("
            "job_id,identity_key,character_id,display_name,source_type,state,approval_state) "
            "VALUES('J-AUTO','script:lead','lead','Lead','seedream_identity_image',"
            "'READY','PENDING_REVIEW')"
        )
        database.execute(
            "INSERT INTO scene_visual_anchors("
            "job_id,anchor_key,description,master_indexes,state,approval_state) "
            "VALUES('J-AUTO','yard','Yard','[1]','READY','PENDING_REVIEW')"
        )
        database.commit()
        try:
            approved = s3_shots._apply_anchor_review_mode(
                database, "J-AUTO", "auto"
            )
            states = {
                row[0] for row in database.execute(
                    "SELECT approval_state FROM character_identity_anchors "
                    "WHERE job_id='J-AUTO' UNION ALL SELECT approval_state "
                    "FROM scene_visual_anchors WHERE job_id='J-AUTO'"
                ).fetchall()
            }
        finally:
            database.close()

        self.assertEqual(approved, 2)
        self.assertEqual(states, {"APPROVED"})
    async def test_scene_anchor_can_use_seedance_clip_and_extracted_frame(self):
        storyboard = {
            "production_settings": {
                "scene_anchor_generator": "seedance_2_5",
                "budget_cents": 5000,
            },
            "masters": [
                {"idx": 1, "scene": "设备检修车间", "scene_anchor_key": "bay"},
                {"idx": 2, "scene": "设备检修车间", "scene_anchor_key": "bay"},
            ],
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(tmp)
            database_path = base / "factory.db"
            video_path = base / "scene.mp4"
            video_path.write_bytes(b"video")
            preview_path = base / "scene-frame.jpg"
            preview_path.write_bytes(b"jpg")
            result = s3_shots.SeedanceGenerationResult(
                video=b"video", video_path=video_path,
                video_url="https://assets/scene.mp4", qa_note="{}",
                native_audio=False, model="doubao-seedance-2-5-260628",
                audio_path=None, cost_cents=408,
                provider_calls=({
                    "task_id": "scene-s25", "intent": "scene_anchor",
                    "usage": {}, "cost_cents": 408,
                },),
            )
            with (
                patch.dict(os.environ, {"FACTORY_DB_PATH": str(database_path)}),
                patch.object(s3_shots, "ASSETS_DIR", base / "assets"),
                patch.object(
                    s3_shots, "_generate_seedance_video",
                    new=AsyncMock(return_value=result),
                ) as video_generate,
                patch.object(
                    s3_shots.seedance, "text_to_image", new=AsyncMock()
                ) as image_generate,
                patch.object(
                    s3_shots.ffmpeg, "extract_frames",
                    new=AsyncMock(return_value=[preview_path]),
                ),
                patch.object(
                    s3_shots.tos, "upload",
                    return_value="https://assets/scene-frame.jpg",
                ),
            ):
                database = db.conn()
                database.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
                    "VALUES(?,?,?,?,?)",
                    ("J-SCENE-S25", "T", "scene", "PRODUCTION_RUNNING",
                     json.dumps(storyboard)),
                )
                database.commit()
                anchors = await s3_shots._ensure_scene_visual_anchors(
                    "J-SCENE-S25", storyboard
                )
                row = database.execute(
                    "SELECT generator,model,video_path,image_path,cost_cents "
                    "FROM scene_visual_anchors WHERE job_id='J-SCENE-S25'"
                ).fetchone()

        self.assertEqual(image_generate.await_count, 0)
        self.assertEqual(video_generate.await_count, 1)
        self.assertEqual(anchors, {"bay": "https://assets/scene-frame.jpg"})
        self.assertEqual(row["generator"], "seedance_2_5")
        self.assertEqual(row["model"], "doubao-seedance-2-5-260628")
        self.assertEqual(row["video_path"], str(video_path))
        self.assertEqual(row["image_path"], str(preview_path))
        self.assertEqual(row["cost_cents"], 408)

    def test_continuity_qa_context_prefers_canonical_refs_and_same_scene_video(self):
        speaker = {"id": "lead", "generation_mode": "ai_generated"}
        cast = [{"id": "client", "generation_mode": "ai_generated"}]
        identity_anchors = {
            "script:lead": s3_shots.DialogueAnchor(
                image_reference="https://assets/lead.png",
                anchor_mode="static_image",
            ),
            "script:client": s3_shots.DialogueAnchor(
                image_reference="https://assets/client.png",
                anchor_mode="static_image",
            ),
        }
        previous_scene = s3_shots.DialogueAnchor(
            "https://cdn/scene-previous.mp4", 8, chain_depth=1,
        )
        previous_speaker = s3_shots.DialogueAnchor(
            "https://cdn/speaker-previous.mp4", 4, chain_depth=1,
        )
        contract = {"version": 1, "scene": {"key": "yard-a"}}

        context = s3_shots._continuity_qa_context(
            {"continuity_contract": contract},
            speaker,
            cast_characters=cast,
            identity_anchors=identity_anchors,
            scene_reference="https://assets/yard.png",
            scene_video_anchor=previous_scene,
            speaker_anchor=previous_speaker,
        )

        self.assertEqual(context["contract"], contract)
        self.assertEqual(
            context["identity_image_urls"],
            ["https://assets/lead.png", "https://assets/client.png"],
        )
        self.assertEqual(context["scene_image_url"], "https://assets/yard.png")
        self.assertEqual(
            context["previous_video_url"],
            "https://cdn/scene-previous.mp4",
        )

    def test_incremental_video_anchor_resets_at_configured_chain_depth(self):
        shallow = s3_shots.DialogueAnchor(
            "https://cdn/shot-2.mp4", 4, chain_depth=2,
        )
        drift_prone = s3_shots.DialogueAnchor(
            "https://cdn/shot-3.mp4", 4, chain_depth=3,
        )

        with patch.dict(os.environ, {"SEEDANCE_CONTINUITY_RESET_INTERVAL": "3"}):
            self.assertIs(s3_shots._incremental_anchor(shallow), shallow)
            self.assertIsNone(s3_shots._incremental_anchor(drift_prone))

    def test_generated_continuity_anchors_must_be_approved_before_master_calls(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-GATE','T-GATE','审核门','PRODUCTION_RUNNING','{}')"
        )
        database.execute(
            "INSERT INTO character_identity_anchors("
            "job_id,identity_key,character_id,display_name,source_type,state,approval_state) "
            "VALUES('J-GATE','script:lead','lead','主角','seedream_identity_image',"
            "'READY','PENDING_REVIEW')"
        )
        database.execute(
            "INSERT INTO character_identity_anchors("
            "job_id,identity_key,character_id,display_name,source_type,state,approval_state) "
            "VALUES('J-GATE','volc:asset://boss','boss','老大','volc_asset',"
            "'READY','PENDING_REVIEW')"
        )
        database.execute(
            "INSERT INTO scene_visual_anchors("
            "job_id,anchor_key,description,master_indexes,state,approval_state) "
            "VALUES('J-GATE','yard','停车场','[1,2]','READY','APPROVED')"
        )
        database.commit()
        try:
            with self.assertRaisesRegex(
                s3_shots.ContinuityAnchorReviewPendingError, "主角"
            ):
                s3_shots._assert_continuity_anchors_approved(
                    database, "J-GATE"
                )
            database.execute(
                "UPDATE character_identity_anchors SET approval_state='APPROVED' "
                "WHERE identity_key='script:lead'"
            )
            database.commit()
            s3_shots._assert_continuity_anchors_approved(database, "J-GATE")
        finally:
            database.close()

    def test_continuity_contract_materializes_canonical_scene_and_character_state(self):
        characters = [
            {
                "id": "boss", "name": "老大", "look": "白色安全帽、荧光绿反光背心",
                "generation_mode": "fixed_asset", "identity_type": "virtual_person",
                "seedance_asset_uri": "asset://asset-boss",
            },
            {
                "id": "client", "name": "客户", "look": "灰色V领毛衣",
                "generation_mode": "ai_generated",
            },
        ]
        storyboard = {
            "characters": characters,
            "masters": [
                {
                    "idx": 1, "speaker": "boss", "scene_anchor_key": "yard-a",
                    "scene": "场景：户外设备停车场；出镜人物：老大、客户；人物动作：面对面交谈",
                    "action": "老大拿着手机，客户站在右侧", "props": ["手机"],
                    "time_of_day": "白天", "lighting": "左前方自然光",
                },
                {
                    "idx": 2, "speaker": "client", "scene_anchor_key": "yard-a",
                    "scene": "场景：户外设备停车场；出镜人物：客户、老大；人物动作：客户回应",
                    "action": "客户继续站在右侧，老大保持左侧",
                    "time_of_day": "白天", "lighting": "左前方自然光",
                },
            ],
        }

        changed = scene_assets.apply_continuity_metadata(storyboard, characters)

        self.assertTrue(changed)
        first = storyboard["masters"][0]
        contract = first["continuity_contract"]
        self.assertEqual(contract["version"], 1)
        self.assertEqual(contract["scene"]["key"], "yard-a")
        self.assertEqual(contract["scene"]["time_of_day"], "白天")
        self.assertEqual(contract["scene"]["lighting"], "左前方自然光")
        self.assertEqual(contract["props"], ["手机"])
        self.assertEqual(contract["camera"]["axis"], "preserve-180-degree-axis")
        by_name = {item["display_name"]: item for item in contract["characters"]}
        self.assertEqual(by_name["老大"]["wardrobe"], "白色安全帽、荧光绿反光背心")
        self.assertEqual(by_name["老大"]["screen_position"], "screen-left")
        self.assertEqual(by_name["客户"]["screen_position"], "screen-right")
        self.assertEqual(
            storyboard["masters"][1]["continuity_contract"]["scene"]["key"],
            "yard-a",
        )

    def test_fixed_asset_background_is_not_treated_as_wardrobe_contract(self):
        characters = [{
            "id": "staff",
            "name": "老大",
            "script_character_name": "女工作人员",
            "generation_mode": "fixed_asset",
            "identity_type": "virtual_person",
            "seedance_asset_uri": "asset://staff",
            "look": "古装戏楼名伶，身穿汉服",
            "background_description": "古代戏楼中的角色背景",
        }]
        storyboard = {
            "characters": characters,
            "masters": [{
                "idx": 1,
                "speaker": "staff",
                "scene": "场景：户外工程设备停车场；出镜人物：老大；人物动作：检查设备",
                "action": "老大佩戴安全帽和反光背心检查设备",
            }],
        }

        scene_assets.apply_continuity_metadata(storyboard, characters)

        wardrobe = storyboard["masters"][0]["continuity_contract"]["characters"][0]["wardrobe"]
        self.assertEqual(
            wardrobe,
            "服装与职业装备服从本剧本场景；同一场景内保持一致",
        )
        self.assertNotIn("汉服", wardrobe)

    def test_ai_character_placeholder_is_not_treated_as_wardrobe_contract(self):
        characters = [{
            "id": "client",
            "name": "客户",
            "script_character_name": "客户",
            "generation_mode": "ai_generated",
            "look": "待在视频制作设置中绑定角色资产",
        }]
        storyboard = {
            "characters": characters,
            "masters": [{
                "idx": 1,
                "speaker": "client",
                "scene": "场景：设备停车场；出镜人物：客户；人物动作：客户检查设备",
                "action": "客户穿灰色工作服检查设备",
            }],
        }

        scene_assets.apply_continuity_metadata(storyboard, characters)

        wardrobe = storyboard["masters"][0]["continuity_contract"]["characters"][0]["wardrobe"]
        self.assertEqual(
            wardrobe,
            "服装与职业装备服从本剧本场景；同一场景内保持一致",
        )
        self.assertNotIn("绑定角色资产", wardrobe)

    def test_source_scene_metadata_locks_repeated_space_and_two_identities(self):
        characters = [
            {
                "id": "女员工-近景", "name": "老大",
                "generation_mode": "fixed_asset",
                "identity_type": "virtual_person",
                "seedance_asset_uri": "asset://asset-boss",
            },
            {
                "id": "女员工-中景", "name": "老大",
                "generation_mode": "fixed_asset",
                "identity_type": "virtual_person",
                "seedance_asset_uri": "asset://asset-boss",
            },
            {
                "id": "黑人客户", "name": "黑人客户",
                "generation_mode": "ai_generated",
            },
        ]
        storyboard = {
            "characters": characters,
            "masters": [
                {
                    "idx": 1, "speaker": "女员工-近景",
                    "scene": "场景：户外工程设备停放场地，背景有黄色工程机械；"
                             "出镜人物：女员工-近景、黑人客户；人物动作：二人站立",
                    "action": "女员工-近景与黑人客户面向镜头",
                },
                {
                    "idx": 2, "speaker": "女员工-中景",
                    "scene": "场景：户外工程设备停放场地，地面为水泥路面；"
                             "出镜人物：女员工-中景、黑人客户；人物动作：二人点赞",
                    "action": "女员工-中景与黑人客户保持原站位",
                },
                {
                    "idx": 3, "speaker": "黑人客户",
                    "scene": "场景：户外工程设备停放场地，背景有黄色吊车；"
                             "出镜人物：黑人客户、女员工-中景；人物动作：客户点赞",
                    "action": "黑人客户说话，女员工-中景在旁边",
                },
            ],
        }

        changed = scene_assets.apply_continuity_metadata(
            storyboard, characters
        )

        self.assertTrue(changed)
        keys = {master["scene_anchor_key"] for master in storyboard["masters"]}
        self.assertEqual(len(keys), 1)
        plan = scene_assets.recurring_scene_anchors(storyboard)
        self.assertEqual(plan[0]["master_indexes"], [1, 2, 3])
        positions = storyboard["masters"][0]["blocking_anchor"]["positions"]
        self.assertEqual(
            [item["screen_position"] for item in positions],
            ["screen-left", "screen-right"],
        )
        self.assertEqual(len(positions), 2)

    def test_dialogue_request_reuses_previous_master_from_same_scene(self):
        previous_scene = s3_shots.DialogueAnchor(
            "https://cdn/scene-previous.mp4", 8
        )
        previous_speaker = s3_shots.DialogueAnchor(
            "https://cdn/speaker-previous.mp4", 4
        )

        request = s3_shots._dialogue_request_kwargs(
            {"duration_sec": 4},
            previous_speaker,
            character={"id": "lead", "generation_mode": "ai_generated"},
            scene_video_anchor=previous_scene,
        )

        self.assertEqual(
            request["reference_videos"],
            [
                "https://cdn/scene-previous.mp4",
                "https://cdn/speaker-previous.mp4",
            ],
        )
        self.assertEqual(request["reference_video_duration_sec"], 12)

    def test_dialogue_prompt_contains_locked_scene_blocking_contract(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 2,
                "duration_sec": 4,
                "voiceover": "继续介绍项目。",
                "scene": "户外设备停车场",
                "action": "两人继续对话",
                "scene_anchor_key": "scene-yard",
                "blocking_anchor": {
                    "axis": "preserve-180-degree-axis",
                    "positions": [
                        {"display_name": "老大", "screen_position": "screen-left"},
                        {"display_name": "客户", "screen_position": "screen-right"},
                    ],
                },
            },
            {"id": "boss", "name": "老大", "generation_mode": "ai_generated"},
            anchored=True,
            scene_anchored=True,
            previous_scene_anchored=True,
        )

        self.assertIn("scene-yard", prompt)
        self.assertIn("screen-left", prompt)
        self.assertIn("screen-right", prompt)
        self.assertIn("180", prompt)
        self.assertIn("人物身份仍以定妆照或授权角色资产为准", prompt)

    def test_dialogue_prompt_serializes_structured_continuity_contract(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 2, "duration_sec": 4, "voiceover": "继续介绍。",
                "scene": "户外停车场", "action": "老大拿着手机与客户交流",
                "scene_anchor_key": "yard-a",
                "blocking_anchor": {"axis": "preserve-180-degree-axis", "positions": []},
                "continuity_contract": {
                    "version": 1,
                    "scene": {
                        "key": "yard-a", "description": "户外停车场",
                        "time_of_day": "白天", "lighting": "左前方自然光",
                    },
                    "characters": [{
                        "display_name": "老大", "wardrobe": "白色安全帽、荧光绿反光背心",
                        "screen_position": "screen-left",
                    }],
                    "props": ["手机"],
                    "camera": {"axis": "preserve-180-degree-axis"},
                },
            },
            {"id": "boss", "name": "老大", "generation_mode": "ai_generated"},
            anchored=True, scene_anchored=True,
        )

        self.assertIn("结构化连续性契约", prompt)
        self.assertIn("白色安全帽、荧光绿反光背心", prompt)
        self.assertIn("screen-left", prompt)
        self.assertIn("手机", prompt)
        self.assertIn("左前方自然光", prompt)

    def test_dialogue_request_combines_scene_and_character_image_references(self):
        request = s3_shots._dialogue_request_kwargs(
            {"duration_sec": 4},
            None,
            character={"id": "lead", "generation_mode": "ai_generated"},
            identity_anchors={
                "script:lead": s3_shots.DialogueAnchor(
                    image_reference="https://assets/lead.png",
                    anchor_mode="static_image",
                )
            },
            scene_reference="https://assets/repair-bay.png",
        )

        self.assertEqual(
            request["references"],
            ["https://assets/repair-bay.png", "https://assets/lead.png"],
        )

    async def test_repeated_scene_anchor_is_generated_once_and_resumed(self):
        storyboard = {
            "production_settings": {"budget_cents": 1000},
            "masters": [
                {
                    "idx": 1, "scene": "设备检修车间",
                    "scene_anchor_key": "repair-bay-a",
                },
                {
                    "idx": 2, "scene": "设备检修车间",
                    "scene_anchor_key": "repair-bay-a",
                },
            ],
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(tmp)
            database_path = base / "factory.db"
            with (
                patch.dict(os.environ, {"FACTORY_DB_PATH": str(database_path)}),
                patch.object(s3_shots, "ASSETS_DIR", base / "assets"),
                patch.object(
                    s3_shots.seedance, "text_to_image",
                    new=AsyncMock(return_value=b"png"),
                ) as generate,
                patch.object(
                    s3_shots.tos, "upload",
                    return_value="https://assets/repair-bay.png",
                ),
            ):
                database = db.conn()
                database.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
                    "VALUES(?,?,?,?,?)",
                    ("J-SCENE", "T-SCENE", "scene", "STORYBOARDED",
                     json.dumps(storyboard)),
                )
                database.commit()
                first = await s3_shots._ensure_scene_visual_anchors(
                    "J-SCENE", storyboard
                )
                second = await s3_shots._ensure_scene_visual_anchors(
                    "J-SCENE", storyboard
                )
                row = database.execute(
                    "SELECT state,cost_cents FROM scene_visual_anchors "
                    "WHERE job_id='J-SCENE'"
                ).fetchone()

        self.assertEqual(first, {"repair-bay-a": "https://assets/repair-bay.png"})
        self.assertEqual(second, first)
        self.assertEqual(generate.await_count, 1)
        self.assertEqual((row["state"], row["cost_cents"]), ("READY", 29))


class ComposeNativeAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_candidate_asr_extends_terminal_edit_to_speech_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp) / "assets"
            base = assets / "J-ASR-BACKFILL"
            master = base / "shots" / "03.mp4"
            audio = master.with_suffix(".wav")
            master.parent.mkdir(parents=True)
            master.write_bytes(b"video")
            audio.write_bytes(b"audio")
            storyboard = {
                "production_mode": "native_master_local_director",
                "production_settings": {"asr_enabled": True},
                "masters": [{"idx": 3, "type": "dialogue"}],
                "shots": [{
                    "idx": 4,
                    "video_master_idx": 3,
                    "audio_master_idx": 3,
                    "video_start": 0,
                    "audio_start": 0,
                    "duration": 16.5,
                    "subtitle": "林夏的末句必须完整说完。",
                }],
            }
            database = sqlite3.connect(":memory:")
            database.row_factory = sqlite3.Row
            database.executescript(db.DDL)
            database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
                "VALUES('J-ASR-BACKFILL','T-1','asr','SHOTS_OK',?)",
                (json.dumps(storyboard, ensure_ascii=False),),
            )
            database.execute(
                "INSERT INTO shots(job_id,idx,state,video_path) "
                "VALUES('J-ASR-BACKFILL',3,'OK',?)",
                (str(master),),
            )
            database.commit()
            rows = database.execute(
                "SELECT idx,video_path FROM shots WHERE job_id='J-ASR-BACKFILL'"
            ).fetchall()
            transcription = bigasr.Transcription(
                "林夏的末句必须完整说完。",
                2060,
                16740,
                (bigasr.TimedUtterance(
                    "林夏的末句必须完整说完。", 11060, 16740
                ),),
            )

            self.assertTrue(hasattr(
                s6_compose, "_refresh_director_speech_timeline"
            ))
            with patch.object(s6_compose, "ASSETS_DIR", assets), \
                    patch.object(
                        s6_compose.bigasr, "transcribe_detailed",
                        return_value=transcription,
                    ) as transcribe, patch.object(
                        s6_compose.ffmpeg, "probe_duration",
                        new=AsyncMock(return_value=17.056),
                    ):
                changed = await s6_compose._refresh_director_speech_timeline(
                    "J-ASR-BACKFILL", storyboard, rows, database
                )

            stored = json.loads(database.execute(
                "SELECT storyboard FROM jobs WHERE job_id='J-ASR-BACKFILL'"
            ).fetchone()["storyboard"])
            database.close()

        self.assertTrue(changed)
        transcribe.assert_called_once_with(audio)
        self.assertEqual(storyboard["shots"][0]["duration"], 16.89)
        self.assertEqual(storyboard["masters"][0]["duration"], 16.89)
        self.assertIsNone(storyboard["shots"][0]["subtitle"])
        self.assertEqual(
            storyboard["shots"][0]["subtitle_cues"][-1]["end"], 16.74
        )
        self.assertEqual(stored, storyboard)

    async def test_asr_backfill_extends_only_terminal_edit_for_shared_master(self):
        storyboard = {
            "production_mode": "native_master_local_director",
            "production_settings": {"asr_enabled": True},
            "masters": [{"idx": 2, "type": "dialogue"}],
            "shots": [
                {
                    "idx": 2, "video_master_idx": 2,
                    "audio_master_idx": 2, "audio_start": 0,
                    "video_start": 0, "duration": 8.5,
                    "subtitle": "第一句",
                },
                {
                    "idx": 3, "video_master_idx": 3,
                    "audio_master_idx": 2, "audio_start": 8.5,
                    "video_start": 0, "duration": 11.0,
                    "subtitle": "第二句",
                },
            ],
        }
        transcription = bigasr.Transcription(
            "第一句。第二句。", 3060, 19880,
            (
                bigasr.TimedUtterance("第一句。", 3060, 9110),
                bigasr.TimedUtterance("第二句。", 12840, 19880),
            ),
        )

        self.assertTrue(hasattr(
            s6_compose, "_extend_terminal_edit_for_speech"
        ))
        changed = s6_compose._extend_terminal_edit_for_speech(
            storyboard, 2, transcription, available_duration=20.064
        )

        self.assertTrue(changed)
        self.assertEqual(storyboard["shots"][0]["duration"], 8.5)
        self.assertEqual(storyboard["shots"][1]["duration"], 11.53)

    def test_internal_director_cut_snaps_to_asr_utterance_boundary(self):
        storyboard = {
            "production_mode": "native_master_local_director",
            "shots": [
                {
                    "idx": 2, "audio_master_idx": 2,
                    "audio_start": 0, "duration": 8.5,
                },
                {
                    "idx": 3, "audio_master_idx": 2,
                    "audio_start": 8.5, "duration": 11.53,
                },
            ],
        }
        transcription = bigasr.Transcription(
            "故障这么复杂。这设备可咋整啊？别先猜设备。",
            3060,
            19880,
            (
                bigasr.TimedUtterance("故障这么复杂。这设备可咋整啊？", 3060, 9110),
                bigasr.TimedUtterance("别先猜设备。", 9550, 11590),
                bigasr.TimedUtterance("十分钟后给你方案。", 12840, 19880),
            ),
        )

        self.assertTrue(hasattr(
            s6_compose, "_align_internal_edits_to_speech_pauses"
        ))
        changed = s6_compose._align_internal_edits_to_speech_pauses(
            storyboard, 2, transcription
        )

        self.assertTrue(changed)
        self.assertEqual(storyboard["shots"][0]["duration"], 9.11)
        self.assertEqual(storyboard["shots"][1]["audio_start"], 9.11)
        self.assertEqual(storyboard["shots"][1]["duration"], 10.92)

    def test_video_qa_rejects_explicit_lip_sync_failure(self):
        decision = video_qa.evaluate(
            {
                "transcript": "林夏立即启动应急调度",
                "confidence": 1.0,
                "lip_sync": False,
            },
            expect_speech=True,
            expected_text="林夏立即启动应急调度",
        )

        self.assertFalse(decision.passed)
        self.assertIn("lip_sync_mismatch", decision.reasons)

    def test_cinematic_dialogue_prompt_keeps_speaker_mouth_visible(self):
        prompt = s3_shots._build_native_dialogue_prompt(
            {
                "idx": 3,
                "duration_sec": 16.5,
                "render_mode": "cinematic_character",
                "speaker": "林夏",
                "speakers": ["林夏"],
                "voiceover": "立即启动应急调度",
                "scene": "暴雨夜抢修现场",
                "action": "林夏指挥抢修",
                "camera": "环绕推进",
            },
            {"id": "林夏", "generation_mode": "ai_generated"},
        )

        self.assertIn("说台词期间", prompt)
        self.assertIn("嘴部必须持续清晰可见", prompt)
        self.assertIn("其他同场人物全程闭嘴", prompt)

    async def test_successful_compose_finalizes_pending_candidate_audit_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp) / "assets"
            base = assets / "J-FINALIZE-CANDIDATE"
            master = base / "shots" / "01.mp4"
            candidate = base / "shots" / "review" / "01-candidate.mp4"
            backup = base / "shots" / "review" / "01-prior.mp4"
            candidate.parent.mkdir(parents=True)
            master.write_bytes(b"accepted")
            candidate.write_bytes(b"stale-candidate")
            backup.write_bytes(b"prior-master")
            database = sqlite3.connect(":memory:")
            database.row_factory = sqlite3.Row
            database.executescript(db.DDL)
            database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
                "VALUES('J-FINALIZE-CANDIDATE','T-1','finalize','SHOTS_OK','{}')"
            )
            database.execute(
                "INSERT INTO shots(job_id,idx,state,video_path,video_qa) "
                "VALUES('J-FINALIZE-CANDIDATE',1,'OK',?,?)",
                (str(master), json.dumps({
                    "status": "candidate_accepted_pending",
                    "human_decision": "accept_candidate",
                    "candidate_path": str(candidate),
                    "prior_backup_path": str(backup),
                    "reasons": ["identity_drift"],
                })),
            )
            database.commit()

            async def write_final(_concat, _bgm, final, **_kwargs):
                final.write_bytes(b"composed")

            with patch.object(s6_compose, "ASSETS_DIR", assets), \
                    patch.object(s6_compose, "conn", return_value=database), \
                    patch.object(
                        s6_compose, "_prep_shot",
                        new=AsyncMock(return_value=master),
                    ), patch.object(
                        s6_compose.ffmpeg, "concat_shots", new=AsyncMock()
                    ), patch.object(
                        s6_compose.ffmpeg, "mix_bgm",
                        new=AsyncMock(side_effect=write_final),
                    ), patch.dict(os.environ, {
                        "APPEND_CTA": "false",
                        "GEN_THUMBNAIL": "false",
                        "GEN_MARKETING_THUMBNAIL": "false",
                    }, clear=False):
                await s6_compose.run("J-FINALIZE-CANDIDATE")
                await s6_compose.run("J-FINALIZE-CANDIDATE")

            qa = json.loads(database.execute(
                "SELECT video_qa FROM shots "
                "WHERE job_id='J-FINALIZE-CANDIDATE'"
            ).fetchone()["video_qa"])
            database.close()

        self.assertEqual(qa["status"], "candidate_accepted")
        self.assertNotIn("candidate_path", qa)
        self.assertNotIn("prior_backup_path", qa)
        self.assertFalse(candidate.exists())
        self.assertFalse(backup.exists())

    def test_asr_timings_replace_estimated_static_subtitles_in_director_shots(self):
        storyboard = {
            "production_mode": "native_master_local_director",
            "shots": [
                {
                    "idx": 1, "audio_master_idx": 2,
                    "audio_start": 0, "duration": 4,
                    "subtitle": "estimated opening label",
                },
                {
                    "idx": 2, "audio_master_idx": 2,
                    "audio_start": 4, "duration": 4,
                    "subtitle": "estimated closing label",
                },
                {
                    "idx": 3, "audio_master_idx": 9,
                    "audio_start": 0, "duration": 4,
                    "subtitle": "another master",
                },
            ],
        }
        transcription = bigasr.Transcription(
            "狗不干？我干。拼的不是腿，是信息和效率。",
            2370,
            6830,
            (
                bigasr.TimedUtterance("狗不干？我干。", 2370, 3650),
                bigasr.TimedUtterance(
                    "拼的不是腿，是信息和效率。", 4010, 6830
                ),
            ),
        )

        changed = s3_shots._apply_master_transcription(
            storyboard, 2, transcription
        )

        self.assertTrue(changed)
        self.assertIsNone(storyboard["shots"][0]["subtitle"])
        self.assertEqual(
            storyboard["shots"][0]["subtitle_cues"],
            [
                {"start": 2.37, "end": 3.223, "text": "狗不干？"},
                {"start": 3.223, "end": 3.65, "text": "我干"},
            ],
        )
        self.assertEqual(
            storyboard["shots"][1]["subtitle_cues"],
            [
                {"start": 0.01, "end": 1.292, "text": "拼的不是腿"},
                {"start": 1.292, "end": 2.83, "text": "是信息和效率"},
            ],
        )
        self.assertEqual(storyboard["shots"][2]["subtitle"], "another master")

    def test_asr_text_bounds_create_a_single_timed_cue_when_utterances_absent(self):
        storyboard = {
            "production_mode": "native_master_local_director",
            "shots": [{
                "idx": 1, "audio_master_idx": 1,
                "audio_start": 1, "duration": 5,
                "subtitle": "estimated",
            }],
        }

        s3_shots._apply_master_transcription(
            storyboard, 1,
            bigasr.Transcription("真实台词。", 1500, 4200),
        )

        self.assertEqual(
            storyboard["shots"][0]["subtitle_cues"],
            [{"start": 0.5, "end": 3.2, "text": "真实台词"}],
        )

    def test_broll_request_includes_every_declared_cast_identity_and_scene_anchor(self):
        lead = {"id": "林夏", "generation_mode": "ai_generated"}
        partner = {"id": "Malik", "generation_mode": "ai_generated"}
        anchors = {
            character_assets.character_identity_key(lead): s3_shots.DialogueAnchor(
                "https://cdn.example/linxia.mp4", 4
            ),
            character_assets.character_identity_key(partner): s3_shots.DialogueAnchor(
                "https://cdn.example/malik.mp4", 4
            ),
        }

        try:
            request = s3_shots._broll_request_kwargs(
                {"duration_sec": 8},
                cast_characters=[lead, partner],
                identity_anchors=anchors,
                scene_reference="https://cdn.example/dispatch.png",
            )
        except TypeError as exc:
            self.fail(f"B-roll continuity references are not supported: {exc}")

        self.assertEqual(
            request["reference_videos"],
            [
                "https://cdn.example/linxia.mp4",
                "https://cdn.example/malik.mp4",
            ],
        )
        self.assertEqual(
            request["references"], ["https://cdn.example/dispatch.png"]
        )

    def test_long_asr_utterance_is_split_into_phrase_level_cues(self):
        storyboard = {
            "production_mode": "native_master_local_director",
            "shots": [{
                "idx": 1, "audio_master_idx": 1,
                "audio_start": 0, "duration": 8,
                "subtitle": "estimated",
            }],
        }

        s3_shots._apply_master_transcription(
            storyboard, 1,
            bigasr.Transcription(
                "暴雨还在下，设备已经到场，先确认支腿区域，再开始升臂。",
                400, 7600,
            ),
        )

        cues = storyboard["shots"][0]["subtitle_cues"]
        self.assertGreaterEqual(len(cues), 4)
        self.assertTrue(all(len(cue["text"]) <= 14 for cue in cues))
        self.assertTrue(all(
            left["end"] <= right["start"]
            for left, right in zip(cues, cues[1:])
        ))
        self.assertEqual(cues[0]["start"], 0.4)
        self.assertEqual(cues[-1]["end"], 7.6)

    def test_director_segments_convert_legacy_static_subtitle_to_timed_phrases(self):
        storyboard = {
            "shots": [{
                "idx": 1,
                "video_master_idx": 1,
                "audio_master_idx": 1,
                "duration": 9,
                "subtitle": "先确认作业高度，再核对水平距离，最后选择合适设备。",
            }]
        }

        segment = s6_compose._director_segments(
            storyboard, [{"idx": 1, "video_path": "master.mp4"}]
        )[0]

        self.assertIsNone(segment["subtitle"])
        self.assertGreaterEqual(len(segment["subtitle_cues"]), 3)
        self.assertEqual(segment["subtitle_cues"][0]["start"], 0.15)
        self.assertEqual(segment["subtitle_cues"][-1]["end"], 8.85)

    def test_director_segments_preserve_timed_subtitle_cues(self):
        storyboard = {
            "shots": [{
                "idx": 1,
                "video_master_idx": 2,
                "audio_master_idx": 2,
                "duration": 6,
                "subtitle": "legacy always-on label",
                "subtitle_cues": [
                    {"start": 2.37, "end": 3.65, "text": "狗不干？我干。"},
                    {"start": 4.01, "end": 6.0, "text": "拼的不是腿，是信息和效率。"},
                ],
            }]
        }
        rows = [{"idx": 2, "video_path": "master-02.mp4"}]

        segments = s6_compose._director_segments(storyboard, rows)

        self.assertEqual(
            segments[0]["subtitle_cues"],
            storyboard["shots"][0]["subtitle_cues"],
        )

    def test_timed_subtitle_filters_are_enabled_only_inside_cue_windows(self):
        with tempfile.TemporaryDirectory() as td:
            filters = ffmpeg._timed_subtitle_filters(
                Path(td),
                "segment-02",
                [
                    {"start": 2.37, "end": 3.65, "text": "狗不干？我干。"},
                    {"start": 4.01, "end": 6.0, "text": "拼的不是腿，是信息和效率。"},
                ],
                width=1080,
            )

            self.assertEqual(len(filters), 2)
            self.assertIn("between(t,2.370,3.650)", filters[0])
            self.assertIn("between(t,4.010,6.000)", filters[1])
            self.assertEqual(
                (Path(td) / "segment-02_sub_01_line_01.txt").read_text(
                    encoding="utf-8"
                ),
                "狗不干？我干",
            )

    def test_subtitle_display_removes_terminal_full_stop_only(self):
        self.assertEqual(ffmpeg._subtitle_display_text("需求自动匹配。"), "需求自动匹配")
        self.assertEqual(ffmpeg._subtitle_display_text("狗都不干！"), "狗都不干！")
        self.assertEqual(ffmpeg._subtitle_display_text("设备租赁？"), "设备租赁？")

    def test_subtitle_text_wraps_inside_vertical_video_safe_width(self):
        text = "我们可以顺着这个思路，去深入挖掘数字背后的真实含义，助力设备租赁业务"

        wrapped = ffmpeg._wrap_subtitle_text(text, width=1080)

        lines = wrapped.splitlines()
        self.assertEqual("".join(lines), text)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(len(line) <= 17 for line in lines))

    def test_subtitle_wrapping_does_not_split_ascii_number_tokens(self):
        text = "看，来源中明确提到了‘2312’，这肯定不是偶然"

        lines = ffmpeg._wrap_subtitle_text(text, width=1080).splitlines()

        self.assertTrue(any("2312" in line for line in lines))
        self.assertFalse(any(line.endswith("‘") for line in lines))

    def test_timed_subtitle_files_use_wrapped_display_text(self):
        with tempfile.TemporaryDirectory() as td:
            filters = ffmpeg._timed_subtitle_filters(
                Path(td),
                "segment-long",
                [{"start": 0, "end": 5, "text": "大家都在猜测这个数字到底和设备租赁有啥关系，毫无头绪啊。"}],
                width=1080,
            )

            rendered = [
                (Path(td) / f"segment-long_sub_01_line_{line:02d}.txt").read_text(
                    encoding="utf-8"
                )
                for line in (1, 2)
            ]
            self.assertNotIn("。", "".join(rendered))
            self.assertTrue(all(len(line) <= 17 for line in rendered))
            self.assertEqual(len(filters), 2)
            self.assertIn("y=h*0.80-34", filters[0])
            self.assertIn("y=h*0.80+34", filters[1])
            self.assertTrue(all("between(t,0.000,5.000)" in item for item in filters))

    def test_native_mix_normalizes_ambience_without_lowering_narration(self):
        self.assertTrue(hasattr(ffmpeg, "_native_narration_filter"))

        filt = ffmpeg._native_narration_filter(
            ambient_lufs=-32.0, narration_db=0.0
        )

        self.assertIn("loudnorm=I=-32.0:TP=-9.0:LRA=7.0", filt)
        self.assertIn("normalize=0", filt)
        self.assertIn("alimiter=limit=0.95", filt)
        self.assertNotIn("volume=-18", filt)

    async def test_seedance_native_track_is_kept_without_external_mix(self):
        row = {
            "idx": 1,
            "video_path": "shot.mp4",
            "audio_path": "voice.mp3",
            "provider": "seedance",
        }
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    s6_compose.ffmpeg, "has_audio_stream",
                    new=AsyncMock(return_value=True),
                ), \
                patch.object(
                    s6_compose.ffmpeg, "mix_video_narration",
                    new=AsyncMock(),
                ) as mix, \
                patch.object(
                    s6_compose.ffmpeg, "probe_duration",
                    new=AsyncMock(return_value=4.0),
                ), \
                patch.object(
                    s6_compose.ffmpeg, "normalize_shot",
                    new=AsyncMock(),
                ):
            await s6_compose._prep_shot(
                row, Path(tmp), subtitle=None, hook=None
            )

        mix.assert_not_awaited()


class StandardDirectorPipelineTests(unittest.TestCase):
    @staticmethod
    def _storyboard() -> dict:
        return {
            "production_profile": "standard",
            "production_mode": "native_master_local_director",
            "characters": [{
                "id": "host",
                "name": "Host",
                "gender": "female",
                "look": "A fictional Chinese equipment-rental consultant",
            }],
            "hook": "Stop generating every shot",
            "bgm": {"mood": "restrained", "prompt": "light rhythm"},
            "masters": [
                {
                    "idx": 1,
                    "type": "dialogue",
                    "speaker": "host",
                    "render_mode": "presenter",
                    "duration": 6,
                    "scene": "equipment yard office",
                    "action": "speaks naturally to camera",
                    "voiceover": "The opening hook is delivered here.",
                },
                {
                    "idx": 2,
                    "type": "dialogue",
                    "speaker": "host",
                    "render_mode": "presenter",
                    "duration": 18,
                    "scene": "same equipment yard office",
                    "action": "continues the explanation",
                    "voiceover": "The complete body narration is delivered continuously here.",
                },
                {
                    "idx": 3,
                    "type": "broll",
                    "duration": 10,
                    "prompt": "Natural close-ups of rental equipment in operation",
                },
            ],
            "shots": [
                {
                    "idx": 1,
                    "video_master_idx": 1,
                    "audio_master_idx": 1,
                    "video_start": 0,
                    "audio_start": 0,
                    "duration": 6,
                    "subtitle": "Opening hook",
                },
                {
                    "idx": 2,
                    "video_master_idx": 2,
                    "audio_master_idx": 2,
                    "video_start": 0,
                    "audio_start": 0,
                    "duration": 6,
                    "subtitle": "Body point one",
                },
                {
                    "idx": 3,
                    "video_master_idx": 3,
                    "audio_master_idx": 2,
                    "video_start": 0,
                    "audio_start": 6,
                    "duration": 6,
                    "subtitle": "Body point two",
                },
                {
                    "idx": 4,
                    "video_master_idx": 2,
                    "audio_master_idx": 2,
                    "video_start": 12,
                    "audio_start": 12,
                    "duration": 6,
                    "subtitle": "Closing line",
                },
            ],
        }

    def test_standard_profile_accepts_three_masters_and_four_edit_shots(self):
        s1_storyboard._validate(self._storyboard())

    def test_standard_profile_accepts_more_than_three_paid_masters_within_duration(self):
        storyboard = self._storyboard()
        storyboard["masters"].append({
            "idx": 4,
            "type": "broll",
            "duration": 4,
            "prompt": "Another paid visual master",
        })

        s1_storyboard._validate(storyboard)

    def test_standard_profile_does_not_cap_total_paid_master_duration(self):
        storyboard = self._storyboard()
        storyboard["masters"].extend([
            {
                "idx": 4,
                "type": "broll",
                "duration": 29,
                "prompt": "A continuous equipment inspection sequence",
            },
            {
                "idx": 5,
                "type": "broll",
                "duration": 29,
                "prompt": "A continuous equipment delivery sequence",
            },
        ])

        self.assertGreater(
            sum(float(master["duration"]) for master in storyboard["masters"]),
            90,
        )
        s1_storyboard._validate(storyboard)

    def test_generation_units_are_masters_not_final_edit_shots(self):
        self.assertTrue(hasattr(s1_storyboard, "_generation_units"))
        units = s1_storyboard._generation_units(self._storyboard())

        self.assertEqual([unit["idx"] for unit in units], [1, 2, 3])

    def test_director_timeline_can_take_video_and_audio_from_different_masters(self):
        self.assertTrue(hasattr(s6_compose, "_director_segments"))
        rows = [
            {"idx": 1, "video_path": "master-1.mp4"},
            {"idx": 2, "video_path": "master-2.mp4"},
            {"idx": 3, "video_path": "master-3.mp4"},
        ]

        segments = s6_compose._director_segments(self._storyboard(), rows)

        cutaway = segments[2]
        self.assertEqual(cutaway["video_path"], "master-3.mp4")
        self.assertEqual(cutaway["native_audio"], "master-2.mp4")
        self.assertEqual(cutaway["audio_start"], 6)

    def test_director_segments_apply_fades_only_to_explicit_scene_change(self):
        storyboard = self._storyboard()
        storyboard["shots"][0].update({
            "continuity_relation": "opening",
            "transition_type": "none",
            "transition_duration_sec": 0,
        })
        storyboard["shots"][1].update({
            "continuity_relation": "scene_change",
            "transition_type": "dip_to_black",
            "transition_duration_sec": 0.12,
        })
        storyboard["shots"][2].update({
            "continuity_relation": "audio_bridge",
            "transition_type": "cut",
            "transition_duration_sec": 0,
        })
        rows = [
            {"idx": 1, "video_path": "master-1.mp4"},
            {"idx": 2, "video_path": "master-2.mp4"},
            {"idx": 3, "video_path": "master-3.mp4"},
        ]

        segments = s6_compose._director_segments(storyboard, rows)

        self.assertEqual(segments[0]["video_fade_out_sec"], 0.12)
        self.assertEqual(segments[1]["video_fade_in_sec"], 0.12)
        self.assertEqual(segments[1]["video_fade_out_sec"], 0)
        self.assertEqual(segments[2]["video_fade_in_sec"], 0)
        self.assertEqual(segments[2]["transition_type"], "cut")

    def test_director_cut_encodes_requested_visual_boundary_fades(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            ffmpeg, "_run", new=AsyncMock()
        ) as run:
            asyncio.run(ffmpeg.cut_director_segment(
                Path(tmp) / "video.mp4",
                Path(tmp) / "audio.mp4",
                Path(tmp) / "out.mp4",
                video_start=0,
                audio_start=0,
                duration=4,
                video_fade_in_sec=0.12,
                video_fade_out_sec=0.12,
            ))

        args = run.await_args.args[0]
        video_filter = args[args.index("-vf") + 1]
        self.assertIn("fade=t=in:st=0:d=0.120", video_filter)
        self.assertIn("fade=t=out:st=3.880:d=0.120", video_filter)

    def test_storyboard_prompt_defaults_to_standard_director_mode(self):
        prompt = prompts.storyboard_prompt(
            title="Equipment rental",
            track="B2B",
            duration=30,
        )

        self.assertIn('"production_profile": "standard"', prompt)
        self.assertIn('"production_mode": "native_master_local_director"', prompt)
        self.assertIn('"masters"', prompt)
        self.assertIn("video_master_idx", prompt)
        self.assertIn("audio_master_idx", prompt)
        self.assertNotIn(
            '"shots": [\n    {\n      "idx": 1,\n      "type": "dialogue"',
            prompt,
        )

    def test_ffmpeg_exposes_local_director_cut(self):
        self.assertTrue(hasattr(ffmpeg, "cut_director_segment"))

    def test_dialogue_audio_master_must_be_consumed_continuously(self):
        storyboard = self._storyboard()
        storyboard["shots"][2]["audio_start"] = 7

        with self.assertRaisesRegex(ValueError, "audio timeline.*continuous"):
            s1_storyboard._validate(storyboard)

    def test_standard_profile_default_job_budget_is_twenty_five_yuan(self):
        env_template = (PROJECT_ROOT / "templates" / "env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("SEEDANCE_JOB_BUDGET_CENTS=2500", env_template)
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(s3_shots._job_budget_cents({}), 2500)


if __name__ == "__main__":
    unittest.main()
