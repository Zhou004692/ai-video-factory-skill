"""Lease-based executor for persisted production graphs."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from typing import Awaitable, Callable, Mapping
import uuid

from . import node_state_machine, provider_capacity, provider_executor, provider_tasks
from .testing import fault_injection


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
    diagnostic = getattr(exc, "transport_diagnostic", None)
    if isinstance(diagnostic, dict):
        output["transport"] = dict(diagnostic)
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


def release_resolved_provider_gates(database: sqlite3.Connection, job_id: str) -> list[str]:
    """Release unpaid sibling pauses after all remote tasks reconcile.

    Caller owns the transaction. This does not start production or approve media.
    """
    job = database.execute("SELECT state,execution_engine,production_spec_hash,production_worker_pid FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not job or job['execution_engine'] != 'graph_v2' or job['state'] not in {
        'PROVIDER_ACTION_REQUIRED', 'PRODUCTION_REVIEW_REQUIRED', 'PRODUCTION_QUEUED',
        'PRODUCTION_RUNNING', 'FAILED',
    } or provider_tasks.unresolved_task_ids(database, job_id):
        return []
    released = _release_unsubmitted_anchor_gates(database, job_id, job)
    rows = database.execute(
        "SELECT n.node_id,s.idx FROM production_nodes n JOIN shots s ON s.job_id=n.job_id AND n.node_id='master:'||s.idx "
        "WHERE n.job_id=? AND n.node_type='master' AND n.state='ACTION_REQUIRED' "
        "AND n.error_code='ProviderTaskGateError' AND n.spec_hash=? "
        "AND COALESCE(n.provider_task_id,'')='' AND n.actual_cost_cents=0 AND n.lease_id IS NULL "
        "AND s.state='PENDING' AND s.cost_cents=0 AND COALESCE(s.video_path,'')='' "
        "AND NOT EXISTS(SELECT 1 FROM provider_calls c WHERE c.job_id=s.job_id AND c.shot_idx=s.idx)",
        (job_id, job['production_spec_hash']),
    ).fetchall()
    for row in rows:
        if database.execute(
            "SELECT 1 FROM production_edges e JOIN production_nodes n ON n.job_id=e.job_id AND n.node_id=e.from_node_id "
            "WHERE e.job_id=? AND e.to_node_id=? AND n.state!='SUCCEEDED' LIMIT 1", (job_id, row['node_id']),
        ).fetchone():
            continue
        node_state_machine.transition_node_in_transaction(
            database, job_id, row['node_id'], 'resume',
            event_id=f"{job_id}:{row['node_id']}:resolved-gate:{uuid.uuid4().hex}",
            payload={'reason':'all_provider_tasks_reconciled','no_prior_submission':True},
        )
        database.execute("UPDATE production_nodes SET error_code=NULL,next_action=NULL,output_json='{}' WHERE job_id=? AND node_id=?", (job_id, row['node_id']))
        released.append(str(row['node_id']))
    return released


def _release_unsubmitted_anchor_gates(database, job_id, job):
    """Only remove a sibling submit gate proven to have no paid/accepted work."""
    if job['production_worker_pid']:
        return []
    from . import anchor_recovery, production_adapters
    calls = database.execute('SELECT provider,usage_json FROM provider_calls WHERE job_id=?', (job_id,)).fetchall()
    rows = database.execute(
        "SELECT * FROM production_nodes WHERE job_id=? AND node_type IN ('character_anchor','scene_anchor') "
        "AND state='ACTION_REQUIRED' AND error_code='ProviderTaskGateError' AND spec_hash=? "
        "AND COALESCE(provider_task_id,'')='' AND actual_cost_cents=0 AND lease_id IS NULL",
        (job_id, job['production_spec_hash']),
    ).fetchall()
    released = []
    for node in rows:
        expected = 'seedance_identity' if node['node_type']=='character_anchor' else 'seedance_scene'
        ambiguous = False
        for call in calls:
            if call['provider'] != expected:
                continue
            try:
                usage = json.loads(call['usage_json'] or '{}')
                owner = usage.get('owner_node_id') if isinstance(usage, dict) else None
            except (ValueError, TypeError):
                owner = None
            if not owner or owner == node['node_id']:
                ambiguous = True
                break
        if ambiguous:
            continue
        try:
            production_adapters._frozen_spec(database, node)
            table, column, key, anchor = anchor_recovery.anchor_row(database, node)
        except (ValueError, KeyError, TypeError):
            continue
        if (anchor['state'] not in {'PENDING', 'FAILED'} or anchor['cost_cents']
                or anchor['approval_state']=='APPROVED' or any(
                    anchor[field] for field in ('video_path','video_url','image_path','image_url','preview_path'))
                or ('asset_uri' in anchor.keys() and anchor['asset_uri'])):
            continue
        if database.execute(
            "SELECT 1 FROM production_edges e JOIN production_nodes n ON n.job_id=e.job_id AND n.node_id=e.from_node_id "
            "WHERE e.job_id=? AND e.to_node_id=? AND n.state!='SUCCEEDED' LIMIT 1", (job_id,node['node_id']),
        ).fetchone():
            continue
        node_state_machine.transition_node_in_transaction(database,job_id,node['node_id'],'resume',
            event_id=f"{job_id}:{node['node_id']}:resolved-gate:{uuid.uuid4().hex}",
            payload={'reason':'all_provider_tasks_reconciled','no_prior_submission':True})
        database.execute("UPDATE production_nodes SET error_code=NULL,next_action=NULL,output_json='{}' WHERE job_id=? AND node_id=?",(job_id,node['node_id']))
        database.execute(f"UPDATE {table} SET state='PENDING',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND {column}=?",(job_id,key))
        released.append(str(node['node_id']))
    return released


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
        "AND lease_id IS NULL AND EXISTS(SELECT 1 FROM jobs j WHERE j.job_id=production_nodes.job_id "
        "AND j.state NOT IN ('REJECTED','REJECTED_PENDING_RECONCILIATION','PUBLISHED')) "
        "ORDER BY paid DESC,node_type,node_id LIMIT ?",
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
    with node_state_machine.atomic(database):
        updated = database.execute(
            "UPDATE production_nodes SET state='RUNNING',lease_id=?,heartbeat_at=?,"
            "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND node_id=? "
            "AND state='READY' AND lease_id IS NULL "
            "AND EXISTS(SELECT 1 FROM jobs j WHERE j.job_id=production_nodes.job_id "
            "AND j.state NOT IN ('REJECTED','REJECTED_PENDING_RECONCILIATION','PUBLISHED'))",
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
            result = node_state_machine.finish_attempt(
                database, job_id, node_id, "fail", lease_id=lease,
                event_id=f"{job_id}:{node_id}:{lease}:missing-handler",
                output={"error": "missing node handler"}, error_code="MissingHandler",
            )
            if result.applied:
                failed.append(node_id)
            return
        if str(row["node_type"]) == "local_compose":
            fault_injection.hit(
                database,
                "during_local_compose",
                job_id=job_id,
                node_id=node_id,
            )
        handler_completed = False
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
            handler_completed = True
            output = dict(outcome.output)
            checkpoint = {
                "event": outcome.event,
                "event_id": f"{job_id}:{node_id}:{lease}:{outcome.event}",
            }
            checkpointed_output = {
                **output,
                "_executor_checkpoint": checkpoint,
            }
            # Persist the completed handler outcome before moving the durable
            # node state. If the process dies in this narrow window, restart
            # recovery can apply the recorded transition without re-running a
            # provider or local handler.
            checkpoint_write = database.execute(
                "UPDATE production_nodes SET output_json=? "
                "WHERE job_id=? AND node_id=? AND lease_id=? AND state='RUNNING'",
                (
                    json.dumps(
                        checkpointed_output,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    job_id,
                    node_id,
                    lease,
                ),
            )
            database.commit()
            if checkpoint_write.rowcount != 1:
                return
            fault_injection.hit(
                database,
                "after_handler_completion_before_node_update",
                job_id=job_id,
                node_id=node_id,
            )
            committed = node_state_machine.finish_attempt(
                database, job_id, node_id, outcome.event, lease_id=lease,
                event_id=f"{job_id}:{node_id}:{lease}:{outcome.event}", output=output,
            )
            if not committed.applied:
                return
            if outcome.event == "succeed":
                succeeded.append(node_id)
            elif outcome.event == "require_review":
                review_required.append(node_id)
            elif outcome.event == "require_action":
                action_required.append(node_id)
            elif outcome.event == "require_budget":
                budget_required.append(node_id)
        except Exception as exc:
            if handler_completed:
                raise  # Keep the durable checkpoint for restart recovery.
            pause_outcome = _pause_outcome_from_exception(exc)
            if pause_outcome is not None:
                output = dict(pause_outcome.output)
                committed = node_state_machine.finish_attempt(
                    database, job_id, node_id, pause_outcome.event, lease_id=lease,
                    event_id=f"{job_id}:{node_id}:{lease}:{pause_outcome.event}",
                    output=output,
                    error_code=str(output.get("stable_code") or type(exc).__name__),
                )
                if not committed.applied:
                    return
                if pause_outcome.event == "require_action":
                    action_required.append(node_id)
                elif pause_outcome.event == "require_budget":
                    budget_required.append(node_id)
                else:
                    review_required.append(node_id)
                return
            committed = node_state_machine.finish_attempt(
                database, job_id, node_id, "fail", lease_id=lease,
                event_id=f"{job_id}:{node_id}:{lease}:failed",
                output={"error": str(exc), **({
                    "submit_result_unknown": exc.submit_result_unknown,
                    "stable_code": getattr(exc, "stable_code", None),
                    **({"transport": dict(exc.transport_diagnostic)}
                       if isinstance(
                           getattr(exc, "transport_diagnostic", None), dict
                       ) else {}),
                } if isinstance(getattr(exc, "submit_result_unknown", None), bool) else {})},
                error_code=type(exc).__name__,
            )
            if committed.applied:
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
    force: bool = False,
) -> dict[str, str]:
    """Recover leases without provider work; force only after proven Worker exit."""
    with node_state_machine.atomic(database):
        return _recover_expired_nodes(database, job_id, now=now,
                                      lease_timeout_sec=lease_timeout_sec, force=force)


def _recover_expired_nodes(database, job_id, *, now, lease_timeout_sec, force=False):
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(
        seconds=max(1, int(lease_timeout_sec))
    )
    recovered: dict[str, str] = {}
    rows = database.execute(
        "SELECT node_id,heartbeat_at,provider_task_id,output_json,lease_id,paid,actual_cost_cents "
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
        if not force and heartbeat > cutoff:
            continue
        try:
            output = json.loads(row["output_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            output = {"recovery_error": "invalid output", "raw_output": row["output_json"]}
        if not isinstance(output, dict):
            output = {"recovery_error": "invalid output", "raw_output": row["output_json"]}
        checkpoint = (
            output.get("_executor_checkpoint")
            if isinstance(output, dict) else None
        )
        if isinstance(checkpoint, dict):
            event = str(checkpoint.get("event") or "")
            event_id = str(checkpoint.get("event_id") or "")
            if event in {
                "succeed", "require_review", "require_action", "require_budget",
            } and event_id:
                clean_output = dict(output)
                clean_output.pop("_executor_checkpoint", None)
                result = node_state_machine.finish_attempt(
                    database, job_id, str(row["node_id"]), event,
                    lease_id=row["lease_id"], event_id=event_id, output=clean_output,
                    error_code=clean_output.get("stable_code"),
                )
                if result.applied:
                    recovered[str(row["node_id"])] = result.state
                continue
        unresolved = provider_tasks.unresolved_task_ids(database, job_id)
        if str(row["provider_task_id"] or "").strip() or (row["paid"] and unresolved):
            event = "require_action"
        elif isinstance(output, dict) and (output.get("candidate_path") or output.get("candidate_url")):
            event = "require_review"
        elif output or row["actual_cost_cents"]:
            event = "require_action"
        else:
            event = "recover_unsubmitted"
        result = node_state_machine.finish_attempt(
            database, job_id, str(row["node_id"]), event,
            lease_id=row["lease_id"],
            event_id=f"{job_id}:{row['node_id']}:{row['lease_id']}:expired:{event}",
            output=output if isinstance(output, dict) else {"recovery_error": "invalid output"},
        )
        if result.applied:
            recovered[str(row["node_id"])] = result.state
    return recovered


def retry_repaired_contract_nodes(
    database: sqlite3.Connection, job_id: str,
) -> tuple[str, ...]:
    with node_state_machine.atomic(database):
        return _retry_repaired_contract_nodes(database, job_id)


def _retry_repaired_contract_nodes(database, job_id):
    """Retry known local contract failures after the implementation is fixed.

    This is deliberately narrower than a generic retry. It only requeues a
    known production node when there is no provider task, no booked cost and no
    output artifact. Runtime anchor lookup failures and frozen-contract
    mismatches happen before a usable paid result exists. Every node receives
    at most one versioned repair retry, so a persistent defect cannot loop.
    """
    recovered: list[str] = []
    rows = database.execute(
        "SELECT node_id,node_type,error_code,provider_task_id,"
        "actual_cost_cents,output_json FROM production_nodes "
        "WHERE job_id=? AND state='FAILED' AND node_type IN ("
        "'character_anchor','scene_anchor','master','bgm','local_compose',"
        "'final_review')",
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
        if not isinstance(output, dict) or set(output) - {"error"}:
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
    return tuple(recovered)
