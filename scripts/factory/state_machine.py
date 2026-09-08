"""Atomic, idempotent state transitions for production jobs."""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Mapping


class InvalidTransitionError(ValueError):
    """Raised when an event cannot be applied to the current job state."""


@dataclass(frozen=True)
class TransitionResult:
    job_id: str
    event: str
    event_id: str
    previous_state: str
    state: str
    applied: bool


_EVENT_RULES: dict[str, tuple[frozenset[str], str | None]] = {
    "preflight_passed": (frozenset({"PENDING", "STORYBOARDED"}), "STORYBOARDED"),
    "queued": (frozenset({"STORYBOARDED", "FAILED"}), "PRODUCTION_QUEUED"),
    "production_started": (frozenset({"PRODUCTION_QUEUED"}), "PRODUCTION_RUNNING"),
    "worker_start_failed": (
        frozenset({"PRODUCTION_RUNNING"}), "PRODUCTION_QUEUED",
    ),
    "provider_paused": (
        frozenset({
            "PENDING", "STORYBOARDED", "PRODUCTION_QUEUED", "PRODUCTION_RUNNING",
            "SHOTS_OK", "BGM_OK", "FAILED",
        }),
        "PROVIDER_ACTION_REQUIRED",
    ),
    "budget_paused": (
        frozenset({
            "PENDING", "STORYBOARDED", "PRODUCTION_QUEUED", "PRODUCTION_RUNNING",
            "SHOTS_OK", "BGM_OK", "FAILED",
        }),
        "BUDGET_CONFIRMATION_REQUIRED",
    ),
    "budget_confirmed": (
        frozenset({"BUDGET_CONFIRMATION_REQUIRED"}), "PRODUCTION_QUEUED",
    ),
    "review_required": (
        frozenset({
            "STORYBOARDED", "PRODUCTION_RUNNING", "FAILED",
            "SHOTS_OK", "BGM_OK",
        }),
        "PRODUCTION_REVIEW_REQUIRED",
    ),
    "master_completed": (frozenset({"PRODUCTION_RUNNING"}), "PRODUCTION_RUNNING"),
    "all_masters_completed": (
        frozenset({
            "PRODUCTION_RUNNING", "STORYBOARDED", "CHARACTER_OK", "VOICE_OK",
        }), "SHOTS_OK",
    ),
    "local_post_started": (frozenset({"SHOTS_OK"}), "SHOTS_OK"),
    "bgm_completed": (frozenset({"SHOTS_OK"}), "BGM_OK"),
    "composed": (frozenset({"SHOTS_OK", "BGM_OK"}), "COMPOSED"),
    "failed": (
        frozenset({
            "PENDING", "STORYBOARDED", "PRODUCTION_QUEUED",
            "PRODUCTION_RUNNING", "SHOTS_OK", "VOICE_OK", "BGM_OK",
            "PROVIDER_ACTION_REQUIRED", "PRODUCTION_REVIEW_REQUIRED",
        }),
        "FAILED",
    ),
    "rejected": (
        frozenset({"PRODUCTION_REVIEW_REQUIRED", "REVIEW_DECIDING"}),
        "REJECTED",
    ),
    "reconciled": (
        frozenset({"PROVIDER_ACTION_REQUIRED"}), None,
    ),
    "resume_generation": (
        frozenset({"FAILED", "PROVIDER_ACTION_REQUIRED"}), "STORYBOARDED",
    ),
    "resume_review": (
        frozenset({"FAILED", "PROVIDER_ACTION_REQUIRED"}),
        "PRODUCTION_REVIEW_REQUIRED",
    ),
    "resume_local_post": (
        frozenset({"FAILED", "PROVIDER_ACTION_REQUIRED"}), "SHOTS_OK",
    ),
    "review_decided": (
        frozenset({"FAILED", "PRODUCTION_REVIEW_REQUIRED", "PROVIDER_ACTION_REQUIRED", "BUDGET_CONFIRMATION_REQUIRED"}), None,
    ),
    "rejection_requested": (
        frozenset({"FAILED", "PRODUCTION_REVIEW_REQUIRED", "PROVIDER_ACTION_REQUIRED", "BUDGET_CONFIRMATION_REQUIRED"}), None,
    ),
    "recovery_requeue": (
        frozenset({"PRODUCTION_RUNNING"}), "PRODUCTION_QUEUED",
    ),
    "recovery_provider": (
        frozenset({"PRODUCTION_RUNNING"}), "PROVIDER_ACTION_REQUIRED",
    ),
    "recovery_review": (
        frozenset({"PRODUCTION_RUNNING"}), "PRODUCTION_REVIEW_REQUIRED",
    ),
    "recovery_failed": (
        frozenset({"PRODUCTION_RUNNING"}), "FAILED",
    ),
    "recovery_required": (
        frozenset({"PRODUCTION_RUNNING"}), "RECOVERY_REQUIRED",
    ),
}

