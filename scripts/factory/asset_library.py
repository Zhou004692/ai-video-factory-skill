"""Approved, rights-safe B-roll reuse for the local-director pipeline."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Iterable


REUSABLE_RIGHTS = {"owned", "licensed", "public_domain"}


def register_asset(
    db: sqlite3.Connection,
    *,
    asset_id: str,
    path: str | Path,
    tags: Iterable[str],
    duration_sec: float,
    orientation: str = "9:16",
    rights_status: str = "owned",
    approval_status: str = "approved",
) -> None:
    """Register or refresh an asset without losing its reuse counter."""
    clean_tags = sorted({str(tag).strip() for tag in tags if str(tag).strip()})
    db.execute(
        "INSERT INTO media_assets(asset_id, path, tags_json, duration_sec, "
        "orientation, rights_status, approval_status) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(asset_id) DO UPDATE SET path=excluded.path, "
        "tags_json=excluded.tags_json, duration_sec=excluded.duration_sec, "
        "orientation=excluded.orientation, rights_status=excluded.rights_status, "
        "approval_status=excluded.approval_status, "
        "updated_at=CURRENT_TIMESTAMP",
        (
            asset_id,
            str(Path(path)),
            json.dumps(clean_tags, ensure_ascii=False),
            float(duration_sec),
            orientation,
            rights_status,
            approval_status,
        ),
    )
    db.commit()


def _match_score(query_tags: set[str], asset_tags: set[str]) -> float:
    if not query_tags:
        return 0.0
    return len(query_tags & asset_tags) / len(query_tags)


def find_broll_match(
    db: sqlite3.Connection,
    *,
    tags: Iterable[str],
    min_duration_sec: float,
    orientation: str = "9:16",
):
    """Return the best eligible asset; no semantic guess means no paid mistake."""
    query_tags = {str(tag).strip() for tag in tags if str(tag).strip()}
    if not query_tags:
        return None
    rows = db.execute(
        "SELECT * FROM media_assets WHERE media_type='video' "
        "AND approval_status='approved' AND duration_sec>=? "
        "AND orientation IN (?, 'adaptive')",
        (float(min_duration_sec), orientation),
    ).fetchall()
    ranked = []
    for row in rows:
        if row["rights_status"] not in REUSABLE_RIGHTS:
            continue
        path = Path(row["path"])
        if not path.exists():
            continue
        score = _match_score(query_tags, set(json.loads(row["tags_json"] or "[]")))
        if score <= 0:
            continue
        ranked.append((score, -float(row["duration_sec"]), row))
    if not ranked:
        return None
    return max(ranked, key=lambda item: (item[0], item[1]))[2]


def list_assets(
    db: sqlite3.Connection,
    *,
    tags: Iterable[str] = (),
) -> list[dict]:
    """List approved reusable assets, optionally filtered by tag overlap."""
    query_tags = {str(tag).strip() for tag in tags if str(tag).strip()}
    rows = db.execute(
        "SELECT * FROM media_assets WHERE media_type='video' "
        "AND approval_status='approved' ORDER BY use_count DESC, asset_id"
    ).fetchall()
    results = []
    for row in rows:
        if row["rights_status"] not in REUSABLE_RIGHTS:
            continue
        item = dict(row)
        item["tags"] = json.loads(item.pop("tags_json") or "[]")
        if query_tags and not query_tags.intersection(item["tags"]):
            continue
        results.append(item)
    return results


def reuse_broll_assets(
    db: sqlite3.Connection,
    job_id: str,
    masters: Iterable[dict],
) -> dict[int, str]:
    """Resolve eligible B-roll masters locally and record an auditable usage."""
    reused: dict[int, str] = {}
    for master in masters:
        if master.get("type") != "broll":
            continue
        idx = int(master["idx"])
        shot = db.execute(
            "SELECT state FROM shots WHERE job_id=? AND idx=?",
            (job_id, idx),
        ).fetchone()
        if not shot or shot["state"] == "OK":
            continue
        tags = master.get("asset_tags") or []
        match = find_broll_match(
            db,
            tags=tags,
            min_duration_sec=float(master.get("duration") or 0),
            orientation=master.get("orientation") or "9:16",
        )
        if match is None:
            continue
        score = _match_score(set(tags), set(json.loads(match["tags_json"] or "[]")))
        inserted = db.execute(
            "INSERT OR IGNORE INTO asset_usages(asset_id, job_id, master_idx, "
            "match_score) VALUES(?,?,?,?)",
            (match["asset_id"], job_id, idx, score),
        ).rowcount
        if inserted:
            db.execute(
                "UPDATE media_assets SET use_count=use_count+1, "
                "updated_at=CURRENT_TIMESTAMP WHERE asset_id=?",
                (match["asset_id"],),
            )
        db.execute(
            "UPDATE shots SET state='OK', provider='asset_library', "
            "video_path=?, cost_cents=0, error=NULL WHERE job_id=? AND idx=?",
            (match["path"], job_id, idx),
        )
        reused[idx] = match["path"]
    db.commit()
    return reused
