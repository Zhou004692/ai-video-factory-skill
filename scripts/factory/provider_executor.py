"""Single lifecycle boundary for paid provider work."""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
import sqlite3
from typing import Awaitable, Callable, Generic, TypeVar

from . import provider_tasks
from .testing import fault_injection


T = TypeVar("T")
UNRESOLVED_STATUSES = provider_tasks.UNRESOLVED_STATUSES
ProviderTaskGateError = provider_tasks.ProviderTaskGateError
ProviderTaskBudgetError = provider_tasks.ProviderTaskBudgetError


class ProviderSubmissionOutcomeUnknown(RuntimeError):
    """The paid POST may have succeeded and must never be repeated blindly."""

    non_retryable = True
    pause_state = "PROVIDER_ACTION_REQUIRED"
    submit_result_unknown = True


class ProductionContractRequiredError(RuntimeError):
    """Paid work is not owned by a READY/RUNNING frozen v2 node."""

    pre_submit = True
    non_retryable = True


@dataclass(frozen=True)
class PaidTaskOwner:
    job_id: str
    shot_idx: int
    provider: str
    intent: str
    node_id: str | None = None
    spec_hash: str | None = None


@dataclass(frozen=True)
class FrozenNodeOwner:
    job_id: str
    node_id: str
    spec_hash: str


_FROZEN_NODE_OWNER: ContextVar[FrozenNodeOwner | None] = ContextVar(
    "factory_frozen_node_owner", default=None,
)


@contextmanager
def paid_task_owner_context(
    *, job_id: str, node_id: str, spec_hash: str,
):
    """Bind all nested provider reservations to one immutable graph node."""
    owner = FrozenNodeOwner(
        job_id=str(job_id), node_id=str(node_id), spec_hash=str(spec_hash),
    )
    token = _FROZEN_NODE_OWNER.set(owner)
    try:
        yield owner
    finally:
        _FROZEN_NODE_OWNER.reset(token)


@dataclass(frozen=True)
class SubmitReceipt(Generic[T]):
    owner: PaidTaskOwner
    task_id: str | None
    value: T


class ProviderExecutor:
    """Reserve and submit paid work exactly once for one immutable owner."""

    def __init__(
        self,
        database: sqlite3.Connection,
        owner: PaidTaskOwner,
        *,
        authorized_task_ids: tuple[str, ...] = (),
        authorized_batch_id: str | None = None,
        estimated_cost_cents: int = 0,
        budget_limit_cents: int = 0,
    ) -> None:
        self.database = database
        self.owner = owner
        self.authorized_task_ids = authorized_task_ids
        self.authorized_batch_id = authorized_batch_id
        self.estimated_cost_cents = estimated_cost_cents
        self.budget_limit_cents = budget_limit_cents

    async def submit_once(
        self,
        submit: Callable[[Callable[[str], None]], Awaitable[T]],
    ) -> SubmitReceipt[T]:
        reservation_id = reserve_paid_submit(
            self.database,
            job_id=self.owner.job_id,
            shot_idx=self.owner.shot_idx,
            provider=self.owner.provider,
            intent=self.owner.intent,
            authorized_task_ids=self.authorized_task_ids,
            authorized_batch_id=self.authorized_batch_id,
            estimated_cost_cents=self.estimated_cost_cents,
            budget_limit_cents=self.budget_limit_cents,
            owner_id=self.owner.node_id,
            spec_hash=self.owner.spec_hash,
        )
        remote_task_id: str | None = None

        def on_task_created(task_id: str) -> None:
            nonlocal remote_task_id
            remote_task_id = str(task_id or "").strip()
            if not remote_task_id:
                raise ProviderSubmissionOutcomeUnknown(
                    "provider returned an empty task id"
                )
            bind_remote_task(
                self.database,
                reservation_id,
                task_id=remote_task_id,
                owner=self.owner,
            )
            fault_injection.hit(
                self.database,
                "after_provider_submission",
                job_id=self.owner.job_id,
                node_id=(
                    self.owner.node_id
                    or f"{self.owner.provider}:{self.owner.shot_idx}:"
                    f"{self.owner.intent}"
                ),
            )

        try:
            value = await submit(on_task_created)
        except BaseException as exc:
            evidence_id = remote_task_id or reservation_id
            if (
                remote_task_id
                or isinstance(exc, (asyncio.CancelledError, ProviderSubmissionOutcomeUnknown))
                or bool(getattr(exc, "submit_result_unknown", False))
                or not bool(getattr(exc, "pre_submit", False))
            ):
                mark_action_required(
                    self.database,
                    evidence_id,
                    message="provider submit outcome requires reconciliation",
                )
            else:
                release_reservation(self.database, reservation_id)
            raise
        if remote_task_id is None:
            mark_action_required(
                self.database,
                reservation_id,
                message="provider response has no durable remote task id",
            )
            raise ProviderSubmissionOutcomeUnknown(
                "provider response has no durable remote task id"
            )
        try:
            fault_injection.hit(
                self.database,
                "after_provider_completion_before_promotion",
                job_id=self.owner.job_id,
                node_id=(
                    self.owner.node_id
                    or f"{self.owner.provider}:{self.owner.shot_idx}:"
                    f"{self.owner.intent}"
                ),
            )
        except BaseException:
            mark_action_required(
                self.database,
                remote_task_id,
                message="provider completion requires durable promotion",
            )
            raise
        return SubmitReceipt(
            owner=self.owner, task_id=remote_task_id, value=value,
        )


