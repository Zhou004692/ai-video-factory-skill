"""Published-video performance snapshots and transparent learning reports."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


COUNT_FIELDS = (
    "views", "completed_views", "likes", "comments", "shares", "saves",
    "follows", "leads",
)

FIELD_ALIASES = {
    "job_id": ("job_id", "任务ID", "任务id", "任务编号"),
    "platform": ("platform", "平台"),
    "captured_at": ("captured_at", "统计时间", "快照时间", "数据时间"),
    "views": ("views", "播放量", "播放次数"),
    "completed_views": ("completed_views", "完播量", "完播次数"),
    "completion_rate": ("completion_rate", "完播率"),
    "average_watch_sec": (
        "average_watch_sec", "平均观看时长", "平均播放时长"
    ),
    "likes": ("likes", "点赞数"),
    "comments": ("comments", "评论数"),
    "shares": ("shares", "转发数", "分享数"),
    "saves": ("saves", "收藏数"),
    "follows": ("follows", "新增关注", "涨粉数"),
    "leads": ("leads", "线索数"),
}

PLATFORM_ALIASES = {
    "抖音": "douyin",
    "视频号": "weixin_channels",
    "微信视频号": "weixin_channels",
}

PLATFORM_EXPORT_FIELDS = {
    "douyin": {
        "item_id": ("作品ID", "作品id", "视频ID", "item_id"),
        "title": ("作品标题", "作品描述", "视频标题", "title"),
        "views": ("播放次数", "播放量", "views"),
        "completed_views": ("完播次数", "完播量", "completed_views"),
        "completion_rate": ("完播率", "completion_rate"),
        "average_watch_sec": (
            "平均播放时长", "平均播放时长(秒)", "平均观看时长", "average_watch_sec"
        ),
        "likes": ("点赞量", "点赞数", "likes"),
        "comments": ("评论量", "评论数", "comments"),
        "shares": ("分享量", "转发量", "分享数", "shares"),
        "saves": ("收藏量", "收藏数", "saves"),
        "follows": ("涨粉量", "新增关注", "follows"),
        "leads": ("线索数", "leads"),
    },
    "weixin_channels": {
        "item_id": ("视频ID", "视频id", "作品ID", "item_id"),
        "title": ("视频描述", "视频标题", "作品标题", "title"),
        "views": ("播放量", "播放次数", "views"),
        "completed_views": ("完播量", "完播次数", "completed_views"),
        "completion_rate": ("完播率", "completion_rate"),
        "average_watch_sec": (
            "平均观看时长(秒)", "平均观看时长", "平均播放时长", "average_watch_sec"
        ),
        "likes": ("点赞次数", "点赞数", "likes"),
        "comments": ("评论次数", "评论数", "comments"),
        "shares": ("转发次数", "分享次数", "shares"),
        "saves": ("收藏次数", "收藏数", "saves"),
        "follows": ("新增关注人数", "新增关注", "follows"),
        "leads": ("线索数", "leads"),
    },
}

PERFORMANCE_WINDOWS_HOURS = (24, 72, 168)


def _nonnegative(name: str, value: int | float | None) -> int | None:
    if value is None:
        return None
    number = int(value)
    if number < 0:
        raise ValueError(f"{name} must be >= 0")
    return number


def _rate(name: str, value: float | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not 0 <= number <= 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return number


def _platform(value: str) -> str:
    normalized = str(value or "").strip().lower()
    normalized = PLATFORM_ALIASES.get(normalized, normalized)
    if normalized not in PLATFORM_EXPORT_FIELDS:
        raise ValueError(
            "platform must be douyin or weixin_channels"
        )
    return normalized


def _utc_time(value: str, *, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def performance_due_tasks(
    db: sqlite3.Connection,
    *,
    now: str | datetime | None = None,
) -> list[dict]:
    """Return missing 24/72/168-hour cumulative snapshot tasks.

    A snapshot belongs to the first collection interval containing its capture
    time.  Late data therefore never overwrites or silently completes an
    earlier missed window.
    """
    if now is None:
        current = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        if now.tzinfo is None:
            raise ValueError("now must include a timezone")
        current = now.astimezone(timezone.utc)
    else:
        current = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        current = current.astimezone(timezone.utc)
    publications = db.execute(
        "SELECT p.publication_id,p.job_id,p.platform,p.item_id,p.published_at,"
        "p.metadata_json,j.title FROM platform_publications p "
        "JOIN jobs j ON j.job_id=p.job_id WHERE p.published_at IS NOT NULL"
    ).fetchall()
    tasks: list[dict] = []
    for publication in publications:
        try:
            metadata = json.loads(publication["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        if str(metadata.get("review_status") or "").lower() in {
            "pending", "pending_review", "submitted"
        }:
            continue
        published = datetime.fromisoformat(
            str(publication["published_at"]).replace("Z", "+00:00")
        )
        if published.tzinfo is None:
            continue
        published = published.astimezone(timezone.utc)
        captures = [
            datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")).astimezone(timezone.utc)
            for row in db.execute(
                "SELECT captured_at FROM performance_snapshots "
                "WHERE job_id=? AND platform=? ORDER BY captured_at",
                (publication["job_id"], publication["platform"]),
            ).fetchall()
        ]
        for index, window_hours in enumerate(PERFORMANCE_WINDOWS_HOURS):
            due_at = published + timedelta(hours=window_hours)
            if current < due_at:
                continue
            next_due = (
                published + timedelta(hours=PERFORMANCE_WINDOWS_HOURS[index + 1])
                if index + 1 < len(PERFORMANCE_WINDOWS_HOURS) else None
            )
            completed = any(
                captured >= due_at and (next_due is None or captured < next_due)
                for captured in captures
            )
            if completed:
                continue
            tasks.append({
                "publication_id": publication["publication_id"],
                "job_id": publication["job_id"],
                "title": publication["title"],
                "platform": publication["platform"],
                "item_id": publication["item_id"],
                "window_hours": window_hours,
                "due_at": due_at.isoformat(),
                "overdue_hours": max(0, int((current - due_at).total_seconds() // 3600)),
            })
    return sorted(tasks, key=lambda item: (item["due_at"], item["publication_id"]))


def register_publication(
    db: sqlite3.Connection,
    job_id: str,
    *,
    platform: str,
    item_id: str,
    item_url: str | None = None,
    title: str | None = None,
    published_at: str | None = None,
    metadata: dict | None = None,
) -> str:
    """Map one canonical platform item to one published factory job."""
    job_id = str(job_id).strip()
    platform = _platform(platform)
    item_id = str(item_id).strip()
    if not item_id:
        raise ValueError("item_id is required")
    published_at = (
        _utc_time(published_at, field="published_at")
        if published_at else None
    )
    job = db.execute(
        "SELECT state, publish_url FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if not job:
        raise ValueError(f"job {job_id} not found")
    if job["state"] != "PUBLISHED" and not job["publish_url"]:
        raise ValueError(f"job {job_id} is not published")
    item_owner = db.execute(
        "SELECT job_id, publication_id FROM platform_publications "
        "WHERE platform=? AND item_id=?", (platform, item_id),
    ).fetchone()
    if item_owner and item_owner["job_id"] != job_id:
        raise ValueError(
            f"{platform} item {item_id} is already mapped to "
            f"{item_owner['job_id']}"
        )
    job_item = db.execute(
        "SELECT item_id, publication_id FROM platform_publications "
        "WHERE job_id=? AND platform=?", (job_id, platform),
    ).fetchone()
    if job_item and job_item["item_id"] != item_id:
        raise ValueError(
            f"job {job_id} already has canonical publication "
            f"{job_item['item_id']} on {platform}"
        )
    identity = f"{job_id}\0{platform}\0{item_id}"
    publication_id = "PUB-" + hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:16]
    db.execute(
        "INSERT INTO platform_publications(publication_id, job_id, platform, "
        "item_id, item_url, title, published_at, metadata_json) "
        "VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(platform, item_id) DO UPDATE SET "
        "item_url=excluded.item_url, title=excluded.title, "
        "published_at=COALESCE(excluded.published_at, "
        "platform_publications.published_at), "
        "metadata_json=excluded.metadata_json, updated_at=CURRENT_TIMESTAMP",
        (
            publication_id, job_id, platform, item_id,
            str(item_url).strip() if item_url else None,
            str(title).strip() if title else None,
            published_at,
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )
    db.commit()
    return publication_id


def _normalize_snapshot(
    db: sqlite3.Connection,
    job_id: str,
    *,
    platform: str,
    captured_at: str | None = None,
    views: int = 0,
    completed_views: int | None = None,
    completion_rate: float | None = None,
    average_watch_sec: float | None = None,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
    saves: int = 0,
    follows: int = 0,
    leads: int = 0,
    metadata: dict | None = None,
) -> dict:
    """Validate and normalize one snapshot without mutating the database."""
    job_id = str(job_id).strip()
    platform = str(platform).strip().lower()
    platform = PLATFORM_ALIASES.get(platform, platform)
    job = db.execute(
        "SELECT state, publish_url FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if not job:
        raise ValueError(f"job {job_id} not found")
    if job["state"] != "PUBLISHED" and not job["publish_url"]:
        raise ValueError(f"job {job_id} is not published")
    if not platform:
        raise ValueError("platform is required")
    captured_at = captured_at or datetime.now(timezone.utc).isoformat()
    captured_at = _utc_time(captured_at, field="captured_at")

    values = {
        "views": views, "completed_views": completed_views, "likes": likes,
        "comments": comments, "shares": shares, "saves": saves,
        "follows": follows, "leads": leads,
    }
    counts = {name: _nonnegative(name, value) for name, value in values.items()}
    views = counts["views"] or 0
    completed_views = counts["completed_views"]
    supplied_rate = _rate("completion_rate", completion_rate)
    derived_rate = (
        completed_views / views if completed_views is not None and views else None
    )
    if completed_views is not None and completed_views > views:
        raise ValueError("completed_views must not exceed views")
    if (supplied_rate is not None and derived_rate is not None
            and abs(supplied_rate - derived_rate) > 0.001):
        raise ValueError("completion_rate does not match completed_views/views")
    completion_rate = derived_rate if derived_rate is not None else supplied_rate
    if average_watch_sec is not None and float(average_watch_sec) < 0:
        raise ValueError("average_watch_sec must be >= 0")

    identity = f"{job_id}\0{platform}\0{captured_at}"
    return {
        "snapshot_id": "PERF-" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()[:16],
        "job_id": job_id, "platform": platform, "captured_at": captured_at,
        "views": views, "completed_views": completed_views,
        "completion_rate": completion_rate,
        "average_watch_sec": (
            None if average_watch_sec is None else float(average_watch_sec)
        ),
        "likes": counts["likes"], "comments": counts["comments"],
        "shares": counts["shares"], "saves": counts["saves"],
        "follows": counts["follows"], "leads": counts["leads"],
        "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
    }


def _write_snapshot(db: sqlite3.Connection, snapshot: dict) -> None:
    db.execute(
        "INSERT INTO performance_snapshots(snapshot_id, job_id, platform, "
        "captured_at, views, completed_views, completion_rate, "
        "average_watch_sec, likes, comments, shares, saves, follows, leads, "
        "metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(job_id, platform, captured_at) DO UPDATE SET "
        "views=excluded.views, completed_views=excluded.completed_views, "
        "completion_rate=excluded.completion_rate, "
        "average_watch_sec=excluded.average_watch_sec, likes=excluded.likes, "
        "comments=excluded.comments, shares=excluded.shares, "
        "saves=excluded.saves, follows=excluded.follows, leads=excluded.leads, "
        "metadata_json=excluded.metadata_json, updated_at=CURRENT_TIMESTAMP",
        tuple(snapshot[field] for field in (
            "snapshot_id", "job_id", "platform", "captured_at", "views",
            "completed_views", "completion_rate", "average_watch_sec", "likes",
            "comments", "shares", "saves", "follows", "leads", "metadata_json",
        )),
    )


def record_snapshot(
    db: sqlite3.Connection,
    job_id: str,
    *,
    platform: str,
    captured_at: str | None = None,
    views: int = 0,
    completed_views: int | None = None,
    completion_rate: float | None = None,
    average_watch_sec: float | None = None,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
    saves: int = 0,
    follows: int = 0,
    leads: int = 0,
    metadata: dict | None = None,
) -> str:
    """Upsert one cumulative platform snapshot for a published job."""
    snapshot = _normalize_snapshot(
        db, job_id, platform=platform, captured_at=captured_at, views=views,
        completed_views=completed_views, completion_rate=completion_rate,
        average_watch_sec=average_watch_sec, likes=likes, comments=comments,
        shares=shares, saves=saves, follows=follows, leads=leads,
        metadata=metadata,
    )
    _write_snapshot(db, snapshot)
    db.commit()
    return snapshot["snapshot_id"]


def _field(row: dict[str, str], name: str) -> str:
    for alias in FIELD_ALIASES[name]:
        if alias in row and str(row[alias] or "").strip():
            return str(row[alias]).strip()
    return ""


def _aliased_field(row: dict[str, str], aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        if alias in row and str(row[alias] or "").strip():
            return str(row[alias]).strip()
    return ""


def _csv_int(value: str, *, default: int | None = None) -> int | None:
    value = value.strip().replace(",", "")
    if not value:
        return default
    return int(float(value))


def _csv_float(value: str) -> float | None:
    value = value.strip().replace(",", "")
    return float(value) if value else None


def _csv_rate(value: str) -> float | None:
    value = value.strip().replace(",", "")
    if not value:
        return None
    if value.endswith("%"):
        return float(value[:-1]) / 100
    number = float(value)
    return number / 100 if 1 < number <= 100 else number


def import_csv(
    db: sqlite3.Connection,
    path: str | Path,
    *,
    default_platform: str | None = None,
    default_captured_at: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Validate a UTF-8 CSV fully, then atomically upsert all snapshots."""
    path = Path(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV header is required")
        parsed: list[dict] = []
        for line_number, row in enumerate(reader, start=2):
            job_id = _field(row, "job_id")
            platform = _field(row, "platform") or str(default_platform or "")
            captured_at = (
                _field(row, "captured_at") or default_captured_at
            )
            try:
                if not captured_at:
                    raise ValueError(
                        "captured_at is required in CSV or as a default"
                    )
                snapshot = _normalize_snapshot(
                    db, job_id, platform=platform,
                    captured_at=captured_at,
                    views=_csv_int(_field(row, "views"), default=0) or 0,
                    completed_views=_csv_int(
                        _field(row, "completed_views"), default=None
                    ),
                    completion_rate=_csv_rate(_field(row, "completion_rate")),
                    average_watch_sec=_csv_float(
                        _field(row, "average_watch_sec")
                    ),
                    likes=_csv_int(_field(row, "likes"), default=0) or 0,
                    comments=_csv_int(_field(row, "comments"), default=0) or 0,
                    shares=_csv_int(_field(row, "shares"), default=0) or 0,
                    saves=_csv_int(_field(row, "saves"), default=0) or 0,
                    follows=_csv_int(_field(row, "follows"), default=0) or 0,
                    leads=_csv_int(_field(row, "leads"), default=0) or 0,
                    metadata={"import_file": path.name, "import_row": line_number},
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"row {line_number} ({job_id or 'missing job_id'}): {exc}"
                ) from exc
            parsed.append(snapshot)
    if not parsed:
        raise ValueError("CSV contains no data rows")

    unique = {snapshot["snapshot_id"]: snapshot for snapshot in parsed}
    snapshots = list(unique.values())
    existing = {
        snapshot["snapshot_id"]
        for snapshot in snapshots
        if db.execute(
            "SELECT 1 FROM performance_snapshots WHERE snapshot_id=?",
            (snapshot["snapshot_id"],),
        ).fetchone()
    }
    result = {
        "rows": len(parsed), "unique_snapshots": len(snapshots),
        "inserted": len(snapshots) - len(existing),
        "updated": len(existing), "dry_run": bool(dry_run),
    }
    if dry_run:
        return result
    try:
        for snapshot in snapshots:
            _write_snapshot(db, snapshot)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result


def import_platform_csv(
    db: sqlite3.Connection,
    path: str | Path,
    *,
    platform: str,
    captured_at: str,
    dry_run: bool = False,
) -> dict:
    """Import a native Douyin or Weixin Channels export via item mapping."""
    platform = _platform(platform)
    if not captured_at:
        raise ValueError("captured_at is required")
    aliases = PLATFORM_EXPORT_FIELDS[platform]
    path = Path(path)
    parsed: list[dict] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV header is required")
        for line_number, row in enumerate(reader, start=2):
            item_id = _aliased_field(row, aliases["item_id"])
            try:
                if not item_id:
                    raise ValueError("platform item_id is required")
                publication = db.execute(
                    "SELECT job_id FROM platform_publications "
                    "WHERE platform=? AND item_id=?", (platform, item_id),
                ).fetchone()
                if not publication:
                    raise ValueError(
                        f"platform item {item_id} is not mapped to a job"
                    )
                snapshot = _normalize_snapshot(
                    db, publication["job_id"], platform=platform,
                    captured_at=captured_at,
                    views=_csv_int(
                        _aliased_field(row, aliases["views"]), default=0
                    ) or 0,
                    completed_views=_csv_int(
                        _aliased_field(row, aliases["completed_views"]),
                        default=None,
                    ),
                    completion_rate=_csv_rate(
                        _aliased_field(row, aliases["completion_rate"])
                    ),
                    average_watch_sec=_csv_float(
                        _aliased_field(row, aliases["average_watch_sec"])
                    ),
                    likes=_csv_int(
                        _aliased_field(row, aliases["likes"]), default=0
                    ) or 0,
                    comments=_csv_int(
                        _aliased_field(row, aliases["comments"]), default=0
                    ) or 0,
                    shares=_csv_int(
                        _aliased_field(row, aliases["shares"]), default=0
                    ) or 0,
                    saves=_csv_int(
                        _aliased_field(row, aliases["saves"]), default=0
                    ) or 0,
                    follows=_csv_int(
                        _aliased_field(row, aliases["follows"]), default=0
                    ) or 0,
                    leads=_csv_int(
                        _aliased_field(row, aliases["leads"]), default=0
                    ) or 0,
                    metadata={
                        "adapter": platform, "platform_item_id": item_id,
                        "platform_title": _aliased_field(row, aliases["title"]),
                        "import_file": path.name, "import_row": line_number,
                    },
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"row {line_number} ({item_id or 'missing item_id'}): {exc}"
                ) from exc
            parsed.append(snapshot)
    if not parsed:
        raise ValueError("CSV contains no data rows")
    unique = {snapshot["snapshot_id"]: snapshot for snapshot in parsed}
    snapshots = list(unique.values())
    existing = {
        snapshot["snapshot_id"] for snapshot in snapshots
        if db.execute(
            "SELECT 1 FROM performance_snapshots WHERE snapshot_id=?",
            (snapshot["snapshot_id"],),
        ).fetchone()
    }
    result = {
        "rows": len(parsed), "matched": len(parsed),
        "unique_snapshots": len(snapshots),
        "inserted": len(snapshots) - len(existing),
        "updated": len(existing), "dry_run": bool(dry_run),
        "platform": platform,
    }
    if dry_run:
        return result
    try:
        for snapshot in snapshots:
            _write_snapshot(db, snapshot)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result


def _latest_snapshots(
    db: sqlite3.Connection, *, platform: str | None = None
) -> list[sqlite3.Row]:
    platform_clause = "p.platform=? AND " if platform else ""
    params = (platform,) if platform else ()
    return db.execute(
        "SELECT p.* FROM performance_snapshots p WHERE " + platform_clause +
        "NOT EXISTS (SELECT 1 FROM performance_snapshots newer "
        "WHERE newer.job_id=p.job_id AND newer.platform=p.platform "
        "AND (newer.captured_at>p.captured_at OR "
        "(newer.captured_at=p.captured_at AND newer.updated_at>p.updated_at)))",
        params,
    ).fetchall()


def _window_snapshots(
    db: sqlite3.Connection, *, platform: str | None, window_hours: float,
    min_window_coverage: float,
) -> tuple[list[sqlite3.Row], list[dict]]:
    platform_sql = "AND p.platform=? " if platform else ""
    params: list = [platform] if platform else []
    rows = db.execute(
        "SELECT p.*, pub.published_at FROM performance_snapshots p "
        "LEFT JOIN platform_publications pub ON pub.job_id=p.job_id "
        "AND pub.platform=p.platform WHERE 1=1 " + platform_sql +
        "ORDER BY p.job_id, p.platform, p.captured_at",
        params,
    ).fetchall()
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault((row["job_id"], row["platform"]), []).append(row)
    selected: list[sqlite3.Row] = []
    excluded: list[dict] = []
    for (job_id, row_platform), snapshots in groups.items():
        published_at = snapshots[0]["published_at"]
        if not published_at:
            excluded.append({
                "job_id": job_id, "platform": row_platform,
                "reason": "published_at_missing",
            })
            continue
        published = datetime.fromisoformat(published_at)
        eligible: list[tuple[float, sqlite3.Row]] = []
        for snapshot in snapshots:
            age_hours = (
                datetime.fromisoformat(snapshot["captured_at"]) - published
            ).total_seconds() / 3600
            if 0 <= age_hours <= window_hours:
                eligible.append((age_hours, snapshot))
        if not eligible:
            excluded.append({
                "job_id": job_id, "platform": row_platform,
                "reason": "snapshot_before_publish_or_after_window",
            })
            continue
        coverage, snapshot = max(eligible, key=lambda item: item[0])
        if coverage < window_hours * min_window_coverage:
            excluded.append({
                "job_id": job_id, "platform": row_platform,
                "reason": "window_not_mature",
                "coverage_hours": round(coverage, 3),
            })
            continue
        selected.append(snapshot)
    return selected, excluded


def _band(score: float) -> str:
    lower = min(int(score // 10) * 10, 90)
    return "90-100" if lower == 90 else f"{lower}-{lower + 9}"


def _aggregate(rows: list[sqlite3.Row]) -> dict:
    views = sum(int(row["views"] or 0) for row in rows)
    rate_views = sum(
        int(row["views"] or 0) for row in rows
        if row["completion_rate"] is not None
    )
    completion = sum(
        float(row["completion_rate"] or 0) * int(row["views"] or 0)
        for row in rows if row["completion_rate"] is not None
    )
    watch_views = sum(
        int(row["views"] or 0) for row in rows
        if row["average_watch_sec"] is not None
    )
    watch_seconds = sum(
        float(row["average_watch_sec"] or 0) * int(row["views"] or 0)
        for row in rows if row["average_watch_sec"] is not None
    )
    engagement = sum(
        int(row[field] or 0) for row in rows
        for field in ("likes", "comments", "shares", "saves")
    )
    follows = sum(int(row["follows"] or 0) for row in rows)
    leads = sum(int(row["leads"] or 0) for row in rows)
    completion_rate = completion / rate_views if rate_views else 0.0
    engagement_rate = engagement / views if views else 0.0
    follow_rate = follows / views if views else 0.0
    lead_rate = leads / views if views else 0.0
    return {
        "views": views,
        "completion_rate": completion_rate,
        "completion_rate_bps": round(completion_rate * 10000),
        "average_watch_sec": watch_seconds / watch_views if watch_views else 0.0,
        "engagement_rate": engagement_rate,
        "engagement_rate_bps": round(engagement_rate * 10000),
        "follow_rate": follow_rate,
        "follow_rate_bps": round(follow_rate * 10000),
        "lead_rate": lead_rate,
        "lead_rate_bps": round(lead_rate * 10000),
        "snapshot_count": len(rows),
    }


def learning_report(
    db: sqlite3.Connection, *, min_views: int = 100,
    platform: str | None = None, window_hours: float | None = None,
    min_window_coverage: float = 0.8,
) -> dict:
    """Aggregate latest platform snapshots by creative, source and score band."""
    if min_views < 0:
        raise ValueError("min_views must be >= 0")
    platform = _platform(platform) if platform else None
    if window_hours is not None and not platform:
        raise ValueError("platform is required for a windowed report")
    if window_hours is not None and window_hours <= 0:
        raise ValueError("window_hours must be > 0")
    if not 0 < min_window_coverage <= 1:
        raise ValueError("min_window_coverage must be between 0 and 1")
    if window_hours is None:
        snapshot_rows = _latest_snapshots(db, platform=platform)
        excluded: list[dict] = []
    else:
        snapshot_rows, excluded = _window_snapshots(
            db, platform=platform, window_hours=float(window_hours),
            min_window_coverage=float(min_window_coverage),
        )
    by_job: dict[str, list[sqlite3.Row]] = {}
    for row in snapshot_rows:
        by_job.setdefault(row["job_id"], []).append(row)

    creatives: list[dict] = []
    source_groups: dict[str, dict] = {}
    band_rows: dict[str, list[sqlite3.Row]] = {}
    for job_id, snapshots in by_job.items():
        metrics = _aggregate(snapshots)
        if metrics["views"] < min_views:
            continue
        row = db.execute(
            "SELECT b.brief_id, b.brief_json, b.approved_angle_index, "
            "j.topic_id FROM topic_briefs b JOIN jobs j "
            "ON j.job_id=b.approved_job_id WHERE b.approved_job_id=? "
            "AND b.status='approved'", (job_id,),
        ).fetchone()
        if not row:
            continue
        brief = json.loads(row["brief_json"])
        angle_index = (
            row["approved_angle_index"]
            if row["approved_angle_index"] is not None
            else int(brief["recommended_index"])
        )
        score = float((brief.get("source_score") or {}).get("overall") or 0)
        creative = {
            "job_id": job_id, "topic_id": row["topic_id"],
            "brief_id": row["brief_id"], "selected_angle_index": angle_index,
            "selected_title": brief["angles"][angle_index]["title"],
            "source_score": score, **metrics,
        }
        creatives.append(creative)
        band_rows.setdefault(_band(score), []).extend(snapshots)
        sources = db.execute(
            "SELECT s.source_id, s.title FROM topic_brief_sources l "
            "JOIN script_sources s ON s.source_id=l.source_id "
            "WHERE l.brief_id=? ORDER BY s.source_id", (row["brief_id"],),
        ).fetchall()
        for source in sources:
            group = source_groups.setdefault(source["source_id"], {
                "source_id": source["source_id"], "title": source["title"],
                "snapshots": [], "creative_ids": set(),
            })
            group["snapshots"].extend(snapshots)
            group["creative_ids"].add(job_id)

    score_bands = [
        {"band": band, **_aggregate(rows)}
        for band, rows in sorted(band_rows.items())
    ]
    source_rows = [
        {
            "source_id": group["source_id"], "title": group["title"],
            "creative_count": len(group["creative_ids"]),
            "attribution": "shared_exposure",
            **_aggregate(group["snapshots"]),
        }
        for group in source_groups.values()
    ]
    creatives.sort(key=lambda item: (-item["views"], item["job_id"]))
    source_rows.sort(key=lambda item: item["source_id"])
    return {
        "min_views": min_views, "platform": platform,
        "window_hours": window_hours,
        "min_window_coverage": min_window_coverage,
        "attribution_note": (
            "Source metrics are shared exposure, not causal contribution."
        ),
        "creatives": creatives,
        "sources": source_rows,
        "score_bands": score_bands,
        "excluded": sorted(
            excluded, key=lambda item: (item["job_id"], item["platform"])
        ),
    }
