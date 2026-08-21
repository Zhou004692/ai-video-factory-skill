from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request
from unittest import mock

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory import db as factory_db
from factory import source_connectors, source_scheduler
from factory.management_api import ManagementService
from management_server import ConsoleHandler, run_source_feed_scheduler


class _Connector:
    def __init__(self, items=None, error: Exception | None = None):
        self.items = items or []
        self.error = error

    def collect(self, cursor=None):
        if self.error:
            raise self.error
        return self.items, '"cursor-2"'


class SourceSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(factory_db.DDL)
        source_scheduler.ensure_schema(self.db)

    def tearDown(self):
        self.db.close()

    def _item(self, external_id="story-1"):
        return source_connectors.ConnectorItem(
            external_id=external_id,
            title="设备更新补贴落地",
            source_url=f"https://example.com/{external_id}",
            excerpt="设备租赁企业可以申请更新补贴",
            published_at="2026-08-15T08:00:00Z",
            raw={"id": external_id},
        )

    def test_collect_is_idempotent_and_keeps_a_candidate_pool(self):
        feed = source_scheduler.create_feed(
            self.db,
            name="行业政策",
            feed_type="rss",
            endpoint="https://example.com/feed.xml",
            source_kind="hotspot",
        )
        connector = _Connector([self._item()])
        with mock.patch.object(source_connectors, "connector_for", return_value=connector):
            first = source_scheduler.collect_feed(self.db, feed["feed_id"])
            second = source_scheduler.collect_feed(self.db, feed["feed_id"])

        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(len(source_scheduler.list_candidates(self.db)), 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_one_broken_feed_does_not_stop_other_feeds(self):
        broken = source_scheduler.create_feed(
            self.db, name="坏源", feed_type="rss",
            endpoint="https://bad.example/feed", source_kind="hotspot",
        )
        healthy = source_scheduler.create_feed(
            self.db, name="好源", feed_type="json_feed",
            endpoint="https://good.example/feed", source_kind="competitor",
        )

        def connector_for(_feed_type, endpoint, client=None):
            if "bad" in endpoint:
                return _Connector(error=RuntimeError("feed unavailable"))
            return _Connector([self._item("story-2")])

        with mock.patch.object(source_connectors, "connector_for", side_effect=connector_for):
            results = source_scheduler.collect_enabled_feeds(self.db)

        self.assertEqual(len(results), 2)
        self.assertEqual(len(source_scheduler.list_candidates(self.db)), 1)
        feeds = {item["feed_id"]: item for item in source_scheduler.list_feeds(self.db)}
        self.assertIn("feed unavailable", feeds[broken["feed_id"]]["last_error"])
        self.assertEqual(feeds[healthy["feed_id"]]["last_error"], "")

    def test_promotion_creates_a_traceable_source_but_no_paid_job(self):
        feed = source_scheduler.create_feed(
            self.db, name="竞品公开视频", feed_type="json_feed",
            endpoint="https://example.com/feed.json", source_kind="competitor",
        )
        with mock.patch.object(
            source_connectors, "connector_for", return_value=_Connector([self._item()])
        ):
            source_scheduler.collect_feed(self.db, feed["feed_id"])
        candidate = source_scheduler.list_candidates(self.db)[0]

        result = source_scheduler.promote_candidate(self.db, candidate["candidate_id"])

        self.assertTrue(result["source_id"].startswith("SRC-"))
        source = self.db.execute(
            "SELECT * FROM script_sources WHERE source_id=?", (result["source_id"],)
        ).fetchone()
        self.assertEqual(source["source_type"], "auto_competitor")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        promoted = source_scheduler.list_candidates(self.db, status="promoted")[0]
        self.assertEqual(promoted["source_id"], result["source_id"])

    def test_management_service_exposes_feed_candidate_and_promotion_actions(self):
        with tempfile.TemporaryDirectory() as folder:
            service = ManagementService(self.db, Path(folder))
            feed = service.create_source_feed({
                "name": "政策热点",
                "feed_type": "rss",
                "endpoint": "https://example.com/feed.xml",
                "source_kind": "hotspot",
            })
            with mock.patch.object(
                source_connectors, "connector_for", return_value=_Connector([self._item()])
            ):
                collected = service.collect_source_feed(feed["feed_id"])
            candidate = service.source_candidates()["candidates"][0]
            promoted = service.promote_source_candidate(candidate["candidate_id"])

        self.assertEqual(collected["created"], 1)
        self.assertEqual(len(service.source_feeds()["feeds"]), 1)
        self.assertTrue(promoted["source_id"].startswith("SRC-"))

    def test_management_http_api_creates_and_lists_public_feeds(self):
        with tempfile.TemporaryDirectory() as folder:
            ConsoleHandler.service = ManagementService(self.db, Path(folder))
            server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = json.dumps({
                    "name": "公开热点",
                    "feed_type": "rss",
                    "endpoint": "https://example.com/feed.xml",
                    "source_kind": "hotspot",
                }).encode("utf-8")
                response = request.urlopen(request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/source-feeds",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ))
                created = json.loads(response.read())
                listed = json.loads(request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/api/source-feeds"
                ).read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertTrue(created["feed_id"].startswith("FEED-"))
        self.assertEqual(listed["feeds"][0]["name"], "公开热点")

    def test_background_scheduler_collects_without_running_at_startup(self):
        stop = threading.Event()
        service = mock.Mock()

        def collected():
            stop.set()
            return []

        service.collect_enabled_source_feeds.side_effect = collected
        thread = threading.Thread(
            target=run_source_feed_scheduler,
            args=(service, threading.RLock(), stop),
            kwargs={"interval_sec": 0.02},
        )
        thread.start()
        self.assertEqual(service.collect_enabled_source_feeds.call_count, 0)
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        service.collect_enabled_source_feeds.assert_called_once_with()

    def test_management_ui_exposes_candidates_without_implying_auto_generation(self):
        html = (ROOT / "scripts" / "management_ui" / "index.html").read_text(
            encoding="utf-8"
        )
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('data-script-stage="candidates"', html)
        self.assertIn('id="candidate-list"', html)
        self.assertIn("/api/source-feeds", js)
        self.assertIn("/api/source-candidates", js)
        self.assertIn("晋级为内容来源", js)
        self.assertIn("候选池不会自动生成剧本或视频", html)

    def test_feed_requires_explicit_authorization_and_valid_interval(self):
        with self.assertRaisesRegex(ValueError, "确认有权"):
            source_scheduler.create_feed(
                self.db,
                name="未授权来源",
                feed_type="rss",
                endpoint="https://example.com/feed.xml",
                source_kind="hotspot",
                authorization_confirmed=False,
            )
        with self.assertRaisesRegex(ValueError, "抓取周期"):
            source_scheduler.create_feed(
                self.db,
                name="过于频繁",
                feed_type="rss",
                endpoint="https://example.com/feed.xml",
                source_kind="hotspot",
                authorization_confirmed=True,
                interval_minutes=1,
            )
        with self.assertRaisesRegex(ValueError, "密钥"):
            source_scheduler.create_feed(
                self.db,
                name="带密钥的地址",
                feed_type="json_feed",
                endpoint="https://example.com/feed.json?access_token=do-not-store",
                source_kind="competitor",
                authorization_confirmed=True,
            )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            source_scheduler.create_feed(
                self.db,
                name="明文来源",
                feed_type="rss",
                endpoint="http://example.com/feed.xml",
                source_kind="hotspot",
                authorization_confirmed=True,
            )

    def test_due_scheduler_skips_feed_until_its_next_run(self):
        now = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
        feed = source_scheduler.create_feed(
            self.db,
            name="每小时热点",
            feed_type="rss",
            endpoint="https://example.com/hourly.xml",
            source_kind="hotspot",
            authorization_confirmed=True,
            interval_minutes=60,
            now=now,
        )
        connector = _Connector([self._item()])
        with mock.patch.object(source_connectors, "connector_for", return_value=connector):
            first = source_scheduler.collect_due_feeds(self.db, now=now)
            early = source_scheduler.collect_due_feeds(
                self.db, now=now + timedelta(minutes=59)
            )
            due = source_scheduler.collect_due_feeds(
                self.db, now=now + timedelta(minutes=60)
            )

        self.assertEqual([result["feed_id"] for result in first], [feed["feed_id"]])
        self.assertEqual(early, [])
        self.assertEqual(len(due), 1)
        listed = source_scheduler.list_feeds(self.db)[0]
        self.assertEqual(listed["interval_minutes"], 60)
        self.assertEqual(listed["last_created_count"], 0)
        self.assertEqual(listed["last_updated_count"], 1)
        self.assertEqual(listed["last_status"], "success")

    def test_cross_feed_tracking_urls_collapse_into_one_traceable_candidate(self):
        first_feed = source_scheduler.create_feed(
            self.db,
            name="行业热点",
            feed_type="rss",
            endpoint="https://hot.example/feed.xml",
            source_kind="hotspot",
            authorization_confirmed=True,
        )
        second_feed = source_scheduler.create_feed(
            self.db,
            name="竞品动态",
            feed_type="json_feed",
            endpoint="https://competitor.example/feed.json",
            source_kind="competitor",
            authorization_confirmed=True,
        )
        first_item = self._item("hot-1")
        first_item = source_connectors.ConnectorItem(
            **{**first_item.__dict__, "source_url": "https://news.example/story?id=7&utm_source=a"}
        )
        second_item = self._item("competitor-9")
        second_item = source_connectors.ConnectorItem(
            **{**second_item.__dict__, "source_url": "https://NEWS.example/story?utm_campaign=b&id=7#top"}
        )
        with mock.patch.object(
            source_connectors,
            "connector_for",
            side_effect=[_Connector([first_item]), _Connector([second_item])],
        ):
            first = source_scheduler.collect_feed(self.db, first_feed["feed_id"])
            second = source_scheduler.collect_feed(self.db, second_feed["feed_id"])

        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["duplicates"], 1)
        candidates = source_scheduler.list_candidates(self.db)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["occurrence_count"], 2)
        self.assertEqual(
            {item["feed_name"] for item in candidates[0]["occurrences"]},
            {"行业热点", "竞品动态"},
        )

    def test_reject_candidate_is_a_review_decision_and_never_creates_a_project(self):
        feed = source_scheduler.create_feed(
            self.db,
            name="竞品源",
            feed_type="json_feed",
            endpoint="https://example.com/competitor.json",
            source_kind="competitor",
            authorization_confirmed=True,
        )
        with mock.patch.object(
            source_connectors, "connector_for", return_value=_Connector([self._item()])
        ):
            source_scheduler.collect_feed(self.db, feed["feed_id"])
        candidate_id = source_scheduler.list_candidates(self.db)[0]["candidate_id"]

        rejected = source_scheduler.reject_candidate(self.db, candidate_id)

        self.assertEqual(rejected["status"], "rejected")
        with self.assertRaisesRegex(ValueError, "已不采用"):
            source_scheduler.promote_candidate(self.db, candidate_id)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM script_sources").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0], 0)

    def test_promoted_source_preserves_all_cross_feed_occurrences_and_is_idempotent(self):
        feed = source_scheduler.create_feed(
            self.db,
            name="授权热点",
            feed_type="rss",
            endpoint="https://example.com/authorized.xml",
            source_kind="hotspot",
            authorization_confirmed=True,
        )
        with mock.patch.object(
            source_connectors, "connector_for", return_value=_Connector([self._item()])
        ):
            source_scheduler.collect_feed(self.db, feed["feed_id"])
        candidate_id = source_scheduler.list_candidates(self.db)[0]["candidate_id"]

        first = source_scheduler.promote_candidate(self.db, candidate_id)
        second = source_scheduler.promote_candidate(self.db, candidate_id)

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["source_id"], second["source_id"])
        source = self.db.execute(
            "SELECT metadata_json FROM script_sources WHERE source_id=?", (first["source_id"],)
        ).fetchone()
        metadata = json.loads(source["metadata_json"])
        self.assertEqual(len(metadata["occurrences"]), 1)

    def test_management_ui_exposes_schedule_authorization_and_reject_action(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="feed-authorization-confirmed"', js)
        self.assertIn('id="feed-interval-minutes"', js)
        self.assertIn("下次抓取", js)
        self.assertIn("不采用", js)
        self.assertIn("/reject", js)

    def test_rss_and_json_feed_connectors_preserve_content_needed_for_review(self):
        rss = source_connectors.RssConnector("https://example.com/feed.xml").parse(
            """<rss><channel><item><guid>r-1</guid><title>行业补贴更新</title>
            <link>https://example.com/r-1</link><description><![CDATA[<b>补贴范围扩大</b>]]>
            </description><pubDate>2026-08-20</pubDate></item></channel></rss>"""
        )
        json_items = source_connectors.JsonFeedConnector(
            "https://example.com/feed.json"
        ).parse(json.dumps({"items": [{
            "id": "v-1", "title": "竞品案例", "external_url": "https://example.com/v-1",
            "content_text": "开场、冲突、证据与行动号召均已拆解",
            "date_published": "2026-08-20T10:00:00Z",
        }]}, ensure_ascii=False))

        self.assertEqual(rss[0].excerpt, "补贴范围扩大")
        self.assertEqual(json_items[0].excerpt, "开场、冲突、证据与行动号召均已拆解")
        self.assertEqual(json_items[0].source_url, "https://example.com/v-1")

    def test_feed_connector_rejects_redirect_to_private_network_before_second_get(self):
        calls = []

        def handler(request_):
            calls.append(str(request_.url))
            return httpx.Response(
                302,
                headers={"Location": "http://127.0.0.1/internal"},
                request=request_,
            )

        with httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client, mock.patch(
            "factory.script_sources._validate_resolved_host", return_value=None
        ):
            connector = source_connectors.RssConnector(
                "https://example.com/feed.xml", client=client
            )
            with self.assertRaisesRegex(ValueError, "public"):
                connector.collect()

        self.assertEqual(calls, ["https://example.com/feed.xml"])

    def test_existing_source_database_is_migrated_without_losing_candidates(self):
        legacy = sqlite3.connect(":memory:")
        legacy.row_factory = sqlite3.Row
        legacy.executescript("""
            CREATE TABLE source_feeds (
                feed_id TEXT PRIMARY KEY,name TEXT NOT NULL,feed_type TEXT NOT NULL,
                endpoint TEXT NOT NULL,source_kind TEXT NOT NULL DEFAULT 'hotspot',
                enabled INTEGER NOT NULL DEFAULT 1,cursor TEXT,last_collected_at TIMESTAMP,
                last_error TEXT NOT NULL DEFAULT '',created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE source_candidates (
                candidate_id TEXT PRIMARY KEY,feed_id TEXT NOT NULL,external_id TEXT NOT NULL,
                title TEXT NOT NULL,source_url TEXT NOT NULL DEFAULT '',excerpt TEXT NOT NULL DEFAULT '',
                published_at TEXT,status TEXT NOT NULL DEFAULT 'candidate',source_id TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}',created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,UNIQUE(feed_id,external_id)
            );
            INSERT INTO source_feeds(feed_id,name,feed_type,endpoint)
                VALUES('F-OLD','历史来源','rss','https://example.com/old.xml');
            INSERT INTO source_candidates(candidate_id,feed_id,external_id,title,source_url,excerpt)
                VALUES('C-OLD','F-OLD','E-OLD','历史候选','https://example.com/story','摘要');
        """)
        try:
            source_scheduler.ensure_schema(legacy)
            candidate = source_scheduler.list_candidates(legacy)[0]
            feed = source_scheduler.list_feeds(legacy)[0]
        finally:
            legacy.close()

        self.assertEqual(candidate["candidate_id"], "C-OLD")
        self.assertTrue(candidate["dedupe_key"])
        self.assertEqual(candidate["occurrence_count"], 1)
        self.assertEqual(len(candidate["occurrences"]), 1)
        self.assertEqual(feed["interval_minutes"], 60)
        self.assertEqual(feed["authorization_confirmed"], 1)

    def test_management_service_updates_feed_schedule_and_rejects_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            service = ManagementService(self.db, Path(folder))
            feed = service.create_source_feed({
                "name": "授权竞品源",
                "feed_type": "json_feed",
                "endpoint": "https://example.com/authorized.json",
                "source_kind": "competitor",
                "authorization_confirmed": True,
                "interval_minutes": 120,
            })
            updated = service.update_source_feed(feed["feed_id"], {
                "enabled": False,
                "interval_minutes": 360,
            })
            with mock.patch.object(
                source_connectors, "connector_for", return_value=_Connector([self._item()])
            ):
                source_scheduler.collect_feed(self.db, feed["feed_id"])
            candidate_id = source_scheduler.list_candidates(self.db)[0]["candidate_id"]
            rejected = service.reject_source_candidate(candidate_id, {"reason": "与品牌定位不符"})

        self.assertEqual(updated["enabled"], 0)
        self.assertEqual(updated["interval_minutes"], 360)
        self.assertEqual(rejected["status"], "rejected")

    def test_http_api_exposes_feed_settings_and_candidate_reject(self):
        service = mock.Mock()
        service.update_source_feed.return_value = {"feed_id": "FEED-1", "enabled": False}
        service.reject_source_candidate.return_value = {"candidate_id": "CAND-1", "status": "rejected"}
        ConsoleHandler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            settings_body = json.dumps({"enabled": False, "interval_minutes": 180}).encode()
            settings = json.loads(request.urlopen(request.Request(
                f"http://127.0.0.1:{server.server_port}/api/source-feeds/FEED-1/settings",
                data=settings_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )).read())
            reject_body = json.dumps({"reason": "重复立意"}).encode()
            rejected = json.loads(request.urlopen(request.Request(
                f"http://127.0.0.1:{server.server_port}/api/source-candidates/CAND-1/reject",
                data=reject_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )).read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(settings["feed_id"], "FEED-1")
        self.assertEqual(rejected["status"], "rejected")
        service.update_source_feed.assert_called_once_with(
            "FEED-1", {"enabled": False, "interval_minutes": 180}
        )
        service.reject_source_candidate.assert_called_once_with(
            "CAND-1", {"reason": "重复立意"}
        )


if __name__ == "__main__":
    unittest.main()
