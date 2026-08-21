"""P0 production failure classification and provider circuit-breaker tests."""
from copy import deepcopy
from pathlib import Path
import asyncio
import importlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from urllib import error as urlerror, request
from unittest.mock import AsyncMock, Mock, patch

import httpx
import httpcore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
PROVIDER_FAILURE_FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "provider_failures"


def load_provider_failure(name: str) -> dict:
    """Load one sanitized provider response captured for the release gate."""
    with (PROVIDER_FAILURE_FIXTURES / f"{name}.json").open(
        encoding="utf-8"
    ) as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise AssertionError(f"provider failure fixture must be an object: {name}")
    return payload


from factory import (  # noqa: E402
    db, master_dependencies, master_risk, orchestrator, provider_capacity,
    provider_tasks, quality_canary, reference_attention, script_drafts,
    script_sources,
)
from factory.clients import (  # noqa: E402
    music, provider_downloads, seedance, seedream_45,
)
from factory.management_api import ManagementService  # noqa: E402
from factory.steps import s3_shots, s5_bgm, s6_compose  # noqa: E402
from factory.video_routing import VideoRoute  # noqa: E402
from management_server import ConsoleHandler  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

try:
    from factory import provider_health  # noqa: E402
except ImportError:
    provider_health = None


class FailureOutcomeContractTests(unittest.TestCase):
    def test_every_failure_has_stable_code_and_one_next_action(self):
        module_spec = importlib.util.find_spec("factory.error_registry")
        self.assertIsNotNone(
            module_spec,
            "central failure registry must exist before classifying failures",
        )
        registry = importlib.import_module("factory.error_registry")

        outcome = registry.describe_failure(
            RuntimeError("precision-sensitive shot blocked")
        )

        self.assertEqual(outcome.code, "prompt_auto_repair_required")
        self.assertEqual(outcome.category, "contract")
        self.assertEqual(outcome.stage, "preflight")
        self.assertEqual(outcome.retry_policy, "manual")
        self.assertEqual(outcome.cost_effect, "none")
        self.assertEqual(outcome.next_action.id, "recompile_production_spec")
        self.assertEqual(outcome.next_action.label, "重新编译生产规格")

    def test_registered_failures_use_fixed_safe_messages_and_unique_actions(self):
        registry = importlib.import_module("factory.error_registry")

        class SignaledFailure(RuntimeError):
            pass

        cases = [
            ("request_invalid", "", "recompile_production_spec", "none"),
            ("input_privacy_rejected", "", "replace_private_reference", "none"),
            ("output_policy_rejected", "", "revise_output_prompt", "none"),
            ("output_copyright_rejected", "", "replace_copyright_reference", "none"),
            ("account_unavailable", "PROVIDER_ACTION_REQUIRED", "view_provider_account_guide", "none"),
            ("transient_network", "", "retry_automatically", "none"),
            ("transient_network", "PROVIDER_ACTION_REQUIRED", "reconcile_provider_task", "unknown"),
        ]
        secret = "Bearer TOP-SECRET https://evil.example/x?token=LEAK C:\\Users\\private"
        for code, pause_state, action_id, cost_effect in cases:
            with self.subTest(code=code, pause_state=pause_state):
                error = SignaledFailure(secret)
                error.stable_code = code
                if pause_state:
                    error.pause_state = pause_state
                outcome = registry.describe_failure(error, stage="master_generation")
                self.assertEqual(outcome.code, code)
                self.assertEqual(outcome.stage, "master_generation")
                self.assertEqual(outcome.next_action.id, action_id)
                self.assertEqual(outcome.cost_effect, cost_effect)
                self.assertTrue(outcome.user_message)
                self.assertTrue(outcome.technical_summary)
                rendered = (
                    outcome.user_message + " " + outcome.technical_summary
                ).casefold()
                for forbidden in (
                    "top-secret", "token=", "evil.example", "c:\\users",
                ):
                    self.assertNotIn(forbidden, rendered)

    def test_unknown_failure_never_defaults_to_continue_generation(self):
        registry = importlib.import_module("factory.error_registry")

        outcome = registry.describe_failure(
            RuntimeError("https://provider.invalid?api_key=secret")
        )

        self.assertEqual(outcome.code, "internal_error")
        self.assertEqual(outcome.retry_policy, "manual")
        self.assertEqual(outcome.next_action.id, "preserve_and_diagnose")
        self.assertNotIn("secret", outcome.user_message.casefold())
        self.assertNotIn("provider.invalid", outcome.technical_summary.casefold())

    def test_orchestrator_persists_only_the_registered_safe_outcome(self):
        database = memory_database()
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) "
            "VALUES('J-OUTCOME','T-OUTCOME','安全错误落库','PENDING')"
        )
        database.commit()

        error = RuntimeError(
            "Bearer TOP-SECRET https://evil.example/x?token=LEAK"
        )
        error.stable_code = "account_unavailable"
        error.pause_state = "PROVIDER_ACTION_REQUIRED"
        with (
            patch.object(orchestrator, "conn", return_value=database),
            patch.object(
                orchestrator, "_run_step", new=AsyncMock(side_effect=error)
            ),
        ):
            asyncio.run(
                orchestrator._process_job("J-OUTCOME", asyncio.Semaphore(1))
            )

        row = database.execute(
            "SELECT state,error,failure_code FROM jobs WHERE job_id='J-OUTCOME'"
        ).fetchone()
        self.assertEqual(row["state"], "PROVIDER_ACTION_REQUIRED")
        self.assertEqual(row["failure_code"], "account_unavailable")
        self.assertEqual(
            row["error"],
            "模型账号当前不可用，请处理账号后再恢复任务",
        )
        events = [
            str(item[0]) for item in database.execute(
                "SELECT message FROM events WHERE job_id='J-OUTCOME'"
            ).fetchall()
        ]
        self.assertTrue(events)
        rendered = " ".join(events).casefold()
        self.assertNotIn("top-secret", rendered)
        self.assertNotIn("token=", rendered)
        self.assertNotIn("evil.example", rendered)
        database.close()

    def test_management_uses_registered_action_for_failed_jobs(self):
        database = memory_database()
        database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state,error,failure_code) "
            "VALUES(?,?,?,'FAILED',?,?)",
            [
                (
                    "J-PRIVACY", "T-PRIVACY", "隐私失败",
                    "输入素材未通过真人隐私校验，请更换已授权素材",
                    "input_privacy_rejected",
                ),
                (
                    "J-INTERNAL", "T-INTERNAL", "未知失败",
                    "生产过程出现未识别异常，已保留现场，请重新诊断",
                    "internal_error",
                ),
            ],
        )
        database.commit()
        with tempfile.TemporaryDirectory() as tmp:
            service = ManagementService(database, Path(tmp))
            jobs = {item["job_id"]: item for item in service.videos()["jobs"]}

        self.assertEqual(
            jobs["J-PRIVACY"]["next_action"]["id"],
            "replace_private_reference",
        )
        self.assertEqual(
            jobs["J-INTERNAL"]["next_action"]["id"],
            "preserve_and_diagnose",
        )
        self.assertFalse(jobs["J-PRIVACY"]["resumable"])
        self.assertFalse(jobs["J-INTERNAL"]["resumable"])
        database.close()


class Seedance25AcceptanceFixtureTests(unittest.TestCase):
    FIXTURE = (
        PROJECT_ROOT / "tests" / "fixtures" / "acceptance"
        / "seedance25_multicharacter.json"
    )

    def test_fixture_covers_the_no_cost_end_to_end_contract(self):
        payload = json.loads(self.FIXTURE.read_text(encoding="utf-8"))

        self.assertEqual(len(payload["characters"]), 3)
        self.assertEqual(len(set(payload["recurring_scenes"])), 2)
        graph = master_dependencies.build_master_dependency_graph(
            payload["masters"]
        )
        self.assertEqual(graph["waves"], payload["expected_dependency_waves"])

        manifest = reference_attention.select_manifest(
            [reference_attention.candidate(**item)
             for item in payload["reference_candidates"]],
            max_video_references=3,
            max_total_references=6,
        )
        duties = reference_attention.compile_prompt_duties(manifest)
        self.assertEqual(
            [item["duty"] for item in manifest],
            [
                "scene_continuity", "speaker_identity",
                "supporting_identity", "voice_continuity",
            ],
        )
        self.assertTrue(any(line.startswith("视频1") for line in duties))
        self.assertTrue(any(line.startswith("音频1") for line in duties))

        risk = master_risk.assess_master(payload["masters"][3])
        self.assertEqual(risk["level"], "high")
        self.assertEqual(
            [item["kind"] for item in payload["review_candidates"]],
            ["edit", "extend"],
        )
        self.assertEqual(
            sum(payload["cost_breakdown_cents"].values()),
            payload["expected_total_cost_cents"],
        )
        self.assertEqual(
            payload["state_path"],
            [
                "STORYBOARDED", "PRODUCTION_QUEUED",
                "PRODUCTION_RUNNING", "ANCHOR_REVIEW_REQUIRED",
                "PRODUCTION_REVIEW_REQUIRED", "SHOTS_OK", "BGM_OK",
                "COMPOSED", "READY_TO_PUBLISH",
            ],
        )
        self.assertEqual(payload["remote_recovery"]["method"], "GET")
        self.assertFalse(payload["remote_recovery"]["allow_submit"])

    def test_fixture_adaptive_capacity_and_provider_ledger_are_idempotent(self):
        payload = json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        database = memory_database()
        reduced = provider_capacity.record_rate_limit(
            database, "seedance",
            configured_limit=payload["configured_concurrency"],
            cooldown_seconds=0,
        )
        self.assertEqual(reduced["effective_limit"], 2)
        for _ in range(5):
            recovered = provider_capacity.record_wave_success(
                database, "seedance",
                configured_limit=payload["configured_concurrency"],
            )
        self.assertEqual(recovered["effective_limit"], 3)

        call = payload["provider_call"]
        database.execute(
            "INSERT OR IGNORE INTO provider_calls(task_id,job_id,shot_idx,"
            "provider,intent,usage_json,cost_cents) VALUES(?,?,?,?,?,?,?)",
            (
                call["task_id"], call["job_id"], call["shot_idx"],
                call["provider"], call["intent"],
                json.dumps({"status": "completed"}), call["cost_cents"],
            ),
        )
        database.execute(
            "INSERT OR IGNORE INTO provider_calls(task_id,job_id,shot_idx,"
            "provider,intent,usage_json,cost_cents) VALUES(?,?,?,?,?,?,?)",
            (
                call["task_id"], call["job_id"], call["shot_idx"],
                call["provider"], call["intent"],
                json.dumps({"status": "completed"}), call["cost_cents"],
            ),
        )
        self.assertEqual(database.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE task_id=?",
            (call["task_id"],),
        ).fetchone()[0], 1)


def memory_database() -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.executescript(db.DDL)
    return database


class ProviderFailureFixtureTests(unittest.TestCase):
    REQUIRED = {
        "account_overdue",
        "privacy_rejected",
        "output_policy_rejected",
        "invalid_parameter",
        "transient_disconnect",
    }

    def test_fixed_provider_failure_fixtures_are_minimal_and_sanitized(self):
        self.assertEqual(
            {path.stem for path in PROVIDER_FAILURE_FIXTURES.glob("*.json")},
            self.REQUIRED,
        )
        forbidden_fragments = (
            "request_id", "task_id", "authorization", "api_key", "token",
            "secret", "credential", "http://", "https://", "file://",
            "c:\\\\", "/users/", "/home/",
        )
        for name in sorted(self.REQUIRED):
            with self.subTest(fixture=name):
                payload = load_provider_failure(name)
                self.assertEqual(
                    set(payload), {"status", "error", "provider_task_state"}
                )
                self.assertIsInstance(payload["status"], int)
                self.assertEqual(set(payload["error"]), {"code", "message"})
                self.assertTrue(str(payload["error"]["code"]).strip())
                self.assertTrue(str(payload["error"]["message"]).strip())
                self.assertIn(
                    payload["provider_task_state"],
                    {"rejected", "failed", "unknown"},
                )
                serialized = json.dumps(payload, ensure_ascii=False).casefold()
                for fragment in forbidden_fragments:
                    self.assertNotIn(fragment, serialized)

    def test_fixed_fixtures_classify_into_the_six_code_contract(self):
        expected = {
            "account_overdue": (
                seedance.SeedanceAccountUnavailableError,
                "account_unavailable",
            ),
            "privacy_rejected": (
                seedance.SeedanceInputPrivacyError,
                "input_privacy_rejected",
            ),
            "output_policy_rejected": (
                seedance.SeedanceOutputPolicyError,
                "output_policy_rejected",
            ),
            "invalid_parameter": (
                seedance.SeedanceParameterError,
                "request_invalid",
            ),
            "transient_disconnect": (
                seedance.SeedanceSubmissionUnknownError,
                "transient_network",
            ),
        }
        for name, (error_type, stable_code) in expected.items():
            with self.subTest(fixture=name):
                payload = load_provider_failure(name)
                error = seedance._task_failure_error(
                    f"fixture-{name}",
                    {
                        "status": payload["provider_task_state"],
                        "error": payload["error"],
                    },
                )
                self.assertIsInstance(error, error_type)
                self.assertEqual(error.stable_code, stable_code)
                self.assertTrue(error.non_retryable)
                self.assertFalse(orchestrator._should_retry_step(error))
                message = str(error)
                self.assertNotIn("Traceback", message)
                self.assertNotRegex(
                    message.casefold(), r"authorization|api[_-]?key|bearer\s+"
                )


class BrowserFaultGateSelfTests(unittest.TestCase):
    def test_unexpected_http_500_fails_without_console_error(self):
        from tests import management_fault_browser_check as browser_check

        page = Mock()
        unexpected_responses = [
            "500 http://127.0.0.1:49152/api/videos"
        ]

        with self.assertRaisesRegex(AssertionError, "500.*api/videos"):
            browser_check.assert_clean(page, [], unexpected_responses)

        page.close.assert_called_once_with()


class ReleaseOutcomeAssertions:
    OUTCOME_FIELDS = {
        "calls", "cost", "job_state", "shot_states", "next_action", "message",
    }

    def assert_release_outcome(
        self, outcome: dict, *, calls: dict, cost: dict, job_state: str,
        shot_states: tuple[str, ...], next_action: dict | None,
        message_contains: str | None,
    ) -> None:
        self.assertEqual(set(outcome), self.OUTCOME_FIELDS)
        self.assertEqual(outcome["calls"], calls)
        self.assertEqual(outcome["cost"], cost)
        self.assertEqual(outcome["job_state"], job_state)
        self.assertEqual(tuple(outcome["shot_states"]), shot_states)
        self.assertEqual(outcome["next_action"], next_action)
        if message_contains is None:
            self.assertIsNone(outcome["message"])
            return
        message = str(outcome["message"] or "")
        self.assertIn(message_contains, message)
        self.assertTrue(message.strip())
        self.assertNotIn("Traceback", message)
        self.assertNotRegex(
            message.casefold(),
            r"authorization|api[_-]?key|bearer\s+|secret[-_ ]?token",
        )


