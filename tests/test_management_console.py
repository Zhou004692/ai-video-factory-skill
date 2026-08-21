from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from urllib import error, request
from unittest import mock
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


from factory import (  # noqa: E402
    db, production_config, production_planner, provider_capacity, provider_tasks,
    review_candidates, scene_assets,
)
from factory.clients import music, seedance  # noqa: E402
from factory.management_api import ManagementService  # noqa: E402
from management_server import (  # noqa: E402
    ConsoleHandler, MEDIA_UPLOAD_MAX_BYTES, parse_byte_range,
    run_provider_reconcile_sweeper, run_queue_supervisor,
    validate_local_bind_host,
)
from http.server import ThreadingHTTPServer  # noqa: E402
import management_server  # noqa: E402


def _database() -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.executescript(db.DDL)
    return database


class ManagementServiceTests(unittest.TestCase):
    def setUp(self):
        # Config-save tests intentionally update the live process environment so
        # the running console reacts immediately.  Keep those mutations local
        # to each test instead of leaking provider/model choices into later
        # modules in the full release suite.
        self.environment = mock.patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        (self.root / "templates").mkdir()
        (self.root / "templates" / "env.example").write_text(
            "ARK_API_KEY=<required>\n"
            "ARK_VIDEO_RESOLUTION=720p\n"
            "PUBLISH_MODE=manual\n",
            encoding="utf-8",
        )
        (self.root / ".env").write_text(
            "ARK_API_KEY=secret-value\n"
            "ARK_VIDEO_RESOLUTION=720p\n",
            encoding="utf-8",
        )
        self.database = _database()
        self.service = ManagementService(self.database, self.root)

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()
        self.environment.stop()

    def test_overview_uses_real_pipeline_counts(self):
        self.database.execute(
            "INSERT INTO script_sources(source_id,fingerprint,source_type,title,content) "
            "VALUES('SRC-1','fp-1','manual','访谈','内容')"
        )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,cost_cents) "
            "VALUES('J-1','T-1','设备租赁','COMPOSED',1234)"
        )
        self.database.execute(
            "INSERT INTO platform_publications(publication_id,job_id,platform,item_id) "
            "VALUES('PUB-1','J-1','douyin','item-1')"
        )
        self.database.commit()

        overview = self.service.overview()

        self.assertEqual(overview["scripts"]["sources"], 1)
        self.assertEqual(overview["videos"]["jobs"], 1)
        self.assertEqual(overview["videos"]["cost_cents"], 1234)
        self.assertEqual(overview["publishing"]["mapped"], 1)

    def test_overview_exposes_structured_reliability_windows_and_release_gate(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,cost_cents,created_at,updated_at) "
            "VALUES('J-METRIC','T-METRIC','可靠性样本','COMPOSED',120,"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        self.database.commit()

        reliability = self.service.overview()["reliability"]

        self.assertEqual(set(reliability["windows"]), {"7d", "30d"})
        self.assertEqual(len(reliability["daily"]), 30)
        self.assertIn(
            reliability["release_gate"]["status"],
            {"pass", "freeze", "insufficient_data"},
        )
        for window in reliability["windows"].values():
            self.assertIn("first_round_progression_rate", window)
            self.assertIn("duplicate_paid_submissions", window)
            self.assertIn("duration_seconds", window)

    def test_explicit_canary_entry_creates_isolated_storyboard_without_provider_call(self):
        result = self.service.create_quality_canary({
            "profile_id": "single-speaker-8s-v1",
            "confirmed": True,
            "budget_cents": 2000,
        })

        row = self.database.execute(
            "SELECT state,storyboard FROM jobs WHERE job_id=?",
            (result["job_id"],),
        ).fetchone()
        storyboard = json.loads(row["storyboard"])
        self.assertEqual(row["state"], "STORYBOARDED")
        self.assertEqual(storyboard["quality_canary"]["launch_mode"], "manual_confirmed")
        self.assertFalse(storyboard["quality_canary"]["scheduler_allowed"])
        settings = storyboard["production_settings"]
        snapshot = settings["generation_snapshot"]
        self.assertEqual(settings["budget_cents"], 2000)
        self.assertEqual(snapshot["job_budget_cents"], 2000)
        self.assertEqual(snapshot["max_auto_retries"], 0)
        self.assertEqual(snapshot["auto_retry_budget_cents"], 0)
        self.assertEqual(
            snapshot["quality_canary_profile_id"], "single-speaker-8s-v1"
        )
        self.assertEqual(
            snapshot["quality_canary_frozen_input"],
            storyboard["quality_canary"]["quality_canary_frozen_input"],
        )
        self.assertEqual(
            snapshot["quality_canary_contract_sha256"],
            storyboard["quality_canary"]["quality_canary_contract_sha256"],
        )
        self.assertTrue(all(
            character["identity_type"] == "custom"
            and not character.get("seedance_asset_uri")
            for character in storyboard["characters"]
        ))
        shots = self.database.execute(
            "SELECT idx,state FROM shots WHERE job_id=? ORDER BY idx",
            (result["job_id"],),
        ).fetchall()
        self.assertEqual(
            [(int(shot["idx"]), str(shot["state"])) for shot in shots],
            [(1, "PENDING")],
        )
        compiled = production_planner.compile_job_spec(
            self.database, result["job_id"], self.root, persist=False,
        )
        self.assertEqual(compiled.spec.budget_cents, 2000)
        self.assertEqual(
            self.database.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0],
            0,
        )

    def test_video_cover_file_prefers_timeline_continuity_cover(self):
        asset_dir = self.root / "data" / "assets" / "J-COVER"
        asset_dir.mkdir(parents=True)
        final = asset_dir / "final.mp4"
        cover = asset_dir / "thumbnail.jpg"
        final.write_bytes(b"video")
        cover.write_bytes(b"first-frame")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,final_path) "
            "VALUES('J-COVER','T-COVER','封面连续性','COMPOSED',?)",
            (str(final),),
        )
        self.database.commit()

        self.assertEqual(self.service.video_cover_file("J-COVER"), cover.resolve())

    def test_content_and_video_lists_cover_first_operational_scale(self):
        self.database.executemany(
            "INSERT INTO script_sources(source_id,fingerprint,source_type,title,content) "
            "VALUES(?,?, 'manual', ?, ?)",
            [
                (f"SRC-SCALE-{index:02d}", f"fp-scale-{index:02d}",
                 f"规模来源 {index:02d}", "用于管理台规模验收的完整来源内容")
                for index in range(50)
            ],
        )
        self.database.executemany(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json) "
            "VALUES(?, 'candidate', ?, '{}')",
            [
                (f"BRIEF-SCALE-{index:02d}", f"规模简报 {index:02d}")
                for index in range(20)
            ],
        )
        self.database.executemany(
            "INSERT INTO script_drafts(draft_id,brief_id,variant_index,status,"
            "request_fingerprint,script_json) VALUES(?,?,0,'candidate',?,'{}')",
            [
                (f"SCRIPT-SCALE-{index:02d}", f"BRIEF-SCALE-{index:02d}",
                 f"request-scale-{index:02d}")
                for index in range(20)
            ],
        )
        self.database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,'STORYBOARDED','{}')",
            [
                (f"J-SCALE-{index:02d}", f"T-SCALE-{index:02d}",
                 f"规模视频 {index:02d}")
                for index in range(30)
            ],
        )
        self.database.execute(
            "INSERT INTO character_identity_anchors("
            "job_id,identity_key,character_id,display_name,source_type,state) "
            "VALUES('J-FAILED-DELETE','script:customer','customer','Customer',"
            "'ai_identity_master','FAILED')"
        )
        self.database.commit()

        scripts = self.service.scripts()
        videos = self.service.videos()

        self.assertEqual(len(scripts["sources"]), 50)
        self.assertEqual(len(scripts["briefs"]), 20)
        self.assertEqual(len(scripts["drafts"]), 20)
        self.assertEqual(len(videos["jobs"]), 30)

    def test_existing_console_probe_reuses_valid_local_service(self):
        probe = getattr(management_server, "existing_console_url", None)
        self.assertIsNotNone(probe, "management server needs a single-instance probe")
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = json.dumps({
            "scripts": {"sources": 1},
            "videos": {"jobs": 1},
            "publishing": {"ready": 0},
        }).encode("utf-8")
        response.__enter__.return_value = response

        with mock.patch("management_server.request.urlopen", return_value=response):
            url = probe("127.0.0.1", 8765)

        self.assertEqual(url, "http://127.0.0.1:8765/#overview")

    def test_overview_separates_manual_review_from_other_video_failures(self):
        self.database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state) VALUES(?,?,?,'FAILED')",
            [
                ("J-REVIEW-QUEUE", "T-REVIEW", "manual review"),
                ("J-OTHER-FAIL", "T-FAIL", "provider failure"),
            ],
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,state,video_qa,error) VALUES(?,1,'FAILED',?,?)",
            [
                (
                    "J-REVIEW-QUEUE",
                    json.dumps({
                        "status": "manual_review_required",
                        "reasons": ["missing_environment_context"],
                    }),
                    "manual review required",
                ),
                (
                    "J-OTHER-FAIL",
                    json.dumps({"status": "qa_error"}),
                    "provider failed",
                ),
            ],
        )
        self.database.commit()

        overview = self.service.overview()

        self.assertEqual(overview["videos"]["review_required"], 1)
        self.assertEqual(overview["videos"]["failed"], 1)
        self.assertEqual(overview["videos"]["failures_total"], 2)

    def test_business_pause_states_have_distinct_counts_and_next_actions(self):
        self.database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,?, '{}')",
            [
                (
                    "J-PRODUCTION-REVIEW", "T-PR", "review candidate",
                    "PRODUCTION_REVIEW_REQUIRED",
                ),
                (
                    "J-PROVIDER-ACTION", "T-PA", "recharge provider",
                    "PROVIDER_ACTION_REQUIRED",
                ),
                (
                    "J-TECHNICAL-FAILURE", "T-TF", "technical failure",
                    "FAILED",
                ),
            ],
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_qa) VALUES(?,?,?,?)",
            (
                "J-PRODUCTION-REVIEW", 1, "FAILED",
                json.dumps({
                    "status": "manual_review_required",
                    "reasons": ["visual_mismatch"],
                }),
            ),
        )
        self.database.commit()

        overview = self.service.overview()["videos"]
        jobs = {
            job["job_id"]: job for job in self.service.videos()["jobs"]
        }

        self.assertEqual(overview["review_required"], 1)
        self.assertEqual(overview["failed"], 1)
        self.assertEqual(overview["failures_total"], 1)
        self.assertEqual(overview["active"], 0)
        review = jobs["J-PRODUCTION-REVIEW"]
        self.assertEqual(review["display_state_label"], "母片待复核")
        self.assertEqual(review["next_action"], {
            "id": "review_master_candidates",
            "label": "预览并处理候选母片",
        })
        provider = jobs["J-PROVIDER-ACTION"]
        self.assertEqual(provider["display_state_label"], "模型账号待处理")
        self.assertEqual(provider["next_action"], {
            "id": "view_provider_account_guide",
            "label": "查看账号处理指引",
        })

    def test_each_nonterminal_video_exposes_exactly_one_allowed_next_action(self):
        expected = {
            "PENDING": "view_progress",
            "STORYBOARDED": "confirm_budget",
            "PRODUCTION_QUEUED": "view_progress",
            "PRODUCTION_RUNNING": "view_progress",
            "ANCHOR_REVIEW_REQUIRED": "review_continuity_anchors",
            "SHOTS_OK": "resume_local_postproduction",
            "BGM_OK": "resume_local_postproduction",
            "COMPOSED": "review_final_video",
            "READY_TO_PUBLISH": "publish_video",
            "FAILED": "resume_failed_video",
            "RECOVERY_REQUIRED": "preserve_and_diagnose",
        }
        self.database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,?, '{}')",
            [
                (f"J-ACTION-{state}", f"T-{index}", state, state)
                for index, state in enumerate(expected, 1)
            ],
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,state,cost_cents) VALUES(?,1,?,0)",
            [
                (
                    f"J-ACTION-{state}",
                    "OK" if state in {
                        "SHOTS_OK", "BGM_OK", "COMPOSED", "READY_TO_PUBLISH",
                    } else "PENDING",
                )
                for state in expected
            ],
        )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-ACTION-REJECTED','T-TERMINAL','terminal','REJECTED','{}')"
        )
        self.database.commit()

        jobs = {
            job["state"]: job
            for job in self.service.videos()["jobs"]
            if job["job_id"].startswith("J-ACTION-")
        }

        for state, action_id in expected.items():
            with self.subTest(state=state):
                job = jobs[state]
                self.assertEqual(job["next_action"]["id"], action_id)
                self.assertEqual(job["allowed_actions"], [job["next_action"]])
                self.assertEqual(job["status_reason"]["code"], state.lower())
                self.assertTrue(job["status_reason"]["message"])
        self.assertIsNone(jobs["REJECTED"]["next_action"])
        self.assertEqual(jobs["REJECTED"]["allowed_actions"], [])

    def test_cost_reconciliation_separates_tracked_calls_from_legacy_total(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,cost_cents) "
            "VALUES('J-COST','T-COST','成本对账','COMPOSED',1200)"
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,cost_cents) "
            "VALUES('CALL-1','J-COST',1,'seedance',800)"
        )
        self.database.commit()

        overview = self.service.overview()
        job = self.service.videos()["jobs"][0]

        self.assertEqual(overview["videos"]["cost_cents"], 1200)
        self.assertEqual(overview["videos"]["tracked_call_cost_cents"], 800)
        self.assertEqual(overview["videos"]["unallocated_cost_cents"], 400)
        self.assertEqual(overview["videos"]["reconciliation_status"], "partial")
        self.assertEqual(job["tracked_call_cost_cents"], 800)
        self.assertEqual(job["unallocated_cost_cents"], 400)
        self.assertEqual(job["cost_reconciliation_status"], "partial")

    def test_overview_cost_calibration_uses_only_completed_confirmed_snapshots(self):
        def storyboard(estimate):
            return json.dumps({
                "production_settings": {
                    "confirmed_cost_estimate": {
                        "projected_total_cost_cents": estimate,
                        "pricing_version": "seedance25-area-v1",
                    },
                },
            })

        self.database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES(?,?,?,?,?,?)",
            [
                ("J-CAL-1", "T-1", "under", "COMPOSED", storyboard(1000), 900),
                ("J-CAL-2", "T-2", "over", "READY_TO_PUBLISH", storyboard(1000), 1100),
                ("J-CAL-3", "T-3", "drift", "PUBLISHED", storyboard(2000), 2400),
                ("J-CAL-ACTIVE", "T-4", "active", "STORYBOARDED", storyboard(5000), 0),
                ("J-CAL-LEGACY", "T-5", "legacy", "COMPOSED", "{}", 500),
            ],
        )
        self.database.commit()

        calibration = self.service.overview()["videos"]["cost_estimation"]

        self.assertEqual(calibration, {
            "sample_count": 3,
            "estimated_cost_cents": 4000,
            "actual_cost_cents": 4400,
            "variance_cents": 400,
            "variance_percent": 10.0,
            "mean_absolute_percentage_error": 13.3,
            "status": "drift",
            "pricing_versions": ["seedance25-area-v1"],
            "segments": {
                "resolution": [],
                "duration_bucket": [],
                "reference_type": [],
            },
        })

    def test_overview_cost_calibration_marks_small_samples_as_calibrating(self):
        storyboard = json.dumps({
            "production_settings": {
                "confirmed_cost_estimate": {
                    "projected_total_cost_cents": 1224,
                    "pricing_version": "seedance25-area-v1",
                },
            },
        })
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-CAL-ONE','T-1','one','COMPOSED',?,1216)",
            (storyboard,),
        )
        self.database.commit()

        calibration = self.service.overview()["videos"]["cost_estimation"]

        self.assertEqual(calibration["sample_count"], 1)
        self.assertEqual(calibration["status"], "calibrating")
        self.assertEqual(calibration["variance_percent"], -0.7)
        self.assertEqual(calibration["mean_absolute_percentage_error"], 0.7)

    def test_overview_cost_calibration_ignores_malformed_estimate_snapshots(self):
        malformed_snapshots = [
            "not-json",
            json.dumps({"production_settings": {"confirmed_cost_estimate": "bad"}}),
            json.dumps({
                "production_settings": {
                    "confirmed_cost_estimate": {
                        "projected_total_cost_cents": "not-a-number",
                        "pricing_version": "seedance25-area-v1",
                    },
                },
            }),
            json.dumps({
                "production_settings": {
                    "confirmed_cost_estimate": {
                        "projected_total_cost_cents": 0,
                        "pricing_version": "seedance25-area-v1",
                    },
                },
            }),
        ]
        self.database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES(?,?,?,?,?,?)",
            [
                (f"J-CAL-BAD-{idx}", f"T-{idx}", "bad", "COMPOSED", snapshot, 100)
                for idx, snapshot in enumerate(malformed_snapshots)
            ],
        )
        self.database.commit()

        calibration = self.service.overview()["videos"]["cost_estimation"]

        self.assertEqual(calibration["sample_count"], 0)
        self.assertEqual(calibration["status"], "no_data")
        self.assertIsNone(calibration["variance_percent"])
        self.assertIsNone(calibration["mean_absolute_percentage_error"])

    def test_overview_cost_calibration_groups_completed_paid_masters(self):
        def storyboard(job_masters):
            return json.dumps({
                "production_settings": {
                    "confirmed_cost_estimate": {
                        "projected_total_cost_cents": sum(
                            item["estimated_cost_cents"] for item in job_masters
                        ),
                        "pricing_version": "seedance25-area-v1",
                        "paid_masters": job_masters,
                    },
                },
            })

        self.database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES(?,?,?,?,?,?)",
            [
                ("J-SEG-1", "T-1", "one", "COMPOSED", storyboard([{
                    "master_idx": 1, "resolution": "720p",
                    "duration_bucket": "4-12s", "reference_type": "image",
                    "estimated_cost_cents": 1000,
                }]), 1100),
                ("J-SEG-2", "T-2", "two", "PUBLISHED", storyboard([{
                    "master_idx": 2, "resolution": "720p",
                    "duration_bucket": "13-24s", "reference_type": "video",
                    "estimated_cost_cents": 2000,
                }]), 1800),
            ],
        )
        self.database.executemany(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "cost_cents,usage_json) VALUES(?,?,?,?,?,?,?)",
            [
                ("CALL-SEG-1", "J-SEG-1", 1, "seedance", "generate", 1100, "{}"),
                ("CALL-SEG-2", "J-SEG-2", 2, "seedance", "generate", 1800, "{}"),
            ],
        )
        self.database.commit()

        segments = self.service.overview()["videos"]["cost_estimation"]["segments"]

        self.assertEqual(segments["resolution"], [{
            "key": "720p", "sample_count": 2,
            "estimated_cost_cents": 3000, "actual_cost_cents": 2900,
            "variance_cents": -100, "variance_percent": -3.3,
            "mean_absolute_percentage_error": 10.0, "status": "calibrating",
        }])
        self.assertEqual(
            [item["key"] for item in segments["duration_bucket"]],
            ["4-12s", "13-24s"],
        )
        self.assertEqual(
            [item["key"] for item in segments["reference_type"]],
            ["image", "video"],
        )

    def test_content_stage_placeholder_is_not_a_video_task(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) "
            "VALUES('J-CONTENT','T-CONTENT','仍在选剧本','PENDING')"
        )
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json,"
            "approved_topic_id,approved_job_id,approved_angle_index) "
            "VALUES('BRIEF-CONTENT','approved','仍在选剧本','{}',"
            "'T-CONTENT','J-CONTENT',0)"
        )
        self.database.commit()

        overview = self.service.overview()
        videos = self.service.videos()

        self.assertEqual(overview["videos"]["jobs"], 0)
        self.assertEqual(overview["videos"]["active"], 0)
        self.assertEqual(videos["jobs"], [])

    def test_unlinked_empty_pending_placeholder_is_not_a_video_task(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-LEGACY-EMPTY','T-LEGACY-EMPTY','历史空占位','PENDING',NULL)"
        )
        self.database.commit()

        overview = self.service.overview()
        videos = self.service.videos()

        self.assertEqual(overview["videos"]["jobs"], 0)
        self.assertEqual(overview["videos"]["active"], 0)
        self.assertEqual(videos["jobs"], [])

    def test_management_schema_tracks_production_worker_pid(self):
        columns = {
            row[1] for row in self.database.execute("PRAGMA table_info(jobs)")
        }

        self.assertIn("production_worker_pid", columns)
        self.assertIn("production_started_at", columns)

    def test_orphaned_running_job_returns_to_queue_on_service_start(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) "
            "VALUES('J-ORPHAN','T-ORPHAN','中断任务','PRODUCTION_RUNNING')"
        )
        self.database.commit()

        ManagementService(self.database, self.root)

        row = self.database.execute(
            "SELECT state FROM jobs WHERE job_id='J-ORPHAN'"
        ).fetchone()
        self.assertEqual(row["state"], "PRODUCTION_QUEUED")
        event = self.database.execute(
            "SELECT level,message FROM events WHERE job_id='J-ORPHAN' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(event["level"], "warning")
        self.assertIn("自动恢复排队", event["message"])

    def test_service_clears_stale_error_from_successful_job_states(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,error) "
            "VALUES('J-SUCCESS','T-SUCCESS','已完成','PUBLISHED','旧的供应商错误')"
        )
        self.database.commit()

        ManagementService(self.database, self.root)

        row = self.database.execute(
            "SELECT error FROM jobs WHERE job_id='J-SUCCESS'"
        ).fetchone()
        self.assertIsNone(row["error"])

    def test_database_connection_enables_foreign_keys(self):
        database_path = self.root / "data" / "foreign-keys.db"
        with mock.patch.object(db, "_DB_PATH", database_path), \
                mock.patch.object(db, "DATA_DIR", database_path.parent):
            database = db.conn()
            try:
                enabled = database.execute("PRAGMA foreign_keys").fetchone()[0]
            finally:
                database.close()

        self.assertEqual(enabled, 1)

    @mock.patch("factory.management_api.source_analysis.analyze_source")
    @mock.patch("factory.management_api.media_sources.ingest_media", new_callable=mock.AsyncMock)
    def test_local_media_source_reuses_ingest_media(self, ingest_media, analyze_source):
        with (self.root / ".env").open("a", encoding="utf-8") as stream:
            stream.write("VOLC_ASR_APPID=asr-app\nVOLC_ASR_TOKEN=asr-token\n")
        media = self.root / "访谈片段.mp4"
        media.write_bytes(b"fake video")
        ingest_media.return_value = SimpleNamespace(
            source_id="SRC-MEDIA", created=True,
        )

        result = self.service.add_media_source(media, title="客户访谈")

        self.assertEqual(result, {
            "source_id": "SRC-MEDIA", "created": True, "source_type": "video",
        })
        ingest_media.assert_awaited_once_with(
            self.database, media.resolve(), title="客户访谈",
            asr_app_id="asr-app", asr_access_token="asr-token",
        )
        analyze_source.assert_called_once_with(self.database, "SRC-MEDIA")

    @mock.patch("factory.management_api.source_analysis.analyze_source")
    @mock.patch("factory.management_api.media_sources.ingest_media", new_callable=mock.AsyncMock)
    def test_duplicate_local_video_creates_a_new_content_project_while_reusing_media(
        self, ingest_media, analyze_source
    ):
        with (self.root / ".env").open("a", encoding="utf-8") as stream:
            stream.write("VOLC_ASR_API_KEY=new-api-key\n")
        metadata = {
            "media_sha256": "same-video-sha",
            "archived_path": str(self.root / "archive" / "source.mp4"),
            "structure_analysis": {"summary": "已解析的视频结构"},
        }
        self.database.execute(
            "INSERT INTO script_sources(source_id,fingerprint,source_type,title,"
            "source_uri,content,metadata_json) VALUES(?,?,?,?,?,?,?)",
            (
                "SRC-ORIGINAL", "fp-original", "video", "第一次项目",
                "local-media://same-video-sha", "完整视频转写与结构内容",
                json.dumps(metadata, ensure_ascii=False),
            ),
        )
        self.database.commit()
        media = self.root / "same-video.mp4"
        media.write_bytes(b"same video")
        ingest_media.return_value = SimpleNamespace(
            source_id="SRC-ORIGINAL", created=False,
        )

        result = self.service.add_media_source(media, title="第二次项目")

        self.assertTrue(result["created"])
        self.assertTrue(result["reused_source"])
        self.assertNotEqual(result["source_id"], "SRC-ORIGINAL")
        rows = self.database.execute(
            "SELECT source_id,title,content,metadata_json FROM script_sources "
            "ORDER BY created_at,source_id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        copied = next(row for row in rows if row["source_id"] == result["source_id"])
        self.assertEqual(copied["title"], "第二次项目")
        self.assertEqual(copied["content"], "完整视频转写与结构内容")
        copied_metadata = json.loads(copied["metadata_json"])
        self.assertEqual(copied_metadata["canonical_source_id"], "SRC-ORIGINAL")
        self.assertEqual(
            {item["project_id"] for item in self.service.content_projects()["projects"]},
            {"SRC-ORIGINAL", result["source_id"]},
        )
        analyze_source.assert_called_once_with(
            self.database, result["source_id"]
        )

    @mock.patch("factory.management_api.topic_briefs.generate_brief")
    @mock.patch("factory.management_api.source_analysis.analyze_source")
    def test_video_source_is_visually_analyzed_before_brief_generation(
        self, analyze_source, generate_brief
    ):
        self.database.execute(
            "INSERT INTO script_sources(source_id,fingerprint,source_type,title,content,metadata_json) "
            "VALUES('SRC-VIDEO','fp-video','video','现场视频',?, '{}')",
            ("设备仓库现场，客户询问租赁价格。销售演示设备动作与成本优势。" * 3,),
        )
        self.database.commit()
        analyze_source.return_value = {
            "summary": "仓库现场演示",
            "segments": [{"role": "proof", "start_sec": 0, "end_sec": 8}],
        }
        generate_brief.return_value = {"brief_id": "BRIEF-VIDEO"}

        result = self.service.generate_brief_from_source("SRC-VIDEO")

        self.assertEqual(result["brief_id"], "BRIEF-VIDEO")
        analyze_source.assert_called_once_with(self.database, "SRC-VIDEO")
        generate_brief.assert_called_once_with(self.database, ["SRC-VIDEO"])

    @mock.patch("factory.management_api.source_analysis.analyze_source")
    @mock.patch("factory.management_api.media_sources.ingest_media", new_callable=mock.AsyncMock)
    def test_local_media_source_prefers_new_asr_api_key(
        self, ingest_media, analyze_source
    ):
        with (self.root / ".env").open("a", encoding="utf-8") as stream:
            stream.write("VOLC_ASR_API_KEY=new-api-key\n")
        media = self.root / "interview.mp4"
        media.write_bytes(b"fake video")
        ingest_media.return_value = SimpleNamespace(
            source_id="SRC-MEDIA", created=True,
        )

        self.service.add_media_source(media, title="interview")

        ingest_media.assert_awaited_once_with(
            self.database, media.resolve(), title="interview",
            asr_api_key="new-api-key", asr_app_id="", asr_access_token="",
        )
        analyze_source.assert_called_once_with(self.database, "SRC-MEDIA")

    @mock.patch("factory.management_api.media_sources.ingest_media", new_callable=mock.AsyncMock)
    def test_local_media_source_rejects_unsupported_file_before_ingestion(self, ingest_media):
        media = self.root / "notes.txt"
        media.write_text("not media", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "音视频格式"):
            self.service.add_media_source(media, title="不支持")

        ingest_media.assert_not_awaited()

    @mock.patch("factory.management_api.media_sources.ingest_media", new_callable=mock.AsyncMock)
    def test_local_media_source_requires_asr_before_ingestion(self, ingest_media):
        media = self.root / "interview.mp4"
        media.write_bytes(b"fake video")

        with self.assertRaisesRegex(
            ValueError,
            "本地音视频转写需要先配置火山语音识别",
        ):
            self.service.add_media_source(media, title="客户访谈")

        ingest_media.assert_not_awaited()

    def test_config_status_never_returns_secret_value(self):
        status = self.service.config_status()

        api_key = next(item for item in status["fields"] if item["key"] == "ARK_API_KEY")
        resolution = next(
            item for item in status["fields"] if item["key"] == "ARK_VIDEO_RESOLUTION"
        )
        self.assertTrue(api_key["configured"])
        self.assertNotIn("value", api_key)
        self.assertEqual(resolution["value"], "720p")
        self.assertNotIn("secret-value", json.dumps(status, ensure_ascii=False))

    def test_config_status_separates_required_setup_from_optional_defaults(self):
        status = self.service.config_status()

        self.assertEqual(
            [group["id"] for group in status["required_groups"]],
            ["ark", "tts", "asr", "storage"],
        )
        required_keys = {
            key
            for group in status["required_groups"]
            for key in group["keys"]
        }
        self.assertEqual(
            required_keys,
            {
                "ARK_API_KEY",
                "VOLC_TTS_APPID",
                "VOLC_TTS_API_KEY",
                "VOLC_ASR_API_KEY",
                "VOLC_AK",
                "VOLC_SK",
                "TOS_BUCKET",
            },
        )
        optional_keys = {item["key"] for item in status["optional_fields"]}
        visible_keys = {item["key"] for item in status["fields"]}
        self.assertNotIn("VOLC_ASR_APPID", visible_keys)
        self.assertNotIn("VOLC_ASR_TOKEN", visible_keys)
        self.assertIn("ARK_VIDEO_RESOLUTION", optional_keys)
        self.assertIn("SEEDANCE_JOB_BUDGET_CENTS", optional_keys)
        self.assertIn(
            "SEEDANCE_PRODUCTION_REFERENCE_VIDEO_BUDGET", optional_keys
        )
        self.assertTrue(all(group["guide"]["steps"] for group in status["required_groups"]))
        self.assertTrue(all(group["guide"]["url"].startswith("https://") for group in status["required_groups"]))

    def test_reference_attention_budget_must_stay_inside_provider_capability(self):
        with self.assertRaisesRegex(ValueError, "1 到 10"):
            self.service.update_config({
                "SEEDANCE_PRODUCTION_REFERENCE_VIDEO_BUDGET": "11",
            })

    def test_script_readiness_only_needs_ark_group(self):
        status = self.service.config_status()

        ark = next(group for group in status["required_groups"] if group["id"] == "ark")
        asr = next(group for group in status["required_groups"] if group["id"] == "asr")
        self.assertTrue(ark["ready"])
        self.assertFalse(asr["ready"])
        self.assertTrue(status["readiness"]["script"]["ready"])
        self.assertFalse(status["readiness"]["video"]["ready"])

    def test_generated_video_readiness_does_not_require_optional_asr(self):
        (self.root / ".env").write_text(
            "ARK_API_KEY=secret-value\n"
            "VOLC_TTS_APPID=tts-app\n"
            "VOLC_TTS_API_KEY=tts-key\n"
            "VOLC_AK=ak\n"
            "VOLC_SK=sk\n"
            "TOS_BUCKET=bucket\n",
            encoding="utf-8",
        )

        status = self.service.config_status()

        self.assertTrue(status["readiness"]["video"]["ready"])
        self.assertNotIn(
            "VOLC_ASR_API_KEY", status["readiness"]["video"]["missing"]
        )

    def test_seedance_native_mode_does_not_require_tts_credentials(self):
        (self.root / ".env").write_text(
            "DIALOGUE_AUDIO_MODE=seedance_native\n"
            "ARK_API_KEY=secret-value\n"
            "VOLC_AK=ak\n"
            "VOLC_SK=sk\n"
            "TOS_BUCKET=bucket\n",
            encoding="utf-8",
        )

        status = self.service.config_status()
        tts = next(
            group for group in status["required_groups"] if group["id"] == "tts"
        )

        self.assertTrue(status["readiness"]["video"]["ready"])
        self.assertNotIn("VOLC_TTS_APPID", status["readiness"]["video"]["missing"])
        self.assertFalse(tts["applies"])
        self.assertFalse(tts["blocking"])
        self.assertTrue(tts["ready"])

    def test_asr_required_group_accepts_new_console_api_key(self):
        with (self.root / ".env").open("a", encoding="utf-8") as stream:
            stream.write("VOLC_ASR_API_KEY=new-api-key\n")

        status = self.service.config_status()
        asr = next(group for group in status["required_groups"] if group["id"] == "asr")

        self.assertTrue(asr["ready"])
        self.assertEqual(asr["missing"], [])
        field = next(
            item for item in status["fields"] if item["key"] == "VOLC_ASR_API_KEY"
        )
        self.assertTrue(field["secret"])
        self.assertNotIn("value", field)

    def test_publish_readiness_supports_direct_or_assisted_channels(self):
        readiness = self.service.config_status()["readiness"]["publish"]

        self.assertEqual(readiness["mode"], "direct_or_assisted")
        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["missing"], ["douyin"])

        (self.root / ".env").write_text(
            "ARK_API_KEY=secret-value\n"
            "WEIXIN_CHANNELS_ASSISTANT_URL=https://channels.weixin.qq.com/\n",
            encoding="utf-8",
        )
        configured = self.service.config_status()["readiness"]["publish"]
        self.assertTrue(configured["ready"])
        self.assertEqual(configured["mode"], "direct_or_assisted")
        self.assertEqual(configured["missing"], ["douyin"])

    def test_config_update_is_whitelisted_and_preserves_existing_secrets(self):
        result = self.service.update_config(
            {"ARK_VIDEO_RESOLUTION": "1080p", "NOT_ALLOWED": "bad"}
        )

        self.assertEqual(result["updated"], ["ARK_VIDEO_RESOLUTION"])
        content = (self.root / ".env").read_text(encoding="utf-8")
        self.assertIn("ARK_API_KEY=secret-value", content)
        self.assertIn("ARK_VIDEO_RESOLUTION=1080p", content)
        self.assertNotIn("NOT_ALLOWED", content)

    def test_real_person_asset_can_be_bound_and_deactivated(self):
        bound = self.service.bind_character(
            character_id="sales_xiaoling",
            display_name="小玲",
            asset_id="asset-202608120001-example",
        )
        self.assertEqual(bound["status"], "Active")
        self.assertEqual(bound["asset_uri"], "asset://asset-202608120001-example")

        inactive = self.service.set_character_status("sales_xiaoling", "Inactive")
        self.assertEqual(inactive["status"], "Inactive")

    def test_virtual_portrait_asset_can_be_bound_and_listed(self):
        bound = self.service.bind_character(
            character_id="host_virtual_01",
            display_name="Virtual Host",
            asset_id="asset-virtual-host-01",
            identity_type="virtual_person",
        )

        self.assertEqual(bound["status"], "Active")
        self.assertEqual(bound["identity_type"], "virtual_person")
        listed = self.service.characters()["characters"]
        self.assertEqual(listed[0]["identity_type"], "virtual_person")
        self.assertEqual(listed[0]["asset_uri"], "asset://asset-virtual-host-01")

    def test_character_binding_rejects_unknown_identity_type(self):
        with self.assertRaisesRegex(ValueError, "real_person.*virtual_person"):
            self.service.bind_character(
                character_id="bad",
                display_name="bad",
                asset_id="asset-bad",
                identity_type="unknown",
            )

    def test_character_library_filters_and_exposes_preview_and_usage(self):
        self.service.bind_character(
            character_id="host_virtual_01",
            display_name="虚拟主播",
            asset_id="asset-virtual-host-01",
            identity_type="virtual_person",
        )
        self.service.bind_character(
            character_id="sales_xiaoling",
            display_name="小玲",
            asset_id="asset-real-xiaoling",
        )
        self.database.execute(
            "UPDATE character_asset_bindings SET metadata_json=? WHERE character_id=?",
            (json.dumps({"ThumbnailUrl": "https://example.com/avatar.png"}),
             "host_virtual_01"),
        )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,characters_json) "
            "VALUES('J-CHAR-USE','T-CHAR','虚拟人讲解','STORYBOARDED',?)",
            (json.dumps([{"id": "host_virtual_01",
                          "asset_id": "asset-virtual-host-01"}]),),
        )
        self.database.commit()

        result = self.service.characters(
            keyword="主播", identity_type="virtual_person", status="Active"
        )

        self.assertEqual(result["total"], 1)
        item = result["characters"][0]
        self.assertIsNone(item["preview_url"])
        self.assertEqual(item["usage_count"], 1)
        self.assertFalse(item["deletable"])
        self.assertEqual(item["usages"][0]["job_id"], "J-CHAR-USE")

    @mock.patch("factory.management_api.volc_assets.VolcAssetClient")
    @mock.patch("factory.management_api.httpx.get")
    def test_binding_character_fetches_and_caches_volc_asset_preview(
        self, http_get, client_type
    ):
        client_type.return_value.get_asset.return_value = {
            "Id": "asset-preview-host",
            "URL": "https://example.com/volc-preview.png",
            "AssetType": "Image",
            "Status": "Active",
        }
        http_get.return_value = SimpleNamespace(
            content=b"fake-png",
            headers={"content-type": "image/png"},
            raise_for_status=mock.Mock(),
        )

        bound = self.service.bind_character(
            character_id="preview_host",
            display_name="预览主播",
            asset_id="asset-preview-host",
            identity_type="virtual_person",
        )

        self.assertEqual(bound["preview_sync"], "synced")
        self.assertEqual(bound["preview_url"], "/api/characters/preview_host/preview")
        listed = self.service.characters(keyword="preview_host")["characters"][0]
        self.assertEqual(listed["preview_url"], "/api/characters/preview_host/preview")
        self.assertEqual(
            self.service.character_preview_file("preview_host").read_bytes(),
            b"fake-png",
        )
        metadata = json.loads(self.database.execute(
            "SELECT metadata_json FROM character_asset_bindings "
            "WHERE character_id='preview_host'"
        ).fetchone()[0])
        self.assertEqual(metadata["AssetType"], "Image")

    @mock.patch("factory.management_api.volc_assets.VolcAssetClient")
    @mock.patch("factory.management_api.httpx.get")
    def test_historical_character_preview_can_be_refreshed(self, http_get, client_type):
        self.database.execute(
            "INSERT INTO character_asset_bindings(character_id,display_name,identity_type,"
            "asset_id,asset_uri,source,status,metadata_json) "
            "VALUES('legacy_host','历史主播','virtual_person','asset-legacy',"
            "'asset://asset-legacy','manual','Active','{}')"
        )
        self.database.commit()
        client_type.return_value.get_asset.return_value = {
            "Id": "asset-legacy", "URL": "https://example.com/legacy.png"
        }
        http_get.return_value = SimpleNamespace(
            content=b"legacy-png",
            headers={"content-type": "image/png"},
            raise_for_status=mock.Mock(),
        )

        refreshed = self.service.refresh_character("legacy_host")

        self.assertEqual(refreshed["preview_url"], "/api/characters/legacy_host/preview")
        self.assertEqual(refreshed["preview_sync"], "synced")
        client_type.return_value.close.assert_called_once_with()

    @mock.patch("factory.management_api.volc_assets.VolcAssetClient")
    def test_binding_survives_unavailable_preview_api(self, client_type):
        from factory.clients.volc_assets import AssetApiUnavailable
        client_type.side_effect = AssetApiUnavailable("not configured")

        bound = self.service.bind_character(
            character_id="offline_host",
            display_name="离线角色",
            asset_id="asset-offline-host",
        )

        self.assertEqual(bound["preview_sync"], "unavailable")
        self.assertIsNone(bound["preview_url"])
        self.assertEqual(self.service.characters()["total"], 1)

    @mock.patch("factory.management_api.volc_assets.VolcAssetClient")
    @mock.patch("factory.management_api.httpx.get")
    def test_virtual_character_background_is_loaded_from_asset_group(
        self, http_get, client_type
    ):
        client = client_type.return_value
        client.get_asset.return_value = {
            "Id": "asset-virtual-story",
            "URL": "https://example.com/virtual.png",
            "GroupId": "group-virtual-story",
        }
        client.get_asset_group.return_value = {
            "Id": "group-virtual-story",
            "Name": "中国 24岁 女 戏楼名伶",
            "Description": "空灵孤冷的戏楼名伶，只为知音唱半折古调。",
        }
        http_get.return_value = SimpleNamespace(
            content=b"virtual-image", headers={"content-type": "image/png"},
            raise_for_status=mock.Mock(),
        )

        bound = self.service.bind_character(
            character_id="virtual_story",
            display_name="名伶",
            asset_id="asset-virtual-story",
            identity_type="virtual_person",
        )

        self.assertEqual(bound["background_description"], "空灵孤冷的戏楼名伶，只为知音唱半折古调。")
        client.get_asset_group.assert_called_once_with("group-virtual-story")
        listed = self.service.characters(keyword="virtual_story")["characters"][0]
        self.assertEqual(listed["background_description_source"], "volc_asset_group")

    def test_real_character_background_can_be_saved_and_updated(self):
        with mock.patch.object(
            self.service, "_fetch_character_metadata", return_value=({}, "unavailable")
        ):
            self.service.bind_character(
                character_id="sales_real",
                display_name="销售顾问",
                asset_id="asset-real-sales",
                background_description="设备租赁行业的一线销售，表达干练可信。",
            )

        updated = self.service.update_character_background(
            "sales_real", "深耕设备租赁十年，擅长用真实案例解释经营问题。"
        )

        self.assertEqual(updated["background_description_source"], "manual")
        listed = self.service.characters(keyword="sales_real")["characters"][0]
        self.assertEqual(
            listed["background_description"],
            "深耕设备租赁十年，擅长用真实案例解释经营问题。",
        )

    def test_custom_character_requires_description_and_activates_after_image_upload(self):
        with self.assertRaisesRegex(ValueError, "人物描述"):
            self.service.create_custom_character(
                character_id="custom_empty", display_name="空角色",
                gender="female",
                background_description="",
            )
        created = self.service.create_custom_character(
            character_id="custom_klein", display_name="克莱恩",
            gender="male",
            background_description="维多利亚时代的年轻绅士与神秘学调查者。",
        )
        self.assertEqual(created["identity_type"], "custom")
        self.assertEqual(created["status"], "Inactive")
        image_one = self.root / "klein-portrait.jpg"
        image_two = self.root / "klein-scene.png"
        image_one.write_bytes(b"jpeg-reference-one")
        image_two.write_bytes(b"png-reference-two")

        uploaded_one = self.service.add_custom_character_image(
            "custom_klein", image_one, original_name="portrait.jpg"
        )
        self.service.add_custom_character_image(
            "custom_klein", image_two, original_name="scene.png"
        )

        self.assertEqual(uploaded_one["status"], "Active")
        listed = self.service.characters(identity_type="custom")["characters"][0]
        self.assertEqual(listed["image_count"], 2)
        self.assertEqual(
            listed["preview_url"],
            f"/api/characters/custom_klein/preview?v={uploaded_one['image_id']}",
        )
        self.assertEqual(
            self.service.character_preview_file("custom_klein").read_bytes(),
            b"jpeg-reference-one",
        )

    def test_custom_character_library_accepts_more_source_photos_than_model_refs(self):
        self.service.create_custom_character(
            character_id="custom_many", display_name="多角度角色",
            gender="female", background_description="多角度人物参考。",
        )
        for index in range(13):
            source = self.root / f"many-{index:02d}.jpg"
            source.write_bytes(f"image-{index}".encode())
            self.service.add_custom_character_image("custom_many", source)

        listed = self.service.characters(identity_type="custom")["characters"][0]
        self.assertEqual(listed["image_count"], 13)

    def test_custom_character_is_written_as_local_multi_image_reference(self):
        self.service.create_custom_character(
            character_id="custom_audrey", display_name="奥黛丽",
            gender="female",
            background_description="敏锐而善良的贵族小姐。",
        )
        first = self.root / "audrey-1.jpg"
        second = self.root / "audrey-2.webp"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        self.service.add_custom_character_image("custom_audrey", first)
        self.service.add_custom_character_image("custom_audrey", second)
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-CUSTOM','T-CUSTOM','自定义角色','STORYBOARDED','{}')"
        )
        self.database.commit()

        self.service.configure_video("J-CUSTOM", {
            "character_id": "custom_audrey", "orientation": "9:16",
            "production_profile": "standard", "subtitle_style": "clean",
            "asr_enabled": True, "budget_cents": 1800,
        })

        character = json.loads(self.database.execute(
            "SELECT characters_json FROM jobs WHERE job_id='J-CUSTOM'"
        ).fetchone()[0])[0]
        self.assertEqual(character["identity_type"], "custom")
        self.assertEqual(character["gender"], "female")
        self.assertNotIn("seedance_asset_uri", character)
        self.assertEqual(len(character["reference_images"]), 2)
        self.assertTrue(Path(character["image_path"]).exists())
        self.assertEqual(
            character["seedance_reference_mode"], "description_only"
        )

    def test_deleting_custom_character_removes_local_reference_images(self):
        self.service.create_custom_character(
            character_id="custom_alger", display_name="阿尔杰",
            gender="male",
            background_description="经验丰富的航海家。",
        )
        source = self.root / "alger.jpg"
        source.write_bytes(b"alger")
        uploaded = self.service.add_custom_character_image("custom_alger", source)
        saved = self.service.character_image_file(
            "custom_alger", uploaded["image_id"]
        )
        self.assertTrue(saved.exists())

        self.service.delete_character("custom_alger")

        self.assertFalse(saved.exists())

    def test_video_configuration_copies_character_background_into_storyboard(self):
        with mock.patch.object(
            self.service, "_fetch_character_metadata", return_value=({}, "unavailable")
        ):
            self.service.bind_character(
                character_id="host_context",
                display_name="业务主播",
                asset_id="asset-host-context",
                background_description="懂设备租赁经营的专业主播。",
            )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-CONTEXT','T-CONTEXT','角色背景透传','STORYBOARDED','{}')"
        )
        self.database.commit()

        self.service.configure_video("J-CONTEXT", {
            "character_id": "host_context", "orientation": "9:16",
            "production_profile": "standard", "subtitle_style": "clean",
            "asr_enabled": True, "budget_cents": 1800,
        })

        character = json.loads(self.database.execute(
            "SELECT characters_json FROM jobs WHERE job_id='J-CONTEXT'"
        ).fetchone()[0])[0]
        self.assertEqual(character["background_description"], "懂设备租赁经营的专业主播。")

    def test_unused_character_can_be_deleted(self):
        self.service.bind_character(
            character_id="unused_host",
            display_name="备用主播",
            asset_id="asset-unused-host",
            identity_type="virtual_person",
        )

        result = self.service.delete_character("unused_host")

        self.assertTrue(result["deleted"])
        self.assertIsNone(self.database.execute(
            "SELECT 1 FROM character_asset_bindings WHERE character_id='unused_host'"
        ).fetchone())

    def test_used_character_cannot_be_deleted(self):
        self.service.bind_character(
            character_id="used_host",
            display_name="在用主播",
            asset_id="asset-used-host",
            identity_type="virtual_person",
        )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,characters_json) "
            "VALUES('J-USED-HOST','T-USED','已绑定角色的视频','COMPOSED',?)",
            (json.dumps([{"id": "used_host"}]),),
        )
        self.database.commit()

        with self.assertRaisesRegex(ValueError, "已被.*视频任务使用"):
            self.service.delete_character("used_host")

        self.assertIsNotNone(self.database.execute(
            "SELECT 1 FROM character_asset_bindings WHERE character_id='used_host'"
        ).fetchone())

    def test_manual_source_is_archived_through_management_service(self):
        result = self.service.add_source(
            source_type="manual",
            title="行业访谈",
            content="设备租赁的核心不是设备，而是周转效率。",
        )

        self.assertTrue(result["source_id"].startswith("SRC-"))
        row = self.database.execute(
            "SELECT title,source_type FROM script_sources WHERE source_id=?",
            (result["source_id"],),
        ).fetchone()
        self.assertEqual(row["title"], "行业访谈")
        self.assertEqual(row["source_type"], "manual")

    def test_duplicate_manual_source_creates_an_independent_content_project(self):
        first = self.service.add_source(
            source_type="manual",
            title="第一版项目",
            content="同一份内容也可以用于不同的选题与剧本方向。",
        )

        second = self.service.add_source(
            source_type="manual",
            title="第二版项目",
            content="同一份内容也可以用于不同的选题与剧本方向。",
        )

        self.assertNotEqual(first["source_id"], second["source_id"])
        self.assertTrue(second["created"])
        self.assertTrue(second["reused_source"])
        projects = self.service.content_projects()["projects"]
        self.assertEqual(
            {project["project_id"] for project in projects},
            {first["source_id"], second["source_id"]},
        )
        second_row = self.database.execute(
            "SELECT title,metadata_json FROM script_sources WHERE source_id=?",
            (second["source_id"],),
        ).fetchone()
        self.assertEqual(second_row["title"], "第二版项目")
        self.assertEqual(
            json.loads(second_row["metadata_json"])["canonical_source_id"],
            first["source_id"],
        )

    def test_content_projects_keep_source_brief_script_video_and_publish_together(self):
        self.database.execute(
            "INSERT INTO script_sources(source_id,fingerprint,source_type,title,content) "
            "VALUES('SRC-P','fp-p','manual','行业材料','足够完整的设备租赁行业材料，用于生成可靠选题和视频内容。')"
        )
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json,approved_job_id) "
            "VALUES('BRIEF-P','approved','租赁经营真相','{}','J-P')"
        )
        self.database.execute(
            "INSERT INTO topic_brief_sources(brief_id,source_id) VALUES('BRIEF-P','SRC-P')"
        )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) VALUES('J-P','T-P','租赁经营真相','READY_TO_PUBLISH')"
        )
        self.database.execute(
            "INSERT INTO script_drafts(draft_id,brief_id,variant_index,status,request_fingerprint,script_json,approved_job_id) "
            "VALUES('SCRIPT-P','BRIEF-P',0,'approved','fp-d','{}','J-P')"
        )
        self.database.execute(
            "INSERT INTO platform_publications(publication_id,job_id,platform,item_id) "
            "VALUES('PUB-P','J-P','douyin','item-p')"
        )
        self.database.commit()

        project = self.service.content_projects()["projects"][0]

        self.assertEqual(project["source"]["source_id"], "SRC-P")
        self.assertEqual(project["brief"]["brief_id"], "BRIEF-P")
        self.assertEqual(project["selected_script_id"], "SCRIPT-P")
        self.assertEqual(project["video"]["job_id"], "J-P")
        self.assertEqual(project["published_platforms"], ["douyin"])
        self.assertEqual(project["next_action"], "continue_publish")

    def test_content_project_keeps_user_entered_name_after_brief_generation(self):
        self.database.execute(
            "INSERT INTO script_sources(source_id,fingerprint,source_type,title,content) "
            "VALUES('SRC-NAMED','fp-named','manual','用户填写的项目名','足够完整的内容来源')"
        )
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json) "
            "VALUES('BRIEF-NAMED','candidate','AI 生成的选题标题','{}')"
        )
        self.database.execute(
            "INSERT INTO topic_brief_sources(brief_id,source_id) "
            "VALUES('BRIEF-NAMED','SRC-NAMED')"
        )
        self.database.commit()

        project = self.service.content_projects()["projects"][0]

        self.assertEqual(project["title"], "用户填写的项目名")
        self.assertEqual(project["brief"]["recommended_title"], "AI 生成的选题标题")

    def test_content_project_hides_brief_placeholder_job_until_script_is_approved(self):
        self.database.execute(
            "INSERT INTO script_sources(source_id,fingerprint,source_type,title,content) "
            "VALUES('SRC-GHOST','fp-ghost','manual','待审核内容','足够完整的内容来源。')"
        )
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json,approved_job_id) "
            "VALUES('BRIEF-GHOST','approved','待审核选题','{}','J-GHOST')"
        )
        self.database.execute(
            "INSERT INTO topic_brief_sources(brief_id,source_id) "
            "VALUES('BRIEF-GHOST','SRC-GHOST')"
        )
        self.database.execute(
            "INSERT INTO script_drafts(draft_id,brief_id,variant_index,status,"
            "request_fingerprint,script_json) "
            "VALUES('SCRIPT-GHOST','BRIEF-GHOST',0,'candidate','fp-ghost-draft','{}')"
        )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) "
            "VALUES('J-GHOST','T-GHOST','旧占位任务','PENDING')"
        )
        self.database.commit()

        project = self.service.content_projects()["projects"][0]

        self.assertIsNone(project["video"])
        self.assertEqual(project["next_action"], "review_script")

    def test_content_project_with_brief_but_no_drafts_returns_to_script_generation(self):
        self.database.execute(
            "INSERT INTO script_sources(source_id,fingerprint,source_type,title,content) "
            "VALUES('SRC-NO-DRAFT','fp-no-draft','manual','待生成剧本内容','足够完整的内容来源，用于生成选题简报和候选剧本。')"
        )
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json) "
            "VALUES('BRIEF-NO-DRAFT','candidate','待生成剧本选题','{}')"
        )
        self.database.execute(
            "INSERT INTO topic_brief_sources(brief_id,source_id) "
            "VALUES('BRIEF-NO-DRAFT','SRC-NO-DRAFT')"
        )
        self.database.commit()

        project = self.service.content_projects()["projects"][0]

        self.assertEqual(project["stage"], "brief")
        self.assertEqual(project["next_action"], "generate_scripts")

    def test_content_project_uses_approved_script_job_as_canonical_video(self):
        self.database.execute(
            "INSERT INTO script_sources(source_id,fingerprint,source_type,title,content) "
            "VALUES('SRC-CANONICAL','fp-canonical','manual','已审核内容','足够完整的内容来源。')"
        )
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json,approved_job_id) "
            "VALUES('BRIEF-CANONICAL','approved','已审核选题','{}','J-OLD')"
        )
        self.database.execute(
            "INSERT INTO topic_brief_sources(brief_id,source_id) "
            "VALUES('BRIEF-CANONICAL','SRC-CANONICAL')"
        )
        self.database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state) VALUES(?,?,?,?)",
            [
                ("J-OLD", "T-OLD", "旧任务", "PENDING"),
                ("J-CURRENT", "T-CURRENT", "当前任务", "STORYBOARDED"),
            ],
        )
        self.database.execute(
            "INSERT INTO script_drafts(draft_id,brief_id,variant_index,status,"
            "request_fingerprint,script_json,approved_job_id) "
            "VALUES('SCRIPT-CANONICAL','BRIEF-CANONICAL',0,'approved',"
            "'fp-canonical-draft','{}','J-CURRENT')"
        )
        self.database.commit()

        project = self.service.content_projects()["projects"][0]

        self.assertEqual(project["video"]["job_id"], "J-CURRENT")
        self.assertEqual(project["next_action"], "configure_video")

    def test_low_quality_source_requires_explicit_risk_confirmation(self):
        result = self.service.add_source(source_type="manual", title="数字", content="2312")
        source_id = result["source_id"]

        quality = self.service.content_projects()["projects"][0]["quality_gate"]
        self.assertFalse(quality["passed"])
        self.assertEqual(quality["level"], "blocked")
        with self.assertRaisesRegex(ValueError, "内容不足"):
            self.service.generate_brief_from_source(source_id)

        with mock.patch("factory.management_api.topic_briefs.generate_brief", return_value={"brief_id": "B"}) as generate:
            response = self.service.generate_brief_from_source(source_id, risk_confirmed=True)
        self.assertEqual(response["brief_id"], "B")
        generate.assert_called_once()

    def test_video_production_settings_are_saved_before_paid_generation(self):
        self.database.execute(
            "INSERT INTO character_asset_bindings(character_id,display_name,asset_id,asset_uri,source,status) "
            "VALUES('sales_xiaoling','小玲','asset-x','asset://asset-x','manual','Active')"
        )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-SET','T-SET','设置视频','STORYBOARDED','{}')"
        )
        self.database.commit()

        result = self.service.configure_video("J-SET", {
            "character_id": "sales_xiaoling", "orientation": "9:16",
            "production_profile": "standard", "subtitle_style": "clean",
            "audio_mode": "seedance_native",
            "asr_enabled": True, "budget_cents": 1800,
        })

        self.assertEqual(result["production_settings"]["character_id"], "sales_xiaoling")
        storyboard = json.loads(self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id='J-SET'"
        ).fetchone()[0])
        self.assertEqual(storyboard["characters"][0]["name"], "小玲")
        self.assertEqual(storyboard["production_settings"]["budget_cents"], 1800)
        self.assertEqual(
            storyboard["production_settings"]["audio_mode"], "seedance_native"
        )
        self.assertEqual(
            self.service.videos()["jobs"][0]["preflight"]["voice_plan"]["mode"],
            "seedance_native",
        )
        self.assertEqual(self.service.videos()["jobs"][0]["preflight"]["budget_cents"], 1800)

    def test_generation_snapshot_keeps_preflight_stable_after_global_config_changes(self):
        self.root.joinpath(".env").write_text(
            "ARK_API_KEY=secret-value\n"
            "ARK_VIDEO_PRIMARY_MODEL=doubao-seedance-2-5-260628\n"
            "ARK_VIDEO_RESOLUTION=1080p\n"
            "ARK_VIDEO_OUTPUT_FORMAT=mp4\n"
            "SEEDANCE_PRODUCTION_REFERENCE_VIDEO_BUDGET=4\n"
            "SEEDANCE_MAX_CONCURRENT_MASTERS=2\n"
            "SEEDANCE_AUTO_RETRY_BUDGET_CENTS=900\n"
            "SEEDANCE_MAX_AUTO_RETRIES=1\n",
            encoding="utf-8",
        )
        storyboard = {
            "production_mode": "native_master_local_director",
            "masters": [{
                "idx": 1, "type": "broll", "duration": 4,
                "scene": "night equipment yard", "asset_tags": [],
            }],
            "characters": [],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-SNAPSHOT','T-SNAPSHOT','snapshot','STORYBOARDED',?)",
            (json.dumps(storyboard),),
        )
        self.database.commit()

        configured = self.service.configure_video("J-SNAPSHOT", {
            "character_mappings": {}, "orientation": "9:16",
            "production_profile": "standard", "subtitle_style": "clean",
            "audio_mode": "seedance_native", "asr_enabled": True,
            "budget_cents": 5000,
        })
        snapshot = configured["production_settings"][
            production_config.GENERATION_SNAPSHOT_KEY
        ]
        self.assertEqual(configured["master_dependency_graph"]["waves"], [[1]])
        self.assertEqual(snapshot, {
            "version": 1,
            "model": "doubao-seedance-2-5-260628",
            "resolution": "1080p",
            "output_format": "mp4",
            "ratio": "9:16",
            "audio_mode": "seedance_native",
            "reference_video_budget": 4,
            "max_concurrent_masters": 2,
            "qa_enabled": True,
            "max_auto_retries": 1,
            "auto_retry_budget_cents": 900,
            "job_budget_cents": 5000,
        })

        self.root.joinpath(".env").write_text(
            "ARK_API_KEY=secret-value\n"
            "ARK_VIDEO_RESOLUTION=480p\n"
            "SEEDANCE_PRODUCTION_REFERENCE_VIDEO_BUDGET=1\n"
            "SEEDANCE_MAX_CONCURRENT_MASTERS=1\n",
            encoding="utf-8",
        )
        job = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-SNAPSHOT"
        )
        self.assertEqual(job["preflight"]["generation_snapshot"], snapshot)
        self.assertEqual(
            job["preflight"]["paid_masters"][0]["estimated_cost_cents"],
            1376,
        )

    def test_generation_snapshot_rejects_mov_until_managed_conversion_exists(self):
        with self.assertRaisesRegex(ValueError, "managed pipeline only supports mp4"):
            production_config.freeze_generation_snapshot({
                "output_format": "mov",
            })

    def test_generation_snapshot_rejects_explicit_nonpositive_limits(self):
        cases = (
            ("reference_video_budget", 0),
            ("max_concurrent_masters", 0),
            ("budget_cents", 0),
        )
        for field, value in cases:
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "must be positive"):
                    production_config.freeze_generation_snapshot({field: value})

    def test_anchor_generators_are_snapshotted_per_video_job(self):
        storyboard = {
            "characters": [{
                "id": "customer", "name": "客户",
                "look": "年轻客户",
            }],
            "masters": [
                {
                    "idx": 1, "type": "dialogue", "speaker": "customer",
                    "duration": 4, "scene": "设备检修车间",
                    "scene_anchor_key": "repair-bay-a",
                },
                {
                    "idx": 2, "type": "dialogue", "speaker": "customer",
                    "duration": 4, "scene": "设备检修车间",
                    "scene_anchor_key": "repair-bay-a",
                },
            ],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-ANCHOR-MODEL','T-A','锚点模型对比','STORYBOARDED',?)",
            (json.dumps(storyboard, ensure_ascii=False),),
        )
        self.database.commit()

        result = self.service.configure_video("J-ANCHOR-MODEL", {
            "character_mappings": {"customer": ""},
            "character_anchor_generator": "seedance_2_5",
            "scene_anchor_generator": "seedance_2_5",
            "orientation": "9:16", "production_profile": "standard",
            "subtitle_style": "clean", "asr_enabled": True,
            "budget_cents": 5000,
        })

        settings = result["production_settings"]
        self.assertEqual(settings["character_anchor_generator"], "seedance_2_5")
        self.assertEqual(settings["scene_anchor_generator"], "seedance_2_5")
        video = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-ANCHOR-MODEL"
        )
        self.assertEqual(
            video["preflight"]["character_makeup_plan"][0]["generator"],
            "seedance_2_5",
        )
        self.assertEqual(
            video["character_makeup_plan"][0]["generator"], "seedance_2_5"
        )
        self.assertEqual(
            video["preflight"]["paid_scene_anchors"][0]["generator"],
            "seedance_2_5",
        )
        self.assertEqual(
            video["scene_anchor_plan"][0]["generator"], "seedance_2_5"
        )
        self.assertEqual(
            video["preflight"]["identity_anchor_cost_cents"], 612
        )
        self.assertEqual(video["preflight"]["scene_anchor_cost_cents"], 612)

    def test_anchor_generator_rejects_unknown_value(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-ANCHOR-BAD','T-A','bad','STORYBOARDED','{}')"
        )
        self.database.commit()

        with self.assertRaisesRegex(ValueError, "锚点生成模型"):
            self.service.configure_video("J-ANCHOR-BAD", {
                "character_anchor_generator": "unknown",
                "scene_anchor_generator": "seedream_5_0",
                "orientation": "9:16", "production_profile": "standard",
                "subtitle_style": "clean", "asr_enabled": True,
                "budget_cents": 5000,
            })

    def test_config_exposes_anchor_generator_select_options(self):
        config = self.service.config_status()
        fields = {field["key"]: field for field in config["fields"]}

        for key in ("CHARACTER_ANCHOR_GENERATOR", "SCENE_ANCHOR_GENERATOR"):
            self.assertEqual(fields[key]["value"], "seedance_2_5")
            self.assertEqual(
                [option["value"] for option in fields[key]["options"]],
                ["seedream_5_0", "seedance_2_5"],
            )

    def test_virtual_portrait_is_written_as_seedance_asset_reference(self):
        self.service.bind_character(
            character_id="host_virtual_01",
            display_name="Virtual Host",
            asset_id="asset-virtual-host-01",
            identity_type="virtual_person",
        )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-VIRTUAL','T-VIRTUAL','virtual','STORYBOARDED','{}')"
        )
        self.database.commit()

        self.service.configure_video("J-VIRTUAL", {
            "character_id": "host_virtual_01", "orientation": "9:16",
            "production_profile": "standard", "subtitle_style": "clean",
            "asr_enabled": True, "budget_cents": 1800,
        })

        storyboard = json.loads(self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id='J-VIRTUAL'"
        ).fetchone()[0])
        character = storyboard["characters"][0]
        self.assertEqual(character["identity_type"], "virtual_person")
        self.assertEqual(
            character["seedance_asset_uri"], "asset://asset-virtual-host-01"
        )
        runtime_characters = json.loads(self.database.execute(
            "SELECT characters_json FROM jobs WHERE job_id='J-VIRTUAL'"
        ).fetchone()[0])
        self.assertEqual(runtime_characters, storyboard["characters"])
        video = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-VIRTUAL"
        )
        self.assertEqual(video["character_makeup_plan"][0]["status"], "ready")
        self.assertEqual(video["preflight"]["paid_identity_anchor_count"], 0)

    def test_multi_speaker_video_maps_each_speaker_to_a_character_asset(self):
        for character_id, display_name, asset_id in (
            ("sales_xiaoling", "小玲", "asset-sales"),
            ("boss_virtual", "老板", "asset-boss"),
        ):
            self.database.execute(
                "INSERT INTO character_asset_bindings("
                "character_id,display_name,asset_id,asset_uri,source,status) "
                "VALUES(?,?,?,?, 'manual','Active')",
                (character_id, display_name, asset_id, f"asset://{asset_id}"),
            )
        storyboard = {
            "production_profile": "standard",
            "production_mode": "native_master_local_director",
            "characters": [
                {"id": "sales", "name": "销售", "gender": "female", "look": "销售"},
                {"id": "boss", "name": "老板", "gender": "male", "look": "老板"},
            ],
            "masters": [
                {"idx": 1, "type": "dialogue", "speaker": "sales", "duration": 8},
                {"idx": 2, "type": "dialogue", "speaker": "boss", "duration": 8},
            ],
            "shots": [], "hook": "对话", "bgm": {},
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-MULTI','T-MULTI','多人对话','STORYBOARDED',?)",
            (json.dumps(storyboard, ensure_ascii=False),),
        )
        self.database.commit()

        result = self.service.configure_video("J-MULTI", {
            "character_mappings": {
                "sales": "sales_xiaoling", "boss": "boss_virtual",
            },
            "orientation": "9:16", "production_profile": "standard",
            "subtitle_style": "clean", "asr_enabled": True,
            "budget_cents": 3000,
        })

        self.assertEqual(result["production_settings"]["character_mappings"], {
            "sales": "sales_xiaoling", "boss": "boss_virtual",
        })
        saved = json.loads(self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id='J-MULTI'"
        ).fetchone()[0])
        self.assertEqual(
            {item["id"]: item["asset_character_id"] for item in saved["characters"]},
            {"sales": "sales_xiaoling", "boss": "boss_virtual"},
        )
        self.assertEqual(
            {item["id"]: item["asset_id"] for item in saved["characters"]},
            {"sales": "asset-sales", "boss": "asset-boss"},
        )
        self.assertEqual(
            [tuple(row) for row in self.database.execute(
                "SELECT idx,speaker_id FROM shots WHERE job_id='J-MULTI' ORDER BY idx"
            ).fetchall()],
            [(1, "sales"), (2, "boss")],
        )

    def test_multi_speaker_video_uses_ai_generation_for_unmapped_characters(self):
        self.database.execute(
            "INSERT INTO character_asset_bindings("
            "character_id,display_name,asset_id,asset_uri,source,status) "
            "VALUES('sales_xiaoling','小玲','asset-sales','asset://asset-sales',"
            "'manual','Active')"
        )
        storyboard = {
            "characters": [
                {"id": "sales", "name": "销售", "look": "干练女销售"},
                {"id": "boss", "name": "老板", "look": "谨慎的中年老板"},
            ],
            "masters": [
                {"idx": 1, "type": "dialogue", "speaker": "sales", "duration": 4},
                {"idx": 2, "type": "dialogue", "speaker": "boss", "duration": 4},
            ],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-MULTI-MISSING','T-M','missing','STORYBOARDED',?)",
            (json.dumps(storyboard),),
        )
        self.database.commit()

        result = self.service.configure_video("J-MULTI-MISSING", {
            "character_mappings": {"sales": "sales_xiaoling", "boss": ""},
            "orientation": "9:16", "production_profile": "standard",
            "subtitle_style": "clean", "asr_enabled": True,
            "budget_cents": 3000,
        })

        self.assertEqual(result["production_settings"]["character_mappings"], {
            "sales": "sales_xiaoling", "boss": "",
        })
        saved = json.loads(self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id='J-MULTI-MISSING'"
        ).fetchone()[0])
        characters = {item["id"]: item for item in saved["characters"]}
        self.assertEqual(characters["sales"]["generation_mode"], "fixed_asset")
        self.assertEqual(characters["sales"]["asset_id"], "asset-sales")
        self.assertEqual(characters["boss"]["generation_mode"], "ai_generated")
        self.assertEqual(characters["boss"]["look"], "谨慎的中年老板")
        self.assertNotIn("asset_id", characters["boss"])
        video = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-MULTI-MISSING"
        )
        makeup = video["character_makeup_plan"]
        self.assertEqual(len(makeup), 2)
        ai_makeup = next(
            item for item in makeup
            if item["source"] == "seedance_identity_anchor"
        )
        self.assertEqual(ai_makeup["status"], "pending")
        self.assertEqual(ai_makeup["estimated_cost_cents"], 612)
        self.assertEqual(video["preflight"]["paid_identity_anchor_count"], 1)
        self.assertEqual(video["preflight"]["identity_anchor_cost_cents"], 612)

    def test_single_dialogue_master_keeps_every_structured_speaker_on_configure(self):
        self.database.execute(
            "INSERT INTO character_asset_bindings("
            "character_id,display_name,asset_id,asset_uri,source,status) "
            "VALUES('boss_virtual','老大','asset-boss','asset://asset-boss',"
            "'manual','Active')"
        )
        storyboard = {
            "production_profile": "standard",
            "characters": [
                {"id": "女性工作人员", "name": "女性工作人员", "look": "安全员"},
                {"id": "非洲客户", "name": "非洲客户", "look": "年轻男性客户"},
            ],
            "masters": [{
                "idx": 1,
                "type": "dialogue",
                "speaker": "女性工作人员",
                "speakers": ["女性工作人员", "非洲客户"],
                "cast": ["女性工作人员", "非洲客户"],
                "duration": 25.5,
                "performance_beats": [
                    {"speaker": "女性工作人员", "dialogue": "欢迎"},
                    {"speaker": "非洲客户", "dialogue": "Good Good Good"},
                ],
            }],
            "shots": [{
                "idx": 1,
                "duration": 10,
                "video_master_idx": 1,
                "audio_master_idx": 1,
                "video_start": 0,
                "audio_start": 0,
            }],
            "hook": "欢迎",
            "bgm": {},
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-ENSEMBLE-CONFIG','T-E','单母片多人对白','STORYBOARDED',?)",
            (json.dumps(storyboard, ensure_ascii=False),),
        )
        self.database.commit()

        result = self.service.configure_video("J-ENSEMBLE-CONFIG", {
            "character_mappings": {"女性工作人员": "boss_virtual"},
            "orientation": "9:16",
            "production_profile": "standard",
            "subtitle_style": "clean",
            "asr_enabled": True,
            "budget_cents": 5000,
        })

        self.assertEqual(result["production_settings"]["character_mappings"], {
            "女性工作人员": "boss_virtual",
            "非洲客户": "",
        })
        saved = json.loads(self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id='J-ENSEMBLE-CONFIG'"
        ).fetchone()[0])
        characters = {item["id"]: item for item in saved["characters"]}
        self.assertEqual(set(characters), {"女性工作人员", "非洲客户"})
        self.assertEqual(characters["女性工作人员"]["generation_mode"], "fixed_asset")
        self.assertEqual(characters["非洲客户"]["generation_mode"], "ai_generated")
        self.assertEqual(characters["非洲客户"]["look"], "年轻男性客户")

    def test_configure_repairs_structured_speaker_missing_from_legacy_characters(self):
        storyboard = {
            "production_profile": "standard",
            "production_mode": "native_master_local_director",
            "characters": [{
                "id": "女性工作人员",
                "name": "女性工作人员",
                "gender": "female",
                "look": "安全员",
            }],
            "masters": [{
                "idx": 1,
                "type": "dialogue",
                "speaker": "女性工作人员",
                "speakers": ["女性工作人员", "非洲客户"],
                "cast": ["女性工作人员", "非洲客户"],
                "duration": 10,
                "render_mode": "cinematic_character",
                "scene": "设备停车场，两人交流",
                "action": "女性介绍，客户回应",
                "voiceover": "欢迎。Good Good Good。",
                "performance_beats": [
                    {"speaker": "女性工作人员", "dialogue": "欢迎"},
                    {"speaker": "非洲客户", "dialogue": "Good Good Good"},
                ],
            }],
            "shots": [{
                "idx": 1,
                "duration": 10,
                "video_master_idx": 1,
                "audio_master_idx": 1,
                "video_start": 0,
                "audio_start": 0,
            }],
            "hook": "欢迎",
            "bgm": {},
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-LEGACY-ENSEMBLE','T-E','历史多人对白','STORYBOARDED',?)",
            (json.dumps(storyboard, ensure_ascii=False),),
        )
        self.database.commit()

        self.service.configure_video("J-LEGACY-ENSEMBLE", {
            "character_mappings": {
                "女性工作人员": "",
                "非洲客户": "",
            },
            "orientation": "9:16",
            "production_profile": "standard",
            "subtitle_style": "clean",
            "asr_enabled": True,
            "budget_cents": 5000,
        })

        saved = json.loads(self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id='J-LEGACY-ENSEMBLE'"
        ).fetchone()[0])
        customer = next(
            item for item in saved["characters"] if item["id"] == "非洲客户"
        )
        self.assertEqual(customer["generation_mode"], "ai_generated")
        self.assertEqual(customer.get("gender"), "unspecified")
        self.assertTrue(customer.get("look"))
        from factory.steps import s1_storyboard
        s1_storyboard._validate(saved)

    def test_configure_video_materializes_scene_and_blocking_anchors(self):
        self.database.execute(
            "INSERT INTO character_asset_bindings("
            "character_id,display_name,identity_type,asset_id,asset_uri,source,status) "
            "VALUES('boss-fixed','老大','virtual_person','asset-boss',"
            "'asset://asset-boss','manual','Active')"
        )
        characters = [
            {"id": "女员工-近景", "name": "女员工-近景", "look": "女员工"},
            {"id": "女员工-中景", "name": "女员工-中景", "look": "女员工"},
            {"id": "客户", "name": "客户", "look": "灰色毛衣客户"},
        ]
        masters = [
            {
                "idx": 1, "type": "dialogue", "speaker": "女员工-近景",
                "duration": 4, "scene": "场景：户外设备停放场；"
                "出镜人物：女员工-近景、客户；人物动作：二人站立",
            },
            {
                "idx": 2, "type": "dialogue", "speaker": "女员工-中景",
                "duration": 4, "scene": "场景：户外设备停放场，水泥地面；"
                "出镜人物：女员工-中景、客户；人物动作：二人点赞",
            },
            {
                "idx": 3, "type": "dialogue", "speaker": "客户",
                "duration": 4, "scene": "场景：户外设备停放场，黄色吊车背景；"
                "出镜人物：客户、女员工-中景；人物动作：客户说话",
            },
        ]
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-CONTINUITY','T-CONTINUITY','连续性','STORYBOARDED',?)",
            (json.dumps({"characters": characters, "masters": masters}),),
        )
        self.database.commit()

        self.service.configure_video("J-CONTINUITY", {
            "character_mappings": {
                "女员工-近景": "boss-fixed",
                "女员工-中景": "boss-fixed",
                "客户": "",
            },
            "orientation": "9:16", "production_profile": "standard",
            "subtitle_style": "clean", "asr_enabled": True,
            "budget_cents": 5000,
        })

        saved = json.loads(self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id='J-CONTINUITY'"
        ).fetchone()[0])
        self.assertEqual(
            len({master["scene_anchor_key"] for master in saved["masters"]}),
            1,
        )
        self.assertEqual(
            len(saved["masters"][0]["blocking_anchor"]["positions"]), 2
        )
        video = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-CONTINUITY"
        )
        self.assertEqual(video["preflight"]["paid_scene_anchor_count"], 1)
        self.assertEqual(
            [item["screen_position"] for item in
             video["scene_anchor_plan"][0]["blocking_anchor"]["positions"]],
            ["screen-left", "screen-right"],
        )

    def test_video_exposes_repeated_scene_anchor_with_preview(self):
        preview = self.root / "data" / "repair-bay.png"
        preview.write_bytes(b"png")
        storyboard = {
            "characters": [],
            "masters": [
                {"idx": 1, "type": "dialogue", "duration": 4,
                 "scene": "设备检修车间", "scene_anchor_key": "repair-bay-a"},
                {"idx": 2, "type": "dialogue", "duration": 4,
                 "scene": "设备检修车间", "scene_anchor_key": "repair-bay-a"},
            ],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,?,?)",
            ("J-SCENE-UI", "T-SCENE-UI", "场景锚点", "STORYBOARDED",
             json.dumps(storyboard, ensure_ascii=False)),
        )
        self.database.execute(
            "INSERT INTO scene_visual_anchors("
            "job_id,anchor_key,description,master_indexes,state,image_path,"
            "preview_path,cost_cents) VALUES(?,?,?,?,?,?,?,?)",
            ("J-SCENE-UI", "repair-bay-a", "设备检修车间", "[1,2]",
             "READY", str(preview), str(preview), 29),
        )
        self.database.commit()

        video = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-SCENE-UI"
        )
        anchor = video["scene_anchor_plan"][0]

        self.assertEqual(anchor["status"], "ready")
        self.assertEqual(anchor["approval_state"], "pending_review")
        self.assertEqual(anchor["master_indexes"], [1, 2])
        self.assertEqual(anchor["cost_cents"], 29)
        self.assertIn("/scenes/", anchor["preview_url"])
        self.assertEqual(
            self.service.scene_anchor_preview_file(
                "J-SCENE-UI", scene_assets.anchor_token("repair-bay-a")
            ),
            preview,
        )

    def test_video_exposes_project_site_anchor_scope(self):
        storyboard = {
            "characters": [],
            "masters": [
                {
                    "idx": 1, "type": "dialogue", "duration": 4,
                    "scene_anchor_key": "loading-yard-night",
                    "site_anchor_key": "coastal-factory-site",
                    "site_anchor_description": "沿海城市工厂项目现场",
                },
                {
                    "idx": 2, "type": "dialogue", "duration": 4,
                    "scene_anchor_key": "production-line-evening",
                    "site_anchor_key": "coastal-factory-site",
                    "site_anchor_description": "沿海城市工厂项目现场",
                },
            ],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,?,?)",
            ("J-SITE-UI", "T-SITE-UI", "项目现场锚点", "STORYBOARDED",
             json.dumps(storyboard, ensure_ascii=False)),
        )
        self.database.commit()

        video = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-SITE-UI"
        )
        anchor = video["scene_anchor_plan"][0]

        self.assertEqual(anchor["anchor_key"], "coastal-factory-site")
        self.assertEqual(anchor["anchor_scope"], "site")
        self.assertEqual(anchor["master_indexes"], [1, 2])
        self.assertEqual(anchor["blocking_anchor"], {})

    def test_approve_continuity_anchors_queues_job_after_every_anchor_is_ready(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-ANCHOR-REVIEW','T-ANCHOR-REVIEW','锚点审核',"
            "'ANCHOR_REVIEW_REQUIRED','{}')"
        )
        self.database.execute(
            "INSERT INTO character_identity_anchors("
            "job_id,identity_key,character_id,display_name,source_type,state,"
            "approval_state) VALUES(?,?,?,?,?,'READY','PENDING_REVIEW')",
            ("J-ANCHOR-REVIEW", "script:lead", "lead", "主角", "seedream_identity_image"),
        )
        self.database.execute(
            "INSERT INTO scene_visual_anchors("
            "job_id,anchor_key,description,master_indexes,state,approval_state) "
            "VALUES(?,?,?,?, 'READY','PENDING_REVIEW')",
            ("J-ANCHOR-REVIEW", "scene-yard", "设备停车场", "[1,2]"),
        )
        self.database.commit()

        with mock.patch.object(
            self.service, "dispatch_production_queue",
            return_value={"dispatched_jobs": ["J-ANCHOR-REVIEW"]},
        ):
            result = self.service.approve_continuity_anchors(
                "J-ANCHOR-REVIEW", note="人物和场景符合预期"
            )

        self.assertEqual(result["state"], "PRODUCTION_QUEUED")
        self.assertEqual(result["dispatched_jobs"], ["J-ANCHOR-REVIEW"])
        states = {
            row[0] for row in self.database.execute(
                "SELECT approval_state FROM character_identity_anchors "
                "WHERE job_id='J-ANCHOR-REVIEW' UNION ALL "
                "SELECT approval_state FROM scene_visual_anchors "
                "WHERE job_id='J-ANCHOR-REVIEW'"
            ).fetchall()
        }
        self.assertEqual(states, {"APPROVED"})

    def test_approve_continuity_anchors_rejects_incomplete_anchor_generation(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-ANCHOR-WAIT','T-ANCHOR-WAIT','锚点未完成',"
            "'ANCHOR_REVIEW_REQUIRED','{}')"
        )
        self.database.execute(
            "INSERT INTO scene_visual_anchors("
            "job_id,anchor_key,description,master_indexes,state,approval_state) "
            "VALUES(?,?,?,?, 'GENERATING','PENDING_REVIEW')",
            ("J-ANCHOR-WAIT", "scene-yard", "设备停车场", "[1,2]"),
        )
        self.database.commit()

        with self.assertRaisesRegex(ValueError, "尚未全部生成完成"):
            self.service.approve_continuity_anchors("J-ANCHOR-WAIT")

        state = self.database.execute(
            "SELECT state FROM jobs WHERE job_id='J-ANCHOR-WAIT'"
        ).fetchone()[0]
        self.assertEqual(state, "ANCHOR_REVIEW_REQUIRED")

    def test_approved_review_requires_every_check_to_pass(self):
        final = self.root / "data" / "checks.mp4"
        final.write_bytes(b"video")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,final_path) "
            "VALUES('J-CHECK','T-CHECK','验收视频','COMPOSED',?)", (str(final),)
        )
        self.database.commit()
        checks = {
            "identity": True, "lip_sync": True, "subtitle_sync": False,
            "visual_integrity": True, "clean_frame": True, "editing_rhythm": True,
        }

        with self.assertRaisesRegex(ValueError, "全部验收项"):
            self.service.review_video("J-CHECK", decision="approved", checks=checks)

        checks["subtitle_sync"] = True
        approved = self.service.review_video(
            "J-CHECK", decision="approved", checks=checks, notes="逐项检查通过"
        )
        self.assertTrue(approved["publish_unlocked"])
        saved = self.database.execute(
            "SELECT checks_json FROM review_events WHERE job_id='J-CHECK'"
        ).fetchone()[0]
        self.assertTrue(json.loads(saved)["subtitle_sync"])

    def test_approved_final_review_resolves_qa_timeout_manual_review_todo(self):
        final = self.root / "data" / "qa-timeout-approved.mp4"
        final.write_bytes(b"video")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,final_path) "
            "VALUES('J-QA-TIMEOUT','T-QA','qa timeout','COMPOSED',?)",
            (str(final),),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_qa) VALUES(?,?,?,?)",
            (
                "J-QA-TIMEOUT", 1, "OK",
                json.dumps({
                    "status": "manual_review_required",
                    "reasons": ["qa_error"],
                }),
            ),
        )
        self.database.commit()
        checks = {
            key: True for key in (
                "identity", "lip_sync", "subtitle_sync",
                "visual_integrity", "clean_frame", "editing_rhythm",
            )
        }

        self.service.review_video(
            "J-QA-TIMEOUT",
            decision="approved",
            checks=checks,
            notes="六项人工验收通过",
        )

        job = self.service.videos()["jobs"][0]
        self.assertFalse(job["shots"][0]["manual_review_required"])
        self.assertEqual(
            job["shots"][0]["quality_status"], "human_review_approved"
        )
        self.assertEqual(job["shots"][0]["quality_reasons"], ["qa_error"])

    def test_video_display_state_requires_publication_evidence(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) VALUES('J-LEGACY','T','旧状态','PUBLISHED')"
        )
        self.database.commit()

        job = self.service.videos()["jobs"][0]

        self.assertEqual(job["display_state"], "LEGACY_UNVERIFIED")
        self.assertEqual(job["display_state_label"], "历史状态待核实")

    def test_video_api_exposes_director_plan(self):
        storyboard = {
            "production_profile": "standard",
            "characters": [{"id": "host", "name": "主讲人"}],
            "masters": [
                {"idx": 1, "type": "dialogue", "duration": 24},
                {"idx": 2, "type": "broll", "duration": 8},
            ],
            "shots": [],
            "director_plan": {
                "strategy": "dialogue_plus_broll",
                "dialogue_master_count": 1,
                "broll_master_count": 1,
                "broll_beats": ["evidence", "reversal"],
            },
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-DIRECTOR','T-DIRECTOR','导演计划','STORYBOARDED',?)",
            (json.dumps(storyboard, ensure_ascii=False),),
        )
        self.database.commit()

        job = self.service.videos()["jobs"][0]

        self.assertEqual(job["director_plan"]["strategy"], "dialogue_plus_broll")
        self.assertEqual(job["director_plan"]["broll_master_count"], 1)

    def test_video_api_exposes_dual_track_director_timeline(self):
        storyboard = {
            "masters": [
                {"idx": 1, "type": "dialogue", "speaker": "presenter",
                 "duration": 20},
                {"idx": 2, "type": "broll", "duration": 8},
            ],
            "shots": [
                {"idx": 1, "beat": "hook", "duration": 4,
                 "video_master_idx": 1, "audio_master_idx": 1,
                 "video_start": 0, "audio_start": 0,
                 "continuity_relation": "opening", "transition_type": "none",
                 "visual_intent": "主讲人开场"},
                {"idx": 2, "beat": "evidence", "duration": 5,
                 "video_master_idx": 2, "audio_master_idx": 1,
                 "video_start": 0, "audio_start": 4,
                 "continuity_relation": "audio_bridge", "transition_type": "cut",
                 "transition_reason": "对白连续，画面切入证据镜头",
                 "visual_intent": "设备台账与调度现场"},
            ],
            "director_plan": {
                "strategy": "dialogue_plus_broll",
                "dialogue_master_count": 1,
                "broll_master_count": 1,
            },
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-TIMELINE','T-1','导演时间线','COMPOSED',?)",
            (json.dumps(storyboard, ensure_ascii=False),),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,state,video_path) "
            "VALUES('J-TIMELINE',1,20,'OK','dialogue.mp4')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,state,video_path) "
            "VALUES('J-TIMELINE',2,8,'OK','broll.mp4')"
        )
        self.database.commit()

        job = self.service.videos()["jobs"][0]

        self.assertEqual(len(job["director_timeline"]), 2)
        self.assertEqual(job["director_timeline"][0]["video_source_label"], "对白母片 #1")
        self.assertEqual(job["director_timeline"][1]["video_source_label"], "场景 B-roll #2")
        self.assertEqual(job["director_timeline"][1]["audio_source_label"], "对白音轨 #1")
        self.assertTrue(job["director_timeline"][1]["uses_broll"])
        self.assertEqual(job["director_timeline"][1]["continuity_relation"], "audio_bridge")
        self.assertEqual(job["director_timeline"][1]["transition_type"], "cut")
        self.assertEqual(
            job["director_timeline"][1]["transition_reason"],
            "对白连续，画面切入证据镜头",
        )
        self.assertGreater(job["shots"][1]["regeneration_estimated_cost_cents"], 0)

    def test_prepare_single_master_regeneration_preserves_other_paid_masters(self):
        final = self.root / "data" / "old-final.mp4"
        final.write_bytes(b"old final")
        storyboard = {
            "masters": [
                {"idx": 1, "type": "dialogue", "duration": 20},
                {"idx": 2, "type": "broll", "duration": 8,
                 "prompt": "旧提示：主讲人掰手指"},
            ],
            "shots": [
                {"idx": 1, "beat": "hook", "duration": 20,
                 "video_master_idx": 1, "audio_master_idx": 1},
                {"idx": 2, "beat": "evidence", "duration": 8,
                 "video_master_idx": 2, "audio_master_idx": 1,
                 "visual_intent": "主讲人掰手指",
                 "broll_hint": "工程设备、调度台账和经营现场"},
            ],
            "production_settings": {"budget_cents": 5000},
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,final_path,cost_cents) "
            "VALUES('J-REGEN','T-1','单母片返修','COMPOSED',?,?,2200)",
            (json.dumps(storyboard, ensure_ascii=False), str(final)),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,state,provider,cost_cents,video_path) "
            "VALUES('J-REGEN',1,20,'OK','seedance',1600,'dialogue.mp4')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,state,provider,cost_cents,video_path) "
            "VALUES('J-REGEN',2,8,'OK','seedance',600,'broll.mp4')"
        )
        self.database.execute(
            "INSERT INTO review_events(job_id,decision,notes) "
            "VALUES('J-REGEN','approved','旧成片已通过')"
        )
        self.database.commit()

        result = self.service.prepare_master_regeneration(
            "J-REGEN", 2, reason="B-roll 行业场景不足"
        )

        job = self.database.execute(
            "SELECT state,final_path,cost_cents,error FROM jobs WHERE job_id='J-REGEN'"
        ).fetchone()
        shots = self.database.execute(
            "SELECT idx,state,cost_cents,video_path,prompt,error FROM shots "
            "WHERE job_id='J-REGEN' ORDER BY idx"
        ).fetchall()
        reviews = self.database.execute(
            "SELECT COUNT(*) FROM review_events WHERE job_id='J-REGEN'"
        ).fetchone()[0]
        repaired_storyboard = json.loads(self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id='J-REGEN'"
        ).fetchone()[0])

        self.assertEqual(result["preserved_masters"], 1)
        self.assertEqual(result["regeneration_master_idx"], 2)
        self.assertEqual(job["state"], "STORYBOARDED")
        self.assertEqual(job["final_path"], str(final))
        self.assertEqual(job["cost_cents"], 2200)
        self.assertEqual(shots[0]["state"], "OK")
        self.assertEqual(shots[0]["video_path"], "dialogue.mp4")
        self.assertEqual(shots[0]["cost_cents"], 1600)
        self.assertEqual(shots[1]["state"], "PENDING")
        self.assertEqual(shots[1]["video_path"], "broll.mp4")
        self.assertEqual(shots[1]["cost_cents"], 600)
        self.assertEqual(reviews, 0)
        self.assertEqual(
            repaired_storyboard["masters"][1]["repair_direction"],
            "B-roll 行业场景不足",
        )
        self.assertIn(
            "本次返修必须解决：B-roll 行业场景不足",
            repaired_storyboard["masters"][1]["prompt"],
        )
        self.assertIn(
            "工程设备、调度台账和经营现场",
            repaired_storyboard["masters"][1]["prompt"],
        )
        self.assertNotIn(
            "主讲人掰手指",
            repaired_storyboard["masters"][1]["prompt"],
        )
        self.assertEqual(
            shots[1]["prompt"],
            repaired_storyboard["masters"][1]["prompt"],
        )

    def test_single_master_regeneration_rejects_active_and_published_jobs(self):
        for state in ("PRODUCTION_RUNNING", "READY_TO_PUBLISH", "PUBLISHED"):
            job_id = f"J-REGEN-{state}"
            self.database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
                "VALUES(?,?,?,?,?)",
                (job_id, "T-1", state, state,
                 json.dumps({"masters": [{"idx": 1, "type": "dialogue"}]})),
            )
            self.database.execute(
                "INSERT INTO shots(job_id,idx,state,video_path) VALUES(?,1,'OK','one.mp4')",
                (job_id,),
            )
        self.database.commit()

        for state in ("PRODUCTION_RUNNING", "READY_TO_PUBLISH", "PUBLISHED"):
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.service.prepare_master_regeneration(
                    f"J-REGEN-{state}", 1, reason="需要返修"
                )

    def test_dialogue_master_regeneration_invalidates_old_speech_timeline(self):
        storyboard = {
            "masters": [{
                "idx": 1, "type": "dialogue", "duration": 17,
                "speaker": "林夏", "voiceover": "新的原生语音会有新的时间轴",
            }],
            "shots": [{
                "idx": 1, "video_master_idx": 1, "audio_master_idx": 1,
                "duration": 16.8, "subtitle": None,
                "subtitle_cues": [{
                    "start": 2.0, "end": 16.5, "text": "旧字幕时间轴",
                }],
                "speech_timeline_version": 2,
            }],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-REGEN-SPEECH','T-1','对白返修','COMPOSED',?)",
            (json.dumps(storyboard, ensure_ascii=False),),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path) "
            "VALUES('J-REGEN-SPEECH',1,'OK','dialogue.mp4')"
        )
        self.database.commit()

        self.service.prepare_master_regeneration(
            "J-REGEN-SPEECH", 1, reason="口型与原生语音不一致"
        )

        repaired = json.loads(self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id='J-REGEN-SPEECH'"
        ).fetchone()[0])
        regenerated_duration = self.database.execute(
            "SELECT duration_sec FROM shots "
            "WHERE job_id='J-REGEN-SPEECH' AND idx=1"
        ).fetchone()[0]
        edit = repaired["shots"][0]
        self.assertNotIn("speech_timeline_version", edit)
        self.assertEqual(edit.get("subtitle_cues"), [])
        self.assertEqual(edit["subtitle"], "旧字幕时间轴")
        self.assertEqual(regenerated_duration, 17)

    def test_prepare_lip_sync_edit_preserves_paid_master_and_speech_timeline(self):
        master_path = self.root / "data" / "assets" / "J-LIP" / "shots" / "03.mp4"
        master_path.parent.mkdir(parents=True, exist_ok=True)
        master_path.write_bytes(b"original cinematic master")
        master_audio = master_path.with_suffix(".wav")
        master_audio.write_bytes(b"original native audio")
        storyboard = {
            "masters": [{
                "idx": 3,
                "type": "dialogue",
                "duration": 20,
                "speaker": "林夏",
                "render_mode": "cinematic_character",
                "voiceover": "我选一台二十八米级曲臂高空作业平台。",
            }],
            "shots": [{
                "idx": 1,
                "video_master_idx": 3,
                "audio_master_idx": 3,
                "duration": 20,
                "subtitle_cues": [{
                    "start": 3.0,
                    "end": 15.0,
                    "text": "我选一台二十八米级曲臂高空作业平台",
                }],
                "speech_timeline_version": 2,
            }],
            "production_settings": {"budget_cents": 5000},
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-LIP','T-1','口型局部修复','COMPOSED',?,2200)",
            (json.dumps(storyboard, ensure_ascii=False),),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,state,provider,cost_cents,"
            "video_path,video_qa) "
            "VALUES('J-LIP',3,20,'OK','seedance',1600,?,?)",
            (
                str(master_path),
                json.dumps({"reasons": ["lip_sync_mismatch"]}),
            ),
        )
        self.database.commit()

        visible_job = next(
            job for job in self.service.videos()["jobs"]
            if job["job_id"] == "J-LIP"
        )
        self.assertTrue(visible_job["shots"][0]["lip_sync_repair_eligible"])
        self.assertEqual(
            visible_job["shots"][0]["lip_sync_repair_suggestion"],
            {
                "start_sec": 3.0,
                "end_sec": 15.0,
                "dialogue": "我选一台二十八米级曲臂高空作业平台",
                "reason": "自动根据字幕时间轴定位口型偏差区间",
            },
        )

        result = self.service.prepare_master_lip_sync_repair(
            "J-LIP", 3, start_sec=3, end_sec=15,
            reason="林夏嘴型比原生语音慢",
        )

        job = self.database.execute(
            "SELECT state,storyboard,cost_cents FROM jobs WHERE job_id='J-LIP'"
        ).fetchone()
        shot = self.database.execute(
            "SELECT state,video_path,cost_cents FROM shots "
            "WHERE job_id='J-LIP' AND idx=3"
        ).fetchone()
        repaired = json.loads(job["storyboard"])
        master = repaired["masters"][0]
        edit = repaired["shots"][0]

        self.assertEqual(result["repair_kind"], "seedance_lip_sync_edit")
        self.assertEqual(job["state"], "STORYBOARDED")
        self.assertEqual(job["cost_cents"], 2200)
        self.assertEqual(shot["state"], "PENDING")
        self.assertEqual(shot["video_path"], str(master_path))
        self.assertEqual(shot["cost_cents"], 1600)
        self.assertEqual(master["repair_mode"], "seedance_lip_sync_edit")
        self.assertEqual(
            Path(master["repair_source_path"]).resolve(), master_path.resolve()
        )
        self.assertEqual(master["repair_start_sec"], 3.0)
        self.assertEqual(master["repair_end_sec"], 15.0)
        self.assertEqual(
            Path(master["repair_audio_path"]).resolve(), master_audio.resolve()
        )
        self.assertEqual(
            master["repair_dialogue"],
            "我选一台二十八米级曲臂高空作业平台",
        )
        self.assertEqual(edit["speech_timeline_version"], 2)
        self.assertEqual(edit["subtitle_cues"][0]["start"], 3.0)

    def test_review_candidate_with_only_lip_sync_mismatch_can_enter_edit_without_accepting(self):
        candidate_path = (
            self.root / "data" / "assets" / "J-LIP-CANDIDATE" / "shots"
            / "review" / "01-original-candidate.mp4"
        )
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_bytes(b"original review candidate")
        storyboard = {
            "masters": [{
                "idx": 1,
                "type": "dialogue",
                "duration": 8,
                "speaker": "林夏",
                "voiceover": "暴雨再大，也不能让客户停工。",
            }],
            "shots": [{
                "idx": 1,
                "video_master_idx": 1,
                "audio_master_idx": 1,
                "duration": 8,
                "subtitle_cues": [{
                    "start": 1.2,
                    "end": 6.8,
                    "text": "暴雨再大，也不能让客户停工",
                }],
            }],
            "production_settings": {"budget_cents": 5000},
        }
        qa = {
            "status": "manual_review_required",
            "reasons": ["lip_sync_mismatch"],
            "candidate_path": str(candidate_path),
            "candidate_url": "https://cdn.example/J-LIP-CANDIDATE/01.mp4",
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-LIP-CANDIDATE','T-1','候选口型修复',"
            "'PRODUCTION_REVIEW_REQUIRED',?,1216)",
            (json.dumps(storyboard, ensure_ascii=False),),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,state,provider,cost_cents,"
            "video_qa) VALUES('J-LIP-CANDIDATE',1,8,'REVIEW_REQUIRED',"
            "'seedance',944,?)",
            (json.dumps(qa, ensure_ascii=False),),
        )
        self.database.commit()

        visible_job = next(
            job for job in self.service.videos()["jobs"]
            if job["job_id"] == "J-LIP-CANDIDATE"
        )
        visible_shot = visible_job["shots"][0]
        self.assertFalse(visible_shot["has_prior_master"])
        self.assertIsNotNone(visible_shot["candidate_preview_url"])
        self.assertTrue(visible_shot["lip_sync_repair_eligible"])
        self.assertEqual(
            visible_shot["lip_sync_repair_suggestion"]["start_sec"], 1.2
        )
        self.assertEqual(
            visible_shot["lip_sync_repair_suggestion"]["end_sec"], 6.8
        )

        result = self.service.prepare_master_lip_sync_repair(
            "J-LIP-CANDIDATE", 1, start_sec=1.2, end_sec=6.8,
            reason="嘴型比原生语音慢",
        )

        persisted = self.database.execute(
            "SELECT state,storyboard,cost_cents FROM jobs "
            "WHERE job_id='J-LIP-CANDIDATE'"
        ).fetchone()
        repaired = json.loads(persisted["storyboard"])
        repaired_master = repaired["masters"][0]
        persisted_qa = json.loads(self.database.execute(
            "SELECT video_qa FROM shots WHERE job_id='J-LIP-CANDIDATE' AND idx=1"
        ).fetchone()[0])
        self.assertEqual(result["repair_source_kind"], "review_candidate")
        self.assertEqual(persisted["state"], "STORYBOARDED")
        self.assertEqual(persisted["cost_cents"], 1216)
        self.assertEqual(
            Path(repaired_master["repair_source_path"]).resolve(),
            candidate_path.resolve(),
        )
        self.assertEqual(
            repaired_master.get("repair_source_url"),
            "https://cdn.example/J-LIP-CANDIDATE/01.mp4",
        )
        self.assertEqual(
            persisted_qa["candidate_path"], str(candidate_path)
        )
        self.assertEqual(candidate_path.read_bytes(), b"original review candidate")

    def test_failed_lip_sync_edit_can_be_prepared_again_from_controlled_source(self):
        candidate_path = (
            self.root / "data" / "assets" / "J-LIP-RETRY" / "shots"
            / "review" / "01-candidate.mp4"
        )
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_bytes(b"preserved review candidate")
        storyboard = {
            "masters": [{
                "idx": 1,
                "type": "dialogue",
                "duration": 8,
                "speaker": "lin_xia",
                "voiceover": "暴雨再大，也不能让客户停工。",
                "repair_mode": "seedance_lip_sync_edit",
                "repair_source_path": str(candidate_path),
            }],
            "shots": [],
            "production_settings": {"budget_cents": 1500},
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-LIP-RETRY','T-1','口型修复重试','FAILED',?,0)",
            (json.dumps(storyboard, ensure_ascii=False),),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,state,cost_cents,video_qa) "
            "VALUES('J-LIP-RETRY',1,8,'FAILED',0,?)",
            (json.dumps({
                "status": "generation_failed",
                "failure_code": "request_invalid",
            }),),
        )
        self.database.commit()

        try:
            result = self.service.prepare_master_lip_sync_repair(
                "J-LIP-RETRY", 1, start_sec=0, end_sec=8,
                reason="使用修正后的官方 edit 合同再次提交",
            )
        except ValueError:
            result = {"repair_source_kind": "missing"}

        persisted = self.database.execute(
            "SELECT state,storyboard FROM jobs WHERE job_id='J-LIP-RETRY'"
        ).fetchone()
        self.assertEqual(result["repair_source_kind"], "persisted_repair_source")
        repaired = json.loads(persisted["storyboard"])
        self.assertEqual(persisted["state"], "STORYBOARDED")
        self.assertEqual(
            Path(repaired["masters"][0]["repair_source_path"]).resolve(),
            candidate_path.resolve(),
        )
        self.assertEqual(
            candidate_path.read_bytes(), b"preserved review candidate"
        )

    def test_master_extension_preserves_prior_and_queues_managed_mov_candidate(self):
        asset_root = self.root / "data" / "assets" / "J-EXTEND" / "shots"
        asset_root.mkdir(parents=True)
        master_path = asset_root / "02.mp4"
        master_path.write_bytes(b"prior-master")
        storyboard = {
            "masters": [{
                "idx": 2, "type": "broll", "duration": 18,
                "scene": "暴雨抢修现场", "action": "工程师走向控制台",
            }],
            "shots": [],
            "production_settings": {"budget_cents": 5000},
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-EXTEND','T-1','母片延长','COMPOSED',?,900)",
            (json.dumps(storyboard, ensure_ascii=False),),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,state,provider,cost_cents,"
            "video_path) VALUES('J-EXTEND',2,18,'OK','seedance',700,?)",
            (str(master_path),),
        )
        self.database.commit()

        visible = next(
            job for job in self.service.videos()["jobs"]
            if job["job_id"] == "J-EXTEND"
        )
        self.assertTrue(visible["shots"][0]["extension_eligible"])

        result = self.service.prepare_master_extension(
            "J-EXTEND", 2, extension_sec=8,
            direction="镜头继续跟随工程师走向控制台并自然收束",
        )

        job = self.database.execute(
            "SELECT state,storyboard,cost_cents FROM jobs WHERE job_id='J-EXTEND'"
        ).fetchone()
        shot = self.database.execute(
            "SELECT state,video_path,cost_cents FROM shots "
            "WHERE job_id='J-EXTEND' AND idx=2"
        ).fetchone()
        master = json.loads(job["storyboard"])["masters"][0]
        self.assertEqual(result["repair_kind"], "seedance_extend")
        self.assertEqual(job["state"], "STORYBOARDED")
        self.assertEqual(job["cost_cents"], 900)
        self.assertEqual(shot["state"], "PENDING")
        self.assertEqual(shot["video_path"], str(master_path))
        self.assertEqual(shot["cost_cents"], 700)
        self.assertEqual(master["repair_mode"], "seedance_extend")
        self.assertEqual(master["extend_duration_sec"], 8.0)
        self.assertIn("自然收束", master["extend_direction"])
        self.assertEqual(
            Path(master["repair_source_path"]).resolve(), master_path.resolve()
        )

    def test_failed_job_can_rebuild_safe_director_plan_without_losing_cost_history(self):
        self._insert_editable_content(approved=True, script_approved=True)
        script = self._editable_script_payload(title="不可执行旧剧本")
        script["beats"][0]["dialogue"] = (
            "非洲客户一次租了一万台设备，这么大的租赁需求，"
            "众能联合凭什么获得认可？这就是我们的交付实力。"
        )
        script["beats"][0]["duration_sec"] = 4.5
        old_storyboard = {
            "characters": [{
                "id": "presenter", "name": "老大",
                "script_character_name": "presenter",
                "generation_mode": "fixed_asset",
                "identity_type": "virtual_person",
                "seedance_asset_uri": "asset://boss",
            }],
            "masters": [{
                "idx": 1, "type": "dialogue", "duration": 4.5,
                "speaker": "presenter", "voiceover": script["beats"][0]["dialogue"],
            }],
            "shots": [],
            "production_settings": {
                "orientation": "9:16", "budget_cents": 10000,
                "production_profile": "standard",
            },
        }
        self.database.execute(
            "UPDATE script_drafts SET script_json=? WHERE draft_id='SCRIPT-EDIT'",
            (json.dumps(script, ensure_ascii=False),),
        )
        self.database.execute(
            "UPDATE jobs SET state='FAILED',storyboard=?,characters_json=?,"
            "cost_cents=2034,error='transcript_mismatch' WHERE job_id='J-TOPIC-EDIT'",
            (
                json.dumps(old_storyboard, ensure_ascii=False),
                json.dumps(old_storyboard["characters"], ensure_ascii=False),
            ),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,state,cost_cents,error) "
            "VALUES('J-TOPIC-EDIT',1,4.5,'FAILED',0,'transcript_mismatch')"
        )
        self.database.commit()

        result = self.service.rebuild_failed_director_plan("J-TOPIC-EDIT")

        job = self.database.execute(
            "SELECT state,cost_cents,error,storyboard FROM jobs "
            "WHERE job_id='J-TOPIC-EDIT'"
        ).fetchone()
        rebuilt = json.loads(job["storyboard"])
        shots = self.database.execute(
            "SELECT state,duration_sec,cost_cents FROM shots "
            "WHERE job_id='J-TOPIC-EDIT' ORDER BY idx"
        ).fetchall()
        self.assertEqual(job["state"], "STORYBOARDED")
        self.assertEqual(job["cost_cents"], 2034)
        self.assertIsNone(job["error"])
        self.assertTrue(rebuilt["director_plan"]["speech_duration_adjusted"])
        self.assertEqual(rebuilt["production_settings"]["budget_cents"], 10000)
        self.assertEqual(rebuilt["characters"][0]["seedance_asset_uri"], "asset://boss")
        self.assertEqual(len(shots), len(rebuilt["masters"]))
        self.assertTrue(all(row["state"] == "PENDING" for row in shots))
        self.assertTrue(all(int(row["cost_cents"] or 0) == 0 for row in shots))
        self.assertEqual(result["invalidated_masters"], 1)

    def test_rebuild_mixed_job_fails_closed_without_deleting_ok_paid_master(self):
        self._insert_editable_content(approved=True, script_approved=True)
        asset_root = self.root / "data" / "assets" / "J-TOPIC-EDIT"
        asset_root.mkdir(parents=True)
        paid_master = asset_root / "01.mp4"
        paid_master.write_bytes(b"paid-ok-master")
        paid_qa = json.dumps({"status": "passed", "score": 0.98})
        self.database.execute(
            "UPDATE jobs SET state='FAILED',storyboard='{}',cost_cents=700,error='shot 2 failed' "
            "WHERE job_id='J-TOPIC-EDIT'"
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,state,cost_cents,video_path,video_qa,error) "
            "VALUES('J-TOPIC-EDIT',?,?,?,?,?,?)",
            [
                (1, "OK", 700, str(paid_master), paid_qa, None),
                (2, "FAILED", 0, None, None, "timeout"),
            ],
        )
        self.database.commit()

        with self.assertRaisesRegex(ValueError, "显式.*成本|成本.*确认"):
            self.service.rebuild_failed_director_plan("J-TOPIC-EDIT")

        rows = self.database.execute(
            "SELECT idx,state,cost_cents,video_path,video_qa,error FROM shots "
            "WHERE job_id='J-TOPIC-EDIT' ORDER BY idx"
        ).fetchall()
        self.assertEqual([dict(row) for row in rows], [
            {"idx": 1, "state": "OK", "cost_cents": 700,
             "video_path": str(paid_master), "video_qa": paid_qa, "error": None},
            {"idx": 2, "state": "FAILED", "cost_cents": 0,
             "video_path": None, "video_qa": None, "error": "timeout"},
        ])
        self.assertEqual(paid_master.read_bytes(), b"paid-ok-master")

    def test_legacy_storyboard_can_upgrade_before_any_paid_call(self):
        self._insert_editable_content(approved=True, script_approved=True)
        legacy_storyboard = {
            "production_profile": "standard",
            "characters": [{"id": "presenter", "name": "固定主播"}],
            "masters": [{
                "idx": 1, "type": "dialogue", "speaker": "presenter",
                "duration": 24, "voiceover": "旧母片",
            }],
            "shots": [],
            "production_settings": {
                "orientation": "16:9", "budget_cents": 3600,
            },
        }
        encoded = json.dumps(legacy_storyboard, ensure_ascii=False)
        self.database.execute(
            "UPDATE jobs SET storyboard=? WHERE job_id='J-TOPIC-EDIT'", (encoded,)
        )
        self.database.execute(
            "UPDATE script_drafts SET storyboard_json=? "
            "WHERE draft_id='SCRIPT-EDIT'", (encoded,)
        )
        self.database.commit()
        self.assertTrue(
            self.service.videos()["jobs"][0]["director_plan_upgradeable"]
        )

        result = self.service.upgrade_director_plan("J-TOPIC-EDIT")

        self.assertEqual(result["director_plan"]["strategy"], "dialogue_plus_broll")
        upgraded = json.loads(self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id='J-TOPIC-EDIT'"
        ).fetchone()[0])
        self.assertEqual(upgraded["production_settings"]["orientation"], "16:9")
        self.assertEqual(upgraded["production_settings"]["budget_cents"], 3600)
        self.assertEqual(upgraded["director_plan"]["broll_master_count"], 1)
        refreshed = self.service.videos()["jobs"][0]
        self.assertFalse(refreshed["director_plan_upgradeable"])

    def test_director_plan_upgrade_rejects_task_with_paid_calls(self):
        self._insert_editable_content(approved=True, script_approved=True)
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,cost_cents) "
            "VALUES('CALL-PAID','J-TOPIC-EDIT',1,'seedance',800)"
        )
        self.database.commit()

        with self.assertRaisesRegex(ValueError, "已经产生付费调用"):
            self.service.upgrade_director_plan("J-TOPIC-EDIT")

    def test_script_draft_can_be_approved_without_provider_call(self):
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json,"
            "approved_job_id) VALUES('BRIEF-1','approved','选题','{}','J-DRAFT')"
        )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) "
            "VALUES('J-DRAFT','T-DRAFT','选题','PENDING')"
        )
        script = {
            "title": "设备租赁真相",
            "creative_thesis": "效率决定利润",
            "target_duration_sec": 24,
            "beats": [
                {
                    "beat": beat,
                    "speaker": "presenter",
                    "dialogue": f"第{index}句台词",
                    "duration_sec": 4,
                    "visual_intent": "办公室口播",
                    "source_ids": ["SRC-1"],
                    "broll_hint": "设备画面",
                }
                for index, beat in enumerate(
                    ("hook", "conflict", "evidence", "reversal", "solution", "cta"), 1
                )
            ],
        }
        self.database.execute(
            "INSERT INTO script_drafts(draft_id,brief_id,variant_index,status,"
            "request_fingerprint,script_json) VALUES(?,?,?,?,?,?)",
            ("SCRIPT-1", "BRIEF-1", 0, "candidate", "fp", json.dumps(script)),
        )
        self.database.commit()

        result = self.service.approve_script("SCRIPT-1")

        self.assertEqual(result["job_id"], "J-DRAFT")
        job = self.database.execute(
            "SELECT state FROM jobs WHERE job_id='J-DRAFT'"
        ).fetchone()
        self.assertEqual(job["state"], "STORYBOARDED")

    def test_video_generation_requires_explicit_confirmation(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-READY','T-1','待生产','STORYBOARDED','{}')"
        )
        self.database.commit()

        with self.assertRaisesRegex(ValueError, "确认"):
            self.service.start_video_job("J-READY", confirmed=False)

    def test_high_risk_long_master_requires_separate_confirmation(self):
        storyboard = {
            "production_settings": {"budget_cents": 10000},
            "masters": [{
                "idx": 1,
                "type": "broll",
                "duration": 26,
                "dialogue_blocks": [
                    {"speaker": "a", "text": "先断电"},
                    {"speaker": "b", "text": "收到"},
                    {"speaker": "c", "text": "开始救援"},
                ],
                "performance_beats": ["奔跑", "拉闸", "攀爬", "固定", "回望"],
                "scene_change_markers": ["楼顶", "吊篮"],
            }],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-RISK','T-1','risk','STORYBOARDED',?)",
            (json.dumps(storyboard),),
        )
        self.database.commit()

        with mock.patch(
            "factory.production_planner.seedance.estimate_cost_cents",
            return_value=900,
        ), mock.patch.object(
            self.service, "dispatch_production_queue",
            return_value={"dispatched_jobs": []},
        ) as dispatch:
            view = self.service.videos()["jobs"][0]
            with self.assertRaisesRegex(ValueError, "高风险"):
                self.service.start_video_job("J-RISK", confirmed=True)
            result = self.service.start_video_job(
                "J-RISK", confirmed=True, high_risk_confirmed=True
            )

        self.assertTrue(view["preflight"]["requires_long_master_confirmation"])
        self.assertEqual(view["preflight"]["high_risk_master_indexes"], [1])
        self.assertEqual(result["status"], "queued")
        dispatch.assert_called_once()

    def test_storyboarded_video_exposes_side_effect_free_cost_preflight(self):
        (self.root / ".env").write_text(
            "ARK_API_KEY=secret-value\n"
            "ARK_VIDEO_RESOLUTION=720p\n"
            "SEEDANCE_JOB_BUDGET_CENTS=1000\n",
            encoding="utf-8",
        )
        storyboard = {
            "production_profile": "standard",
            "masters": [
                {"idx": 1, "type": "dialogue", "duration": 8},
                {"idx": 2, "type": "broll", "duration": 6,
                 "asset_tags": ["equipment"]},
            ],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-PREFLIGHT','T-1','preflight','STORYBOARDED',?)",
            (json.dumps(storyboard),),
        )
        self.database.commit()

        with mock.patch(
            "factory.production_planner.seedance.estimate_cost_cents",
            side_effect=lambda duration, _resolution: int(duration * 100),
        ) as estimate:
            job = self.service.videos()["jobs"][0]

        self.assertEqual(job["master_count"], 2)
        self.assertEqual(
            [item["idx"] for item in job["planned_masters"]], [1, 2]
        )
        self.assertEqual(job["total_master_duration_sec"], 14.0)
        self.assertEqual(job["preflight"]["estimated_paid_calls"], 2)
        self.assertEqual(job["preflight"]["estimated_cost_cents"], 1400)
        self.assertEqual(job["preflight"]["budget_cents"], 1000)
        self.assertFalse(job["preflight"]["budget_ok"])
        self.assertEqual(estimate.call_count, 2)

    def test_start_video_job_blocks_confirmed_generation_over_hard_budget(self):
        storyboard = {
            "masters": [{"idx": 1, "type": "dialogue", "duration": 12}],
        }
        (self.root / ".env").write_text(
            "ARK_API_KEY=secret-value\nSEEDANCE_JOB_BUDGET_CENTS=100\n",
            encoding="utf-8",
        )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-BLOCK-SPAWN','T-1','budget blocked','STORYBOARDED',?)",
            (json.dumps(storyboard),),
        )
        self.database.commit()

        with mock.patch(
            "factory.production_planner.seedance.estimate_cost_cents",
            return_value=900,
        ), mock.patch("factory.management_api.subprocess.Popen") as popen:
            with self.assertRaisesRegex(
                ValueError, "请在预检中明确提高预算"
            ):
                self.service.start_video_job(
                    "J-BLOCK-SPAWN", confirmed=True
                )

        popen.assert_not_called()
        state = self.database.execute(
            "SELECT state FROM jobs WHERE job_id='J-BLOCK-SPAWN'"
        ).fetchone()["state"]
        self.assertEqual(state, "STORYBOARDED")

    def test_start_video_job_accepts_explicit_per_job_budget_increase(self):
        storyboard = {
            "production_settings": {"budget_cents": 100},
            "masters": [{"idx": 1, "type": "broll", "duration": 12}],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-BUDGET-OVERRIDE','T-1','budget override','STORYBOARDED',?)",
            (json.dumps(storyboard),),
        )
        self.database.commit()

        with mock.patch(
            "factory.production_planner.seedance.estimate_cost_cents",
            return_value=900,
        ), mock.patch.object(
            self.service, "dispatch_production_queue",
            return_value={"dispatched_jobs": []},
        ):
            result = self.service.start_video_job(
                "J-BUDGET-OVERRIDE",
                confirmed=True,
                approved_budget_cents=900,
            )

        self.assertEqual(result["status"], "queued")
        row = self.database.execute(
            "SELECT state,storyboard FROM jobs WHERE job_id='J-BUDGET-OVERRIDE'"
        ).fetchone()
        self.assertEqual(row["state"], "PRODUCTION_QUEUED")
        self.assertEqual(
            json.loads(row["storyboard"])["production_settings"]["budget_cents"],
            900,
        )
        confirmed = json.loads(row["storyboard"])["production_settings"][
            "confirmed_cost_estimate"
        ]
        self.assertEqual(confirmed["estimated_additional_cost_cents"], 900)
        self.assertEqual(confirmed["projected_total_cost_cents"], 900)
        self.assertEqual(confirmed["current_cost_cents"], 0)
        self.assertEqual(confirmed["budget_cents"], 900)
        self.assertEqual(confirmed["pricing_version"], "seedance25-area-v1")

    def test_completed_video_exposes_confirmed_cost_variance_without_reestimating(self):
        storyboard = {
            "production_settings": {
                "budget_cents": 2500,
                "confirmed_cost_estimate": {
                    "estimated_additional_cost_cents": 1224,
                    "projected_total_cost_cents": 1224,
                    "current_cost_cents": 0,
                    "budget_cents": 2500,
                    "pricing_version": "seedance25-area-v1",
                },
            },
            "masters": [{"idx": 1, "type": "dialogue", "duration": 8}],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-COST-OBS','T-1','cost observed','COMPOSED',?,1216)",
            (json.dumps(storyboard),),
        )
        self.database.commit()

        with mock.patch(
            "factory.production_planner.analyze_storyboard",
            side_effect=AssertionError("completed jobs must not be re-estimated"),
        ):
            job = next(
                item for item in self.service.videos()["jobs"]
                if item["job_id"] == "J-COST-OBS"
            )

        self.assertEqual(job["cost_observation"], {
            "estimated_cost_cents": 1224,
            "actual_cost_cents": 1216,
            "variance_cents": -8,
            "variance_percent": -0.7,
            "status": "within_estimate",
            "is_final": True,
            "pricing_version": "seedance25-area-v1",
        })

    def test_historical_video_without_confirmed_estimate_is_not_backfilled(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-COST-LEGACY','T-1','legacy','COMPOSED','{}',500)"
        )
        self.database.commit()

        job = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-COST-LEGACY"
        )

        self.assertEqual(job["cost_observation"]["status"], "unavailable")
        self.assertIsNone(job["cost_observation"]["estimated_cost_cents"])
        self.assertEqual(job["cost_observation"]["actual_cost_cents"], 500)

    def test_resume_failed_video_preserves_completed_masters(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,error) "
            "VALUES('J-RESUME','T-1','resume','FAILED','{}',1889,'budget')"
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,state,cost_cents,video_path,error) "
            "VALUES(?,?,?,?,?,?)",
            [
                ("J-RESUME", 1, "OK", 1550, "01.mp4", None),
                ("J-RESUME", 2, "OK", 339, "02.mp4", None),
                ("J-RESUME", 3, "FAILED", 0, None, "budget"),
            ],
        )
        self.database.commit()

        result = self.service.resume_failed_video("J-RESUME")

        self.assertEqual(result["preserved_masters"], 2)
        self.assertEqual(result["remaining_masters"], 1)
        job = self.database.execute(
            "SELECT state,cost_cents,error FROM jobs WHERE job_id='J-RESUME'"
        ).fetchone()
        self.assertEqual(dict(job), {
            "state": "STORYBOARDED", "cost_cents": 1889, "error": None,
        })
        shots = self.database.execute(
            "SELECT idx,state,cost_cents,video_path,error FROM shots "
            "WHERE job_id='J-RESUME' ORDER BY idx"
        ).fetchall()
        self.assertEqual(shots[0]["state"], "OK")
        self.assertEqual(shots[0]["cost_cents"], 1550)
        self.assertEqual(shots[2]["state"], "PENDING")
        self.assertIsNone(shots[2]["error"])

    def test_resume_failed_video_migrates_legacy_candidate_to_review_before_retry(self):
        candidate = (
            self.root / "data" / "assets" / "J-RESUME-REVIEW"
            / "shots" / "review" / "01-candidate.mp4"
        )
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"legacy-paid-candidate")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,error) "
            "VALUES('J-RESUME-REVIEW','T-1','resume review','FAILED','{}',675,'qa')"
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,state,cost_cents,video_qa,error) "
            "VALUES(?,?,?,?,?,?)",
            [
                (
                    "J-RESUME-REVIEW", 1, "FAILED", 675,
                    json.dumps({
                        "status": "manual_review_required",
                        "candidate_path": str(candidate),
                    }),
                    "visual mismatch",
                ),
                (
                    "J-RESUME-REVIEW", 2, "FAILED", 0, None,
                    "connection interrupted",
                ),
            ],
        )
        self.database.commit()

        result = self.service.resume_failed_video("J-RESUME-REVIEW")

        self.assertEqual(result["state"], "PRODUCTION_REVIEW_REQUIRED")
        self.assertEqual(result["remaining_masters"], 1)
        self.assertEqual(result["manual_review_masters"], 1)
        shots = self.database.execute(
            "SELECT idx,state,error FROM shots WHERE job_id='J-RESUME-REVIEW' "
            "ORDER BY idx"
        ).fetchall()
        self.assertEqual(shots[0]["state"], "REVIEW_REQUIRED")
        self.assertEqual(shots[0]["error"], "visual mismatch")
        self.assertEqual(shots[1]["state"], "FAILED")
        self.assertEqual(shots[1]["error"], "connection interrupted")
        job = self.database.execute(
            "SELECT state FROM jobs WHERE job_id='J-RESUME-REVIEW'"
        ).fetchone()
        self.assertEqual(job["state"], "PRODUCTION_REVIEW_REQUIRED")

    def test_resume_failed_video_requeues_manual_review_without_preview_artifact(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,error) "
            "VALUES('J-RESUME-NO-PREVIEW','T-1','resume missing preview',"
            "'FAILED','{}',544,'qa')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents,video_qa,error) "
            "VALUES('J-RESUME-NO-PREVIEW',1,'FAILED',544,?,?)",
            (
                json.dumps({
                    "status": "manual_review_required",
                    "candidate_path": None,
                    "candidate_url": None,
                }),
                "visual mismatch",
            ),
        )
        self.database.commit()

        result = self.service.resume_failed_video("J-RESUME-NO-PREVIEW")

        self.assertEqual(result["remaining_masters"], 1)
        self.assertEqual(result["manual_review_masters"], 0)
        shot = self.database.execute(
            "SELECT state,error,cost_cents,video_qa FROM shots "
            "WHERE job_id='J-RESUME-NO-PREVIEW'"
        ).fetchone()
        self.assertEqual(shot["state"], "PENDING")
        self.assertIsNone(shot["error"])
        self.assertIsNone(shot["video_qa"])
        self.assertEqual(shot["cost_cents"], 544)

    def test_resume_legacy_url_only_candidate_does_not_create_fake_review(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,error) "
            "VALUES('J-LEGACY-URL-ONLY','T-1','legacy url only',"
            "'FAILED','{}',600,'qa')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents,video_qa,error) "
            "VALUES('J-LEGACY-URL-ONLY',1,'FAILED',600,?,?)",
            (
                json.dumps({
                    "status": "manual_review_required",
                    "candidate_path": str(
                        self.root / "data" / "assets" / "J-LEGACY-URL-ONLY"
                        / "shots" / "review" / "stale.mp4"
                    ),
                    "candidate_url": "https://cdn.example/stale.mp4",
                }),
                "visual mismatch",
            ),
        )
        self.database.commit()

        result = self.service.resume_failed_video("J-LEGACY-URL-ONLY")

        self.assertEqual(result["manual_review_masters"], 0)
        self.assertEqual(result["remaining_masters"], 1)
        shot = self.database.execute(
            "SELECT state,video_qa,error FROM shots "
            "WHERE job_id='J-LEGACY-URL-ONLY'"
        ).fetchone()
        self.assertEqual(shot["state"], "PENDING")
        self.assertIsNone(shot["video_qa"])
        self.assertIsNone(shot["error"])

    def test_video_api_marks_preserved_quality_warning_for_manual_review(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-QA-WARN','T-1','qa warning','PRODUCTION_RUNNING','{}')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,provider,video_qa,video_path) "
            "VALUES('J-QA-WARN',1,'OK','seedance',?,?)",
            (json.dumps({
                "status": "manual_review_required",
                "reasons": ["transcript_mismatch"],
            }), "01.mp4"),
        )
        self.database.commit()

        job = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-QA-WARN"
        )

        self.assertTrue(job["shots"][0]["manual_review_required"])
        self.assertEqual(
            job["shots"][0]["quality_reasons"], ["transcript_mismatch"]
        )

    def test_video_api_exposes_reviewable_broll_candidate_preview(self):
        asset_root = self.root / "data" / "assets" / "J-CANDIDATE"
        candidate = asset_root / "shots" / "review" / "02-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        old_master = asset_root / "shots" / "02.mp4"
        old_master.write_bytes(b"accepted")
        final = asset_root / "final.mp4"
        final.write_bytes(b"old-final")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,final_path) "
            "VALUES('J-CANDIDATE','T-1','candidate review',"
            "'PRODUCTION_REVIEW_REQUIRED','{}',?)",
            (str(final),),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
            "VALUES('J-CANDIDATE',2,'REVIEW_REQUIRED',?,?,?)",
            (
                str(old_master),
                json.dumps({
                    "status": "manual_review_required",
                    "reasons": ["missing_industry_subject"],
                    "candidate_path": str(candidate),
                    "candidate_url": "https://cdn/candidate.mp4",
                }),
                "semantic review required",
            ),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_url) "
            "VALUES('J-CANDIDATE',1,'OK',?,'https://cdn/previous.mp4')",
            (str(old_master),),
        )
        self.database.commit()

        job = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-CANDIDATE"
        )

        review_shot = next(shot for shot in job["shots"] if shot["idx"] == 2)
        self.assertEqual(
            review_shot["candidate_preview_url"],
            "/api/videos/J-CANDIDATE/masters/2/candidate",
        )
        self.assertTrue(review_shot["has_prior_master"])
        self.assertEqual(review_shot["state"], "REVIEW_REQUIRED")
        self.assertEqual(job["review_required_master_count"], 1)
        self.assertEqual(
            review_shot["previous_master_preview_url"],
            "https://cdn/previous.mp4",
        )
        self.assertEqual(self.service.overview()["videos"]["review_required"], 1)
        todo = next(
            group for group in self.service.todo_center()["groups"]
            if group["key"] == "manual_review"
        )["items"]
        self.assertTrue(any(item["id"] == "J-CANDIDATE" for item in todo))
        self.assertEqual(
            self.service.master_candidate_file("J-CANDIDATE", 2),
            candidate.resolve(),
        )

    def test_accept_first_generation_candidate_waits_for_other_failed_master(self):
        asset_root = self.root / "data" / "assets" / "J-FIRST-CANDIDATE"
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"first-paid-candidate")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,error) "
            "VALUES('J-FIRST-CANDIDATE','T-1','first candidate','FAILED','{}','qa')"
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
            "VALUES(?,?,?,?,?,?)",
            [
                (
                    "J-FIRST-CANDIDATE", 1, "FAILED", None,
                    json.dumps({
                        "status": "manual_review_required",
                        "reasons": ["visual_mismatch"],
                        "candidate_path": str(candidate),
                        "candidate_url": "https://cdn/first-candidate.mp4",
                    }),
                    "manual review",
                ),
                (
                    "J-FIRST-CANDIDATE", 2, "FAILED", None, None,
                    "provider policy",
                ),
            ],
        )
        self.database.commit()

        with mock.patch("factory.management_api.s6_compose.run") as compose, \
                mock.patch.object(
                    self.service, "dispatch_production_queue",
                    return_value={"dispatched_jobs": []},
                ):
            result = self.service.review_master_candidate(
                "J-FIRST-CANDIDATE", 1, decision="accept_candidate"
            )

        compose.assert_not_called()
        self.assertEqual(result["state"], "PRODUCTION_QUEUED")
        self.assertEqual(result["remaining_generation_count"], 1)
        accepted = self.database.execute(
            "SELECT state,video_path,video_url,error FROM shots "
            "WHERE job_id='J-FIRST-CANDIDATE' AND idx=1"
        ).fetchone()
        self.assertEqual(accepted["state"], "OK")
        self.assertIsNone(accepted["error"])
        self.assertEqual(accepted["video_url"], "https://cdn/first-candidate.mp4")
        self.assertEqual(Path(accepted["video_path"]).read_bytes(), b"first-paid-candidate")
        retry = self.database.execute(
            "SELECT state,error FROM shots "
            "WHERE job_id='J-FIRST-CANDIDATE' AND idx=2"
        ).fetchone()
        self.assertEqual(retry["state"], "PENDING")
        self.assertIsNone(retry["error"])

    def test_accept_candidate_queues_only_remaining_non_ok_masters(self):
        asset_root = self.root / "data" / "assets" / "J-ACCEPT-QUEUE"
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,error) "
            "VALUES('J-ACCEPT-QUEUE','T-1','accept queue',"
            "'PRODUCTION_REVIEW_REQUIRED','{}',900,'qa')"
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,state,cost_cents,video_qa,error) "
            "VALUES(?,?,?,?,?,?)",
            [
                (
                    "J-ACCEPT-QUEUE", 1, "REVIEW_REQUIRED", 600,
                    json.dumps({
                        "status": "manual_review_required",
                        "candidate_path": str(candidate),
                        "candidate_url": "https://cdn/candidate.mp4",
                    }),
                    "soft quality gate",
                ),
                ("J-ACCEPT-QUEUE", 2, "PENDING", 300, None, None),
                ("J-ACCEPT-QUEUE", 3, "FAILED", 0, None, "timeout"),
            ],
        )
        self.database.commit()

        with mock.patch.object(
            self.service, "dispatch_production_queue",
            return_value={"dispatched_jobs": ["J-ACCEPT-QUEUE"]},
        ) as dispatch:
            result = self.service.review_master_candidate(
                "J-ACCEPT-QUEUE", 1, decision="accept_candidate"
            )

        dispatch.assert_called_once_with()
        self.assertEqual(result["state"], "PRODUCTION_QUEUED")
        self.assertEqual(result["remaining_generation_count"], 2)
        rows = self.database.execute(
            "SELECT idx,state,cost_cents FROM shots "
            "WHERE job_id='J-ACCEPT-QUEUE' ORDER BY idx"
        ).fetchall()
        self.assertEqual([row["state"] for row in rows], ["OK", "PENDING", "PENDING"])
        self.assertEqual([row["cost_cents"] for row in rows], [600, 300, 0])
        job = self.database.execute(
            "SELECT state,cost_cents,error FROM jobs WHERE job_id='J-ACCEPT-QUEUE'"
        ).fetchone()
        self.assertEqual(dict(job), {
            "state": "PRODUCTION_QUEUED", "cost_cents": 900, "error": None,
        })

    def test_regenerate_master_clears_only_candidate_and_preserves_other_master(self):
        asset_root = self.root / "data" / "assets" / "J-REGENERATE-ONE"
        candidate = asset_root / "shots" / "review" / "02-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        accepted = asset_root / "shots" / "01.mp4"
        accepted.write_bytes(b"accepted")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,error) "
            "VALUES('J-REGENERATE-ONE','T-1','regenerate one',"
            "'PRODUCTION_REVIEW_REQUIRED',?,1300,'qa')",
            (json.dumps({
                "masters": [
                    {"idx": 1, "type": "dialogue", "duration": 8},
                    {"idx": 2, "type": "dialogue", "duration": 20,
                     "voiceover": "返修后的对白"},
                ],
                "shots": [{
                    "idx": 1, "video_master_idx": 2,
                    "audio_master_idx": 2, "duration": 18,
                    "subtitle": None,
                    "subtitle_cues": [{
                        "start": 2, "end": 17.5, "text": "旧字幕",
                    }],
                    "speech_timeline_version": 2,
                }],
            }, ensure_ascii=False),),
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,state,cost_cents,video_path,video_qa,error) "
            "VALUES(?,?,?,?,?,?,?)",
            [
                ("J-REGENERATE-ONE", 1, "OK", 700, str(accepted), None, None),
                (
                    "J-REGENERATE-ONE", 2, "REVIEW_REQUIRED", 600, None,
                    json.dumps({
                        "status": "manual_review_required",
                        "candidate_path": str(candidate),
                    }),
                    "soft quality gate",
                ),
            ],
        )
        self.database.commit()

        with mock.patch.object(
            self.service, "dispatch_production_queue",
            return_value={"dispatched_jobs": []},
        ) as dispatch:
            result = self.service.review_master_candidate(
                "J-REGENERATE-ONE", 2, decision="regenerate_master"
            )

        dispatch.assert_called_once_with()
        self.assertEqual(result["state"], "PRODUCTION_QUEUED")
        self.assertFalse(candidate.exists())
        shots = self.database.execute(
            "SELECT idx,state,cost_cents,video_path,video_qa,error FROM shots "
            "WHERE job_id='J-REGENERATE-ONE' ORDER BY idx"
        ).fetchall()
        self.assertEqual(shots[0]["state"], "OK")
        self.assertEqual(shots[0]["cost_cents"], 700)
        self.assertEqual(shots[0]["video_path"], str(accepted))
        self.assertEqual(accepted.read_bytes(), b"accepted")
        self.assertEqual(shots[1]["state"], "PENDING")
        self.assertEqual(shots[1]["cost_cents"], 600)
        self.assertIsNone(shots[1]["video_qa"])
        self.assertIsNone(shots[1]["error"])
        refreshed = json.loads(self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id='J-REGENERATE-ONE'"
        ).fetchone()[0])
        self.assertNotIn(
            "speech_timeline_version", refreshed["shots"][0]
        )
        self.assertEqual(refreshed["shots"][0]["subtitle_cues"], [])
        self.assertEqual(refreshed["shots"][0]["subtitle"], "旧字幕")
        self.assertEqual(self.database.execute(
            "SELECT duration_sec FROM shots "
            "WHERE job_id='J-REGENERATE-ONE' AND idx=2"
        ).fetchone()[0], 20)

    def test_regenerate_master_waits_while_another_candidate_needs_review(self):
        asset_root = self.root / "data" / "assets" / "J-REGENERATE-MULTI"
        review_dir = asset_root / "shots" / "review"
        review_dir.mkdir(parents=True)
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,error) "
            "VALUES('J-REGENERATE-MULTI','T-1','regenerate multi',"
            "'PRODUCTION_REVIEW_REQUIRED','{}','qa')"
        )
        for idx in (1, 2):
            candidate = review_dir / f"{idx:02d}-candidate.mp4"
            candidate.write_bytes(b"candidate")
            self.database.execute(
                "INSERT INTO shots(job_id,idx,state,video_qa,error) "
                "VALUES('J-REGENERATE-MULTI',?,'REVIEW_REQUIRED',?,?)",
                (idx, json.dumps({
                    "status": "manual_review_required",
                    "candidate_path": str(candidate),
                }), "soft quality gate"),
            )
        self.database.commit()

        with mock.patch.object(self.service, "dispatch_production_queue") as dispatch:
            result = self.service.review_master_candidate(
                "J-REGENERATE-MULTI", 1, decision="regenerate_master"
            )

        dispatch.assert_not_called()
        self.assertEqual(result["state"], "PRODUCTION_REVIEW_REQUIRED")
        self.assertEqual(result["remaining_review_count"], 1)
        states = self.database.execute(
            "SELECT state FROM shots WHERE job_id='J-REGENERATE-MULTI' ORDER BY idx"
        ).fetchall()
        self.assertEqual([row["state"] for row in states], ["PENDING", "REVIEW_REQUIRED"])

    def test_unresolved_provider_task_blocks_regenerate_without_mutation(self):
        asset_root = self.root / "data" / "assets" / "J-REGENERATE-GATED"
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,error) "
            "VALUES('J-REGENERATE-GATED','T-1','regenerate gated',"
            "'PRODUCTION_REVIEW_REQUIRED','{}','qa')"
        )
        qa = json.dumps({
            "status": "manual_review_required", "candidate_path": str(candidate),
        })
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_qa,error) "
            "VALUES('J-REGENERATE-GATED',1,'REVIEW_REQUIRED',?,'qa')", (qa,)
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES('task-pending','J-REGENERATE-GATED',1,"
            "'seedance','master',?,600)",
            (json.dumps({"status": "pending"}),),
        )
        self.database.commit()

        with self.assertRaisesRegex(ValueError, "尚未核对"):
            self.service.review_master_candidate(
                "J-REGENERATE-GATED", 1, decision="regenerate_master"
            )

        shot = self.database.execute(
            "SELECT state,video_qa,error FROM shots "
            "WHERE job_id='J-REGENERATE-GATED'"
        ).fetchone()
        self.assertEqual(shot["state"], "REVIEW_REQUIRED")
        self.assertEqual(shot["video_qa"], qa)
        self.assertEqual(shot["error"], "qa")
        self.assertTrue(candidate.exists())

    def test_reject_job_terminates_all_reviews_without_deleting_paid_candidates(self):
        asset_root = self.root / "data" / "assets" / "J-REJECT-JOB"
        review_dir = asset_root / "shots" / "review"
        review_dir.mkdir(parents=True)
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,error) "
            "VALUES('J-REJECT-JOB','T-1','reject job',"
            "'PRODUCTION_REVIEW_REQUIRED','{}',1200,'qa')"
        )
        candidates = []
        for idx in (1, 2):
            candidate = review_dir / f"{idx:02d}-candidate.mp4"
            candidate.write_bytes(f"candidate-{idx}".encode())
            candidates.append(candidate)
            self.database.execute(
                "INSERT INTO shots(job_id,idx,state,cost_cents,video_qa,error) "
                "VALUES('J-REJECT-JOB',?,'REVIEW_REQUIRED',600,?,?)",
                (idx, json.dumps({
                    "status": "manual_review_required",
                    "candidate_path": str(candidate),
                }), "soft quality gate"),
            )
        self.database.commit()

        with mock.patch.object(self.service, "dispatch_production_queue") as dispatch:
            result = self.service.review_master_candidate(
                "J-REJECT-JOB", 1, decision="reject_job"
            )

        dispatch.assert_not_called()
        self.assertEqual(result["state"], "REJECTED")
        self.assertEqual(result["decision"], "reject_job")
        job = self.database.execute(
            "SELECT state,cost_cents,error FROM jobs WHERE job_id='J-REJECT-JOB'"
        ).fetchone()
        self.assertEqual(job["state"], "REJECTED")
        self.assertEqual(job["cost_cents"], 1200)
        self.assertIn("人工终止", job["error"])
        shots = self.database.execute(
            "SELECT state,video_qa FROM shots WHERE job_id='J-REJECT-JOB' ORDER BY idx"
        ).fetchall()
        self.assertEqual([row["state"] for row in shots], ["FAILED", "FAILED"])
        self.assertTrue(all(
            json.loads(row["video_qa"])["human_decision"] == "reject_job"
            for row in shots
        ))
        self.assertTrue(all(candidate.exists() for candidate in candidates))

    def test_rejected_job_is_terminal_and_all_recovery_entries_refuse_it(self):
        asset_root = self.root / "data" / "assets" / "J-REJECT-TERMINAL"
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"paid-candidate")
        prior = asset_root / "shots" / "01.mp4"
        prior.write_bytes(b"paid-prior")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,error) "
            "VALUES('J-REJECT-TERMINAL','T-1','reject terminal',"
            "'PRODUCTION_REVIEW_REQUIRED','{}',600,'qa')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents,video_path,video_qa,error) "
            "VALUES('J-REJECT-TERMINAL',1,'REVIEW_REQUIRED',600,?,?,?)",
            (str(prior), json.dumps({
                "status": "manual_review_required",
                "candidate_path": str(candidate),
            }), "soft quality gate"),
        )
        self.database.commit()

        result = self.service.review_master_candidate(
            "J-REJECT-TERMINAL", 1, decision="reject_job"
        )

        self.assertEqual(result["state"], "REJECTED")
        job = next(item for item in self.service.videos()["jobs"]
                   if item["job_id"] == "J-REJECT-TERMINAL")
        self.assertEqual(job["state"], "REJECTED")
        self.assertFalse(job["resumable"])
        self.assertTrue(candidate.exists())
        qa = json.loads(self.database.execute(
            "SELECT video_qa FROM shots WHERE job_id='J-REJECT-TERMINAL'"
        ).fetchone()["video_qa"])
        self.assertEqual(qa["human_decision"], "reject_job")
        for action in (
            lambda: self.service.resume_failed_video("J-REJECT-TERMINAL"),
            lambda: self.service.rebuild_failed_director_plan("J-REJECT-TERMINAL"),
            lambda: self.service.prepare_master_regeneration(
                "J-REJECT-TERMINAL", 1, reason="must stay stopped"
            ),
            lambda: self.service.start_video_job("J-REJECT-TERMINAL", confirmed=True),
        ):
            with self.assertRaises(ValueError):
                action()

    def test_accept_all_ok_pauses_before_paid_bgm_when_provider_task_unresolved(self):
        asset_root = self.root / "data" / "assets" / "J-ACCEPT-BGM-GATE"
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        (self.root / ".env").write_text(
            "DEFAULT_MUSIC_PROVIDER=suno\nSUNO_API_KEY=fake\n", encoding="utf-8"
        )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,error) "
            "VALUES('J-ACCEPT-BGM-GATE','T-1','bgm gate',"
            "'PRODUCTION_REVIEW_REQUIRED','{}','qa')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_qa,error) "
            "VALUES('J-ACCEPT-BGM-GATE',1,'REVIEW_REQUIRED',?,'qa')",
            (json.dumps({
                "status": "manual_review_required",
                "candidate_path": str(candidate),
            }),),
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES('task-still-pending','J-ACCEPT-BGM-GATE',"
            "1,'seedance','master',?,600)",
            (json.dumps({"status": "pending"}),),
        )
        self.database.commit()

        with mock.patch.dict(
            os.environ, {"DEFAULT_MUSIC_PROVIDER": "suno"}, clear=False
        ), mock.patch(
            "factory.management_api.s5_bgm.run", new=mock.AsyncMock()
        ) as bgm, mock.patch(
            "factory.management_api.s6_compose.run", new=mock.AsyncMock()
        ) as compose:
            result = self.service.review_master_candidate(
                "J-ACCEPT-BGM-GATE", 1, decision="accept_candidate"
            )

        bgm.assert_not_awaited()
        compose.assert_not_awaited()
        self.assertEqual(result["state"], "PROVIDER_ACTION_REQUIRED")
        self.assertEqual(self.database.execute(
            "SELECT state FROM shots WHERE job_id='J-ACCEPT-BGM-GATE'"
        ).fetchone()["state"], "OK")

    def test_accept_with_existing_bgm_keeps_unresolved_job_reconcilable(self):
        job_id = "J-ACCEPT-EXISTING-BGM-GATE"
        asset_root = self.root / "data" / "assets" / job_id
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        bgm = asset_root / "bgm.mp3"
        bgm.write_bytes(b"existing-bgm")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,bgm_path,error) "
            "VALUES(?, 'T-1', 'existing bgm gate', "
            "'PRODUCTION_REVIEW_REQUIRED', '{}', ?, 'qa')",
            (job_id, str(bgm)),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_qa,error) "
            "VALUES(?,1,'REVIEW_REQUIRED',?,'qa')",
            (job_id, json.dumps({
                "status": "manual_review_required",
                "candidate_path": str(candidate),
            })),
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES('suno-existing-bgm',?,0,'suno',"
            "'bgm',?,0)",
            (job_id, json.dumps({"status": "pending"})),
        )
        self.database.commit()

        with mock.patch(
            "factory.management_api.s5_bgm.run", new=mock.AsyncMock()
        ) as s5, mock.patch(
            "factory.management_api.s6_compose.run", new=mock.AsyncMock()
        ) as s6, mock.patch.object(
            self.service, "dispatch_production_queue"
        ) as dispatch:
            result = self.service.review_master_candidate(
                job_id, 1, decision="accept_candidate"
            )

        self.assertEqual(result["state"], "PROVIDER_ACTION_REQUIRED")
        self.assertEqual(self.database.execute(
            "SELECT state FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()["state"], "PROVIDER_ACTION_REQUIRED")
        self.assertEqual(self.database.execute(
            "SELECT state FROM shots WHERE job_id=? AND idx=1", (job_id,)
        ).fetchone()["state"], "OK")
        s5.assert_not_awaited()
        s6.assert_not_awaited()
        dispatch.assert_not_called()

        with mock.patch(
            "factory.management_api.music.inspect_task",
            new=mock.AsyncMock(return_value={
                "status": "failed", "usage": {}, "cost_cents": 20,
            }),
        ) as inspect:
            reconciled = self.service.reconcile_provider_task(
                job_id, "suno-existing-bgm"
            )
        inspect.assert_awaited_once_with("suno-existing-bgm", provider="suno")
        self.assertEqual(reconciled["remote_status"], "failed")

    def test_resume_candidate_with_unresolved_task_stays_reconcilable(self):
        job_id = "J-RESUME-CANDIDATE-GATE"
        asset_root = self.root / "data" / "assets" / job_id
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        bgm = asset_root / "bgm.mp3"
        bgm.write_bytes(b"existing-bgm")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,bgm_path,error) "
            "VALUES(?, 'T-1', 'resume candidate gate', 'FAILED', '{}', ?, 'qa')",
            (job_id, str(bgm)),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_qa,error) "
            "VALUES(?,1,'FAILED',?,'qa')",
            (job_id, json.dumps({
                "status": "manual_review_required",
                "candidate_path": str(candidate),
            })),
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES('suno-resume-candidate',?,0,'suno',"
            "'bgm',?,0)",
            (job_id, json.dumps({"status": "pending"})),
        )
        self.database.commit()

        with mock.patch(
            "factory.management_api.s5_bgm.run", new=mock.AsyncMock()
        ) as s5, mock.patch(
            "factory.management_api.s6_compose.run", new=mock.AsyncMock()
        ) as s6, mock.patch.object(
            self.service, "dispatch_production_queue"
        ) as dispatch:
            result = self.service.resume_failed_video(job_id)

        self.assertEqual(result["state"], "PROVIDER_ACTION_REQUIRED")
        self.assertEqual(self.database.execute(
            "SELECT state FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()["state"], "PROVIDER_ACTION_REQUIRED")
        self.assertEqual(self.database.execute(
            "SELECT state FROM shots WHERE job_id=? AND idx=1", (job_id,)
        ).fetchone()["state"], "REVIEW_REQUIRED")
        s5.assert_not_awaited()
        s6.assert_not_awaited()
        dispatch.assert_not_called()

        with mock.patch(
            "factory.management_api.music.inspect_task",
            new=mock.AsyncMock(return_value={
                "status": "failed", "usage": {}, "cost_cents": 20,
            }),
        ):
            reconciled = self.service.reconcile_provider_task(
                job_id, "suno-resume-candidate"
            )
        self.assertEqual(reconciled["remote_status"], "failed")

    def test_local_reservation_without_remote_alias_is_not_reconcilable(self):
        job_id = "J-LOCAL-NO-REMOTE-ALIAS"
        local_id = "local-submit:seedance:J-LOCAL-NO-REMOTE-ALIAS:1:generate"
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,error) "
            "VALUES(?, 'T-1', 'no alias', 'PROVIDER_ACTION_REQUIRED', '{}', "
            "'provider result unknown')",
            (job_id,),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,error) "
            "VALUES(?,1,'FAILED','provider result unknown')",
            (job_id,),
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES(?,?,1,'seedance','generate',?,0)",
            (local_id, job_id, json.dumps({"status": "action_required"})),
        )
        self.database.commit()

        unresolved = next(
            job for job in self.service.videos()["jobs"]
            if job["job_id"] == job_id
        )["unresolved_provider_tasks"]
        self.assertEqual(unresolved[0]["task_id"], local_id)
        self.assertFalse(unresolved[0]["reconcilable"])
        self.assertIn("人工", unresolved[0]["action_message"])

        with mock.patch(
            "factory.management_api.seedance.inspect_task",
            new=mock.AsyncMock(),
        ) as inspect:
            with self.assertRaisesRegex(ValueError, "remote_task_id|人工"):
                self.service.reconcile_provider_task(job_id, local_id)

        inspect.assert_not_awaited()
        row = self.database.execute(
            "SELECT usage_json FROM provider_calls WHERE task_id=?", (local_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row["usage_json"])["status"], "action_required")

    def test_local_reservation_self_alias_is_not_promoted_or_inspected(self):
        job_id = "J-LOCAL-SELF-ALIAS"
        local_id = "local-submit:seedance:J-LOCAL-SELF-ALIAS:1:generate"
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,error) "
            "VALUES(?, 'T-1', 'self alias', 'PROVIDER_ACTION_REQUIRED', '{}', "
            "'provider result unknown')",
            (job_id,),
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES(?,?,1,'seedance','generate',?,0)",
            (local_id, job_id, json.dumps({
                "status": "action_required", "remote_task_id": local_id,
            })),
        )
        self.database.commit()

        with mock.patch(
            "factory.management_api.seedance.inspect_task",
            new=mock.AsyncMock(),
        ) as inspect:
            with self.assertRaisesRegex(ValueError, "remote_task_id|人工"):
                self.service.reconcile_provider_task(job_id, local_id)

        inspect.assert_not_awaited()
        row = self.database.execute(
            "SELECT usage_json FROM provider_calls WHERE task_id=?", (local_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(
            json.loads(row["usage_json"])["remote_task_id"], local_id
        )

    def test_accept_uses_same_music_provider_precedence_as_paid_submit(self):
        asset_root = self.root / "data" / "assets" / "J-ACCEPT-CONFIG-RACE"
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        (self.root / ".env").write_text(
            "DEFAULT_MUSIC_PROVIDER=silent\n", encoding="utf-8"
        )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,error) "
            "VALUES('J-ACCEPT-CONFIG-RACE','T-1','config race',"
            "'PRODUCTION_REVIEW_REQUIRED','{}','qa')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_qa,error) "
            "VALUES('J-ACCEPT-CONFIG-RACE',1,'REVIEW_REQUIRED',?,'qa')",
            (json.dumps({
                "status": "manual_review_required",
                "candidate_path": str(candidate),
            }),),
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES('task-config-race','J-ACCEPT-CONFIG-RACE',"
            "1,'seedance','master',?,600)",
            (json.dumps({"status": "pending"}),),
        )
        self.database.commit()

        async def attempted_bgm(_job_id):
            await music.generate("lo-fi", 60)

        with mock.patch.dict(
            os.environ,
            {"DEFAULT_MUSIC_PROVIDER": "suno", "SUNO_API_KEY": "fake"},
            clear=False,
        ), mock.patch(
            "factory.management_api.s5_bgm.run",
            new=mock.AsyncMock(side_effect=attempted_bgm),
        ) as bgm, mock.patch(
            "factory.clients.music._via_suno",
            new=mock.AsyncMock(return_value=b"paid-music"),
        ) as paid_submit, mock.patch(
            "factory.management_api.s6_compose.run", new=mock.AsyncMock()
        ) as compose:
            result = self.service.review_master_candidate(
                "J-ACCEPT-CONFIG-RACE", 1, decision="accept_candidate"
            )

        self.assertEqual(result["state"], "PROVIDER_ACTION_REQUIRED")
        bgm.assert_not_awaited()
        paid_submit.assert_not_awaited()
        compose.assert_not_awaited()

    def test_keep_prior_works_when_candidate_file_is_missing(self):
        asset_root = self.root / "data" / "assets" / "J-KEEP-MISSING-CANDIDATE"
        prior = asset_root / "shots" / "01.mp4"
        prior.parent.mkdir(parents=True)
        prior.write_bytes(b"prior")
        final = asset_root / "final.mp4"
        final.write_bytes(b"final")
        missing = asset_root / "shots" / "review" / "missing.mp4"
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,final_path,error) "
            "VALUES('J-KEEP-MISSING-CANDIDATE','T-1','keep missing',"
            "'PRODUCTION_REVIEW_REQUIRED','{}',?,'qa')", (str(final),)
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
            "VALUES('J-KEEP-MISSING-CANDIDATE',1,'REVIEW_REQUIRED',?,?,?)",
            (str(prior), json.dumps({
                "status": "manual_review_required",
                "candidate_path": str(missing),
            }), "qa"),
        )
        self.database.commit()

        result = self.service.review_master_candidate(
            "J-KEEP-MISSING-CANDIDATE", 1, decision="keep_prior"
        )

        self.assertEqual(result["state"], "COMPOSED")
        self.assertEqual(prior.read_bytes(), b"prior")
        self.assertEqual(self.database.execute(
            "SELECT state FROM shots WHERE job_id='J-KEEP-MISSING-CANDIDATE'"
        ).fetchone()["state"], "OK")

    def test_resume_failed_all_ok_reruns_only_local_postproduction(self):
        asset_root = self.root / "data" / "assets" / "J-LOCAL-RESUME"
        master = asset_root / "shots" / "01.mp4"
        master.parent.mkdir(parents=True)
        master.write_bytes(b"accepted")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,error) "
            "VALUES('J-LOCAL-RESUME','T-1','local resume','FAILED','{}',600,"
            "'local recompose failed: ffmpeg')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents,video_path,video_qa) "
            "VALUES('J-LOCAL-RESUME',1,'OK',600,?,?)",
            (str(master), json.dumps({
                "status": "candidate_accepted",
                "human_decision": "accept_candidate",
            })),
        )
        self.database.commit()

        with mock.patch(
            "factory.management_api.s5_bgm.run", new=mock.AsyncMock()
        ) as bgm, mock.patch(
            "factory.management_api.s6_compose.run", new=mock.AsyncMock()
        ) as compose, mock.patch.object(
            self.service, "dispatch_production_queue"
        ) as dispatch:
            result = self.service.resume_failed_video("J-LOCAL-RESUME")

        bgm.assert_awaited_once_with("J-LOCAL-RESUME", provider="silent")
        compose.assert_awaited_once_with("J-LOCAL-RESUME")
        dispatch.assert_not_called()
        self.assertEqual(result["state"], "COMPOSED")
        shot = self.database.execute(
            "SELECT state,cost_cents,video_path FROM shots "
            "WHERE job_id='J-LOCAL-RESUME'"
        ).fetchone()
        self.assertEqual(dict(shot), {
            "state": "OK", "cost_cents": 600, "video_path": str(master),
        })

    def test_bgm_ok_exposes_local_postproduction_resume_action(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-BGM-STUCK','T-1','bgm stuck','BGM_OK','{}',600)"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents) "
            "VALUES('J-BGM-STUCK',1,'OK',600)"
        )
        self.database.commit()

        job = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-BGM-STUCK"
        )

        self.assertTrue(job["local_postproduction_resumable"])
        self.assertTrue(job["resumable"])
        self.assertEqual(job["next_action"], {
            "id": "resume_local_postproduction",
            "label": "继续本地合成",
        })

    def test_bgm_ok_resume_only_recomposes_and_finalizes_accepted_candidate(self):
        asset_root = self.root / "data" / "assets" / "J-BGM-RESUME"
        master = asset_root / "shots" / "01.mp4"
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        prior = asset_root / "shots" / ".01.prior-review.mp4"
        bgm_path = asset_root / "bgm.mp3"
        candidate.parent.mkdir(parents=True)
        master.write_bytes(b"accepted")
        candidate.write_bytes(b"accepted")
        prior.write_bytes(b"prior")
        bgm_path.write_bytes(b"local-bgm")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,bgm_path) "
            "VALUES('J-BGM-RESUME','T-1','bgm resume','BGM_OK','{}',600,?)",
            (str(bgm_path),),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents,video_path,video_qa) "
            "VALUES('J-BGM-RESUME',1,'OK',600,?,?)",
            (str(master), json.dumps({
                "status": "candidate_accepted_pending",
                "human_decision": "accept_candidate",
                "candidate_path": str(candidate),
                "prior_backup_path": str(prior),
            })),
        )
        self.database.commit()

        with mock.patch(
            "factory.management_api.s5_bgm.run", new=mock.AsyncMock()
        ) as bgm, mock.patch(
            "factory.management_api.s6_compose.run", new=mock.AsyncMock()
        ) as compose, mock.patch.object(
            self.service, "dispatch_production_queue"
        ) as dispatch:
            result = self.service.resume_failed_video("J-BGM-RESUME")

        bgm.assert_not_awaited()
        compose.assert_awaited_once_with("J-BGM-RESUME")
        dispatch.assert_not_called()
        self.assertEqual(result["state"], "COMPOSED")
        job = self.database.execute(
            "SELECT state,cost_cents FROM jobs WHERE job_id='J-BGM-RESUME'"
        ).fetchone()
        self.assertEqual(dict(job), {"state": "COMPOSED", "cost_cents": 600})
        qa = json.loads(self.database.execute(
            "SELECT video_qa FROM shots WHERE job_id='J-BGM-RESUME'"
        ).fetchone()["video_qa"])
        self.assertEqual(qa["status"], "candidate_accepted")
        self.assertFalse(candidate.exists())
        self.assertFalse(prior.exists())
        self.assertEqual(self.database.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE job_id='J-BGM-RESUME'"
        ).fetchone()[0], 0)

    def test_bgm_ok_resume_rebuilds_missing_local_silent_bgm_before_compose(self):
        asset_root = self.root / "data" / "assets" / "J-BGM-MISSING"
        master = asset_root / "shots" / "01.mp4"
        master.parent.mkdir(parents=True)
        master.write_bytes(b"accepted")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-BGM-MISSING','T-1','missing bgm','BGM_OK','{}',600)"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents,video_path) "
            "VALUES('J-BGM-MISSING',1,'OK',600,?)",
            (str(master),),
        )
        self.database.commit()

        with mock.patch(
            "factory.management_api.s5_bgm.run", new=mock.AsyncMock()
        ) as bgm, mock.patch(
            "factory.management_api.s6_compose.run", new=mock.AsyncMock()
        ) as compose, mock.patch.object(
            self.service, "dispatch_production_queue"
        ) as dispatch:
            result = self.service.resume_failed_video("J-BGM-MISSING")

        bgm.assert_awaited_once_with("J-BGM-MISSING", provider="silent")
        compose.assert_awaited_once_with("J-BGM-MISSING")
        dispatch.assert_not_called()
        self.assertEqual(result["state"], "COMPOSED")
        self.assertEqual(self.database.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE job_id='J-BGM-MISSING'"
        ).fetchone()[0], 0)

    def test_local_resume_uses_same_music_provider_precedence_as_paid_submit(self):
        asset_root = self.root / "data" / "assets" / "J-RESUME-CONFIG-RACE"
        master = asset_root / "shots" / "01.mp4"
        master.parent.mkdir(parents=True)
        master.write_bytes(b"accepted")
        (self.root / ".env").write_text(
            "DEFAULT_MUSIC_PROVIDER=silent\n", encoding="utf-8"
        )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,error) "
            "VALUES('J-RESUME-CONFIG-RACE','T-1','resume race','FAILED','{}',600,"
            "'local postproduction failed')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents,video_path) "
            "VALUES('J-RESUME-CONFIG-RACE',1,'OK',600,?)",
            (str(master),),
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES('task-resume-race','J-RESUME-CONFIG-RACE',"
            "1,'seedance','master',?,600)",
            (json.dumps({"status": "pending"}),),
        )
        self.database.commit()

        async def attempted_bgm(_job_id):
            await music.generate("lo-fi", 60)

        with mock.patch.dict(
            os.environ,
            {"DEFAULT_MUSIC_PROVIDER": "suno", "SUNO_API_KEY": "fake"},
            clear=False,
        ), mock.patch(
            "factory.management_api.s5_bgm.run",
            new=mock.AsyncMock(side_effect=attempted_bgm),
        ) as bgm, mock.patch(
            "factory.clients.music._via_suno",
            new=mock.AsyncMock(return_value=b"paid-music"),
        ) as paid_submit, mock.patch(
            "factory.management_api.s6_compose.run", new=mock.AsyncMock()
        ) as compose:
            result = self.service.resume_failed_video("J-RESUME-CONFIG-RACE")

        bgm.assert_not_awaited()
        paid_submit.assert_not_awaited()
        compose.assert_not_awaited()
        self.assertEqual(result["state"], "PROVIDER_ACTION_REQUIRED")
        self.assertIn("task-resume-race", result["unresolved_provider_tasks"])
        self.assertEqual(self.database.execute(
            "SELECT state FROM jobs WHERE job_id='J-RESUME-CONFIG-RACE'"
        ).fetchone()["state"], "PROVIDER_ACTION_REQUIRED")

    def test_accept_only_candidate_runs_local_bgm_then_compose(self):
        asset_root = self.root / "data" / "assets" / "J-ACCEPT-COMPLETE"
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,error) "
            "VALUES('J-ACCEPT-COMPLETE','T-1','accept complete',"
            "'PRODUCTION_REVIEW_REQUIRED','{}','qa')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_qa,error) "
            "VALUES('J-ACCEPT-COMPLETE',1,'REVIEW_REQUIRED',?,?)",
            (json.dumps({
                "status": "manual_review_required",
                "candidate_path": str(candidate),
            }), "soft quality gate"),
        )
        self.database.commit()

        with mock.patch(
            "factory.management_api.s5_bgm.run", new=mock.AsyncMock()
        ) as bgm, mock.patch(
            "factory.management_api.s6_compose.run", new=mock.AsyncMock()
        ) as compose:
            result = self.service.review_master_candidate(
                "J-ACCEPT-COMPLETE", 1, decision="accept_candidate"
            )

        bgm.assert_awaited_once_with("J-ACCEPT-COMPLETE", provider="silent")
        compose.assert_awaited_once_with("J-ACCEPT-COMPLETE")
        self.assertEqual(result["state"], "COMPOSED")
        self.assertEqual(self.database.execute(
            "SELECT state FROM jobs WHERE job_id='J-ACCEPT-COMPLETE'"
        ).fetchone()["state"], "COMPOSED")

    def test_first_candidate_stays_accepted_when_local_compose_fails(self):
        asset_root = self.root / "data" / "assets" / "J-FIRST-COMPOSE-FAIL"
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,error) "
            "VALUES('J-FIRST-COMPOSE-FAIL','T-1','compose fail',"
            "'PRODUCTION_REVIEW_REQUIRED','{}','qa')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_qa,error) "
            "VALUES('J-FIRST-COMPOSE-FAIL',1,'REVIEW_REQUIRED',?,?)",
            (json.dumps({
                "status": "manual_review_required",
                "candidate_path": str(candidate),
            }), "soft quality gate"),
        )
        self.database.commit()

        with mock.patch(
            "factory.management_api.s5_bgm.run", new=mock.AsyncMock()
        ), mock.patch(
            "factory.management_api.s6_compose.run",
            new=mock.AsyncMock(side_effect=RuntimeError("ffmpeg failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "ffmpeg failed"):
                self.service.review_master_candidate(
                    "J-FIRST-COMPOSE-FAIL", 1, decision="accept_candidate"
                )

        job = self.database.execute(
            "SELECT state,error FROM jobs WHERE job_id='J-FIRST-COMPOSE-FAIL'"
        ).fetchone()
        shot = self.database.execute(
            "SELECT state,video_path,video_qa,error FROM shots "
            "WHERE job_id='J-FIRST-COMPOSE-FAIL'"
        ).fetchone()
        self.assertEqual(job["state"], "FAILED")
        self.assertEqual(
            job["error"],
            "生产过程出现未识别异常，已保留现场，请重新诊断",
        )
        self.assertEqual(shot["state"], "OK")
        self.assertIsNone(shot["error"])
        self.assertTrue(Path(shot["video_path"]).is_file())
        self.assertEqual(
            json.loads(shot["video_qa"])["human_decision"], "accept_candidate"
        )

    def test_keep_prior_master_discards_candidate_and_restores_composed_job(self):
        asset_root = self.root / "data" / "assets" / "J-KEEP-PRIOR"
        candidate = asset_root / "shots" / "review" / "02-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"rejected-candidate")
        old_master = asset_root / "shots" / "02.mp4"
        old_master.write_bytes(b"accepted-master")
        final = asset_root / "final.mp4"
        final.write_bytes(b"accepted-final")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,final_path,error) "
            "VALUES('J-KEEP-PRIOR','T-1','keep prior','FAILED','{}',?,'qa')",
            (str(final),),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error,cost_cents) "
            "VALUES('J-KEEP-PRIOR',2,'FAILED',?,?,?,608)",
            (
                str(old_master),
                json.dumps({
                    "status": "manual_review_required",
                    "reasons": ["meaningless_hand_closeup"],
                    "candidate_path": str(candidate),
                }),
                "semantic review required",
            ),
        )
        self.database.commit()

        result = self.service.review_master_candidate(
            "J-KEEP-PRIOR", 2, decision="keep_prior"
        )

        job = self.database.execute(
            "SELECT state,final_path,error FROM jobs WHERE job_id='J-KEEP-PRIOR'"
        ).fetchone()
        shot = self.database.execute(
            "SELECT state,video_path,video_qa,error,cost_cents FROM shots "
            "WHERE job_id='J-KEEP-PRIOR' AND idx=2"
        ).fetchone()
        self.assertEqual(result["decision"], "keep_prior")
        self.assertEqual(job["state"], "COMPOSED")
        self.assertEqual(job["final_path"], str(final))
        self.assertIsNone(job["error"])
        self.assertEqual(shot["state"], "OK")
        self.assertEqual(shot["video_path"], str(old_master))
        self.assertIsNone(shot["error"])
        self.assertEqual(shot["cost_cents"], 608)
        self.assertEqual(old_master.read_bytes(), b"accepted-master")
        self.assertEqual(final.read_bytes(), b"accepted-final")
        self.assertFalse(candidate.exists())
        resolved_qa = json.loads(shot["video_qa"])
        self.assertEqual(resolved_qa["status"], "candidate_rejected")
        self.assertEqual(resolved_qa["human_decision"], "keep_prior")
        self.assertTrue(resolved_qa["reviewed_at"])

    def test_legacy_keep_prior_cannot_complete_without_a_prior_master(self):
        asset_root = self.root / "data" / "assets" / "J-NO-PRIOR"
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"paid-candidate")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,error) "
            "VALUES('J-NO-PRIOR','T-1','no prior','FAILED','{}','qa')"
        )
        qa = json.dumps({
            "status": "manual_review_required", "candidate_path": str(candidate),
        })
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_qa,error) "
            "VALUES('J-NO-PRIOR',1,'FAILED',?,'qa')", (qa,)
        )
        self.database.commit()

        with self.assertRaisesRegex(ValueError, "没有可保留的旧版"):
            self.service.review_master_candidate(
                "J-NO-PRIOR", 1, decision="keep_prior"
            )

        shot = self.database.execute(
            "SELECT state,video_qa FROM shots WHERE job_id='J-NO-PRIOR'"
        ).fetchone()
        self.assertEqual(shot["state"], "FAILED")
        self.assertEqual(shot["video_qa"], qa)
        self.assertTrue(candidate.exists())

    def test_resolving_one_of_two_candidates_keeps_job_in_review_queue(self):
        asset_root = self.root / "data" / "assets" / "J-MULTI-REVIEW"
        review_dir = asset_root / "shots" / "review"
        review_dir.mkdir(parents=True)
        final = asset_root / "final.mp4"
        final.write_bytes(b"prior-final")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,final_path,error) "
            "VALUES('J-MULTI-REVIEW','T-1','multi review','PRODUCTION_REVIEW_REQUIRED','{}',?,'qa')",
            (str(final),),
        )
        for idx in (1, 2):
            prior = asset_root / "shots" / f"{idx:02d}.mp4"
            prior.write_bytes(f"prior-{idx}".encode())
            candidate = review_dir / f"{idx:02d}-candidate.mp4"
            candidate.write_bytes(f"candidate-{idx}".encode())
            self.database.execute(
                "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
                "VALUES('J-MULTI-REVIEW',?,'FAILED',?,?,?)",
                (
                    idx,
                    str(prior),
                    json.dumps({
                        "status": "manual_review_required",
                        "reasons": ["missing_environment_context"],
                        "candidate_path": str(candidate),
                    }),
                    "semantic review required",
                ),
            )
        self.database.commit()

        result = self.service.review_master_candidate(
            "J-MULTI-REVIEW", 1, decision="keep_prior"
        )

        job = self.database.execute(
            "SELECT state,error FROM jobs WHERE job_id='J-MULTI-REVIEW'"
        ).fetchone()
        shots = self.database.execute(
            "SELECT idx,state,video_qa FROM shots WHERE job_id='J-MULTI-REVIEW' "
            "ORDER BY idx"
        ).fetchall()
        self.assertEqual(result["state"], "PRODUCTION_REVIEW_REQUIRED")
        self.assertEqual(result["remaining_review_count"], 1)
        self.assertEqual(job["state"], "PRODUCTION_REVIEW_REQUIRED")
        self.assertIn("1 条母片待人工复核", job["error"])
        self.assertEqual(shots[0]["state"], "OK")
        self.assertEqual(shots[1]["state"], "FAILED")
        self.assertEqual(
            json.loads(shots[1]["video_qa"])["status"],
            "manual_review_required",
        )
        self.assertEqual(self.service.overview()["videos"]["review_required"], 1)

    def test_accepting_candidate_waits_for_all_reviews_before_local_recompose(self):
        asset_root = self.root / "data" / "assets" / "J-MULTI-ACCEPT"
        review_dir = asset_root / "shots" / "review"
        review_dir.mkdir(parents=True)
        final = asset_root / "final.mp4"
        final.write_bytes(b"prior-final")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,final_path,error) "
            "VALUES('J-MULTI-ACCEPT','T-1','multi accept','PRODUCTION_REVIEW_REQUIRED','{}',?,'qa')",
            (str(final),),
        )
        for idx in (1, 2):
            prior = asset_root / "shots" / f"{idx:02d}.mp4"
            prior.write_bytes(f"prior-{idx}".encode())
            candidate = review_dir / f"{idx:02d}-candidate.mp4"
            candidate.write_bytes(f"candidate-{idx}".encode())
            self.database.execute(
                "INSERT INTO shots(job_id,idx,state,video_path,video_url,video_qa,error) "
                "VALUES('J-MULTI-ACCEPT',?,'FAILED',?,'https://cdn/prior.mp4',?,?)",
                (
                    idx,
                    str(prior),
                    json.dumps({
                        "status": "manual_review_required",
                        "reasons": ["missing_meaningful_action"],
                        "candidate_path": str(candidate),
                        "candidate_url": f"https://cdn/candidate-{idx}.mp4",
                    }),
                    "semantic review required",
                ),
            )
        self.database.commit()

        async def local_recompose(job_id):
            self.assertEqual(job_id, "J-MULTI-ACCEPT")
            final.write_bytes(b"recomposed-final")

        with mock.patch(
            "factory.management_api.s6_compose.run",
            new=mock.AsyncMock(side_effect=local_recompose),
        ) as compose:
            first = self.service.review_master_candidate(
                "J-MULTI-ACCEPT", 1, decision="accept_candidate"
            )
            compose.assert_not_awaited()
            self.assertEqual(first["state"], "PRODUCTION_REVIEW_REQUIRED")
            self.assertEqual(first["remaining_review_count"], 1)
            self.assertEqual(final.read_bytes(), b"prior-final")
            mid_job = next(
                item for item in self.service.videos()["jobs"]
                if item["job_id"] == "J-MULTI-ACCEPT"
            )
            self.assertEqual(
                mid_job["shots"][0]["quality_status"],
                "candidate_accepted_pending",
            )
            self.assertTrue(mid_job["shots"][0]["review_resolved"])
            self.assertIsNone(mid_job["shots"][0]["candidate_preview_url"])
            self.assertTrue(mid_job["shots"][1]["manual_review_required"])
            self.assertIsNotNone(mid_job["shots"][1]["candidate_preview_url"])

            second = self.service.review_master_candidate(
                "J-MULTI-ACCEPT", 2, decision="keep_prior"
            )

        compose.assert_awaited_once_with("J-MULTI-ACCEPT")
        self.assertEqual(second["state"], "COMPOSED")
        self.assertEqual(second["remaining_review_count"], 0)
        self.assertEqual(final.read_bytes(), b"recomposed-final")
        job = self.database.execute(
            "SELECT state,error FROM jobs WHERE job_id='J-MULTI-ACCEPT'"
        ).fetchone()
        shots = self.database.execute(
            "SELECT idx,state,video_path,video_qa FROM shots "
            "WHERE job_id='J-MULTI-ACCEPT' ORDER BY idx"
        ).fetchall()
        self.assertEqual(job["state"], "COMPOSED")
        self.assertIsNone(job["error"])
        self.assertEqual([shot["state"] for shot in shots], ["OK", "OK"])
        self.assertEqual(
            json.loads(shots[0]["video_qa"])["status"], "candidate_accepted"
        )
        self.assertEqual(
            Path(shots[0]["video_path"]).read_bytes(), b"candidate-1"
        )
        self.assertEqual(
            json.loads(shots[1]["video_qa"])["status"], "candidate_rejected"
        )

    def test_accept_then_regenerate_finalizes_pending_accept_after_production(self):
        asset_root = self.root / "data" / "assets" / "J-ACCEPT-THEN-REGEN"
        review_dir = asset_root / "shots" / "review"
        review_dir.mkdir(parents=True)
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,error) "
            "VALUES('J-ACCEPT-THEN-REGEN','T-1','mixed decisions',"
            "'PRODUCTION_REVIEW_REQUIRED','{}',1200,'qa')"
        )
        paths = {}
        for idx in (1, 2):
            prior = asset_root / "shots" / f"{idx:02d}.mp4"
            prior.write_bytes(f"prior-{idx}".encode())
            candidate = review_dir / f"{idx:02d}-candidate.mp4"
            candidate.write_bytes(f"candidate-{idx}".encode())
            if idx == 1:
                prior.with_suffix(".wav").write_bytes(b"prior-audio-1")
                candidate.with_suffix(".wav").write_bytes(b"candidate-audio-1")
            paths[idx] = (prior, candidate)
            self.database.execute(
                "INSERT INTO shots(job_id,idx,state,cost_cents,video_path,video_qa,error) "
                "VALUES('J-ACCEPT-THEN-REGEN',?,'REVIEW_REQUIRED',600,?,?,?)",
                (idx, str(prior), json.dumps({
                    "status": "manual_review_required",
                    "candidate_path": str(candidate),
                }), "soft quality gate"),
            )
        self.database.commit()

        first = self.service.review_master_candidate(
            "J-ACCEPT-THEN-REGEN", 1, decision="accept_candidate"
        )
        with mock.patch.object(
            self.service, "dispatch_production_queue",
            return_value={"dispatched_jobs": ["J-ACCEPT-THEN-REGEN"]},
        ) as dispatch:
            second = self.service.review_master_candidate(
                "J-ACCEPT-THEN-REGEN", 2, decision="regenerate_master"
            )

        self.assertEqual(first["state"], "PRODUCTION_REVIEW_REQUIRED")
        self.assertEqual(second["state"], "PRODUCTION_QUEUED")
        dispatch.assert_called_once_with()
        rows = self.database.execute(
            "SELECT idx,state,cost_cents,video_path,video_qa FROM shots "
            "WHERE job_id='J-ACCEPT-THEN-REGEN' ORDER BY idx"
        ).fetchall()
        self.assertEqual([row["state"] for row in rows], ["OK", "PENDING"])
        self.assertEqual([row["cost_cents"] for row in rows], [600, 600])
        self.assertEqual(paths[1][0].read_bytes(), b"candidate-1")
        self.assertEqual(
            json.loads(rows[0]["video_qa"])["status"],
            "candidate_accepted_pending",
        )
        self.assertTrue(paths[2][0].is_file())
        self.assertFalse(paths[2][1].exists())

        self.database.execute(
            "UPDATE shots SET state='OK',video_path=? "
            "WHERE job_id='J-ACCEPT-THEN-REGEN' AND idx=2",
            (str(paths[2][0]),),
        )
        self.database.commit()
        finalized = review_candidates.finalize_accepted_candidates(
            self.database, self.root / "data" / "assets",
            "J-ACCEPT-THEN-REGEN",
        )
        finalized_again = review_candidates.finalize_accepted_candidates(
            self.database, self.root / "data" / "assets",
            "J-ACCEPT-THEN-REGEN",
        )

        final_qa = json.loads(self.database.execute(
            "SELECT video_qa FROM shots "
            "WHERE job_id='J-ACCEPT-THEN-REGEN' AND idx=1"
        ).fetchone()["video_qa"])
        self.assertEqual((finalized, finalized_again), (1, 0))
        self.assertEqual(final_qa["status"], "candidate_accepted")
        self.assertNotIn("candidate_path", final_qa)
        self.assertNotIn("prior_backup_path", final_qa)
        self.assertNotIn("review_operation", final_qa)
        self.assertEqual(
            paths[1][0].with_suffix(".wav").read_bytes(), b"candidate-audio-1"
        )
        self.assertEqual(
            list((asset_root / "review" / "staging").glob("*")), []
        )

    def test_accept_candidate_replaces_master_and_recomposes_locally(self):
        asset_root = self.root / "data" / "assets" / "J-ACCEPT-CANDIDATE"
        candidate = asset_root / "shots" / "review" / "02-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"approved-candidate")
        old_master = asset_root / "shots" / "02.mp4"
        old_master.write_bytes(b"accepted-master")
        final = asset_root / "final.mp4"
        final.write_bytes(b"accepted-final")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,final_path,error) "
            "VALUES('J-ACCEPT-CANDIDATE','T-1','accept candidate','FAILED','{}',?,'qa')",
            (str(final),),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_url,video_qa,error,cost_cents) "
            "VALUES('J-ACCEPT-CANDIDATE',2,'FAILED',?,'https://cdn/old.mp4',?,?,608)",
            (
                str(old_master),
                json.dumps({
                    "status": "manual_review_required",
                    "reasons": ["missing_environment_context"],
                    "candidate_path": str(candidate),
                    "candidate_url": "https://cdn/candidate.mp4",
                }),
                "semantic review required",
            ),
        )
        self.database.commit()

        async def local_recompose(job_id):
            self.assertEqual(job_id, "J-ACCEPT-CANDIDATE")
            final.write_bytes(b"recomposed-final")

        with mock.patch(
            "factory.management_api.s6_compose.run",
            new=mock.AsyncMock(side_effect=local_recompose),
        ) as compose:
            result = self.service.review_master_candidate(
                "J-ACCEPT-CANDIDATE", 2, decision="accept_candidate"
            )

        shot = self.database.execute(
            "SELECT state,video_path,video_url,video_qa,error,cost_cents "
            "FROM shots WHERE job_id='J-ACCEPT-CANDIDATE' AND idx=2"
        ).fetchone()
        job = self.database.execute(
            "SELECT state,error FROM jobs WHERE job_id='J-ACCEPT-CANDIDATE'"
        ).fetchone()
        compose.assert_awaited_once_with("J-ACCEPT-CANDIDATE")
        self.assertEqual(result["decision"], "accept_candidate")
        self.assertEqual(job["state"], "COMPOSED")
        self.assertIsNone(job["error"])
        self.assertEqual(shot["state"], "OK")
        self.assertEqual(shot["video_path"], str(old_master.resolve()))
        self.assertEqual(shot["video_url"], "https://cdn/candidate.mp4")
        self.assertEqual(shot["cost_cents"], 608)
        self.assertEqual(old_master.read_bytes(), b"approved-candidate")
        self.assertEqual(final.read_bytes(), b"recomposed-final")
        self.assertFalse(candidate.exists())
        resolved_qa = json.loads(shot["video_qa"])
        self.assertEqual(resolved_qa["status"], "candidate_accepted")
        self.assertEqual(resolved_qa["human_decision"], "accept_candidate")
        self.assertTrue(resolved_qa["reviewed_at"])

    def test_readable_text_candidate_can_be_accepted_by_human_with_warning(self):
        asset_root = self.root / "data" / "assets" / "J-TEXT-OVERLAY"
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate-with-overlay")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,error) "
            "VALUES('J-TEXT-OVERLAY','T-1','overlay','PRODUCTION_REVIEW_REQUIRED','{}','qa')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_qa,error,cost_cents) "
            "VALUES('J-TEXT-OVERLAY',1,'REVIEW_REQUIRED',?,?,600)",
            (
                json.dumps({
                    "status": "manual_review_required",
                    "reasons": ["unexpected_readable_text"],
                    "candidate_path": str(candidate),
                    "candidate_url": "https://cdn/candidate.mp4",
                }),
                "画面出现可读文字",
            ),
        )
        self.database.commit()

        with mock.patch(
            "factory.management_api.s5_bgm.run", new=mock.AsyncMock()
        ), mock.patch(
            "factory.management_api.s6_compose.run", new=mock.AsyncMock()
        ):
            result = self.service.review_master_candidate(
                "J-TEXT-OVERLAY", 1, decision="accept_candidate"
            )

        shot = self.database.execute(
            "SELECT state,video_qa FROM shots "
            "WHERE job_id='J-TEXT-OVERLAY' AND idx=1"
        ).fetchone()
        self.assertEqual(result["decision"], "accept_candidate")
        self.assertEqual(shot["state"], "OK")
        self.assertFalse(candidate.is_file())
        qa = json.loads(shot["video_qa"])
        self.assertEqual(qa["human_decision"], "accept_candidate")
        self.assertIn("unexpected_readable_text", qa["reasons"])

    def test_committed_accept_final_backup_cleanup_failure_returns_success(self):
        job_id = "J-ACCEPT-FINAL-CLEANUP-DENIED"
        asset_root = self.root / "data" / "assets" / job_id
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        active = asset_root / "shots" / "01.mp4"
        active.write_bytes(b"prior-master")
        final = asset_root / "final.mp4"
        final.write_bytes(b"prior-final")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,final_path) "
            "VALUES(?,?,?,'PRODUCTION_REVIEW_REQUIRED','{}',?)",
            (job_id, "T-1", "accept cleanup", str(final)),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
            "VALUES(?,1,'REVIEW_REQUIRED',?,?,'qa')",
            (job_id, str(active), json.dumps({
                "status": "manual_review_required",
                "candidate_path": str(candidate),
            })),
        )
        self.database.commit()

        async def compose(_job_id):
            final.write_bytes(b"recomposed")

        real_unlink = Path.unlink

        def deny_final_backup_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith("final-") and Path(path).is_file():
                raise PermissionError("final backup cleanup denied")
            return real_unlink(path, *args, **kwargs)

        with mock.patch(
            "factory.management_api.s6_compose.run",
            new=mock.AsyncMock(side_effect=compose),
        ), mock.patch.object(Path, "unlink", new=deny_final_backup_cleanup):
            result = self.service.review_master_candidate(
                job_id, 1, decision="accept_candidate"
            )

        self.assertEqual(result["state"], "COMPOSED")
        self.assertEqual(self.database.execute(
            "SELECT state FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()[0], "COMPOSED")
        self.assertEqual(final.read_bytes(), b"recomposed")

    def test_video_api_exposes_resolved_candidate_review_audit(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-REVIEW-AUDIT','T-1','review audit','COMPOSED','{}')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_qa,error) VALUES(?,?,?,?,NULL)",
            (
                "J-REVIEW-AUDIT",
                2,
                "OK",
                json.dumps({
                    "status": "candidate_rejected",
                    "reasons": ["missing_environment_context"],
                    "human_decision": "keep_prior",
                    "reviewed_at": "2026-08-14T10:30:00Z",
                }),
            ),
        )
        self.database.commit()

        job = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-REVIEW-AUDIT"
        )
        shot = job["shots"][0]

        self.assertEqual(shot["quality_status"], "candidate_rejected")
        self.assertEqual(shot["human_decision"], "keep_prior")
        self.assertEqual(shot["reviewed_at"], "2026-08-14T10:30:00Z")
        self.assertTrue(shot["review_resolved"])
        self.assertFalse(shot["manual_review_required"])

    def test_accept_candidate_preserves_decision_when_local_recompose_fails(self):
        asset_root = self.root / "data" / "assets" / "J-CANDIDATE-ROLLBACK"
        candidate = asset_root / "shots" / "review" / "02-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate-that-failed-compose")
        old_master = asset_root / "shots" / "02.mp4"
        old_master.write_bytes(b"prior-master")
        final = asset_root / "final.mp4"
        final.write_bytes(b"prior-final")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,final_path,error) "
            "VALUES('J-CANDIDATE-ROLLBACK','T-1','rollback','FAILED','{}',?,'qa')",
            (str(final),),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_url,video_qa,error) "
            "VALUES('J-CANDIDATE-ROLLBACK',2,'FAILED',?,'https://cdn/prior.mp4',?,?)",
            (
                str(old_master),
                json.dumps({
                    "status": "manual_review_required",
                    "reasons": ["missing_meaningful_action"],
                    "candidate_path": str(candidate),
                    "candidate_url": "https://cdn/candidate.mp4",
                }),
                "semantic review required",
            ),
        )
        self.database.commit()

        with mock.patch(
            "factory.management_api.s6_compose.run",
            new=mock.AsyncMock(side_effect=RuntimeError("ffmpeg failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "ffmpeg failed"):
                self.service.review_master_candidate(
                    "J-CANDIDATE-ROLLBACK", 2, decision="accept_candidate"
                )

        shot = self.database.execute(
            "SELECT state,video_url,video_qa,error FROM shots "
            "WHERE job_id='J-CANDIDATE-ROLLBACK' AND idx=2"
        ).fetchone()
        job = self.database.execute(
            "SELECT state,error FROM jobs WHERE job_id='J-CANDIDATE-ROLLBACK'"
        ).fetchone()
        self.assertEqual(job["state"], "FAILED")
        self.assertEqual(
            job["error"],
            "生产过程出现未识别异常，已保留现场，请重新诊断",
        )
        self.assertEqual(shot["state"], "OK")
        self.assertEqual(shot["video_url"], "https://cdn/candidate.mp4")
        self.assertEqual(old_master.read_bytes(), b"candidate-that-failed-compose")
        self.assertEqual(final.read_bytes(), b"prior-final")
        self.assertTrue(candidate.exists())
        qa = json.loads(shot["video_qa"])
        self.assertEqual(qa["status"], "candidate_accepted_pending")
        self.assertEqual(qa["human_decision"], "accept_candidate")
        self.assertTrue(Path(qa["prior_backup_path"]).is_file())
        self.assertTrue(
            Path(qa["prior_backup_path"]).resolve().is_relative_to(
                (asset_root / "review" / "staging").resolve()
            )
        )

    def test_review_rejects_external_prior_without_touching_paid_files(self):
        asset_root = self.root / "data" / "assets" / "J-EXTERNAL-PRIOR"
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"controlled-candidate")
        external = self.root / "legacy-external-prior.mp4"
        external.write_bytes(b"external-paid-master")
        qa = json.dumps({
            "status": "manual_review_required", "candidate_path": str(candidate),
        })
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-EXTERNAL-PRIOR','T-1','external prior',"
            "'PRODUCTION_REVIEW_REQUIRED','{}')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
            "VALUES('J-EXTERNAL-PRIOR',1,'REVIEW_REQUIRED',?,?,'qa')",
            (str(external), qa),
        )
        other = asset_root / "shots" / "review" / "02-candidate.mp4"
        other.write_bytes(b"other")
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_qa,error) "
            "VALUES('J-EXTERNAL-PRIOR',2,'REVIEW_REQUIRED',?,'qa')",
            (json.dumps({"status": "manual_review_required",
                         "candidate_path": str(other)}),),
        )
        self.database.commit()

        with self.assertRaisesRegex(ValueError, "受控目录"):
            self.service.review_master_candidate(
                "J-EXTERNAL-PRIOR", 1, decision="accept_candidate"
            )

        self.assertEqual(external.read_bytes(), b"external-paid-master")
        self.assertEqual(candidate.read_bytes(), b"controlled-candidate")
        row = self.database.execute(
            "SELECT state,video_path,video_qa FROM shots "
            "WHERE job_id='J-EXTERNAL-PRIOR'"
        ).fetchone()
        self.assertEqual((row["state"], row["video_path"], row["video_qa"]),
                         ("REVIEW_REQUIRED", str(external), qa))

    def test_review_rejects_external_final_and_bgm_before_candidate_mutation(self):
        for suffix, column in (("final", "final_path"), ("bgm", "bgm_path")):
            with self.subTest(column=column):
                job_id = f"J-EXTERNAL-{suffix.upper()}"
                asset_root = self.root / "data" / "assets" / job_id
                candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
                candidate.parent.mkdir(parents=True)
                candidate.write_bytes(b"controlled-candidate")
                external = self.root / f"legacy-external-{suffix}.bin"
                external.write_bytes(b"external-asset")
                self.database.execute(
                    f"INSERT INTO jobs(job_id,topic_id,title,state,storyboard,{column}) "
                    "VALUES(?,?,?,'PRODUCTION_REVIEW_REQUIRED','{}',?)",
                    (job_id, "T-1", suffix, str(external)),
                )
                self.database.execute(
                    "INSERT INTO shots(job_id,idx,state,video_qa,error) "
                    "VALUES(?,1,'REVIEW_REQUIRED',?,'qa')",
                    (job_id, json.dumps({
                        "status": "manual_review_required",
                        "candidate_path": str(candidate),
                    })),
                )
                other = candidate.parent / "02-candidate.mp4"
                other.write_bytes(b"other")
                self.database.execute(
                    "INSERT INTO shots(job_id,idx,state,video_qa,error) "
                    "VALUES(?,2,'REVIEW_REQUIRED',?,'qa')",
                    (job_id, json.dumps({"status": "manual_review_required",
                                         "candidate_path": str(other)})),
                )
                self.database.commit()

                with self.assertRaisesRegex(ValueError, "受控目录"):
                    self.service.review_master_candidate(
                        job_id, 1, decision="accept_candidate"
                    )

                self.assertEqual(external.read_bytes(), b"external-asset")
                self.assertEqual(candidate.read_bytes(), b"controlled-candidate")
                self.assertEqual(self.database.execute(
                    "SELECT state FROM shots WHERE job_id=?", (job_id,)
                ).fetchone()["state"], "REVIEW_REQUIRED")

    def test_finalizer_keeps_external_audit_references_and_files(self):
        external_candidate = self.root / "external-candidate.mp4"
        external_backup = self.root / "external-backup.mp4"
        external_candidate.write_bytes(b"candidate")
        external_backup.write_bytes(b"backup")
        active = self.root / "data" / "assets" / "J-FINALIZE-EXTERNAL" / "shots" / "01.mp4"
        active.parent.mkdir(parents=True)
        active.write_bytes(b"active")
        qa = {
            "status": "candidate_accepted_pending",
            "candidate_path": str(external_candidate),
            "prior_backup_path": str(external_backup),
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) "
            "VALUES('J-FINALIZE-EXTERNAL','T-1','external audit','COMPOSED')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa) "
            "VALUES('J-FINALIZE-EXTERNAL',1,'OK',?,?)",
            (str(active), json.dumps(qa)),
        )
        self.database.commit()

        finalized = review_candidates.finalize_accepted_candidates(
            self.database, self.root / "data" / "assets", "J-FINALIZE-EXTERNAL"
        )

        self.assertEqual(finalized, 0)
        self.assertEqual(external_candidate.read_bytes(), b"candidate")
        self.assertEqual(external_backup.read_bytes(), b"backup")
        persisted = json.loads(self.database.execute(
            "SELECT video_qa FROM shots WHERE job_id='J-FINALIZE-EXTERNAL'"
        ).fetchone()["video_qa"])
        self.assertEqual(persisted, qa)

    def test_concurrent_accept_claim_allows_exactly_one_decision(self):
        database_path = self.root / "review-cas.sqlite3"
        seed = sqlite3.connect(database_path)
        seed.row_factory = sqlite3.Row
        seed.executescript(db.DDL)
        asset_root = self.root / "data" / "assets" / "J-REVIEW-CAS"
        review_dir = asset_root / "shots" / "review"
        review_dir.mkdir(parents=True)
        candidate = review_dir / "01-candidate.mp4"
        candidate.write_bytes(b"candidate")
        prior = asset_root / "shots" / "01.mp4"
        prior.write_bytes(b"prior")
        other_candidate = review_dir / "02-candidate.mp4"
        other_candidate.write_bytes(b"other")
        seed.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-REVIEW-CAS','T-1','cas','PRODUCTION_REVIEW_REQUIRED','{}')"
        )
        for idx, path, old in ((1, candidate, prior), (2, other_candidate, None)):
            seed.execute(
                "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
                "VALUES('J-REVIEW-CAS',?,'REVIEW_REQUIRED',?,?,'qa')",
                (idx, str(old) if old else None, json.dumps({
                    "status": "manual_review_required", "candidate_path": str(path),
                })),
            )
        seed.commit()
        seed.close()
        barrier = threading.Barrier(2)
        outcomes = []
        connections = []
        services = []
        for _ in range(2):
            connection = sqlite3.connect(database_path, timeout=5,
                                         check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connections.append(connection)
            services.append(ManagementService(connection, self.root))

        def decide(service):
            barrier.wait()
            try:
                outcomes.append(("ok", service.review_master_candidate(
                    "J-REVIEW-CAS", 1, decision="accept_candidate"
                )))
            except Exception as exc:  # the losing CAS is the expected branch
                outcomes.append(("error", exc))

        threads = [
            threading.Thread(target=decide, args=(service,), daemon=True)
            for service in services
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        for connection in connections:
            connection.close()
        self.assertEqual([kind for kind, _ in outcomes].count("ok"), 1)
        self.assertEqual([kind for kind, _ in outcomes].count("error"), 1)
        verify = sqlite3.connect(database_path)
        verify.row_factory = sqlite3.Row
        row = verify.execute(
            "SELECT state,video_path,video_qa FROM shots "
            "WHERE job_id='J-REVIEW-CAS' AND idx=1"
        ).fetchone()
        verify.close()
        persisted = json.loads(row["video_qa"])
        self.assertEqual(row["state"], "OK")
        self.assertEqual(Path(row["video_path"]).read_bytes(), b"candidate")
        self.assertEqual(persisted["status"], "candidate_accepted_pending")
        self.assertEqual(Path(persisted["prior_backup_path"]).read_bytes(), b"prior")

    def test_accept_db_failure_restores_prior_and_candidate_after_claim(self):
        asset_root = self.root / "data" / "assets" / "J-REVIEW-DB-FAIL"
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        prior = asset_root / "shots" / "01.mp4"
        prior.write_bytes(b"prior")
        qa = json.dumps({
            "status": "manual_review_required", "candidate_path": str(candidate),
        })
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-REVIEW-DB-FAIL','T-1','db fail',"
            "'PRODUCTION_REVIEW_REQUIRED','{}')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
            "VALUES('J-REVIEW-DB-FAIL',1,'REVIEW_REQUIRED',?,?,'qa')",
            (str(prior), qa),
        )
        self.database.execute(
            "CREATE TRIGGER reject_review_accept BEFORE UPDATE OF state ON shots "
            "WHEN NEW.job_id='J-REVIEW-DB-FAIL' AND OLD.state='REVIEW_DECIDING' "
            "AND NEW.state='OK' BEGIN SELECT RAISE(ABORT,'db write failed'); END"
        )
        self.database.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "db write failed"):
            self.service.review_master_candidate(
                "J-REVIEW-DB-FAIL", 1, decision="accept_candidate"
            )

        row = self.database.execute(
            "SELECT state,video_path,video_qa FROM shots "
            "WHERE job_id='J-REVIEW-DB-FAIL'"
        ).fetchone()
        self.assertEqual((row["state"], row["video_path"], row["video_qa"]),
                         ("REVIEW_REQUIRED", str(prior), qa))
        self.assertEqual(prior.read_bytes(), b"prior")
        self.assertEqual(candidate.read_bytes(), b"candidate")

    def test_accept_db_failure_restores_prior_video_and_audio_as_one_unit(self):
        asset_root = self.root / "data" / "assets" / "J-REVIEW-AV-ROLLBACK"
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate-video")
        candidate.with_suffix(".wav").write_bytes(b"candidate-audio")
        prior = asset_root / "shots" / "01.mp4"
        prior.write_bytes(b"prior-video")
        prior.with_suffix(".wav").write_bytes(b"prior-audio")
        qa = json.dumps({
            "status": "manual_review_required", "candidate_path": str(candidate),
        })
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-REVIEW-AV-ROLLBACK','T-1','av rollback',"
            "'PRODUCTION_REVIEW_REQUIRED','{}')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
            "VALUES('J-REVIEW-AV-ROLLBACK',1,'REVIEW_REQUIRED',?,?,'qa')",
            (str(prior), qa),
        )
        self.database.execute(
            "CREATE TRIGGER reject_review_av_accept BEFORE UPDATE OF state ON shots "
            "WHEN NEW.job_id='J-REVIEW-AV-ROLLBACK' "
            "AND OLD.state='REVIEW_DECIDING' AND NEW.state='OK' "
            "BEGIN SELECT RAISE(ABORT,'av db write failed'); END"
        )
        self.database.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "av db write failed"):
            self.service.review_master_candidate(
                "J-REVIEW-AV-ROLLBACK", 1, decision="accept_candidate"
            )

        self.assertEqual(prior.read_bytes(), b"prior-video")
        self.assertEqual(prior.with_suffix(".wav").read_bytes(), b"prior-audio")
        self.assertEqual(candidate.read_bytes(), b"candidate-video")
        self.assertEqual(
            candidate.with_suffix(".wav").read_bytes(), b"candidate-audio"
        )

    def test_reject_job_wins_over_inflight_accept_on_another_shot(self):
        database_path = self.root / "review-reject-race.sqlite3"
        seed = sqlite3.connect(database_path)
        seed.row_factory = sqlite3.Row
        seed.executescript(db.DDL)
        asset_root = self.root / "data" / "assets" / "J-REVIEW-REJECT-RACE"
        review_dir = asset_root / "shots" / "review"
        review_dir.mkdir(parents=True)
        prior = asset_root / "shots" / "01.mp4"
        prior.write_bytes(b"prior-video")
        prior.with_suffix(".wav").write_bytes(b"prior-audio")
        candidates = []
        for idx in (1, 2):
            candidate = review_dir / f"{idx:02d}-candidate.mp4"
            candidate.write_bytes(f"candidate-video-{idx}".encode())
            candidate.with_suffix(".wav").write_bytes(
                f"candidate-audio-{idx}".encode()
            )
            candidates.append(candidate)
        seed.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-REVIEW-REJECT-RACE','T-1','race',"
            "'PRODUCTION_REVIEW_REQUIRED','{}')"
        )
        for idx, candidate in enumerate(candidates, 1):
            seed.execute(
                "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
                "VALUES('J-REVIEW-REJECT-RACE',?,'REVIEW_REQUIRED',?,?,'qa')",
                (
                    idx, str(prior) if idx == 1 else None,
                    json.dumps({
                        "status": "manual_review_required",
                        "candidate_path": str(candidate),
                    }),
                ),
            )
        seed.commit()
        seed.close()
        accept_db = sqlite3.connect(
            database_path, timeout=5, check_same_thread=False
        )
        reject_db = sqlite3.connect(database_path, timeout=5)
        accept_db.row_factory = sqlite3.Row
        reject_db.row_factory = sqlite3.Row
        accept_service = ManagementService(accept_db, self.root)
        reject_service = ManagementService(reject_db, self.root)
        accept_service.dispatch_production_queue = lambda: {"dispatched_jobs": []}
        reject_service.dispatch_production_queue = lambda: {"dispatched_jobs": []}
        staged = threading.Event()
        release = threading.Event()
        outcome = []
        real_copy2 = __import__("shutil").copy2

        def pause_after_video_stage(source, target, *args, **kwargs):
            result = real_copy2(source, target, *args, **kwargs)
            if not staged.is_set():
                staged.set()
                self.assertTrue(release.wait(timeout=10))
            return result

        def accept():
            try:
                outcome.append(accept_service.review_master_candidate(
                    "J-REVIEW-REJECT-RACE", 1, decision="accept_candidate"
                ))
            except Exception as exc:
                outcome.append(exc)

        try:
            with mock.patch(
                "factory.management_api.shutil.copy2",
                side_effect=pause_after_video_stage,
            ):
                thread = threading.Thread(target=accept, daemon=True)
                thread.start()
                self.assertTrue(
                    staged.wait(timeout=10),
                    f"accept did not reach file staging: {outcome!r}",
                )
                staged_row = reject_db.execute(
                    "SELECT state FROM shots WHERE job_id=? AND idx=1",
                    ("J-REVIEW-REJECT-RACE",),
                ).fetchone()
                self.assertEqual(staged_row["state"], "REVIEW_DECIDING")
                rejected = reject_service.review_master_candidate(
                    "J-REVIEW-REJECT-RACE", 2, decision="reject_job"
                )
                release.set()
                thread.join(timeout=10)

            self.assertFalse(thread.is_alive())
            self.assertEqual(rejected["state"], "REJECTED")
            row = reject_db.execute(
                "SELECT state FROM jobs WHERE job_id='J-REVIEW-REJECT-RACE'"
            ).fetchone()
            self.assertEqual(row["state"], "REJECTED")
            self.assertNotIn(
                "PRODUCTION_QUEUED",
                [value.get("state") for value in outcome if isinstance(value, dict)],
            )
            shots = reject_db.execute(
                "SELECT state,video_qa FROM shots "
                "WHERE job_id='J-REVIEW-REJECT-RACE' ORDER BY idx"
            ).fetchall()
            self.assertTrue(all(row["state"] == "FAILED" for row in shots))
            self.assertTrue(all(
                json.loads(row["video_qa"])["human_decision"] == "reject_job"
                for row in shots
            ))
            self.assertTrue(all(
                "review_operation" not in json.loads(row["video_qa"])
                for row in shots
            ))
            self.assertEqual(prior.read_bytes(), b"prior-video")
            self.assertEqual(prior.with_suffix(".wav").read_bytes(), b"prior-audio")
            self.assertEqual(candidates[0].read_bytes(), b"candidate-video-1")
            self.assertEqual(
                candidates[0].with_suffix(".wav").read_bytes(), b"candidate-audio-1"
            )
        finally:
            release.set()
            if "thread" in locals():
                thread.join(timeout=10)
            accept_db.close()
            reject_db.close()

    def test_decision_commit_linearizes_before_later_reject_without_half_commit(self):
        class PausingConnection(sqlite3.Connection):
            pause_after_commits = 0
            committed = None
            release = None

            def commit(self):
                result = super().commit()
                if self.pause_after_commits:
                    self.pause_after_commits -= 1
                    if self.pause_after_commits == 0:
                        self.committed.set()
                        if not self.release.wait(timeout=10):
                            raise AssertionError("timed out after decision commit")
                return result

        for decision in ("accept_candidate", "regenerate_master", "keep_prior"):
            with self.subTest(decision=decision):
                database_path = self.root / f"review-after-{decision}.sqlite3"
                seed = sqlite3.connect(database_path)
                seed.row_factory = sqlite3.Row
                seed.executescript(db.DDL)
                job_id = f"J-AFTER-{decision.upper()}"
                asset_root = self.root / "data" / "assets" / job_id
                review_dir = asset_root / "shots" / "review"
                review_dir.mkdir(parents=True)
                prior = asset_root / "shots" / "01.mp4"
                prior.write_bytes(b"prior")
                candidates = []
                for idx in (1, 2):
                    candidate = review_dir / f"{idx:02d}-candidate.mp4"
                    candidate.write_bytes(f"candidate-{idx}".encode())
                    candidates.append(candidate)
                seed.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
                    "VALUES(?,?,?,'PRODUCTION_REVIEW_REQUIRED','{}')",
                    (job_id, "T-1", "after commit"),
                )
                for idx, candidate in enumerate(candidates, 1):
                    seed.execute(
                        "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
                        "VALUES(?,?,'REVIEW_REQUIRED',?,?,'qa')",
                        (
                            job_id, idx, str(prior) if idx == 1 else None,
                            json.dumps({
                                "status": "manual_review_required",
                                "candidate_path": str(candidate),
                            }),
                        ),
                    )
                seed.commit()
                seed.close()
                action_db = sqlite3.connect(
                    database_path, timeout=5, check_same_thread=False,
                    factory=PausingConnection,
                )
                reject_db = sqlite3.connect(database_path, timeout=5)
                action_db.row_factory = sqlite3.Row
                reject_db.row_factory = sqlite3.Row
                action_service = ManagementService(action_db, self.root)
                reject_service = ManagementService(reject_db, self.root)
                action_service.dispatch_production_queue = (
                    lambda: {"dispatched_jobs": []}
                )
                committed = threading.Event()
                release = threading.Event()
                outcome = []
                action_db.committed = committed
                action_db.release = release
                # Pause only after the decision transaction has committed.  The
                # durable review journal deliberately adds earlier phase commits.
                action_db.pause_after_commits = {
                    "accept_candidate": 5,
                    "regenerate_master": 3,
                    "keep_prior": 2,
                }[decision]

                def act():
                    try:
                        outcome.append(action_service.review_master_candidate(
                            job_id, 1, decision=decision
                        ))
                    except Exception as exc:
                        outcome.append(exc)

                try:
                    thread = threading.Thread(target=act, daemon=True)
                    thread.start()
                    self.assertTrue(
                        committed.wait(timeout=10),
                        f"decision did not reach committed point: {outcome!r}",
                    )
                    committed_job = reject_db.execute(
                        "SELECT state,error FROM jobs WHERE job_id=?", (job_id,)
                    ).fetchone()
                    self.assertEqual(
                        committed_job[:],
                        ("PRODUCTION_REVIEW_REQUIRED", "仍有 1 条母片待人工复核"),
                    )
                    rejected = reject_service.review_master_candidate(
                        job_id, 2, decision="reject_job"
                    )
                    self.assertEqual(rejected["state"], "REJECTED")
                    self.assertEqual(reject_db.execute(
                        "SELECT state FROM jobs WHERE job_id=?", (job_id,)
                    ).fetchone()[0], "REJECTED")
                    expected_shot_state = (
                        "PENDING" if decision == "regenerate_master" else "OK"
                    )
                    self.assertEqual(reject_db.execute(
                        "SELECT state FROM shots WHERE job_id=? AND idx=1",
                        (job_id,),
                    ).fetchone()[0], expected_shot_state)
                    release.set()
                    thread.join(timeout=10)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(len(outcome), 1)
                    self.assertIsInstance(outcome[0], dict)
                    self.assertEqual(
                        outcome[0]["state"], "PRODUCTION_REVIEW_REQUIRED"
                    )
                    self.assertEqual(reject_db.execute(
                        "SELECT state FROM jobs WHERE job_id=?", (job_id,)
                    ).fetchone()[0], "REJECTED")
                finally:
                    release.set()
                    if "thread" in locals():
                        thread.join(timeout=10)
                    action_db.close()
                    reject_db.close()

    def test_accept_audio_backup_failure_preserves_prior_pair_and_candidate(self):
        job_id = "J-REVIEW-AUDIO-BACKUP-FAIL"
        asset_root = self.root / "data" / "assets" / job_id
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate-video")
        candidate.with_suffix(".wav").write_bytes(b"candidate-audio")
        prior = asset_root / "shots" / "01.mp4"
        prior.write_bytes(b"prior-video")
        prior.with_suffix(".wav").write_bytes(b"prior-audio")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,'PRODUCTION_REVIEW_REQUIRED','{}')",
            (job_id, "T-1", "audio backup failure"),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
            "VALUES(?,1,'REVIEW_REQUIRED',?,?,'qa')",
            (job_id, str(prior), json.dumps({
                "status": "manual_review_required",
                "candidate_path": str(candidate),
            })),
        )
        self.database.commit()
        real_replace = type(prior).replace
        real_copy2 = __import__("shutil").copy2

        def fail_audio_backup(path, target):
            if Path(path).suffix == ".wav" and Path(path).name == "01.wav":
                raise OSError("audio backup replace failed")
            return real_replace(path, target)

        def fail_audio_backup_copy(source, target, *args, **kwargs):
            if Path(source).suffix == ".wav" and Path(source).name == "01.wav":
                raise OSError("audio backup replace failed")
            return real_copy2(source, target, *args, **kwargs)

        with mock.patch.object(
            type(prior), "replace", new=fail_audio_backup
        ), mock.patch(
            "factory.management_api.shutil.copy2",
            side_effect=fail_audio_backup_copy,
        ), self.assertRaisesRegex(OSError, "audio backup replace failed"):
            self.service.review_master_candidate(
                job_id, 1, decision="accept_candidate"
            )

        self.assertEqual(prior.read_bytes(), b"prior-video")
        self.assertEqual(prior.with_suffix(".wav").read_bytes(), b"prior-audio")
        self.assertEqual(candidate.read_bytes(), b"candidate-video")
        self.assertEqual(
            candidate.with_suffix(".wav").read_bytes(), b"candidate-audio"
        )
        self.assertEqual(self.database.execute(
            "SELECT state FROM shots WHERE job_id=?", (job_id,)
        ).fetchone()[0], "REVIEW_REQUIRED")

    def test_accept_rejects_same_size_corrupt_audio_backup_before_mutation(self):
        job_id = "J-REVIEW-AUDIO-BACKUP-CORRUPT"
        asset_root = self.root / "data" / "assets" / job_id
        review_dir = asset_root / "shots" / "review"
        review_dir.mkdir(parents=True)
        prior = asset_root / "shots" / "01.mp4"
        prior.write_bytes(b"prior-video")
        prior.with_suffix(".wav").write_bytes(b"prior-audio")
        candidates = []
        for idx in (1, 2):
            candidate = review_dir / f"{idx:02d}-candidate.mp4"
            candidate.write_bytes(f"candidate-{idx}".encode())
            candidates.append(candidate)
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,'PRODUCTION_REVIEW_REQUIRED','{}')",
            (job_id, "T-1", "corrupt backup"),
        )
        for idx, candidate in enumerate(candidates, 1):
            self.database.execute(
                "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
                "VALUES(?,?,'REVIEW_REQUIRED',?,?,'qa')",
                (
                    job_id, idx, str(prior) if idx == 1 else None,
                    json.dumps({
                        "status": "manual_review_required",
                        "candidate_path": str(candidate),
                    }),
                ),
            )
        self.database.commit()
        real_copy2 = __import__("shutil").copy2

        def corrupt_audio_copy(source, target, *args, **kwargs):
            if Path(source).name == "01.wav":
                Path(target).write_bytes(b"x" * Path(source).stat().st_size)
                return str(target)
            return real_copy2(source, target, *args, **kwargs)

        with mock.patch(
            "factory.management_api.shutil.copy2",
            side_effect=corrupt_audio_copy,
        ), self.assertRaisesRegex(OSError, "旧母片音频备份校验失败"):
            self.service.review_master_candidate(
                job_id, 1, decision="accept_candidate"
            )

        self.assertEqual(prior.read_bytes(), b"prior-video")
        self.assertEqual(prior.with_suffix(".wav").read_bytes(), b"prior-audio")
        self.assertEqual(candidates[0].read_bytes(), b"candidate-1")
        self.assertEqual(self.database.execute(
            "SELECT state FROM shots WHERE job_id=? AND idx=1", (job_id,)
        ).fetchone()[0], "REVIEW_REQUIRED")

    def test_accept_repeated_prepare_failures_do_not_accumulate_staging_or_backups(self):
        job_id = "J-REVIEW-PREPARE-RETRY-CLEAN"
        asset_root = self.root / "data" / "assets" / job_id
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate-video")
        candidate.with_suffix(".wav").write_bytes(b"candidate-audio")
        prior = asset_root / "shots" / "01.mp4"
        prior.write_bytes(b"prior-video")
        prior.with_suffix(".wav").write_bytes(b"prior-audio")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,'PRODUCTION_REVIEW_REQUIRED','{}')",
            (job_id, "T-1", "prepare retry clean"),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
            "VALUES(?,1,'REVIEW_REQUIRED',?,?,'qa')",
            (job_id, str(prior), json.dumps({
                "status": "manual_review_required",
                "candidate_path": str(candidate),
            })),
        )
        self.database.commit()
        real_copy2 = __import__("shutil").copy2

        def fail_candidate_audio(source, target, *args, **kwargs):
            if Path(source).name == "01-candidate.wav":
                raise OSError("candidate audio prepare failed")
            return real_copy2(source, target, *args, **kwargs)

        for _ in range(2):
            with mock.patch(
                "factory.management_api.shutil.copy2",
                side_effect=fail_candidate_audio,
            ), self.assertRaisesRegex(OSError, "candidate audio prepare failed"):
                self.service.review_master_candidate(
                    job_id, 1, decision="accept_candidate"
                )

        staging_dir = asset_root / "review" / "staging"
        self.assertEqual(list(staging_dir.glob("*")), [])
        self.assertEqual(prior.read_bytes(), b"prior-video")
        self.assertEqual(prior.with_suffix(".wav").read_bytes(), b"prior-audio")
        self.assertEqual(candidate.read_bytes(), b"candidate-video")
        self.assertEqual(
            candidate.with_suffix(".wav").read_bytes(), b"candidate-audio"
        )

    def test_accept_repeated_db_failures_delete_restored_unreferenced_backups(self):
        job_id = "J-REVIEW-DB-RETRY-CLEAN"
        asset_root = self.root / "data" / "assets" / job_id
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate-video")
        candidate.with_suffix(".wav").write_bytes(b"candidate-audio")
        prior = asset_root / "shots" / "01.mp4"
        prior.write_bytes(b"prior-video")
        prior.with_suffix(".wav").write_bytes(b"prior-audio")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,'PRODUCTION_REVIEW_REQUIRED','{}')",
            (job_id, "T-1", "db retry clean"),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
            "VALUES(?,1,'REVIEW_REQUIRED',?,?,'qa')",
            (job_id, str(prior), json.dumps({
                "status": "manual_review_required",
                "candidate_path": str(candidate),
            })),
        )
        self.database.execute(
            "CREATE TRIGGER fail_repeated_accept BEFORE UPDATE OF state ON shots "
            "WHEN NEW.job_id='J-REVIEW-DB-RETRY-CLEAN' "
            "AND OLD.state='REVIEW_DECIDING' AND NEW.state='OK' "
            "BEGIN SELECT RAISE(ABORT,'repeated accept db failed'); END"
        )
        self.database.commit()

        for _ in range(2):
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "repeated accept db failed"
            ):
                self.service.review_master_candidate(
                    job_id, 1, decision="accept_candidate"
                )

        staging_dir = asset_root / "review" / "staging"
        self.assertEqual(list(staging_dir.glob("*")), [])
        self.assertEqual(prior.read_bytes(), b"prior-video")
        self.assertEqual(prior.with_suffix(".wav").read_bytes(), b"prior-audio")
        self.assertEqual(candidate.read_bytes(), b"candidate-video")
        self.assertEqual(
            candidate.with_suffix(".wav").read_bytes(), b"candidate-audio"
        )

    def test_restart_recovers_accept_review_operation_at_every_crash_phase(self):
        for phase in ("claimed", "video_published", "audio_published"):
            with self.subTest(phase=phase):
                database_path = self.root / f"accept-recovery-{phase}.sqlite3"
                database = sqlite3.connect(database_path)
                database.row_factory = sqlite3.Row
                database.executescript(db.DDL)
                job_id = f"J-ACCEPT-RECOVERY-{phase.upper()}"
                asset_root = self.root / "data" / "assets" / job_id
                review_dir = asset_root / "shots" / "review"
                staging_dir = asset_root / "review" / "staging"
                review_dir.mkdir(parents=True)
                staging_dir.mkdir(parents=True)
                active = asset_root / "shots" / "01.mp4"
                active_audio = active.with_suffix(".wav")
                candidate = review_dir / "01-candidate.mp4"
                candidate_audio = candidate.with_suffix(".wav")
                backup = staging_dir / "01-operation-prior.mp4"
                backup_audio = backup.with_suffix(".wav")
                candidate.write_bytes(b"candidate-video")
                candidate_audio.write_bytes(b"candidate-audio")
                active.write_bytes(
                    b"candidate-video" if phase != "claimed" else b"prior-video"
                )
                active_audio.write_bytes(
                    b"candidate-audio" if phase == "audio_published" else b"prior-audio"
                )
                if phase != "claimed":
                    backup.write_bytes(b"prior-video")
                    backup_audio.write_bytes(b"prior-audio")
                operation = {
                    "decision": "accept_candidate", "phase": phase,
                    "candidate_path": str(candidate),
                    "active_path": str(active),
                    "prior_backup_path": str(backup),
                    "prior_audio_backup_path": str(backup_audio),
                    "candidate_sha256": hashlib.sha256(b"candidate-video").hexdigest(),
                }
                qa = {
                    "status": "manual_review_required",
                    "candidate_path": str(candidate),
                    "review_operation": operation,
                }
                database.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
                    "VALUES(?,?,?,'PRODUCTION_REVIEW_REQUIRED','{}')",
                    (job_id, "T-1", "accept recovery"),
                )
                database.execute(
                    "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
                    "VALUES(?,1,'REVIEW_DECIDING',?,?,NULL)",
                    (job_id, str(active), json.dumps(qa)),
                )
                database.commit()
                database.close()

                recovered_db = sqlite3.connect(database_path)
                recovered_db.row_factory = sqlite3.Row
                ManagementService(recovered_db, self.root)
                shot = recovered_db.execute(
                    "SELECT state,video_qa FROM shots WHERE job_id=?", (job_id,)
                ).fetchone()
                recovered_qa = json.loads(shot["video_qa"])
                self.assertEqual(shot["state"], "REVIEW_REQUIRED")
                self.assertNotIn("review_operation", recovered_qa)
                self.assertEqual(active.read_bytes(), b"prior-video")
                self.assertEqual(active_audio.read_bytes(), b"prior-audio")
                self.assertEqual(candidate.read_bytes(), b"candidate-video")
                self.assertFalse(backup.exists())
                self.assertFalse(backup_audio.exists())
                recovered_db.close()

    def test_review_recovery_commit_failure_keeps_backup_for_next_restart(self):
        class FailingRecoveryCommit(sqlite3.Connection):
            fail_next_commit = False

            def commit(self):
                if self.fail_next_commit:
                    self.fail_next_commit = False
                    raise sqlite3.OperationalError("recovery commit failed")
                return super().commit()

        database_path = self.root / "accept-recovery-restart.sqlite3"
        database = sqlite3.connect(database_path, factory=FailingRecoveryCommit)
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        service = ManagementService(database, self.root)
        job_id = "J-ACCEPT-RECOVERY-RESTART"
        asset_root = self.root / "data" / "assets" / job_id
        review_dir = asset_root / "shots" / "review"
        staging_dir = asset_root / "review" / "staging"
        review_dir.mkdir(parents=True)
        staging_dir.mkdir(parents=True)
        active = asset_root / "shots" / "01.mp4"
        candidate = review_dir / "01-candidate.mp4"
        backup = staging_dir / "01-prior.mp4"
        active.write_bytes(b"candidate")
        candidate.write_bytes(b"candidate")
        backup.write_bytes(b"prior")
        operation = {
            "decision": "accept_candidate", "phase": "video_published",
            "candidate_path": str(candidate), "active_path": str(active),
            "prior_backup_path": str(backup),
        }
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,'PRODUCTION_REVIEW_REQUIRED','{}')",
            (job_id, "T-1", "recovery restart"),
        )
        database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa) "
            "VALUES(?,1,'REVIEW_DECIDING',?,?)",
            (job_id, str(active), json.dumps({
                "status": "manual_review_required",
                "candidate_path": str(candidate),
                "review_operation": operation,
            })),
        )
        database.commit()
        database.fail_next_commit = True
        with self.assertRaisesRegex(sqlite3.OperationalError, "recovery commit"):
            service._recover_review_operations()
        database.close()

        restarted = sqlite3.connect(database_path)
        restarted.row_factory = sqlite3.Row
        ManagementService(restarted, self.root)
        recovered = restarted.execute(
            "SELECT state FROM shots WHERE job_id=?", (job_id,)
        ).fetchone()
        self.assertEqual(recovered["state"], "REVIEW_REQUIRED")
        self.assertEqual(active.read_bytes(), b"prior")
        self.assertEqual(candidate.read_bytes(), b"candidate")
        restarted.close()

    def test_restart_restores_inflight_accept_after_reject_won(self):
        database_path = self.root / "accept-rejected-recovery.sqlite3"
        database = sqlite3.connect(database_path)
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        job_id = "J-ACCEPT-REJECTED-RECOVERY"
        asset_root = self.root / "data" / "assets" / job_id
        review_dir = asset_root / "shots" / "review"
        staging_dir = asset_root / "review" / "staging"
        review_dir.mkdir(parents=True)
        staging_dir.mkdir(parents=True)
        active = asset_root / "shots" / "01.mp4"
        active_audio = active.with_suffix(".wav")
        candidate = review_dir / "01-candidate.mp4"
        candidate_audio = candidate.with_suffix(".wav")
        backup = staging_dir / "01-prior.mp4"
        backup_audio = backup.with_suffix(".wav")
        active.write_bytes(b"candidate-video")
        active_audio.write_bytes(b"prior-audio")
        candidate.write_bytes(b"candidate-video")
        candidate_audio.write_bytes(b"candidate-audio")
        backup.write_bytes(b"prior-video")
        backup_audio.write_bytes(b"prior-audio")
        operation = {
            "decision": "accept_candidate", "phase": "video_published",
            "candidate_path": str(candidate), "active_path": str(active),
            "prior_backup_path": str(backup),
            "prior_audio_backup_path": str(backup_audio),
        }
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,'REJECTED','{}')",
            (job_id, "T-1", "rejected during accept"),
        )
        database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
            "VALUES(?,1,'FAILED',?,?,?)",
            (job_id, str(active), json.dumps({
                "status": "job_rejected", "human_decision": "reject_job",
                "candidate_path": str(candidate),
                "review_operation": operation,
            }), "人工终止任务；候选产物已保留"),
        )
        database.commit()
        database.close()

        restarted = sqlite3.connect(database_path)
        restarted.row_factory = sqlite3.Row
        ManagementService(restarted, self.root)
        shot = restarted.execute(
            "SELECT state,video_qa FROM shots WHERE job_id=?", (job_id,)
        ).fetchone()
        qa = json.loads(shot["video_qa"])
        self.assertEqual(shot["state"], "FAILED")
        self.assertEqual(qa["human_decision"], "reject_job")
        self.assertNotIn("review_operation", qa)
        self.assertEqual(active.read_bytes(), b"prior-video")
        self.assertEqual(active_audio.read_bytes(), b"prior-audio")
        self.assertEqual(candidate.read_bytes(), b"candidate-video")
        self.assertEqual(candidate_audio.read_bytes(), b"candidate-audio")
        restarted.close()

    def test_restart_preserves_committed_accept_and_finishes_regenerate_journal(self):
        database_path = self.root / "committed-review-recovery.sqlite3"
        database = sqlite3.connect(database_path)
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        for idx, decision in ((1, "accept_candidate"), (2, "regenerate_master")):
            job_id = f"J-COMMITTED-{idx}"
            asset_root = self.root / "data" / "assets" / job_id
            review_dir = asset_root / "shots" / "review"
            staging_dir = asset_root / "review" / "staging"
            review_dir.mkdir(parents=True)
            staging_dir.mkdir(parents=True)
            candidate = review_dir / f"{idx:02d}-candidate.mp4"
            candidate.write_bytes(b"candidate")
            archive = staging_dir / f"{idx:02d}-regenerate.mp4"
            if decision == "regenerate_master":
                candidate.replace(archive)
            active = asset_root / "shots" / f"{idx:02d}.mp4"
            if decision == "accept_candidate":
                active.write_bytes(b"candidate")
            operation = {
                "decision": decision, "phase": "committed",
                "candidate_path": str(candidate),
                "archive_path": str(archive),
                "active_path": str(active),
            }
            qa = {
                "status": (
                    "candidate_accepted_pending"
                    if decision == "accept_candidate" else "regenerate_committed"
                ),
                "candidate_path": str(candidate),
                "review_operation": operation,
            }
            database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
                "VALUES(?,?,?,'PRODUCTION_REVIEW_REQUIRED','{}')",
                (job_id, "T-1", "committed recovery"),
            )
            database.execute(
                "INSERT INTO shots(job_id,idx,state,video_path,video_qa) "
                "VALUES(?,?,?,?,?)",
                (
                    job_id, idx,
                    "OK" if decision == "accept_candidate" else "PENDING",
                    str(active) if decision == "accept_candidate" else None,
                    json.dumps(qa),
                ),
            )
        database.commit()
        database.close()

        recovered_db = sqlite3.connect(database_path)
        recovered_db.row_factory = sqlite3.Row
        ManagementService(recovered_db, self.root)
        accept = recovered_db.execute(
            "SELECT state,video_qa FROM shots WHERE job_id='J-COMMITTED-1'"
        ).fetchone()
        regenerate = recovered_db.execute(
            "SELECT state,video_qa FROM shots WHERE job_id='J-COMMITTED-2'"
        ).fetchone()
        self.assertEqual(accept["state"], "OK")
        self.assertEqual(json.loads(accept["video_qa"])["status"],
                         "candidate_accepted_pending")
        self.assertEqual(regenerate["state"], "PENDING")
        self.assertIsNone(regenerate["video_qa"])
        self.assertFalse(
            (self.root / "data" / "assets" / "J-COMMITTED-2" / "review"
             / "staging" / "02-regenerate.mp4").exists()
        )
        recovered_db.close()

    def test_committed_regenerate_cleanup_failure_returns_success_and_dispatches(self):
        job_id = "J-REGENERATE-CLEANUP-DENIED"
        asset_root = self.root / "data" / "assets" / job_id
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,'PRODUCTION_REVIEW_REQUIRED','{}')",
            (job_id, "T-1", "cleanup denied"),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_qa,error) "
            "VALUES(?,1,'REVIEW_REQUIRED',?,'qa')",
            (job_id, json.dumps({
                "status": "manual_review_required",
                "candidate_path": str(candidate),
            })),
        )
        self.database.commit()
        dispatched = []
        self.service.dispatch_production_queue = lambda: (
            dispatched.append(job_id) or {"dispatched_jobs": [job_id]}
        )
        real_unlink = Path.unlink

        def deny_archive_cleanup(path, *args, **kwargs):
            if "regenerate" in Path(path).name:
                raise PermissionError("archive cleanup denied")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=deny_archive_cleanup):
            result = self.service.review_master_candidate(
                job_id, 1, decision="regenerate_master"
            )

        self.assertEqual(result["state"], "PRODUCTION_QUEUED")
        self.assertEqual(dispatched, [job_id])
        persisted = self.database.execute(
            "SELECT state,video_qa FROM shots WHERE job_id=?", (job_id,)
        ).fetchone()
        self.assertEqual(persisted["state"], "PENDING")
        self.assertEqual(
            json.loads(persisted["video_qa"])["review_operation"]["phase"],
            "committed",
        )
        repeated = self.service.review_master_candidate(
            job_id, 1, decision="regenerate_master"
        )
        self.assertEqual(repeated["state"], "PRODUCTION_QUEUED")

    def test_committed_keep_prior_cleanup_failure_returns_committed_result(self):
        job_id = "J-KEEP-CLEANUP-DENIED"
        asset_root = self.root / "data" / "assets" / job_id
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        prior = asset_root / "shots" / "01.mp4"
        prior.write_bytes(b"prior")
        final = asset_root / "final.mp4"
        final.write_bytes(b"final")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,final_path) "
            "VALUES(?,?,?,'PRODUCTION_REVIEW_REQUIRED','{}',?)",
            (job_id, "T-1", "keep cleanup", str(final)),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
            "VALUES(?,1,'REVIEW_REQUIRED',?,?,'qa')",
            (job_id, str(prior), json.dumps({
                "status": "manual_review_required",
                "candidate_path": str(candidate),
            })),
        )
        self.database.commit()
        real_unlink = Path.unlink

        def deny_candidate_cleanup(path, *args, **kwargs):
            if Path(path).name == "01-candidate.mp4":
                raise PermissionError("candidate cleanup denied")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=deny_candidate_cleanup):
            result = self.service.review_master_candidate(
                job_id, 1, decision="keep_prior"
            )

        self.assertEqual(result["state"], "COMPOSED")
        repeated = self.service.review_master_candidate(
            job_id, 1, decision="keep_prior"
        )
        self.assertEqual(repeated["state"], "COMPOSED")

    def test_reject_job_wins_over_inflight_regenerate_and_keep_prior(self):
        for decision in ("regenerate_master", "keep_prior"):
            with self.subTest(decision=decision):
                database_path = self.root / f"review-{decision}-race.sqlite3"
                seed = sqlite3.connect(database_path)
                seed.row_factory = sqlite3.Row
                seed.executescript(db.DDL)
                job_id = f"J-{decision.upper()}-REJECT-RACE"
                asset_root = self.root / "data" / "assets" / job_id
                review_dir = asset_root / "shots" / "review"
                review_dir.mkdir(parents=True)
                prior = asset_root / "shots" / "01.mp4"
                prior.write_bytes(b"prior-video")
                candidates = []
                for idx in (1, 2):
                    candidate = review_dir / f"{idx:02d}-candidate.mp4"
                    candidate.write_bytes(f"candidate-{idx}".encode())
                    candidates.append(candidate)
                seed.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
                    "VALUES(?,?,?,'PRODUCTION_REVIEW_REQUIRED','{}')",
                    (job_id, "T-1", "decision race"),
                )
                for idx, candidate in enumerate(candidates, 1):
                    seed.execute(
                        "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
                        "VALUES(?,?,'REVIEW_REQUIRED',?,?,'qa')",
                        (
                            job_id, idx, str(prior) if idx == 1 else None,
                            json.dumps({
                                "status": "manual_review_required",
                                "candidate_path": str(candidate),
                            }),
                        ),
                    )
                seed.commit()
                seed.close()
                action_db = sqlite3.connect(
                    database_path, timeout=5, check_same_thread=False
                )
                reject_db = sqlite3.connect(database_path, timeout=5)
                action_db.row_factory = sqlite3.Row
                reject_db.row_factory = sqlite3.Row
                action_service = ManagementService(action_db, self.root)
                reject_service = ManagementService(reject_db, self.root)
                action_service.dispatch_production_queue = (
                    lambda: {"dispatched_jobs": []}
                )
                staged = threading.Event()
                release = threading.Event()
                outcome = []
                real_replace = Path.replace
                real_dumps = json.dumps

                def pause_replace(path, target):
                    result = real_replace(path, target)
                    if not staged.is_set():
                        staged.set()
                        self.assertTrue(release.wait(timeout=10))
                    return result

                def pause_keep_prior(payload, *args, **kwargs):
                    if (
                        isinstance(payload, dict)
                        and payload.get("human_decision") == "keep_prior"
                        and not staged.is_set()
                    ):
                        staged.set()
                        self.assertTrue(release.wait(timeout=10))
                    return real_dumps(payload, *args, **kwargs)

                def act():
                    try:
                        outcome.append(action_service.review_master_candidate(
                            job_id, 1, decision=decision
                        ))
                    except Exception as exc:
                        outcome.append(exc)

                patch_target = (
                    mock.patch.object(
                        type(candidates[0]), "replace", new=pause_replace
                    )
                    if decision == "regenerate_master"
                    else mock.patch(
                        "factory.management_api.json.dumps",
                        side_effect=pause_keep_prior,
                    )
                )
                try:
                    with patch_target:
                        thread = threading.Thread(target=act, daemon=True)
                        thread.start()
                        self.assertTrue(
                            staged.wait(timeout=10),
                            f"{decision} did not reach decision staging: {outcome!r}",
                        )
                        rejected = reject_service.review_master_candidate(
                            job_id, 2, decision="reject_job"
                        )
                        release.set()
                        thread.join(timeout=10)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(rejected["state"], "REJECTED")
                    self.assertEqual(reject_db.execute(
                        "SELECT state FROM jobs WHERE job_id=?", (job_id,)
                    ).fetchone()[0], "REJECTED")
                    self.assertTrue(all(
                        row[0] == "FAILED" for row in reject_db.execute(
                            "SELECT state FROM shots WHERE job_id=?", (job_id,)
                        ).fetchall()
                    ))
                    self.assertEqual(candidates[0].read_bytes(), b"candidate-1")
                finally:
                    release.set()
                    if "thread" in locals():
                        thread.join(timeout=10)
                    action_db.close()
                    reject_db.close()

    def test_rejected_job_reconciles_late_provider_cost_without_resuming_production(self):
        job_id = "J-REJECT-PENDING-COST"
        asset_root = self.root / "data" / "assets" / job_id
        candidate = asset_root / "shots" / "review" / "01-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"review-candidate")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES(?,?,?,'PRODUCTION_REVIEW_REQUIRED','{}',0)",
            (job_id, "T-1", "reject pending cost"),
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,state,video_qa,error) VALUES(?,?,?,?,?)",
            [
                (job_id, 1, "REVIEW_REQUIRED", json.dumps({
                    "status": "manual_review_required",
                    "candidate_path": str(candidate),
                }), "qa"),
                (job_id, 2, "FAILED", None, "remote unknown"),
            ],
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES('seedance-late-bill',?,2,'seedance',"
            "'generate',?,0)",
            (job_id, json.dumps({"status": "action_required"})),
        )
        self.database.commit()

        rejected = self.service.review_master_candidate(
            job_id, 1, decision="reject_job"
        )

        self.assertEqual(rejected["state"], "REJECTED_PENDING_RECONCILIATION")
        todo = {
            group["key"]: group for group in self.service.todo_center()["groups"]
        }
        pending_item = next(
            item for item in todo["provider_action"]["items"]
            if item["id"] == job_id
        )
        self.assertEqual(pending_item["stage"],
                         "REJECTED_PENDING_RECONCILIATION")
        with self.assertRaisesRegex(ValueError, "成本.*核对"):
            self.service.delete_video(job_id)
        inspected = {
            "status": "completed", "video": b"late-paid-video",
            "video_url": "https://cdn/late.mp4", "usage": {},
            "cost_cents": 700,
        }
        with mock.patch(
            "factory.management_api.seedance.inspect_task",
            new=mock.AsyncMock(return_value=inspected),
        ), mock.patch(
            "factory.management_api.seedance.text_to_video",
            new=mock.AsyncMock(),
        ) as submit:
            reconciled = self.service.reconcile_provider_task(
                job_id, "seedance-late-bill"
            )

        submit.assert_not_awaited()
        self.assertEqual(reconciled["state"], "REJECTED")
        job = self.database.execute(
            "SELECT state,cost_cents FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        self.assertEqual(job[:], ("REJECTED", 700))
        self.assertEqual(self.database.execute(
            "SELECT state FROM shots WHERE job_id=? AND idx=2", (job_id,)
        ).fetchone()[0], "FAILED")
        call = self.database.execute(
            "SELECT cost_cents,usage_json FROM provider_calls "
            "WHERE task_id='seedance-late-bill'"
        ).fetchone()
        self.assertEqual(call["cost_cents"], 700)
        self.assertEqual(json.loads(call["usage_json"])["status"], "completed")

    def test_failed_video_is_resumable_when_every_master_failed_before_charge(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,error) "
            "VALUES('J-PRIVACY','T-1','privacy','FAILED','{}',0,'privacy')"
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,state,cost_cents,error) "
            "VALUES(?,?,?,?,?)",
            [
                ("J-PRIVACY", 1, "FAILED", 0, "privacy"),
                ("J-PRIVACY", 2, "FAILED", 0, "privacy"),
            ],
        )
        self.database.commit()

        job = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-PRIVACY"
        )

        self.assertTrue(job["resumable"])

    def test_request_invalid_identity_anchor_can_be_reset_after_client_fix(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,"
            "error,failure_code) VALUES(?,?,?,?,?,?,?,?)",
            (
                "J-ANCHOR-PARAM-RETRY", "T-ANCHOR", "anchor parameter retry",
                "FAILED", "{}", 0, "request invalid", "request_invalid",
            ),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents) VALUES(?,?,?,?)",
            ("J-ANCHOR-PARAM-RETRY", 1, "PENDING", 0),
        )
        self.database.execute(
            "INSERT INTO character_identity_anchors("
            "job_id,identity_key,character_id,display_name,source_type,state,"
            "anchor_mode,duration_sec,cost_cents,error) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "J-ANCHOR-PARAM-RETRY", "script:engineer", "engineer",
                "Engineer", "seedance_identity_anchor", "FAILED",
                "dynamic_video", 4, 0, "request invalid",
            ),
        )
        self.database.commit()

        job = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-ANCHOR-PARAM-RETRY"
        )
        self.assertTrue(job["resumable"])
        self.assertEqual(job["next_action"]["id"], "retry_identity_anchor")

        result = self.service.resume_failed_video("J-ANCHOR-PARAM-RETRY")

        self.assertEqual(result["state"], "STORYBOARDED")
        anchor = self.database.execute(
            "SELECT state,error,cost_cents FROM character_identity_anchors "
            "WHERE job_id='J-ANCHOR-PARAM-RETRY'"
        ).fetchone()
        self.assertEqual(anchor[:], ("PENDING", None, 0))
        persisted = self.database.execute(
            "SELECT state,error,failure_code,cost_cents FROM jobs "
            "WHERE job_id='J-ANCHOR-PARAM-RETRY'"
        ).fetchone()
        self.assertEqual(persisted[:], ("STORYBOARDED", None, None, 0))

    def test_request_invalid_anchor_with_provider_evidence_stays_fail_closed(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,"
            "error,failure_code) VALUES(?,?,?,?,?,?,?,?)",
            (
                "J-ANCHOR-PARAM-PAID", "T-ANCHOR", "paid anchor parameter",
                "FAILED", "{}", 29, "request invalid", "request_invalid",
            ),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents) VALUES(?,?,?,?)",
            ("J-ANCHOR-PARAM-PAID", 1, "PENDING", 0),
        )
        self.database.execute(
            "INSERT INTO character_identity_anchors("
            "job_id,identity_key,character_id,display_name,source_type,state,"
            "anchor_mode,duration_sec,cost_cents,error) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "J-ANCHOR-PARAM-PAID", "script:engineer", "engineer",
                "Engineer", "seedance_identity_anchor", "FAILED",
                "dynamic_video", 4, 0, "request invalid",
            ),
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "cost_cents,usage_json) VALUES(?,?,?,?,?,?,?)",
            (
                "remote-anchor-task", "J-ANCHOR-PARAM-PAID", -1001,
                "seedance_identity", "generate", 29,
                json.dumps({"status": "completed"}),
            ),
        )
        self.database.commit()

        job = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-ANCHOR-PARAM-PAID"
        )
        self.assertFalse(job["resumable"])
        with self.assertRaises(ValueError):
            self.service.resume_failed_video("J-ANCHOR-PARAM-PAID")

    def test_partially_completed_storyboard_is_displayed_as_paused_production(self):
        storyboard = {
            "masters": [
                {"idx": 1, "type": "dialogue", "duration": 5},
                {"idx": 2, "type": "dialogue", "duration": 5},
            ],
            "production_settings": {"budget_cents": 2500},
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-PARTIAL','T-1','partial','STORYBOARDED',?,500)",
            (json.dumps(storyboard),),
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,duration_sec,state,cost_cents,video_path) "
            "VALUES(?,?,?,?,?,?)",
            [
                ("J-PARTIAL", 1, 5, "OK", 500, "01.mp4"),
                ("J-PARTIAL", 2, 5, "PENDING", 0, None),
            ],
        )
        self.database.commit()

        job = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-PARTIAL"
        )

        self.assertTrue(job["partial_production"])
        self.assertEqual(job["display_state"], "PRODUCTION_PAUSED")
        self.assertEqual(job["display_state_label"], "待继续生成")

    def test_reconfiguring_resumed_video_does_not_delete_completed_masters(self):
        storyboard = {
            "characters": [{"id": "sales", "name": "销售"}],
            "masters": [
                {"idx": 1, "type": "dialogue", "speaker": "sales", "duration": 5},
                {"idx": 2, "type": "dialogue", "speaker": "sales", "duration": 5},
            ],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents) "
            "VALUES('J-RECONFIG','T-1','reconfig','STORYBOARDED',?,500)",
            (json.dumps(storyboard, ensure_ascii=False),),
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,duration_sec,state,cost_cents,video_path) "
            "VALUES(?,?,?,?,?,?)",
            [
                ("J-RECONFIG", 1, 5, "OK", 500, "01.mp4"),
                ("J-RECONFIG", 2, 5, "PENDING", 0, None),
            ],
        )
        self.database.commit()

        self.service.configure_video("J-RECONFIG", {
            "character_mappings": {"sales": ""},
            "orientation": "9:16",
            "production_profile": "standard",
            "subtitle_style": "clean",
            "asr_enabled": True,
            "budget_cents": 2500,
        })

        completed = self.database.execute(
            "SELECT state,cost_cents,video_path FROM shots "
            "WHERE job_id='J-RECONFIG' AND idx=1"
        ).fetchone()
        self.assertEqual(dict(completed), {
            "state": "OK", "cost_cents": 500, "video_path": "01.mp4",
        })

    def test_start_video_job_accepts_ai_generated_dialogue_speakers(self):
        storyboard = {
            "production_settings": {"budget_cents": 4000},
            "masters": [
                {"idx": 1, "type": "dialogue", "speaker": "sales", "duration": 5},
                {"idx": 2, "type": "dialogue", "speaker": "boss", "duration": 5},
            ],
            "characters": [
                {
                    "id": "sales", "name": "销售", "look": "干练的设备租赁销售",
                    "generation_mode": "ai_generated",
                },
                {
                    "id": "boss", "name": "老板", "look": "谨慎的设备租赁老板",
                    "generation_mode": "ai_generated",
                },
            ],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-UNMAPPED','T-1','unmapped','STORYBOARDED',?)",
            (json.dumps(storyboard, ensure_ascii=False),),
        )
        self.database.commit()

        with mock.patch.object(
            self.service,
            "dispatch_production_queue",
            return_value={"dispatched_jobs": [], "queued_jobs": ["J-UNMAPPED"]},
        ) as dispatch:
            result = self.service.start_video_job("J-UNMAPPED", confirmed=True)

        self.assertEqual(result, {"job_id": "J-UNMAPPED", "status": "queued"})
        dispatch.assert_called_once_with()

    def test_start_video_job_rejects_dialogue_speakers_without_a_generation_plan(self):
        storyboard = {
            "masters": [
                {"idx": 1, "type": "dialogue", "speaker": "sales", "duration": 5},
                {"idx": 2, "type": "dialogue", "speaker": "boss", "duration": 5},
            ],
            "characters": [
                {"id": "sales", "name": "销售", "look": "placeholder"},
                {"id": "boss", "name": "老板", "look": "placeholder"},
            ],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-NO-PLAN','T-1','no plan','STORYBOARDED',?)",
            (json.dumps(storyboard, ensure_ascii=False),),
        )
        self.database.commit()

        with mock.patch.object(self.service, "dispatch_production_queue") as dispatch:
            with self.assertRaisesRegex(ValueError, "sales.*boss|boss.*sales"):
                self.service.start_video_job("J-NO-PLAN", confirmed=True)

        dispatch.assert_not_called()

    def test_start_video_job_blocks_legacy_master_over_thirty_seconds_before_paid_worker(self):
        storyboard = {
            "production_profile": "standard",
            "production_mode": "native_master_local_director",
            "production_settings": {"budget_cents": 10000},
            "characters": [{
                "id": "presenter", "name": "Presenter", "look": "professional",
                "gender": "female", "asset_id": "asset-presenter",
                "asset_uri": "asset://asset-presenter",
            }],
            "hook": "Hook",
            "masters": [{
                "idx": 1,
                "type": "dialogue",
                "speaker": "presenter",
                "render_mode": "presenter",
                "duration": 36,
                "scene": "Office",
                "action": "Talk to camera",
                "voiceover": "Long dialogue",
            }],
            "shots": [{
                "idx": index,
                "duration": 6,
                "video_master_idx": 1,
                "audio_master_idx": 1,
                "video_start": (index - 1) * 6,
                "audio_start": (index - 1) * 6,
                "subtitle": "Long dialogue",
            } for index in range(1, 7)],
            "bgm": {"mood": "calm", "prompt": "light music"},
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-INVALID-STORYBOARD','T-1','invalid','STORYBOARDED',?)",
            (json.dumps(storyboard),),
        )
        self.database.commit()

        with mock.patch("factory.management_api.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ValueError, "母片 #1.*30 seconds"):
                self.service.start_video_job(
                    "J-INVALID-STORYBOARD", confirmed=True
                )

        popen.assert_not_called()
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-INVALID-STORYBOARD'"
            ).fetchone()["state"],
            "STORYBOARDED",
        )

    def test_start_video_job_atomically_prevents_duplicate_paid_workers(self):
        storyboard = {
            "masters": [{"idx": 1, "type": "dialogue", "duration": 5}],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-CLAIM','T-1','claim once','STORYBOARDED',?)",
            (json.dumps(storyboard),),
        )
        self.database.commit()

        with mock.patch(
            "factory.production_planner.seedance.estimate_cost_cents",
            return_value=100,
        ), mock.patch("factory.management_api.subprocess.Popen") as popen:
            first = self.service.start_video_job("J-CLAIM", confirmed=True)
            with self.assertRaisesRegex(ValueError, "已经启动"):
                self.service.start_video_job("J-CLAIM", confirmed=True)

        self.assertEqual(first["status"], "started")
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-CLAIM'"
            ).fetchone()["state"],
            "PRODUCTION_RUNNING",
        )

    def test_start_video_job_can_queue_a_new_attempt_with_the_same_spec(self):
        storyboard = {
            "production_settings": {"budget_cents": 5000},
            "masters": [{"idx": 1, "type": "broll", "duration": 5}],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-RETRY-SAME-SPEC','T-1','retry same spec','STORYBOARDED',?)",
            (json.dumps(storyboard),),
        )
        self.database.commit()

        dispatch_result = {
            "dispatched_jobs": [], "queued_jobs": ["J-RETRY-SAME-SPEC"],
        }
        with mock.patch.object(
            self.service, "dispatch_production_queue",
            return_value=dispatch_result,
        ):
            first = self.service.start_video_job(
                "J-RETRY-SAME-SPEC", confirmed=True
            )
            self.database.execute(
                "UPDATE jobs SET state='STORYBOARDED' "
                "WHERE job_id='J-RETRY-SAME-SPEC'"
            )
            self.database.commit()
            second = self.service.start_video_job(
                "J-RETRY-SAME-SPEC", confirmed=True
            )

        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "queued")
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-RETRY-SAME-SPEC'"
            ).fetchone()["state"],
            "PRODUCTION_QUEUED",
        )
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM job_transitions "
                "WHERE job_id='J-RETRY-SAME-SPEC' AND event='queued'"
            ).fetchone()[0],
            2,
        )

    def test_start_video_job_rolls_back_claim_when_worker_spawn_fails(self):
        storyboard = {
            "masters": [{"idx": 1, "type": "dialogue", "duration": 5}],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-SPAWN-FAIL','T-1','spawn fail','STORYBOARDED',?)",
            (json.dumps(storyboard),),
        )
        self.database.commit()

        with mock.patch(
            "factory.production_planner.seedance.estimate_cost_cents",
            return_value=100,
        ), mock.patch(
            "factory.management_api.subprocess.Popen",
            side_effect=OSError("spawn failed"),
        ):
            with self.assertRaisesRegex(OSError, "spawn failed"):
                self.service.start_video_job("J-SPAWN-FAIL", confirmed=True)

        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-SPAWN-FAIL'"
            ).fetchone()["state"],
            "PRODUCTION_QUEUED",
        )

    @mock.patch("factory.management_api.subprocess.Popen")
    def test_delete_failed_video_immediately_dispatches_oldest_queued_job(self, popen):
        (self.root / ".env").write_text(
            "ARK_API_KEY=secret-value\nMAX_CONCURRENT_JOBS=1\n",
            encoding="utf-8",
        )
        self.database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,created_at) "
            "VALUES(?,?,?,?,?,?)",
            [
                ("J-FAILED-DELETE", "T-F", "failed", "FAILED", "{}", "2026-01-01 00:00:00"),
                ("J-QUEUE-OLD", "T-Q1", "old", "PRODUCTION_QUEUED", "{}", "2026-01-01 00:01:00"),
                ("J-QUEUE-NEW", "T-Q2", "new", "PRODUCTION_QUEUED", "{}", "2026-01-01 00:02:00"),
            ],
        )
        self.database.execute(
            "INSERT INTO job_transitions(event_id,job_id,event,from_state,to_state) "
            "VALUES('J-FAILED-DELETE:failed','J-FAILED-DELETE','failed',"
            "'PRODUCTION_RUNNING','FAILED')"
        )
        self.database.commit()

        result = self.service.delete_video("J-FAILED-DELETE")

        self.assertEqual(result["dispatched_jobs"], ["J-QUEUE-OLD"])
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM character_identity_anchors "
                "WHERE job_id='J-FAILED-DELETE'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM job_transitions "
                "WHERE job_id='J-FAILED-DELETE'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-QUEUE-OLD'"
            ).fetchone()["state"],
            "PRODUCTION_RUNNING",
        )
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-QUEUE-NEW'"
            ).fetchone()["state"],
            "PRODUCTION_QUEUED",
        )
        command = popen.call_args.args[0]
        self.assertIn("--dispatched", command)
        self.assertIn("J-QUEUE-OLD", command)

    def test_delete_failed_video_returns_linked_script_to_reviewable_state(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-LINKED-FAILED','TOPIC-1','failed','FAILED','{}')"
        )
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json,"
            "approved_topic_id,approved_job_id,approved_angle_index) "
            "VALUES('BRIEF-LINKED','approved','topic','{}','TOPIC-1','J-LINKED-FAILED',0)"
        )
        self.database.execute(
            "INSERT INTO script_drafts(draft_id,brief_id,variant_index,status,"
            "request_fingerprint,script_json,approved_job_id,storyboard_json) "
            "VALUES('SCRIPT-LINKED','BRIEF-LINKED',0,'approved','fp','{}',"
            "'J-LINKED-FAILED','{}')"
        )
        self.database.commit()

        result = self.service.delete_video("J-LINKED-FAILED")

        self.assertEqual(result["restored_draft_id"], "SCRIPT-LINKED")
        draft = self.database.execute(
            "SELECT status,approved_job_id,storyboard_json FROM script_drafts "
            "WHERE draft_id='SCRIPT-LINKED'"
        ).fetchone()
        self.assertEqual(draft["status"], "candidate")
        self.assertIsNone(draft["approved_job_id"])
        self.assertIsNone(draft["storyboard_json"])
        brief = self.database.execute(
            "SELECT approved_job_id FROM topic_briefs WHERE brief_id='BRIEF-LINKED'"
        ).fetchone()
        self.assertEqual(brief["approved_job_id"], "J-LINKED-FAILED")

    def test_startup_repairs_approved_draft_whose_video_was_already_deleted(self):
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json,"
            "approved_topic_id,approved_job_id,approved_angle_index) "
            "VALUES('BRIEF-ORPHAN','approved','topic','{}','TOPIC-1','J-MISSING',0)"
        )
        self.database.execute(
            "INSERT INTO script_drafts(draft_id,brief_id,variant_index,status,"
            "request_fingerprint,script_json,approved_job_id,storyboard_json) "
            "VALUES('SCRIPT-ORPHAN','BRIEF-ORPHAN',0,'approved','fp','{}',"
            "'J-MISSING','{}')"
        )
        self.database.commit()

        repaired = self.service._repair_dangling_job_links()

        self.assertEqual(repaired, ["SCRIPT-ORPHAN"])
        row = self.database.execute(
            "SELECT status,approved_job_id FROM script_drafts "
            "WHERE draft_id='SCRIPT-ORPHAN'"
        ).fetchone()
        self.assertEqual(row["status"], "candidate")
        self.assertIsNone(row["approved_job_id"])
        recreated = self.database.execute(
            "SELECT state FROM jobs WHERE job_id='J-MISSING'"
        ).fetchone()
        self.assertIsNone(recreated)

    @mock.patch("factory.management_api.subprocess.Popen")
    def test_dispatcher_does_not_exceed_configured_active_job_limit(self, popen):
        (self.root / ".env").write_text(
            "ARK_API_KEY=secret-value\nMAX_CONCURRENT_JOBS=1\n",
            encoding="utf-8",
        )
        self.database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) VALUES(?,?,?,?,?)",
            [
                ("J-ACTIVE", "T-A", "active", "BGM_OK", "{}"),
                ("J-WAIT", "T-W", "waiting", "PRODUCTION_QUEUED", "{}"),
            ],
        )
        self.database.commit()

        result = self.service.dispatch_production_queue()

        self.assertEqual(result["dispatched_jobs"], [])
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-WAIT'"
            ).fetchone()["state"],
            "PRODUCTION_QUEUED",
        )
        popen.assert_not_called()

    @mock.patch("factory.management_api.subprocess.Popen")
    def test_dispatcher_persists_worker_pid_for_restart_recovery(self, popen):
        (self.root / ".env").write_text(
            "ARK_API_KEY=secret-value\nMAX_CONCURRENT_JOBS=1\n",
            encoding="utf-8",
        )
        popen.return_value.pid = 4321
        popen.return_value.poll.return_value = None
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-PID','T-PID','recoverable','PRODUCTION_QUEUED','{}')"
        )
        self.database.commit()

        self.service.dispatch_production_queue()

        row = self.database.execute(
            "SELECT state,production_worker_pid,production_started_at "
            "FROM jobs WHERE job_id='J-PID'"
        ).fetchone()
        self.assertEqual(row["state"], "PRODUCTION_RUNNING")
        self.assertEqual(row["production_worker_pid"], 4321)
        self.assertIsNotNone(row["production_started_at"])

    def test_queue_supervisor_pumps_queue_until_stopped(self):
        service = mock.Mock()
        stop = mock.Mock()
        stop.wait.side_effect = [False, True]
        lock = threading.RLock()

        run_queue_supervisor(service, lock, stop, interval_sec=0)

        service.dispatch_production_queue.assert_called_once_with()

    def test_queue_supervisor_persists_dispatch_errors(self):
        service = mock.Mock()
        service.dispatch_production_queue.side_effect = RuntimeError("queue db locked")
        stop = mock.Mock()
        stop.wait.side_effect = [False, True]

        run_queue_supervisor(service, threading.RLock(), stop, interval_sec=0)

        service.record_queue_supervisor_error.assert_called_once_with(
            "queue db locked"
        )

    @mock.patch("factory.management_api.subprocess.Popen")
    def test_exited_worker_is_failed_and_next_queued_job_is_dispatched(self, popen):
        (self.root / ".env").write_text(
            "ARK_API_KEY=secret-value\nMAX_CONCURRENT_JOBS=1\n",
            encoding="utf-8",
        )
        exited = mock.Mock()
        exited.poll.return_value = 2
        running = mock.Mock()
        running.poll.return_value = None
        popen.side_effect = [exited, running]
        self.database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,created_at) "
            "VALUES(?,?,?,?,?,?)",
            [
                ("J-WORKER-EXIT", "T-1", "first", "PRODUCTION_QUEUED", "{}", "2026-01-01 00:00:00"),
                ("J-WORKER-NEXT", "T-2", "next", "PRODUCTION_QUEUED", "{}", "2026-01-01 00:01:00"),
            ],
        )
        self.database.commit()

        first = self.service.dispatch_production_queue()
        second = self.service.dispatch_production_queue()

        self.assertEqual(first["dispatched_jobs"], ["J-WORKER-EXIT"])
        self.assertEqual(second["dispatched_jobs"], ["J-WORKER-NEXT"])
        failed = self.database.execute(
            "SELECT state,error FROM jobs WHERE job_id='J-WORKER-EXIT'"
        ).fetchone()
        self.assertEqual(failed["state"], "FAILED")
        self.assertEqual(
            failed["error"], "本地生产进程意外退出，任务现场已保留"
        )
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-WORKER-NEXT'"
            ).fetchone()["state"],
            "PRODUCTION_RUNNING",
        )

    @mock.patch("factory.management_api.subprocess.Popen")
    def test_exited_worker_preserves_business_pause_and_dispatches_next_job(self, popen):
        (self.root / ".env").write_text(
            "ARK_API_KEY=secret-value\nMAX_CONCURRENT_JOBS=1\n",
            encoding="utf-8",
        )
        exited = mock.Mock()
        exited.poll.return_value = 0
        running = mock.Mock()
        running.poll.return_value = None
        popen.side_effect = [exited, running]
        self.database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,created_at) "
            "VALUES(?,?,?,?,?,?)",
            [
                (
                    "J-REVIEW-EXIT", "T-REVIEW", "review pause",
                    "PRODUCTION_QUEUED", "{}", "2026-01-01 00:00:00",
                ),
                (
                    "J-REVIEW-NEXT", "T-NEXT", "next",
                    "PRODUCTION_QUEUED", "{}", "2026-01-01 00:01:00",
                ),
            ],
        )
        self.database.commit()

        first = self.service.dispatch_production_queue()
        self.database.execute(
            "UPDATE jobs SET state='PRODUCTION_REVIEW_REQUIRED',"
            "error='candidate requires human review' WHERE job_id='J-REVIEW-EXIT'"
        )
        self.database.commit()
        second = self.service.dispatch_production_queue()

        self.assertEqual(first["dispatched_jobs"], ["J-REVIEW-EXIT"])
        self.assertEqual(second["dispatched_jobs"], ["J-REVIEW-NEXT"])
        paused = self.database.execute(
            "SELECT state,error,failure_code,production_worker_pid FROM jobs "
            "WHERE job_id='J-REVIEW-EXIT'"
        ).fetchone()
        self.assertEqual(paused["state"], "PRODUCTION_REVIEW_REQUIRED")
        self.assertEqual(paused["error"], "candidate requires human review")
        self.assertIsNone(paused["failure_code"])
        self.assertIsNone(paused["production_worker_pid"])
        self.assertNotIn("J-REVIEW-EXIT", second["failed_workers"])

    def test_production_health_reports_business_queue_state(self):
        self.database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state,production_worker_pid) "
            "VALUES(?,?,?,?,?)",
            [
                ("J-HEALTH-RUN", "T-RUN", "正在生成", "PRODUCTION_RUNNING", os.getpid()),
                ("J-HEALTH-QUEUE", "T-QUEUE", "等待生成", "PRODUCTION_QUEUED", None),
            ],
        )
        self.database.executemany(
            "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
            [
                (
                    "J-RECOVERED",
                    "production_queue",
                    "warning",
                    "检测到生产进程已中断，任务已自动恢复排队",
                ),
                (None, "production_queue", "error", "queue db locked"),
            ],
        )
        self.database.commit()

        health_method = getattr(self.service, "production_health", None)
        self.assertIsNotNone(health_method)
        payload = health_method(supervisor_alive=False)

        self.assertEqual(
            set(payload),
            {
                "supervisor_alive",
                "concurrency_limit",
                "running",
                "queued",
                "orphaned_recovered",
                "last_error",
                "master_capacity",
                "provider_recovery",
            },
        )
        self.assertFalse(payload["supervisor_alive"])
        self.assertEqual(payload["concurrency_limit"], 1)
        self.assertEqual([item["job_id"] for item in payload["running"]], ["J-HEALTH-RUN"])
        self.assertEqual([item["job_id"] for item in payload["queued"]], ["J-HEALTH-QUEUE"])
        self.assertEqual(payload["orphaned_recovered"], 1)
        self.assertEqual(payload["last_error"], "queue db locked")
        self.assertEqual(payload["master_capacity"]["configured_limit"], 3)
        self.assertEqual(payload["master_capacity"]["effective_limit"], 3)

    def test_production_health_exposes_learned_master_capacity(self):
        provider_capacity.status(
            self.database, "seedance", configured_limit=3
        )
        provider_capacity.record_rate_limit(
            self.database, "seedance", configured_limit=3,
            cooldown_seconds=0,
        )

        payload = self.service.production_health(supervisor_alive=True)

        self.assertEqual(payload["master_capacity"]["configured_limit"], 3)
        self.assertEqual(payload["master_capacity"]["learned_limit"], 2)
        self.assertEqual(payload["master_capacity"]["effective_limit"], 2)
        self.assertEqual(
            payload["master_capacity"]["last_reason"],
            "definite_rate_limit",
        )

    def test_provider_reconcile_sweep_is_bounded_get_only_and_continues_after_failure(self):
        self.database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state) VALUES(?,?,?,?)",
            [
                ("J-SWEEP-1", "T-1", "核对一", "PROVIDER_ACTION_REQUIRED"),
                ("J-SWEEP-2", "T-2", "核对二", "PROVIDER_ACTION_REQUIRED"),
                ("J-SWEEP-3", "T-3", "核对三", "PROVIDER_ACTION_REQUIRED"),
            ],
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,state) VALUES(?,1,'FAILED')",
            [("J-SWEEP-1",), ("J-SWEEP-2",), ("J-SWEEP-3",)],
        )
        self.database.executemany(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES(?,?,1,'seedance','generate',?,0)",
            [
                ("task-sweep-1", "J-SWEEP-1", json.dumps({"status": "pending"})),
                ("task-sweep-2", "J-SWEEP-2", json.dumps({"status": "action_required"})),
                ("task-sweep-3", "J-SWEEP-3", json.dumps({"status": "pending"})),
            ],
        )
        self.database.commit()

        async def inspect(task_id):
            if task_id == "task-sweep-1":
                raise RuntimeError("temporary GET failure")
            return {"status": "running"}

        with mock.patch.object(
            seedance, "inspect_task", new=mock.AsyncMock(side_effect=inspect)
        ) as inspect_task, mock.patch.object(
            seedance, "text_to_video", new=mock.AsyncMock()
        ) as submit:
            report = self.service.reconcile_provider_tasks(limit=2)

        self.assertEqual(report["checked"], 2)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["pending"], 1)
        self.assertEqual(report["completed"], 0)
        self.assertEqual(inspect_task.await_count, 2)
        self.assertEqual(submit.await_count, 0)
        self.assertEqual(
            [item["task_id"] for item in report["items"]],
            ["task-sweep-1", "task-sweep-2"],
        )

    def test_provider_reconcile_sweeper_thread_stops_cleanly(self):
        service = mock.Mock()
        stop = threading.Event()
        service.reconcile_provider_tasks.side_effect = lambda limit: stop.set()

        run_provider_reconcile_sweeper(
            service, threading.RLock(), stop, interval_sec=0, limit=7
        )

        service.reconcile_provider_tasks.assert_called_once_with(limit=7)

    def test_production_health_exposes_last_provider_reconcile_sweep(self):
        self.service._last_provider_sweep = {
            "last_sweep_at": "2026-08-20T01:02:03Z",
            "checked": 3,
            "completed": 1,
            "pending": 1,
            "failed": 1,
            "skipped": 0,
            "items": [],
        }

        payload = self.service.production_health(supervisor_alive=True)

        self.assertEqual(payload["provider_recovery"]["checked"], 3)
        self.assertEqual(payload["provider_recovery"]["completed"], 1)

    def test_queued_or_running_video_cannot_be_deleted(self):
        for state in ("PRODUCTION_QUEUED", "PRODUCTION_RUNNING"):
            job_id = f"J-{state}"
            self.database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state) VALUES(?,?,?,?)",
                (job_id, "T-1", state, state),
            )
        self.database.commit()

        for state in ("PRODUCTION_QUEUED", "PRODUCTION_RUNNING"):
            with self.subTest(state=state), self.assertRaisesRegex(
                ValueError, "生成中"
            ):
                self.service.delete_video(f"J-{state}")

    def test_provider_reconciliation_job_cannot_be_deleted(self):
        for state in (
            "PROVIDER_ACTION_REQUIRED", "REJECTED_PENDING_RECONCILIATION",
        ):
            self.database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state) VALUES(?,?,?,?)",
                (f"J-{state}", "T-1", state, state),
            )
        self.database.commit()

        for state in (
            "PROVIDER_ACTION_REQUIRED", "REJECTED_PENDING_RECONCILIATION",
        ):
            with self.subTest(state=state), self.assertRaisesRegex(
                ValueError, "成本.*核对"
            ):
                self.service.delete_video(f"J-{state}")

    def test_video_workspace_exposes_preflight_shots_and_latest_human_review(self):
        final = self.root / "data" / "review.mp4"
        final.write_bytes(b"video")
        storyboard = {
            "production_profile": "standard",
            "characters": [{"id": "sales_xiaoling", "name": "小玲"}],
            "shots": [{"idx": 1, "duration": 8}, {"idx": 2, "duration": 6}],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,final_path,cost_cents) "
            "VALUES('J-REVIEW','T-1','待验收成片','COMPOSED',?,?,1200)",
            (json.dumps(storyboard), str(final)),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,state,provider,cost_cents,video_path) "
            "VALUES('J-REVIEW',1,8,'DONE','seedance',700,'master-1.mp4')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,state,provider,cost_cents,error) "
            "VALUES('J-REVIEW',2,6,'FAILED','seedance',500,'口型不一致')"
        )
        self.database.execute(
            "INSERT INTO review_events(job_id,decision,manual_minutes,first_pass,notes) "
            "VALUES('J-REVIEW','local_recut',4.5,0,'字幕需要微调')"
        )
        self.database.commit()

        job = self.service.videos()["jobs"][0]

        self.assertEqual(job["production_profile"], "standard")
        self.assertEqual(job["final_version"], final.stat().st_mtime_ns)
        self.assertEqual(job["characters"][0]["name"], "小玲")
        self.assertEqual(job["master_count"], 2)
        self.assertEqual(job["total_master_duration_sec"], 14.0)
        self.assertEqual(job["shots"][1]["error"], "口型不一致")
        self.assertEqual(job["latest_review"]["decision"], "local_recut")

    def test_only_human_approved_composed_video_can_enter_publish_queue(self):
        final = self.root / "data" / "approval.mp4"
        final.write_bytes(b"video")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,final_path) "
            "VALUES('J-APPROVAL','T-1','待人工验收','COMPOSED',?)", (str(final),)
        )
        self.database.commit()

        with self.assertRaisesRegex(ValueError, "人工验收"):
            self.service.submit_for_publish("J-APPROVAL")

        result = self.service.review_video(
            "J-APPROVAL", decision="approved", manual_minutes=3,
            first_pass=True, notes="身份、口型、字幕、手部均通过",
            checks={key: True for key in (
                "identity", "lip_sync", "subtitle_sync", "visual_integrity",
                "clean_frame", "editing_rhythm",
            )},
        )
        submitted = self.service.submit_for_publish("J-APPROVAL")

        self.assertEqual(result["decision"], "approved")
        self.assertEqual(submitted["state"], "READY_TO_PUBLISH")

    def test_rejected_or_recut_review_does_not_unlock_publication(self):
        final = self.root / "data" / "reject.mp4"
        final.write_bytes(b"video")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,final_path) "
            "VALUES('J-REJECT','T-1','验收不通过','COMPOSED',?)", (str(final),)
        )
        self.database.commit()

        self.service.review_video(
            "J-REJECT", decision="local_recut", notes="字幕需要修正"
        )

        with self.assertRaisesRegex(ValueError, "人工验收"):
            self.service.submit_for_publish("J-REJECT")

    def test_script_workspace_returns_full_source_brief_and_draft_content(self):
        self.database.execute(
            "INSERT INTO script_sources(source_id,fingerprint,source_type,title,source_uri,content) "
            "VALUES('SRC-CONTENT','fp-content','manual','人工材料','manual://cli','来源正文')"
        )
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,recommended_title,brief_json) "
            "VALUES('BRIEF-CONTENT','选题标题',?)",
            (json.dumps({"audience": "设备老板", "angles": [{"title": "角度一"}]}),),
        )
        self.database.execute(
            "INSERT INTO script_drafts(draft_id,brief_id,variant_index,request_fingerprint,script_json) "
            "VALUES('SCRIPT-CONTENT','BRIEF-CONTENT',0,'fp-script',?)",
            (json.dumps({"title": "完整剧本", "beats": [{"dialogue": "具体台词"}]}),),
        )
        self.database.commit()

        result = self.service.scripts()

        self.assertEqual(result["sources"][0]["content"], "来源正文")
        self.assertEqual(result["sources"][0]["source_display"], "人工创建")
        self.assertEqual(result["briefs"][0]["brief"]["audience"], "设备老板")
        self.assertEqual(result["drafts"][0]["script"]["title"], "完整剧本")

    def test_local_media_source_hides_temporary_machine_path(self):
        self.database.execute(
            "INSERT INTO script_sources(source_id,fingerprint,source_type,title,"
            "source_uri,content) VALUES(?,?,?,?,?,?)",
            (
                "SRC-LOCAL", "fp-local", "video", "访谈视频",
                r"C:\\temp\\management_uploads\\source-x\\clip.mp4", "转写正文",
            ),
        )
        self.database.commit()

        source = self.service.scripts()["sources"][0]

        self.assertEqual(source["source_display"], "人工上传")

    def test_candidate_script_and_idle_video_records_can_be_deleted(self):
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,recommended_title,brief_json) "
            "VALUES('BRIEF-DELETE','删除选题','{}')"
        )
        self.database.execute(
            "INSERT INTO script_drafts(draft_id,brief_id,variant_index,request_fingerprint,script_json) "
            "VALUES('SCRIPT-DELETE','BRIEF-DELETE',0,'fp-delete','{}')"
        )
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) "
            "VALUES('J-DELETE','T-DELETE','删除视频','STORYBOARDED')"
        )
        self.database.commit()

        self.assertEqual(self.service.delete_script("SCRIPT-DELETE")["deleted"], True)
        self.assertEqual(self.service.delete_video("J-DELETE")["deleted"], True)
        self.assertIsNone(self.database.execute(
            "SELECT 1 FROM script_drafts WHERE draft_id='SCRIPT-DELETE'"
        ).fetchone())
        self.assertIsNone(self.database.execute(
            "SELECT 1 FROM jobs WHERE job_id='J-DELETE'"
        ).fetchone())

    def test_completed_video_must_be_submitted_before_it_enters_publish_queue(self):
        final = self.root / "data" / "final.mp4"
        final.write_bytes(b"video")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,final_path) "
            "VALUES('J-COMPOSED','T-1','成品视频','COMPOSED',?)", (str(final),)
        )
        self.database.execute(
            "INSERT INTO review_events(job_id,decision,manual_minutes,first_pass,notes) "
            "VALUES('J-COMPOSED','approved',2,1,'验收通过')"
        )
        self.database.commit()

        self.assertEqual(self.service.publishing()["ready"], [])
        submitted = self.service.submit_for_publish("J-COMPOSED")

        self.assertEqual(submitted["state"], "READY_TO_PUBLISH")
        self.assertEqual(self.service.publishing()["ready"][0]["job_id"], "J-COMPOSED")

    def test_recent_activity_is_business_facing_and_chinese(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,updated_at) "
            "VALUES('J-ACT','T-1','租赁真相','COMPOSED',CURRENT_TIMESTAMP)"
        )
        self.database.execute(
            "INSERT INTO events(job_id,step,level,message) "
            "VALUES('J-ACT','s6_compose','error','raw provider stack trace')"
        )
        self.database.commit()

        activity = self.service.recent_events()["events"]

        self.assertEqual(activity[0]["category"], "视频")
        self.assertIn("成片", activity[0]["message"])
        self.assertNotIn("raw provider", json.dumps(activity, ensure_ascii=False))

    def test_publication_registration_marks_job_published_after_all_channels(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) "
            "VALUES('J-PUBLISH','T-1','多渠道发布','READY_TO_PUBLISH')"
        )
        self.database.commit()

        result = self.service.register_publications({
            "job_id": "J-PUBLISH",
            "channels": [
                {"platform": "douyin", "item_id": "dy-1"},
                {"platform": "weixin_channels", "item_id": "wx-1"},
            ],
        })

        self.assertEqual(result["saved"], 2)
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-PUBLISH'"
            ).fetchone()["state"],
            "PUBLISHED",
        )

    def test_publication_registration_rejects_non_ready_job_and_unknown_channel(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) "
            "VALUES('J-NOT-READY','T-1','not ready','COMPOSED')"
        )
        self.database.commit()

        with self.assertRaisesRegex(ValueError, "待发布"):
            self.service.register_publication({
                "job_id": "J-NOT-READY", "platform": "douyin", "item_id": "x",
            })

        self.database.execute(
            "UPDATE jobs SET state='READY_TO_PUBLISH' WHERE job_id='J-NOT-READY'"
        )
        self.database.commit()
        with self.assertRaisesRegex(ValueError, "渠道"):
            self.service.register_publication({
                "job_id": "J-NOT-READY", "platform": "unknown", "item_id": "x",
            })

    def test_batch_publication_validation_fails_before_writing_any_channel(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) "
            "VALUES('J-ATOMIC','T-1','atomic','READY_TO_PUBLISH')"
        )
        self.database.commit()

        with self.assertRaisesRegex(ValueError, "渠道"):
            self.service.register_publications({
                "job_id": "J-ATOMIC",
                "channels": [
                    {"platform": "douyin", "item_id": "dy-ok"},
                    {"platform": "unknown", "item_id": "bad"},
                ],
            })

        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM platform_publications WHERE job_id='J-ATOMIC'"
            ).fetchone()[0],
            0,
        )

    def test_partial_channel_publish_remains_available_for_the_other_channel(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) "
            "VALUES('J-PARTIAL','T-1','分渠道发布','READY_TO_PUBLISH')"
        )
        self.database.commit()

        result = self.service.register_publications({
            "job_id": "J-PARTIAL",
            "channels": [{"platform": "douyin", "item_id": "dy-only"}],
        })
        ready = self.service.publishing()["ready"]

        self.assertEqual(result["state"], "READY_TO_PUBLISH")
        self.assertEqual(ready[0]["published_platforms"], ["douyin"])
        self.assertEqual(ready[0]["remaining_platforms"], ["weixin_channels"])

    def test_pending_platform_review_is_not_counted_as_published(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) "
            "VALUES('J-REVIEW','T-1','pending review','READY_TO_PUBLISH')"
        )
        self.database.commit()

        result = self.service.register_publications({
            "job_id": "J-REVIEW",
            "channels": [
                {
                    "platform": "douyin",
                    "item_id": "dy-reviewing",
                    "metadata": {"review_status": "pending", "publish_source": "openapi"},
                },
                {"platform": "weixin_channels", "item_id": "wx-published"},
            ],
        })
        publishing = self.service.publishing()
        ready = next(item for item in publishing["ready"] if item["job_id"] == "J-REVIEW")
        douyin = next(
            item for item in publishing["publications"]
            if item["job_id"] == "J-REVIEW" and item["platform"] == "douyin"
        )

        self.assertEqual(result["state"], "READY_TO_PUBLISH")
        self.assertEqual(ready["published_platforms"], ["weixin_channels"])
        self.assertEqual(ready["pending_platforms"], ["douyin"])
        self.assertEqual(ready["remaining_platforms"], [])
        self.assertEqual(douyin["verification_status"], "pending_review")
        self.assertEqual(self.service.overview()["publishing"]["published"], 0)

    def test_legacy_published_without_mapping_is_unverified_not_truly_published(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) "
            "VALUES('J-LEGACY','T-1','Legacy publish','PUBLISHED')"
        )
        self.database.commit()

        overview = self.service.overview()
        publishing = self.service.publishing()

        self.assertEqual(overview["publishing"]["published"], 0)
        self.assertEqual(overview["publishing"]["legacy_unverified"], 1)
        self.assertEqual(publishing["publications"], [])
        self.assertEqual(publishing["unverified"][0]["job_id"], "J-LEGACY")
        self.assertEqual(
            publishing["unverified"][0]["verification_status"],
            "legacy_unverified",
        )

    def test_publication_detail_records_and_returns_latest_performance_snapshot(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) "
            "VALUES('J-PERF','T-1','Performance video','PUBLISHED')"
        )
        self.database.execute(
            "INSERT INTO platform_publications(publication_id,job_id,platform,item_id) "
            "VALUES('PUB-PERF','J-PERF','douyin','dy-perf')"
        )
        self.database.commit()

        recorded = self.service.record_performance_snapshot("PUB-PERF", {
            "captured_at": "2026-08-13T12:00:00+08:00",
            "views": 1000,
            "completed_views": 400,
            "average_watch_sec": 18.5,
            "likes": 60,
            "comments": 12,
            "shares": 8,
            "saves": 20,
            "follows": 15,
            "leads": 3,
        })

        self.assertEqual(recorded["latest_snapshot"]["views"], 1000)
        self.assertEqual(recorded["latest_snapshot"]["completion_rate"], 0.4)
        publication = self.service.publishing()["publications"][0]
        self.assertEqual(publication["latest_snapshot"]["snapshot_id"], recorded["snapshot_id"])

    def test_publishing_exposes_due_performance_windows_as_actionable_tasks(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) "
            "VALUES('J-DUE','T-1','待回收绩效','PUBLISHED')"
        )
        self.database.execute(
            "INSERT INTO platform_publications("
            "publication_id,job_id,platform,item_id,published_at"
            ") VALUES('PUB-DUE','J-DUE','weixin_channels','wx-due',"
            "'2026-08-01T00:00:00+00:00')"
        )
        self.database.commit()

        due = self.service.publishing()["performance_due"]

        self.assertEqual([item["window_hours"] for item in due], [24, 72, 168])
        self.assertEqual(due[0]["publication_id"], "PUB-DUE")

    def test_config_exposes_platform_publish_modes_and_priority_impact(self):
        status = self.service.config_status()

        groups = {group["id"]: group for group in status["required_groups"]}
        self.assertEqual(groups["ark"]["priority"], "P0")
        self.assertEqual(groups["asr"]["priority"], "P1")
        self.assertFalse(groups["asr"]["blocking"])
        self.assertIn("字幕", groups["asr"]["impact"])
        platforms = {item["id"]: item for item in status["publish_platforms"]}
        self.assertEqual(platforms["douyin"]["mode"], "oauth")
        self.assertFalse(platforms["douyin"]["connected"])
        self.assertEqual(platforms["weixin_channels"]["mode"], "official_assistant")
        self.assertTrue(platforms["weixin_channels"]["configured"])
        self.assertEqual(
            platforms["weixin_channels"]["entry_url"],
            "https://channels.weixin.qq.com/login",
        )
        self.assertEqual(
            platforms["weixin_channels"]["account_binding"],
            "official_qr_session",
        )

    def test_weixin_assistant_override_rejects_non_official_host(self):
        (self.root / ".env").write_text(
            "WEIXIN_CHANNELS_ASSISTANT_URL=https://example.com/fake-login\n",
            encoding="utf-8",
        )

        platforms = {
            item["id"]: item for item in self.service.config_status()["publish_platforms"]
        }

        self.assertEqual(
            platforms["weixin_channels"]["entry_url"],
            "https://channels.weixin.qq.com/login",
        )

    def test_oauth_access_token_is_never_returned_to_the_browser(self):
        (self.root / ".env").write_text(
            "DOUYIN_ACCESS_TOKEN=private-oauth-token\n", encoding="utf-8"
        )

        fields = {
            field["key"]: field for field in self.service.config_status()["fields"]
        }

        self.assertNotIn("DOUYIN_ACCESS_TOKEN", fields)
        self.assertNotIn(
            "private-oauth-token", json.dumps(self.service.config_status())
        )


    @mock.patch("factory.management_api.script_drafts.generate_drafts")
    @mock.patch("factory.management_api.topic_briefs.approve_brief")
    @mock.patch("factory.management_api.topic_briefs.generate_brief")
    def test_management_service_runs_source_to_script_without_cli(
        self, generate_brief, approve_brief, generate_drafts
    ):
        self.database.execute(
            "INSERT INTO script_sources(source_id,fingerprint,source_type,title,content) "
            "VALUES('SRC-1','fp-source-flow','manual','人工素材','正文')"
        )
        self.database.commit()
        generate_brief.return_value = {
            "brief_id": "BRIEF-1", "status": "candidate",
            "brief": {"recommended_index": 0, "angles": [{}, {}, {}]},
            "cached": False,
        }
        approve_brief.return_value = "J-TOPIC-1"
        generate_drafts.return_value = {
            "brief_id": "BRIEF-1", "drafts": [{}, {}, {}], "cached": False,
        }

        brief = self.service.generate_brief_from_source(
            "SRC-1", risk_confirmed=True
        )
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,recommended_title,brief_json) "
            "VALUES('BRIEF-1','候选选题',?)",
            (json.dumps({"audience": "设备老板", "angles": [{}, {}, {}]}),),
        )
        self.database.commit()
        scripts = self.service.confirm_brief_and_generate_scripts(
            "BRIEF-1",
            angle_index=1,
            generation_preferences={
                "suggested_duration_sec": 45,
                "suggested_shot_count": 7,
                "suggested_character_count": 3,
            },
        )

        self.assertEqual(brief["brief_id"], "BRIEF-1")
        self.assertEqual(scripts["job_id"], "J-TOPIC-1")
        self.assertEqual(scripts["draft_count"], 3)
        approve_brief.assert_called_once()
        self.assertEqual(approve_brief.call_args.kwargs["angle_index"], 1)
        self.assertEqual(
            generate_drafts.call_args.kwargs["preferences"],
            {
                "suggested_duration_sec": 45,
                "suggested_shot_count": 7,
                "suggested_character_count": 3,
            },
        )

    @mock.patch("factory.management_api.script_drafts.generate_drafts")
    def test_script_generation_failure_rolls_back_brief_approval(self, generate_drafts):
        self.database.execute(
            "INSERT INTO script_sources(source_id,fingerprint,source_type,title,content) "
            "VALUES('SRC-ROLLBACK','fp-rollback','manual','待生成来源','完整正文')"
        )
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json) "
            "VALUES('BRIEF-ROLLBACK','candidate','待生成简报',?)",
            (json.dumps({
                "audience": "测试受众",
                "recommended_index": 0,
                "angles": [{"title": "测试角度"}],
            }, ensure_ascii=False),),
        )
        self.database.execute(
            "INSERT INTO topic_brief_sources(brief_id,source_id) "
            "VALUES('BRIEF-ROLLBACK','SRC-ROLLBACK')"
        )
        self.database.commit()
        generate_drafts.side_effect = RuntimeError("provider timeout")

        with self.assertRaisesRegex(RuntimeError, "provider timeout"):
            self.service.confirm_brief_and_generate_scripts(
                "BRIEF-ROLLBACK", angle_index=0
            )

        brief = self.database.execute(
            "SELECT status,approved_topic_id,approved_job_id,approved_angle_index "
            "FROM topic_briefs WHERE brief_id='BRIEF-ROLLBACK'"
        ).fetchone()
        self.assertEqual(brief["status"], "candidate")
        self.assertIsNone(brief["approved_topic_id"])
        self.assertIsNone(brief["approved_job_id"])
        self.assertIsNone(brief["approved_angle_index"])
        self.assertIsNone(self.database.execute(
            "SELECT 1 FROM jobs WHERE job_id='J-TOPIC-ROLLBACK'"
        ).fetchone())

    def test_candidate_brief_can_be_edited_with_revision_history(self):
        self._insert_editable_content()
        payload = self._editable_brief_payload(title="人工调整后的选题")

        result = self.service.update_brief(
            "BRIEF-EDIT", payload, change_note="强化核心冲突"
        )

        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["brief"]["angles"][0]["title"], "人工调整后的选题")
        row = self.database.execute(
            "SELECT recommended_title,revision FROM topic_briefs "
            "WHERE brief_id='BRIEF-EDIT'"
        ).fetchone()
        self.assertEqual(dict(row), {
            "recommended_title": "人工调整后的选题", "revision": 2,
        })
        history = self.database.execute(
            "SELECT revision,change_note FROM content_revisions "
            "WHERE entity_type='brief' AND entity_id='BRIEF-EDIT'"
        ).fetchone()
        self.assertEqual(dict(history), {
            "revision": 1, "change_note": "强化核心冲突",
        })

    def test_brief_edit_invalidates_only_unapproved_downstream_content(self):
        self._insert_editable_content(approved=True)

        result = self.service.update_brief(
            "BRIEF-EDIT", self._editable_brief_payload(title="新选题")
        )

        self.assertTrue(result["downstream_invalidated"])
        self.assertIsNone(self.database.execute(
            "SELECT 1 FROM script_drafts WHERE brief_id='BRIEF-EDIT'"
        ).fetchone())
        self.assertIsNone(self.database.execute(
            "SELECT 1 FROM jobs WHERE job_id='J-TOPIC-EDIT'"
        ).fetchone())
        brief = self.database.execute(
            "SELECT status,approved_job_id FROM topic_briefs "
            "WHERE brief_id='BRIEF-EDIT'"
        ).fetchone()
        self.assertEqual(brief["status"], "candidate")
        self.assertIsNone(brief["approved_job_id"])

    def test_brief_edit_is_locked_after_script_enters_video_pipeline(self):
        self._insert_editable_content(approved=True, script_approved=True)

        with self.assertRaisesRegex(ValueError, "已进入视频制作"):
            self.service.update_brief(
                "BRIEF-EDIT", self._editable_brief_payload(title="不应覆盖")
            )

    def test_candidate_script_can_be_edited_and_locks_after_approval(self):
        self._insert_editable_content(approved=True)
        payload = self._editable_script_payload(title="人工改写版")

        result = self.service.update_script_draft(
            "SCRIPT-EDIT", payload, change_note="调整台词节奏"
        )

        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["script"]["title"], "人工改写版")
        history = self.database.execute(
            "SELECT revision,change_note FROM content_revisions "
            "WHERE entity_type='script' AND entity_id='SCRIPT-EDIT'"
        ).fetchone()
        self.assertEqual(dict(history), {
            "revision": 1, "change_note": "调整台词节奏",
        })
        self.database.execute(
            "UPDATE script_drafts SET status='approved' "
            "WHERE draft_id='SCRIPT-EDIT'"
        )
        self.database.commit()
        with self.assertRaisesRegex(ValueError, "已提交视频制作"):
            self.service.update_script_draft("SCRIPT-EDIT", payload)

    def test_script_edit_can_recalculate_beat_and_total_durations_from_dialogue(self):
        self._insert_editable_content(approved=True)
        payload = self._editable_script_payload(title="自动重估时长")
        payload["beats"][0]["dialogue"] = (
            "这是一段编辑后明显更长的台词，需要留出自然停顿，让人物能够完整说完。"
        )

        result = self.service.update_script_draft(
            "SCRIPT-EDIT",
            payload,
            change_note="扩写开场台词",
            auto_adjust_durations=True,
        )

        script = result["script"]
        self.assertTrue(result["duration_adjusted"])
        self.assertEqual(script["duration_mode"], "auto")
        self.assertGreater(script["beats"][0]["duration_sec"], 4)
        self.assertEqual(
            script["total_duration_sec"],
            round(sum(beat["duration_sec"] for beat in script["beats"]), 2),
        )
        self.assertEqual(script["target_duration_sec"], script["total_duration_sec"])

    def test_manual_script_edit_repairs_zero_duration_from_dialogue(self):
        self._insert_editable_content(approved=True)
        payload = self._editable_script_payload(title="人工修订零时长")
        payload["beats"][2]["dialogue"] = "证据段落仍然有完整台词"
        payload["beats"][2]["duration_sec"] = 0
        payload["target_duration_sec"] = 20
        payload["total_duration_sec"] = 20

        result = self.service.update_script_draft(
            "SCRIPT-EDIT", payload, auto_adjust_durations=False
        )

        script = result["script"]
        self.assertGreater(script["beats"][2]["duration_sec"], 0)
        self.assertEqual(
            script["target_duration_sec"], script["total_duration_sec"]
        )
        self.assertTrue(result["duration_adjusted"])

    def _editable_brief_payload(self, *, title="原始选题"):
        angles = []
        for index in range(3):
            angles.append({
                "title": title if index == 0 else f"备选角度 {index + 1}",
                "hook": f"开场钩子 {index + 1}",
                "promise": "让观众获得明确结论",
                "story_arc": "冲突→证据→反转→行动",
                "evidence": ["来源中的关键事实"],
                "risk_notes": [],
                "production_notes": "人物行动与场景运镜结合",
                "fact_ids": ["F1"],
            })
        return {
            "audience": "目标观众",
            "source_score": {
                "relevance": 90, "story_potential": 85,
                "evidence_quality": 80, "production_fit": 88,
                "reason": "来源完整",
            },
            "source_digest": {"facts": [{
                "fact_id": "F1", "statement": "来源中的关键事实",
                "quote": "来源中的关键事实", "source_id": "SRC-EDIT",
                "importance": "must_keep",
            }]},
            "angles": angles,
            "recommended_index": 0,
            "recommendation_reason": "事实覆盖完整",
        }

    def _editable_script_payload(self, *, title="候选剧本"):
        beats = []
        for index, beat in enumerate(
            ("hook", "conflict", "evidence", "reversal", "solution", "cta")
        ):
            beats.append({
                "beat": beat, "speaker": "presenter",
                "dialogue": f"第 {index + 1} 句人工台词",
                "duration_sec": 4, "visual_intent": "人物移动并配合镜头推进",
                "source_ids": ["SRC-EDIT"], "broll_hint": "现场细节",
            })
        return {
            "title": title, "creative_thesis": "用事实建立冲突",
            "target_duration_sec": 24, "total_duration_sec": 24,
            "beats": beats,
        }

    def _insert_editable_content(self, *, approved=False, script_approved=False):
        brief = self._editable_brief_payload()
        self.database.execute(
            "INSERT INTO script_sources(source_id,fingerprint,source_type,title,content) "
            "VALUES('SRC-EDIT','fp-edit','manual','可编辑来源','来源中的关键事实')"
        )
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json,"
            "approved_topic_id,approved_job_id,approved_angle_index) "
            "VALUES('BRIEF-EDIT',?,?,?,?,?,?)",
            (
                "approved" if approved else "candidate", "原始选题",
                json.dumps(brief, ensure_ascii=False),
                "TOPIC-EDIT" if approved else None,
                "J-TOPIC-EDIT" if approved else None,
                0 if approved else None,
            ),
        )
        self.database.execute(
            "INSERT INTO topic_brief_sources(brief_id,source_id) "
            "VALUES('BRIEF-EDIT','SRC-EDIT')"
        )
        if approved:
            self.database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state) "
                "VALUES('J-TOPIC-EDIT','TOPIC-EDIT','原始选题',?)",
                ("STORYBOARDED" if script_approved else "PENDING",),
            )
            self.database.execute(
                "INSERT INTO topic_script_sources(topic_id,source_id) "
                "VALUES('TOPIC-EDIT','SRC-EDIT')"
            )
            self.database.execute(
                "INSERT INTO script_drafts(draft_id,brief_id,variant_index,status,"
                "request_fingerprint,script_json,approved_job_id) VALUES(?,?,?,?,?,?,?)",
                (
                    "SCRIPT-EDIT", "BRIEF-EDIT", 0,
                    "approved" if script_approved else "candidate", "fp-script-edit",
                    json.dumps(self._editable_script_payload(), ensure_ascii=False),
                    "J-TOPIC-EDIT" if script_approved else None,
                ),
            )
        self.database.commit()

    def test_source_and_brief_delete_clear_only_unapproved_descendants(self):
        self.database.execute(
            "INSERT INTO script_sources(source_id,fingerprint,source_type,title,content) "
            "VALUES('SRC-DELETE-FLOW','fp-flow','manual','待删来源','正文')"
        )
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,recommended_title,brief_json) "
            "VALUES('BRIEF-DELETE-FLOW','待删简报','{}')"
        )
        self.database.execute(
            "INSERT INTO topic_brief_sources(brief_id,source_id) "
            "VALUES('BRIEF-DELETE-FLOW','SRC-DELETE-FLOW')"
        )
        self.database.execute(
            "INSERT INTO script_drafts(draft_id,brief_id,variant_index,"
            "request_fingerprint,script_json) "
            "VALUES('SCRIPT-DELETE-FLOW','BRIEF-DELETE-FLOW',0,'fp-script','{}')"
        )
        self.database.commit()

        result = self.service.delete_source("SRC-DELETE-FLOW")

        self.assertEqual(result["deleted_briefs"], 1)
        self.assertIsNone(self.database.execute(
            "SELECT 1 FROM script_sources WHERE source_id='SRC-DELETE-FLOW'"
        ).fetchone())
        self.assertIsNone(self.database.execute(
            "SELECT 1 FROM topic_briefs WHERE brief_id='BRIEF-DELETE-FLOW'"
        ).fetchone())

    def test_approved_brief_and_its_source_cannot_be_deleted(self):
        self.database.execute(
            "INSERT INTO script_sources(source_id,fingerprint,source_type,title,content) "
            "VALUES('SRC-PROTECTED','fp-protected','manual','受保护来源','正文')"
        )
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json,"
            "approved_job_id) VALUES('BRIEF-PROTECTED','approved','已确认简报','{}','J-X')"
        )
        self.database.execute(
            "INSERT INTO topic_brief_sources(brief_id,source_id) "
            "VALUES('BRIEF-PROTECTED','SRC-PROTECTED')"
        )
        self.database.commit()

        with self.assertRaisesRegex(ValueError, "进入视频流水线"):
            self.service.delete_brief("BRIEF-PROTECTED")
        with self.assertRaisesRegex(ValueError, "进入视频流水线"):
            self.service.delete_source("SRC-PROTECTED")


    def test_batch_candidate_delete_is_atomic_when_one_id_is_invalid(self):
        self.database.execute(
            "INSERT INTO source_feeds(feed_id,feed_type,name,endpoint) "
            "VALUES('FEED-BATCH','rss','批量源','https://example.com/feed.xml')"
        )
        self.database.execute(
            "INSERT INTO source_candidates(candidate_id,feed_id,external_id,title) "
            "VALUES('CAND-VALID','FEED-BATCH','ext-valid','有效候选')"
        )
        self.database.commit()

        with self.assertRaisesRegex(ValueError, "CAND-MISSING"):
            self.service.batch_delete_source_candidates(
                ["CAND-VALID", "CAND-MISSING"]
            )

        self.assertIsNotNone(self.database.execute(
            "SELECT 1 FROM source_candidates WHERE candidate_id='CAND-VALID'"
        ).fetchone())

    def test_batch_script_rejection_is_atomic_when_one_draft_is_invalid(self):
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json) "
            "VALUES('BRIEF-BATCH','approved','批量简报','{}')"
        )
        self.database.execute(
            "INSERT INTO script_drafts(draft_id,brief_id,variant_index,status,"
            "request_fingerprint,script_json) "
            "VALUES('SCRIPT-VALID','BRIEF-BATCH',0,'candidate','fp-valid','{}')"
        )
        self.database.commit()

        with self.assertRaisesRegex(ValueError, "SCRIPT-MISSING"):
            self.service.batch_reject_scripts(["SCRIPT-VALID", "SCRIPT-MISSING"])

        status = self.database.execute(
            "SELECT status FROM script_drafts WHERE draft_id='SCRIPT-VALID'"
        ).fetchone()["status"]
        self.assertEqual(status, "candidate")

    def test_batch_production_preflight_never_starts_or_mutates_jobs(self):
        storyboard = {
            "masters": [{"idx": 1, "type": "dialogue", "duration": 8}],
            "production_settings": {"budget_cents": 2500},
        }
        self.database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) VALUES(?,?,?,?,?)",
            [
                ("J-PREFLIGHT-1", "T-P1", "预检一", "STORYBOARDED", json.dumps(storyboard)),
                ("J-PREFLIGHT-2", "T-P2", "预检二", "STORYBOARDED", json.dumps(storyboard)),
            ],
        )
        self.database.commit()

        with mock.patch("factory.management_api.subprocess.Popen") as popen:
            result = self.service.batch_production_preflight(
                ["J-PREFLIGHT-1", "J-PREFLIGHT-2"]
            )

        popen.assert_not_called()
        self.assertEqual(result["paid_provider_calls"], 0)
        self.assertEqual(result["job_count"], 2)
        states = [row[0] for row in self.database.execute(
            "SELECT state FROM jobs WHERE job_id LIKE 'J-PREFLIGHT-%' ORDER BY job_id"
        )]
        self.assertEqual(states, ["STORYBOARDED", "STORYBOARDED"])

    def test_todo_center_groups_records_by_required_human_action(self):
        self.database.execute(
            "INSERT INTO source_feeds(feed_id,feed_type,name,endpoint) "
            "VALUES('FEED-TODO','rss','待办源','https://example.com/feed.xml')"
        )
        self.database.execute(
            "INSERT INTO source_candidates(candidate_id,feed_id,external_id,title) "
            "VALUES('CAND-TODO','FEED-TODO','ext-todo','待决定热点')"
        )
        self.database.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json) "
            "VALUES('BRIEF-TODO','approved','待办简报','{}')"
        )
        self.database.execute(
            "INSERT INTO script_drafts(draft_id,brief_id,variant_index,status,"
            "request_fingerprint,script_json) "
            "VALUES('SCRIPT-TODO','BRIEF-TODO',0,'candidate','fp-todo','{}')"
        )
        self.database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) VALUES(?,?,?,?,?)",
            [
                ("J-TODO-SETUP", "T-S", "待制作设置", "STORYBOARDED", "{}"),
                ("J-TODO-REVIEW", "T-R", "待母片复核", "FAILED", "{}"),
                ("J-TODO-ANCHOR", "T-AR", "待锚点确认", "ANCHOR_REVIEW_REQUIRED", "{}"),
                ("J-TODO-ACCEPT", "T-A", "待成片验收", "COMPOSED", "{}"),
                ("J-TODO-PUBLISH", "T-P", "待发布", "READY_TO_PUBLISH", "{}"),
                ("J-TODO-PERF", "T-F", "待绩效", "PUBLISHED", "{}"),
                ("J-TODO-PROVIDER", "T-PA", "待模型核对", "PROVIDER_ACTION_REQUIRED", "{}"),
            ],
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_qa) VALUES(?,?,?,?)",
            ("J-TODO-REVIEW", 1, "FAILED", json.dumps({"status": "manual_review_required"})),
        )
        self.database.execute(
            "INSERT INTO platform_publications(publication_id,job_id,platform,item_id,title,published_at,metadata_json) "
            "VALUES('PUB-TODO','J-TODO-PERF','douyin','item-todo','待绩效',"
            "'2026-08-01T00:00:00+00:00','{}')"
        )
        self.database.commit()

        groups = {group["key"]: group for group in self.service.todo_center()["groups"]}

        self.assertEqual(set(groups), {
            "content_decision", "production_setup", "manual_review",
            "provider_action", "final_acceptance", "publish", "performance",
        })
        self.assertIn("CAND-TODO", {item["id"] for item in groups["content_decision"]["items"]})
        self.assertIn("SCRIPT-TODO", {item["id"] for item in groups["content_decision"]["items"]})
        self.assertEqual(groups["production_setup"]["items"][0]["id"], "J-TODO-SETUP")
        self.assertEqual(
            {item["id"] for item in groups["manual_review"]["items"]},
            {"J-TODO-REVIEW", "J-TODO-ANCHOR"},
        )
        anchor_item = next(
            item for item in groups["manual_review"]["items"]
            if item["id"] == "J-TODO-ANCHOR"
        )
        self.assertEqual(anchor_item["kind"], "anchor_review")
        self.assertEqual(anchor_item["stage"], "ANCHOR_REVIEW_REQUIRED")
        self.assertGreaterEqual(self.service.overview()["videos"]["active"], 1)
        self.assertEqual(groups["final_acceptance"]["items"][0]["id"], "J-TODO-ACCEPT")
        self.assertEqual(groups["publish"]["items"][0]["id"], "J-TODO-PUBLISH")
        self.assertEqual(groups["provider_action"]["items"][0]["id"], "J-TODO-PROVIDER")
        self.assertEqual(groups["provider_action"]["items"][0]["kind"], "provider_action")
        self.assertTrue(any(
            item["id"] == "PUB-TODO" for item in groups["performance"]["items"]
        ))

    def test_provider_action_job_is_not_eligible_for_master_review(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-PROVIDER-REVIEW','T-PA','provider action',"
            "'PROVIDER_ACTION_REQUIRED','{}')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_qa) VALUES(?,?,?,?)",
            (
                "J-PROVIDER-REVIEW", 1, "FAILED",
                json.dumps({"status": "manual_review_required"}),
            ),
        )
        self.database.commit()

        video = next(
            job for job in self.service.videos()["jobs"]
            if job["job_id"] == "J-PROVIDER-REVIEW"
        )
        groups = {
            group["key"]: group for group in self.service.todo_center()["groups"]
        }

        self.assertEqual(video["state"], "PROVIDER_ACTION_REQUIRED")
        self.assertTrue(video["shots"][0]["manual_review_required"])
        self.assertNotIn(
            "J-PROVIDER-REVIEW",
            {item["id"] for item in groups["manual_review"]["items"]},
        )

    def test_provider_task_reconciliation_completed_seedance_enters_review_without_repost(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,error) "
            "VALUES('J-RECONCILE','T-1','reconcile',"
            "'PROVIDER_ACTION_REQUIRED','{}',0,'unknown remote task')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents,error) "
            "VALUES('J-RECONCILE',1,'FAILED',0,'submit result unknown')"
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES('seedance-remote-1','J-RECONCILE',1,"
            "'seedance','generate',?,0)",
            (json.dumps({"status": "action_required"}),),
        )
        self.database.commit()

        inspected = {
            "status": "completed", "video": b"remote-paid-video",
            "video_url": "https://cdn/remote-paid.mp4",
            "usage": {"completion_tokens": 100000}, "cost_cents": 700,
        }
        with mock.patch(
            "factory.management_api.seedance.inspect_task",
            new=mock.AsyncMock(return_value=inspected),
        ) as inspect, mock.patch(
            "factory.management_api.seedance.text_to_video",
            new=mock.AsyncMock(),
        ) as submit:
            result = self.service.reconcile_provider_task(
                "J-RECONCILE", "seedance-remote-1"
            )

        inspect.assert_awaited_once_with("seedance-remote-1")
        submit.assert_not_awaited()
        self.assertEqual(result["state"], "PRODUCTION_REVIEW_REQUIRED")
        self.assertEqual(result["cost_delta_cents"], 700)
        shot = self.database.execute(
            "SELECT state,video_qa,cost_cents FROM shots "
            "WHERE job_id='J-RECONCILE'"
        ).fetchone()
        qa = json.loads(shot["video_qa"])
        self.assertEqual(shot["state"], "REVIEW_REQUIRED")
        self.assertEqual(shot["cost_cents"], 700)
        self.assertEqual(Path(qa["candidate_path"]).read_bytes(), b"remote-paid-video")
        call = self.database.execute(
            "SELECT usage_json,cost_cents FROM provider_calls "
            "WHERE task_id='seedance-remote-1'"
        ).fetchone()
        self.assertEqual(json.loads(call["usage_json"])["status"], "completed")
        self.assertEqual(call["cost_cents"], 700)
        self.assertEqual(self.database.execute(
            "SELECT cost_cents FROM jobs WHERE job_id='J-RECONCILE'"
        ).fetchone()["cost_cents"], 700)

    def test_provider_task_reconciliation_pending_keeps_job_paused(self):
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES('J-RECONCILE-PENDING','T-1','pending',"
            "'PROVIDER_ACTION_REQUIRED','{}')"
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state) "
            "VALUES('J-RECONCILE-PENDING',1,'FAILED')"
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES('seedance-still-running',"
            "'J-RECONCILE-PENDING',1,'seedance','generate',?,0)",
            (json.dumps({"status": "action_required"}),),
        )
        self.database.commit()

        with mock.patch(
            "factory.management_api.seedance.inspect_task",
            new=mock.AsyncMock(return_value={"status": "pending"}),
        ):
            result = self.service.reconcile_provider_task(
                "J-RECONCILE-PENDING", "seedance-still-running"
            )

        self.assertEqual(result["state"], "PROVIDER_ACTION_REQUIRED")
        self.assertEqual(self.database.execute(
            "SELECT state FROM jobs WHERE job_id='J-RECONCILE-PENDING'"
        ).fetchone()["state"], "PROVIDER_ACTION_REQUIRED")

    def test_reconcile_seedance_db_failure_does_not_replace_existing_candidate(self):
        job_id = "J-RECONCILE-SEEDANCE-ROLLBACK"
        task_id = "seedance-rollback-task"
        candidate = (
            self.root / "data" / "assets" / job_id / "shots" / "review"
            / f"01-reconciled-{task_id[-12:]}.mp4"
        )
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"prior-candidate")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,error) "
            "VALUES(?,?,?,'PROVIDER_ACTION_REQUIRED','{}',0,'unknown')",
            (job_id, "T-1", "seedance rollback"),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents,error) "
            "VALUES(?,1,'FAILED',0,'unknown')",
            (job_id,),
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES(?, ?, 1, 'seedance', 'generate', ?, 0)",
            (task_id, job_id, json.dumps({"status": "action_required"})),
        )
        self.database.execute(
            "CREATE TRIGGER fail_seedance_reconcile BEFORE UPDATE OF state ON shots "
            "WHEN NEW.job_id='J-RECONCILE-SEEDANCE-ROLLBACK' "
            "AND NEW.state='REVIEW_REQUIRED' "
            "BEGIN SELECT RAISE(ABORT,'seedance reconcile db failed'); END"
        )
        self.database.commit()
        inspected = {
            "status": "completed", "video": b"new-remote-candidate",
            "video_url": "https://cdn/new.mp4", "usage": {}, "cost_cents": 700,
        }

        with mock.patch(
            "factory.management_api.seedance.inspect_task",
            new=mock.AsyncMock(return_value=inspected),
        ), self.assertRaisesRegex(
            sqlite3.IntegrityError, "seedance reconcile db failed"
        ):
            self.service.reconcile_provider_task(job_id, task_id)

        self.assertEqual(candidate.read_bytes(), b"prior-candidate")
        self.assertEqual(self.database.execute(
            "SELECT state,cost_cents FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()[:], ("PROVIDER_ACTION_REQUIRED", 0))
        self.assertEqual(json.loads(self.database.execute(
            "SELECT usage_json FROM provider_calls WHERE task_id=?", (task_id,)
        ).fetchone()[0])["status"], "action_required")

    def test_reconcile_suno_db_failure_does_not_replace_existing_bgm(self):
        job_id = "J-RECONCILE-SUNO-ROLLBACK"
        task_id = "suno-rollback-task"
        bgm = self.root / "data" / "assets" / job_id / "bgm.mp3"
        bgm.parent.mkdir(parents=True)
        bgm.write_bytes(b"prior-bgm")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,bgm_path,"
            "cost_cents,error) VALUES(?,?,?,'PROVIDER_ACTION_REQUIRED','{}',?,0,'unknown')",
            (job_id, "T-1", "suno rollback", str(bgm)),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state) VALUES(?,1,'OK')", (job_id,)
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES(?, ?, 0, 'suno', 'bgm', ?, 0)",
            (task_id, job_id, json.dumps({"status": "action_required"})),
        )
        self.database.execute(
            "CREATE TRIGGER fail_suno_reconcile BEFORE UPDATE OF bgm_path ON jobs "
            "WHEN NEW.job_id='J-RECONCILE-SUNO-ROLLBACK' "
            "BEGIN SELECT RAISE(ABORT,'suno reconcile db failed'); END"
        )
        self.database.commit()
        inspected = {
            "status": "completed", "audio": b"new-remote-bgm",
            "usage": {}, "cost_cents": 300,
        }

        with mock.patch(
            "factory.management_api.music.inspect_task",
            new=mock.AsyncMock(return_value=inspected),
        ), self.assertRaisesRegex(
            sqlite3.IntegrityError, "suno reconcile db failed"
        ):
            self.service.reconcile_provider_task(job_id, task_id)

        self.assertEqual(bgm.read_bytes(), b"prior-bgm")
        self.assertEqual(self.database.execute(
            "SELECT state,cost_cents FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()[:], ("PROVIDER_ACTION_REQUIRED", 0))
        self.assertEqual(json.loads(self.database.execute(
            "SELECT usage_json FROM provider_calls WHERE task_id=?", (task_id,)
        ).fetchone()[0])["status"], "action_required")

    def test_reconcile_publish_failure_is_retryable_without_remote_reinspection(self):
        for provider in ("seedance", "suno"):
            with self.subTest(provider=provider):
                job_id = f"J-RECONCILE-PUBLISH-{provider.upper()}"
                task_id = f"{provider}-publish-retry-task"
                asset_root = self.root / "data" / "assets" / job_id
                if provider == "seedance":
                    target = (
                        asset_root / "shots" / "review"
                        / f"01-reconciled-{task_id[-12:]}.mp4"
                    )
                    inspected = {
                        "status": "completed", "video": b"new-candidate",
                        "video_url": "https://cdn/retry.mp4",
                        "usage": {}, "cost_cents": 700,
                    }
                    shot_state = "FAILED"
                    shot_idx = 1
                else:
                    target = asset_root / "bgm.mp3"
                    inspected = {
                        "status": "completed", "audio": b"new-bgm",
                        "usage": {}, "cost_cents": 300,
                    }
                    shot_state = "OK"
                    shot_idx = 0
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"prior-asset")
                self.database.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,bgm_path,"
                    "cost_cents,error) VALUES(?,?,?,'PROVIDER_ACTION_REQUIRED','{}',?,0,'unknown')",
                    (
                        job_id, "T-1", "publish retry",
                        str(target) if provider == "suno" else None,
                    ),
                )
                self.database.execute(
                    "INSERT INTO shots(job_id,idx,state,cost_cents,error) "
                    "VALUES(?,1,?,0,'unknown')",
                    (job_id, shot_state),
                )
                self.database.execute(
                    "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,"
                    "intent,usage_json,cost_cents) VALUES(?,?,?,?,?,?,0)",
                    (
                        task_id, job_id, shot_idx, provider,
                        "generate" if provider == "seedance" else "bgm",
                        json.dumps({"status": "action_required"}),
                    ),
                )
                self.database.commit()
                inspect_target = (
                    "factory.management_api.seedance.inspect_task"
                    if provider == "seedance"
                    else "factory.management_api.music.inspect_task"
                )
                inspect = mock.AsyncMock(return_value=inspected)
                real_replace = type(target).replace
                publish_failed = False

                def fail_publish(path, destination):
                    nonlocal publish_failed
                    if Path(destination).name == target.name and not publish_failed:
                        publish_failed = True
                        raise OSError("atomic publish failed")
                    return real_replace(path, destination)

                with mock.patch(inspect_target, new=inspect), mock.patch.object(
                    type(target), "replace", new=fail_publish
                ), self.assertRaisesRegex(OSError, "atomic publish failed"):
                    self.service.reconcile_provider_task(job_id, task_id)

                self.assertEqual(target.read_bytes(), b"prior-asset")
                paused_job = self.database.execute(
                    "SELECT state,cost_cents,error FROM jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                self.assertEqual(paused_job[:2], (
                    "PROVIDER_ACTION_REQUIRED", inspected["cost_cents"]
                ))
                self.assertIn("等待原子发布", paused_job["error"])
                usage = json.loads(self.database.execute(
                    "SELECT usage_json FROM provider_calls WHERE task_id=?",
                    (task_id,),
                ).fetchone()[0])
                self.assertEqual(usage["status"], "action_required")
                self.assertTrue(Path(usage["publish_staging_path"]).is_file())

                with mock.patch(inspect_target, new=inspect):
                    recovered = self.service.reconcile_provider_task(
                        job_id, task_id
                    )

                self.assertEqual(target.read_bytes(), (
                    b"new-candidate" if provider == "seedance" else b"new-bgm"
                ))
                self.assertNotEqual(recovered["state"], "PROVIDER_ACTION_REQUIRED")
                self.assertEqual(inspect.await_count, 1)
                self.assertEqual(self.database.execute(
                    "SELECT cost_cents FROM jobs WHERE job_id=?", (job_id,)
                ).fetchone()[0], inspected["cost_cents"])

    def test_reconcile_journal_replay_recomputes_state_after_other_task_resolves(self):
        job_id = "J-RECONCILE-JOURNAL-ORDER"
        task_a = "seedance-journal-a"
        task_b = "seedance-journal-b"
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,error) "
            "VALUES(?,?,?,'PROVIDER_ACTION_REQUIRED','{}',0,'unknown')",
            (job_id, "T-1", "journal order"),
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,state,cost_cents,error) "
            "VALUES(?,?,'FAILED',0,'unknown')",
            [(job_id, 1), (job_id, 2)],
        )
        self.database.executemany(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES(?,?,?,'seedance','generate',?,0)",
            [
                (task_a, job_id, 1, json.dumps({"status": "action_required"})),
                (task_b, job_id, 2, json.dumps({"status": "action_required"})),
            ],
        )
        self.database.commit()
        inspections = {
            task_a: {
                "status": "completed", "video": b"candidate-a",
                "video_url": "https://cdn/a.mp4", "usage": {},
                "cost_cents": 700,
            },
            task_b: {"status": "failed", "usage": {}, "cost_cents": 200},
        }

        async def inspect(task_id):
            return inspections[task_id]

        inspector = mock.AsyncMock(side_effect=inspect)
        real_replace = Path.replace
        failed_once = False

        def fail_a_publish_once(path, destination):
            nonlocal failed_once
            if Path(destination).suffix == ".mp4" and not failed_once:
                failed_once = True
                raise OSError("publish A failed")
            return real_replace(path, destination)

        with mock.patch(
            "factory.management_api.seedance.inspect_task", new=inspector
        ), mock.patch.object(
            Path, "replace", new=fail_a_publish_once
        ), self.assertRaisesRegex(OSError, "publish A failed"):
            self.service.reconcile_provider_task(job_id, task_a)

        with mock.patch(
            "factory.management_api.seedance.inspect_task", new=inspector
        ):
            resolved_b = self.service.reconcile_provider_task(job_id, task_b)
            self.assertEqual(resolved_b["state"], "PROVIDER_ACTION_REQUIRED")
            replayed_a = self.service.reconcile_provider_task(job_id, task_a)

        self.assertEqual(replayed_a["state"], "PRODUCTION_REVIEW_REQUIRED")
        self.assertEqual(inspector.await_count, 2)
        self.assertEqual(provider_tasks.unresolved_task_ids(
            self.database, job_id
        ), [])
        self.assertEqual(self.database.execute(
            "SELECT state,cost_cents FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()[:], ("PRODUCTION_REVIEW_REQUIRED", 900))

    def test_reconcile_success_ignores_staging_cleanup_permission_error(self):
        job_id = "J-RECONCILE-CLEANUP-PERMISSION"
        task_id = "seedance-cleanup-permission"
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,error) "
            "VALUES(?,?,?,'PROVIDER_ACTION_REQUIRED','{}',0,'unknown')",
            (job_id, "T-1", "cleanup permission"),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents,error) "
            "VALUES(?,1,'FAILED',0,'unknown')",
            (job_id,),
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES(?,?,1,'seedance','generate',?,0)",
            (task_id, job_id, json.dumps({"status": "action_required"})),
        )
        self.database.commit()
        inspected = {
            "status": "completed", "video": b"cleanup-video",
            "video_url": "https://cdn/cleanup.mp4", "usage": {},
            "cost_cents": 700,
        }
        real_unlink = Path.unlink

        def deny_stage_cleanup(path, *args, **kwargs):
            if Path(path).name.endswith(".stage"):
                raise PermissionError("staging cleanup denied")
            return real_unlink(path, *args, **kwargs)

        with mock.patch(
            "factory.management_api.seedance.inspect_task",
            new=mock.AsyncMock(return_value=inspected),
        ), mock.patch.object(Path, "unlink", new=deny_stage_cleanup):
            result = self.service.reconcile_provider_task(job_id, task_id)

        self.assertEqual(result["state"], "PRODUCTION_REVIEW_REQUIRED")
        self.assertEqual(self.database.execute(
            "SELECT state FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()[0], "PRODUCTION_REVIEW_REQUIRED")
        self.assertEqual(json.loads(self.database.execute(
            "SELECT usage_json FROM provider_calls WHERE task_id=?", (task_id,)
        ).fetchone()[0])["status"], "completed")

    def test_reconcile_second_phase_db_failure_replays_journal_without_inspect(self):
        for provider in ("seedance", "suno"):
            with self.subTest(provider=provider):
                job_id = f"J-RECONCILE-FINALIZE-{provider.upper()}"
                task_id = f"{provider}-finalize-db-retry"
                asset_root = self.root / "data" / "assets" / job_id
                if provider == "seedance":
                    target = (
                        asset_root / "shots" / "review"
                        / f"01-reconciled-{task_id[-12:]}.mp4"
                    )
                    inspected = {
                        "status": "completed", "video": b"finalize-video",
                        "video_url": "https://cdn/finalize.mp4", "usage": {},
                        "cost_cents": 700,
                    }
                    shot_state = "FAILED"
                    shot_idx = 1
                else:
                    target = asset_root / "bgm.mp3"
                    inspected = {
                        "status": "completed", "audio": b"finalize-audio",
                        "usage": {}, "cost_cents": 300,
                    }
                    shot_state = "OK"
                    shot_idx = 0
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"prior")
                self.database.execute(
                    "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,bgm_path,"
                    "cost_cents,error) VALUES(?,?,?,'PROVIDER_ACTION_REQUIRED','{}',?,0,'unknown')",
                    (
                        job_id, "T-1", "finalize db retry",
                        str(target) if provider == "suno" else None,
                    ),
                )
                self.database.execute(
                    "INSERT INTO shots(job_id,idx,state,cost_cents,error) "
                    "VALUES(?,1,?,0,'unknown')",
                    (job_id, shot_state),
                )
                self.database.execute(
                    "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,"
                    "intent,usage_json,cost_cents) VALUES(?,?,?,?,?,?,0)",
                    (
                        task_id, job_id, shot_idx, provider,
                        "generate" if provider == "seedance" else "bgm",
                        json.dumps({"status": "action_required"}),
                    ),
                )
                self.database.execute(
                    f"CREATE TRIGGER fail_{provider}_publish_finalize "
                    "BEFORE UPDATE OF state ON jobs "
                    f"WHEN NEW.job_id='{job_id}' "
                    "AND OLD.state='PROVIDER_ACTION_REQUIRED' "
                    "AND NEW.state!='PROVIDER_ACTION_REQUIRED' "
                    "BEGIN SELECT RAISE(ABORT,'publish finalize db failed'); END"
                )
                self.database.commit()
                inspect_target = (
                    "factory.management_api.seedance.inspect_task"
                    if provider == "seedance"
                    else "factory.management_api.music.inspect_task"
                )
                inspector = mock.AsyncMock(return_value=inspected)

                with mock.patch(
                    inspect_target, new=inspector
                ), self.assertRaisesRegex(
                    sqlite3.IntegrityError, "publish finalize db failed"
                ):
                    self.service.reconcile_provider_task(job_id, task_id)

                self.assertNotEqual(target.read_bytes(), b"prior")
                usage = json.loads(self.database.execute(
                    "SELECT usage_json FROM provider_calls WHERE task_id=?",
                    (task_id,),
                ).fetchone()[0])
                self.assertEqual(usage["status"], "action_required")
                self.assertTrue(Path(usage["publish_staging_path"]).is_file())
                self.database.execute(f"DROP TRIGGER fail_{provider}_publish_finalize")
                self.database.commit()

                with mock.patch(inspect_target, new=inspector):
                    recovered = self.service.reconcile_provider_task(
                        job_id, task_id
                    )

                self.assertNotEqual(recovered["state"], "PROVIDER_ACTION_REQUIRED")
                self.assertEqual(inspector.await_count, 1)
                self.assertEqual(self.database.execute(
                    "SELECT cost_cents FROM jobs WHERE job_id=?", (job_id,)
                ).fetchone()[0], inspected["cost_cents"])

    def test_reconcile_one_of_multiple_tasks_keeps_job_paused_until_all_resolved(self):
        job_id = "J-RECONCILE-MULTI"
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,cost_cents,error) "
            "VALUES(?,?,?,'PROVIDER_ACTION_REQUIRED','{}',0,'unknown')",
            (job_id, "T-1", "multiple remote tasks"),
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,state,cost_cents,error) "
            "VALUES(?,?,'FAILED',0,'unknown')",
            [(job_id, 1), (job_id, 2)],
        )
        self.database.executemany(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES(?, ?, ?, 'seedance', 'generate', ?, 0)",
            [
                ("seedance-multi-1", job_id, 1,
                 json.dumps({"status": "action_required"})),
                ("seedance-multi-2", job_id, 2,
                 json.dumps({"status": "pending"})),
            ],
        )
        self.database.commit()
        inspections = {
            "seedance-multi-1": {
                "status": "completed", "video": b"candidate-1",
                "video_url": "https://cdn/1.mp4", "usage": {}, "cost_cents": 700,
            },
            "seedance-multi-2": {
                "status": "failed", "usage": {}, "cost_cents": 200,
            },
        }

        async def inspect(task_id):
            return inspections[task_id]

        with mock.patch(
            "factory.management_api.seedance.inspect_task",
            new=mock.AsyncMock(side_effect=inspect),
        ), mock.patch(
            "factory.management_api.seedance.text_to_video",
            new=mock.AsyncMock(),
        ) as submit:
            first = self.service.reconcile_provider_task(
                job_id, "seedance-multi-1"
            )
            self.assertEqual(first["state"], "PROVIDER_ACTION_REQUIRED")
            replay = self.service.reconcile_provider_task(
                job_id, "seedance-multi-1"
            )
            self.assertEqual(replay["cost_delta_cents"], 0)
            self.assertEqual(self.database.execute(
                "SELECT cost_cents FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()[0], 700)
            second = self.service.reconcile_provider_task(
                job_id, "seedance-multi-2"
            )

        submit.assert_not_awaited()
        self.assertEqual(second["state"], "PRODUCTION_REVIEW_REQUIRED")
        self.assertEqual(self.database.execute(
            "SELECT state,cost_cents FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()[:], ("PRODUCTION_REVIEW_REQUIRED", 900))
        self.assertEqual(self.database.execute(
            "SELECT state FROM shots WHERE job_id=? AND idx=1", (job_id,)
        ).fetchone()[0], "REVIEW_REQUIRED")
        self.assertEqual(self.database.execute(
            "SELECT state FROM shots WHERE job_id=? AND idx=2", (job_id,)
        ).fetchone()[0], "PENDING")


class ManagementUiContractTests(unittest.TestCase):
    def test_video_drawer_consumes_backend_next_action_as_its_only_primary_action(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "const videoNextActionLabel=job=>job.next_action?.label||'查看任务详情'",
            js,
        )
        self.assertIn('id="drawer-next-action"', js)
        self.assertIn("function runVideoNextAction(job)", js)
        self.assertIn("action==='configure_music_provider'", js)
        self.assertIn("pendingMusicResumeJobId", js)
        self.assertIn("音乐配置已生效，本地成片已完成", js)
        self.assertNotIn('id="drawer-resume-video"', js)
        self.assertNotIn('id="drawer-produce"', js)
        self.assertNotIn('id="drawer-review-approve"', js)
        self.assertNotIn('id="drawer-submit-publish"', js)
        self.assertNotIn('id="drawer-business-pause-action"', js)

    def test_ui_exposes_seedance_lip_sync_repair_without_sacrificing_performance(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("lip_sync_repair_eligible", js)
        self.assertIn("data-lip-sync-repair", js)
        self.assertIn("confirmLipSyncRepair", js)
        self.assertIn("/lip-sync-repair", js)
        self.assertIn("局部口型修复", js)
        self.assertIn("保留原生语音、人物表演和镜头运动", js)
        self.assertIn("repair_start_sec", js)
        self.assertIn("repair_end_sec", js)
        self.assertIn("lip_sync_repair_suggestion", js)
        self.assertIn("AI 建议区间", js)
        self.assertIn("suggestion.start_sec", js)
        self.assertIn("suggestion.end_sec", js)
        self.assertIn("data-candidate-lip-sync-repair", js)
        self.assertIn("无需先接受口型不合格候选", js)

    def test_ui_exposes_managed_master_extension_as_review_candidate(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("extension_eligible", js)
        self.assertIn("data-extend-master", js)
        self.assertIn("confirmMasterExtension", js)
        self.assertIn("/extend", js)
        self.assertIn("延长 4–30 秒", js)
        self.assertIn("原母片保持不变", js)
        self.assertIn("extension_sec", js)
        self.assertIn("direction", js)

    def test_ui_exposes_configured_and_learned_master_capacity(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("master_capacity", js)
        self.assertIn("effective_limit", js)
        self.assertIn("configured_limit", js)
        self.assertIn("母片并发", js)
        self.assertIn("限流后自动降档", js)

    def test_ui_exposes_get_only_provider_recovery_status_and_manual_sweep(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("provider_recovery", js)
        self.assertIn("/api/production/reconcile", js)
        self.assertIn("run-provider-reconcile", js)
        self.assertIn("checked", js)
        self.assertIn("pending", js)

    def test_ui_requires_explicit_confirmation_for_high_risk_long_masters(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("requires_long_master_confirmation", js)
        self.assertIn("high-risk-master-confirm", js)
        self.assertIn("high_risk_confirmed", js)
        self.assertIn("长母片质量风险", js)

    def test_optimized_non_concurrency_controls_are_exposed_inline(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("setting-anchor-review-mode", js)
        self.assertIn("自动验收锚点", js)
        self.assertIn("生成后人工确认（推荐）", js)
        self.assertIn("current.anchor_review_mode||'manual'", js)
        self.assertIn("ASR 未配置时自动使用估算字幕", js)
        self.assertIn("approved_budget_cents", js)
        self.assertIn("确认新预算并开始生成", js)
        self.assertIn("review-check-all", js)
        self.assertIn("一键勾选全部六项", js)
        self.assertIn("1–24 张", js)
        self.assertNotIn("建议总时长需在 15–72 秒之间", js)
        self.assertNotIn("建议分镜数需在 4–8 个之间", js)
    def test_preflight_exposes_the_frozen_master_dependency_plan(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("master_dependency_graph", js)
        self.assertIn("母片依赖调度", js)
        self.assertIn("并发波次", js)

    def test_non_reconcilable_provider_task_has_no_posting_button(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("task.reconcilable===false", js)
        self.assertIn("task.action_message", js)
        self.assertIn("人工处置", js)
        self.assertIn("data-provider-task", js)

    def test_rejected_pending_reconciliation_has_distinct_terminal_ui_copy(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "REJECTED_PENDING_RECONCILIATION:'已终止，待成本核对'", js
        )
        self.assertIn(
            "'PRODUCTION_REVIEW_REQUIRED','PROVIDER_ACTION_REQUIRED','REJECTED_PENDING_RECONCILIATION'",
            js,
        )
        self.assertIn("已终止任务的成本已核对", js)
        self.assertIn(
            "['PROVIDER_ACTION_REQUIRED','REJECTED_PENDING_RECONCILIATION'].includes(job?.state))$('#drawer-delete-video')?.remove()",
            js,
        )

    def test_provider_action_review_result_has_distinct_ui_branch_and_todo(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("result.state==='PROVIDER_ACTION_REQUIRED'", js)
        self.assertIn("provider_action:'需模型核对'", js)
        self.assertIn("存在待核对远端任务", js)
        provider_branch = js.index("result.state==='PROVIDER_ACTION_REQUIRED'")
        recomposed_branch = js.index("result.state==='COMPOSED'", provider_branch)
        self.assertLess(provider_branch, recomposed_branch)
    def test_rejected_video_is_terminal_not_resumable_in_ui_contract(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("REJECTED:'已人工终止'", js)
        self.assertIn("new Set(['FAILED','BLOCKED','REJECTED'])", js)
        self.assertNotIn("job.state==='REJECTED'?'<button", js)

    def test_business_pause_states_have_unique_labels_actions_and_filters(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("review_master_candidates", js)
        self.assertIn("view_provider_account_guide", js)
        self.assertIn("filter==='REVIEW_REQUIRED'?jobNeedsManualReview(job)", js)
        self.assertIn("job.state==='PRODUCTION_REVIEW_REQUIRED'", js)
        self.assertIn(
            "const exceptionalVideoStates=new Set(['FAILED','BLOCKED','REJECTED'])",
            js,
        )
        active_line = next(
            line for line in js.splitlines()
            if line.startswith("const activeProductionStates=")
        )
        self.assertNotIn("PRODUCTION_REVIEW_REQUIRED", active_line)
        self.assertNotIn("PROVIDER_ACTION_REQUIRED", active_line)

    def test_video_master_progress_uses_chinese_runtime_states(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("const masterStateLabel", js)
        self.assertIn("GENERATING:'生成中'", js)
        self.assertIn("PENDING:'等待生成'", js)
        self.assertIn("masterStateLabel(shot.state)", js)

    def test_anchor_review_stage_has_explicit_preview_and_resume_action(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        server = (ROOT / "scripts" / "management_server.py").read_text(
            encoding="utf-8"
        )
        for capability in (
            "ANCHOR_REVIEW_REQUIRED", "review_continuity_anchors",
            "/anchors/approve", "approval_state",
        ):
            self.assertIn(capability, js)
        self.assertIn('endswith("/anchors/approve")', server)

    def test_video_detail_and_preflight_render_scene_anchor_assets(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        css = (ROOT / "scripts" / "management_ui" / "styles.css").read_text(
            encoding="utf-8"
        )
        server = (ROOT / "scripts" / "management_server.py").read_text(
            encoding="utf-8"
        )

        for capability in (
            "sceneAnchorCards", "场景连续性锚点", "关联母片",
            "scene_anchor_plan", "paid_scene_anchor_count",
            "本任务没有跨母片重复的同一物理场景",
            "人物一致性仍由角色定妆锚点保障",
            "项目现场连续性锚点",
            "固定站位",
        ):
            self.assertIn(capability, js)
        self.assertNotIn("本任务没有跨母片重复的核心空间", js)
        self.assertGreaterEqual(js.count("sceneAnchorCards(job)"), 2)
        self.assertIn("scene-anchor-card", css)
        self.assertIn("scene-blocking-stage", js)
        self.assertIn("scene-position", js)
        self.assertIn("scene-blocking-stage", css)
        self.assertNotIn("grid-row:1/span 99", css)
        self.assertIn('and "/scenes/" in parsed.path', server)

    def test_anchor_generators_are_selectable_globally_and_per_video(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        for capability in (
            "CHARACTER_ANCHOR_GENERATOR", "SCENE_ANCHOR_GENERATOR",
            "setting-character-anchor-generator",
            "setting-scene-anchor-generator",
            "Seedream 5.0 · 静态锚点图",
            "Seedance 2.5 · 动态母片抽帧",
            "character_anchor_generator", "scene_anchor_generator",
        ):
            self.assertIn(capability, js)
        self.assertIn(
            "configValue('CHARACTER_ANCHOR_GENERATOR')||'seedance_2_5'", js
        )
        self.assertIn(
            "configValue('SCENE_ANCHOR_GENERATOR')||'seedance_2_5'", js
        )

    def test_reference_attention_budget_is_visible_in_configuration(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "SEEDANCE_PRODUCTION_REFERENCE_VIDEO_BUDGET", js
        )
        self.assertIn("参考视频注意力预算", js)

    def test_script_editor_can_auto_reestimate_durations_with_manual_override(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        for capability in (
            "edit-script-auto-duration", "自动重估分镜时长", "预计总时长",
            "estimateDialogueDuration", "duration_mode", "auto_adjust_durations",
            "可关闭自动调整后手工填写",
        ):
            self.assertIn(capability, js)
        self.assertIn(
            'data-duration-input type="number" min="1.5" step="0.5"', js
        )
        self.assertNotIn(
            'data-duration-input type="number" min="1.5" max=', js
        )

    def test_script_generation_modal_offers_optional_creative_guidance(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        for capability in (
            "建议总时长", "建议分镜数", "建议角色数", "留空由 AI 决定",
            "script-suggest-duration", "script-suggest-shots",
            "script-suggest-characters", "suggested_duration_sec",
            "suggested_shot_count", "suggested_character_count",
        ):
            self.assertIn(capability, js)

    def test_video_workbench_shows_business_queue_health_without_worker_pid(self):
        html = (ROOT / "scripts" / "management_ui" / "index.html").read_text(
            encoding="utf-8"
        )
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="production-health"', html)
        self.assertIn("/api/production/health", js)
        self.assertIn("排队中", js)
        self.assertIn("监督器", js)
        self.assertNotIn("production_worker_pid", js)

    def test_character_library_is_an_independent_manageable_workspace(self):
        ui_root = ROOT / "scripts" / "management_ui"
        html = (ui_root / "index.html").read_text(encoding="utf-8")
        js = (ui_root / "app.js").read_text(encoding="utf-8")

        self.assertIn('data-page="characters"', html)
        self.assertIn('id="page-characters"', html)
        self.assertIn('id="character-search"', html)
        self.assertIn('id="character-type-filter"', html)
        self.assertIn('id="character-status-filter"', html)
        self.assertIn("openCharacterDrawer", js)
        self.assertIn("deleteCharacter", js)
        self.assertIn("refreshCharacter", js)
        self.assertIn("data-character-refresh", js)
        self.assertIn("background_description", js)
        self.assertIn("editCharacterBackground", js)
        self.assertNotIn('id="character-list"', html)

    def test_video_settings_render_per_speaker_character_mapping(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("character_mappings", js)
        self.assertIn("AI 按剧本人物自动生成（推荐）", js)
        self.assertIn("未绑定角色由 AI 先生成独立身份母片并提取定妆照", js)
        self.assertIn("人工模式会暂停等待确认", js)
        self.assertNotIn("请先为这些剧本人物指定角色", js)
        self.assertNotIn("保存角色映射并进入预检", js)
        self.assertIn("data-speaker-mapping", js)
        self.assertIn("剧本人物", js)
        self.assertIn("beat.speaker", js)

    def test_video_settings_include_all_speakers_inside_one_dialogue_master(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("master.speakers", js)
        self.assertIn("master.performance_beats", js)
        self.assertIn("characterMappingSummary", js)
        self.assertIn("customCharacterSeedanceNotice", js)
        self.assertIn("seedance_reference_mode", js)
        self.assertIn("多图定妆 · 描述驱动", js)
        self.assertIn("本地图片仅用于角色资产管理和人物设定", js)
        self.assertIn("先生成合规的身份母片与定妆帧", js)
        self.assertIn("人物形象方案", js)

    def test_delete_video_reports_automatic_queue_progress(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("dispatched_jobs", js)
        self.assertIn("已自动开始", js)

    def test_video_in_progress_tab_groups_every_active_production_state(self):
        ui_root = ROOT / "scripts" / "management_ui"
        html = (ui_root / "index.html").read_text(encoding="utf-8")
        js = (ui_root / "app.js").read_text(encoding="utf-8")

        self.assertIn('data-state="in_progress">生成中', html)
        self.assertIn("videoMatchesFilter", js)
        for state in (
            "PENDING",
            "PRODUCTION_QUEUED",
            "PRODUCTION_RUNNING",
            "SHOTS_OK",
            "VOICE_OK",
            "BGM_OK",
        ):
            self.assertIn(f"'{state}'", js)

    def test_manual_review_has_its_own_dashboard_queue_and_video_filter(self):
        ui_root = ROOT / "scripts" / "management_ui"
        html = (ui_root / "index.html").read_text(encoding="utf-8")
        js = (ui_root / "app.js").read_text(encoding="utf-8")

        self.assertIn('data-state="REVIEW_REQUIRED">待复核', html)
        self.assertIn("filter==='REVIEW_REQUIRED'", js)
        self.assertIn(
            "job.shots?.some(shot=>shot.manual_review_required)", js
        )
        self.assertIn("o.videos.review_required", js)
        self.assertIn("待人工复核", js)
        self.assertIn("其他异常", js)
        self.assertIn('data-global-filter="video-review"', html)
        self.assertIn('id="global-video-review-count"', html)
        self.assertIn("global-video-review-count", js)
        self.assertIn("const reviewJobs=", js)
        self.assertIn(
            "'video-review':()=>reviewJobs.length?openVideoDrawer(reviewJobs[0])",
            js,
        )
        self.assertIn("button.dataset.state===state.videoFilter", js)
        self.assertIn(
            ".action-items{grid-template-columns:repeat(4,1fr)}",
            (ui_root / "styles.css").read_text(encoding="utf-8"),
        )

    def test_recent_business_events_open_their_related_record(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("openRecentEvent", js)
        self.assertIn("data-event-id", js)
        self.assertIn("event.draft_id", js)
        self.assertIn("event.publication_id", js)
        self.assertIn("await navigate('scripts')", js)
        self.assertIn("await navigate('videos')", js)
        self.assertIn("await navigate('publishing')", js)
        self.assertIn("recentEvents:[]", js)
        self.assertIn("studio-project-events", js)
        self.assertIn("data-studio-event-id", js)

    def test_project_next_action_waits_for_loaded_data_before_opening_record(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        route = js[
            js.index("async function routeProjectAction") : js.index(
                "function renderScripts"
            )
        ]

        self.assertIn("async function routeProjectAction", js)
        self.assertIn("await navigate('scripts')", route)
        self.assertIn("await navigate('videos')", route)
        self.assertIn("openDraftDrawer", route)
        self.assertNotIn("setTimeout", route)

    def test_project_without_drafts_routes_to_brief_for_script_generation(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        route = js[
            js.index("async function routeProjectAction") : js.index(
                "function renderScripts"
            )
        ]

        self.assertIn("generate_scripts:'选择角度并生成剧本'", js)
        self.assertIn("if(project.next_action==='generate_scripts')", route)
        self.assertIn("const stayInWorkbench=state.page==='overview'", route)
        self.assertIn(
            "if(!stayInWorkbench&&!await navigate('scripts'))return", route
        )
        self.assertIn("state.scripts?.briefs", route)
        self.assertIn("openBriefDrawer", route)

    def test_global_project_picker_updates_workbench_before_async_refresh(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        renderer = js[
            js.index("function renderGlobalProjects") : js.index(
                "const renderGlobalProjectsBase"
            )
        ]

        self.assertIn(
            "state.studioStage=studioStageOrder[studioStageIndex(project.stage)];"
            "renderStudioProject(project);navigate('overview')",
            renderer,
        )

    def test_studio_publish_action_uses_the_loaded_project_route(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        render_stage = js[
            js.index("function renderStudioStage") : js.index(
                "function renderStudioInspector"
            )
        ]

        self.assertIn(
            "$('#studio-publish-action').onclick=()=>routeProjectAction(project)",
            render_stage,
        )
        self.assertNotIn("setTimeout", render_stage)

    def test_douyin_binding_waits_for_configuration_page_data(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        handler = js[js.index("const bind=$('#go-bind-douyin')") : js.index("const direct=")]

        self.assertIn("bind.onclick=async", handler)
        self.assertIn("await navigate('config')", handler)
        self.assertNotIn("setTimeout", handler)

    def test_publish_channel_config_opens_the_matching_platform_panel(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        publishing = js[
            js.index("function renderPublishing") : js.index(
                "function performanceSnapshotBlock"
            )
        ]

        self.assertIn('data-go-config="${p.id}"', publishing)
        self.assertIn("b.onclick=async", publishing)
        self.assertIn("if(!await navigate('config'))return", publishing)
        self.assertIn('b.dataset.goConfig', publishing)
        self.assertIn("panel.hidden=false", publishing)
        self.assertIn("panel.scrollIntoView", publishing)

    def test_character_usage_waits_for_video_page_data(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        handler = js[
            js.index("$$('[data-character-job]')") : js.index(
                "function openCharacterCreate"
            )
        ]

        self.assertIn("button.onclick=async", handler)
        self.assertIn("await navigate('videos')", handler)
        self.assertNotIn("setTimeout", handler)

    def test_new_project_opens_input_in_workbench_without_tab_switch(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        handler = js[js.index("const startNewProject=") : js.index("$('#global-new-project').onclick")]

        self.assertIn("const startNewProject=()=>", handler)
        self.assertIn("trigger.click()", handler)
        self.assertNotIn("navigate('scripts')", handler)
        self.assertNotIn("setTimeout", handler)

    def test_page_loader_ignores_stale_responses_and_returns_load_status(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        loader = js[js.index("async function loadPage") : js.index("function renderOverview")]

        self.assertIn("loadSequence:0", js)
        self.assertIn("const loadToken=++state.loadSequence", loader)
        self.assertIn("const stale=()=>", loader)
        self.assertIn("if(stale())return false", loader)
        self.assertIn("if(!stale())toast(e.message,true)", loader)
        self.assertIn("return true", loader)
        self.assertIn("return false", loader)
        self.assertIn(
            "const characterLoadToken=++state.characterLoadSequence", js
        )
        self.assertIn(
            "if(stale()||characterLoadToken!==state.characterLoadSequence)return false",
            js,
        )

    def test_record_openers_stop_when_target_page_loading_fails(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        recent = js[js.index("async function openRecentEvent") : js.index("function bindRecentEvents")]
        route = js[
            js.index("async function routeProjectAction") : js.index(
                "function renderScripts"
            )
        ]
        bind = js[js.index("const bind=$('#go-bind-douyin')") : js.index("const direct=")]
        character = js[
            js.index("$$('[data-character-job]')") : js.index(
                "function openCharacterCreate"
            )
        ]
        new_project = js[
            js.index("const startNewProject=") : js.index(
                "$('#global-new-project').onclick"
            )
        ]

        self.assertIn("const stayInWorkbench=state.page==='overview'", recent)
        self.assertGreaterEqual(
            recent.count("if(!stayInWorkbench&&!await navigate("), 3
        )
        self.assertIn("const stayInWorkbench=state.page==='overview'", route)
        self.assertGreaterEqual(
            route.count("if(!stayInWorkbench&&!await navigate("), 4
        )
        self.assertIn("if(!await navigate('config'))return", bind)
        self.assertIn("if(!await navigate('videos'))return", character)
        self.assertIn("trigger.click()", new_project)
        self.assertNotIn("navigate('scripts')", new_project)

    def test_post_mutation_followups_require_fresh_page_data(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertGreaterEqual(
            js.count("if(!await loadPage('scripts'))return"), 5
        )
        self.assertGreaterEqual(
            js.count("if(!await loadPage('videos'))return"), 5
        )
        self.assertNotIn("await loadPage('scripts');open", js)
        self.assertNotIn("await loadPage('scripts');toast", js)
        self.assertNotIn("await loadPage('videos');const fresh", js)
        self.assertNotIn("await loadPage('videos');openVideoDrawer", js)

    def test_character_loader_discards_stale_search_results_and_reports_failure(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        loader = js[js.index("async function loadCharacters") : js.index("function renderCharacters")]

        self.assertIn("characterLoadSequence:0", js)
        self.assertIn("const loadToken=++state.characterLoadSequence", loader)
        self.assertIn("const stale=()=>", loader)
        self.assertIn("if(stale())return false", loader)
        self.assertIn("if(!stale())toast(e.message,true)", loader)
        self.assertIn("return true", loader)
        self.assertIn("return false", loader)

    def test_character_mutations_wait_for_a_fresh_library_result(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("if(!await loadCharacters())return;openCharacterDrawer", js)
        self.assertGreaterEqual(js.count("await loadCharacters()"), 5)
        self.assertNotIn(";loadCharacters()}catch", js)

    def test_active_video_polling_is_scoped_and_does_not_reload_the_page(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        poller_start = js.find("async function pollActiveProduction")
        self.assertGreaterEqual(poller_start, 0)
        poller_end = js.find("function scheduleActiveProductionPoll", poller_start)
        self.assertGreater(poller_end, poller_start)
        poller = js[poller_start:poller_end]

        self.assertIn("openVideoJobId:''", js)
        self.assertIn("state.openVideoJobId=''", js)
        self.assertIn("state.openVideoJobId=job.job_id", js)
        self.assertIn("state.page!=='videos'", poller)
        self.assertIn("document.visibilityState!=='visible'", poller)
        self.assertIn("hasActiveProductionJobs()", poller)
        self.assertIn("api('/api/videos')", poller)
        self.assertIn("api('/api/production/health')", poller)
        self.assertIn("patchVideoProductionData", poller)
        self.assertNotIn("loadPage(", poller)
        self.assertNotIn("renderVideos()", poller)
        self.assertIn("drawerBody.scrollTop=scrollTop", js)
        self.assertIn("if(state.polling)return", poller)
        self.assertIn("setTimeout(pollActiveProduction,5000)", js)
        self.assertNotIn("setInterval(pollVisiblePage,5000)", js)

    def test_active_video_polling_patches_changed_rows_and_stops_for_static_pages(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        patcher_start = js.find("function patchVideoProductionData")
        self.assertGreaterEqual(patcher_start, 0)
        patcher_end = js.find("async function pollActiveProduction", patcher_start)
        self.assertGreater(patcher_end, patcher_start)
        patcher = js[patcher_start:patcher_end]

        self.assertIn("videoJobFingerprint", patcher)
        self.assertIn("row.innerHTML=videoRowContents(job)", patcher)
        self.assertIn("if(structureChanged)renderVideos()", patcher)
        self.assertIn("if(previousFingerprint===nextFingerprint)continue", patcher)
        self.assertIn("clearTimeout(state.productionPollTimer)", js)
        self.assertIn("if(state.page!=='videos'||!hasActiveProductionJobs())return", js)
        self.assertIn("document.addEventListener('visibilitychange'", js)

    def test_background_polling_excludes_human_waiting_and_unstarted_video_states(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        start = js.find("const autoPollProductionStates")
        self.assertGreaterEqual(start, 0)
        declaration = js[start : js.find(";", start) + 1]

        self.assertIn("'PRODUCTION_QUEUED'", declaration)
        self.assertIn("'PRODUCTION_RUNNING'", declaration)
        self.assertNotIn("'STORYBOARDED'", declaration)
        self.assertNotIn("'ANCHOR_REVIEW_REQUIRED'", declaration)
        self.assertNotIn("'PRODUCTION_PAUSED'", declaration)
        self.assertIn("autoPollProductionStates.has", js)

    def test_management_page_busts_cache_for_active_production_polling_bundle(self):
        html = (ROOT / "scripts" / "management_ui" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("/app.js?v=20260819-audio-mode-v1", html)

    def test_audio_mode_is_selectable_globally_and_per_video(self):
        html = (ROOT / "scripts" / "management_ui" / "index.html").read_text(
            encoding="utf-8"
        )
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("对白可选固定火山声线或 Seedance 原生声线", html)
        self.assertIn("DIALOGUE_AUDIO_MODE", js)
        self.assertIn('id="setting-audio-mode"', js)
        self.assertIn("audio_mode:$('#setting-audio-mode').value", js)
        self.assertIn("Seedance 原生声线", js)

    def test_budget_gate_requires_budget_increase_and_failed_job_can_resume(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("需要确认新的任务预算", js)
        self.assertIn("确认新预算并开始生成", js)
        self.assertIn("继续生成剩余母片", js)
        self.assertIn("/resume", js)
        self.assertIn("approved_budget_cents", js)
        self.assertIn("confirmProduction(fresh)", js)
        self.assertNotIn("openProductionSettings(fresh)", js)

    def test_video_drawer_uses_backend_resume_action_label(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="drawer-next-action"', js)
        self.assertIn("function runVideoNextAction(job)", js)

    def test_partial_production_is_in_progress_and_resumes_without_reconfiguration(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("job.partial_production", js)
        self.assertIn("待继续生成", js)
        self.assertIn("confirmProduction(job)", js)

    def test_character_library_distinguishes_real_and_virtual_portrait_assets(self):
        html = (ROOT / "scripts" / "management_ui" / "index.html").read_text(
            encoding="utf-8"
        )
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("角色资产库", html)
        self.assertIn('value="real_person"', js)
        self.assertIn('value="virtual_person"', js)
        self.assertIn("火山虚拟人像库", js)
        self.assertIn("identity_type", js)

    def test_ui_exposes_content_project_centered_modules(self):
        html = (ROOT / "scripts" / "management_ui" / "index.html").read_text(
            encoding="utf-8"
        )
        for label in ("内容项目", "内容与剧本", "视频制作", "发布与数据", "角色资产库", "配置中心"):
            self.assertIn(label, html)

    def test_unified_todo_center_is_an_independent_primary_page(self):
        ui_root = ROOT / "scripts" / "management_ui"
        html = (ui_root / "index.html").read_text(encoding="utf-8")
        js = (ui_root / "app.js").read_text(encoding="utf-8")
        css = (ui_root / "styles.css").read_text(encoding="utf-8")

        self.assertIn('data-page="todos"', html)
        self.assertIn('id="page-todos"', html)
        overview = html[
            html.index('id="page-overview"') : html.index('id="page-todos"')
        ]
        todo_page = html[
            html.index('id="page-todos"') : html.index('id="page-projects"')
        ]
        self.assertNotIn('id="action-queue"', overview)
        self.assertIn('id="action-queue"', todo_page)
        self.assertIn("todos:['UNIFIED TODO / 01','统一待办中心']", js)
        self.assertIn("if(page==='todos')", js)
        self.assertIn(
            "state.todos=todos;state.recentEvents=eventData.events||[];"
            "renderTodoCenter(todos,eventData)",
            js,
        )
        self.assertIn("todos:state.todos", js)
        self.assertIn('body[data-page="todos"] .global-stage-strip', css)

    def test_unified_todo_center_includes_clickable_business_activity(self):
        ui_root = ROOT / "scripts" / "management_ui"
        html = (ui_root / "index.html").read_text(encoding="utf-8")
        js = (ui_root / "app.js").read_text(encoding="utf-8")

        todo_page = html[
            html.index('id="page-todos"') : html.index('id="page-projects"')
        ]
        self.assertIn('id="todo-recent-events"', todo_page)
        self.assertIn("最新动态", todo_page)
        self.assertIn("业务里程碑，不展示模型调用与执行日志", todo_page)
        self.assertIn(
            "Promise.all([api('/api/todos'),api('/api/events?limit=12')])",
            js,
        )
        self.assertIn("function renderTodoRecentEvents", js)
        self.assertIn('data-todo-event-id="${esc(event.id)}"', js)
        self.assertIn("openRecentEvent(event)", js)

    def test_config_ui_prioritizes_required_setup_and_collapses_optional_fields(self):
        html = (ROOT / "scripts" / "management_ui" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("必要配置", html)
        self.assertIn("可选配置", html)
        self.assertIn("advanced-config", html)

    def test_ui_uses_local_ant_design_system_without_external_runtime(self):
        ui_root = ROOT / "scripts" / "management_ui"
        html = (ui_root / "index.html").read_text(encoding="utf-8")
        css = (ui_root / "styles.css").read_text(encoding="utf-8")
        self.assertIn('class="app-shell ant-layout"', html)
        self.assertIn('class="sidebar ant-layout-sider"', html)
        self.assertIn('class="topbar ant-layout-header"', html)
        self.assertIn("--ant-color-primary", css)
        self.assertIn("--ant-border-radius", css)
        self.assertNotIn("unpkg.com", html)
        self.assertNotIn("cdn.jsdelivr.net", html)

    def test_content_library_is_workbench_only_and_modules_use_readable_scale(self):
        css = (
            ROOT / "scripts" / "management_ui" / "styles.css"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'body:not([data-page="overview"]) .global-project-panel', css
        )
        self.assertIn(
            'body:not([data-page="overview"]) .global-stage-strip', css
        )
        self.assertIn(
            'body:not([data-page="overview"]) .module-workbench', css
        )
        self.assertIn(
            'body[data-page="overview"] .global-project-panel', css
        )
        self.assertIn(
            'body[data-page="overview"] .studio-reading', css
        )

    def test_long_source_drawer_uses_one_scroll_container(self):
        css = (
            ROOT / "scripts" / "management_ui" / "styles.css"
        ).read_text(encoding="utf-8")

        self.assertIn(
            ".drawer-body{flex:1;min-height:0;overflow-y:auto;overflow-x:hidden",
            css,
        )

    def test_workbench_source_canvas_scrolls_and_toasts_have_contrast(self):
        css = (
            ROOT / "scripts" / "management_ui" / "styles.css"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'body[data-page="overview"] .studio-canvas{display:grid;grid-template-rows:auto minmax(0,1fr);min-height:0;overflow:hidden}',
            css,
        )
        self.assertIn(
            'body[data-page="overview"] .studio-stage-body{min-height:0;overflow-y:auto',
            css,
        )
        self.assertIn('.toast{color:#fff;background:#35453a', css)
        self.assertIn('.toast.error{color:#fff;background:#8f3f3f', css)
        self.assertIn(
            ".source-content .reading-content{max-height:none;overflow:visible}",
            css,
        )

    def test_errors_are_summarized_with_persistent_technical_details(self):
        script = (
            ROOT / "scripts" / "management_ui" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn("function summarizeError", script)
        self.assertIn("查看技术详情", script)
        self.assertIn("lastTechnicalError", script)
        self.assertIn("火山方舟账号欠费", script)
        self.assertIn("充值后继续生成剩余母片", script)
        self.assertIn("母片已生成，自动质检未完成，需人工复核", script)

    def test_only_active_video_production_refreshes_without_manual_navigation(self):
        script = (
            ROOT / "scripts" / "management_ui" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn("state.page!=='videos'", script)
        self.assertIn("hasActiveProductionJobs()", script)
        self.assertNotIn(
            "['overview','todos','videos','publishing'].includes(state.page)", script
        )

    def test_video_preflight_explains_director_plan_and_broll_cost(self):
        script = (
            ROOT / "scripts" / "management_ui" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn("function directorPlanLabel", script)
        self.assertIn("导演方案", script)
        self.assertIn("场景 B-roll", script)

    def test_video_drawer_exposes_director_timeline_and_single_master_regeneration(self):
        ui_root = ROOT / "scripts" / "management_ui"
        script = (ui_root / "app.js").read_text(encoding="utf-8")
        styles = (ui_root / "styles.css").read_text(encoding="utf-8")
        server = (ROOT / "scripts" / "management_server.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("director_timeline", script)
        self.assertIn("导演时间线", script)
        self.assertIn("画面轨", script)
        self.assertIn("声音轨", script)
        self.assertIn("镜头衔接", script)
        self.assertIn("transition_type", script)
        self.assertIn("沿用原剪辑策略", script)
        self.assertIn("单独返修", script)
        self.assertIn("/regenerate", script)
        self.assertIn("director-timeline", styles)
        self.assertIn("prepare_master_regeneration", server)

    def test_cost_ui_distinguishes_tracked_and_unallocated_cost(self):
        script = (
            ROOT / "scripts" / "management_ui" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn("已归集调用明细", script)
        self.assertIn("未分解成本", script)

    def test_legacy_director_plan_has_safe_upgrade_action(self):
        script = (
            ROOT / "scripts" / "management_ui" / "app.js"
        ).read_text(encoding="utf-8")
        server = (
            ROOT / "scripts" / "management_server.py"
        ).read_text(encoding="utf-8")

        self.assertIn("升级新版导演方案", script)
        self.assertIn("director_plan_upgradeable", script)
        self.assertIn("/upgrade-director", script)
        self.assertIn("/upgrade-director", server)
        self.assertIn("upgrade_director_plan", server)

    def test_failed_video_exposes_safe_director_plan_rebuild(self):
        script = (
            ROOT / "scripts" / "management_ui" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn("重建可执行导演计划", script)
        self.assertIn("/rebuild-director", script)
        self.assertIn("历史成本保留", script)

    def test_modal_stays_interactive_when_opened_from_drawer(self):
        css = (
            ROOT / "scripts" / "management_ui" / "styles.css"
        ).read_text(encoding="utf-8")

        self.assertIn(".drawer-backdrop{position:fixed;inset:0;z-index:40", css)
        self.assertIn(".modal-backdrop{z-index:70}", css)
        self.assertIn(".toast{z-index:80}", css)

    def test_long_modal_owns_vertical_scroll_inside_the_viewport(self):
        css = (
            ROOT / "scripts" / "management_ui" / "styles.css"
        ).read_text(encoding="utf-8")

        self.assertIn(
            ".modal{max-height:calc(100dvh - 40px);display:grid;"
            "grid-template-rows:auto auto minmax(0,1fr);overflow:hidden}",
            css,
        )
        self.assertIn(
            "#modal-body{min-height:0;overflow-y:auto;overflow-x:hidden;"
            "overscroll-behavior:contain;scrollbar-gutter:stable}",
            css,
        )

    def test_master_quality_warning_is_not_rendered_as_failed(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("s.manual_review_required?'review':s.error?'failed':''", js)
        self.assertIn("待人工复核", js)

    def test_broll_semantic_review_reasons_have_clear_chinese_labels(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("qualityReasonLabel", js)
        self.assertIn("缺少行业主体或设备", js)
        self.assertIn("缺少环境建立镜头", js)
        self.assertIn("缺少有效业务动作", js)
        self.assertIn("无意义手部特写占比过高", js)

    def test_ui_exposes_review_context_and_exactly_three_primary_decisions(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("openMasterCandidateReview", js)
        self.assertIn("candidate_preview_url", js)
        self.assertIn("previous_master_preview_url", js)
        self.assertIn("人物定妆", js)
        self.assertIn("场景锚点", js)
        self.assertIn("上一条连续母片", js)
        self.assertIn("未提供", js)
        self.assertIn('data-candidate-decision="accept_candidate"', js)
        self.assertIn('data-candidate-decision="regenerate_master"', js)
        self.assertIn('data-candidate-decision="reject_job"', js)
        self.assertNotIn('data-candidate-decision="keep_prior"', js)
        self.assertIn("接受候选", js)
        self.assertIn("单条重做", js)
        self.assertIn("终止任务", js)

    def test_ui_warns_but_still_offers_accept_for_readable_text_candidate(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "quality_reasons?.includes('unexpected_readable_text')", js
        )
        self.assertIn("仍可人工接受", js)
        self.assertIn('data-candidate-decision="accept_candidate"', js)
        self.assertIn("查看技术详情", js)
        self.assertIn("/candidate-review", js)
        self.assertIn("remaining_review_count", js)
        self.assertIn("remaining_generation_count", js)
        self.assertIn("继续复核其余母片", js)
        self.assertIn("已接受候选，等待其余母片", js)

    def test_ui_prioritizes_resolved_human_decision_over_old_quality_reasons(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("qualityDecisionLabel", js)
        self.assertIn("人工已接受候选", js)
        self.assertIn("人工已保留旧版", js)
        self.assertIn("shot.human_decision", js)
        self.assertIn("shot.review_resolved", js)
        self.assertIn("shot.reviewed_at", js)

    def test_approved_brief_without_drafts_is_shown_as_retryable(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("生成失败，可重试", js)
        self.assertIn("生成未完成，点击重试", js)
        self.assertIn("重新生成三版剧本", js)

    def test_ui_exposes_operational_workspace_interactions(self):
        ui_root = ROOT / "scripts" / "management_ui"
        html = (ui_root / "index.html").read_text(encoding="utf-8")
        js = (ui_root / "app.js").read_text(encoding="utf-8")
        for interaction in (
            "action-queue",
            "script-stage-tabs",
            "video-search",
            "context-drawer",
        ):
            self.assertIn(interaction, html)
        self.assertIn("openVideoDrawer", js)
        self.assertIn("openSourceDrawer", js)
        self.assertIn("openPublicationDrawer", js)
        self.assertNotIn("command-palette", html)
        self.assertNotIn("command-panel", html)
        self.assertNotIn("Ctrl+K", html)
        self.assertNotIn('id="quick-create"', html)

    def test_video_drawer_exposes_confirmed_actual_and_variance_costs(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("确认预估", js)
        self.assertIn("实际累计", js)
        self.assertIn("成本偏差", js)
        self.assertIn("历史任务未保存确认预估", js)
        self.assertIn("job.cost_observation", js)

    def test_overview_exposes_cost_estimation_calibration_without_auto_repricing(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("o.videos.cost_estimation", js)
        self.assertIn("估算样本", js)
        self.assertIn("总体偏差", js)
        self.assertIn("平均绝对偏差", js)
        self.assertIn("样本不足，仅积累数据", js)
        self.assertIn("分辨率校准", js)
        self.assertIn("时长校准", js)
        self.assertIn("参考素材校准", js)
        self.assertIn("calibration.segments", js)
        self.assertNotIn("autoReprice", js)

    def test_unified_todo_no_longer_consumes_project_workbench_space(self):
        ui_root = ROOT / "scripts" / "management_ui"
        html = (ui_root / "index.html").read_text(encoding="utf-8")
        js = (ui_root / "app.js").read_text(encoding="utf-8")
        css = (ui_root / "styles.css").read_text(encoding="utf-8")

        overview = html.split('id="page-overview"', 1)[1].split(
            'id="page-todos"', 1
        )[0]
        todo_page = html.split('id="page-todos"', 1)[1].split(
            'id="page-projects"', 1
        )[0]
        self.assertNotIn('id="action-queue"', overview)
        self.assertIn('id="studio-workbench"', overview)
        self.assertIn('id="action-queue"', todo_page)
        self.assertNotIn("legacy.before(target)", js)
        self.assertIn(
            'body[data-page="overview"] #page-overview{display:block;', css
        )
        self.assertIn(
            'body[data-page="overview"] .studio-workbench{height:100%;', css
        )

    def test_ui_exposes_content_review_media_actions_and_channel_publishing(self):
        ui_root = ROOT / "scripts" / "management_ui"
        html = (ui_root / "index.html").read_text(encoding="utf-8")
        js = (ui_root / "app.js").read_text(encoding="utf-8")
        for capability in (
            "source-content",
            "brief-content",
            "script-content",
            "deleteScript",
            "deleteVideo",
            "submitForPublish",
            "video-preview",
            "videoCoverUrl",
            "video.poster=videoCoverUrl",
            "确认并推送抖音",
            "打开视频号助手",
            "登录状态由微信官方页面保管",
            "https://channels.weixin.qq.com/login",
            "publish-platform-config",
            "直连发布与结果回流",
            "recordPerformanceSnapshot",
            "累计绩效快照",
            "captured_at",
            "来源事实底稿",
            "编辑简报",
            "编辑剧本",
            "editBrief",
            "editScript",
        ):
            self.assertIn(capability, html + js)
        self.assertNotIn("pub-submit", js)
        self.assertIn("openPublishPicker", js)
        self.assertIn("videoStateLabel", js)
        self.assertIn("briefAngleCards", js)
        self.assertIn("remaining_platforms", js)
        self.assertIn("config-task-summary", js)
        self.assertIn("sourceTypeLabel", js)
        self.assertIn("deleteSource", js)
        self.assertIn("deleteBrief", js)
        self.assertIn("generateBriefFromSource", js)

    def test_video_preview_urls_are_cache_busted_after_local_recompose(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("videoFileUrl", js)
        self.assertIn("final_version", js)
        self.assertIn("Cache-Control\", \"no-store", (
            ROOT / "scripts" / "management_server.py"
        ).read_text(encoding="utf-8"))
        self.assertIn("confirmBriefAndGenerateScripts", js)

    def test_management_ui_static_assets_are_never_browser_cached(self):
        server = (
            ROOT / "scripts" / "management_server.py"
        ).read_text(encoding="utf-8")

        self.assertIn('self.send_header("Cache-Control", "no-store")', server)
        self.assertIn('self.send_header("Pragma", "no-cache")', server)

    def test_new_source_modal_supports_local_video_and_audio_upload_states(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        for capability in (
            'value="video"', 'value="audio"', 'id="source-file"',
            ".mp4,.mov,.webm", "uploadMediaSource", "正在上传", "正在转写",
            "video:'本地视频'", "audio:'本地音频'",
        ):
            self.assertIn(capability, js)

    def test_new_source_flow_focuses_the_new_independent_content_project(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("state.studioProjectId=result.source_id", js)
        self.assertIn("已复用来源解析结果并新建独立内容项目", js)

    def test_video_source_drawer_exposes_reconstructed_script_content(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        for capability in (
            "sourceAnalysisMarkup", "视频剧本解析", "场景", "人物与动作",
            "说话人", "镜头", "画面文字", "原声与台词", "重新解析视频",
            "/analyze", "正在转写并解析画面", "视频已转写、解析并归档",
        ):
            self.assertIn(capability, js)

    def test_workbench_constrains_grid_row_so_long_stage_content_can_scroll(self):
        css = (ROOT / "scripts" / "management_ui" / "styles.css").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'body[data-page="overview"] .studio-workbench{grid-template-rows:minmax(0,1fr)}',
            css,
        )

    def test_media_upload_checks_asr_readiness_before_sending_file(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("ensureAsrReadyForMedia", js)
        self.assertIn("本地音视频转写需要先配置火山语音识别", js)
        self.assertIn("去配置中心", js)

    def test_ui_exposes_video_preflight_and_human_acceptance_gate(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        for capability in (
            "生产预检与成本确认",
            "人工成片验收",
            "身份一致",
            "口型与语音",
            "字幕同步",
            "手部与画面",
            "openVideoReview",
            "/review",
        ):
            self.assertIn(capability, js)

    def test_publishing_renderers_have_one_authoritative_definition(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertEqual(js.count("function renderPublishing("), 1)
        self.assertEqual(js.count("function openPublicationDrawer("), 1)

    def test_ui_exposes_only_safe_batch_operations(self):
        script = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        for capability in (
            "批量删除候选", "批量标记不采用", "批量生产预检",
            "/api/source-candidates/batch-delete",
            "/api/scripts/batch-reject",
            "/api/videos/batch-preflight",
        ):
            self.assertIn(capability, script)
        self.assertNotIn("批量自动生成", script)
        self.assertNotIn("批量自动发布", script)

    def test_ui_renders_six_group_todo_center_with_direct_targets(self):
        script = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("/api/todos", script)
        self.assertIn("function renderTodoCenter", script)
        self.assertIn("function openTodoTarget", script)
        for label in (
            "需内容决策", "需制作设置", "需人工复核",
            "需成片验收", "需发布", "需绩效录入",
        ):
            self.assertIn(label, script)

    def test_page_loading_failure_keeps_content_and_exposes_retry(self):
        script = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("function setPageLoadState", script)
        self.assertIn("正在加载最新数据", script)
        self.assertIn("保留当前内容", script)
        self.assertIn("data-page-retry", script)

    def test_source_drawer_opens_existing_brief_without_regeneration(self):
        script = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("function linkedBriefForSource", script)
        self.assertIn("查看已生成简报", script)
        self.assertIn("if(existingBrief)return openBriefDrawer(existingBrief)", script)


class ManagementMediaServerTests(unittest.TestCase):
    def test_binary_upload_content_types_are_route_specific_and_exact(self):
        service = mock.Mock()
        service.add_media_source.return_value = {"source_id": "SRC-TYPE"}
        service.add_custom_character_image.return_value = {
            "character_id": "CHAR-1", "image_id": "IMG-TYPE"
        }
        with tempfile.TemporaryDirectory() as tmp:
            service.root = Path(tmp)
            ConsoleHandler.service = service
            server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            cases = (
                (
                    "/api/sources/media?filename=source.mp4",
                    "video/mp4", service.add_media_source,
                ),
                (
                    "/api/characters/CHAR-1/images?filename=portrait.png",
                    "image/png", service.add_custom_character_image,
                ),
            )
            try:
                for path, valid_type, service_call in cases:
                    with self.subTest(path=path, content_type="text/plain"):
                        with self.assertRaises(error.HTTPError) as rejected:
                            request.urlopen(request.Request(
                                f"{base_url}{path}", data=b"payload",
                                headers={"Content-Type": "text/plain", "Origin": base_url},
                                method="POST",
                            ))
                        self.assertEqual(rejected.exception.code, 415)
                        service_call.assert_not_called()
                    with self.subTest(path=path, content_type=valid_type):
                        response = request.urlopen(request.Request(
                            f"{base_url}{path}", data=b"payload",
                            headers={"Content-Type": valid_type, "Origin": base_url},
                            method="POST",
                        ))
                        self.assertEqual(response.status, 200)
                        response.read()
                        self.assertEqual(service_call.call_count, 1)

                with self.assertRaises(error.HTTPError) as inexact:
                    request.urlopen(request.Request(
                        f"{base_url}/api/characters/CHAR-1/nested/images",
                        data=b"payload",
                        headers={
                            "Content-Type": "application/octet-stream",
                            "Origin": base_url,
                        },
                        method="POST",
                    ))
                self.assertEqual(inexact.exception.code, 415)
                self.assertEqual(service.add_custom_character_image.call_count, 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_binary_uploads_reject_foreign_origin_and_allow_same_origin(self):
        service = mock.Mock()
        service.add_media_source.return_value = {"source_id": "SRC-SAME"}
        service.add_custom_character_image.return_value = {
            "character_id": "CHAR-1", "image_id": "IMG-SAME"
        }
        with tempfile.TemporaryDirectory() as tmp:
            service.root = Path(tmp)
            ConsoleHandler.service = service
            server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            cases = (
                (
                    "/api/sources/media?filename=source.mp4",
                    service.add_media_source,
                ),
                (
                    "/api/characters/CHAR-1/images?filename=portrait.png",
                    service.add_custom_character_image,
                ),
            )
            try:
                for path, service_call in cases:
                    with self.subTest(path=path, origin="foreign"):
                        with self.assertRaises(error.HTTPError) as rejected:
                            request.urlopen(request.Request(
                                f"{base_url}{path}",
                                data=b"binary payload",
                                headers={
                                    "Content-Type": "application/octet-stream",
                                    "Origin": "https://evil.example",
                                },
                                method="POST",
                            ))
                        self.assertEqual(rejected.exception.code, 403)
                        service_call.assert_not_called()

                    with self.subTest(path=path, origin="same"):
                        response = request.urlopen(request.Request(
                            f"{base_url}{path}",
                            data=b"binary payload",
                            headers={
                                "Content-Type": "application/octet-stream",
                                "Origin": base_url,
                            },
                            method="POST",
                        ))
                        self.assertEqual(response.status, 200)
                        response.read()
                        self.assertEqual(service_call.call_count, 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_all_json_posts_reject_foreign_origin_and_wrong_content_type_before_produce(self):
        service = mock.Mock()
        ConsoleHandler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = (
            f"http://127.0.0.1:{server.server_port}"
            "/api/videos/J/produce"
        )
        try:
            with self.assertRaises(error.HTTPError) as foreign:
                request.urlopen(request.Request(
                    endpoint,
                    data=b'{"confirmed":true}',
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://evil.example",
                    },
                    method="POST",
                ))
            with self.assertRaises(error.HTTPError) as plain_text:
                request.urlopen(request.Request(
                    endpoint,
                    data=b'{"confirmed":true}',
                    headers={"Content-Type": "text/plain"},
                    method="POST",
                ))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(foreign.exception.code, 403)
        self.assertEqual(plain_text.exception.code, 415)
        service.start_video_job.assert_not_called()

    def test_foreign_origin_is_rejected_before_other_write_routes(self):
        service = mock.Mock()
        ConsoleHandler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cases = (
            ("/api/source-candidates/batch-delete", {"candidate_ids": ["C-1"]}),
            ("/api/config", {"changes": {"PUBLISH_MODE": "manual"}}),
            ("/api/videos/J/review", {"decision": "accept"}),
        )
        try:
            for path, payload in cases:
                with self.subTest(path=path):
                    with self.assertRaises(error.HTTPError) as rejected:
                        request.urlopen(request.Request(
                            f"http://127.0.0.1:{server.server_port}{path}",
                            data=json.dumps(payload).encode("utf-8"),
                            headers={
                                "Content-Type": "application/json",
                                "Origin": "https://evil.example",
                            },
                            method="POST",
                        ))
                    self.assertEqual(rejected.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        service.batch_delete_source_candidates.assert_not_called()
        service.update_config.assert_not_called()
        service.review_video.assert_not_called()

    def test_failed_video_rebuild_endpoint_calls_service(self):
        service = mock.Mock()
        service.rebuild_failed_director_plan.return_value = {
            "job_id": "J-FAILED", "state": "STORYBOARDED"
        }
        ConsoleHandler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = request.urlopen(request.Request(
                f"http://127.0.0.1:{server.server_port}"
                "/api/videos/J-FAILED/rebuild-director",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            ))
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(payload["state"], "STORYBOARDED")
        service.rebuild_failed_director_plan.assert_called_once_with("J-FAILED")

    def test_script_edit_endpoint_forwards_auto_duration_choice(self):
        service = mock.Mock()
        service.update_script_draft.return_value = {
            "draft_id": "SCRIPT-AUTO", "duration_adjusted": True,
        }
        ConsoleHandler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = request.urlopen(request.Request(
                f"http://127.0.0.1:{server.server_port}"
                "/api/scripts/SCRIPT-AUTO/edit",
                data=json.dumps({
                    "script": {"title": "编辑后剧本"},
                    "change_note": "调整台词",
                    "auto_adjust_durations": True,
                }).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            ))
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertTrue(payload["duration_adjusted"])
        service.update_script_draft.assert_called_once_with(
            "SCRIPT-AUTO",
            {"title": "编辑后剧本"},
            change_note="调整台词",
            auto_adjust_durations=True,
        )

    def test_management_server_rejects_non_loopback_bind(self):
        self.assertEqual(validate_local_bind_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(validate_local_bind_host("localhost"), "localhost")
        with self.assertRaisesRegex(ValueError, "本机"):
            validate_local_bind_host("0.0.0.0")

    def test_script_generation_endpoint_forwards_optional_creative_guidance(self):
        service = mock.Mock()
        service.confirm_brief_and_generate_scripts.return_value = {
            "brief_id": "BRIEF-GUIDED", "job_id": "J-GUIDED",
            "draft_count": 3, "cached": False,
        }
        ConsoleHandler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        request_payload = {
            "confirmed": True,
            "angle_index": 2,
            "suggested_duration_sec": 50,
            "suggested_shot_count": 8,
            "suggested_character_count": 3,
        }
        try:
            response = request.urlopen(request.Request(
                f"http://127.0.0.1:{server.server_port}"
                "/api/briefs/BRIEF-GUIDED/generate-scripts",
                data=json.dumps(request_payload).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            ))
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(payload["draft_count"], 3)
        service.confirm_brief_and_generate_scripts.assert_called_once_with(
            "BRIEF-GUIDED",
            angle_index=2,
            generation_preferences={
                "suggested_duration_sec": 50,
                "suggested_shot_count": 8,
                "suggested_character_count": 3,
            },
        )

    def test_production_health_endpoint_reports_supervisor_liveness(self):
        service = mock.Mock()
        service.production_health.return_value = {
            "supervisor_alive": True,
            "concurrency_limit": 1,
            "running": [],
            "queued": [],
            "orphaned_recovered": 0,
            "last_error": None,
        }
        ConsoleHandler.service = service
        ConsoleHandler.queue_supervisor = mock.Mock()
        ConsoleHandler.queue_supervisor.is_alive.return_value = True
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/production/health"
            )
            content_type = response.headers.get_content_type()
            body = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(content_type, "application/json")
        payload = json.loads(body)
        self.assertTrue(payload["supervisor_alive"])
        service.production_health.assert_called_once_with(supervisor_alive=True)

    def test_manual_provider_reconcile_endpoint_is_bounded(self):
        service = mock.Mock()
        service.reconcile_provider_tasks.return_value = {
            "checked": 4, "completed": 2, "pending": 2, "failed": 0,
        }
        ConsoleHandler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = request.urlopen(request.Request(
                f"http://127.0.0.1:{server.server_port}/api/production/reconcile",
                data=json.dumps({"limit": 4}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            ))
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(payload["checked"], 4)
        service.reconcile_provider_tasks.assert_called_once_with(limit=4)

    def test_performance_snapshot_endpoint_delegates_to_publication_service(self):
        service = mock.Mock()
        service.record_performance_snapshot.return_value = {
            "snapshot_id": "PERF-1", "latest_snapshot": {"views": 321},
        }
        ConsoleHandler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = (
                f"http://127.0.0.1:{server.server_port}"
                "/api/publications/PUB-1/performance"
            )
            response = request.urlopen(request.Request(
                url,
                data=json.dumps({"views": 321}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            ))
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(payload["snapshot_id"], "PERF-1")
        service.record_performance_snapshot.assert_called_once_with(
            "PUB-1", {"views": 321}
        )

    def test_safe_batch_endpoints_delegate_without_starting_production(self):
        service = mock.Mock()
        service.batch_delete_source_candidates.return_value = {"deleted": 2}
        service.batch_reject_scripts.return_value = {"rejected": 2}
        service.batch_production_preflight.return_value = {
            "job_count": 2, "paid_provider_calls": 0,
        }
        ConsoleHandler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cases = [
            ("/api/source-candidates/batch-delete", {"candidate_ids": ["C-1", "C-2"]}),
            ("/api/scripts/batch-reject", {"draft_ids": ["D-1", "D-2"]}),
            ("/api/videos/batch-preflight", {"job_ids": ["J-1", "J-2"]}),
        ]
        try:
            for path, payload in cases:
                response = request.urlopen(request.Request(
                    f"http://127.0.0.1:{server.server_port}{path}",
                    data=json.dumps(payload).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                ))
                self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        service.batch_delete_source_candidates.assert_called_once_with(["C-1", "C-2"])
        service.batch_reject_scripts.assert_called_once_with(["D-1", "D-2"])
        service.batch_production_preflight.assert_called_once_with(["J-1", "J-2"])
        service.start_video_job.assert_not_called()

    def test_master_candidate_review_endpoint_delegates_human_decision(self):
        service = mock.Mock()
        service.review_master_candidate.return_value = {
            "job_id": "J-REVIEW", "master_idx": 2,
            "decision": "keep_prior", "state": "COMPOSED",
        }
        ConsoleHandler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = (
                f"http://127.0.0.1:{server.server_port}"
                "/api/videos/J-REVIEW/masters/2/candidate-review"
            )
            response = request.urlopen(request.Request(
                url,
                data=json.dumps({"decision": "keep_prior"}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            ))
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(payload["state"], "COMPOSED")
        service.review_master_candidate.assert_called_once_with(
            "J-REVIEW", 2, decision="keep_prior"
        )

    def test_master_lip_sync_repair_endpoint_delegates_precise_interval(self):
        service = mock.Mock()
        service.prepare_master_lip_sync_repair.return_value = {
            "job_id": "J-LIP", "master_idx": 3,
            "repair_kind": "seedance_lip_sync_edit", "state": "STORYBOARDED",
        }
        ConsoleHandler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = (
                f"http://127.0.0.1:{server.server_port}"
                "/api/videos/J-LIP/masters/3/lip-sync-repair"
            )
            response = request.urlopen(request.Request(
                url,
                data=json.dumps({
                    "repair_start_sec": 3,
                    "repair_end_sec": 15,
                    "reason": "林夏嘴型比原生语音慢",
                }).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            ))
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(payload["repair_kind"], "seedance_lip_sync_edit")
        service.prepare_master_lip_sync_repair.assert_called_once_with(
            "J-LIP", 3, start_sec=3.0, end_sec=15.0,
            reason="林夏嘴型比原生语音慢",
        )

    def test_master_extension_endpoint_delegates_duration_and_direction(self):
        service = mock.Mock()
        service.prepare_master_extension.return_value = {
            "job_id": "J-EXTEND", "master_idx": 2,
            "repair_kind": "seedance_extend", "state": "STORYBOARDED",
        }
        ConsoleHandler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = (
                f"http://127.0.0.1:{server.server_port}"
                "/api/videos/J-EXTEND/masters/2/extend"
            )
            response = request.urlopen(request.Request(
                url,
                data=json.dumps({
                    "extension_sec": 8,
                    "direction": "继续跟随工程师走向控制台",
                }).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            ))
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(payload["repair_kind"], "seedance_extend")
        service.prepare_master_extension.assert_called_once_with(
            "J-EXTEND", 2, extension_sec=8.0,
            direction="继续跟随工程师走向控制台",
        )

    def test_master_candidate_preview_endpoint_streams_review_video(self):
        service = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "candidate.mp4"
            candidate.write_bytes(b"candidate-video")
            service.master_candidate_file.return_value = candidate
            ConsoleHandler.service = service
            server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = (
                    f"http://127.0.0.1:{server.server_port}"
                    "/api/videos/J-REVIEW/masters/2/candidate"
                )
                response = request.urlopen(url)
                body = response.read()
                content_type = response.headers["Content-Type"]
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(body, b"candidate-video")
        self.assertEqual(content_type, "video/mp4")
        service.master_candidate_file.assert_called_once_with("J-REVIEW", 2)

    def test_byte_range_supports_preview_seeking_and_suffix_requests(self):
        self.assertEqual(parse_byte_range("bytes=10-19", 100), (10, 19))
        self.assertEqual(parse_byte_range("bytes=90-", 100), (90, 99))
        self.assertEqual(parse_byte_range("bytes=-10", 100), (90, 99))

    def test_invalid_byte_range_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_byte_range("bytes=100-120", 100)

    def test_media_upload_endpoint_sanitizes_filename_and_removes_temporary_file(self):
        captured = {}
        service = mock.Mock()

        def add_media_source(path, *, title):
            captured["path"] = Path(path)
            captured["bytes"] = Path(path).read_bytes()
            captured["title"] = title
            return {"source_id": "SRC-UPLOAD", "created": True, "source_type": "video"}

        service.add_media_source.side_effect = add_media_source
        with tempfile.TemporaryDirectory() as tmp:
            service.root = Path(tmp)
            ConsoleHandler.service = service
            server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = (
                    f"http://127.0.0.1:{server.server_port}/api/sources/media"
                    "?filename=..%2F..%2Finterview.mp4&title=%E5%AE%A2%E6%88%B7%E8%AE%BF%E8%B0%88"
                )
                response = request.urlopen(request.Request(
                    url, data=b"video bytes", method="POST",
                    headers={"Content-Type": "application/octet-stream"},
                ))
                payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(payload["source_id"], "SRC-UPLOAD")
        self.assertEqual(captured["bytes"], b"video bytes")
        self.assertEqual(captured["title"], "客户访谈")
        self.assertNotIn("..", captured["path"].name)
        self.assertEqual(captured["path"].suffix, ".mp4")
        self.assertFalse(captured["path"].exists())

    def test_media_upload_endpoint_rejects_declared_oversize_before_reading(self):
        service = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            service.root = Path(tmp)
            ConsoleHandler.service = service
            server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                req = request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/sources/media?filename=large.mp4",
                    data=b"x", method="POST",
                    headers={
                        "Content-Length": str(MEDIA_UPLOAD_MAX_BYTES + 1),
                        "Content-Type": "application/octet-stream",
                    },
                )
                with self.assertRaises(error.HTTPError) as raised:
                    request.urlopen(req)
                payload = json.loads(raised.exception.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(raised.exception.code, 413)
        self.assertIn("过大", payload["error"])
        service.add_media_source.assert_not_called()


if __name__ == "__main__":
    unittest.main()
