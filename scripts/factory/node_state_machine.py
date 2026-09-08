"""Idempotent state transitions for persisted production nodes."""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Mapping


NODE_STATES = frozenset({
    "PENDING", "READY", "RUNNING", "SUCCEEDED", "REVIEW_REQUIRED",
    "ACTION_REQUIRED", "BUDGET_REQUIRED", "FAILED", "SKIPPED",
})

_TRANSITIONS = {
    ("READY", "start"): "RUNNING",
    ("RUNNING", "succeed"): "SUCCEEDED",
    ("RUNNING", "require_review"): "REVIEW_REQUIRED",
    ("REVIEW_REQUIRED", "approve"): "SUCCEEDED",
    ("REVIEW_REQUIRED", "regenerate"): "READY",
    # Approving an unexpected replacement identity unblocks an unfinished
    # master; it does not approve a generated master candidate.
    ("REVIEW_REQUIRED", "resume_anchor_dependency"): "READY",
    ("REVIEW_REQUIRED", "reject"): "FAILED",
    ("RUNNING", "require_action"): "ACTION_REQUIRED",
    ("ACTION_REQUIRED", "resume"): "READY",
    ("RUNNING", "require_budget"): "BUDGET_REQUIRED",
    ("BUDGET_REQUIRED", "resume"): "READY",
    # A provider result can arrive after a local pause was persisted. The paid
    # remote artifact is authoritative and must converge to human review rather
    # than remain stranded behind the obsolete pause state.
    ("BUDGET_REQUIRED", "reconcile_review"): "REVIEW_REQUIRED",
    ("ACTION_REQUIRED", "reconcile_review"): "REVIEW_REQUIRED",
    ("FAILED", "reconcile_review"): "REVIEW_REQUIRED",
    ("RUNNING", "reconcile_review"): "REVIEW_REQUIRED",
    ("READY", "reconcile_review"): "REVIEW_REQUIRED",
    ("RUNNING", "fail"): "FAILED",
    ("READY", "skip"): "SKIPPED",
    ("FAILED", "retry"): "READY",
}


@dataclass(frozen=True)
class TransitionResult:
    applied: bool
    state: str


def _promote_dependents(database: sqlite3.Connection, job_id: str) -> None:
    pending = database.execute(
        "SELECT node_id FROM production_nodes WHERE job_id=? AND state='PENDING'",
        (job_id,),
    ).fetchall()
    for row in pending:
        node_id = row["node_id"]
        dependencies = database.execute(
            "SELECT e.edge_type,n.state FROM production_edges e "
            "JOIN production_nodes n ON n.job_id=e.job_id "
            "AND n.node_id=e.from_node_id "
            "WHERE e.job_id=? AND e.to_node_id=?",
            (job_id, node_id),
        ).fetchall()
        ready = bool(dependencies) and all(
            dependency["state"] == "SUCCEEDED"
            or (
                dependency["state"] == "SKIPPED"
                and dependency["edge_type"].startswith("optional")
            )
            for dependency in dependencies
        )
        if ready:
            database.execute(
                "UPDATE production_nodes SET state='READY',updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=? AND node_id=? AND state='PENDING'",
                (job_id, node_id),
            )


def transition_node(
    database: sqlite3.Connection,
    job_id: str,
    node_id: str,
    event: str,
    *,
    event_id: str,
    payload: Mapping[str, object] | None = None,
) -> TransitionResult:
    """Apply an event once and promote newly unblocked downstream nodes."""
    with database:
        return transition_node_in_transaction(
            database,
            job_id,
            node_id,
            event,
            event_id=event_id,
            payload=payload,
        )


def transition_node_in_transaction(
    database: sqlite3.Connection,
    job_id: str,
    node_id: str,
    event: str,
    *,
    event_id: str,
    payload: Mapping[str, object] | None = None,
) -> TransitionResult:
    """Apply a node event inside the caller's existing database transaction."""
    duplicate = database.execute(
        "SELECT to_state FROM production_node_events "
        "WHERE job_id=? AND event_id=?",
        (job_id, event_id),
    ).fetchone()
    if duplicate is not None:
        return TransitionResult(False, str(duplicate["to_state"]))
    row = database.execute(
        "SELECT state FROM production_nodes WHERE job_id=? AND node_id=?",
        (job_id, node_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"production node not found: {job_id}/{node_id}")
    current = str(row["state"])
    target = _TRANSITIONS.get((current, event))
    if target is None:
        raise ValueError(
            f"invalid node transition: {node_id} {current} --{event}--> ?"
        )
    database.execute(
        "INSERT INTO production_node_events("
        "job_id,event_id,node_id,event,from_state,to_state,payload_json) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            job_id, event_id, node_id, event, current, target,
            json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
        ),
    )
    database.execute(
        "UPDATE production_nodes SET state=?,updated_at=CURRENT_TIMESTAMP "
        "WHERE job_id=? AND node_id=? AND state=?",
        (target, job_id, node_id, current),
    )
    if target in {"SUCCEEDED", "SKIPPED"}:
        _promote_dependents(database, job_id)
    return TransitionResult(True, target)
