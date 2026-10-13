"""Persistence and scheduling primitives for authorized topic feeds."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from . import script_sources, source_connectors


MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 7 * 24 * 60
_TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "from", "ref", "share_source", "source", "spm",
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS source_feeds (
    feed_id                 TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    feed_type               TEXT NOT NULL,
    endpoint                TEXT NOT NULL,
    source_kind             TEXT NOT NULL DEFAULT 'hotspot',
    enabled                 INTEGER NOT NULL DEFAULT 1,
    authorization_confirmed INTEGER NOT NULL DEFAULT 1,
    interval_minutes        INTEGER NOT NULL DEFAULT 60,
    cursor                  TEXT,
    next_collect_at         TIMESTAMP,
    last_collected_at       TIMESTAMP,
    last_status             TEXT NOT NULL DEFAULT 'never',
    last_created_count      INTEGER NOT NULL DEFAULT 0,
    last_updated_count      INTEGER NOT NULL DEFAULT 0,
    last_duplicate_count    INTEGER NOT NULL DEFAULT 0,
    last_error              TEXT NOT NULL DEFAULT '',
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_candidates (
    candidate_id        TEXT PRIMARY KEY,
    feed_id             TEXT NOT NULL,
    external_id         TEXT NOT NULL,
    dedupe_key          TEXT NOT NULL DEFAULT '',
    title               TEXT NOT NULL,
    source_url          TEXT NOT NULL DEFAULT '',
    excerpt             TEXT NOT NULL DEFAULT '',
    published_at        TEXT,
    status              TEXT NOT NULL DEFAULT 'candidate',
    source_id           TEXT,
    occurrence_count    INTEGER NOT NULL DEFAULT 1,
    rejection_reason    TEXT NOT NULL DEFAULT '',
    raw_json            TEXT NOT NULL DEFAULT '{}',
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(feed_id, external_id),
    FOREIGN KEY(feed_id) REFERENCES source_feeds(feed_id),
    FOREIGN KEY(source_id) REFERENCES script_sources(source_id)
);

CREATE TABLE IF NOT EXISTS source_candidate_occurrences (
    candidate_id    TEXT NOT NULL,
    feed_id         TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    source_url      TEXT NOT NULL DEFAULT '',
    published_at    TEXT,
    raw_json        TEXT NOT NULL DEFAULT '{}',
    first_seen_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(feed_id, external_id),
    FOREIGN KEY(candidate_id) REFERENCES source_candidates(candidate_id),
    FOREIGN KEY(feed_id) REFERENCES source_feeds(feed_id)
);

CREATE INDEX IF NOT EXISTS idx_source_candidates_status
    ON source_candidates(status, published_at, created_at);
CREATE INDEX IF NOT EXISTS idx_source_candidates_feed
    ON source_candidates(feed_id, created_at);
CREATE INDEX IF NOT EXISTS idx_source_candidates_dedupe
    ON source_candidates(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_source_candidate_occurrences_candidate
    ON source_candidate_occurrences(candidate_id, first_seen_at);
"""


_MIGRATION_COLUMNS = {
    "source_feeds": {
        "authorization_confirmed": "INTEGER NOT NULL DEFAULT 1",
        "interval_minutes": "INTEGER NOT NULL DEFAULT 60",
        "next_collect_at": "TIMESTAMP",
        "last_status": "TEXT NOT NULL DEFAULT 'never'",
        "last_created_count": "INTEGER NOT NULL DEFAULT 0",
        "last_updated_count": "INTEGER NOT NULL DEFAULT 0",
        "last_duplicate_count": "INTEGER NOT NULL DEFAULT 0",
    },
    "source_candidates": {
        "dedupe_key": "TEXT NOT NULL DEFAULT ''",
        "occurrence_count": "INTEGER NOT NULL DEFAULT 1",
        "rejection_reason": "TEXT NOT NULL DEFAULT ''",
    },
}


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _utc_now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _add_missing_columns(database: sqlite3.Connection, table: str) -> None:
    existing = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in database.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, definition in _MIGRATION_COLUMNS[table].items():
        if name not in existing:
            database.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def canonical_source_url(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if not host:
        return value
    port = parsed.port
    netloc = host
    if port and not ((parsed.scheme.lower() == "https" and port == 443)
                     or (parsed.scheme.lower() == "http" and port == 80)):
        netloc = f"{host}:{port}"
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        normalized = key.casefold()
        if normalized.startswith("utm_") or normalized in _TRACKING_QUERY_KEYS:
            continue
        query.append((key, item))
    query.sort(key=lambda pair: (pair[0].casefold(), pair[1]))
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, urlencode(query), ""))