class ProductionFailureOutcomeMatrixTests(
    ReleaseOutcomeAssertions, unittest.IsolatedAsyncioTestCase
):
    """Release-level outcomes: calls, cost, state, action, and safe message."""

    def setUp(self):
        self.database = memory_database()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def insert_job(self, job_id: str, *, state: str = "PRODUCTION_RUNNING") -> None:
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES(?,?,?,?,?,0)",
            (job_id, "T-MATRIX", "固定故障矩阵", state, "{}"),
        )
        self.database.commit()

    def job_view(self, job_id: str) -> dict:
        return next(
            job for job in ManagementService(self.database, self.root).videos()["jobs"]
            if job["job_id"] == job_id
        )

    def assert_safe_message(self, message: str) -> None:
        self.assertTrue(message.strip())
        self.assertNotIn("Traceback", message)
        self.assertNotRegex(
            message.casefold(),
            r"authorization|api[_-]?key|bearer\s+|secret[-_ ]?token",
        )

    async def test_invalid_ai_json_has_one_repair_and_complete_zero_paid_outcome(self):
        from tests.test_script_drafts import _brief_payload

        source = script_sources.ingest_text(
            self.database,
            "客户需求散落在多个业务员的聊天记录里，信息无法统一管理。",
            title="固定矩阵输入",
            source_uri="manual://failure-matrix-json",
        )
        brief_id = "BRIEF-MATRIX-JSON"
        job_id = "J-MATRIX-JSON"
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,"
            "brief_json,approved_topic_id,approved_job_id,approved_angle_index) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                brief_id, "approved", "固定 JSON 故障矩阵",
                json.dumps(_brief_payload(), ensure_ascii=False),
                "T-MATRIX-JSON", job_id, 0,
            ),
        )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,cost_cents) "
            "VALUES(?,?,?,'PENDING',0)",
            (job_id, "T-MATRIX-JSON", "固定 JSON 故障矩阵"),
        )
        self.database.execute(
            "INSERT INTO topic_brief_sources(brief_id,source_id) VALUES(?,?)",
            (brief_id, source.source_id),
        )
        self.database.commit()
        model_calls = 0

        def invalid_json(_prompt, max_tokens):
            del max_tokens
            nonlocal model_calls
            model_calls += 1
            raise json.JSONDecodeError(
                "Expecting ',' delimiter", '{"variants": [}', 14
            )

        with self.assertRaises(ValueError) as raised:
            script_drafts.generate_drafts(
                self.database, brief_id, generate_json=invalid_json
            )

        job = self.database.execute(
            "SELECT state,cost_cents FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        shots = self.database.execute(
            "SELECT state,cost_cents FROM shots WHERE job_id=? ORDER BY idx",
            (job_id,),
        ).fetchall()
        visible_ids = {
            item["job_id"]
            for item in ManagementService(self.database, self.root).videos()["jobs"]
        }
        self.assertNotIn(job_id, visible_ids)
        self.assert_release_outcome(
            {
                "calls": {
                    "model": model_calls, "provider_submit": 0,
                    "provider_rows": self.database.execute(
                        "SELECT COUNT(*) FROM provider_calls WHERE job_id=?",
                        (job_id,),
                    ).fetchone()[0],
                },
                "cost": {
                    "job": job["cost_cents"],
                    "shots": tuple(row["cost_cents"] for row in shots),
                },
                "job_state": job["state"],
                "shot_states": tuple(row["state"] for row in shots),
                "next_action": None,
                "message": str(raised.exception),
            },
            calls={"model": 2, "provider_submit": 0, "provider_rows": 0},
            cost={"job": 0, "shots": ()},
            job_state="PENDING", shot_states=(), next_action=None,
            message_contains="AI 连续两次返回的剧本 JSON 格式无效",
        )

    async def test_preconnect_failure_has_finite_retries_and_zero_new_cost(self):
        self.insert_job("J-PRECONNECT", state="PENDING")
        connect_error = seedance._submission_transport_error(
            httpx.ConnectError("connection refused before request")
        )
        run_step = AsyncMock(side_effect=connect_error)
        with (
            patch.object(orchestrator, "conn", return_value=self.database),
            patch.object(orchestrator, "_run_step", new=run_step),
            patch.object(orchestrator, "MAX_RETRIES", 3),
            patch.object(orchestrator.asyncio, "sleep", new=AsyncMock()),
        ):
            await orchestrator._process_job(
                "J-PRECONNECT", asyncio.Semaphore(1)
            )

        row = self.database.execute(
            "SELECT state,cost_cents,error FROM jobs WHERE job_id='J-PRECONNECT'"
        ).fetchone()
        view = self.job_view("J-PRECONNECT")
        self.assertEqual(run_step.await_count, 3, "submit attempts must be finite")
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE job_id='J-PRECONNECT'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual((row["cost_cents"], row["state"]), (0, "FAILED"))
        self.assertEqual(view["next_action"], {
            "id": "retry_failed_job",
            "label": "重新尝试任务",
        })
        self.assert_safe_message(row["error"])

    async def test_account_circuit_blocks_sibling_submit_and_exposes_next_action(self):
        self.insert_job("J-ACCOUNT-MATRIX")
        for idx in (1, 2):
            self.database.execute(
                "INSERT INTO shots(job_id,idx,state) VALUES(?,?,'GENERATING')",
                ("J-ACCOUNT-MATRIX", idx),
            )
        self.database.commit()
        payload = load_provider_failure("account_overdue")
        account_error = seedance._task_failure_error(
            "fixture-account", {
                "status": payload["provider_task_state"],
                "error": payload["error"],
            }
        )
        submit = AsyncMock(side_effect=account_error)
        route = VideoRoute(
            model="seedance-test", tier="seedance25",
            generate_audio=False, qa_required=False, fallback_model=None,
        )
        with patch.object(s3_shots.seedance, "text_to_video", new=submit):
            with self.assertRaises(seedance.SeedanceAccountUnavailableError) as first:
                await s3_shots._generate_seedance_video(
                    prompt="safe equipment scene", route=route,
                    request_kwargs={"duration": 4, "resolution": "480p"},
                    out_path=self.root / "first.mp4", key_hint="account-first",
                    job_id="J-ACCOUNT-MATRIX", shot_idx=1,
                    database=self.database,
                )
            with self.assertRaises(seedance.SeedanceAccountUnavailableError) as sibling:
                await s3_shots._generate_seedance_video(
                    prompt="safe sibling scene", route=route,
                    request_kwargs={"duration": 4, "resolution": "480p"},
                    out_path=self.root / "sibling.mp4", key_hint="account-sibling",
                    job_id="J-ACCOUNT-MATRIX", shot_idx=2,
                    database=self.database,
                )
        s3_shots._record_failed_generation(
            self.database, "J-ACCOUNT-MATRIX", 1, first.exception
        )
        s3_shots._record_failed_generation(
            self.database, "J-ACCOUNT-MATRIX", 2, sibling.exception
        )
        aggregate = s3_shots._aggregate_shot_failures([(1, first.exception)])
        with (
            patch.object(orchestrator, "conn", return_value=self.database),
            patch.object(
                orchestrator, "_run_step", new=AsyncMock(side_effect=aggregate)
            ),
        ):
            await orchestrator._process_job(
                "J-ACCOUNT-MATRIX", asyncio.Semaphore(1)
            )

        row = self.database.execute(
            "SELECT state,cost_cents,error FROM jobs "
            "WHERE job_id='J-ACCOUNT-MATRIX'"
        ).fetchone()
        shots = self.database.execute(
            "SELECT state,cost_cents FROM shots "
            "WHERE job_id='J-ACCOUNT-MATRIX' ORDER BY idx"
        ).fetchall()
        view = self.job_view("J-ACCOUNT-MATRIX")
        self.assertEqual(
            tuple(item["failure_code"] for item in view["shots"]),
            ("account_unavailable", "account_unavailable"),
        )
        self.assertEqual(submit.await_count, 1, "the sibling must make zero POSTs")
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM provider_calls "
                "WHERE job_id='J-ACCOUNT-MATRIX'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual((row["cost_cents"], row["state"]), (
            0, "PROVIDER_ACTION_REQUIRED",
        ))
        self.assertEqual(view["next_action"]["id"], "view_provider_account_guide")
        self.assertIn("账号", view["next_action"]["label"])
        self.assert_safe_message(row["error"])
        self.assertIsInstance(
            sibling.exception, seedance.SeedanceAccountUnavailableError
        )
        self.assert_release_outcome(
            {
                "calls": {
                    "provider_submit": submit.await_count,
                    "provider_rows": self.database.execute(
                        "SELECT COUNT(*) FROM provider_calls "
                        "WHERE job_id='J-ACCOUNT-MATRIX'"
                    ).fetchone()[0],
                },
                "cost": {
                    "job": view["cost_cents"],
                    "shots": tuple(item["cost_cents"] for item in view["shots"]),
                },
                "job_state": view["state"],
                "shot_states": tuple(item["state"] for item in view["shots"]),
                "next_action": view["next_action"],
                "message": view["error"],
            },
            calls={"provider_submit": 1, "provider_rows": 0},
            cost={"job": 0, "shots": (0, 0)},
            job_state="PROVIDER_ACTION_REQUIRED",
            shot_states=("FAILED", "FAILED"),
            next_action={
                "id": "view_provider_account_guide",
                "label": "查看账号处理指引",
            },
            message_contains="账号",
        )

        service = ManagementService(self.database, self.root)
        with self.assertRaises(seedance.SeedanceAccountUnavailableError):
            service.resume_failed_video("J-ACCOUNT-MATRIX")
        still_paused = self.job_view("J-ACCOUNT-MATRIX")
        self.assertFalse(still_paused["resumable"])
        self.assertEqual(
            tuple(item["state"] for item in still_paused["shots"]),
            ("FAILED", "FAILED"),
        )
        service.clear_seedance_provider_block()
        cleared_view = self.job_view("J-ACCOUNT-MATRIX")
        self.assertTrue(cleared_view["resumable"])
        self.assertEqual(
            cleared_view["next_action"],
            {"id": "resume_failed_video", "label": "继续生成剩余母片"},
        )
        recovered = service.resume_failed_video("J-ACCOUNT-MATRIX")
        recovered_shots = self.database.execute(
            "SELECT state FROM shots WHERE job_id='J-ACCOUNT-MATRIX' ORDER BY idx"
        ).fetchall()
        self.assertEqual(recovered["state"], "STORYBOARDED")
        self.assertEqual(
            tuple(item["state"] for item in recovered_shots),
            ("PENDING", "PENDING"),
        )

    async def test_submit_ambiguity_has_one_post_and_complete_paused_outcome(self):
        job_id = "J-SUBMIT-AMBIGUOUS"
        self.insert_job(job_id)
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,duration_sec,cost_cents) "
            "VALUES(?,1,'GENERATING',4,0)",
            (job_id,),
        )
        self.database.commit()
        calls = {"provider_submit": 0}

        def handler(provider_request):
            if provider_request.method == "POST":
                calls["provider_submit"] += 1
            raise httpx.RemoteProtocolError(
                "server disconnected after request", request=provider_request
            )

        real_async_client = httpx.AsyncClient

        def client_factory(*_args, **kwargs):
            return real_async_client(
                transport=httpx.MockTransport(handler),
                headers=kwargs.get("headers"),
            )

        route = VideoRoute(
            model="seedance-test", tier="seedance25",
            generate_audio=False, qa_required=False, fallback_model=None,
        )
        with (
            patch.dict(os.environ, {"ARK_API_KEY": "test-key"}),
            patch.object(
                seedance.httpx, "AsyncClient", side_effect=client_factory
            ),
            patch.object(seedance.asyncio, "sleep", new=AsyncMock()),
        ):
            with self.assertRaises(
                seedance.SeedanceSubmissionUnknownError
            ) as raised:
                await s3_shots._generate_seedance_video(
                    prompt="safe equipment scene", route=route,
                    request_kwargs={
                        "duration": 4, "resolution": "480p", "timeout": 5,
                    },
                    out_path=self.root / "ambiguous.mp4",
                    key_hint="submit-ambiguous", job_id=job_id, shot_idx=1,
                    database=self.database,
                )
        s3_shots._record_failed_generation(
            self.database, job_id, 1, raised.exception
        )
        aggregate = s3_shots._aggregate_shot_failures([(1, raised.exception)])
        with (
            patch.object(orchestrator, "conn", return_value=self.database),
            patch.object(
                orchestrator, "_run_step", new=AsyncMock(side_effect=aggregate)
            ),
        ):
            await orchestrator._process_job(job_id, asyncio.Semaphore(1))

        job = self.database.execute(
            "SELECT state,cost_cents,error FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        shots = self.database.execute(
            "SELECT state,cost_cents FROM shots WHERE job_id=? ORDER BY idx",
            (job_id,),
        ).fetchall()
        provider_rows = self.database.execute(
            "SELECT task_id,cost_cents,usage_json FROM provider_calls "
            "WHERE job_id=?",
            (job_id,),
        ).fetchall()
        view = self.job_view(job_id)
        self.assertFalse(view["resumable"])
        self.assertEqual(len(view["unresolved_provider_tasks"]), 1)
        self.assertFalse(view["unresolved_provider_tasks"][0]["reconcilable"])
        self.assert_release_outcome(
            {
                "calls": {
                    **calls, "provider_rows": len(provider_rows),
                },
                "cost": {
                    "job": view["cost_cents"],
                    "shots": tuple(item["cost_cents"] for item in view["shots"]),
                },
                "job_state": view["state"],
                "shot_states": tuple(item["state"] for item in view["shots"]),
                "next_action": view["next_action"],
                "message": view["error"],
            },
            calls={"provider_submit": 1, "provider_rows": 1},
            cost={"job": 0, "shots": (0,)},
            job_state="PROVIDER_ACTION_REQUIRED", shot_states=("FAILED",),
            next_action={
                "id": "view_provider_account_guide",
                "label": "查看账号处理指引",
            },
            message_contains="提交结果未知",
        )

    async def test_policy_privacy_and_copyright_are_zero_retry_zero_cost(self):
        fixtures = {
            "privacy": load_provider_failure("privacy_rejected"),
            "output_policy": load_provider_failure("output_policy_rejected"),
        }
        errors = {
            name: seedance._task_failure_error(
                f"fixture-{name}", {
                    "status": payload["provider_task_state"],
                    "error": payload["error"],
                },
            )
            for name, payload in fixtures.items()
        }
        errors["copyright"] = seedance._task_failure_error(
            "fixture-copyright", {
                "status": "rejected",
                "error": {
                    "code": "CopyrightViolation",
                    "message": "Generated output matched protected content",
                },
            }
        )
        for name, error in errors.items():
            with self.subTest(failure=name):
                job_id = f"J-{name.upper()}"
                self.insert_job(job_id)
                self.database.execute(
                    "INSERT INTO shots(job_id,idx,state,duration_sec,cost_cents) "
                    "VALUES(?,1,'GENERATING',4,0)",
                    (job_id,),
                )
                self.database.commit()
                s3_shots._record_failed_generation(
                    self.database, job_id, 1, error
                )
                aggregate = s3_shots._aggregate_shot_failures([(1, error)])
                run_step = AsyncMock(side_effect=aggregate)
                with (
                    patch.object(orchestrator, "conn", return_value=self.database),
                    patch.object(orchestrator, "_run_step", new=run_step),
                ):
                    await orchestrator._process_job(
                        job_id, asyncio.Semaphore(1)
                    )
                row = self.database.execute(
                    "SELECT state,cost_cents,error FROM jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                view = self.job_view(job_id)
                provider_submit = AsyncMock()
                service = ManagementService(self.database, self.root)
                with (
                    patch.object(
                        service, "dispatch_production_queue"
                    ) as dispatch,
                    patch.object(
                        s3_shots.seedance, "text_to_video", new=provider_submit
                    ),
                    self.assertRaisesRegex(
                        ValueError, "不可恢复|不能继续|failure code"
                    ),
                ):
                    service.resume_failed_video(job_id)
                self.assertEqual(run_step.await_count, 1)
                dispatch.assert_not_called()
                provider_submit.assert_not_awaited()
                self.assertEqual(
                    self.database.execute(
                        "SELECT COUNT(*) FROM provider_calls WHERE job_id=?",
                        (job_id,),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual((row["cost_cents"], row["state"]), (
                    0, "PROVIDER_ACTION_REQUIRED",
                ))
                self.assertFalse(view["resumable"])
                self.assertEqual(
                    view["next_action"]["id"], "view_provider_account_guide"
                )
                self.assertEqual(
                    view["shots"][0]["failure_code"], error.stable_code
                )
                persisted = self.database.execute(
                    "SELECT state FROM shots WHERE job_id=? AND idx=1",
                    (job_id,),
                ).fetchone()
                self.assertEqual(persisted["state"], "FAILED")
                self.assert_safe_message(row["error"])

    async def test_anchor_failures_only_resume_cleared_account_code(self):
        ManagementService(self.database, self.root)
        payloads = {
            "account": load_provider_failure("account_overdue"),
            "privacy": load_provider_failure("privacy_rejected"),
            "output_policy": load_provider_failure("output_policy_rejected"),
        }
        errors = {
            name: seedance._task_failure_error(
                f"fixture-anchor-{name}", {
                    "status": payload["provider_task_state"],
                    "error": payload["error"],
                },
            )
            for name, payload in payloads.items()
        }
        errors["copyright"] = seedance._task_failure_error(
            "fixture-anchor-copyright", {
                "status": "rejected",
                "error": {
                    "code": "CopyrightViolation",
                    "message": "Generated output matched protected content",
                },
            },
        )
        expected_codes = {
            "account": "account_unavailable",
            "privacy": "input_privacy_rejected",
            "output_policy": "output_policy_rejected",
            "copyright": "output_copyright_rejected",
        }
        characters = [{
            "id": "lead", "name": "虚构讲解员",
            "generation_mode": "ai_generated",
        }]
        storyboard = {
            "characters": characters,
            "masters": [{
                "idx": 1, "type": "dialogue", "speaker": "lead",
                "duration": 4, "voiceover": "设备巡检已完成。",
            }],
        }

        for name, anchor_error in errors.items():
            with self.subTest(failure=name):
                job_id = f"J-ANCHOR-{name.upper()}"
                self.database.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,"
                    "characters_json,cost_cents) VALUES(?,?,?,'PRODUCTION_RUNNING',?,?,0)",
                    (
                        job_id, "T-ANCHOR", "锚点固定故障矩阵",
                        json.dumps(storyboard, ensure_ascii=False),
                        json.dumps(characters, ensure_ascii=False),
                    ),
                )
                self.database.execute(
                    "INSERT INTO shots(job_id,idx,state,duration_sec,cost_cents) "
                    "VALUES(?,1,'PENDING',4,0)",
                    (job_id,),
                )
                self.database.commit()
                if name == "account":
                    provider_health.block(
                        self.database, "seedance", "account_unavailable",
                        "Seedance account action required",
                    )

                async def run_anchor_step(_job_id, _step):
                    await s3_shots.run(_job_id)

                with (
                    patch.object(s3_shots, "conn", return_value=self.database),
                    patch.object(
                        s3_shots, "_ensure_character_identity_anchors",
                        new=AsyncMock(side_effect=anchor_error),
                    ) as ensure_anchor,
                    patch.object(orchestrator, "conn", return_value=self.database),
                    patch.object(orchestrator, "_run_step", new=run_anchor_step),
                ):
                    await orchestrator._process_job(
                        job_id, asyncio.Semaphore(1)
                    )
                ensure_anchor.assert_awaited_once()

                view = self.job_view(job_id)
                self.assertEqual(view["state"], "PROVIDER_ACTION_REQUIRED")
                self.assertEqual(view["shots"][0]["state"], "PENDING")
                self.assertEqual(view["failure_code"], expected_codes[name])
                self.assertEqual(view["cost_cents"], 0)
                self.assertEqual(
                    self.database.execute(
                        "SELECT COUNT(*) FROM provider_calls WHERE job_id=?",
                        (job_id,),
                    ).fetchone()[0],
                    0,
                )
                self.assert_safe_message(view["error"])
                provider_submit = AsyncMock()
                service = ManagementService(self.database, self.root)
                with (
                    patch.object(
                        service, "dispatch_production_queue"
                    ) as dispatch,
                    patch.object(
                        s3_shots.seedance, "text_to_video", new=provider_submit
                    ),
                ):
                    if name == "account":
                        self.assertFalse(view["resumable"])
                        with self.assertRaises(
                            seedance.SeedanceAccountUnavailableError
                        ):
                            service.resume_failed_video(job_id)
                        service.clear_seedance_provider_block()
                        cleared = self.job_view(job_id)
                        self.assertTrue(cleared["resumable"])
                        recovered = service.resume_failed_video(job_id)
                        self.assertEqual(recovered["state"], "STORYBOARDED")
                    else:
                        self.assertFalse(view["resumable"])
                        with self.assertRaisesRegex(
                            ValueError, "不可恢复|failure code"
                        ):
                            service.resume_failed_video(job_id)
                dispatch.assert_not_called()
                provider_submit.assert_not_awaited()
                shot_state = self.database.execute(
                    "SELECT state FROM shots WHERE job_id=? AND idx=1",
                    (job_id,),
                ).fetchone()["state"]
                self.assertEqual(shot_state, "PENDING")

    async def test_image_submission_policy_400_failed_jobs_never_resume(self):
        cases = {
            "privacy": (
                "InputImageSensitiveContentDetected",
                "Input image may include private material",
                "input_privacy_rejected",
            ),
            "output_policy": (
                "OutputVideoSensitiveContentDetected",
                "Generated output rejected by policy",
                "output_policy_rejected",
            ),
            "copyright": (
                "CopyrightViolation",
                "Generated output matched protected content",
                "output_copyright_rejected",
            ),
        }
        for name, (provider_code, provider_message, stable_code) in cases.items():
            with self.subTest(failure=name):
                job_id = f"J-IMAGE-SUBMISSION-{name.upper()}"
                self.insert_job(job_id)
                self.database.execute(
                    "INSERT INTO shots(job_id,idx,state,duration_sec,cost_cents) "
                    "VALUES(?,1,'PENDING',4,0)",
                    (job_id,),
                )
                self.database.commit()
                response = httpx.Response(
                    400,
                    json={
                        "error": {
                            "code": provider_code,
                            "message": provider_message,
                        },
                    },
                )
                submission_error = seedance._submission_error(
                    response,
                    endpoint="/images/generations",
                    model="seedream-test",
                    content_roles=["user"],
                )
                self.assertEqual(submission_error.stable_code, stable_code)
                self.assertFalse(hasattr(submission_error, "pause_state"))
                with (
                    patch.object(orchestrator, "conn", return_value=self.database),
                    patch.object(
                        orchestrator, "_run_step",
                        new=AsyncMock(side_effect=submission_error),
                    ),
                ):
                    await orchestrator._process_job(
                        job_id, asyncio.Semaphore(1)
                    )

                view = self.job_view(job_id)
                self.assertEqual(view["state"], "FAILED")
                self.assertEqual(view["failure_code"], stable_code)
                self.assertFalse(view["resumable"])
                provider_submit = AsyncMock()
                service = ManagementService(self.database, self.root)
                with (
                    patch.object(
                        service, "dispatch_production_queue"
                    ) as dispatch,
                    patch.object(
                        s3_shots.seedance, "text_to_image", new=provider_submit
                    ),
                    self.assertRaisesRegex(
                        ValueError, "不可恢复|failure code"
                    ),
                ):
                    service.resume_failed_video(job_id)
                dispatch.assert_not_called()
                provider_submit.assert_not_awaited()
                persisted = self.database.execute(
                    "SELECT state,failure_code FROM jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                self.assertEqual(
                    (persisted["state"], persisted["failure_code"]),
                    ("FAILED", stable_code),
                )

    async def test_soft_mismatch_candidate_controls_review_vs_failed_state(self):
        with patch.object(s3_shots, "ASSETS_DIR", self.root):
            for reason in ("transcript_mismatch", "visual_mismatch"):
                for has_candidate in (True, False):
                    with self.subTest(reason=reason, candidate=has_candidate):
                        suffix = "CANDIDATE" if has_candidate else "NONE"
                        job_id = f"J-{reason.upper()}-{suffix}"
                        self.insert_job(job_id)
                        self.database.execute(
                            "INSERT INTO shots(job_id,idx,state,duration_sec) "
                            "VALUES(?,1,'GENERATING',4)",
                            (job_id,),
                        )
                        self.database.commit()
                        candidate = None
                        if has_candidate:
                            candidate = self.root / job_id / "review" / "candidate.mp4"
                            candidate.parent.mkdir(parents=True)
                            candidate.write_bytes(b"paid-candidate")
                        error = s3_shots.SeedanceQualityError(
                            f"{reason} requires manual review",
                            cost_cents=406,
                            provider_calls=({
                                "task_id": f"task-{reason}-{suffix}",
                                "intent": "generate",
                                "usage": {"status": "completed"},
                                "cost_cents": 406,
                            },),
                            reasons=(reason,), candidate_path=candidate,
                            candidate_url=(
                                "https://assets.example/candidate.mp4"
                                if has_candidate else None
                            ),
                        )
                        s3_shots._record_failed_generation(
                            self.database, job_id, 1, error
                        )
                        aggregate = s3_shots._aggregate_shot_failures([(1, error)])
                        with (
                            patch.object(
                                orchestrator, "conn", return_value=self.database
                            ),
                            patch.object(
                                orchestrator, "_run_step",
                                new=AsyncMock(side_effect=aggregate),
                            ),
                        ):
                            await orchestrator._process_job(
                                job_id, asyncio.Semaphore(1)
                            )
                        row = self.database.execute(
                            "SELECT state,cost_cents,error FROM jobs WHERE job_id=?",
                            (job_id,),
                        ).fetchone()
                        shot = self.database.execute(
                            "SELECT state,cost_cents FROM shots WHERE job_id=?",
                            (job_id,),
                        ).fetchone()
                        view = self.job_view(job_id)
                        expected_state = (
                            "PRODUCTION_REVIEW_REQUIRED" if has_candidate else "FAILED"
                        )
                        expected_shot = "REVIEW_REQUIRED" if has_candidate else "FAILED"
                        self.assertEqual(
                            self.database.execute(
                                "SELECT COUNT(*) FROM provider_calls WHERE job_id=?",
                                (job_id,),
                            ).fetchone()[0],
                            1,
                        )
                        self.assertEqual(
                            (row["cost_cents"], row["state"]),
                            (406, expected_state),
                        )
                        self.assertEqual(
                            (shot["cost_cents"], shot["state"]),
                            (406, expected_shot),
                        )
                        self.assertEqual(
                            view["next_action"]["id"] if has_candidate else None,
                            "review_master_candidates" if has_candidate else None,
                        )
                        self.assert_safe_message(row["error"])


class BackgroundMusicFailureMatrixTests(
    ReleaseOutcomeAssertions, unittest.IsolatedAsyncioTestCase
):
    async def test_default_local_silence_and_explicit_library_failure_are_distinct(self):
        database = memory_database()
        storyboard = json.dumps({
            "masters": [{"idx": 1, "duration": 4, "type": "dialogue"}],
        })
        for job_id in ("J-BGM-LOCAL", "J-BGM-LIBRARY"):
            database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
                "VALUES(?, 'T-BGM', 'bgm matrix', 'SHOTS_OK', ?, 0)",
                (job_id, storyboard),
            )
            database.execute(
                "INSERT INTO shots(job_id,idx,state,duration_sec,cost_cents) "
                "VALUES(?,1,'OK',4,0)",
                (job_id,),
            )
        database.commit()
        bgm_transition = tuple(
            item for item in orchestrator.TRANSITIONS
            if item[0].value == "SHOTS_OK"
        )
        with tempfile.TemporaryDirectory() as tmp:
            silent = AsyncMock(return_value=b"silent-bgm")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(s5_bgm, "conn", return_value=database),
                patch.object(s5_bgm, "ASSETS_DIR", Path(tmp)),
                patch.object(music, "_via_silent", new=silent),
                patch.object(orchestrator, "conn", return_value=database),
                patch.object(orchestrator, "TRANSITIONS", bgm_transition),
            ):
                await orchestrator._process_job(
                    "J-BGM-LOCAL", asyncio.Semaphore(1)
                )
            local_artifact = Path(tmp) / "J-BGM-LOCAL" / "bgm.mp3"
            self.assertEqual(local_artifact.read_bytes(), b"silent-bgm")

            library = Mock(side_effect=music.MusicProviderConfigError(
                "no local bgm"
            ))
            paid_submit = AsyncMock()
            with (
                patch.dict(
                    os.environ, {"DEFAULT_MUSIC_PROVIDER": "library"}, clear=True
                ),
                patch.object(s5_bgm, "conn", return_value=database),
                patch.object(s5_bgm, "ASSETS_DIR", Path(tmp)),
                patch.object(music, "_via_library", new=library),
                patch.object(music, "_via_suno", new=paid_submit),
                patch.object(orchestrator, "conn", return_value=database),
                patch.object(orchestrator, "TRANSITIONS", bgm_transition),
                patch.object(orchestrator, "MAX_RETRIES", 3),
                patch.object(orchestrator.asyncio, "sleep", new=AsyncMock()),
            ):
                await orchestrator._process_job(
                    "J-BGM-LIBRARY", asyncio.Semaphore(1)
                )

            views = {
                item["job_id"]: item
                for item in ManagementService(database, Path(tmp)).videos()["jobs"]
                if item["job_id"] in {"J-BGM-LOCAL", "J-BGM-LIBRARY"}
            }

        jobs = {
            row["job_id"]: row
            for row in database.execute(
                "SELECT job_id,state,cost_cents,bgm_path,error FROM jobs "
                "WHERE job_id LIKE 'J-BGM-%'"
            ).fetchall()
        }
        shots = {
            row["job_id"]: row
            for row in database.execute(
                "SELECT job_id,state,cost_cents FROM shots "
                "WHERE job_id LIKE 'J-BGM-%'"
            ).fetchall()
        }
        self.assert_release_outcome(
            {
                "calls": {
                    "local": silent.await_count, "provider_submit": 0,
                    "provider_rows": database.execute(
                        "SELECT COUNT(*) FROM provider_calls "
                        "WHERE job_id='J-BGM-LOCAL'"
                    ).fetchone()[0],
                },
                "cost": {
                    "job": views["J-BGM-LOCAL"]["cost_cents"],
                    "shots": tuple(
                        item["cost_cents"]
                        for item in views["J-BGM-LOCAL"]["shots"]
                    ),
                },
                "job_state": views["J-BGM-LOCAL"]["state"],
                "shot_states": tuple(
                    item["state"] for item in views["J-BGM-LOCAL"]["shots"]
                ),
                "next_action": views["J-BGM-LOCAL"]["next_action"],
                "message": views["J-BGM-LOCAL"]["error"],
            },
            calls={"local": 1, "provider_submit": 0, "provider_rows": 0},
            cost={"job": 0, "shots": (0,)},
            job_state="BGM_OK", shot_states=("OK",), next_action={
                "id": "resume_local_postproduction",
                "label": "继续本地合成",
            },
            message_contains=None,
        )
        self.assert_release_outcome(
            {
                "calls": {
                    "local": library.call_count,
                    "provider_submit": paid_submit.await_count,
                    "provider_rows": database.execute(
                        "SELECT COUNT(*) FROM provider_calls "
                        "WHERE job_id='J-BGM-LIBRARY'"
                    ).fetchone()[0],
                },
                "cost": {
                    "job": views["J-BGM-LIBRARY"]["cost_cents"],
                    "shots": tuple(
                        item["cost_cents"]
                        for item in views["J-BGM-LIBRARY"]["shots"]
                    ),
                },
                "job_state": views["J-BGM-LIBRARY"]["state"],
                "shot_states": tuple(
                    item["state"] for item in views["J-BGM-LIBRARY"]["shots"]
                ),
                "next_action": views["J-BGM-LIBRARY"]["next_action"],
                "message": views["J-BGM-LIBRARY"]["error"],
            },
            calls={"local": 1, "provider_submit": 0, "provider_rows": 0},
            cost={"job": 0, "shots": (0,)},
            job_state="FAILED", shot_states=("OK",), next_action={
                "id": "configure_music_provider",
                "label": "检查音乐配置",
            },
            message_contains="音乐",
        )
        self.assertTrue(jobs["J-BGM-LOCAL"]["bgm_path"])
        self.assertFalse(jobs["J-BGM-LIBRARY"]["bgm_path"])
        paid_submit.assert_not_awaited()
        database.close()

    def test_music_configuration_failure_resumes_only_local_postproduction_after_fix(self):
        database = memory_database()
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,"
            "failure_code,error) VALUES(?, 'T-BGM', 'bgm recovery', 'FAILED', ?, 0, ?, ?)",
            (
                "J-BGM-RECOVER",
                json.dumps({"masters": [{"idx": 1, "duration": 4, "type": "dialogue"}]}),
                "music_configuration_invalid",
                "音乐素材库配置不可用，请检查音乐配置",
            ),
        )
        database.execute(
            "INSERT INTO shots(job_id,idx,state,duration_sec,cost_cents) "
            "VALUES('J-BGM-RECOVER',1,'OK',4,0)"
        )
        database.commit()
        with tempfile.TemporaryDirectory() as tmp:
            service = ManagementService(database, Path(tmp))
            with (
                patch.object(service, "_music_provider", return_value="silent"),
                patch.object(s5_bgm, "run", new=AsyncMock()),
                patch.object(s6_compose, "run", new=AsyncMock()),
                patch(
                    "factory.management_api.finalize_accepted_candidates",
                    return_value=None,
                ),
            ):
                result = service.resume_failed_video("J-BGM-RECOVER")

        self.assertEqual(result["state"], "COMPOSED")
        self.assertEqual(result["preserved_masters"], 1)
        persisted = database.execute(
            "SELECT state,failure_code,cost_cents FROM jobs WHERE job_id='J-BGM-RECOVER'"
        ).fetchone()
        self.assertEqual(
            (persisted["state"], persisted["failure_code"], persisted["cost_cents"]),
            ("COMPOSED", None, 0),
        )
        database.close()


