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


def admit_stopped_provider_reconciliation(
    database: sqlite3.Connection,
    job_id: str,
    task_id: str,
    *,
    worker_alive: Callable[[object], bool],
) -> bool:
    """Transfer a crashed graph Worker lease to the known-task GET path.

    A supervisor may have recorded FAILED before its node lease expired. The
    remote task remains unresolved, so waiting for the lease timeout or
    resubmitting generation would both be wrong. This command only applies to
    a stopped, non-policy graph failure with no live owner and never calls a
    provider.
    """
    with node_state_machine.atomic(database):
        row = database.execute(
            "SELECT state,failure_code,production_worker_pid,execution_engine "
            "FROM jobs WHERE job_id=?", (job_id,),
        ).fetchone()
        if not row or row["state"] != "FAILED" \
                or str(row["failure_code"] or "") in POLICY_FAILURE_CODES \
                or row["execution_engine"] != "graph_v2":
            return False
        if row["production_worker_pid"] and worker_alive(row["production_worker_pid"]):
            raise ValueError("Production Worker is still running")
        if task_id not in provider_tasks.unresolved_task_ids(database, job_id):
            raise ValueError("Remote task is not pending reconciliation")
        owners: set[str] = set()
        for call in database.execute(
            "SELECT task_id,shot_idx,provider,usage_json FROM provider_calls "
            "WHERE job_id=?", (job_id,),
        ).fetchall():
            if provider_tasks.effective_task_id(call["task_id"], call["usage_json"]) != task_id:
                continue
            provider = str(call["provider"] or "").casefold()
            if provider == "seedance" and int(call["shot_idx"] or 0) > 0:
                node = database.execute(
                    "SELECT node_id FROM production_nodes WHERE job_id=? "
                    "AND node_id=? AND node_type='master'",
                    (job_id, f"master:{int(call['shot_idx'])}"),
                ).fetchone()
                if node:
                    owners.add(str(node["node_id"]))
            else:
                expected_type = {
                    "seedance_scene": "scene_anchor",
                    "seedance_identity": "character_anchor",
                }.get(provider)
                query = ("SELECT node_id FROM production_nodes WHERE job_id=? "
                         "AND provider_task_id=?" +
                         (" AND node_type=?" if expected_type else ""))
                params = ((job_id, task_id, expected_type) if expected_type
                          else (job_id, task_id))
                owners.update(str(node["node_id"]) for node in database.execute(
                    query, params,
                ).fetchall())
        if len(owners) != 1:
            raise ValueError("Remote task has no unique graph node owner")
        production_executor.recover_expired_nodes(database, job_id, force=True)
        if database.execute(
            "SELECT 1 FROM production_nodes WHERE job_id=? AND state='RUNNING' LIMIT 1",
            (job_id,),
        ).fetchone():
            raise ValueError("Production node still owns a live lease")
        transition(
            database, job_id, "provider_paused",
            event_id=f"{job_id}:stopped-provider:{task_id}",
            updates={
                "production_worker_pid": None,
                "worker_lease_id": None,
                "production_heartbeat_at": None,
                "error": "本地生产进程已中断，远端任务待核对；不会重复提交生成",
                "failure_code": None,
            },
            payload={"task_id": task_id, "reason": "worker_exit",
                     "previous_failure_code": row["failure_code"]},
        )
        return True


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