def candidate_dedupe_key(item: source_connectors.ConnectorItem) -> str:
    canonical_url = canonical_source_url(item.source_url)
    if canonical_url:
        material = f"url:{canonical_url}"
    else:
        normalized = " ".join(f"{item.title} {item.excerpt}".casefold().split())
        material = f"content:{normalized}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def ensure_schema(database: sqlite3.Connection) -> None:
    # Avoid indexes on new columns until legacy tables have been migrated.
    table_sql, index_sql = SCHEMA.split("CREATE INDEX", 1)
    database.executescript(table_sql)
    _add_missing_columns(database, "source_feeds")
    _add_missing_columns(database, "source_candidates")
    database.executescript("CREATE INDEX" + index_sql)
    rows = database.execute(
        "SELECT candidate_id,feed_id,external_id,title,source_url,excerpt,"
        "published_at,raw_json,dedupe_key FROM source_candidates"
    ).fetchall()
    for row in rows:
        if not str(row["dedupe_key"] or ""):
            item = source_connectors.ConnectorItem(
                external_id=str(row["external_id"]), title=str(row["title"]),
                source_url=str(row["source_url"] or ""), excerpt=str(row["excerpt"] or ""),
                published_at=row["published_at"], raw=json.loads(row["raw_json"] or "{}"),
            )
            database.execute(
                "UPDATE source_candidates SET dedupe_key=? WHERE candidate_id=?",
                (candidate_dedupe_key(item), row["candidate_id"]),
            )
        database.execute(
            "INSERT OR IGNORE INTO source_candidate_occurrences("
            "candidate_id,feed_id,external_id,source_url,published_at,raw_json) "
            "VALUES(?,?,?,?,?,?)",
            (row["candidate_id"], row["feed_id"], row["external_id"],
             row["source_url"], row["published_at"], row["raw_json"]),
        )
    database.commit()


def _validate_interval(value: int | str) -> int:
    try:
        interval = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("抓取周期必须是分钟数") from exc
    if not MIN_INTERVAL_MINUTES <= interval <= MAX_INTERVAL_MINUTES:
        raise ValueError(
            f"抓取周期必须在 {MIN_INTERVAL_MINUTES} 到 {MAX_INTERVAL_MINUTES} 分钟之间"
        )
    return interval


def _validate_feed_endpoint(value: str) -> str:
    endpoint = script_sources.validate_public_url(value)
    parsed = urlsplit(endpoint)
    if parsed.scheme.casefold() != "https":
        raise ValueError("自动抓取来源必须使用 HTTPS 地址")
    sensitive_markers = ("token", "key", "secret", "signature", "password", "credential")
    has_secret_query = any(
        any(marker in key.casefold() for marker in sensitive_markers)
        for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
    )
    if parsed.username or parsed.password or has_secret_query:
        raise ValueError("Feed 地址不能包含账号、Token 或签名密钥")
    return endpoint


def create_feed(
    database: sqlite3.Connection,
    *,
    name: str,
    feed_type: str,
    endpoint: str,
    source_kind: str = "hotspot",
    enabled: bool = True,
    authorization_confirmed: bool = True,
    interval_minutes: int = 60,
    now: datetime | None = None,
) -> dict[str, Any]:
    name = str(name or "").strip()
    if not name:
        raise ValueError("来源名称不能为空")
    if authorization_confirmed is not True:
        raise ValueError("请先确认有权采集和使用该来源内容")
    interval = _validate_interval(interval_minutes)
    normalized_type = str(feed_type or "").strip().lower()
    source_connectors.connector_for(normalized_type, endpoint)
    endpoint = _validate_feed_endpoint(endpoint)
    normalized_kind = str(source_kind or "hotspot").strip().lower()
    if normalized_kind not in {"hotspot", "competitor"}:
        raise ValueError("内容类型必须是热点信息或竞品内容")
    digest = hashlib.sha256(f"{normalized_type}|{endpoint}".encode("utf-8")).hexdigest()[:16]
    feed_id = f"FEED-{digest}"
    database.execute(
        "INSERT INTO source_feeds("
        "feed_id,name,feed_type,endpoint,source_kind,enabled,authorization_confirmed,"
        "interval_minutes,next_collect_at) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(feed_id) DO UPDATE SET name=excluded.name,"
        "source_kind=excluded.source_kind,enabled=excluded.enabled,"
        "authorization_confirmed=excluded.authorization_confirmed,"
        "interval_minutes=excluded.interval_minutes,updated_at=CURRENT_TIMESTAMP",
        (feed_id, name, normalized_type, endpoint, normalized_kind, int(enabled), 1,
         interval, _timestamp(_utc_now(now))),
    )
    database.commit()
    return _row(database.execute(
        "SELECT * FROM source_feeds WHERE feed_id=?", (feed_id,)
    ).fetchone()) or {}


