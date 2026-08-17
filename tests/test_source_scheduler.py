from __future__ import annotations

import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request
from unittest import mock


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


if __name__ == "__main__":
    unittest.main()
