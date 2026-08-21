"""Single lifecycle boundary for paid provider work."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import sqlite3
from typing import Awaitable, Callable, Generic, TypeVar

from . import provider_tasks


T = TypeVar("T")
UNRESOLVED_STATUSES = provider_tasks.UNRESOLVED_STATUSES
ProviderTaskGateError = provider_tasks.ProviderTaskGateError
ProviderTaskBudgetError = provider_tasks.ProviderTaskBudgetError


class ProviderSubmissionOutcomeUnknown(RuntimeError):
    """The paid POST may have succeeded and must never be repeated blindly."""

    non_retryable = True
    pause_state = "PROVIDER_ACTION_REQUIRED"
    submit_result_unknown = True


@dataclass(frozen=True)
class PaidTaskOwner:
    job_id: str
    shot_idx: int
    provider: str
    intent: str


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
        return SubmitReceipt(
            owner=self.owner, task_id=remote_task_id, value=value,
        )


def reserve_paid_submit(database: sqlite3.Connection, **kwargs) -> str:
    return provider_tasks.reserve_paid_submit(database, **kwargs)


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
    provider_tasks.promote_reservation(
        database,
        reservation_id,
        task_id=task_id,
        job_id=owner.job_id,
        shot_idx=owner.shot_idx,
        provider=owner.provider,
        intent=owner.intent,
    )


def persist_remote_task_id(database: sqlite3.Connection, *args, **kwargs) -> None:
    provider_tasks.persist_remote_task_id(database, *args, **kwargs)


def promote_reservation(database: sqlite3.Connection, *args, **kwargs) -> None:
    provider_tasks.promote_reservation(database, *args, **kwargs)


def mark_action_required(database: sqlite3.Connection, *args, **kwargs) -> None:
    provider_tasks.mark_action_required(database, *args, **kwargs)


def release_reservation(database: sqlite3.Connection, *args, **kwargs) -> None:
    provider_tasks.release_reservation(database, *args, **kwargs)


def upsert_final_call(database: sqlite3.Connection, **kwargs) -> int:
    return provider_tasks.upsert_final_call(database, **kwargs)
