from __future__ import annotations

import json
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


from factory import db, scene_assets  # noqa: E402
from factory.management_api import ManagementService  # noqa: E402
from management_server import (  # noqa: E402
    ConsoleHandler, MEDIA_UPLOAD_MAX_BYTES, parse_byte_range,
    run_queue_supervisor, validate_local_bind_host,
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
            ["ark", "asr", "storage"],
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
        self.assertTrue(all(group["guide"]["steps"] for group in status["required_groups"]))
        self.assertTrue(all(group["guide"]["url"].startswith("https://") for group in status["required_groups"]))

    def test_script_readiness_only_needs_ark_group(self):
        status = self.service.config_status()

        ark = next(group for group in status["required_groups"] if group["id"] == "ark")
        asr = next(group for group in status["required_groups"] if group["id"] == "asr")
        self.assertTrue(ark["ready"])
        self.assertFalse(asr["ready"])
        self.assertTrue(status["readiness"]["script"]["ready"])
        self.assertFalse(status["readiness"]["video"]["ready"])

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
            "asr_enabled": True, "budget_cents": 1800,
        })

        self.assertEqual(result["production_settings"]["character_id"], "sales_xiaoling")
        storyboard = json.loads(self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id='J-SET'"
        ).fetchone()[0])
        self.assertEqual(storyboard["characters"][0]["name"], "小玲")
        self.assertEqual(storyboard["production_settings"]["budget_cents"], 1800)
        self.assertEqual(self.service.videos()["jobs"][0]["preflight"]["budget_cents"], 1800)

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
            if item["source"] == "seedream_identity_image"
        )
        self.assertEqual(ai_makeup["status"], "pending")
        self.assertEqual(ai_makeup["estimated_cost_cents"], 29)
        self.assertEqual(video["preflight"]["paid_identity_anchor_count"], 1)
        self.assertEqual(video["preflight"]["identity_anchor_cost_cents"], 29)

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
                 "visual_intent": "主讲人开场"},
                {"idx": 2, "beat": "evidence", "duration": 5,
                 "video_master_idx": 2, "audio_master_idx": 1,
                 "video_start": 0, "audio_start": 4,
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
                ValueError, "超过已确认的单任务预算上限"
            ):
                self.service.start_video_job(
                    "J-BLOCK-SPAWN", confirmed=True
                )

        popen.assert_not_called()
        state = self.database.execute(
            "SELECT state FROM jobs WHERE job_id='J-BLOCK-SPAWN'"
        ).fetchone()["state"]
        self.assertEqual(state, "STORYBOARDED")

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

    def test_resume_failed_video_does_not_requeue_manual_review_candidate(self):
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
                        "candidate_path": "review/01-candidate.mp4",
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

        self.assertEqual(result["remaining_masters"], 1)
        self.assertEqual(result["manual_review_masters"], 1)
        shots = self.database.execute(
            "SELECT idx,state,error FROM shots WHERE job_id='J-RESUME-REVIEW' "
            "ORDER BY idx"
        ).fetchall()
        self.assertEqual(shots[0]["state"], "FAILED")
        self.assertEqual(shots[0]["error"], "visual mismatch")
        self.assertEqual(shots[1]["state"], "PENDING")
        self.assertIsNone(shots[1]["error"])

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
            "VALUES('J-CANDIDATE','T-1','candidate review','FAILED','{}',?)",
            (str(final),),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
            "VALUES('J-CANDIDATE',2,'FAILED',?,?,?)",
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
        self.database.commit()

        job = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-CANDIDATE"
        )

        self.assertEqual(
            job["shots"][0]["candidate_preview_url"],
            "/api/videos/J-CANDIDATE/masters/2/candidate",
        )
        self.assertTrue(job["shots"][0]["has_prior_master"])
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

        with mock.patch("factory.management_api.s6_compose.run") as compose:
            result = self.service.review_master_candidate(
                "J-FIRST-CANDIDATE", 1, decision="accept_candidate"
            )

        compose.assert_not_called()
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["remaining_generation_count"], 1)
        accepted = self.database.execute(
            "SELECT state,video_path,video_url,error FROM shots "
            "WHERE job_id='J-FIRST-CANDIDATE' AND idx=1"
        ).fetchone()
        self.assertEqual(accepted["state"], "OK")
        self.assertIsNone(accepted["error"])
        self.assertEqual(accepted["video_url"], "https://cdn/first-candidate.mp4")
        self.assertEqual(Path(accepted["video_path"]).read_bytes(), b"first-paid-candidate")

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

    def test_resolving_one_of_two_candidates_keeps_job_in_review_queue(self):
        asset_root = self.root / "data" / "assets" / "J-MULTI-REVIEW"
        review_dir = asset_root / "shots" / "review"
        review_dir.mkdir(parents=True)
        final = asset_root / "final.mp4"
        final.write_bytes(b"prior-final")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,final_path,error) "
            "VALUES('J-MULTI-REVIEW','T-1','multi review','FAILED','{}',?,'qa')",
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
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["remaining_review_count"], 1)
        self.assertEqual(job["state"], "FAILED")
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
            "VALUES('J-MULTI-ACCEPT','T-1','multi accept','FAILED','{}',?,'qa')",
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
            self.assertEqual(first["state"], "FAILED")
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

    def test_accept_candidate_restores_prior_files_when_local_recompose_fails(self):
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
        self.assertIn("ffmpeg failed", job["error"])
        self.assertEqual(shot["state"], "FAILED")
        self.assertEqual(shot["video_url"], "https://cdn/prior.mp4")
        self.assertEqual(old_master.read_bytes(), b"prior-master")
        self.assertEqual(final.read_bytes(), b"prior-final")
        self.assertTrue(candidate.exists())
        self.assertEqual(
            json.loads(shot["video_qa"])["candidate_path"], str(candidate)
        )

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
        self.assertIn("worker exited", failed["error"])
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-WORKER-NEXT'"
            ).fetchone()["state"],
            "PRODUCTION_RUNNING",
        )

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
            },
        )
        self.assertFalse(payload["supervisor_alive"])
        self.assertEqual(payload["concurrency_limit"], 1)
        self.assertEqual([item["job_id"] for item in payload["running"]], ["J-HEALTH-RUN"])
        self.assertEqual([item["job_id"] for item in payload["queued"]], ["J-HEALTH-QUEUE"])
        self.assertEqual(payload["orphaned_recovered"], 1)
        self.assertEqual(payload["last_error"], "queue db locked")

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
            "final_acceptance", "publish", "performance",
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
        self.assertTrue(any(
            item["id"] == "PUB-TODO" for item in groups["performance"]["items"]
        ))


