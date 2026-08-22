"""Shared safety and cleanup rules for paid master review candidates."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3


def controlled_job_asset_path(
    assets_dir: Path,
    job_id: str,
    raw_path: object,
    *,
    require_file: bool = False,
) -> Path | None:
    """Resolve a path only when it is contained by this job's asset root."""
    value = str(raw_path or "").strip()
    if not value:
        return None
    candidate = Path(value).resolve()
    allowed_root = (Path(assets_dir) / str(job_id)).resolve()
    if not candidate.is_relative_to(allowed_root):
        return None
    if require_file and not candidate.is_file():
        return None
    return candidate


def controlled_candidate_path(
    assets_dir: Path, job_id: str, raw_path: object
) -> Path | None:
    """Return a real local candidate contained by this job's asset directory."""
    return controlled_job_asset_path(
        assets_dir, job_id, raw_path, require_file=True
    )


def finalize_accepted_candidates(
    database: sqlite3.Connection, assets_dir: Path, job_id: str
) -> int:
    """Finalize accepted-candidate audit after successful local production."""
    rows = database.execute(
        "SELECT idx,video_path,video_qa FROM shots WHERE job_id=? ORDER BY idx",
        (job_id,),
    ).fetchall()
    finalized = 0
    for row in rows:
        try:
            qa = json.loads(row["video_qa"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if qa.get("status") != "candidate_accepted_pending":
            continue
        active = controlled_job_asset_path(
            assets_dir, job_id, row["video_path"], require_file=True
        )
        if active is None:
            continue
        cleanup_paths: set[Path] = set()
        unsafe_reference = False
        for key in ("candidate_path", "prior_backup_path"):
            raw_path = str(qa.get(key) or "").strip()
            if not raw_path:
                continue
            path = controlled_job_asset_path(assets_dir, job_id, raw_path)
            if path is None:
                unsafe_reference = True
                break
            cleanup_paths.add(path)
            sidecar = controlled_job_asset_path(
                assets_dir, job_id, path.with_suffix(".wav")
            )
            if sidecar is None:
                unsafe_reference = True
                break
            cleanup_paths.add(sidecar)
        operation = qa.get("review_operation")
        if isinstance(operation, dict):
            for key in (
                "candidate_path", "prior_backup_path",
                "prior_audio_backup_path", "prepared_video_path",
                "prepared_audio_path",
            ):
                raw_path = str(operation.get(key) or "").strip()
                if not raw_path:
                    continue
                path = controlled_job_asset_path(assets_dir, job_id, raw_path)
                if path is None:
                    unsafe_reference = True
                    break
                cleanup_paths.add(path)
        if unsafe_reference:
            continue
        try:
            for path in cleanup_paths:
                if path != active:
                    path.unlink(missing_ok=True)
                if path != active and path.exists():
                    raise OSError(f"failed to remove review audit asset: {path}")
        except OSError:
            continue
        for key in (
            "candidate_path", "candidate_url", "prior_backup_path",
            "prior_video_url", "review_operation",
        ):
            qa.pop(key, None)
        qa["status"] = "candidate_accepted"
        database.execute(
            "UPDATE shots SET video_qa=? WHERE job_id=? AND idx=?",
            (json.dumps(qa, ensure_ascii=False), job_id, int(row["idx"])),
        )
        finalized += 1
    if finalized:
        database.commit()
    return finalized
