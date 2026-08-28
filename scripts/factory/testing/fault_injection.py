"""Persistent one-shot crash injection for deterministic replay runs.

The module is deliberately inert unless ``FACTORY_PROVIDER_MODE=replay``.
Configured failpoints are persisted in the active SQLite database before the
crash is raised, so reopening the same isolated database does not crash at the
same boundary twice.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
import sqlite3


FAILPOINTS = frozenset({
    "after_provider_submission",
    "after_provider_completion_before_promotion",
    "after_handler_completion_before_node_update",
    "after_node_update_before_job_projection",
    "during_local_compose",
    "after_service_restart",
})


class InjectedCrash(BaseException):
    """Simulate process termination without entering normal error handling."""

    stable_code = "replay_injected_crash"

    def __init__(self, point: str, *, job_id: str, node_id: str = "") -> None:
        self.point = point
        self.job_id = job_id
        self.node_id = node_id
        super().__init__(f"replay crash at {point}: {job_id}/{node_id or '-'}")


def _configured_points() -> frozenset[str]:
    raw = str(os.environ.get("FACTORY_REPLAY_FAILPOINTS", "") or "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _ensure_table(database: sqlite3.Connection) -> None:
    database.execute(
        "CREATE TABLE IF NOT EXISTS replay_fault_injections("
        "job_id TEXT NOT NULL,node_id TEXT NOT NULL,point TEXT NOT NULL,"
        "invocation_count INTEGER NOT NULL DEFAULT 0,"
        "triggered_count INTEGER NOT NULL DEFAULT 0,last_at TEXT NOT NULL,"
        "PRIMARY KEY(job_id,node_id,point))"
    )
    database.commit()


def hit(
    database: sqlite3.Connection,
    point: str,
    *,
    job_id: str,
    node_id: str = "",
) -> bool:
    """Raise once for an armed replay failpoint; return ``False`` otherwise."""
    point = str(point or "").strip()
    if point not in FAILPOINTS:
        raise ValueError(f"unknown replay failpoint: {point}")
    if str(os.environ.get("FACTORY_PROVIDER_MODE", "live")).casefold() != "replay":
        return False
    if point not in _configured_points():
        return False
    job_id = str(job_id or "").strip()
    node_id = str(node_id or "").strip()
    if not job_id:
        raise ValueError("replay failpoint requires job_id")
    _ensure_table(database)
    timestamp = datetime.now(timezone.utc).isoformat()
    with database:
        database.execute(
            "INSERT INTO replay_fault_injections("
            "job_id,node_id,point,invocation_count,triggered_count,last_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(job_id,node_id,point) DO UPDATE SET "
            "invocation_count=invocation_count+1,last_at=excluded.last_at",
            (job_id, node_id, point, 1, 0, timestamp),
        )
        row = database.execute(
            "SELECT triggered_count FROM replay_fault_injections "
            "WHERE job_id=? AND node_id=? AND point=?",
            (job_id, node_id, point),
        ).fetchone()
        should_crash = int(row[0] or 0) == 0
        if should_crash:
            database.execute(
                "UPDATE replay_fault_injections SET triggered_count=1,last_at=? "
                "WHERE job_id=? AND node_id=? AND point=?",
                (timestamp, job_id, node_id, point),
            )
    if should_crash:
        raise InjectedCrash(point, job_id=job_id, node_id=node_id)
    return False


def stats(database: sqlite3.Connection, *, job_id: str) -> list[dict]:
    """Return durable counters for regression assertions and reports."""
    _ensure_table(database)
    rows = database.execute(
        "SELECT job_id,node_id,point,invocation_count,triggered_count,last_at "
        "FROM replay_fault_injections WHERE job_id=? ORDER BY point,node_id",
        (str(job_id),),
    ).fetchall()
    columns = (
        "job_id", "node_id", "point", "invocation_count",
        "triggered_count", "last_at",
    )
    return [dict(zip(columns, tuple(row))) for row in rows]
