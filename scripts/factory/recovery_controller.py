"""Deterministic recovery for expired production worker leases."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3
from typing import Callable

from . import node_state_machine, production_executor, production_projection, provider_tasks
from .state_machine import InvalidTransitionError, transition


POLICY_FAILURE_CODES = frozenset({
    "input_privacy_rejected",
    "output_policy_rejected",
    "output_copyright_rejected",
})


@dataclass(frozen=True)
class RecoveryOutcome:
    job_id: str
    action: str
    state: str
    task_ids: tuple[str, ...] = ()


def record_heartbeat(
    database: sqlite3.Connection,
    job_id: str,
    lease_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    timestamp = (now or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    ).isoformat()
    updated = database.execute(
        "UPDATE jobs SET production_heartbeat_at=?,worker_lease_id=?,"
        "updated_at=CURRENT_TIMESTAMP WHERE job_id=? "
        "AND state='PRODUCTION_RUNNING' "
        "AND (worker_lease_id IS NULL OR worker_lease_id=?)",
        (timestamp, lease_id, job_id, lease_id),
    )
    database.commit()
    return int(updated.rowcount or 0) == 1


def _parse_time(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def reconcile_job(
    database: sqlite3.Connection,
    job_id: str,
    *,
    now: datetime | None = None,
    worker_alive: Callable[[object], bool] = lambda _pid: False,
    lease_timeout_sec: int = 60,
) -> RecoveryOutcome:
    """Recover one expired job without performing any provider POST."""
    with node_state_machine.atomic(database):
        return _reconcile_job(database, job_id, now=now, worker_alive=worker_alive,
                              lease_timeout_sec=lease_timeout_sec)


def _reconcile_job(database, job_id, *, now, worker_alive, lease_timeout_sec):
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    row = database.execute(
        "SELECT state,failure_code,production_worker_pid,"
        "production_heartbeat_at,recovery_attempts,execution_engine FROM jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise KeyError(job_id)
    state = str(row["state"])
    if state != "PRODUCTION_RUNNING":
        return RecoveryOutcome(job_id, "none", state)
    heartbeat = _parse_time(row["production_heartbeat_at"])
    lease_live = bool(
        heartbeat
        and (current_time - heartbeat).total_seconds() <= lease_timeout_sec
    )
    if lease_live or worker_alive(row["production_worker_pid"]):
        return RecoveryOutcome(job_id, "none", state)

    attempts = max(0, int(row["recovery_attempts"] or 0))
    base_updates = {
        "production_worker_pid": None,
        "worker_lease_id": None,
        "production_heartbeat_at": None,
        "recovery_attempts": attempts + 1,
    }
    event_suffix = f"{attempts + 1}"
    failure_code = str(row["failure_code"] or "").strip()
    if failure_code in POLICY_FAILURE_CODES:
        transition(
            database, job_id, "recovery_failed",
            event_id=f"{job_id}:recovery-policy:{event_suffix}",
            updates={
                **base_updates,
                "error": "内容政策拒绝不可自动恢复",
            },
        )
        return RecoveryOutcome(job_id, "terminal_policy_failure", "FAILED")
    if row['execution_engine'] == 'graph_v2' and database.execute(
        'SELECT 1 FROM production_nodes WHERE job_id=? LIMIT 1', (job_id,),
    ).fetchone():
        production_executor.recover_expired_nodes(
            database, job_id, now=current_time, lease_timeout_sec=lease_timeout_sec,
        )
        graph_states = {r[0] for r in database.execute(
            'SELECT state FROM production_nodes WHERE job_id=?', (job_id,),
        )}
        # A node may have a newer heartbeat than the job lease. Do not clear it.
        if 'RUNNING' in graph_states:
            return RecoveryOutcome(job_id, 'none', state)
        report = production_projection.reconcile(database, job_id)
        if report.projected_job_state != 'PRODUCTION_QUEUED':
            production_projection.persist(database, job_id)
            database.execute(
                'UPDATE jobs SET worker_lease_id=NULL,production_heartbeat_at=NULL,recovery_attempts=? WHERE job_id=?',
                (attempts + 1, job_id),
            )
            action = {'PROVIDER_ACTION_REQUIRED': 'reconcile_provider',
                      'PRODUCTION_REVIEW_REQUIRED': 'manual_review'}.get(report.projected_job_state, report.next_action)
            return RecoveryOutcome(job_id, action, report.projected_job_state,
                                   tuple(provider_tasks.unresolved_task_ids(database, job_id)))
        if 'READY' not in graph_states:
            transition(database, job_id, 'recovery_required',
                       event_id=f'{job_id}:recovery-stranded:{event_suffix}',
                       updates={**base_updates, 'error': '还有未完成工作，但没有可执行节点，请检查依赖状态'})
            return RecoveryOutcome(job_id, 'manual_recovery', 'RECOVERY_REQUIRED')
    if attempts >= 3:
        transition(
            database, job_id, "recovery_required",
            event_id=f"{job_id}:recovery-manual:{event_suffix}",
            updates={
                **base_updates,
                "error": "自动恢复已达到上限，请人工诊断任务现场",
            },
        )
        return RecoveryOutcome(job_id, "manual_recovery", "RECOVERY_REQUIRED")
    unresolved = tuple(provider_tasks.unresolved_task_ids(database, job_id))
    if unresolved:
        transition(
            database, job_id, "recovery_provider",
            event_id=f"{job_id}:recovery-provider:{event_suffix}",
            updates={
                **base_updates,
                "error": "远端任务状态待核对，不会重新提交",
            },
        )
        return RecoveryOutcome(
            job_id, "reconcile_provider", "PROVIDER_ACTION_REQUIRED", unresolved,
        )
    review = database.execute(
        "SELECT 1 FROM shots WHERE job_id=? AND state IN "
        "('REVIEW_REQUIRED','REVIEW_DECIDING') LIMIT 1",
        (job_id,),
    ).fetchone()
    if review is not None:
        transition(
            database, job_id, "recovery_review",
            event_id=f"{job_id}:recovery-review:{event_suffix}",
            updates={
                **base_updates,
                "error": "候选母片等待人工复核",
            },
        )
        return RecoveryOutcome(
            job_id, "manual_review", "PRODUCTION_REVIEW_REQUIRED"
        )
    transition(
        database, job_id, "recovery_requeue",
        event_id=f"{job_id}:recovery-requeue:{event_suffix}",
        updates={**base_updates, "error": None, "failure_code": None},
    )
    return RecoveryOutcome(job_id, "requeue", "PRODUCTION_QUEUED")
