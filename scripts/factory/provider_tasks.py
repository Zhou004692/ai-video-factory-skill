"""Shared idempotency and reconciliation guards for paid provider submits."""
from __future__ import annotations

import json
import sqlite3
import uuid


UNRESOLVED_STATUSES = frozenset({
    "submitting", "pending", "staged", "unknown", "action_required",
})


class ProviderTaskGateError(RuntimeError):
    non_retryable = True
    pause_state = "PROVIDER_ACTION_REQUIRED"


class ProviderTaskBudgetError(RuntimeError):
    non_retryable = True


def _status(raw: object) -> str:
    try:
        value = json.loads(str(raw or "{}"))
    except (json.JSONDecodeError, TypeError):
        return "action_required"
    if not isinstance(value, dict):
        return "action_required"
    return str(value.get("status") or "").strip().casefold()


def effective_task_id(task_id: object, usage_json: object) -> str:
    """Return the remote id recorded on a local reservation when available."""
    try:
        usage = json.loads(str(usage_json or "{}"))
    except (json.JSONDecodeError, TypeError):
        usage = {}
    remote = usage.get("remote_task_id") if isinstance(usage, dict) else None
    return str(remote or task_id or "").strip()


def unresolved_task_ids(database: sqlite3.Connection, job_id: str) -> list[str]:
    rows = database.execute(
        "SELECT task_id,usage_json FROM provider_calls WHERE job_id=? "
        "ORDER BY created_at,task_id",
        (job_id,),
    ).fetchall()
    return [
        effective_task_id(
            row["task_id"] if isinstance(row, sqlite3.Row) else row[0],
            row["usage_json"] if isinstance(row, sqlite3.Row) else row[1],
        )
        for row in rows
        if _status(row["usage_json"] if isinstance(row, sqlite3.Row) else row[1])
        in UNRESOLVED_STATUSES
    ]


def begin_paid_submit_batch(
    database: sqlite3.Connection,
    job_id: str,
) -> str:
    """Open one fail-closed paid-submit wave after checking old work."""
    try:
        database.execute("BEGIN IMMEDIATE")
        assert_no_unresolved(database, job_id)
        database.commit()
    except Exception:
        database.rollback()
        raise
    return uuid.uuid4().hex


def persist_remote_task_id(
    database: sqlite3.Connection,
    reservation_id: str,
    *,
    remote_task_id: str,
    provider: str,
    intent: str,
) -> None:
    """Durably bind a returned remote id before promotion can fail or crash."""
    try:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT usage_json FROM provider_calls WHERE task_id=? "
            "AND task_id LIKE 'local-submit:%'",
            (reservation_id,),
        ).fetchone()
        if row is None:
            raise ProviderTaskGateError(
                f"local provider reservation not found: {reservation_id}"
            )
        try:
            usage = json.loads(row[0] or "{}")
        except (json.JSONDecodeError, TypeError):
            usage = {}
        usage.update({
            "status": "action_required",
            "remote_task_id": str(remote_task_id),
            "provider": str(provider),
            "intent": str(intent),
        })
        updated = database.execute(
            "UPDATE provider_calls SET usage_json=? WHERE task_id=?",
            (json.dumps(usage, ensure_ascii=False), reservation_id),
        )
        if int(updated.rowcount or 0) != 1:
            raise ProviderTaskGateError(
                f"local provider reservation changed: {reservation_id}"
            )
        database.commit()
    except Exception:
        database.rollback()
        raise


def assert_no_unresolved(
    database: sqlite3.Connection,
    job_id: str,
    *,
    authorized_task_ids: tuple[str, ...] = (),
    authorized_batch_id: str | None = None,
) -> None:
    authorized = {str(task_id) for task_id in authorized_task_ids}
    batch_id = str(authorized_batch_id or "").strip()
    unresolved: list[str] = []
    rows = database.execute(
        "SELECT task_id,usage_json FROM provider_calls WHERE job_id=? "
        "ORDER BY created_at,task_id",
        (job_id,),
    ).fetchall()
    for row in rows:
        raw_task_id = row["task_id"] if isinstance(row, sqlite3.Row) else row[0]
        raw_usage = row["usage_json"] if isinstance(row, sqlite3.Row) else row[1]
        if _status(raw_usage) not in UNRESOLVED_STATUSES:
            continue
        task_id = effective_task_id(raw_task_id, raw_usage)
        if task_id in authorized:
            continue
        try:
            usage = json.loads(str(raw_usage or "{}"))
        except (json.JSONDecodeError, TypeError):
            usage = {}
        if (
            batch_id
            and isinstance(usage, dict)
            and str(usage.get("batch_id") or "") == batch_id
        ):
            continue
        unresolved.append(task_id)
    if unresolved:
        raise ProviderTaskGateError(
            "存在尚未核对的远端任务，不能重新提交；请先核对 task_id："
            + ", ".join(unresolved[:5])
        )