@unittest.skipIf(provider_health is None, "provider health not implemented")
class PaidImageCircuitMatrixTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = memory_database()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def insert_job(self, job_id: str) -> None:
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,'PRODUCTION_RUNNING','{}')",
            (job_id, "T-IMAGE-CIRCUIT", "付费锚点图故障矩阵"),
        )
        self.database.commit()

    async def submit_image(self, job_id: str, *, scene: bool = False):
        return await s3_shots._submit_paid_image(
            self.database,
            job_id=job_id,
            shot_idx=(-2001 if scene else -1001),
            provider=("seedream_scene" if scene else "seedream_identity"),
            intent=("scene_visual_anchor" if scene else "character_identity_anchor"),
            task_id=f"seedream-anchor:{job_id}",
            prompt="fictional equipment yard" if scene else "fictional presenter",
            resolution="1440x2560", model="seedream-test",
            usage={}, cost_cents=29,
        )

    async def test_open_seedance_circuit_blocks_identity_and_scene_before_reservation(self):
        for job_id in ("J-IMAGE-BLOCK-IDENTITY", "J-IMAGE-BLOCK-SCENE"):
            self.insert_job(job_id)
        provider_health.block(
            self.database, "seedance", "account_unavailable",
            "credential-shaped provider detail must not persist",
        )
        submit = AsyncMock(return_value=b"must-not-submit")
        with patch.object(s3_shots.seedance, "text_to_image", new=submit):
            with self.assertRaises(seedance.SeedanceAccountUnavailableError):
                await self.submit_image("J-IMAGE-BLOCK-IDENTITY")
            with self.assertRaises(seedance.SeedanceAccountUnavailableError):
                await self.submit_image("J-IMAGE-BLOCK-SCENE", scene=True)
        submit.assert_not_awaited()
        self.assertEqual(
            self.database.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0],
            0,
        )

    async def test_first_image_account_failure_blocks_sibling_but_policy_does_not(self):
        for job_id in (
            "J-IMAGE-ACCOUNT-FIRST", "J-IMAGE-ACCOUNT-SIBLING",
            "J-IMAGE-PRIVACY", "J-IMAGE-POLICY",
        ):
            self.insert_job(job_id)
        account = seedance.SeedanceAccountUnavailableError(
            "raw Authorization Bearer secret-provider-value"
        )
        submit = AsyncMock(side_effect=account)
        with patch.object(s3_shots.seedance, "text_to_image", new=submit):
            with self.assertRaises(seedance.SeedanceAccountUnavailableError):
                await self.submit_image("J-IMAGE-ACCOUNT-FIRST")
            with self.assertRaises(seedance.SeedanceAccountUnavailableError):
                await self.submit_image("J-IMAGE-ACCOUNT-SIBLING", scene=True)
        self.assertEqual(submit.await_count, 1)
        self.assertEqual(
            provider_health.get_status(self.database, "seedance")["status"],
            provider_health.ACTION_REQUIRED,
        )
        self.assertNotIn(
            "secret-provider-value",
            str(provider_health.get_status(
                self.database, "seedance"
            )["reason_message"]),
        )
        self.assertEqual(
            self.database.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0],
            0,
        )

        provider_health.clear(self.database, "seedance")
        policy_errors = {
            "J-IMAGE-PRIVACY": seedance.SeedanceInputPrivacyError("private input"),
            "J-IMAGE-POLICY": seedance.SeedanceOutputPolicyError("policy output"),
        }
        for job_id, policy_error in policy_errors.items():
            with self.subTest(job_id=job_id):
                policy_error.pause_state = "PROVIDER_ACTION_REQUIRED"
                policy_submit = AsyncMock(side_effect=policy_error)
                with patch.object(
                    s3_shots.seedance, "text_to_image", new=policy_submit
                ):
                    with self.assertRaises(type(policy_error)):
                        await self.submit_image(job_id)
                policy_submit.assert_awaited_once()
                self.assertEqual(
                    provider_health.get_status(
                        self.database, "seedance"
                    )["status"],
                    provider_health.AVAILABLE,
                )
                self.assertEqual(
                    self.database.execute(
                        "SELECT COUNT(*) FROM provider_calls WHERE job_id=?",
                        (job_id,),
                    ).fetchone()[0],
                    0,
                )
                self.database.execute(
                    "UPDATE jobs SET state='PRODUCTION_RUNNING' WHERE job_id=?",
                    (job_id,),
                )
                self.database.commit()
                with (
                    patch.object(orchestrator, "conn", return_value=self.database),
                    patch.object(
                        orchestrator, "_run_step",
                        new=AsyncMock(side_effect=policy_error),
                    ),
                ):
                    await orchestrator._process_job(
                        job_id, asyncio.Semaphore(1)
                    )
                views = ManagementService(
                    self.database, self.root
                ).videos()["jobs"]
                job_view = next(item for item in views if item["job_id"] == job_id)
                self.assertEqual(job_view["state"], "PROVIDER_ACTION_REQUIRED")
                self.assertEqual(job_view["failure_code"], policy_error.stable_code)
                self.assertFalse(job_view["resumable"])
                service = ManagementService(self.database, self.root)
                with patch.object(
                    service, "dispatch_production_queue"
                ) as dispatch:
                    with self.assertRaisesRegex(
                        ValueError, "不可恢复|failure code"
                    ):
                        service.resume_failed_video(job_id)
                dispatch.assert_not_called()
                self.assertEqual(policy_submit.await_count, 1)

    async def test_unsafe_image_download_is_task_first_and_never_reposts(self):
        self.insert_job("J-IMAGE-UNSAFE-DOWNLOAD")
        counts = {"post": 0, "get": 0}
        real_async_client = httpx.AsyncClient

        def handler(request):
            if request.method == "POST":
                counts["post"] += 1
                return httpx.Response(200, json={
                    "data": [{"url": "https://127.0.0.1/private.png"}],
                })
            counts["get"] += 1
            return httpx.Response(200, content=b"must-not-download")

        def client_factory(*_args, **kwargs):
            return real_async_client(
                transport=httpx.MockTransport(handler),
                timeout=kwargs.get("timeout"),
                follow_redirects=kwargs.get("follow_redirects", False),
                headers=kwargs.get("headers"),
                trust_env=kwargs.get("trust_env", True),
            )

        with (
            patch.dict(os.environ, {"ARK_API_KEY": "provider-secret"}),
            patch.object(
                seedance.httpx, "AsyncClient", side_effect=client_factory
            ),
        ):
            with self.assertRaises(seedance.SeedanceSubmissionUnknownError):
                await self.submit_image("J-IMAGE-UNSAFE-DOWNLOAD")
            with self.assertRaises(provider_tasks.ProviderTaskGateError):
                await self.submit_image("J-IMAGE-UNSAFE-DOWNLOAD")

        self.assertEqual(counts, {"post": 1, "get": 0})
        rows = self.database.execute(
            "SELECT task_id,usage_json FROM provider_calls WHERE job_id=?",
            ("J-IMAGE-UNSAFE-DOWNLOAD",),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["task_id"].startswith("local-submit:"))
        self.assertEqual(
            json.loads(rows[0]["usage_json"])["status"], "action_required"
        )


