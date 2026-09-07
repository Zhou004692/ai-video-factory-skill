"""Production-economics measurements based on approved output."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


REVIEW_DECISIONS = {"approved", "local_recut", "paid_regen", "rejected"}
DETERMINISTIC_FAILURE_CODES = frozenset({
    "request_invalid",
    "input_privacy_rejected",
    "output_policy_rejected",
    "output_copyright_rejected",
    "prompt_auto_repair_required",
    "music_configuration_invalid",
})
RECOVERY_SUCCESS_EVENTS = frozenset({
    "recovery_requeue", "recovery_provider", "recovery_review",
})
RECOVERY_ATTEMPT_EVENTS = RECOVERY_SUCCESS_EVENTS | frozenset({
    "recovery_failed", "recovery_required",
})
ENGINEERING_INTERVENTION_TYPES = frozenset({
    "database_edit", "standalone_script_restart", "manual_file_injection",
    "engineering_recovery_command",
})


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
    with db:
        record_review_in_transaction(
            db, job_id, decision,
            manual_minutes=manual_minutes,
            notes=notes,
            first_pass=first_pass,
            checks=checks,
        )


def record_review_in_transaction(
    db: sqlite3.Connection,
    job_id: str,
    decision: str,
    *,
    manual_minutes: float = 0,
    notes: str | None = None,
    first_pass: bool | None = None,
    checks: dict[str, bool] | None = None,
) -> None:
    """Record a review without committing the caller's transaction."""
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


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _scope(
    batch_id: str | None,
    since: str | None,
    until: str | None = None,
) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    if batch_id:
        clauses.append("batch_id=?")
        params.append(batch_id)
    if since:
        clauses.append("created_at>=?")
        params.append(since)
    if until:
        clauses.append("created_at<?")
        params.append(until)
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