def reserve_paid_submit(
    database: sqlite3.Connection,
    *,
    job_id: str,
    shot_idx: int,
    provider: str,
    intent: str,
    authorized_task_ids: tuple[str, ...] = (),
    authorized_batch_id: str | None = None,
    estimated_cost_cents: int = 0,
    budget_limit_cents: int = 0,
) -> str:
    """Atomically gate a job and claim its next paid POST before networking."""
    reservation_id = f"local-submit:{provider}:{job_id}:{int(shot_idx)}:{intent}"
    try:
        database.execute("BEGIN IMMEDIATE")
        assert_no_unresolved(
            database,
            job_id,
            authorized_task_ids=authorized_task_ids,
            authorized_batch_id=authorized_batch_id,
        )
        estimate = max(0, int(estimated_cost_cents or 0))
        budget_limit = max(0, int(budget_limit_cents or 0))
        if budget_limit:
            job = database.execute(
                "SELECT cost_cents FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            current_cost = max(0, int(job[0] or 0)) if job else 0
            reserved_cost = 0
            pending = database.execute(
                "SELECT cost_cents,usage_json FROM provider_calls "
                "WHERE job_id=?",
                (job_id,),
            ).fetchall()
            for row in pending:
                raw_usage = row[1]
                if _status(raw_usage) not in UNRESOLVED_STATUSES:
                    continue
                try:
                    pending_usage = json.loads(str(raw_usage or "{}"))
                except (json.JSONDecodeError, TypeError):
                    pending_usage = {}
                reserved_cost += max(
                    max(0, int(row[0] or 0)),
                    max(0, int(
                        pending_usage.get("reserved_cost_cents") or 0
                    )) if isinstance(pending_usage, dict) else 0,
                )
            if current_cost + reserved_cost + estimate > budget_limit:
                raise ProviderTaskBudgetError(
                    "paid submit budget exceeded before reservation: "
                    f"current={current_cost}, reserved={reserved_cost}, "
                    f"next_estimate={estimate}, limit={budget_limit} cents"
                )
        usage = {"status": "submitting"}
        if authorized_batch_id:
            usage["batch_id"] = str(authorized_batch_id)
        if estimate:
            usage["reserved_cost_cents"] = estimate
        database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES(?,?,?,?,?,?,0)",
            (
                reservation_id, job_id, int(shot_idx), provider, intent,
                json.dumps(usage),
            ),
        )
        database.commit()
        return reservation_id
    except Exception:
        database.rollback()
        raise


def promote_reservation(
    database: sqlite3.Connection,
    reservation_id: str,
    *,
    task_id: str,
    job_id: str,
    shot_idx: int,
    provider: str,
    intent: str,
) -> None:
    """Persist the remote id before polling and retire the local submit claim."""
    if not str(task_id or "").strip() or str(task_id) == str(reservation_id):
        raise ProviderTaskGateError(
            "Remote provider task_id must differ from the local reservation; "
            "reservation evidence was preserved"
        )
    try:
        database.execute("BEGIN IMMEDIATE")
        reservation = database.execute(
            "SELECT usage_json FROM provider_calls WHERE task_id=? "
            "AND task_id LIKE 'local-submit:%'",
            (reservation_id,),
        ).fetchone()
        if reservation is None:
            raise ProviderTaskGateError(
                f"local provider reservation not found: {reservation_id}"
            )
        try:
            reservation_usage = json.loads(reservation[0] or "{}")
        except (json.JSONDecodeError, TypeError):
            reservation_usage = {}
        pending_usage = {"status": "pending"}
        if isinstance(reservation_usage, dict):
            for key in ("batch_id", "reserved_cost_cents"):
                if reservation_usage.get(key):
                    pending_usage[key] = reservation_usage[key]
        existing = database.execute(
            "SELECT job_id,shot_idx,provider,intent FROM provider_calls "
            "WHERE task_id=?",
            (task_id,),
        ).fetchone()
        expected_owner = (job_id, int(shot_idx), provider, intent)
        if existing is not None:
            actual_owner = tuple(existing)
            if actual_owner != expected_owner:
                raise ProviderTaskGateError(
                    "Provider task_id ownership conflict; local reservation "
                    f"remains unresolved: {task_id}"
                )
        else:
            database.execute(
                "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,"
                "intent,usage_json,cost_cents) VALUES(?,?,?,?,?,?,0)",
                (
                    task_id, job_id, int(shot_idx), provider, intent,
                    json.dumps(pending_usage),
                ),
            )
        database.execute(
            "DELETE FROM provider_calls WHERE task_id=?", (reservation_id,)
        )
        database.commit()
    except Exception:
        database.rollback()
        raise