class ProviderImageDownloadSafetyTests(unittest.IsolatedAsyncioTestCase):
    OPERATIONS = ("seedance_t2i", "seedream_i2i", "seedream_t2i")

    async def _invoke(self, operation: str) -> bytes:
        if operation == "seedance_t2i":
            return await seedance.text_to_image(
                "fictional equipment yard", model="seedance-image-test"
            )
        if operation == "seedream_i2i":
            return await seedream_45.i2i(
                "fictional equipment yard",
                ["data:image/png;base64,aW1hZ2U="],
                model="seedream-image-test",
            )
        if operation == "seedream_t2i":
            return await seedream_45.t2i("fictional equipment yard")
        raise AssertionError(f"unknown image operation: {operation}")

    @staticmethod
    def _client_factory(handler):
        real_async_client = httpx.AsyncClient

        def factory(*_args, **kwargs):
            return real_async_client(
                transport=httpx.MockTransport(handler),
                timeout=kwargs.get("timeout"),
                follow_redirects=kwargs.get("follow_redirects", False),
                headers=kwargs.get("headers"),
                trust_env=kwargs.get("trust_env", True),
            )

        return factory

    async def test_all_image_result_downloads_are_credential_free(self):
        for operation in self.OPERATIONS:
            with self.subTest(operation=operation):
                requests = []

                def handler(request):
                    requests.append(request)
                    if request.method == "POST":
                        return httpx.Response(200, json={
                            "data": [{
                                "url": "https://attacker.example/image.png",
                            }],
                        })
                    return httpx.Response(200, content=b"safe-image")

                with (
                    patch.dict(
                        os.environ, {"ARK_API_KEY": "provider-secret"}
                    ),
                    patch.object(
                        httpx, "AsyncClient",
                        side_effect=self._client_factory(handler),
                    ),
                    patch.object(
                        provider_downloads.socket, "getaddrinfo",
                        return_value=[
                            (2, 1, 6, "", ("93.184.216.34", 443)),
                        ],
                    ),
                ):
                    image = await self._invoke(operation)

                self.assertEqual(image, b"safe-image")
                self.assertEqual(
                    [request.method for request in requests], ["POST", "GET"]
                )
                self.assertIsNone(requests[1].headers.get("authorization"))
                self.assertEqual(requests[1].headers.get("host"), "attacker.example")

    async def test_all_image_result_downloads_reject_unsafe_urls_before_get(self):
        unsafe_cases = (
            ("http", "http://public.example/image.png", "93.184.216.34"),
            ("private", "https://127.0.0.1/image.png", "93.184.216.34"),
            ("localhost", "https://localhost/image.png", "93.184.216.34"),
            ("dns_rebinding", "https://rebind.example/image.png", "127.0.0.1"),
        )
        for operation in self.OPERATIONS:
            for label, unsafe_url, resolved_ip in unsafe_cases:
                with self.subTest(operation=operation, unsafe=label):
                    counts = {"post": 0, "get": 0}

                    def handler(request):
                        if request.method == "POST":
                            counts["post"] += 1
                            return httpx.Response(200, json={
                                "data": [{"url": unsafe_url}],
                            })
                        counts["get"] += 1
                        return httpx.Response(
                            200, content=b"must-not-download"
                        )

                    with (
                        patch.dict(
                            os.environ, {"ARK_API_KEY": "provider-secret"}
                        ),
                        patch.object(
                            httpx, "AsyncClient",
                            side_effect=self._client_factory(handler),
                        ),
                        patch.object(
                            provider_downloads.socket, "getaddrinfo",
                            return_value=[
                                (2, 1, 6, "", (resolved_ip, 443)),
                            ],
                        ),
                    ):
                        with self.assertRaises((
                            ValueError,
                            seedance.SeedanceSubmissionUnknownError,
                        )):
                            await self._invoke(operation)

                    self.assertEqual(counts, {"post": 1, "get": 0})


class ProviderHealthModuleTests(unittest.TestCase):
    def test_provider_health_module_exists(self):
        self.assertIsNotNone(provider_health)


class ProviderDownloadPinningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Mock TLS streams deliberately yield several times; keep the release
        # gate free of asyncio's debug-only 100 ms slow-callback diagnostics.
        asyncio.get_running_loop().slow_callback_duration = 1.0

    @staticmethod
    def _client_factory_for_backend(backend):
        real_async_client = httpx.AsyncClient

        def factory(*_args, **kwargs):
            transport = httpx.AsyncHTTPTransport()
            transport._pool = httpcore.AsyncConnectionPool(
                network_backend=backend
            )
            return real_async_client(
                transport=transport,
                timeout=kwargs.get("timeout"),
                follow_redirects=kwargs.get("follow_redirects", False),
                headers=kwargs.get("headers"),
                trust_env=kwargs.get("trust_env", True),
            )

        return factory

    async def test_redirect_to_new_hostname_uses_new_pool_and_sni(self):
        redirect = (
            b"HTTP/1.1 302 Found\r\n"
            b"location: https://b.example/final\r\n"
            b"content-length: 0\r\n\r\n"
        )
        completed = (
            b"HTTP/1.1 200 OK\r\ncontent-length: 5\r\n\r\nmedia"
        )

        class RecordingStream(httpcore.AsyncMockStream):
            def __init__(self, buffer, recorder):
                super().__init__(buffer)
                self.recorder = recorder

            async def start_tls(
                self, ssl_context, server_hostname=None, timeout=None
            ):
                del ssl_context, timeout
                self.recorder.append(server_hostname)
                return self

        class RecordingBackend(httpcore.AsyncMockBackend):
            def __init__(self):
                super().__init__([])
                self.connects = []
                self.sni = []

            async def connect_tcp(
                self, host, port, timeout=None, local_address=None,
                socket_options=None,
            ):
                del timeout, local_address, socket_options
                self.connects.append((host, port))
                buffer = (
                    [redirect, completed]
                    if len(self.connects) == 1 else [completed]
                )
                return RecordingStream(buffer, self.sni)

        backend = RecordingBackend()
        with patch(
            "socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("8.8.8.8", 443))],
        ) as resolve, patch.object(
            provider_downloads.httpx,
            "AsyncClient",
            side_effect=self._client_factory_for_backend(backend),
        ):
            body, final_url = await provider_downloads.download_bytes(
                "https://a.example/start"
            )

        self.assertEqual(body, b"media")
        self.assertEqual(final_url, "https://b.example/final")
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(
            backend.connects, [("8.8.8.8", 443), ("8.8.8.8", 443)]
        )
        self.assertEqual(backend.sni, ["a.example", "b.example"])

    async def test_dual_stack_connect_failure_fails_over_to_next_ip(self):
        completed = (
            b"HTTP/1.1 200 OK\r\ncontent-length: 5\r\n\r\nmedia"
        )

        class FailoverBackend(httpcore.AsyncMockBackend):
            def __init__(self):
                super().__init__([])
                self.connects = []

            async def connect_tcp(
                self, host, port, timeout=None, local_address=None,
                socket_options=None,
            ):
                del timeout, local_address, socket_options
                self.connects.append((host, port))
                if ":" in host:
                    raise httpcore.ConnectError("ipv6 unavailable")
                return httpcore.AsyncMockStream([completed])

        backend = FailoverBackend()
        dns_records = [
            (10, 1, 6, "", ("2001:4860:4860::8888", 443, 0, 0)),
            (2, 1, 6, "", ("8.8.8.8", 443)),
        ]
        with patch(
            "socket.getaddrinfo", return_value=dns_records
        ) as resolve, patch.object(
            provider_downloads.httpx,
            "AsyncClient",
            side_effect=self._client_factory_for_backend(backend),
        ):
            body, _ = await provider_downloads.download_bytes(
                "https://dual.example/media"
            )

        self.assertEqual(body, b"media")
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(backend.connects, [
            ("2001:4860:4860::8888", 443), ("8.8.8.8", 443),
        ])

    async def test_http_failure_does_not_blindly_try_next_ip(self):
        attempted_hosts = []
        real_async_client = httpx.AsyncClient

        def handler(request):
            attempted_hosts.append(request.url.host)
            return httpx.Response(503, text="unavailable")

        def client_factory(*_args, **kwargs):
            return real_async_client(
                transport=httpx.MockTransport(handler),
                headers=kwargs.get("headers"),
            )

        with patch(
            "socket.getaddrinfo",
            return_value=[
                (10, 1, 6, "", ("2001:4860:4860::8888", 443, 0, 0)),
                (2, 1, 6, "", ("8.8.8.8", 443)),
            ],
        ), patch.object(
            provider_downloads.httpx,
            "AsyncClient",
            side_effect=client_factory,
        ):
            with self.assertRaises(httpx.HTTPStatusError):
                await provider_downloads.download_bytes(
                    "https://dual.example/media"
                )

        self.assertEqual(attempted_hosts, ["2001:4860:4860::8888"])

    async def test_public_literal_ip_is_allowed_and_environment_proxy_disabled(self):
        observed = {}
        real_async_client = httpx.AsyncClient

        def handler(request):
            observed["url"] = str(request.url)
            return httpx.Response(200, content=b"media")

        def client_factory(*_args, **kwargs):
            observed["trust_env"] = kwargs.get("trust_env")
            return real_async_client(
                transport=httpx.MockTransport(handler),
                headers=kwargs.get("headers"),
            )

        with patch.object(
            provider_downloads.httpx,
            "AsyncClient",
            side_effect=client_factory,
        ):
            body, _ = await provider_downloads.download_bytes(
                "https://8.8.8.8/media.mp4"
            )

        self.assertEqual(body, b"media")
        self.assertEqual(observed["url"], "https://8.8.8.8/media.mp4")
        self.assertIs(observed["trust_env"], False)

    async def test_dns_validation_pins_connection_ip_against_rebinding(self):
        observed = {}
        real_async_client = httpx.AsyncClient

        def handler(request):
            observed.update({
                "url_host": request.url.host,
                "host_header": request.headers.get("host"),
                "sni_hostname": request.extensions.get("sni_hostname"),
            })
            return httpx.Response(200, content=b"pinned")

        def client_factory(*_args, **kwargs):
            return real_async_client(
                transport=httpx.MockTransport(handler),
                headers=kwargs.get("headers"),
            )

        dns_results = [
            [(2, 1, 6, "", ("8.8.8.8", 443))],
            [(2, 1, 6, "", ("127.0.0.1", 443))],
        ]
        with patch("socket.getaddrinfo", side_effect=dns_results) as resolve, \
                patch.object(
                    provider_downloads.httpx,
                    "AsyncClient",
                    side_effect=client_factory,
                ):
            body, final_url = await provider_downloads.download_bytes(
                "https://cdn.example/media.mp4"
            )

        self.assertEqual(body, b"pinned")
        self.assertEqual(final_url, "https://cdn.example/media.mp4")
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(observed["url_host"], "8.8.8.8")
        self.assertEqual(observed["host_header"], "cdn.example")
        self.assertEqual(observed["sni_hostname"], "cdn.example")

    async def test_trusted_host_policy_never_bypasses_private_ip_checks(self):
        with patch.dict(os.environ, {
            "PROVIDER_DOWNLOAD_TRUSTED_HOSTS": "127.0.0.1,trusted.example",
        }, clear=True):
            with self.assertRaisesRegex(ValueError, "public|安全"):
                await provider_downloads.download_bytes(
                    "https://127.0.0.1/private"
                )
            with patch(
                "socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("10.0.0.9", 443))],
            ):
                with self.assertRaisesRegex(ValueError, "public|安全"):
                    await provider_downloads.download_bytes(
                        "https://trusted.example/private"
                    )