_DYNAMIC_EVENT_TARGETS: dict[str, frozenset[str]] = {
    "review_required": frozenset({
        "ANCHOR_REVIEW_REQUIRED", "PRODUCTION_REVIEW_REQUIRED",
    }),
    "reconciled": frozenset({
        "PRODUCTION_REVIEW_REQUIRED", "PRODUCTION_QUEUED", "BGM_OK",
        "SHOTS_OK", "FAILED", "REJECTED",
    }),
    "review_decided": frozenset({
        "FAILED", "BUDGET_CONFIRMATION_REQUIRED", "RECOVERY_REQUIRED",
        "PROVIDER_ACTION_REQUIRED", "PRODUCTION_REVIEW_REQUIRED",
        "PRODUCTION_QUEUED", "SHOTS_OK", "COMPOSED",
    }),
    "rejection_requested": frozenset({
        "REJECTED", "REJECTED_PENDING_RECONCILIATION",
    }),
}

_ALLOWED_UPDATE_COLUMNS = frozenset({
    "error", "failure_code", "retries", "production_worker_pid",
    "production_started_at", "bgm_path", "final_path", "cost_cents",
    "storyboard",
    "production_heartbeat_at", "worker_lease_id", "recovery_attempts",
})


def _ensure_schema(database: sqlite3.Connection) -> None:
    database.execute(
        "CREATE TABLE IF NOT EXISTS job_transitions ("
        "transition_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "event_id TEXT NOT NULL UNIQUE,"
        "job_id TEXT NOT NULL,"
        "event TEXT NOT NULL,"
        "from_state TEXT NOT NULL,"
        "to_state TEXT NOT NULL,"
        "payload_json TEXT NOT NULL DEFAULT '{}',"
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
        "FOREIGN KEY(job_id) REFERENCES jobs(job_id))"
    )
    database.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_transitions_job "
        "ON job_transitions(job_id, transition_id)"
    )


def transition(
    database: sqlite3.Connection,
    job_id: str,
    event: str,
    *,
    event_id: str,
    target_state: str | None = None,
    updates: Mapping[str, object] | None = None,
    payload: Mapping[str, object] | None = None,
) -> TransitionResult:
    """Apply one job event and its business fields in a single transaction."""
    if not event_id or not str(event_id).strip():
        raise ValueError("event_id is required")
    if event not in _EVENT_RULES:
        raise InvalidTransitionError(f"unknown transition event: {event}")
    invalid_columns = set(updates or {}) - _ALLOWED_UPDATE_COLUMNS
    if invalid_columns:
        raise ValueError(
            "unsupported transition updates: " + ", ".join(sorted(invalid_columns))
        )
    owns_transaction = not database.in_transaction
    savepoint = "production_state_transition"
    if owns_transaction:
        database.execute("BEGIN IMMEDIATE")
    else:
        database.execute(f"SAVEPOINT {savepoint}")
    try:
        _ensure_schema(database)
        existing = database.execute(
            "SELECT job_id,event,from_state,to_state FROM job_transitions "
            "WHERE event_id=?", (str(event_id),),
        ).fetchone()
        if existing is not None:
            if existing["job_id"] != job_id or existing["event"] != event:
                raise InvalidTransitionError("event_id belongs to another transition")
            if owns_transaction:
                database.commit()
            else:
                database.execute(f"RELEASE SAVEPOINT {savepoint}")
            return TransitionResult(
                job_id=job_id,
                event=event,
                event_id=str(event_id),
                previous_state=str(existing["from_state"]),
                state=str(existing["to_state"]),
                applied=False,
            )
        row = database.execute(
            "SELECT state FROM jobs WHERE job_id=?", (job_id,),
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        current = str(row["state"])
        allowed, default_target = _EVENT_RULES[event]
        if current not in allowed:
            raise InvalidTransitionError(
                f"event {event} is invalid from state {current}"
            )
        resolved_target = str(target_state or default_target or "").strip()
        if not resolved_target:
            raise InvalidTransitionError(f"event {event} requires target_state")
        allowed_targets = _DYNAMIC_EVENT_TARGETS.get(event)
        if allowed_targets is None:
            allowed_targets = frozenset({str(default_target)})
        if resolved_target not in allowed_targets:
            raise InvalidTransitionError(
                f"event {event} cannot transition to {resolved_target}"
            )
        assignments = ["state=?", "updated_at=CURRENT_TIMESTAMP"]
        values: list[object] = [resolved_target]
        for column, value in (updates or {}).items():
            assignments.append(f"{column}=?")
            values.append(value)
        values.extend((job_id, current))
        cursor = database.execute(
            f"UPDATE jobs SET {','.join(assignments)} "
            "WHERE job_id=? AND state=?",
            values,
        )
        if cursor.rowcount != 1:
            raise InvalidTransitionError("job state changed concurrently")
        database.execute(
            "INSERT INTO job_transitions("
            "event_id,job_id,event,from_state,to_state,payload_json"
            ") VALUES(?,?,?,?,?,?)",
            (
                str(event_id), job_id, event, current, resolved_target,
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        if owns_transaction:
            database.commit()
        else:
            database.execute(f"RELEASE SAVEPOINT {savepoint}")
        return TransitionResult(
            job_id=job_id,
            event=event,
            event_id=str(event_id),
            previous_state=current,
            state=resolved_target,
            applied=True,
        )
    except BaseException:
        if owns_transaction:
            database.rollback()
        else:
            database.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            database.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