def assert_frozen_owner(
    database: sqlite3.Connection,
    *,
    job_id: str,
    owner_id: str,
    spec_hash: str,
) -> None:
    row = database.execute(
        "SELECT spec_hash,state,paid FROM production_nodes "
        "WHERE job_id=? AND node_id=?",
        (job_id, owner_id),
    ).fetchone()
    if (
        row is None
        or str(row["spec_hash"]) != str(spec_hash)
        or str(row["state"]) not in {"READY", "RUNNING"}
        or not bool(row["paid"])
    ):
        raise ProductionContractRequiredError("付费调用缺少已冻结生产节点")


def reserve_paid_submit(database: sqlite3.Connection, **kwargs) -> str:
    owner_id = str(kwargs.pop("owner_id", "") or "").strip()
    spec_hash = str(kwargs.pop("spec_hash", "") or "").strip()
    contextual_owner = _FROZEN_NODE_OWNER.get()
    requested_job_id = str(kwargs.get("job_id") or "")
    if contextual_owner is not None:
        if contextual_owner.job_id != requested_job_id:
            raise ProductionContractRequiredError(
                "paid provider call crossed its frozen job boundary"
            )
        if owner_id and owner_id != contextual_owner.node_id:
            raise ProductionContractRequiredError(
                "paid provider call changed its frozen node owner"
            )
        if spec_hash and spec_hash != contextual_owner.spec_hash:
            raise ProductionContractRequiredError(
                "paid provider call changed its frozen spec hash"
            )
        owner_id = contextual_owner.node_id
        spec_hash = contextual_owner.spec_hash
    require_v2 = os.environ.get("FACTORY_REQUIRE_V2_CONTRACT", "").strip() == "1"
    if owner_id or spec_hash or require_v2:
        if not owner_id or not spec_hash:
            raise ProductionContractRequiredError("付费调用缺少已冻结生产节点")
        assert_frozen_owner(
            database,
            job_id=str(kwargs.get("job_id") or ""),
            owner_id=owner_id,
            spec_hash=spec_hash,
        )
    reservation = provider_tasks.reserve_paid_submit(database, **kwargs)
    if owner_id:
        row = database.execute('SELECT usage_json FROM provider_calls WHERE task_id=?',(reservation,)).fetchone()
        usage = json.loads(row[0])
        usage.update({'owner_node_id':owner_id,'owner_spec_hash':spec_hash})
        database.execute('UPDATE provider_calls SET usage_json=? WHERE task_id=?',(json.dumps(usage),reservation))
        database.commit()
    return reservation


def begin_paid_submit_batch(database: sqlite3.Connection, job_id: str) -> str:
    return provider_tasks.begin_paid_submit_batch(database, job_id)


def assert_no_unresolved(database: sqlite3.Connection, job_id: str, **kwargs) -> None:
    provider_tasks.assert_no_unresolved(database, job_id, **kwargs)


def unresolved_task_ids(database: sqlite3.Connection, job_id: str) -> list[str]:
    return provider_tasks.unresolved_task_ids(database, job_id)


def bind_remote_task(
    database: sqlite3.Connection,
    reservation_id: str,
    *,
    task_id: str,
    owner: PaidTaskOwner,
) -> None:
    provider_tasks.persist_remote_task_id(
        database,
        reservation_id,
        remote_task_id=task_id,
        provider=owner.provider,
        intent=owner.intent,
    )
    if owner.node_id and owner.spec_hash:
        _bind_frozen_node_task(
            database,
            FrozenNodeOwner(
                job_id=owner.job_id,
                node_id=owner.node_id,
                spec_hash=owner.spec_hash,
            ),
            task_id,
        )
    provider_tasks.promote_reservation(
        database,
        reservation_id,
        task_id=task_id,
        job_id=owner.job_id,
        shot_idx=owner.shot_idx,
        provider=owner.provider,
        intent=owner.intent,
    )


