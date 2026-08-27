"""Lease-based executor for persisted production graphs."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from typing import Awaitable, Callable, Mapping
import uuid

from . import node_state_machine, provider_capacity, provider_executor


@dataclass(frozen=True)
class NodeOutcome:
    event: str
    output: Mapping[str, object]

    @classmethod
    def succeed(cls, output: Mapping[str, object] | None = None) -> "NodeOutcome":
        return cls("succeed", dict(output or {}))

    @classmethod
    def require_review(
        cls, output: Mapping[str, object] | None = None,
    ) -> "NodeOutcome":
        return cls("require_review", dict(output or {}))

    @classmethod
    def require_action(
        cls, output: Mapping[str, object] | None = None,
    ) -> "NodeOutcome":
        return cls("require_action", dict(output or {}))

    @classmethod
    def require_budget(
        cls, output: Mapping[str, object] | None = None,
    ) -> "NodeOutcome":
        return cls("require_budget", dict(output or {}))


NodeHandler = Callable[
    [sqlite3.Row], Awaitable[NodeOutcome | Mapping[str, object] | None]
]


def _pause_outcome_from_exception(exc: Exception) -> NodeOutcome | None:
    pause_state = str(getattr(exc, "pause_state", "") or "").strip()
    output: dict[str, object] = {
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    for attribute in (
        "candidate_path", "candidate_url", "provider_task_id", "task_id",
        "stable_code", "current_cost_cents", "pending_cost_cents",
        "next_estimate_cents", "budget_limit_cents",
    ):
        value = getattr(exc, attribute, None)
        if value not in (None, ""):
            output[attribute] = (
                int(value)
                if attribute.endswith("_cents") else str(value)
            )
    if pause_state == "PROVIDER_ACTION_REQUIRED":
        return NodeOutcome.require_action(output)
    if pause_state == "BUDGET_CONFIRMATION_REQUIRED":
        return NodeOutcome.require_budget(output)
    if pause_state in {
        "PRODUCTION_REVIEW_REQUIRED", "ANCHOR_REVIEW_REQUIRED",
    }:
        return NodeOutcome.require_review(output)
    return None


@dataclass(frozen=True)
class WaveResult:
    started: tuple[str, ...]
    succeeded: tuple[str, ...]
    failed: tuple[str, ...]
    review_required: tuple[str, ...] = ()
    action_required: tuple[str, ...] = ()
    budget_required: tuple[str, ...] = ()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ready_nodes(
    database: sqlite3.Connection,
    job_id: str,
    *,
    configured_limit: int,
    provider: str = "seedance",
) -> tuple[sqlite3.Row, ...]:
    """Select READY work within provider and job concurrency capacity."""
    effective = provider_capacity.effective_limit(
        database, provider, configured_limit=max(1, int(configured_limit)),
    )
    running = int(database.execute(
        "SELECT COUNT(*) FROM production_nodes WHERE job_id=? AND state='RUNNING'",
        (job_id,),
    ).fetchone()[0])
    available = max(0, effective - running)
    if not available:
        return ()
    return tuple(database.execute(
        "SELECT * FROM production_nodes WHERE job_id=? AND state='READY' "
        "AND lease_id IS NULL ORDER BY paid DESC,node_type,node_id LIMIT ?",
        (job_id, available),
    ).fetchall())


def acquire_lease(
    database: sqlite3.Connection,
    job_id: str,
    node_id: str,
    *,
    lease_id: str | None = None,
) -> str | None:
    lease = lease_id or uuid.uuid4().hex
    timestamp = _now()
    with database:
        updated = database.execute(
            "UPDATE production_nodes SET state='RUNNING',lease_id=?,heartbeat_at=?,"
            "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND node_id=? "
            "AND state='READY' AND lease_id IS NULL",
            (lease, timestamp, job_id, node_id),
        )
        if int(updated.rowcount or 0) == 1:
            database.execute(
                "INSERT INTO production_node_events("
                "job_id,event_id,node_id,event,from_state,to_state,payload_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    job_id, f"{job_id}:{node_id}:{lease}:started", node_id,
                    "start", "READY", "RUNNING", "{}",
                ),
            )
    return lease if int(updated.rowcount or 0) == 1 else None


def record_heartbeat(
    database: sqlite3.Connection, job_id: str, node_id: str, lease_id: str,
) -> bool:
    updated = database.execute(
        "UPDATE production_nodes SET heartbeat_at=?,updated_at=CURRENT_TIMESTAMP "
        "WHERE job_id=? AND node_id=? AND state='RUNNING' AND lease_id=?",
        (_now(), job_id, node_id, lease_id),
    )
    database.commit()
    return int(updated.rowcount or 0) == 1


async def run_ready_wave(
    database: sqlite3.Connection,
    job_id: str,
    *,
    configured_limit: int,
    handlers: Mapping[str, NodeHandler],
) -> WaveResult:
    selected = ready_nodes(
        database, job_id, configured_limit=configured_limit,
    )
    claimed: list[tuple[sqlite3.Row, str]] = []
    for row in selected:
        lease = acquire_lease(database, job_id, str(row["node_id"]))
        if lease:
            claimed.append((row, lease))

    succeeded: list[str] = []
    failed: list[str] = []
    review_required: list[str] = []
    action_required: list[str] = []
    budget_required: list[str] = []

    async def run_one(row: sqlite3.Row, lease: str) -> None:
        node_id = str(row["node_id"])
        handler = handlers.get(str(row["node_type"]))
        if handler is None:
            failed.append(node_id)
            node_state_machine.transition_node(
                database, job_id, node_id, "fail",
                event_id=f"{job_id}:{node_id}:{lease}:missing-handler",
                payload={"error": "missing node handler"},
            )
            return
        try:
            with provider_executor.paid_task_owner_context(
                job_id=job_id, node_id=node_id,
                spec_hash=str(row["spec_hash"]),
            ):
                raw_outcome = await handler(row)
            outcome = (
                raw_outcome
                if isinstance(raw_outcome, NodeOutcome)
                else NodeOutcome.succeed(raw_outcome)
            )
            output = dict(outcome.output)
            database.execute(
                "UPDATE production_nodes SET output_json=?,lease_id=NULL,"
                "heartbeat_at=NULL WHERE job_id=? AND node_id=? AND lease_id=?",
                (json.dumps(output, ensure_ascii=False, sort_keys=True),
                 job_id, node_id, lease),
            )
            database.commit()
            node_state_machine.transition_node(
                database, job_id, node_id, outcome.event,
                event_id=f"{job_id}:{node_id}:{lease}:{outcome.event}",
            )
            if outcome.event == "succeed":
                succeeded.append(node_id)
            elif outcome.event == "require_review":
                review_required.append(node_id)
            elif outcome.event == "require_action":
                action_required.append(node_id)
            elif outcome.event == "require_budget":
                budget_required.append(node_id)
        except Exception as exc:
            pause_outcome = _pause_outcome_from_exception(exc)
            if pause_outcome is not None:
                output = dict(pause_outcome.output)
                next_action = (
                    "reconcile_provider_task"
                    if pause_outcome.event == "require_action"
                    else "increase_job_budget"
                    if pause_outcome.event == "require_budget"
                    else "review_candidate"
                )
                provider_task_id = str(
                    output.get("provider_task_id")
                    or output.get("task_id") or ""
                ).strip() or None
                database.execute(
                    "UPDATE production_nodes SET error_code=?,output_json=?,"
                    "provider_task_id=COALESCE(?,provider_task_id),next_action=?,"
                    "lease_id=NULL,heartbeat_at=NULL WHERE job_id=? "
                    "AND node_id=? AND lease_id=?",
                    (
                        str(output.get("stable_code") or type(exc).__name__),
                        json.dumps(output, ensure_ascii=False, sort_keys=True),
                        provider_task_id, next_action, job_id, node_id, lease,
                    ),
                )
                database.commit()
                node_state_machine.transition_node(
                    database, job_id, node_id, pause_outcome.event,
                    event_id=(
                        f"{job_id}:{node_id}:{lease}:{pause_outcome.event}"
                    ),
                    payload=output,
                )
                if pause_outcome.event == "require_action":
                    action_required.append(node_id)
                elif pause_outcome.event == "require_budget":
                    budget_required.append(node_id)
                else:
                    review_required.append(node_id)
                return
            database.execute(
                "UPDATE production_nodes SET error_code=?,lease_id=NULL,"
                "heartbeat_at=NULL WHERE job_id=? AND node_id=? AND lease_id=?",
                (type(exc).__name__, job_id, node_id, lease),
            )
            database.commit()
            node_state_machine.transition_node(
                database, job_id, node_id, "fail",
                event_id=f"{job_id}:{node_id}:{lease}:failed",
                payload={"error": str(exc)},
            )
            failed.append(node_id)

    await asyncio.gather(*(run_one(row, lease) for row, lease in claimed))
    return WaveResult(
        started=tuple(str(row["node_id"]) for row, _ in claimed),
        succeeded=tuple(succeeded),
        failed=tuple(failed),
        review_required=tuple(review_required),
        action_required=tuple(action_required),
        budget_required=tuple(budget_required),
    )


def recover_expired_nodes(
    database: sqlite3.Connection,
    job_id: str,
    *,
    now: datetime | None = None,
    lease_timeout_sec: int = 60,
) -> dict[str, str]:
    """Recover expired leases without issuing provider work."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(
        seconds=max(1, int(lease_timeout_sec))
    )
    recovered: dict[str, str] = {}
    rows = database.execute(
        "SELECT node_id,heartbeat_at,provider_task_id,output_json "
        "FROM production_nodes WHERE job_id=? AND state='RUNNING'",
        (job_id,),
    ).fetchall()
    for row in rows:
        try:
            heartbeat = datetime.fromisoformat(
                str(row["heartbeat_at"] or "").replace("Z", "+00:00")
            )
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        except ValueError:
            heartbeat = datetime.min.replace(tzinfo=timezone.utc)
        if heartbeat > cutoff:
            continue
        try:
            output = json.loads(row["output_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            output = {}
        if str(row["provider_task_id"] or "").strip():
            state, action = "ACTION_REQUIRED", "reconcile_provider_task"
        elif isinstance(output, dict) and (
            output.get("candidate_path") or output.get("candidate_url")
        ):
            state, action = "REVIEW_REQUIRED", "review_candidate"
        else:
            state, action = "READY", "retry_node"
        database.execute(
            "UPDATE production_nodes SET state=?,next_action=?,lease_id=NULL,"
            "heartbeat_at=NULL,updated_at=CURRENT_TIMESTAMP "
            "WHERE job_id=? AND node_id=? AND state='RUNNING'",
            (state, action, job_id, row["node_id"]),
        )
        recovered[str(row["node_id"])] = state
    database.commit()
    return recovered


def retry_repaired_contract_nodes(
    database: sqlite3.Connection, job_id: str,
) -> tuple[str, ...]:
    """Retry known local contract failures after the implementation is fixed.

    This is deliberately narrower than a generic retry.  It only requeues an
    anchor or master node when there is no provider task, no booked cost and no
    output artifact. Runtime anchor lookup failures and frozen-contract
    mismatches happen before a usable paid result exists. Every node receives
    at most one versioned repair retry, so a persistent defect cannot loop.
    """
    recovered: list[str] = []
    rows = database.execute(
        "SELECT node_id,node_type,error_code,provider_task_id,"
        "actual_cost_cents,output_json FROM production_nodes "
        "WHERE job_id=? AND state='FAILED' "
        "AND node_type IN ('character_anchor','scene_anchor','master')",
        (job_id,),
    ).fetchall()
    for row in rows:
        node_id = str(row["node_id"])
        if (
            str(row["provider_task_id"] or "").strip()
            or int(row["actual_cost_cents"] or 0) != 0
        ):
            continue
        try:
            output = json.loads(row["output_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            output = {"invalid": True}
        if output:
            continue

        failure = database.execute(
            "SELECT payload_json FROM production_node_events "
            "WHERE job_id=? AND node_id=? AND event='fail' "
            "ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (job_id, node_id),
        ).fetchone()
        try:
            payload = json.loads(failure["payload_json"] or "{}") if failure else {}
        except (TypeError, json.JSONDecodeError):
            payload = {}
        error_message = str(payload.get("error") or "")
        error_code = str(row["error_code"] or "")
        stale_master_anchor_lookup = (
            str(row["node_type"]) == "master"
            and error_code == "RuntimeError"
            and error_message.startswith("frozen scene anchor is not ready:")
        )
        repairable = error_code == "FrozenSpecMismatchError" or (
            str(row["node_type"]) == "scene_anchor"
            and error_code == "RuntimeError"
            and error_message.startswith("scene anchor is not ready:")
        ) or stale_master_anchor_lookup
        if not repairable:
            continue

        # v1 could only retry the persisted stale legacy site-anchor lookup.
        # The compiler-side removal of that stale key is repair v2, so affected
        # unpaid masters receive exactly one additional migration retry. Other
        # contract failures retain the original one-shot retry boundary.
        repair_version = "v2" if stale_master_anchor_lookup else "v1"
        repair_event_id = (
            f"{job_id}:{node_id}:contract-repair-{repair_version}"
        )
        if database.execute(
            "SELECT 1 FROM production_node_events "
            "WHERE job_id=? AND event_id=?",
            (job_id, repair_event_id),
        ).fetchone():
            continue

        result = node_state_machine.transition_node(
            database,
            job_id,
            node_id,
            "retry",
            event_id=repair_event_id,
            payload={
                "reason": "implementation_contract_repaired",
                "previous_error_code": error_code,
            },
        )
        if not result.applied:
            continue
        database.execute(
            "UPDATE production_nodes SET error_code=NULL,next_action=NULL,"
            "output_json='{}',updated_at=CURRENT_TIMESTAMP "
            "WHERE job_id=? AND node_id=? AND state='READY'",
            (job_id, node_id),
        )
        recovered.append(node_id)
    database.commit()
    return tuple(recovered)
