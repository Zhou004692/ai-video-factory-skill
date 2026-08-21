"""调度器。状态机驱动,失败可续跑,指数退避重试。"""
import asyncio
import csv
import importlib
import os
import uuid

from .db import conn, log
from .error_registry import describe_failure, describe_retry_exhausted
from .models import TRANSITIONS, TERMINAL_STATES
from . import recovery_controller
from .state_machine import transition


BATCH_EXCLUDED_STATES = TERMINAL_STATES


STEP_MODULES = {
    "s1_storyboard": "factory.steps.s1_storyboard",
    "s3_shots":      "factory.steps.s3_shots",
    "s5_bgm":        "factory.steps.s5_bgm",
    "s6_compose":    "factory.steps.s6_compose",
    "s7_publish":    "factory.steps.s7_publish",
}

MAX_CONCURRENT_JOBS  = int(os.getenv("MAX_CONCURRENT_JOBS", "4"))
MAX_CONCURRENT_SHOTS = int(os.getenv("MAX_CONCURRENT_SHOTS", "3"))
MAX_RETRIES          = int(os.getenv("MAX_RETRIES", "3"))

async def _run_step(job_id: str, step: str):
    mod = importlib.import_module(STEP_MODULES[step])
    return await mod.run(job_id)


async def _heartbeat_worker(job_id: str, lease_id: str) -> None:
    while True:
        await asyncio.sleep(10)
        heartbeat_db = conn()
        try:
            if not recovery_controller.record_heartbeat(
                heartbeat_db, job_id, lease_id
            ):
                return
        finally:
            heartbeat_db.close()


def _should_retry_step(error: BaseException) -> bool:
    """Paid quality/budget guards require explicit human action."""
    if bool(getattr(error, "non_retryable", False)):
        return False
    return describe_failure(error).retry_policy == "auto"


def _transition_event_for_step(step: str) -> str:
    return {
        "s1_storyboard": "preflight_passed",
        "s3_shots": "all_masters_completed",
        "s5_bgm": "bgm_completed",
        "s6_compose": "composed",
    }[step]


def _event_id(job_id: str, step: str, outcome: str) -> str:
    return f"{job_id}:{step}:{outcome}:{uuid.uuid4().hex}"


async def _process_job(job_id: str, sem: asyncio.Semaphore):
    async with sem:
        db = conn()
        while True:
            row = db.execute(
                "SELECT state, retries FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if not row:
                return
            state = row["state"]
            retries = row["retries"]

            if state in TERMINAL_STATES:
                return

            nxt = next(((s, n) for (cur, s, n) in TRANSITIONS if cur.value == state),
                       None)
            if nxt is None:
                return

            step, new_state = nxt
            heartbeat_task = None
            lease_id = ""
            try:
                log(db, job_id, step, "INFO", f"start {step}")
                if state == "PRODUCTION_RUNNING":
                    lease_id = uuid.uuid4().hex
                    if not recovery_controller.record_heartbeat(
                        db, job_id, lease_id
                    ):
                        return
                    heartbeat_task = asyncio.create_task(
                        _heartbeat_worker(job_id, lease_id)
                    )
                await _run_step(job_id, step)
                transition(
                    db,
                    job_id,
                    _transition_event_for_step(step),
                    event_id=_event_id(job_id, step, "completed"),
                    target_state=new_state.value,
                    updates={
                        "retries": 0,
                        "error": None,
                        "failure_code": None,
                    },
                )
                log(db, job_id, step, "INFO", f"done {step} → {new_state.value}")
            except Exception as e:
                outcome = describe_failure(e, stage=step)
                failure_code = outcome.code
                pause_state = str(getattr(e, "pause_state", "") or "").strip()
                if pause_state:
                    pause_error = outcome.user_message
                    event = (
                        "provider_paused"
                        if pause_state == "PROVIDER_ACTION_REQUIRED"
                        else "review_required"
                    )
                    transition(
                        db,
                        job_id,
                        event,
                        event_id=_event_id(job_id, step, "paused"),
                        target_state=pause_state,
                        updates={
                            "error": pause_error,
                            "failure_code": failure_code,
                            "production_worker_pid": None,
                        },
                    )
                    log(
                        db, job_id, step, "INFO",
                        f"{outcome.code}: {outcome.technical_summary}",
                    )
                    return
                if not _should_retry_step(e):
                    transition(
                        db,
                        job_id,
                        "failed",
                        event_id=_event_id(job_id, step, "failed"),
                        updates={
                            "error": outcome.user_message,
                            "failure_code": failure_code,
                        },
                    )
                    log(
                        db, job_id, step, "ERROR",
                        f"{outcome.code}: {outcome.technical_summary}",
                    )
                    return
                next_retry = retries + 1
                if next_retry >= MAX_RETRIES:
                    outcome = describe_retry_exhausted(outcome, stage=step)
                    failure_code = outcome.code
                    transition(
                        db,
                        job_id,
                        "failed",
                        event_id=_event_id(job_id, step, "retry-exhausted"),
                        updates={
                            "error": outcome.user_message,
                            "failure_code": failure_code,
                        },
                    )
                    log(
                        db, job_id, step, "ERROR",
                        f"{outcome.code}: {outcome.technical_summary}",
                    )
                    return
                db.execute(
                    "UPDATE jobs SET retries=retries+1, error=? WHERE job_id=?",
                    (outcome.user_message, job_id)
                )
                db.commit()
                log(
                    db, job_id, step, "WARN",
                    f"retry {next_retry}/{MAX_RETRIES}: "
                    f"{outcome.code}: {outcome.technical_summary}",
                )
                await asyncio.sleep(min(2 ** retries, 30))
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass


def _import_topics(csv_path: str, batch_id: str | None = None):
    db = conn()
    n_new = 0
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            topic_id = (row.get("topic_id") or "").strip()
            title = (row.get("title") or "").strip()
            if not topic_id or not title:
                continue
            job_id = f"J-{topic_id}"
            cur = db.execute(
                "INSERT OR IGNORE INTO jobs(job_id, topic_id, title, track, "
                "style, target_audience, priority, batch_id) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (job_id, topic_id, title,
                 row.get("track", "").strip(),
                 (row.get("style") or "").strip() or None,
                 row.get("target_audience", "").strip(),
                 int(row.get("priority") or 3),
                 batch_id)
            )
            n_new += cur.rowcount
    db.commit()
    return n_new


async def run_batch(csv_path: str, batch_id: str | None = None):
    n = _import_topics(csv_path, batch_id=batch_id)
    print(f"导入 {n} 条新主题")

    db = conn()
    excluded = tuple(sorted(BATCH_EXCLUDED_STATES))
    placeholders = ",".join("?" for _ in excluded)
    where = f"state NOT IN ({placeholders})"
    params: tuple = excluded
    if batch_id:
        where += " AND batch_id=?"
        params = (*params, batch_id)
    pending = db.execute(
        f"SELECT job_id FROM jobs WHERE {where} "
        "ORDER BY priority DESC, created_at ASC",
        params,
    ).fetchall()
    print(f"待处理 {len(pending)} 个 job")

    if not pending:
        return

    sem = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
    await asyncio.gather(*(_process_job(r["job_id"], sem) for r in pending),
                         return_exceptions=False)

    # 汇总
    rows = db.execute(
        "SELECT state, COUNT(*) c, COALESCE(SUM(cost_cents),0) cost "
        "FROM jobs GROUP BY state"
    ).fetchall()
    print("\n=== Summary ===")
    for r in rows:
        print(f"  {r['state']:15} {r['c']:4d}  RMB {r['cost']/100:.2f}")
