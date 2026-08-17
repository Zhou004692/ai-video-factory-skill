from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class SourceConnectorTests(unittest.TestCase):
    def test_source_connector_module_exists(self):
        self.assertIsNotNone(importlib.util.find_spec("factory.source_connectors"))

    def test_rss_item_is_normalized_as_a_candidate(self):
        from factory import source_connectors

        connector_type = getattr(source_connectors, "RssConnector", None)
        self.assertIsNotNone(connector_type)
        entry = ElementTree.fromstring(
            "<item><guid>news-1</guid><title>设备更新政策</title>"
            "<link>https://example.com/news/1</link>"
            "<description>设备租赁企业可申请更新补贴</description>"
            "<pubDate>Fri, 15 Aug 2026 08:00:00 GMT</pubDate></item>"
        )

        item = connector_type("https://example.com/feed.xml").normalize(entry)

        self.assertEqual(item.external_id, "news-1")
        self.assertEqual(item.source_url, "https://example.com/news/1")
        self.assertEqual(item.title, "设备更新政策")
        self.assertEqual(item.status, "candidate")

    def test_json_feed_item_preserves_publication_metadata(self):
        from factory import source_connectors

        connector_type = getattr(source_connectors, "JsonFeedConnector", None)
        self.assertIsNotNone(connector_type)
        payload = json.dumps({
            "version": "https://jsonfeed.org/version/1.1",
            "items": [{
                "id": "post-1",
                "url": "https://example.com/posts/1",
                "title": "竞品短视频观察",
                "summary": "三秒钩子和真实案例是主要结构",
                "date_published": "2026-08-15T08:00:00Z",
            }],
        })

        items = connector_type("https://example.com/feed.json").parse(payload)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].external_id, "post-1")
        self.assertEqual(items[0].published_at, "2026-08-15T08:00:00Z")
        self.assertEqual(items[0].excerpt, "三秒钩子和真实案例是主要结构")


if __name__ == "__main__":
    unittest.main()