def _usage(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _percentile(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def reliability_summary(
    database: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    now: datetime | None = None,
    stale_after_seconds: int = 120,
) -> dict[str, Any]:
    """Aggregate release-safety metrics from structured persisted evidence."""
    scoped_jobs, params = _scope(None, since, until)
    rows = database.execute(
        "SELECT job_id,state,cost_cents,failure_code,created_at,updated_at,"
        "production_started_at,production_heartbeat_at FROM jobs "
        f"WHERE job_id IN ({scoped_jobs})",
        params,
    ).fetchall()
    job_ids = {str(row["job_id"]) for row in rows}
    if job_ids:
        placeholders = ",".join("?" for _ in job_ids)
        job_params = sorted(job_ids)
        transitions = database.execute(
            "SELECT job_id,event,to_state FROM job_transitions "
            f"WHERE job_id IN ({placeholders})",
            job_params,
        ).fetchall()
        reviews = database.execute(
            "SELECT job_id,decision,first_pass FROM review_events "
            f"WHERE job_id IN ({placeholders})",
            job_params,
        ).fetchall()
        calls = database.execute(
            "SELECT job_id,cost_cents,usage_json FROM provider_calls "
            f"WHERE job_id IN ({placeholders})",
            job_params,
        ).fetchall()
    else:
        transitions, reviews, calls = [], [], []

    manual_review_jobs = {
        str(row["job_id"]) for row in transitions
        if str(row["event"]) == "review_required"
        or str(row["to_state"]) == "PRODUCTION_REVIEW_REQUIRED"
    }
    manual_review_jobs.update(
        str(row["job_id"]) for row in reviews
        if str(row["decision"]) in {"local_recut", "paid_regen", "rejected"}
    )
    first_pass_jobs = {
        str(row["job_id"]) for row in reviews
        if str(row["decision"]) == "approved" and int(row["first_pass"] or 0) == 1
    }
    eligible_jobs = job_ids - manual_review_jobs
    first_round_jobs = eligible_jobs & first_pass_jobs

    paid_jobs = {
        str(row["job_id"]) for row in calls if int(row["cost_cents"] or 0) > 0
    }
    deterministic_post_paid = {
        str(row["job_id"]) for row in rows
        if str(row["job_id"]) in paid_jobs
        and str(row["failure_code"] or "") in DETERMINISTIC_FAILURE_CODES
    }
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stale_cutoff = current_time - timedelta(seconds=max(1, stale_after_seconds))
    unknown_stuck: set[str] = set()
    for row in rows:
        state = str(row["state"] or "")
        if state == "RECOVERY_REQUIRED":
            unknown_stuck.add(str(row["job_id"]))
            continue
        if state not in {"PRODUCTION_QUEUED", "PRODUCTION_RUNNING"}:
            continue
        heartbeat = _parse_timestamp(
            row["production_heartbeat_at"] or row["production_started_at"]
        )
        if heartbeat is not None and heartbeat < stale_cutoff:
            unknown_stuck.add(str(row["job_id"]))

    recovery_events = [
        str(row["event"]) for row in transitions
        if str(row["event"]) in RECOVERY_ATTEMPT_EVENTS
    ]
    recovery_successes = sum(
        event in RECOVERY_SUCCESS_EVENTS for event in recovery_events
    )
    duplicates = sum(
        bool(_usage(row["usage_json"]).get("duplicate_submission"))
        for row in calls
        if int(row["cost_cents"] or 0) > 0
    )

    durations = []
    costs = []
    for row in rows:
        created = _parse_timestamp(row["created_at"])
        updated = _parse_timestamp(row["updated_at"])
        if created is not None and updated is not None:
            durations.append(max(0.0, (updated - created).total_seconds()))
        costs.append(float(row["cost_cents"] or 0))

    return {
        "since": since,
        "until": until,
        "created_jobs": len(job_ids),
        "first_round_eligible_jobs": len(eligible_jobs),
        "first_round_progression_rate": _ratio(
            len(first_round_jobs), len(eligible_jobs)
        ),
        "deterministic_post_paid_failures": len(deterministic_post_paid),
        "unknown_stuck_jobs": len(unknown_stuck),
        "recovery_success_rate": _ratio(recovery_successes, len(recovery_events)),
        "manual_review_rate": _ratio(len(manual_review_jobs), len(job_ids)),
        "duplicate_paid_submissions": int(duplicates),
        "duration_seconds": {
            "p50": round(_percentile(durations, 0.50), 3),
            "p95": round(_percentile(durations, 0.95), 3),
        },
        "cost_cents": {
            "p50": int(round(_percentile(costs, 0.50))),
            "p95": int(round(_percentile(costs, 0.95))),
        },
    }


def reliability_trends(
    database: sqlite3.Connection,
    *,
    days: int = 30,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return non-overlapping UTC daily reliability summaries."""
    if days <= 0:
        raise ValueError("days must be > 0")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    result: list[dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        start = today - timedelta(days=offset)
        end = start + timedelta(days=1)
        report = reliability_summary(
            database,
            since=start.strftime("%Y-%m-%d %H:%M:%S"),
            until=end.strftime("%Y-%m-%d %H:%M:%S"),
            now=current,
        )
        report["date"] = start.date().isoformat()
        result.append(report)
    return result


def record_engineering_intervention(
    database: sqlite3.Connection,
    job_id: str,
    intervention_type: str,
    *,
    description: str = "",
) -> None:
    if intervention_type not in ENGINEERING_INTERVENTION_TYPES:
        raise ValueError("unknown engineering intervention type")
    database.execute(
        "INSERT INTO production_interventions("
        "job_id,intervention_type,description) VALUES(?,?,?)",
        (job_id, intervention_type, str(description or "")),
    )
    database.commit()


def production_v2_summary(database: sqlite3.Connection) -> dict[str, Any]:
    """Release metrics for graph_v2 jobs and their latest compilation mode."""
    jobs = database.execute(
        "SELECT job_id,state,cost_cents,failure_code,created_at,updated_at,"
        "production_started_at,production_heartbeat_at "
        "FROM jobs WHERE execution_engine='graph_v2' ORDER BY created_at,job_id"
    ).fetchall()
    job_ids = [str(row["job_id"]) for row in jobs]
    if not job_ids:
        return {
            "total_real_jobs": 0,
            "compilation_success_rate": 0.0,
            "first_pass_usable_rate": 0.0,
            "unknown_stuck_jobs": 0,
            "deterministic_runtime_escape": 0,
            "manual_review_rate": 0.0,
            "recovery_success_rate": 1.0,
            "duplicate_paid_submissions": 0,
            "cost_variance_cents": 0,
            "duration_seconds": {"p50": 0.0, "p95": 0.0},
            "modes": {},
            "switch_gate": {"eligible": False, "failed": ["total_real_jobs"]},
        }
    placeholders = ",".join("?" for _ in job_ids)
    compilation_rows = database.execute(
        "SELECT c.job_id,c.status,c.mode FROM production_compilation_runs c "
        "JOIN (SELECT job_id,MAX(run_id) run_id FROM production_compilation_runs "
        f"WHERE job_id IN ({placeholders}) GROUP BY job_id) latest "
        "ON latest.run_id=c.run_id",
        job_ids,
    ).fetchall()
    compilation = {str(row["job_id"]): row for row in compilation_rows}
    reviews = database.execute(
        "SELECT job_id,decision,first_pass FROM review_events "
        f"WHERE job_id IN ({placeholders}) ORDER BY id",
        job_ids,
    ).fetchall()
    review_by_job: dict[str, list[sqlite3.Row]] = {}
    for row in reviews:
        review_by_job.setdefault(str(row["job_id"]), []).append(row)
    interventions = {
        str(row["job_id"]) for row in database.execute(
            "SELECT DISTINCT job_id FROM production_interventions "
            f"WHERE job_id IN ({placeholders})",
            job_ids,
        ).fetchall()
    }
    calls = database.execute(
        "SELECT job_id,usage_json FROM provider_calls "
        f"WHERE job_id IN ({placeholders})",
        job_ids,
    ).fetchall()
    transitions = database.execute(
        "SELECT job_id,event FROM job_transitions "
        f"WHERE job_id IN ({placeholders})",
        job_ids,
    ).fetchall()
    recovery_events = [
        str(row["event"]) for row in transitions
        if str(row["event"]) in RECOVERY_ATTEMPT_EVENTS
    ]
    recovery_successes = sum(
        event in RECOVERY_SUCCESS_EVENTS for event in recovery_events
    )
    reviewed_jobs = set(review_by_job)
    first_pass_usable = {
        job_id for job_id, job_reviews in review_by_job.items()
        if job_id not in interventions and any(
            str(row["decision"]) == "approved"
            and int(row["first_pass"] or 0) == 1
            for row in job_reviews
        )
    }
    stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=120)
    unknown_stuck = 0
    for row in jobs:
        state = str(row["state"] or "")
        if state == "RECOVERY_REQUIRED":
            unknown_stuck += 1
        elif state == "PRODUCTION_RUNNING":
            heartbeat = _parse_timestamp(
                row["production_heartbeat_at"] or row["production_started_at"]
            )
            if heartbeat is not None and heartbeat < stale_cutoff:
                unknown_stuck += 1
    deterministic_escapes = sum(
        str(row["failure_code"] or "") in DETERMINISTIC_FAILURE_CODES
        for row in jobs
    )
    duplicate_submissions = sum(
        bool(_usage(row["usage_json"]).get("duplicate_submission"))
        for row in calls
    )
    estimated = int(database.execute(
        "SELECT COALESCE(SUM(estimated_cost_cents),0) FROM production_nodes "
        f"WHERE job_id IN ({placeholders}) AND paid=1",
        job_ids,
    ).fetchone()[0])
    actual = sum(int(row["cost_cents"] or 0) for row in jobs)
    durations = []
    for row in jobs:
        created = _parse_timestamp(row["created_at"])
        updated = _parse_timestamp(row["updated_at"])
        if created and updated:
            durations.append(max(0.0, (updated - created).total_seconds()))
    modes: dict[str, dict[str, object]] = {}
    for mode in ("stable", "enhanced", "experimental"):
        mode_jobs = {
            job_id for job_id, row in compilation.items()
            if str(row["mode"] or "") == mode
        }
        mode_reviewed = mode_jobs & reviewed_jobs
        explicit_decisions = {
            job_id for job_id in mode_reviewed if review_by_job.get(job_id)
        }
        modes[mode] = {
            "jobs": len(mode_jobs),
            "first_pass_usable_rate": _ratio(
                len(mode_jobs & first_pass_usable), len(mode_reviewed)
            ),
            "explicit_human_decision_rate": _ratio(
                len(explicit_decisions), len(mode_jobs)
            ),
        }
    report = {
        "total_real_jobs": len(job_ids),
        "compilation_success_rate": _ratio(
            sum(
                str(row["status"]) in {"match", "compatible_difference"}
                for row in compilation_rows
            ),
            len(job_ids),
        ),
        "first_pass_usable_rate": _ratio(
            len(first_pass_usable), len(reviewed_jobs)
        ),
        "unknown_stuck_jobs": int(unknown_stuck),
        "deterministic_runtime_escape": int(deterministic_escapes),
        "manual_review_rate": _ratio(len(reviewed_jobs), len(job_ids)),
        "recovery_success_rate": (
            _ratio(recovery_successes, len(recovery_events))
            if recovery_events else 1.0
        ),
        "duplicate_paid_submissions": int(duplicate_submissions),
        "cost_variance_cents": actual - estimated,
        "duration_seconds": {
            "p50": round(_percentile(durations, 0.50), 3),
            "p95": round(_percentile(durations, 0.95), 3),
        },
        "modes": modes,
    }
    checks = {
        "total_real_jobs": report["total_real_jobs"] >= 10,
        "unknown_stuck_jobs": report["unknown_stuck_jobs"] == 0,
        "deterministic_runtime_escape": report["deterministic_runtime_escape"] == 0,
        "duplicate_paid_submissions": report["duplicate_paid_submissions"] == 0,
        "recovery_success_rate": report["recovery_success_rate"] == 1.0,
        "stable_first_pass_usable_rate": modes["stable"]["first_pass_usable_rate"] >= 0.90,
        "enhanced_first_pass_usable_rate": modes["enhanced"]["first_pass_usable_rate"] >= 0.75,
        "experimental_jobs_have_explicit_human_decision": modes["experimental"]["explicit_human_decision_rate"] == 1.0,
    }
    report["switch_gate"] = {
        "eligible": all(checks.values()),
        "checks": checks,
        "failed": [name for name, passed in checks.items() if not passed],
    }
    return report