def _bind_frozen_node_task(
    database: sqlite3.Connection,
    owner: FrozenNodeOwner,
    task_id: str,
) -> None:
    from . import node_state_machine
    with node_state_machine.atomic(database):
        current = database.execute('SELECT provider_task_id FROM production_nodes WHERE job_id=? AND node_id=? AND spec_hash=? AND state=\'RUNNING\'',(owner.job_id,owner.node_id,owner.spec_hash)).fetchone()
        previous = str(current[0] or '') if current else ''
        if previous and previous != task_id:
            prior = database.execute('SELECT * FROM provider_calls WHERE task_id=? AND job_id=?',(previous,owner.job_id)).fetchone()
            reservations = database.execute("SELECT * FROM provider_calls WHERE job_id=? AND task_id LIKE 'local-submit:%'",(owner.job_id,)).fetchall()
            successors = []
            for reservation in reservations:
                usage = json.loads(reservation['usage_json'] or '{}')
                if (usage.get('remote_task_id') == task_id and usage.get('owner_node_id') == owner.node_id
                        and usage.get('owner_spec_hash') == owner.spec_hash and not reservation['cost_cents']):
                    successors.append(reservation)
            if (not prior or provider_tasks._status(prior['usage_json']) not in {'completed','failed'} or len(successors) != 1
                    or tuple(prior[k] for k in ('shot_idx','provider','intent')) != tuple(successors[0][k] for k in ('shot_idx','provider','intent'))):
                raise ProductionContractRequiredError('prior provider task is unresolved or retry ownership differs')
            database.execute('UPDATE production_nodes SET provider_task_id=? WHERE job_id=? AND node_id=? AND spec_hash=? AND state=\'RUNNING\' AND provider_task_id=?',
                             (task_id,owner.job_id,owner.node_id,owner.spec_hash,previous))
            database.execute('INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)',
                             (owner.job_id,'provider_retry_binding','info',json.dumps({'node_id':owner.node_id,'previous_task_id':previous,'task_id':task_id})))
    updated = database.execute(
        "UPDATE production_nodes SET provider_task_id=?,"
        "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND node_id=? "
        "AND spec_hash=? AND state='RUNNING' AND "
        "(provider_task_id IS NULL OR provider_task_id=?)",
        (
            str(task_id), owner.job_id, owner.node_id, owner.spec_hash,
            str(task_id),
        ),
    )
    database.commit()
    if int(updated.rowcount or 0) != 1:
        raise ProductionContractRequiredError(
            "remote provider task could not bind to its frozen node"
        )


def persist_remote_task_id(database: sqlite3.Connection, *args, **kwargs) -> None:
    provider_tasks.persist_remote_task_id(database, *args, **kwargs)
    contextual_owner = _FROZEN_NODE_OWNER.get()
    if contextual_owner is not None:
        task_id = str(kwargs.get("remote_task_id") or "").strip()
        if not task_id:
            raise ProviderSubmissionOutcomeUnknown(
                "provider returned an empty task id"
            )
        _bind_frozen_node_task(database, contextual_owner, task_id)


def promote_reservation(database: sqlite3.Connection, *args, **kwargs) -> None:
    provider_tasks.promote_reservation(database, *args, **kwargs)
    contextual_owner = _FROZEN_NODE_OWNER.get()
    if contextual_owner is not None:
        task_id = str(kwargs.get("task_id") or "").strip()
        if not task_id:
            raise ProviderSubmissionOutcomeUnknown(
                "provider promotion has no remote task id"
            )
        _bind_frozen_node_task(database, contextual_owner, task_id)


def mark_action_required(database: sqlite3.Connection, *args, **kwargs) -> None:
    provider_tasks.mark_action_required(database, *args, **kwargs)


def release_reservation(database: sqlite3.Connection, *args, **kwargs) -> None:
    provider_tasks.release_reservation(database, *args, **kwargs)


def upsert_final_call(database: sqlite3.Connection, **kwargs) -> int:
    delta = provider_tasks.upsert_final_call(database, **kwargs)
    contextual_owner = _FROZEN_NODE_OWNER.get()
    if contextual_owner is not None and delta:
        updated = database.execute(
            "UPDATE production_nodes SET actual_cost_cents="
            "actual_cost_cents+?,updated_at=CURRENT_TIMESTAMP "
            "WHERE job_id=? AND node_id=? AND spec_hash=?",
            (
                int(delta), contextual_owner.job_id,
                contextual_owner.node_id, contextual_owner.spec_hash,
            ),
        )
        database.commit()
        if int(updated.rowcount or 0) != 1:
            raise ProductionContractRequiredError(
                "provider cost could not bind to its frozen node"
            )
    return delta