def update_feed(
    database: sqlite3.Connection,
    feed_id: str,
    *,
    enabled: bool,
    interval_minutes: int,
) -> dict[str, Any]:
    interval = _validate_interval(interval_minutes)
    cursor = database.execute(
        "UPDATE source_feeds SET enabled=?,interval_minutes=?,updated_at=CURRENT_TIMESTAMP "
        "WHERE feed_id=?",
        (int(enabled), interval, feed_id),
    )
    if cursor.rowcount != 1:
        database.rollback()
        raise KeyError(f"自动来源不存在: {feed_id}")
    database.commit()
    return _row(database.execute(
        "SELECT * FROM source_feeds WHERE feed_id=?", (feed_id,)
    ).fetchone()) or {}


def list_feeds(database: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in database.execute(
        "SELECT * FROM source_feeds ORDER BY created_at DESC,feed_id"
    ).fetchall()]


def _candidate_id(dedupe_key: str) -> str:
    return f"CAND-{dedupe_key[:16]}"


def _safe_collect_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"来源返回 HTTP {exc.response.status_code}，请检查地址或来源权限"
    if isinstance(exc, httpx.RequestError):
        return "连接来源失败，请检查网络、地址或来源权限"
    message = str(exc).strip()
    return message[:300] if message else f"来源解析失败（{type(exc).__name__}）"


def _record_occurrence(
    database: sqlite3.Connection,
    *,
    candidate_id: str,
    feed_id: str,
    item: source_connectors.ConnectorItem,
    now_text: str,
) -> None:
    database.execute(
        "INSERT INTO source_candidate_occurrences("
        "candidate_id,feed_id,external_id,source_url,published_at,raw_json,"
        "first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(feed_id,external_id) DO UPDATE SET "
        "source_url=excluded.source_url,published_at=excluded.published_at,"
        "raw_json=excluded.raw_json,last_seen_at=excluded.last_seen_at",
        (candidate_id, feed_id, item.external_id, item.source_url, item.published_at,
         json.dumps(item.raw, ensure_ascii=False), now_text, now_text),
    )


