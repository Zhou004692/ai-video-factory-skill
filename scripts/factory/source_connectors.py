"""Public feed connectors for automatic topic discovery.

Only open, standards-based feeds are handled here.  Platform-specific authenticated
scrapers belong in separate adapters so collecting candidates never depends on a
browser session or silently gains publishing permissions.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import json
import re
from typing import Any, Iterable
from xml.etree import ElementTree

import httpx


_TAG_RE = re.compile(r"<[^>]+>")


def _plain_text(value: str | None) -> str:
    return " ".join(unescape(_TAG_RE.sub(" ", value or "")).split())


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ElementTree.Element, *names: str) -> str:
    wanted = set(names)
    for child in element:
        if _local_name(child.tag) in wanted:
            return "".join(child.itertext()).strip()
    return ""


@dataclass(frozen=True)
class ConnectorItem:
    external_id: str
    title: str
    source_url: str
    excerpt: str
    published_at: str | None
    raw: dict[str, Any]
    status: str = "candidate"


class _PublicFeedConnector:
    def __init__(self, endpoint: str, client: httpx.Client | None = None):
        self.endpoint = endpoint
        self._client = client

    def collect(self, cursor: str | None = None) -> tuple[list[ConnectorItem], str | None]:
        headers = {"User-Agent": "OB-AI-VIDEO/1.0 public-feed-collector"}
        if cursor:
            headers["If-None-Match"] = cursor
        if self._client is not None:
            response = self._client.get(self.endpoint, headers=headers)
        else:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                response = client.get(self.endpoint, headers=headers)
        if response.status_code == 304:
            return [], cursor
        response.raise_for_status()
        next_cursor = response.headers.get("ETag") or response.headers.get("Last-Modified") or cursor
        return self.parse(response.text), next_cursor


class RssConnector(_PublicFeedConnector):
    """Normalize RSS 2.0 and Atom entries into candidate records."""

    def normalize(self, entry: ElementTree.Element) -> ConnectorItem:
        link = _child_text(entry, "link")
        if not link:
            for child in entry:
                if _local_name(child.tag) == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        external_id = _child_text(entry, "guid", "id") or link
        title = _plain_text(_child_text(entry, "title"))
        excerpt = _plain_text(_child_text(entry, "description", "summary", "content"))
        published_at = _child_text(entry, "pubDate", "published", "updated") or None
        return ConnectorItem(
            external_id=external_id or f"{title}|{published_at or ''}",
            title=title or "未命名内容",
            source_url=link,
            excerpt=excerpt,
            published_at=published_at,
            raw={
                "format": "rss_or_atom",
                "title": title,
                "link": link,
                "published_at": published_at,
            },
        )

    def parse(self, xml_text: str) -> list[ConnectorItem]:
        root = ElementTree.fromstring(xml_text)
        entries: list[ElementTree.Element] = []
        for element in root.iter():
            if _local_name(element.tag) in {"item", "entry"}:
                entries.append(element)
        return [self.normalize(entry) for entry in entries]


class JsonFeedConnector(_PublicFeedConnector):
    """Normalize JSON Feed 1.x documents into candidate records."""

    def normalize(self, item: dict[str, Any]) -> ConnectorItem:
        source_url = str(item.get("url") or item.get("external_url") or "")
        external_id = str(item.get("id") or source_url)
        title = _plain_text(str(item.get("title") or ""))
        excerpt_value = (
            item.get("summary")
            or item.get("content_text")
            or item.get("content_html")
            or ""
        )
        published_at = item.get("date_published") or item.get("date_modified")
        return ConnectorItem(
            external_id=external_id or f"{title}|{published_at or ''}",
            title=title or "未命名内容",
            source_url=source_url,
            excerpt=_plain_text(str(excerpt_value)),
            published_at=str(published_at) if published_at else None,
            raw=item,
        )

    def parse(self, json_text: str) -> list[ConnectorItem]:
        document = json.loads(json_text)
        items: Iterable[dict[str, Any]] = document.get("items") or []
        return [self.normalize(item) for item in items if isinstance(item, dict)]


def connector_for(
    feed_type: str,
    endpoint: str,
    client: httpx.Client | None = None,
) -> _PublicFeedConnector:
    normalized = feed_type.strip().lower()
    if normalized in {"rss", "atom", "xml"}:
        return RssConnector(endpoint, client=client)
    if normalized in {"json", "json_feed", "jsonfeed"}:
        return JsonFeedConnector(endpoint, client=client)
    raise ValueError(f"unsupported public feed type: {feed_type}")
