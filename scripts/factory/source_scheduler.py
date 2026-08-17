"""Persistence and scheduling primitives for automatic topic candidates."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from . import script_sources, source_connectors


SCHEMA = """
CREATE TABLE IF NOT EXISTS source_feeds (
    feed_id             TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    feed_type           TEXT NOT NULL,
    endpoint            TEXT NOT NULL,
    source_kind         TEXT NOT NULL DEFAULT 'hotspot',
    enabled             INTEGER NOT NULL DEFAULT 1,
    cursor              TEXT,
    last_collected_at   TIMESTAMP,
    last_error          TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_candidates (
    candidate_id    TEXT PRIMARY KEY,
    feed_id         TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    title           TEXT NOT NULL,
    source_url      TEXT NOT NULL DEFAULT '',
    excerpt         TEXT NOT NULL DEFAULT '',
    published_at    TEXT,
    status          TEXT NOT NULL DEFAULT 'candidate',
    source_id       TEXT,
    raw_json        TEXT NOT NULL DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(feed_id, external_id),
    FOREIGN KEY(feed_id) REFERENCES source_feeds(feed_id),
    FOREIGN KEY(source_id) REFERENCES script_sources(source_id)
);

CREATE INDEX IF NOT EXISTS idx_source_candidates_status
    ON source_candidates(status, published_at, created_at);
CREATE INDEX IF NOT EXISTS idx_source_candidates_feed
    ON source_candidates(feed_id, created_at);
"""


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def ensure_schema(database: sqlite3.Connection) -> None:
    database.executescript(SCHEMA)
    database.commit()


def create_feed(
    database: sqlite3.Connection,
    *,
    name: str,
    feed_type: str,
    endpoint: str,
    source_kind: str = "hotspot",
    enabled: bool = True,
) -> dict[str, Any]:
    name = str(name or "").strip()
    if not name:
        raise ValueError("feed name is required")
    normalized_type = str(feed_type or "").strip().lower()
    # Validate type without touching the network.
    source_connectors.connector_for(normalized_type, endpoint)
    endpoint = script_sources.validate_public_url(endpoint)
    normalized_kind = str(source_kind or "hotspot").strip().lower()
    if normalized_kind not in {"hotspot", "competitor"}:
        raise ValueError("source_kind must be hotspot or competitor")
    digest = hashlib.sha256(
        f"{normalized_type}|{endpoint}".encode("utf-8")
    ).hexdigest()[:16]
    feed_id = f"FEED-{digest}"
    database.execute(
        "INSERT INTO source_feeds(feed_id,name,feed_type,endpoint,source_kind,enabled) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(feed_id) DO UPDATE SET "
        "name=excluded.name, source_kind=excluded.source_kind, enabled=excluded.enabled, "
        "updated_at=CURRENT_TIMESTAMP",
        (feed_id, name, normalized_type, endpoint, normalized_kind, int(enabled)),
    )
    database.commit()
    return _row(database.execute(
        "SELECT * FROM source_feeds WHERE feed_id=?", (feed_id,)
    ).fetchone()) or {}


def list_feeds(database: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in database.execute(
        "SELECT * FROM source_feeds ORDER BY created_at DESC, feed_id"
    ).fetchall()]


def _candidate_id(feed_id: str, external_id: str) -> str:
    digest = hashlib.sha256(f"{feed_id}|{external_id}".encode("utf-8")).hexdigest()
    return f"CAND-{digest[:16]}"


def collect_feed(database: sqlite3.Connection, feed_id: str) -> dict[str, Any]:
    feed = database.execute(
        "SELECT * FROM source_feeds WHERE feed_id=?", (feed_id,)
    ).fetchone()
    if feed is None:
        raise KeyError(f"source feed not found: {feed_id}")
    connector = source_connectors.connector_for(feed["feed_type"], feed["endpoint"])
    try:
        items, cursor = connector.collect(feed["cursor"])
        created = 0
        updated = 0
        for item in items:
            existing = database.execute(
                "SELECT candidate_id FROM source_candidates "
                "WHERE feed_id=? AND external_id=?",
                (feed_id, item.external_id),
            ).fetchone()
            candidate_id = _candidate_id(feed_id, item.external_id)
            database.execute(
                "INSERT INTO source_candidates("
                "candidate_id,feed_id,external_id,title,source_url,excerpt,published_at,raw_json"
                ") VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(feed_id,external_id) DO UPDATE SET "
                "title=excluded.title, source_url=excluded.source_url, "
                "excerpt=excluded.excerpt, published_at=excluded.published_at, "
                "raw_json=excluded.raw_json, updated_at=CURRENT_TIMESTAMP",
                (
                    candidate_id, feed_id, item.external_id, item.title,
                    item.source_url, item.excerpt, item.published_at,
                    json.dumps(item.raw, ensure_ascii=False),
                ),
            )
            if existing:
                updated += 1
            else:
                created += 1
        database.execute(
            "UPDATE source_feeds SET cursor=?, last_collected_at=CURRENT_TIMESTAMP, "
            "last_error='', updated_at=CURRENT_TIMESTAMP WHERE feed_id=?",
            (cursor, feed_id),
        )
        database.commit()
        return {"feed_id": feed_id, "created": created, "updated": updated, "error": ""}
    except Exception as exc:
        database.rollback()
        message = str(exc)[:1000]
        database.execute(
            "UPDATE source_feeds SET last_error=?, updated_at=CURRENT_TIMESTAMP WHERE feed_id=?",
            (message, feed_id),
        )
        database.commit()
        return {"feed_id": feed_id, "created": 0, "updated": 0, "error": message}


def collect_enabled_feeds(database: sqlite3.Connection) -> list[dict[str, Any]]:
    feed_ids = [row["feed_id"] for row in database.execute(
        "SELECT feed_id FROM source_feeds WHERE enabled=1 ORDER BY created_at, feed_id"
    ).fetchall()]
    return [collect_feed(database, feed_id) for feed_id in feed_ids]


def list_candidates(
    database: sqlite3.Connection,
    *,
    status: str | None = "candidate",
) -> list[dict[str, Any]]:
    if status:
        rows = database.execute(
            "SELECT c.*, f.name AS feed_name, f.source_kind, f.feed_type "
            "FROM source_candidates c JOIN source_feeds f ON f.feed_id=c.feed_id "
            "WHERE c.status=? ORDER BY COALESCE(c.published_at,c.created_at) DESC",
            (status,),
        ).fetchall()
    else:
        rows = database.execute(
            "SELECT c.*, f.name AS feed_name, f.source_kind, f.feed_type "
            "FROM source_candidates c JOIN source_feeds f ON f.feed_id=c.feed_id "
            "ORDER BY COALESCE(c.published_at,c.created_at) DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def promote_candidate(database: sqlite3.Connection, candidate_id: str) -> dict[str, Any]:
    candidate = database.execute(
        "SELECT c.*, f.name AS feed_name, f.source_kind FROM source_candidates c "
        "JOIN source_feeds f ON f.feed_id=c.feed_id WHERE c.candidate_id=?",
        (candidate_id,),
    ).fetchone()
    if candidate is None:
        raise KeyError(f"source candidate not found: {candidate_id}")
    if candidate["status"] == "promoted" and candidate["source_id"]:
        return {"candidate_id": candidate_id, "source_id": candidate["source_id"], "created": False}
    raw = json.loads(candidate["raw_json"] or "{}")
    result = script_sources.ingest_text(
        database,
        candidate["excerpt"] or candidate["title"],
        title=candidate["title"],
        source_uri=candidate["source_url"] or None,
        source_type=f"auto_{candidate['source_kind']}",
        metadata={
            "feed_id": candidate["feed_id"],
            "feed_name": candidate["feed_name"],
            "candidate_id": candidate_id,
            "external_id": candidate["external_id"],
            "published_at": candidate["published_at"],
            "raw": raw,
        },
        deduplication_key=f"auto-source:{candidate['feed_id']}:{candidate['external_id']}",
    )
    database.execute(
        "UPDATE source_candidates SET status='promoted', source_id=?, "
        "updated_at=CURRENT_TIMESTAMP WHERE candidate_id=?",
        (result.source_id, candidate_id),
    )
    database.commit()
    return {"candidate_id": candidate_id, "source_id": result.source_id, "created": result.created}


def delete_candidate(database: sqlite3.Connection, candidate_id: str) -> bool:
    cursor = database.execute(
        "DELETE FROM source_candidates WHERE candidate_id=? AND status='candidate'",
        (candidate_id,),
    )
    database.commit()
    return cursor.rowcount == 1


def delete_feed(database: sqlite3.Connection, feed_id: str) -> bool:
    pending = database.execute(
        "SELECT COUNT(*) FROM source_candidates WHERE feed_id=? AND status='candidate'",
        (feed_id,),
    ).fetchone()[0]
    if pending:
        raise ValueError("请先处理或删除该订阅源的候选内容")
    cursor = database.execute("DELETE FROM source_feeds WHERE feed_id=?", (feed_id,))
    database.commit()
    return cursor.rowcount == 1