def collect_feed(
    database: sqlite3.Connection,
    feed_id: str,
    *,
    now: datetime | None = None,
    external_io=None,
) -> dict[str, Any]:
    feed = database.execute(
        "SELECT * FROM source_feeds WHERE feed_id=?", (feed_id,)
    ).fetchone()
    if feed is None:
        raise KeyError(f"自动来源不存在: {feed_id}")
    connector = source_connectors.connector_for(feed["feed_type"], feed["endpoint"])
    current = _utc_now(now)
    now_text = _timestamp(current)
    next_text = _timestamp(current + timedelta(minutes=int(feed["interval_minutes"] or 60)))
    feed_identity = dict(feed)
    def still_current():
        row = database.execute('SELECT * FROM source_feeds WHERE feed_id=?', (feed_id,)).fetchone()
        return row is not None and dict(row) == feed_identity
    try:
        operation = lambda: connector.collect(feed["cursor"])
        items, cursor = external_io(operation, step='source_collection', target={'feed_id': feed_id}) if external_io else operation()
        if not database.in_transaction:
            database.execute('BEGIN IMMEDIATE')
        if not still_current():
            database.rollback()
            return {'feed_id': feed_id, 'created': 0, 'updated': 0, 'duplicates': 0,
                    'error': '', 'stale': True, 'applied': False}
        created = updated = duplicates = 0
        for item in items:
            occurrence = database.execute(
                "SELECT candidate_id FROM source_candidate_occurrences "
                "WHERE feed_id=? AND external_id=?",
                (feed_id, item.external_id),
            ).fetchone()
            dedupe_key = candidate_dedupe_key(item)
            canonical = occurrence or database.execute(
                "SELECT candidate_id FROM source_candidates WHERE dedupe_key=? "
                "ORDER BY created_at,candidate_id LIMIT 1",
                (dedupe_key,),
            ).fetchone()
            candidate_id = str(canonical["candidate_id"]) if canonical else _candidate_id(dedupe_key)
            if canonical is None:
                database.execute(
                    "INSERT INTO source_candidates("
                    "candidate_id,feed_id,external_id,dedupe_key,title,source_url,excerpt,"
                    "published_at,raw_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (candidate_id, feed_id, item.external_id, dedupe_key, item.title,
                     item.source_url, item.excerpt, item.published_at,
                     json.dumps(item.raw, ensure_ascii=False), now_text, now_text),
                )
                created += 1
            else:
                database.execute(
                    "UPDATE source_candidates SET title=?,excerpt=?,updated_at=? "
                    "WHERE candidate_id=?",
                    (item.title, item.excerpt, now_text, candidate_id),
                )
                if occurrence is None:
                    duplicates += 1
                else:
                    updated += 1
            _record_occurrence(
                database, candidate_id=candidate_id, feed_id=feed_id,
                item=item, now_text=now_text,
            )
            database.execute(
                "UPDATE source_candidates SET occurrence_count=("
                "SELECT COUNT(*) FROM source_candidate_occurrences WHERE candidate_id=?"
                ") WHERE candidate_id=?",
                (candidate_id, candidate_id),
            )
        database.execute(
            "UPDATE source_feeds SET cursor=?,last_collected_at=?,next_collect_at=?,"
            "last_status='success',last_created_count=?,last_updated_count=?,"
            "last_duplicate_count=?,last_error='',updated_at=CURRENT_TIMESTAMP WHERE feed_id=?",
            (cursor, now_text, next_text, created, updated, duplicates, feed_id),
        )
        database.commit()
        return {
            "feed_id": feed_id, "created": created, "updated": updated,
            "duplicates": duplicates, "error": "", "next_collect_at": next_text,
        }
    except Exception as exc:
        database.rollback()
        database.execute('BEGIN IMMEDIATE')
        if not still_current():
            database.rollback()
            return {'feed_id': feed_id, 'created': 0, 'updated': 0, 'duplicates': 0,
                    'error': '', 'stale': True, 'applied': False}
        message = _safe_collect_error(exc)
        database.execute(
            "UPDATE source_feeds SET last_collected_at=?,next_collect_at=?,"
            "last_status='error',last_created_count=0,last_updated_count=0,"
            "last_duplicate_count=0,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE feed_id=?",
            (now_text, next_text, message, feed_id),
        )
        database.commit()
        return {
            "feed_id": feed_id, "created": 0, "updated": 0,
            "duplicates": 0, "error": message, "next_collect_at": next_text,
        }


def collect_due_feeds(
    database: sqlite3.Connection,
    *,
    now: datetime | None = None,
    external_io=None,
) -> list[dict[str, Any]]:
    current = _utc_now(now)
    feed_ids = [row["feed_id"] for row in database.execute(
        "SELECT feed_id FROM source_feeds WHERE enabled=1 "
        "AND authorization_confirmed=1 AND (next_collect_at IS NULL OR next_collect_at<=?) "
        "ORDER BY COALESCE(next_collect_at,created_at),feed_id",
        (_timestamp(current),),
    ).fetchall()]
    return [collect_feed(database, feed_id, now=current, external_io=external_io) for feed_id in feed_ids]


def collect_enabled_feeds(database: sqlite3.Connection) -> list[dict[str, Any]]:
    """Compatibility alias; enabled feeds are collected only when due."""
    return collect_due_feeds(database)