@unittest.skipIf(provider_health is None, "provider health not implemented")
class ProviderCircuitBreakerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = memory_database()

    def tearDown(self):
        self.database.close()

    def test_account_overdue_opens_persistent_circuit_without_secrets(self):
        provider_health.block(
            self.database,
            "seedance",
            "account_unavailable",
            "Account overdue; Authorization: Bearer secret-token; "
            'request={"content":[{"text":"private prompt"}]}',
        )

        status = provider_health.get_status(self.database, "seedance")

        self.assertEqual(status["status"], "action_required")
        self.assertEqual(status["reason_code"], "account_unavailable")
        self.assertIsNotNone(status["blocked_at"])
        self.assertNotIn("secret-token", status["reason_message"])
        self.assertNotIn("private prompt", status["reason_message"])
        self.assertNotIn("Authorization", status["reason_message"])

    def test_structured_provider_messages_keep_only_safe_account_context(self):
        messages = (
            json.dumps({
                "error": {
                    "code": "AccountOverdue",
                    "message": "balance issue",
                    "request": {
                        "headers": {"Authorization": "Bearer json-secret"},
                        "body": {"prompt": "private json prompt"},
                    },
                }
            }),
            str({
                "error": {
                    "code": "InvalidAccessKey",
                    "api_key": "dict-secret",
                    "body": {"content": "private dict content"},
                }
            }),
        )

        for message in messages:
            with self.subTest(message=message[:20]):
                status = provider_health.block(
                    self.database, "seedance", "account_unavailable", message
                )
                stored = status["reason_message"]
                for secret in (
                    "json-secret", "private json prompt", "dict-secret",
                    "private dict content", "Authorization", "api_key",
                    "request", "body", "prompt", "content",
                ):
                    self.assertNotIn(secret, stored)
                self.assertIn("Seedance 账号需要人工处理", stored)

    def test_unstructured_provider_message_drops_unknown_raw_text(self):
        status = provider_health.block(
            self.database,
            "seedance",
            "account_unavailable",
            "AccountDisabled unexpected raw diagnostic with customer prompt",
        )

        stored = status["reason_message"]
        self.assertIn("AccountDisabled", stored)
        self.assertNotIn("unexpected raw diagnostic", stored)
        self.assertNotIn("customer prompt", stored)

    def test_provider_reason_code_cannot_persist_credentials(self):
        status = provider_health.block(
            self.database,
            "seedance",
            "AccountOverdue Authorization=Bearer code-secret",
            "account unavailable",
        )

        self.assertEqual(status["reason_code"], "account_unavailable")
        self.assertNotIn("code-secret", status["reason_message"])

    def test_repeated_clear_preserves_first_cleared_timestamp(self):
        provider_health.clear(self.database, "seedance")
        self.database.execute(
            "UPDATE provider_runtime SET cleared_at='2000-01-02 03:04:05' "
            "WHERE provider='seedance'"
        )
        self.database.commit()

        first = provider_health.get_status(self.database, "seedance")
        second = provider_health.clear(self.database, "seedance")

        self.assertEqual(first, second)

    def test_local_circuit_error_stays_inside_six_code_contract(self):
        provider_health.block(
            self.database,
            "seedance",
            "AccountDisabled",
            "provider account disabled",
        )

        with self.assertRaises(
            seedance.SeedanceAccountUnavailableError
        ) as raised:
            provider_health.assert_available(self.database, "seedance")

        self.assertEqual(raised.exception.code, "account_unavailable")
        self.assertEqual(raised.exception.stable_code, "account_unavailable")

    async def test_open_circuit_blocks_next_submit_before_provider_call(self):
        provider_health.block(
            self.database,
            "seedance",
            "account_unavailable",
            "Seedance account requires action",
        )
        generate = AsyncMock(return_value=b"video")

        with patch.object(s3_shots.seedance, "text_to_video", new=generate):
            with self.assertRaises(seedance.SeedanceAccountUnavailableError):
                await self._generate(database=self.database)

        generate.assert_not_awaited()

    async def test_clear_allows_submit_again(self):
        provider_health.block(
            self.database,
            "seedance",
            "account_unavailable",
            "Seedance account requires action",
        )
        provider_health.clear(self.database, "seedance")
        generate = AsyncMock(return_value=b"video")

        with (
            patch.object(s3_shots.seedance, "text_to_video", new=generate),
            patch.object(s3_shots.tos, "upload", return_value="https://asset/video"),
        ):
            result = await self._generate(database=self.database)

        self.assertEqual(result.video, b"video")
        generate.assert_awaited_once()
        self.assertEqual(
            provider_health.get_status(self.database, "seedance")["status"],
            "available",
        )

    async def test_observed_account_error_opens_circuit(self):
        generate = AsyncMock(
            side_effect=seedance.SeedanceAccountUnavailableError("account overdue")
        )

        with patch.object(s3_shots.seedance, "text_to_video", new=generate):
            with self.assertRaises(seedance.SeedanceAccountUnavailableError):
                await self._generate(database=self.database)

        self.assertEqual(generate.await_count, 1)
        status = provider_health.get_status(self.database, "seedance")
        self.assertEqual(status["status"], "action_required")
        self.assertEqual(status["reason_code"], "account_unavailable")

    async def test_non_overdue_account_error_also_opens_circuit(self):
        response = Mock(
            status_code=403,
            text=(
                '{"error":{"code":"AccountDisabled","message":'
                '"account has been disabled"}}'
            ),
        )
        account_error = seedance._submission_error(
            response,
            endpoint="https://ark.example/tasks",
            model="seedance-test",
            content_roles=[],
        )
        generate = AsyncMock(side_effect=account_error)

        with patch.object(s3_shots.seedance, "text_to_video", new=generate):
            with self.assertRaises(Exception) as raised:
                await self._generate(database=self.database)

        self.assertIsInstance(
            raised.exception, seedance.SeedanceAccountUnavailableError
        )
        self.assertEqual(
            provider_health.get_status(self.database, "seedance")["status"],
            "action_required",
        )

    async def _generate(self, *, database):
        route = VideoRoute(
            model="seedance-test",
            tier="seedance25",
            generate_audio=False,
            qa_required=False,
            fallback_model=None,
        )
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return await s3_shots._generate_seedance_video(
            prompt="safe prompt",
            route=route,
            request_kwargs={"duration": 4, "resolution": "480p"},
            out_path=Path(temp.name) / "shot.mp4",
            key_hint="failure-matrix",
            database=database,
        )


class SeedanceFailureClassificationTests(unittest.TestCase):
    def test_401_invalid_access_key_is_account_unavailable(self):
        response = Mock(
            status_code=401,
            text=(
                '{"error":{"code":"InvalidAccessKey","message":'
                '"access key is invalid"}}'
            ),
        )

        error = seedance._submission_error(
            response,
            endpoint="https://ark.example/tasks",
            model="seedance-test",
            content_roles=[],
        )

        self.assertIsInstance(error, seedance.SeedanceAccountUnavailableError)
        self.assertEqual(error.code, "account_unavailable")

    def test_403_account_disabled_is_account_unavailable(self):
        response = Mock(
            status_code=403,
            text=(
                '{"error":{"code":"AccountDisabled","message":'
                '"account has been disabled"}}'
            ),
        )

        error = seedance._submission_error(
            response,
            endpoint="https://ark.example/tasks",
            model="seedance-test",
            content_roles=[],
        )

        self.assertIsInstance(error, seedance.SeedanceAccountUnavailableError)
        self.assertEqual(error.code, "account_unavailable")

    def test_poll_account_suspended_is_account_unavailable(self):
        error = seedance._task_failure_error(
            "task-account",
            {
                "status": "failed",
                "error": {
                    "code": "AccountSuspended",
                    "message": "provider account is suspended",
                },
            },
        )

        self.assertIsInstance(error, seedance.SeedanceAccountUnavailableError)
        self.assertEqual(error.stable_code, "account_unavailable")

    def test_explicit_request_error_precedes_403_account_fallback(self):
        response = Mock(
            status_code=403,
            text=(
                '{"error":{"code":"InvalidParameter","message":'
                '"duration is invalid"}}'
            ),
        )

        error = seedance._submission_error(
            response,
            endpoint="https://ark.example/tasks",
            model="seedance-test",
            content_roles=[],
        )

        self.assertIsInstance(error, seedance.SeedanceParameterError)
        self.assertEqual(error.code, "request_invalid")

    def test_async_output_policy_rejection_is_non_retryable_and_does_not_block(self):
        database = memory_database()
        try:
            error = seedance._task_failure_error(
                "task-policy",
                {
                    "status": "failed",
                    "error": {
                        "code": "OutputVideoSensitiveContentDetected.PolicyViolation",
                        "message": "policy rejected output",
                    },
                },
            )
            self.assertIsInstance(error, seedance.SeedanceOutputPolicyError)
            self.assertEqual(error.code, "output_policy_rejected")
            self.assertTrue(error.non_retryable)
            self.assertEqual(
                provider_health.get_status(database, "seedance")["status"],
                "available",
            )
        finally:
            database.close()

    def test_explicit_policy_violation_code_is_output_policy_rejection(self):
        error = seedance._task_failure_error(
            "task-policy",
            {
                "status": "failed",
                "error": {
                    "code": "PolicyViolation",
                    "message": "generated output was rejected",
                },
            },
        )

        self.assertIsInstance(error, seedance.SeedanceOutputPolicyError)
        self.assertEqual(error.stable_code, "output_policy_rejected")
        self.assertTrue(error.non_retryable)

    def test_copyright_rejection_has_stable_code(self):
        error = seedance._task_failure_error(
            "task-copyright",
            {
                "status": "failed",
                "error": {
                    "code": "OutputVideoSensitiveContentDetected.CopyrightViolation",
                    "message": "copyright restriction",
                },
            },
        )

        self.assertIsInstance(error, seedance.SeedanceOutputPolicyError)
        self.assertEqual(error.code, "output_copyright_rejected")
        self.assertTrue(error.non_retryable)

    def test_copyright_code_without_sensitive_prefix_is_still_non_retryable(self):
        error = seedance._task_failure_error(
            "task-copyright",
            {
                "status": "failed",
                "error": {
                    "code": "CopyrightViolation",
                    "message": "generated output matched protected content",
                },
            },
        )

        self.assertIsInstance(error, seedance.SeedanceOutputPolicyError)
        self.assertEqual(error.code, "output_copyright_rejected")
        self.assertTrue(error.non_retryable)

    def test_transient_network_retry_depends_on_submit_certainty(self):
        connect = seedance._submission_transport_error(
            seedance.httpx.ConnectError("connection refused")
        )
        ambiguous_transport = seedance._submission_transport_error(
            seedance.httpx.RemoteProtocolError("server disconnected")
        )
        response = Mock(status_code=503, text="temporary upstream failure")
        service_unavailable = seedance._submission_error(
            response,
            endpoint="https://ark.example/tasks",
            model="seedance-test",
            content_roles=[],
        )

        self.assertTrue(connect.retryable)
        self.assertFalse(connect.non_retryable)
        self.assertTrue(orchestrator._should_retry_step(connect))
        for error in (ambiguous_transport, service_unavailable):
            self.assertEqual(error.code, "transient_network")
            self.assertFalse(error.retryable)
            self.assertTrue(error.non_retryable)
            self.assertFalse(orchestrator._should_retry_step(error))

    def test_every_terminal_classification_uses_six_code_contract(self):
        allowed = {
            "account_unavailable",
            "request_invalid",
            "input_privacy_rejected",
            "output_policy_rejected",
            "output_copyright_rejected",
            "transient_network",
        }
        scenarios = (
            ("AccountOverdue", "overdue"),
            ("InvalidParameter", "bad duration"),
            ("InputImagesSensitiveContentDetected", "privacy"),
            ("OutputVideoSensitiveContentDetected", "policy"),
            ("CopyrightViolation", "copyright"),
            ("RemoteProtocolError", "network"),
            ("NewUnknownTerminalCode", "provider diagnostic detail"),
        )

        for provider_code, detail in scenarios:
            with self.subTest(provider_code=provider_code):
                error = seedance._task_failure_error(
                    "task-matrix",
                    {"error": {"code": provider_code, "message": detail}},
                )
                self.assertIn(getattr(error, "stable_code", None), allowed)
                self.assertEqual(error.code, error.stable_code)
        unknown = seedance._task_failure_error(
            "task-unknown",
            {
                "error": {
                    "code": "NewUnknownTerminalCode",
                    "message": "provider diagnostic detail",
                }
            },
        )
        self.assertEqual(unknown.code, "request_invalid")
        self.assertTrue(unknown.non_retryable)
        self.assertEqual(
            getattr(unknown, "provider_code", None), "NewUnknownTerminalCode"
        )
        self.assertEqual(
            getattr(unknown, "provider_detail", None),
            "未知 Provider 终止错误，原始详情已省略",
        )
        self.assertIn("NewUnknownTerminalCode", str(unknown))
        self.assertNotIn("provider diagnostic detail", str(unknown))

    def test_opaque_terminal_failure_payload_fails_closed(self):
        try:
            error = seedance._task_failure_error(
                "task-opaque",
                {"status": "failed", "error": "opaque provider failure"},
            )
        except Exception as exc:
            self.fail(f"opaque terminal error was not classified: {exc}")

        self.assertIsInstance(error, seedance.SeedanceParameterError)
        self.assertEqual(error.stable_code, "request_invalid")
        self.assertTrue(error.non_retryable)
        self.assertEqual(
            error.provider_detail,
            "未知 Provider 终止错误，原始详情已省略",
        )
        self.assertNotIn("opaque provider failure", str(error))

    def test_unknown_terminal_technical_detail_is_sanitized(self):
        error = seedance._provider_error(
            provider_code="UnknownTerminal",
            provider_message=(
                "Authorization: Bearer secret-token; "
                'request={"prompt":"private prompt"}'
            ),
            task_id="task-secret",
        )

        self.assertNotIn("secret-token", error.provider_detail)
        self.assertNotIn("private prompt", error.provider_detail)
        self.assertNotIn("secret-token", str(error))
        self.assertNotIn("private prompt", str(error))


