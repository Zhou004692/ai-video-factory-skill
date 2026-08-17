"""Production-economics measurements based on approved output."""
from __future__ import annotations

import json
import sqlite3


REVIEW_DECISIONS = {"approved", "local_recut", "paid_regen", "rejected"}


def record_review(
    db: sqlite3.Connection,
    job_id: str,
    decision: str,
    *,
    manual_minutes: float = 0,
    notes: str | None = None,
    first_pass: bool | None = None,
    checks: dict[str, bool] | None = None,
) -> None:
    if decision not in REVIEW_DECISIONS:
        raise ValueError(
            f"decision must be one of {sorted(REVIEW_DECISIONS)}, got {decision!r}"
        )
    if manual_minutes < 0:
        raise ValueError("manual_minutes must be >= 0")
    if not db.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone():
        raise ValueError(f"job {job_id} not found")
    db.execute(
        "INSERT INTO review_events(job_id, decision, manual_minutes, notes, "
        "first_pass, checks_json) VALUES(?,?,?,?,?,?)",
        (
            job_id,
            decision,
            float(manual_minutes),
            notes,
            None if first_pass is None else int(first_pass),
            json.dumps(checks or {}, ensure_ascii=False),
        ),
    )
    db.commit()


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _scope(batch_id: str | None, since: str | None) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    if batch_id:
        clauses.append("batch_id=?")
        params.append(batch_id)
    if since:
        clauses.append("created_at>=?")
        params.append(since)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return f"SELECT job_id FROM jobs{where}", params


def summarize(
    db: sqlite3.Connection,
    *,
    batch_id: str | None = None,
    since: str | None = None,
) -> dict[str, float | int | str | None]:
    """Return metrics with one shared job cohort for every numerator."""
    scoped_jobs, params = _scope(batch_id, since)
    created = int(db.execute(
        f"SELECT COUNT(*) FROM ({scoped_jobs})", params
    ).fetchone()[0])
    approved = int(db.execute(
        "SELECT COUNT(DISTINCT job_id) FROM review_events "
        f"WHERE decision='approved' AND job_id IN ({scoped_jobs})",
        params,
    ).fetchone()[0])
    model_cost = int(db.execute(
        "SELECT COALESCE(SUM(cost_cents), 0) FROM provider_calls "
        f"WHERE job_id IN ({scoped_jobs})",
        params,
    ).fetchone()[0])
    paid_calls = int(db.execute(
        "SELECT COUNT(*) FROM provider_calls "
        f"WHERE job_id IN ({scoped_jobs})",
        params,
    ).fetchone()[0])
    manual_minutes = float(db.execute(
        "SELECT COALESCE(SUM(manual_minutes), 0) FROM review_events "
        f"WHERE job_id IN ({scoped_jobs})",
        params,
    ).fetchone()[0])
    local_repairs = int(db.execute(
        "SELECT COUNT(*) FROM review_events WHERE decision='local_recut' "
        f"AND job_id IN ({scoped_jobs})",
        params,
    ).fetchone()[0])
    paid_repairs = int(db.execute(
        "SELECT COUNT(*) FROM review_events WHERE decision='paid_regen' "
        f"AND job_id IN ({scoped_jobs})",
        params,
    ).fetchone()[0])
    first_pass = int(db.execute(
        "SELECT COUNT(DISTINCT job_id) FROM review_events "
        "WHERE decision='approved' AND first_pass=1 "
        f"AND job_id IN ({scoped_jobs})",
        params,
    ).fetchone()[0])
    repairs = local_repairs + paid_repairs
    return {
        "batch_id": batch_id,
        "since": since,
        "created_jobs": created,
        "approved_jobs": approved,
        "qualified_yield_rate": _ratio(approved, created),
        "first_pass_rate": _ratio(first_pass, approved),
        "model_cost_cents": model_cost,
        "cost_per_approved_cents": int(model_cost / approved) if approved else 0,
        "manual_minutes": manual_minutes,
        "paid_calls_per_approved": _ratio(paid_calls, approved),
        "zero_cost_repair_rate": _ratio(local_repairs, repairs),
    }