def mark_action_required(
    database: sqlite3.Connection, task_id: str, *, message: str = ""
) -> None:
    row = database.execute(
        "SELECT usage_json FROM provider_calls WHERE task_id=?", (task_id,)
    ).fetchone()
    usage: dict = {}
    if row:
        try:
            usage = json.loads(row[0] or "{}")
        except (json.JSONDecodeError, TypeError):
            usage = {}
    usage.update({"status": "action_required"})
    if message:
        usage["message"] = str(message)[:500]
    database.execute(
        "UPDATE provider_calls SET usage_json=? WHERE task_id=?",
        (json.dumps(usage, ensure_ascii=False), task_id),
    )
    database.commit()


def release_reservation(database: sqlite3.Connection, reservation_id: str) -> None:
    database.execute(
        "DELETE FROM provider_calls WHERE task_id=? AND task_id LIKE 'local-submit:%'",
        (reservation_id,),
    )
    database.commit()


def upsert_final_call(
    database: sqlite3.Connection,
    *,
    task_id: str,
    job_id: str,
    shot_idx: int,
    provider: str,
    intent: str,
    status: str,
    usage: dict | None = None,
    cost_cents: int = 0,
) -> int:
    """Persist final provider evidence and return the newly billable delta."""
    owned_transaction = not database.in_transaction
    try:
        if owned_transaction:
            database.execute("BEGIN IMMEDIATE")
        else:
            # Upgrade a caller-owned deferred transaction to a write lock
            # before checking ownership, even when the table is unchanged.
            database.execute(
                "UPDATE provider_calls SET task_id=task_id WHERE 0"
            )
        existing = database.execute(
            "SELECT job_id,shot_idx,provider,intent,cost_cents,usage_json "
            "FROM provider_calls WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if existing is not None:
            actual_owner = tuple(existing[:4])
            expected_owner = (job_id, int(shot_idx), provider, intent)
            if actual_owner != expected_owner:
                raise ProviderTaskGateError(
                    "Provider task_id ownership conflict; final call rejected: "
                    f"{task_id}"
                )
        old_cost = max(0, int(existing[4] or 0)) if existing else 0
        final_cost = max(old_cost, max(0, int(cost_cents or 0)))
        payload = dict(usage or {})
        payload["status"] = status
        existing_payload: dict = {}
        if existing is not None:
            try:
                existing_payload = json.loads(existing[5] or "{}")
            except (json.JSONDecodeError, TypeError):
                existing_payload = {}
            if isinstance(existing_payload, dict):
                for key in ("batch_id", "reserved_cost_cents"):
                    if existing_payload.get(key):
                        payload[key] = existing_payload[key]
        if existing is not None and str(status).strip().casefold() == "pending":
            existing_status = str(
                existing_payload.get("status")
                if isinstance(existing_payload, dict) else ""
            ).strip().casefold()
            if existing_status not in {"", "pending", "submitting"}:
                payload = existing_payload
        database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "usage_json,cost_cents) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET intent=excluded.intent,"
            "usage_json=excluded.usage_json,cost_cents=excluded.cost_cents",
            (
                task_id, job_id, int(shot_idx), provider, intent,
                json.dumps(payload, ensure_ascii=False), final_cost,
            ),
        )
        if owned_transaction and getattr(database, "isolation_level", None) is None:
            database.commit()
        return final_cost - old_cost
    except Exception:
        if owned_transaction:
            database.rollback()
        raise