class SeedanceHttpResponseParsingTests(unittest.IsolatedAsyncioTestCase):
    def assert_request_invalid(self, error: Exception, detail: str) -> None:
        self.assertIsInstance(error, seedance.SeedanceParameterError)
        self.assertEqual(error.stable_code, "request_invalid")
        self.assertTrue(error.non_retryable)
        self.assertFalse(orchestrator._should_retry_step(error))
        self.assertIn(detail, getattr(error, "provider_detail", ""))
        self.assertLessEqual(len(error.provider_detail), 200)

    def assert_submit_ambiguous(self, error: Exception) -> None:
        self.assertIsInstance(error, seedance.SeedanceSubmissionUnknownError)
        self.assertEqual(error.stable_code, "transient_network")
        self.assertTrue(error.non_retryable)
        self.assertEqual(error.pause_state, "PROVIDER_ACTION_REQUIRED")
        self.assertFalse(orchestrator._should_retry_step(error))
        self.assertFalse(hasattr(error, "task_id"))

    async def test_submit_malformed_json_fails_closed_without_raw_body(self):
        error = await self._call_text_to_video(lambda _request: httpx.Response(
            200,
            text=(
                "not-json Authorization: Bearer provider-secret "
                'request={"prompt":"private prompt"}'
            ),
        ))

        self.assert_submit_ambiguous(error)
        self.assertNotIn("provider-secret", str(error))
        self.assertNotIn("private prompt", str(error))

    async def test_submit_non_object_json_fails_closed(self):
        error = await self._call_text_to_video(
            lambda _request: httpx.Response(200, json=[{"id": "task"}])
        )

        self.assert_submit_ambiguous(error)

    async def test_submit_missing_or_empty_task_id_fails_closed(self):
        for payload in ({}, {"id": ""}):
            with self.subTest(payload=payload):
                error = await self._call_text_to_video(
                    lambda _request, body=payload: httpx.Response(200, json=body)
                )
                self.assert_submit_ambiguous(error)

    async def test_poll_non_object_json_fails_closed(self):
        responses = iter((
            httpx.Response(200, json={"id": "task-poll"}),
            httpx.Response(200, json=[{"status": "succeeded"}]),
        ))
        error = await self._call_text_to_video(lambda _request: next(responses))

        self.assert_request_invalid(error, "poll response must be an object")
        self.assertEqual(error.task_id, "task-poll")
        self.assertEqual(error.pause_state, "PROVIDER_ACTION_REQUIRED")

    async def test_poll_malformed_json_fails_closed_without_raw_body(self):
        responses = iter((
            httpx.Response(200, json={"id": "task-poll"}),
            httpx.Response(200, text="not-json Bearer provider-secret"),
        ))
        error = await self._call_text_to_video(lambda _request: next(responses))

        self.assert_request_invalid(error, "poll response")
        self.assertEqual(error.task_id, "task-poll")
        self.assertEqual(error.pause_state, "PROVIDER_ACTION_REQUIRED")
        self.assertNotIn("provider-secret", str(error))

    async def test_succeeded_result_missing_or_empty_video_url_fails_closed(self):
        cases = (
            ("succeeded", {}),
            ("succeeded", {"video_url": ""}),
            ("completed", {}),
        )
        for status, content in cases:
            with self.subTest(status=status, content=content):
                responses = iter((
                    httpx.Response(200, json={"id": "task-complete"}),
                    httpx.Response(
                        200,
                        json={"status": status, "content": content},
                    ),
                ))
                error = await self._call_text_to_video(
                    lambda _request, items=responses: next(items)
                )
                self.assert_request_invalid(error, "missing video_url")
                self.assertEqual(error.task_id, "task-complete")
                self.assertEqual(error.pause_state, "PROVIDER_ACTION_REQUIRED")

    async def _call_text_to_video(self, handler) -> Exception:
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        with (
            patch.dict("os.environ", {"ARK_API_KEY": "test-key"}),
            patch.object(seedance.httpx, "AsyncClient", return_value=client),
            patch.object(seedance.asyncio, "sleep", new=AsyncMock()),
        ):
            try:
                await seedance.text_to_video(
                    "safe equipment scene",
                    duration=4,
                    resolution="480p",
                    timeout=5,
                )
            except Exception as exc:
                return exc
        self.fail("malformed provider response unexpectedly succeeded")

    async def test_seedance_download_clients_never_forward_api_authorization(self):
        for operation in ("generate", "inspect"):
            with self.subTest(operation=operation):
                seen_download_headers = []

                def handler(request):
                    if request.headers.get("host") != "evil.example":
                        if request.method == "POST":
                            return httpx.Response(200, json={"id": "task-safe"})
                        return httpx.Response(200, json={
                            "status": "completed",
                            "content": {
                                "video_url": "https://evil.example/video.mp4"
                            },
                            "usage": {"completion_tokens": 1000},
                        })
                    seen_download_headers.append(dict(request.headers))
                    return httpx.Response(200, content=b"video")

                real_async_client = httpx.AsyncClient

                def client_factory(*_args, **kwargs):
                    return real_async_client(
                        transport=httpx.MockTransport(handler),
                        headers=kwargs.get("headers"),
                    )

                with patch.dict(os.environ, {
                    "ARK_API_KEY": "provider-secret",
                    "PROVIDER_DOWNLOAD_TRUSTED_HOSTS": "evil.example",
                }), patch(
                    "socket.getaddrinfo",
                    return_value=[(2, 1, 6, "", ("8.8.8.8", 443))],
                ), patch.object(
                    seedance.httpx, "AsyncClient", side_effect=client_factory
                ), patch.object(
                    seedance.asyncio, "sleep", new=AsyncMock()
                ):
                    if operation == "generate":
                        video = await seedance.text_to_video(
                            "equipment", duration=4, resolution="480p", timeout=5
                        )
                    else:
                        inspected = await seedance.inspect_task("task-safe")
                        video = inspected["video"]

                self.assertEqual(video, b"video")
                self.assertEqual(len(seen_download_headers), 1)
                lowered = {
                    str(key).casefold(): value
                    for key, value in seen_download_headers[0].items()
                }
                self.assertNotIn("authorization", lowered)

    async def test_seedance_rejects_unsafe_urls_before_generate_or_inspect_download(self):
        for operation in ("generate", "inspect"):
            for unsafe_url in (
                "http://public.example/video.mp4",
                "https://localhost/video.mp4",
                "https://127.0.0.1/video.mp4",
                "https://10.0.0.9/video.mp4",
                "https://169.254.2.3/video.mp4",
            ):
                with self.subTest(operation=operation, url=unsafe_url):
                    downloads = 0

                    def handler(request):
                        nonlocal downloads
                        if request.url.host not in {
                            "localhost", "127.0.0.1", "10.0.0.9",
                            "169.254.2.3", "public.example",
                        }:
                            if request.method == "POST":
                                return httpx.Response(
                                    200, json={"id": "task-unsafe"}
                                )
                            return httpx.Response(200, json={
                                "status": "completed",
                                "content": {"video_url": unsafe_url},
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
                        os.environ, {"ARK_API_KEY": "test-key"}, clear=True
                    ), patch.object(
                        seedance.httpx, "AsyncClient", side_effect=client_factory
                    ), patch.object(
                        seedance.asyncio, "sleep", new=AsyncMock()
                    ):
                        with self.assertRaisesRegex(
                            ValueError, "HTTPS|public|安全"
                        ):
                            if operation == "generate":
                                await seedance.text_to_video(
                                    "equipment", duration=4,
                                    resolution="480p", timeout=5,
                                )
                            else:
                                await seedance.inspect_task("task-unsafe")
                    self.assertEqual(downloads, 0)

    def test_submit_and_poll_account_errors_share_classification(self):
        response = Mock(
            status_code=403,
            text='{"error":{"code":"AccountOverdue","message":"overdue"}}',
        )
        submit_error = seedance._submission_error(
            response,
            endpoint="https://ark.example/tasks",
            model="seedance-test",
            content_roles=[],
        )
        poll_error = seedance._task_failure_error(
            "task-account",
            {"error": {"code": "AccountOverdue", "message": "overdue"}},
        )

        for error in (submit_error, poll_error):
            self.assertIsInstance(error, seedance.SeedanceAccountUnavailableError)
            self.assertEqual(error.code, "account_unavailable")
            self.assertEqual(error.pause_state, "PROVIDER_ACTION_REQUIRED")
            self.assertTrue(error.non_retryable)


class SeedancePaidCallAmbiguityTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_transient_failure_never_reposts_known_task(self):
        counts = {"post": 0, "poll": 0}

        def handler(provider_request):
            if provider_request.method == "POST":
                counts["post"] += 1
                return httpx.Response(200, json={"id": "task-terminal-503"})
            counts["poll"] += 1
            return httpx.Response(200, json={
                "status": "failed",
                "error": {
                    "code": "InternalServerError",
                    "message": "service unavailable",
                },
            })

        error = await self._call_text_to_video(handler)

        self.assertEqual(counts, {"post": 1, "poll": 1})
        self.assertEqual(error.stable_code, "transient_network")
        self.assertTrue(error.non_retryable)
        self.assertEqual(error.pause_state, "PROVIDER_ACTION_REQUIRED")
        self.assertEqual(error.task_id, "task-terminal-503")
        self.assertFalse(orchestrator._should_retry_step(error))
        self.assertIn("核对", str(error))
        self.assertNotIn("可按重试策略", str(error))

    async def test_terminal_failure_updates_pending_call_and_cost_once(self):
        database = memory_database()
        counts = {"post": 0}

        def handler(provider_request):
            if provider_request.method == "POST":
                counts["post"] += 1
                return httpx.Response(200, json={"id": "task-failed-billing"})
            return httpx.Response(200, json={
                "status": "failed",
                "error": {"code": "InternalServerError", "message": "timeout"},
                "usage": {"completion_tokens": 11000},
            })

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        route = VideoRoute(
            model="seedance-test", tier="seedance25", generate_audio=False,
            qa_required=False, fallback_model=None,
        )
        caught = None
        with (
            patch.dict(os.environ, {"ARK_API_KEY": "test-key"}),
            patch.object(seedance.httpx, "AsyncClient", return_value=client),
            patch.object(seedance.asyncio, "sleep", new=AsyncMock()),
        ):
            try:
                await s3_shots._generate_seedance_video(
                    prompt="safe equipment scene",
                    route=route,
                    request_kwargs={
                        "duration": 4, "resolution": "480p", "timeout": 10,
                    },
                    out_path=Path(tempfile.gettempdir()) / "unused-failed.mp4",
                    key_hint="failed-task",
                    job_id="J-FAILED-TASK",
                    shot_idx=8,
                    database=database,
                )
            except Exception as exc:
                caught = exc

        self.assertEqual(counts["post"], 1)
        self.assertEqual(caught.task_id, "task-failed-billing")
        self.assertFalse(orchestrator._should_retry_step(caught))
        row = database.execute(
            "SELECT cost_cents,usage_json FROM provider_calls "
            "WHERE task_id='task-failed-billing'"
        ).fetchone()
        usage = json.loads(row["usage_json"])
        self.assertEqual(row["cost_cents"], 77)
        self.assertEqual(usage["status"], "failed")
        self.assertEqual(usage["completion_tokens"], 11000)
        database.close()

    async def test_submit_remote_protocol_error_is_ambiguous_and_posts_once(self):
        counts = {"post": 0}

        def handler(provider_request):
            if provider_request.method == "POST":
                counts["post"] += 1
            raise httpx.RemoteProtocolError(
                "server disconnected after request", request=provider_request
            )

        error = await self._call_text_to_video(handler)

        self.assertEqual(counts["post"], 1)
        self.assertEqual(error.stable_code, "transient_network")
        self.assertTrue(error.non_retryable)
        self.assertFalse(orchestrator._should_retry_step(error))
        self.assertEqual(error.pause_state, "PROVIDER_ACTION_REQUIRED")
        self.assertIn("提交结果未知", str(error))

    async def test_submit_connect_error_remains_retryable(self):
        def handler(provider_request):
            raise httpx.ConnectError("connection refused", request=provider_request)

        error = await self._call_text_to_video(handler)

        self.assertEqual(error.stable_code, "transient_network")
        self.assertFalse(error.non_retryable)
        self.assertTrue(orchestrator._should_retry_step(error))

    async def test_submit_http_5xx_is_ambiguous_and_posts_once(self):
        counts = {"post": 0}

        def handler(provider_request):
            counts["post"] += int(provider_request.method == "POST")
            return httpx.Response(503, json={"error": {"code": "Busy"}})

        error = await self._call_text_to_video(handler)

        self.assertEqual(counts["post"], 1)
        self.assertEqual(error.stable_code, "transient_network")
        self.assertTrue(error.non_retryable)
        self.assertEqual(error.pause_state, "PROVIDER_ACTION_REQUIRED")

    async def test_poll_5xx_retries_same_task_then_succeeds_without_repost(self):
        counts = {"post": 0, "poll": 0, "download": 0}

        def handler(provider_request):
            if provider_request.method == "POST":
                counts["post"] += 1
                return httpx.Response(200, json={"id": "task-poll-retry"})
            if provider_request.url.path.endswith("task-poll-retry"):
                counts["poll"] += 1
                if counts["poll"] == 1:
                    return httpx.Response(503, json={"error": {"code": "Busy"}})
                return httpx.Response(200, json={
                    "status": "succeeded",
                    "content": {"video_url": "https://media.example/video.mp4"},
                })
            counts["download"] += 1
            return httpx.Response(200, content=b"video")

        result = await self._call_text_to_video(handler, timeout=15)

        self.assertEqual(result, b"video")
        self.assertEqual(counts, {"post": 1, "poll": 2, "download": 1})

    async def test_poll_exhaustion_keeps_one_post_and_persists_pending_task(self):
        database = memory_database()
        counts = {"post": 0, "poll": 0}

        def handler(provider_request):
            if provider_request.method == "POST":
                counts["post"] += 1
                return httpx.Response(200, json={"id": "task-pending-001"})
            counts["poll"] += 1
            raise httpx.ReadTimeout("poll timeout", request=provider_request)

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        route = VideoRoute(
            model="seedance-test", tier="seedance25", generate_audio=False,
            qa_required=False, fallback_model=None,
        )
        caught = None
        with (
            patch.dict(os.environ, {"ARK_API_KEY": "test-key"}),
            patch.object(seedance.httpx, "AsyncClient", return_value=client),
            patch.object(seedance.asyncio, "sleep", new=AsyncMock()),
        ):
            try:
                await s3_shots._generate_seedance_video(
                    prompt="safe equipment scene",
                    route=route,
                    request_kwargs={
                        "duration": 4, "resolution": "480p", "timeout": 10,
                    },
                    out_path=Path(tempfile.gettempdir()) / "unused-pending.mp4",
                    key_hint="pending-task",
                    job_id="J-PENDING",
                    shot_idx=7,
                    database=database,
                )
            except Exception as exc:
                caught = exc

        self.assertIsInstance(caught, seedance.SeedanceSubmissionUnknownError)
        self.assertEqual(counts, {"post": 1, "poll": 2})
        self.assertTrue(caught.non_retryable)
        self.assertEqual(caught.task_id, "task-pending-001")
        row = database.execute(
            "SELECT task_id,job_id,shot_idx,intent,cost_cents,usage_json "
            "FROM provider_calls WHERE task_id='task-pending-001'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual((row["job_id"], row["shot_idx"], row["cost_cents"]), (
            "J-PENDING", 7, 0,
        ))
        self.assertEqual(
            json.loads(row["usage_json"])["status"], "action_required"
        )
        database.close()

    async def test_download_timeout_pauses_same_task_without_repost(self):
        counts = {"post": 0}

        def handler(provider_request):
            if provider_request.method == "POST":
                counts["post"] += 1
                return httpx.Response(200, json={"id": "task-download"})
            if provider_request.url.path.endswith("task-download"):
                return httpx.Response(200, json={
                    "status": "succeeded",
                    "content": {"video_url": "https://media.example/video.mp4"},
                })
            raise httpx.ReadTimeout("download timeout", request=provider_request)

        error = await self._call_text_to_video(handler)

        self.assertEqual(counts["post"], 1)
        self.assertIsInstance(error, seedance.SeedanceSubmissionUnknownError)
        self.assertTrue(error.non_retryable)
        self.assertEqual(error.pause_state, "PROVIDER_ACTION_REQUIRED")
        self.assertEqual(error.task_id, "task-download")
        self.assertFalse(orchestrator._should_retry_step(error))

    async def _call_text_to_video(self, handler, *, timeout=10, catch=True):
        real_async_client = httpx.AsyncClient

        def client_factory(*_args, **kwargs):
            return real_async_client(
                transport=httpx.MockTransport(handler),
                headers=kwargs.get("headers"),
            )
        with (
            patch.dict(os.environ, {
                "ARK_API_KEY": "test-key",
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
        ):
            try:
                return await seedance.text_to_video(
                    "safe equipment scene", duration=4, resolution="480p",
                    timeout=timeout,
                )
            except Exception as exc:
                if catch:
                    return exc
                raise
        self.fail("provider call unexpectedly succeeded")


class SeedanceLocalValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_image_fallback_disables_visible_watermark(self):
        captured = {}

        def submit(request):
            captured.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={"data": [{"url": "https://cdn.example/identity.png"}]},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(submit))
        with (
            patch.dict(os.environ, {"ARK_API_KEY": "test-key"}),
            patch.object(seedance.httpx, "AsyncClient", return_value=client),
            patch.object(
                seedance.provider_downloads,
                "download_bytes",
                new=AsyncMock(return_value=(b"png", "https://cdn.example/identity.png")),
            ),
        ):
            result = await seedance.text_to_image("clean identity portrait")

        self.assertEqual(result, b"png")
        self.assertIn("watermark", captured)
        self.assertIs(captured["watermark"], False)

    def test_invalid_reference_role_uses_request_invalid(self):
        caught = None
        try:
            seedance._push_image([], "invalid_role", "https://example.test/a.png")
        except Exception as exc:
            caught = exc

        self.assertIsInstance(caught, seedance.SeedanceParameterError)
        self.assertEqual(caught.stable_code, "request_invalid")
        self.assertTrue(caught.non_retryable)

    async def test_missing_api_key_is_classified_as_account_unavailable(self):
        caught = None
        with patch.dict(os.environ, {}, clear=True):
            try:
                await seedance.text_to_video(
                    "safe prompt", duration=4, resolution="480p"
                )
            except Exception as exc:
                caught = exc

        self.assertIsInstance(caught, seedance.SeedanceAccountUnavailableError)
        self.assertEqual(caught.stable_code, "account_unavailable")
        self.assertTrue(caught.non_retryable)
        self.assertFalse(orchestrator._should_retry_step(caught))

    async def test_public_local_validation_failures_use_request_invalid(self):
        cases = (
            {"resolution": "4k"},
            {"output_format": "avi"},
            {"task_intent": "edit", "ratio": "9:16"},
            {"references": [f"https://example.test/{i}.png" for i in range(31)]},
            {"first_frame": "definitely-missing-reference.png"},
        )
        with patch.dict(os.environ, {"ARK_API_KEY": "test-key"}):
            for kwargs in cases:
                with self.subTest(kwargs=tuple(kwargs)):
                    caught = None
                    try:
                        await seedance.text_to_video(
                            "safe prompt", duration=4, **kwargs
                        )
                    except Exception as exc:
                        caught = exc
                    self.assertIsInstance(caught, seedance.SeedanceParameterError)
                    self.assertEqual(caught.stable_code, "request_invalid")
                    self.assertTrue(caught.non_retryable)
                    self.assertFalse(orchestrator._should_retry_step(caught))

    async def test_public_image_fallback_http_error_has_stable_classification(self):
        error = await self._call_text_to_image(
            lambda _provider_request: httpx.Response(
                400,
                json={"error": {
                    "code": "InvalidParameter",
                    "message": "bad request with private prompt",
                }},
            )
        )

        self.assertIsInstance(error, seedance.SeedanceParameterError)
        self.assertEqual(error.stable_code, "request_invalid")
        self.assertTrue(error.non_retryable)
        self.assertNotIn("private prompt", str(error))

    async def test_public_image_fallback_malformed_success_is_submit_unknown(self):
        error = await self._call_text_to_image(
            lambda _provider_request: httpx.Response(200, text="not-json")
        )

        self.assertIsInstance(error, seedance.SeedanceSubmissionUnknownError)
        self.assertEqual(error.stable_code, "transient_network")
        self.assertTrue(error.non_retryable)
        self.assertEqual(error.pause_state, "PROVIDER_ACTION_REQUIRED")

    async def test_public_image_fallback_missing_url_is_submit_unknown(self):
        error = await self._call_text_to_image(
            lambda _provider_request: httpx.Response(200, json={"data": []})
        )

        self.assertIsInstance(error, seedance.SeedanceSubmissionUnknownError)
        self.assertEqual(error.stable_code, "transient_network")
        self.assertTrue(error.non_retryable)
        self.assertEqual(error.pause_state, "PROVIDER_ACTION_REQUIRED")

    async def _call_text_to_image(self, handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with (
            patch.dict(os.environ, {"ARK_API_KEY": "test-key"}),
            patch.object(seedance.httpx, "AsyncClient", return_value=client),
        ):
            try:
                return await seedance.text_to_image("safe prompt")
            except Exception as exc:
                return exc
        self.fail("invalid image provider response unexpectedly succeeded")


class ProviderCallPendingBillingTests(unittest.TestCase):
    def test_upsert_final_call_cross_owner_race_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "provider-race.db")
            setup = sqlite3.connect(path, isolation_level=None)
            setup.executescript(db.DDL)
            setup.close()
            start = threading.Barrier(2)
            select_race = threading.Barrier(2)
            outcomes = []

            class CoordinatedConnection:
                def __init__(self):
                    self.raw = sqlite3.connect(
                        path, isolation_level=None, timeout=5,
                        check_same_thread=False,
                    )
                    self.saw_immediate = False

                @property
                def in_transaction(self):
                    return self.raw.in_transaction

                def execute(self, sql, parameters=()):
                    if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
                        self.saw_immediate = True
                    if (
                        "FROM provider_calls WHERE task_id=?" in sql
                        and not self.saw_immediate
                    ):
                        select_race.wait(timeout=5)
                    return self.raw.execute(sql, parameters)

                def commit(self):
                    return self.raw.commit()

                def rollback(self):
                    return self.raw.rollback()

                def close(self):
                    self.raw.close()

            def worker(owner: str, cost: int):
                database = CoordinatedConnection()
                try:
                    start.wait(timeout=5)
                    delta = provider_tasks.upsert_final_call(
                        database,
                        task_id="task-race-owner",
                        job_id=owner,
                        shot_idx=1,
                        provider="seedance",
                        intent="generate",
                        status="completed",
                        usage={"owner_marker": owner},
                        cost_cents=cost,
                    )
                    outcomes.append(("ok", owner, delta))
                except Exception as exc:
                    outcomes.append(("error", owner, exc))
                finally:
                    database.close()

            threads = [
                threading.Thread(target=worker, args=("J-RACE-A", 11)),
                threading.Thread(target=worker, args=("J-RACE-B", 22)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())

            successes = [item for item in outcomes if item[0] == "ok"]
            failures = [item for item in outcomes if item[0] == "error"]
            self.assertEqual(len(successes), 1, outcomes)
            self.assertEqual(len(failures), 1, outcomes)
            self.assertIsInstance(
                failures[0][2], provider_tasks.ProviderTaskGateError
            )
            winner = successes[0]
            self.assertEqual(winner[2], 11 if winner[1] == "J-RACE-A" else 22)
            verify = sqlite3.connect(path)
            verify.row_factory = sqlite3.Row
            row = verify.execute(
                "SELECT job_id,intent,cost_cents,usage_json "
                "FROM provider_calls WHERE task_id='task-race-owner'"
            ).fetchone()
            verify.close()
            self.assertEqual(row["job_id"], winner[1])
            self.assertEqual(row["intent"], "generate")
            self.assertEqual(
                row["cost_cents"], 11 if winner[1] == "J-RACE-A" else 22
            )
            self.assertEqual(
                json.loads(row["usage_json"])["owner_marker"], winner[1]
            )

    def test_promote_reservation_rejects_its_own_storage_id(self):
        database = memory_database()
        reservation_id = provider_tasks.reserve_paid_submit(
            database,
            job_id="J-SELF-PROMOTE",
            shot_idx=1,
            provider="seedance",
            intent="generate",
        )
        provider_tasks.persist_remote_task_id(
            database,
            reservation_id,
            remote_task_id=reservation_id,
            provider="seedance",
            intent="generate",
        )

        with self.assertRaises(provider_tasks.ProviderTaskGateError):
            provider_tasks.promote_reservation(
                database,
                reservation_id,
                task_id=reservation_id,
                job_id="J-SELF-PROMOTE",
                shot_idx=1,
                provider="seedance",
                intent="generate",
            )

        row = database.execute(
            "SELECT usage_json FROM provider_calls WHERE task_id=?",
            (reservation_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(
            json.loads(row["usage_json"])["remote_task_id"], reservation_id
        )
        database.close()

    def test_upsert_final_call_rejects_cross_owner_and_keeps_reservation(self):
        database = memory_database()
        reservation_id = provider_tasks.reserve_paid_submit(
            database,
            job_id="J-NEW",
            shot_idx=1,
            provider="seedream_identity",
            intent="character_identity_anchor",
        )
        database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES('shared-task','J-OLD',9,'seedream_scene',"
            "'scene_visual_anchor',?,11)",
            (json.dumps({"status": "completed"}),),
        )
        database.commit()

        with self.assertRaises(provider_tasks.ProviderTaskGateError):
            provider_tasks.upsert_final_call(
                database,
                task_id="shared-task",
                job_id="J-NEW",
                shot_idx=1,
                provider="seedream_identity",
                intent="character_identity_anchor",
                status="completed",
                cost_cents=29,
            )
        database.rollback()

        collision = database.execute(
            "SELECT job_id,shot_idx,provider,intent,cost_cents FROM provider_calls "
            "WHERE task_id='shared-task'"
        ).fetchone()
        self.assertEqual(tuple(collision), (
            "J-OLD", 9, "seedream_scene", "scene_visual_anchor", 11,
        ))
        self.assertIsNotNone(database.execute(
            "SELECT 1 FROM provider_calls WHERE task_id=?", (reservation_id,)
        ).fetchone())
        database.close()

    def test_upsert_final_call_is_idempotent_for_exact_owner(self):
        database = memory_database()
        database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES('same-task','J-SAME',1,'seedream_identity',"
            "'character_identity_anchor',?,0)",
            (json.dumps({"status": "pending"}),),
        )
        database.commit()
        kwargs = dict(
            task_id="same-task", job_id="J-SAME", shot_idx=1,
            provider="seedream_identity", intent="character_identity_anchor",
            status="completed", cost_cents=29,
        )

        first = provider_tasks.upsert_final_call(database, **kwargs)
        second = provider_tasks.upsert_final_call(database, **kwargs)

        self.assertEqual((first, second), (29, 0))
        owner = database.execute(
            "SELECT job_id,shot_idx,provider,intent,cost_cents FROM provider_calls "
            "WHERE task_id='same-task'"
        ).fetchone()
        self.assertEqual(tuple(owner), (
            "J-SAME", 1, "seedream_identity", "character_identity_anchor", 29,
        ))
        database.close()

    def test_promote_reservation_fails_closed_on_cross_owner_task_id_collision(self):
        cases = (
            ("J-OTHER", 1, "seedance", "generate"),
            ("J-OWNER", 2, "seedance", "generate"),
        )
        for other_job, other_shot, other_provider, other_intent in cases:
            with self.subTest(other_job=other_job, other_shot=other_shot):
                database = memory_database()
                reservation_id = provider_tasks.reserve_paid_submit(
                    database,
                    job_id="J-OWNER",
                    shot_idx=1,
                    provider="seedance",
                    intent="generate",
                )
                database.execute(
                    "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,usage_json) "
                    "VALUES('remote-collision',?,?,?,?,?)",
                    (
                        other_job, other_shot, other_provider, other_intent,
                        json.dumps({"status": "pending"}),
                    ),
                )
                database.commit()

                with self.assertRaises(provider_tasks.ProviderTaskGateError):
                    provider_tasks.promote_reservation(
                        database,
                        reservation_id,
                        task_id="remote-collision",
                        job_id="J-OWNER",
                        shot_idx=1,
                        provider="seedance",
                        intent="generate",
                    )

                reservation = database.execute(
                    "SELECT usage_json FROM provider_calls WHERE task_id=?",
                    (reservation_id,),
                ).fetchone()
                collision = database.execute(
                    "SELECT job_id,shot_idx,provider,intent FROM provider_calls "
                    "WHERE task_id='remote-collision'"
                ).fetchone()
                self.assertIsNotNone(reservation)
                self.assertIn(
                    json.loads(reservation["usage_json"])["status"],
                    {"submitting", "action_required"},
                )
                self.assertEqual(
                    tuple(collision),
                    (other_job, other_shot, other_provider, other_intent),
                )
                database.close()

    def test_promote_reservation_reuses_exact_same_owner_task_id(self):
        database = memory_database()
        reservation_id = provider_tasks.reserve_paid_submit(
            database,
            job_id="J-OWNER",
            shot_idx=1,
            provider="seedance",
            intent="generate",
        )
        database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,usage_json) "
            "VALUES('remote-same','J-OWNER',1,'seedance','generate',?)",
            (json.dumps({"status": "pending"}),),
        )
        database.commit()

        provider_tasks.promote_reservation(
            database,
            reservation_id,
            task_id="remote-same",
            job_id="J-OWNER",
            shot_idx=1,
            provider="seedance",
            intent="generate",
        )

        self.assertIsNone(database.execute(
            "SELECT 1 FROM provider_calls WHERE task_id=?", (reservation_id,)
        ).fetchone())
        self.assertEqual(database.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE task_id='remote-same'"
        ).fetchone()[0], 1)
        database.close()

    def test_provider_usage_only_keeps_numeric_billing_fields(self):
        usage = s3_shots._safe_provider_usage({
            "completion_tokens": "Authorization: Bearer secret-token",
            "total_tokens": 42,
            "request": {"body": {"prompt": "private prompt"}},
        })

        self.assertEqual(usage, {"total_tokens": 42})
        self.assertNotIn("secret-token", json.dumps(usage))
        self.assertNotIn("private prompt", json.dumps(usage))

    def test_pending_provider_call_upgrades_to_final_cost_once(self):
        database = memory_database()
        pending = ({
            "task_id": "task-billing", "intent": "generate",
            "usage": {"status": "pending"}, "cost_cents": 0,
        },)
        final = ({
            "task_id": "task-billing", "intent": "generate",
            "usage": {"status": "completed", "completion_tokens": 1000},
            "cost_cents": 77,
        },)

        first = s3_shots._record_provider_calls(
            database, "J-BILL", 3, "seedance", pending
        )
        second = s3_shots._record_provider_calls(
            database, "J-BILL", 3, "seedance", final
        )
        third = s3_shots._record_provider_calls(
            database, "J-BILL", 3, "seedance", final
        )

        row = database.execute(
            "SELECT cost_cents,usage_json FROM provider_calls "
            "WHERE task_id='task-billing'"
        ).fetchone()
        self.assertEqual((first, second, third), (0, 77, 0))
        self.assertEqual(row["cost_cents"], 77)
        self.assertEqual(json.loads(row["usage_json"])["status"], "completed")
        self.assertEqual(json.loads(row["usage_json"])["completion_tokens"], 1000)
        database.close()

    def test_record_provider_calls_rejects_cross_owner_task_id(self):
        cases = (
            ("J-OTHER", 1, "seedance", "generate"),
            ("J-OWNER", 2, "seedance", "generate"),
            ("J-OWNER", 1, "suno", "generate"),
            ("J-OWNER", 1, "seedance", "edit"),
        )
        for job_id, shot_idx, provider, intent in cases:
            with self.subTest(
                job_id=job_id, shot_idx=shot_idx,
                provider=provider, intent=intent,
            ):
                database = memory_database()
                database.execute(
                    "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,"
                    "intent,cost_cents,usage_json) VALUES('owned-task','J-OWNER',"
                    "1,'seedance','generate',77,?)",
                    (json.dumps({
                        "status": "completed", "completion_tokens": 11000,
                    }),),
                )
                database.commit()
                incoming = ({
                    "task_id": "owned-task",
                    "intent": intent,
                    "usage": {
                        "status": "completed", "completion_tokens": 999999,
                    },
                    "cost_cents": 7000,
                },)

                with self.assertRaises(provider_tasks.ProviderTaskGateError):
                    s3_shots._record_provider_calls(
                        database, job_id, shot_idx, provider, incoming
                    )

                row = database.execute(
                    "SELECT job_id,shot_idx,provider,intent,cost_cents,usage_json "
                    "FROM provider_calls WHERE task_id='owned-task'"
                ).fetchone()
                self.assertEqual(tuple(row[:5]), (
                    "J-OWNER", 1, "seedance", "generate", 77,
                ))
                usage = json.loads(row["usage_json"])
                self.assertEqual(usage["completion_tokens"], 11000)
                database.close()


@unittest.skipIf(provider_health is None, "provider health not implemented")
class ProviderManagementTests(unittest.TestCase):
    @staticmethod
    def _insert_pending_call(database, job_id: str, *, shot_idx: int = 1) -> None:
        database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "cost_cents,usage_json) VALUES(?,?,?,?,?,?,?)",
            (
                f"task-pending-{job_id}", job_id, shot_idx, "seedance",
                "generate", 0, json.dumps({"status": "pending"}),
            ),
        )

    def test_resume_moves_unresolved_pending_provider_task_to_reconcile_state(self):
        database = memory_database()
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,error) "
            "VALUES('J-RESUME-PENDING','T-1','pending remote task','FAILED','timeout')"
        )
        database.execute(
            "INSERT INTO shots(job_id,idx,state,error) "
            "VALUES('J-RESUME-PENDING',1,'FAILED','timeout')"
        )
        database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "cost_cents,usage_json) VALUES(?,?,?,?,?,?,?)",
            (
                "task-still-pending", "J-RESUME-PENDING", 1, "seedance",
                "generate", 0, json.dumps({"status": "pending"}),
            ),
        )
        database.commit()
        with tempfile.TemporaryDirectory() as tmp:
            service = ManagementService(database, Path(tmp))
            result = service.resume_failed_video("J-RESUME-PENDING")

        job = database.execute(
            "SELECT state FROM jobs WHERE job_id='J-RESUME-PENDING'"
        ).fetchone()
        shot = database.execute(
            "SELECT state FROM shots WHERE job_id='J-RESUME-PENDING'"
        ).fetchone()
        self.assertEqual(result["state"], "PROVIDER_ACTION_REQUIRED")
        self.assertIn("task-still-pending", result["unresolved_provider_tasks"])
        self.assertEqual(
            (job["state"], shot["state"]),
            ("PROVIDER_ACTION_REQUIRED", "FAILED"),
        )
        database.close()

    def test_other_paid_resume_entries_reject_unresolved_pending_task(self):
        database = memory_database()
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,error) "
            "VALUES('J-START-PENDING','T-1','start pending','STORYBOARDED','{}',NULL)"
        )
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,error) "
            "VALUES('J-REGEN-PENDING','T-1','regen pending','FAILED',?,'timeout')",
            (json.dumps({"masters": [{"idx": 1, "type": "dialogue"}]}),),
        )
        database.execute(
            "INSERT INTO shots(job_id,idx,state,error) "
            "VALUES('J-REGEN-PENDING',1,'FAILED','timeout')"
        )
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,error) "
            "VALUES('J-REBUILD-PENDING','T-1','rebuild pending','FAILED','{}','timeout')"
        )
        self._insert_pending_call(database, "J-START-PENDING")
        self._insert_pending_call(database, "J-REGEN-PENDING")
        self._insert_pending_call(database, "J-REBUILD-PENDING")
        database.commit()
        with tempfile.TemporaryDirectory() as tmp:
            service = ManagementService(database, Path(tmp))
            with self.assertRaisesRegex(ValueError, "task-pending-J-START-PENDING"):
                service.start_video_job("J-START-PENDING", confirmed=True)
            with self.assertRaisesRegex(ValueError, "task-pending-J-REGEN-PENDING"):
                service.prepare_master_regeneration(
                    "J-REGEN-PENDING", 1, reason="confirmed repair"
                )
            with self.assertRaisesRegex(ValueError, "task-pending-J-REBUILD-PENDING"):
                service.rebuild_failed_director_plan("J-REBUILD-PENDING")
        database.close()

    def test_resume_allows_confirmed_terminal_failed_provider_task(self):
        database = memory_database()
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,error) "
            "VALUES('J-RESUME-FAILED','T-1','terminal failed','FAILED','provider')"
        )
        database.execute(
            "INSERT INTO shots(job_id,idx,state,error) "
            "VALUES('J-RESUME-FAILED',1,'FAILED','provider')"
        )
        database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "cost_cents,usage_json) VALUES(?,?,?,?,?,?,?)",
            (
                "task-terminal-failed", "J-RESUME-FAILED", 1, "seedance",
                "generate", 0, json.dumps({"status": "failed"}),
            ),
        )
        database.commit()
        with tempfile.TemporaryDirectory() as tmp:
            result = ManagementService(database, Path(tmp)).resume_failed_video(
                "J-RESUME-FAILED"
            )

        self.assertEqual(result["state"], "STORYBOARDED")
        database.close()

    def test_clear_block_does_not_dispatch_or_resume_jobs(self):
        database = memory_database()
        with tempfile.TemporaryDirectory() as tmp:
            service = ManagementService(database, Path(tmp))
            provider_health.block(
                database,
                "seedance",
                "account_unavailable",
                "account action required",
            )
            with patch.object(
                service, "dispatch_production_queue"
            ) as dispatch:
                result = service.clear_seedance_provider_block()

        dispatch.assert_not_called()
        self.assertEqual(result["provider"]["status"], "available")
        self.assertIn("不自动", result["message"])
        database.close()

    def test_management_server_exposes_provider_status_and_manual_clear(self):
        server = (PROJECT_ROOT / "scripts" / "management_server.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('"/api/providers": self.service.provider_statuses', server)
        self.assertIn(
            'request_path == "/api/providers/seedance/clear-block"', server
        )
        self.assertIn("clear_seedance_provider_block", server)

    def test_clear_block_http_endpoint_only_calls_manual_clear(self):
        service = Mock()
        service.clear_seedance_provider_block.return_value = {
            "provider": {"provider": "seedance", "status": "available"},
            "message": "cleared",
        }
        ConsoleHandler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = request.urlopen(request.Request(
                f"http://127.0.0.1:{server.server_port}"
                "/api/providers/seedance/clear-block",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            ))
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(payload["provider"]["status"], "available")
        service.clear_seedance_provider_block.assert_called_once_with()
        service.dispatch_production_queue.assert_not_called()

    def test_clear_block_requires_same_origin_json_without_side_effects(self):
        service = Mock()
        service.clear_seedance_provider_block.return_value = {
            "provider": {"provider": "seedance", "status": "available"},
            "message": "cleared",
        }
        ConsoleHandler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        endpoint = f"{base_url}/api/providers/seedance/clear-block"
        try:
            response = request.urlopen(request.Request(
                endpoint,
                data=b"{}",
                headers={"Content-Type": "application/json", "Origin": base_url},
                method="POST",
            ))
            self.assertEqual(response.status, 200)

            with self.assertRaises(urlerror.HTTPError) as foreign:
                request.urlopen(request.Request(
                    endpoint,
                    data=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://evil.example",
                    },
                    method="POST",
                ))
            self.assertEqual(foreign.exception.code, 403)

            with self.assertRaises(urlerror.HTTPError) as wrong_type:
                request.urlopen(request.Request(
                    endpoint,
                    data=b"{}",
                    headers={"Content-Type": "text/plain"},
                    method="POST",
                ))
            self.assertEqual(wrong_type.exception.code, 415)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        service.clear_seedance_provider_block.assert_called_once_with()
        service.dispatch_production_queue.assert_not_called()

    def test_config_ui_shows_provider_block_and_manual_clear_contract(self):
        html = (PROJECT_ROOT / "scripts" / "management_ui" / "index.html").read_text(
            encoding="utf-8"
        )
        script = (PROJECT_ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="provider-runtime-status"', html)
        self.assertIn("我已处理账号问题，恢复生产", script)
        self.assertIn("/api/providers/seedance/clear-block", script)
        self.assertIn("已解除本地阻断，请返回任务确认续跑", script)
        self.assertIn("不会自动检测余额", script)
        self.assertIn("不会自动续跑", script)


class RealQualityCanaryContractTests(unittest.TestCase):
    def test_frozen_profiles_are_exactly_the_three_authorized_shapes(self):
        profiles = quality_canary.load_frozen_profiles()

        self.assertEqual(
            [(item["duration_sec"], len(item["characters"])) for item in profiles],
            [(8, 1), (18, 2), (26, 3)],
        )
        self.assertEqual(
            [item["budget_cents"] for item in profiles], [1500, 3000, 4500]
        )
        self.assertTrue(all(item["max_auto_retries"] == 0 for item in profiles))
        self.assertTrue(all(item["requires_explicit_authorization"] for item in profiles))

    def test_acceptance_record_preserves_cost_tasks_and_review_risks(self):
        job = {
            "job_id": "J-CANARY-OBSERVED", "title": "已完成真实样本",
            "state": "READY_TO_PUBLISH", "cost_cents": 9102,
            "total_master_duration_sec": 70.5,
            "production_settings": {
                "orientation": "9:16", "audio_mode": "seedance_native",
                "generation_snapshot": {
                    "model": "doubao-seedance-2-5-260628",
                    "max_auto_retries": 1, "job_budget_cents": 15000,
                },
            },
            "storyboard": {"title": "完整输入", "masters": [{"idx": 1}]},
            "characters": [{"id": "lead", "name": "林夏"}],
            "scene_anchor_plan": [{"anchor_key": "site", "status": "ready"}],
            "character_makeup_plan": [{"identity_key": "lead", "status": "ready"}],
            "shots": [{
                "idx": 3, "state": "OK", "cost_cents": 2626,
                "quality_reasons": ["lip_sync_mismatch", "visual_mismatch"],
                "quality_status": "candidate_accepted",
                "human_decision": "accept_candidate",
                "video_path": r"C:\runtime\data\assets\J-CANARY-OBSERVED\shots\03.mp4",
                "video_url": "https://signed.example/private.mp4?token=secret",
            }],
            "latest_review": {
                "decision": "approved", "first_pass": 0,
                "checks": {"identity": True, "lip_sync": True},
            },
            "final_path": r"C:\runtime\data\assets\J-CANARY-OBSERVED\final.mp4",
        }
        calls = [{
            "task_id": "remote-task-1", "shot_idx": 3,
            "provider": "seedance", "intent": "generate",
            "cost_cents": 2626,
            "usage_json": json.dumps({
                "total_tokens": 375092, "status": "completed",
                "reference_manifest": [{"kind": "video", "duty": "speaker_identity"}],
            }),
        }]

        record = quality_canary.build_acceptance_record(
            job, calls, root=Path(r"C:\runtime"), run_id="observed-test13"
        )

        self.assertEqual(record["classification"], "observational")
        self.assertEqual(record["completion_status"], "evidence_only")
        self.assertEqual(record["cost"]["actual_cents"], 9102)
        self.assertEqual(record["provider_tasks"][0]["task_id"], "remote-task-1")
        self.assertEqual(record["provider_tasks"][0]["total_tokens"], 375092)
        self.assertEqual(
            record["quality"]["shot_findings"][0]["quality_reasons"],
            ["lip_sync_mismatch", "visual_mismatch"],
        )
        self.assertEqual(record["quality"]["final_review"]["first_pass"], 0)
        self.assertIsNone(record["quality"]["manual_scores"])
        serialized = json.dumps(record, ensure_ascii=False)
        self.assertNotIn("signed.example", serialized)
        self.assertNotIn("token=secret", serialized)

    def test_canary_classification_requires_the_exact_frozen_contract(self):
        frozen = quality_canary.prepare_run_contract("single-speaker-8s-v1")
        base_job = {
            "job_id": "J-CANARY-8S",
            "title": "8 秒正式样本",
            "state": "READY_TO_PUBLISH",
            "cost_cents": 700,
            "total_master_duration_sec": 8,
            "characters": [{"name": "林夏"}],
            "storyboard": {},
            "shots": [],
            "production_settings": {
                "generation_snapshot": dict(frozen),
            },
        }

        exact = quality_canary.build_acceptance_record(
            base_job, [], root=PROJECT_ROOT, run_id="exact-canary",
            profile_id="single-speaker-8s-v1",
        )
        self.assertEqual(exact["classification"], "canary")
        self.assertEqual(exact["profile_id"], "single-speaker-8s-v1")

        wrong_script = deepcopy(base_job)
        wrong_script["production_settings"]["generation_snapshot"][
            "quality_canary_frozen_input"
        ]["script"][0]["dialogue"] = "这不是冻结脚本"
        observed = quality_canary.build_acceptance_record(
            wrong_script, [], root=PROJECT_ROOT, run_id="spoofed-canary",
            profile_id="single-speaker-8s-v1",
        )
        self.assertEqual(observed["classification"], "observational")
        self.assertIsNone(observed["profile_id"])

        same_shape_wrong_character = deepcopy(base_job)
        same_shape_wrong_character["characters"] = [{"name": "其他角色"}]
        observed = quality_canary.build_acceptance_record(
            same_shape_wrong_character, [], root=PROJECT_ROOT,
            run_id="same-shape-wrong-character",
            profile_id="single-speaker-8s-v1",
        )
        self.assertEqual(observed["classification"], "observational")
        self.assertIsNone(observed["profile_id"])

    def test_write_acceptance_record_is_atomic_and_runtime_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = {"schema_version": 1, "run_id": "run-001"}
            path = quality_canary.write_acceptance_record(root, record)

            self.assertEqual(
                path, root / "data" / "acceptance" / "run-001" / "acceptance.json"
            )
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), record)
            self.assertFalse(path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
