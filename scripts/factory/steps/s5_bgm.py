"""s5: 生成或挑选 BGM。"""
import json
import os
from pathlib import Path
import secrets
import shutil

from .. import ASSETS_DIR, provider_executor as provider_tasks
from ..db import conn
from ..clients import music


def _current_bgm(job) -> Path | None:
    value = str(job["bgm_path"] or "").strip()
    path = Path(value) if value else None
    return path if path and path.is_file() else None


async def run(job_id: str, *, provider: str | None = None) -> None:
    """Generate BGM with a submit reservation and recoverable publish journal."""
    database = conn()
    job = database.execute(
        "SELECT * FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if not job:
        raise ValueError(f"video job not found: {job_id}")
    storyboard = json.loads(job["storyboard"] or "{}")
    bgm_hint = storyboard.get("bgm", {})
    resolved_provider = music.resolve_provider(explicit=provider)
    paid = music.may_submit_paid_work(resolved_provider)

    existing_bgm = _current_bgm(job)
    if existing_bgm:
        if paid:
            provider_tasks.assert_no_unresolved(database, job_id)
        return

    reservation_id = None
    remote_task_id = None
    metadata: dict = {}
    original_state = str(job["state"] or "SHOTS_OK")

    def task_created(task_id: str) -> None:
        nonlocal reservation_id, remote_task_id
        remote_task_id = str(task_id)
        provider_tasks.persist_remote_task_id(
            database,
            reservation_id,
            remote_task_id=remote_task_id,
            provider=resolved_provider,
            intent="bgm",
        )
        provider_tasks.promote_reservation(
            database,
            reservation_id,
            task_id=remote_task_id,
            job_id=job_id,
            shot_idx=0,
            provider=resolved_provider,
            intent="bgm",
        )
        reservation_id = None

    def task_updated(task_id: str, status: str, data: dict) -> None:
        normalized = str(status or "").strip().casefold()
        payload = data if isinstance(data, dict) else {}
        metadata["remote_status"] = normalized
        metadata["usage"] = (
            payload.get("usage") if isinstance(payload.get("usage"), dict)
            else {}
        )
        if normalized == "completed":
            return
        usage = dict(metadata["usage"])
        if payload.get("remote_status"):
            usage["remote_status"] = str(payload["remote_status"])
        cost = max(0, int(payload.get("cost_cents") or 0))
        delta = provider_tasks.upsert_final_call(
            database,
            task_id=str(task_id),
            job_id=job_id,
            shot_idx=0,
            provider=resolved_provider,
            intent="bgm",
            status=normalized,
            usage=usage,
            cost_cents=cost,
        )
        if delta:
            database.execute(
                "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id=?",
                (delta, job_id),
            )
        database.commit()

    if paid:
        reservation_id = provider_tasks.reserve_paid_submit(
            database,
            job_id=job_id,
            shot_idx=0,
            provider=resolved_provider,
            intent="bgm",
        )
    try:
        kwargs = {
            "style": bgm_hint.get("style", "lo-fi"),
            "duration_sec": 60,
            "bpm_range": tuple(bgm_hint.get("bpm_range", [80, 100])),
            "mood": bgm_hint.get("mood", "中性"),
            "provider": resolved_provider,
        }
        if paid:
            kwargs.update({
                "metadata_out": metadata,
                "on_task_created": task_created,
                "on_task_updated": task_updated,
            })
        audio = await music.generate(**kwargs)
    except BaseException as exc:
        pause_required = (
            str(getattr(exc, "pause_state", ""))
            == "PROVIDER_ACTION_REQUIRED"
        )
        post_ambiguous = bool(
            metadata.get("post_started")
            and not metadata.get("definitive_rejection")
        )
        if reservation_id:
            if pause_required or post_ambiguous:
                provider_tasks.mark_action_required(
                    database, reservation_id, message=str(exc)
                )
            else:
                provider_tasks.release_reservation(database, reservation_id)
        elif remote_task_id:
            provider_tasks.mark_action_required(
                database, remote_task_id, message=str(exc)
            )
        raise

    if reservation_id:
        provider_tasks.release_reservation(database, reservation_id)
    target = ASSETS_DIR / job_id / "bgm.mp3"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not paid:
        target.write_bytes(audio)
        cost = music.estimate_cost_cents(60, provider=resolved_provider)
        database.execute(
            "UPDATE jobs SET bgm_path=?,cost_cents=cost_cents+? "
            "WHERE job_id=?",
            (str(target), cost, job_id),
        )
        database.commit()
        return

    if not remote_task_id:
        raise music.MusicTaskUnknownError(
            "Suno 已返回音频但缺少可核对 task_id"
        )
    staging_dir = target.parent / "review" / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staging = staging_dir / (
        f".{target.name}.{secrets.token_hex(8)}.stage"
    )
    with staging.open("xb") as stream:
        stream.write(audio)
        stream.flush()
        os.fsync(stream.fileno())
    usage = dict(metadata.get("usage") or {})
    usage.update({
        "publish_staging_path": str(staging),
        "publish_target_path": str(target),
        "publish_resume_state": original_state,
        "publish_kind": resolved_provider,
    })
    cost = music.estimate_cost_cents(60, provider=resolved_provider)
    try:
        database.execute("BEGIN IMMEDIATE")
        delta = provider_tasks.upsert_final_call(
            database,
            task_id=remote_task_id,
            job_id=job_id,
            shot_idx=0,
            provider=resolved_provider,
            intent="bgm",
            status="action_required",
            usage=usage,
            cost_cents=cost,
        )
        updated = database.execute(
            "UPDATE jobs SET bgm_path=?,cost_cents=cost_cents+?,"
            "state='PROVIDER_ACTION_REQUIRED',error=? WHERE job_id=?",
            (
                str(target), delta,
                "Suno 音频已生成，等待原子发布暂存产物",
                job_id,
            ),
        )
        if int(updated.rowcount or 0) != 1:
            raise ValueError(f"video job not found: {job_id}")
        database.commit()
    except Exception:
        database.rollback()
        provider_tasks.mark_action_required(
            database, remote_task_id,
            message="Suno 音频已下载，但 BGM 落盘记账失败；请核对 task_id",
        )
        raise

    publish_copy = target.parent / (
        f".{target.name}.{secrets.token_hex(8)}.publish"
    )
    try:
        shutil.copy2(staging, publish_copy)
        with publish_copy.open("rb+") as stream:
            os.fsync(stream.fileno())
        publish_copy.replace(target)
        completed_usage = dict(metadata.get("usage") or {})
        database.execute("BEGIN IMMEDIATE")
        provider_tasks.upsert_final_call(
            database,
            task_id=remote_task_id,
            job_id=job_id,
            shot_idx=0,
            provider=resolved_provider,
            intent="bgm",
            status="completed",
            usage=completed_usage,
            cost_cents=cost,
        )
        restored = database.execute(
            "UPDATE jobs SET state=?,error=NULL WHERE job_id=? "
            "AND state='PROVIDER_ACTION_REQUIRED'",
            (original_state, job_id),
        )
        if int(restored.rowcount or 0) != 1:
            raise ValueError("BGM publish state changed concurrently")
        database.commit()
    except BaseException as exc:
        database.rollback()
        publish_copy.unlink(missing_ok=True)
        raise music.MusicTaskUnknownError(
            "Suno 音频已计费并暂存，发布结果待恢复；禁止重新提交",
            task_id=remote_task_id,
        ) from exc
    try:
        staging.unlink(missing_ok=True)
    except OSError:
        pass