def _occurrences(database: sqlite3.Connection, candidate_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in database.execute(
        "SELECT o.feed_id,o.external_id,o.source_url,o.published_at,o.first_seen_at,"
        "o.last_seen_at,f.name AS feed_name,f.source_kind,f.feed_type "
        "FROM source_candidate_occurrences o JOIN source_feeds f ON f.feed_id=o.feed_id "
        "WHERE o.candidate_id=? ORDER BY o.first_seen_at,o.feed_id",
        (candidate_id,),
    ).fetchall()]


def list_candidates(
    database: sqlite3.Connection,
    *,
    status: str | None = "candidate",
) -> list[dict[str, Any]]:
    where = "WHERE c.status=?" if status else ""
    params = (status,) if status else ()
    rows = database.execute(
        "SELECT c.*,f.name AS feed_name,f.source_kind,f.feed_type "
        "FROM source_candidates c JOIN source_feeds f ON f.feed_id=c.feed_id "
        f"{where} ORDER BY COALESCE(c.published_at,c.created_at) DESC",
        params,
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["occurrences"] = _occurrences(database, str(row["candidate_id"]))
        item["source_kinds"] = sorted({entry["source_kind"] for entry in item["occurrences"]})
        result.append(item)
    return result


def promote_candidate(database: sqlite3.Connection, candidate_id: str) -> dict[str, Any]:
    candidate = database.execute(
        "SELECT c.*,f.name AS feed_name,f.source_kind FROM source_candidates c "
        "JOIN source_feeds f ON f.feed_id=c.feed_id WHERE c.candidate_id=?",
        (candidate_id,),
    ).fetchone()
    if candidate is None:
        raise KeyError(f"自动选题候选不存在: {candidate_id}")
    if candidate["status"] == "promoted" and candidate["source_id"]:
        return {"candidate_id": candidate_id, "source_id": candidate["source_id"], "created": False}
    if candidate["status"] == "rejected":
        raise ValueError("该候选已不采用，不能晋级为内容来源")
    if candidate["status"] != "candidate":
        raise ValueError("仅待审核候选可以晋级为内容来源")
    raw = json.loads(candidate["raw_json"] or "{}")
    occurrences = _occurrences(database, candidate_id)
    result = script_sources.ingest_text(
        database,
        candidate["excerpt"] or candidate["title"],
        title=candidate["title"],
        source_uri=candidate["source_url"] or None,
        source_type=f"auto_{candidate['source_kind']}",
        metadata={
            "feed_id": candidate["feed_id"], "feed_name": candidate["feed_name"],
            "candidate_id": candidate_id, "external_id": candidate["external_id"],
            "published_at": candidate["published_at"], "dedupe_key": candidate["dedupe_key"],
            "occurrences": occurrences, "raw": raw,
        },
        deduplication_key=f"auto-candidate:{candidate['dedupe_key']}",
    )
    database.execute(
        "UPDATE source_candidates SET status='promoted',source_id=?,updated_at=CURRENT_TIMESTAMP "
        "WHERE candidate_id=? AND status='candidate'",
        (result.source_id, candidate_id),
    )
    database.commit()
    return {"candidate_id": candidate_id, "source_id": result.source_id, "created": result.created}


def reject_candidate(
    database: sqlite3.Connection,
    candidate_id: str,
    *,
    reason: str = "",
) -> dict[str, Any]:
    cursor = database.execute(
        "UPDATE source_candidates SET status='rejected',rejection_reason=?,"
        "updated_at=CURRENT_TIMESTAMP WHERE candidate_id=? AND status='candidate'",
        (str(reason or "").strip()[:300], candidate_id),
    )
    if cursor.rowcount != 1:
        database.rollback()
        raise ValueError("仅待审核候选可以标记为不采用")
    database.commit()
    return {"candidate_id": candidate_id, "status": "rejected"}


def delete_candidate(database: sqlite3.Connection, candidate_id: str) -> bool:
    row = database.execute(
        "SELECT status FROM source_candidates WHERE candidate_id=?", (candidate_id,)
    ).fetchone()
    if row is None or row["status"] not in {"candidate", "rejected"}:
        return False
    database.execute(
        "DELETE FROM source_candidate_occurrences WHERE candidate_id=?", (candidate_id,)
    )
    cursor = database.execute(
        "DELETE FROM source_candidates WHERE candidate_id=?", (candidate_id,)
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
