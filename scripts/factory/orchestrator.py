"""调度器。状态机驱动,失败可续跑,指数退避重试。"""
import asyncio
import csv
import importlib
import os
import traceback

from .db import conn, log
from .models import TRANSITIONS, TERMINAL_STATES


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

STABLE_FAILURE_CODES = {
    "request_invalid", "input_privacy_rejected",
    "output_policy_rejected", "output_copyright_rejected",
    "account_unavailable", "transient_network",
}


def _stable_failure_code(error: BaseException) -> str | None:
    code = str(getattr(error, "stable_code", "") or "").strip()
    return code if code in STABLE_FAILURE_CODES else None


async def _run_step(job_id: str, step: str):
    mod = importlib.import_module(STEP_MODULES[step])
    return await mod.run(job_id)


def _should_retry_step(error: BaseException) -> bool:
    """Paid quality/budget guards require explicit human action."""
    return not bool(getattr(error, "non_retryable", False))


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
            try:
                log(db, job_id, step, "INFO", f"start {step}")
                await _run_step(job_id, step)
                db.execute(
                    "UPDATE jobs SET state=?, updated_at=CURRENT_TIMESTAMP, "
                    "retries=0, error=NULL, failure_code=NULL WHERE job_id=?",
                    (new_state.value, job_id)
                )
                db.commit()
                log(db, job_id, step, "INFO", f"done {step} → {new_state.value}")
            except Exception as e:
                tb = traceback.format_exc(limit=4)
                failure_code = _stable_failure_code(e)
                pause_state = str(getattr(e, "pause_state", "") or "").strip()
                if pause_state:
                    pause_error = (
                        str(e)[:2000]
                        if pause_state in {
                            "PRODUCTION_REVIEW_REQUIRED",
                            "PROVIDER_ACTION_REQUIRED",
                        }
                        else None
                    )
                    db.execute(
                        "UPDATE jobs SET state=?,error=?,failure_code=?,"
                        "production_worker_pid=NULL,updated_at=CURRENT_TIMESTAMP "
                        "WHERE job_id=?",
                        (pause_state, pause_error, failure_code, job_id),
                    )
                    db.commit()
                    log(db, job_id, step, "INFO", f"paused for review: {e}")
                    return
                if not _should_retry_step(e):
                    db.execute(
                        "UPDATE jobs SET state='FAILED',error=?,failure_code=? "
                        "WHERE job_id=?",
                        (f"{step}: {e}", failure_code, job_id)
                    )
                    db.commit()
                    log(db, job_id, step, "ERROR",
                        f"stopped without step retry: {e}\n{tb}")
                    return
                next_retry = retries + 1
                if next_retry >= MAX_RETRIES:
                    db.execute(
                        "UPDATE jobs SET state='FAILED',error=?,failure_code=? "
                        "WHERE job_id=?",
                        (f"{step}: {e}", failure_code, job_id)
                    )
                    db.commit()
                    log(db, job_id, step, "ERROR",
                        f"permanently failed: {e}\n{tb}")
                    return
                db.execute(
                    "UPDATE jobs SET retries=retries+1, error=? WHERE job_id=?",
                    (str(e), job_id)
                )
                db.commit()
                log(db, job_id, step, "WARN",
                    f"retry {next_retry}/{MAX_RETRIES}: {e}")
                await asyncio.sleep(min(2 ** retries, 30))


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