class ManagementUiContractTests(unittest.TestCase):
    def test_anchor_review_stage_has_explicit_preview_and_resume_action(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        server = (ROOT / "scripts" / "management_server.py").read_text(
            encoding="utf-8"
        )

        for capability in (
            "ANCHOR_REVIEW_REQUIRED", "锚点待确认", "确认锚点并继续生成",
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
            "本任务没有跨母片重复的核心空间",
            "固定站位",
        ):
            self.assertIn(capability, js)
        self.assertGreaterEqual(js.count("sceneAnchorCards(job)"), 2)
        self.assertIn("scene-anchor-card", css)
        self.assertIn('and "/scenes/" in parsed.path', server)

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
        self.assertIn("缺少锚点时不会开始付费场景生成", js)
        self.assertNotIn("请先为这些剧本人物指定角色", js)
        self.assertNotIn("保存角色映射并进入预检", js)
        self.assertIn("data-speaker-mapping", js)
        self.assertIn("剧本人物", js)
        self.assertIn("beat.speaker", js)
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

    def test_active_video_drawer_refreshes_with_polling_without_resetting_finished_video(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        poller_start = js.find("async function pollVisiblePage")
        self.assertGreaterEqual(poller_start, 0)
        poller = js[poller_start:]

        self.assertIn("openVideoJobId:''", js)
        self.assertIn("state.openVideoJobId=''", js)
        self.assertIn("state.openVideoJobId=job.job_id", js)
        self.assertIn("activeProductionStates.has", poller)
        self.assertIn("if(!await loadPage('videos'))return", poller)
        self.assertIn("openVideoDrawer(fresh)", poller)
        self.assertIn("drawerBody.scrollTop=scrollTop", poller)
        self.assertIn("if(state.polling)return", poller)

    def test_budget_gate_requires_budget_increase_and_failed_job_can_resume(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("预计成本超过预算上限", js)
        self.assertIn("请返回制作设置提高预算", js)
        self.assertIn("继续生成剩余母片", js)
        self.assertIn("/resume", js)
        self.assertIn("disabled", js)
        self.assertIn("confirmProduction(fresh)", js)
        self.assertNotIn("openProductionSettings(fresh)", js)

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
        self.assertIn("state.todos=todos;renderTodoCenter(todos)", js)
        self.assertIn("todos:state.todos", js)
        self.assertIn('body[data-page="todos"] .global-stage-strip', css)

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

    def test_live_pipeline_pages_refresh_without_manual_navigation(self):
        script = (
            ROOT / "scripts" / "management_ui" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn(
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

        self.assertIn("s.manual_review_required?'review':''", js)
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

    def test_ui_exposes_master_candidate_preview_and_two_human_decisions(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("openMasterCandidateReview", js)
        self.assertIn("candidate_preview_url", js)
        self.assertIn("has_prior_master", js)
        self.assertIn("拒绝候选并重新生成", js)
        self.assertIn("接受候选并本地重合成", js)
        self.assertIn("保留旧版", js)
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


class ManagementMediaServerTests(unittest.TestCase):
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
                    headers={"Content-Length": str(MEDIA_UPLOAD_MAX_BYTES + 1)},
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
