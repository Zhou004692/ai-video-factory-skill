"""Published-video performance snapshots and transparent learning reports."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import math
from decimal import Decimal, InvalidOperation
from . import content_review


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


def _nonnegative(name: str, value) -> int | None:
    if value is None or value == '':
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a finite nonnegative integer")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number != number.to_integral_value() or number > 9223372036854775807:
            raise ValueError(f"{name} must be a finite nonnegative integer")
        return int(number)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a finite nonnegative integer") from exc


def _number(name: str, value, *, upper=None):
    if value is None or value == '':
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite and >= 0") from exc
    if isinstance(value, bool) or not math.isfinite(number) or number < 0 or (upper is not None and number > upper):
        raise ValueError(f"{name} must be finite and between 0 and {upper}" if upper is not None else f"{name} must be finite and >= 0")
    return number


def _rate(name, value):
    return _number(name, value, upper=1)


RESERVED_METADATA = {'provided_fields', 'provenance', 'creative_context', 'creative_version',
    'source_proof', 'final_sha256', 'upload_binding', 'verified_revision', 'publication_context'}


def _metadata(value):
    if value is not None and not isinstance(value, dict):
        raise ValueError('metadata must be an object')
    return {k:v for k,v in (value or {}).items() if k not in RESERVED_METADATA}


def project_snapshot(row) -> dict:
    """Public projection never treats ambiguous legacy zeros as measurements."""
    result = dict(row)
    if not result.get('measurement_version'):
        for field in COUNT_FIELDS:
            if result.get(field) == 0:
                result[field] = None
        result['provenance'] = 'legacy_nonzero_only; zero_presence_unknown'
    else:
        result['provenance'] = 'recorded_measurement'
    result['provided_fields'] = [f for f in COUNT_FIELDS + ('completion_rate','average_watch_sec') if result.get(f) is not None]
    result['creative_context'] = content_review.object_json(result.get('context_json'))
    return result


def capture_context(db, job_id, *, assets_dir=None) -> dict:
    """Capture what is actually authorized now, never reconstruct old history."""
    from . import db as database
    from .review_candidates import controlled_job_asset_path
    job = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    assets = Path(assets_dir) if assets_dir is not None else database.database_path().parent / 'assets'
    report = content_review.require_final(db, assets, job_id)
    final = controlled_job_asset_path(assets, job_id, job['final_path'], require_file=True)
    sha = None
    if final:
        with final.open('rb') as handle:
            sha = hashlib.file_digest(handle, 'sha256').hexdigest()
    sb = content_review.object_json(job['storyboard'])
    spec = content_review.object_json(job['production_spec_json']) if 'production_spec_json' in job.keys() else {}
    frozen = spec.get('runtime_storyboard') if isinstance(spec.get('runtime_storyboard'),dict) else {}
    frozen_meta = frozen.get('script_metadata') or {}
    context = {'version': 1, 'authority': 'local_publication_context_v1',
        'binding_status': 'local_asset_captured' if sha else 'legacy_unverified',
        'final_sha256': sha, 'content_contract_hash': content_review.contract_hash(sb) if content_review.enabled(sb) else None,
        'edit_plan_fingerprint': content_review.edit_plan_fingerprint(sb) if sb else None,
        'review_proof': report, 'publication_revision': content_review.publication_revision(db, job_id, report) if report else None,
        'creative_ir': content_review.object_json(job['creative_ir_json']) if 'creative_ir_json' in job.keys() else {},
        'creative_ir_version': job['creative_ir_version'] if 'creative_ir_version' in job.keys() else None,
        'intent': None, 'topic_id': job['topic_id'],
        'creative_provenance': 'unverified_current_association', 'tags_provenance': 'unverified_current_association'}
    brief = db.execute("SELECT * FROM topic_briefs WHERE approved_job_id=? AND status='approved' ORDER BY created_at DESC LIMIT 1", (job_id,)).fetchone()
    if brief:
        data = content_review.object_json(brief['brief_json'])
        index = brief['approved_angle_index'] if brief['approved_angle_index'] is not None else int(data.get('recommended_index') or 0)
        angles = data.get('angles') or []
        context.update(brief_id=brief['brief_id'], selected_angle_index=index,
            selected_title=(angles[index].get('title') if index in range(len(angles)) else job['title']),
            source_score=(data.get('source_score') or {}).get('overall'))
        tag = db.execute('SELECT hook_type,narrative_pattern,proof_type,cta_type FROM creative_angle_tags WHERE brief_id=? AND angle_index=?', (brief['brief_id'],index)).fetchone()
        context['tags'] = dict(tag) if tag else {}
        context['sources'] = [dict(r) for r in db.execute('SELECT s.source_id,s.title FROM topic_brief_sources l JOIN script_sources s ON s.source_id=l.source_id WHERE l.brief_id=? ORDER BY s.source_id',(brief['brief_id'],))]
    # A current brief is only an association declaration, even for legacy jobs.
    # Absence of a render/frozen input must never promote live tags to evidence.
    from . import creative_ir
    context['declared_association'] = {key:context.pop(key,None) for key in
        ('brief_id','selected_angle_index','selected_title','source_score','tags','sources',
         'creative_ir','creative_ir_version')}
    score = frozen_meta.get('source_score')
    if isinstance(score, dict):
        score = score.get('overall')
    context.update(creative_ir=creative_ir.from_storyboard(frozen).as_dict() if frozen else {},
        creative_ir_version=1 if frozen else None,
        creative_provenance='frozen_production_input' if frozen else 'unknown',
        production_spec_hash=job['production_spec_hash'] if 'production_spec_hash' in job.keys() else None,
        frozen_creative_ir_hash=spec.get('creative_ir_hash'),
        brief_id=frozen_meta.get('brief_id'), selected_angle_index=frozen_meta.get('selected_angle_index'),
        selected_title=frozen_meta.get('title') or frozen.get('title'),
        source_score=score, source_score_provenance='frozen_production_input' if score is not None else 'unknown',
        tags=frozen_meta.get('creative_tags') or frozen.get('creative_tags') or {},
        tags_provenance='frozen_production_input' if (frozen_meta.get('creative_tags') or frozen.get('creative_tags')) else 'unknown',
        intent=frozen_meta.get('creative_thesis') or frozen.get('intent') or frozen.get('creative_intent'),
        source_provenance='frozen_production_input' if frozen_meta.get('source_ids') else 'unknown',
        sources=[{'source_id':source_id,'title':None} for source_id in frozen_meta.get('source_ids') or []])
    return context


def persist_publication(db, job_id, *, platform, item_id, item_url=None, title=None,
                        published_at=None, metadata=None, context=None) -> str:
    """Caller holds transaction and publication authorization; contexts never update."""
    platform = _platform(platform)
    item_id = str(item_id or '').strip()
    if not item_id:
        raise ValueError('item_id is required')
    published_at = (_utc_time(published_at, field='published_at')
                    if published_at is not None and published_at != "" else None)
    metadata = _metadata(metadata)
    existing = db.execute('SELECT * FROM platform_publications WHERE platform=? AND item_id=?', (platform,item_id)).fetchone()
    if existing:
        if existing['job_id'] != job_id:
            raise ValueError(f'{platform} item {item_id} is already mapped to {existing["job_id"]}')
        db.execute('UPDATE platform_publications SET item_url=COALESCE(?,item_url),title=COALESCE(?,title),published_at=COALESCE(published_at,?),metadata_json=?,updated_at=CURRENT_TIMESTAMP WHERE publication_id=?',
            (item_url,title,published_at,json.dumps(metadata,ensure_ascii=False),existing['publication_id']))
        return existing['publication_id']
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_publication_current ON platform_publications(job_id,platform) WHERE is_current=1')
    identity = f'{job_id}\0{platform}\0{item_id}'
    pub_id = 'PUB-' + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
    db.execute('UPDATE platform_publications SET is_current=0 WHERE job_id=? AND platform=?',(job_id,platform))
    db.execute('INSERT INTO platform_publications(publication_id,job_id,platform,item_id,item_url,title,published_at,metadata_json,context_json) VALUES(?,?,?,?,?,?,?,?,?)',
        (pub_id,job_id,platform,item_id,item_url,title,published_at,json.dumps(metadata,ensure_ascii=False),json.dumps(context or {},ensure_ascii=False,allow_nan=False)))
    return pub_id


def _platform(value: str) -> str:
    normalized = str(value or "").strip().lower()
    normalized = PLATFORM_ALIASES.get(normalized, normalized)
    if normalized not in PLATFORM_EXPORT_FIELDS:
        raise ValueError(
            "platform must be douyin or weixin_channels"
        )
    return normalized


def _utc_time(value: str, *, field: str) -> str:
    if not isinstance(value,str):
        raise ValueError(f"{field} must be ISO-8601 with timezone")
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
                "WHERE publication_id=? ORDER BY captured_at",
                (publication["publication_id"],),
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


def register_publication(db, job_id, *, platform, item_id, item_url=None, title=None,
                         published_at=None, metadata=None) -> str:
    """Register a manual association; this is not verification of remote bytes."""
    from .node_state_machine import atomic
    job_id = str(job_id).strip()
    platform = _platform(platform)
    with atomic(db):
        existing = db.execute('SELECT * FROM platform_publications WHERE platform=? AND item_id=?',(platform,str(item_id).strip())).fetchone()
        if existing:
            if existing['job_id'] != job_id:
                raise ValueError(f'{platform} item {item_id} is already mapped to {existing["job_id"]}')
            return persist_publication(db,job_id,platform=platform,item_id=item_id,item_url=item_url,title=title,published_at=published_at,metadata=metadata)
        job = db.execute('SELECT state,publish_url FROM jobs WHERE job_id=?',(job_id,)).fetchone()
        if not job:
            raise ValueError(f'job {job_id} not found')
        if job['state'] != 'PUBLISHED' and not job['publish_url']:
            raise ValueError(f'job {job_id} is not published')
        context = capture_context(db, job_id)
        return persist_publication(db,job_id,platform=platform,item_id=item_id,
            item_url=item_url,title=title,published_at=published_at,metadata=metadata,context=context)


def _normalize_snapshot(
    db: sqlite3.Connection,
    job_id: str,
    *,
    platform: str,
    publication_id: str | None = None,
    captured_at: str | None = None,
    views: int | None = None,
    completed_views: int | None = None,
    completion_rate: float | None = None,
    average_watch_sec: float | None = None,
    likes: int | None = None,
    comments: int | None = None,
    shares: int | None = None,
    saves: int | None = None,
    follows: int | None = None,
    leads: int | None = None,
    metadata: dict | None = None,
) -> dict:
    """Validate and normalize one snapshot without mutating the database."""
    job_id = str(job_id).strip()
    platform = _platform(platform)
    publication = db.execute('SELECT * FROM platform_publications WHERE publication_id=?', (publication_id,)).fetchone() if publication_id else db.execute('SELECT * FROM platform_publications WHERE job_id=? AND platform=? AND is_current=1',(job_id,platform)).fetchone()
    if publication_id and not publication:
        raise ValueError('publication not found')
    if publication and (publication['job_id'] != job_id or publication['platform'] != platform):
        raise ValueError('publication does not match job/platform')
    if publication and str(content_review.object_json(publication['metadata_json']).get('review_status') or '').lower() in {'pending','pending_review','submitted'}:
        raise ValueError('publication is pending platform review')
    job = db.execute('SELECT state,publish_url FROM jobs WHERE job_id=?',(job_id,)).fetchone()
    if not job:
        raise ValueError(f'job {job_id} not found')
    if not publication and job['state'] != 'PUBLISHED' and not job['publish_url']:
        raise ValueError(f'job {job_id} is not published')
    metadata = _metadata(metadata)
    if 'observation_window_hours' in metadata:
        metadata['observation_window_hours'] = _number('observation_window_hours',metadata['observation_window_hours'])
        if not metadata['observation_window_hours']:
            raise ValueError('observation_window_hours must be > 0')
    for field in ('observation_start_at','observation_end_at'):
        if metadata.get(field) is not None:
            metadata[field] = _utc_time(metadata[field],field=field)
    if metadata.get('observation_start_at') and metadata.get('observation_end_at') and metadata['observation_start_at'] >= metadata['observation_end_at']:
        raise ValueError('observation window start must precede end')
    if captured_at is None or captured_at == "":
        captured_at = datetime.now(timezone.utc).isoformat()
    captured_at = _utc_time(captured_at, field="captured_at")

    values = {
        "views": views, "completed_views": completed_views, "likes": likes,
        "comments": comments, "shares": shares, "saves": saves,
        "follows": follows, "leads": leads,
    }
    counts = {name: _nonnegative(name, value) for name, value in values.items()}
    views = counts["views"]
    completed_views = counts["completed_views"]
    supplied_rate = _rate("completion_rate", completion_rate)
    derived_rate = (
        completed_views / views if completed_views is not None and views else None
    )
    if completed_views is not None and views is not None and completed_views > views:
        raise ValueError("completed_views must not exceed views")
    if (supplied_rate is not None and derived_rate is not None
            and abs(supplied_rate - derived_rate) > 0.0051):
        raise ValueError("completion_rate does not match completed_views/views")
    completion_rate = derived_rate if derived_rate is not None else supplied_rate
    average_watch_sec = _number('average_watch_sec',average_watch_sec)
    if metadata.get('observation_end_at') and metadata['observation_end_at'] > captured_at:
        raise ValueError('observation window ends after capture')
    if all(metadata.get(field) for field in ('observation_start_at','observation_end_at','observation_window_hours')):
        hours = (datetime.fromisoformat(metadata['observation_end_at']) -
                 datetime.fromisoformat(metadata['observation_start_at'])).total_seconds() / 3600
        if abs(hours - metadata['observation_window_hours']) > 1 / 3600:
            raise ValueError('observation_window_hours does not match start/end')
    publication_id = publication['publication_id'] if publication else None
    observation_key = publication_id or 'unbound'
    context = content_review.object_json(publication['context_json']) if publication else capture_context(db,job_id)
    metadata.update(provided_fields=[f for f,v in {**counts,'completion_rate':supplied_rate,'average_watch_sec':average_watch_sec}.items() if v is not None],provenance='recorded_measurement')
    identity = f"{job_id}\0{platform}\0{observation_key}\0{captured_at}"
    return {
        "snapshot_id": "PERF-" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()[:16],
        "job_id": job_id, "platform": platform, "captured_at": captured_at,
        "publication_id": publication_id, "observation_key": observation_key,
        "measurement_version": 1, "context_json": json.dumps(context,ensure_ascii=False,allow_nan=False),
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


def _write_snapshot(db: sqlite3.Connection, snapshot: dict) -> str:
    # Replace measurements at one item/instant; preserve immutable captured context.
    fields = list(snapshot)
    updates = [f for f in fields if f not in {'snapshot_id','job_id','platform','captured_at','publication_id','observation_key','context_json'}]
    db.execute(f"INSERT INTO performance_snapshots({','.join(fields)}) VALUES({','.join('?' for _ in fields)}) "
        "ON CONFLICT(job_id,platform,observation_key,captured_at) DO UPDATE SET " +
        ','.join(f'{f}=excluded.{f}' for f in updates) + ',updated_at=CURRENT_TIMESTAMP', tuple(snapshot.values()))
    return db.execute('SELECT snapshot_id FROM performance_snapshots WHERE job_id=? AND platform=? AND observation_key=? AND captured_at=?',
        (snapshot['job_id'],snapshot['platform'],snapshot['observation_key'],snapshot['captured_at'])).fetchone()[0]


def record_snapshot(
    db: sqlite3.Connection,
    job_id: str,
    *,
    platform: str,
    publication_id: str | None = None,
    captured_at: str | None = None,
    views: int | None = None,
    completed_views: int | None = None,
    completion_rate: float | None = None,
    average_watch_sec: float | None = None,
    likes: int | None = None,
    comments: int | None = None,
    shares: int | None = None,
    saves: int | None = None,
    follows: int | None = None,
    leads: int | None = None,
    metadata: dict | None = None,
) -> str:
    """Upsert one cumulative platform snapshot for a published job."""
    snapshot = _normalize_snapshot(
        db, job_id, platform=platform, publication_id=publication_id, captured_at=captured_at, views=views,
        completed_views=completed_views, completion_rate=completion_rate,
        average_watch_sec=average_watch_sec, likes=likes, comments=comments,
        shares=shares, saves=saves, follows=follows, leads=leads,
        metadata=metadata,
    )
    persisted_id = _write_snapshot(db, snapshot)
    db.commit()
    return persisted_id


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
    return _nonnegative("CSV count", value)


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
                    db, job_id, platform=platform, publication_id=row.get("publication_id") or None,
                    captured_at=captured_at,
                    views=_csv_int(_field(row, "views"), default=None),
                    completed_views=_csv_int(
                        _field(row, "completed_views"), default=None
                    ),
                    completion_rate=_csv_rate(_field(row, "completion_rate")),
                    average_watch_sec=_csv_float(
                        _field(row, "average_watch_sec")
                    ),
                    likes=_csv_int(_field(row, "likes"), default=None),
                    comments=_csv_int(_field(row, "comments"), default=None),
                    shares=_csv_int(_field(row, "shares"), default=None),
                    saves=_csv_int(_field(row, "saves"), default=None),
                    follows=_csv_int(_field(row, "follows"), default=None),
                    leads=_csv_int(_field(row, "leads"), default=None),
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
            "SELECT 1 FROM performance_snapshots WHERE job_id=? AND platform=? AND observation_key=? AND captured_at=?",
            (snapshot["job_id"],snapshot["platform"],snapshot["observation_key"],snapshot["captured_at"]),
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
                    "SELECT job_id,publication_id FROM platform_publications "
                    "WHERE platform=? AND item_id=?", (platform, item_id),
                ).fetchone()
                if not publication:
                    raise ValueError(
                        f"platform item {item_id} is not mapped to a job"
                    )
                snapshot = _normalize_snapshot(
                    db, publication["job_id"], platform=platform, publication_id=publication["publication_id"],
                    captured_at=captured_at,
                    views=_csv_int(
                        _aliased_field(row, aliases["views"]), default=None
                    ),
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
                        _aliased_field(row, aliases["likes"]), default=None
                    ),
                    comments=_csv_int(
                        _aliased_field(row, aliases["comments"]), default=None
                    ),
                    shares=_csv_int(
                        _aliased_field(row, aliases["shares"]), default=None
                    ),
                    saves=_csv_int(
                        _aliased_field(row, aliases["saves"]), default=None
                    ),
                    follows=_csv_int(
                        _aliased_field(row, aliases["follows"]), default=None
                    ),
                    leads=_csv_int(
                        _aliased_field(row, aliases["leads"]), default=None
                    ),
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
            "SELECT 1 FROM performance_snapshots WHERE job_id=? AND platform=? AND observation_key=? AND captured_at=?",
            (snapshot["job_id"],snapshot["platform"],snapshot["observation_key"],snapshot["captured_at"]),
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
    return [project_snapshot(row) for row in db.execute(
        "SELECT p.* FROM performance_snapshots p WHERE " + platform_clause +
        "NOT EXISTS (SELECT 1 FROM performance_snapshots newer "
        "WHERE newer.job_id=p.job_id AND newer.platform=p.platform AND newer.observation_key=p.observation_key "
        "AND (newer.captured_at>p.captured_at OR "
        "(newer.captured_at=p.captured_at AND newer.updated_at>p.updated_at)))",
        params,
    ).fetchall()]


def _window_snapshots(
    db: sqlite3.Connection, *, platform: str | None, window_hours: float,
    min_window_coverage: float,
) -> tuple[list[sqlite3.Row], list[dict]]:
    platform_sql = "AND p.platform=? " if platform else ""
    params: list = [platform] if platform else []
    rows = db.execute(
        "SELECT p.*, pub.published_at FROM performance_snapshots p "
        "LEFT JOIN platform_publications pub ON pub.publication_id=p.publication_id "
        "WHERE 1=1 " + platform_sql +
        "ORDER BY p.job_id, p.platform, p.captured_at",
        params,
    ).fetchall()
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault((row["job_id"], row["platform"], row["observation_key"]), []).append(project_snapshot(row))
    selected: list[sqlite3.Row] = []
    excluded: list[dict] = []
    for (job_id, row_platform, observation_key), snapshots in groups.items():
        published_at = snapshots[0]["published_at"]
        if not published_at:
            excluded.append({
                "job_id": job_id, "platform": row_platform, "publication_id": snapshots[0]["publication_id"],
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
                "job_id": job_id, "platform": row_platform, "publication_id": snapshots[0]["publication_id"],
                "reason": "snapshot_before_publish_or_after_window",
            })
            continue
        coverage, snapshot = max(eligible, key=lambda item: item[0])
        if coverage < window_hours * min_window_coverage:
            excluded.append({
                "job_id": job_id, "platform": row_platform, "publication_id": snapshots[0]["publication_id"],
                "reason": "window_not_mature",
                "coverage_hours": round(coverage, 3),
            })
            continue
        selected.append(snapshot)
    return selected, excluded


def _band(score: float) -> str:
    lower = min(int(score // 10) * 10, 90)
    return "90-100" if lower == 90 else f"{lower}-{lower + 9}"


def _aggregate(rows) -> dict:
    rows = [project_snapshot(row) if 'measurement_version' in row.keys() and 'provided_fields' not in row.keys() else dict(row) for row in rows]
    coverage = {}
    view_rows = [r for r in rows if r.get('views') is not None]
    views = sum(r['views'] for r in view_rows) if view_rows else None
    coverage['views'] = {'snapshots': len(view_rows), 'views': views}
    result = {'views': views, 'snapshot_count': len(rows)}
    metrics = {'completion_rate': ('completion_rate',), 'average_watch_sec': ('average_watch_sec',),
        'engagement_rate': ('likes','comments','shares','saves'), 'follow_rate': ('follows',), 'lead_rate': ('leads',)}
    for metric, fields in metrics.items():
        eligible = [r for r in rows if r.get('views') is not None and all(r.get(f) is not None for f in fields)]
        denominator = sum(r['views'] for r in eligible)
        numerator = sum(r[fields[0]] * r['views'] if metric in {'completion_rate','average_watch_sec'} else sum(r[f] for f in fields) for r in eligible)
        result[metric] = numerator / denominator if denominator else None
        coverage[metric] = {'snapshots': len(eligible), 'views': denominator, 'total_snapshots': len(rows)}
        if metric != 'average_watch_sec':
            result[metric + '_bps'] = round(result[metric]*10000) if result[metric] is not None else None
    # Counts have independent measured coverage too; partial engagement is never filled in.
    for field in COUNT_FIELDS[1:]:
        known = [r for r in rows if r.get(field) is not None]
        result[field] = sum(r[field] for r in known) if known else None
        coverage[field] = {'snapshots': len(known), 'total_snapshots': len(rows)}
    result['coverage'] = coverage
    return result


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
    by_context = {}
    for row in snapshot_rows:
        by_context.setdefault((row['job_id'],row['observation_key']), []).append(row)

    creatives = []
    observations = []
    source_groups = {}
    band_rows = {}
    for (job_id, observation_key), snapshots in by_context.items():
        metrics = _aggregate(snapshots)
        context = content_review.object_json(snapshots[0].get('context_json'))
        observations.append({'job_id':job_id,'publication_id':snapshots[0]['publication_id'],
            'platform':snapshots[0]['platform'],'creative_provenance':context.get('creative_provenance','unknown'),**metrics})
        if metrics['views'] is None or metrics['views'] < min_views:
            continue
        if context.get('creative_provenance') != 'frozen_production_input' or not context.get('creative_ir'):
            excluded.append({'job_id': job_id,'platform': snapshots[0]['platform'],
                'publication_id': snapshots[0]['publication_id'],'reason': 'historical_creative_context_unknown'})
            continue
        score = context.get('source_score') if context.get('source_score_provenance') == 'frozen_production_input' else None
        creative = {'job_id':job_id,'publication_id':snapshots[0]['publication_id'],
            'creative_version': observation_key, 'topic_id':context.get('topic_id'),
            'brief_id':context.get('brief_id'),'selected_angle_index':context.get('selected_angle_index'),
            'selected_title':context.get('selected_title'),'source_score':score,
            'tags':context.get('tags') or {}, 'creative_context':context, **metrics}
        creatives.append(creative)
        if score is not None:
            band_rows.setdefault(_band(float(score)), []).extend(snapshots)
        sources = ((context.get('sources') or [])
                   if context.get('source_provenance','frozen_production_input') == 'frozen_production_input' else [])
        for source in sources:
            group = source_groups.setdefault(source['source_id'], {'source_id':source['source_id'],
                'title':source.get('title'),'snapshots':[],'creative_ids':set()})
            group['snapshots'].extend(snapshots)
            group['creative_ids'].add((job_id,observation_key))

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
            "Distribution observations are not visual quality or causal contribution. Source metrics are shared exposure."
        ),
        "creatives": creatives,
        "observations": observations,
        "sources": source_rows,
        "score_bands": score_bands,
        "excluded": sorted(
            excluded, key=lambda item: (item["job_id"], item["platform"])
        ),
    }
