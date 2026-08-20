"""Persistent local circuit breaker for paid provider submissions."""
from __future__ import annotations

import ast
import json
import sqlite3


AVAILABLE = "available"
ACTION_REQUIRED = "action_required"


_ACCOUNT_CODES = {
    value.casefold(): value for value in (
        "AccountOverdue",
        "AccountDisabled",
        "AccountSuspended",
        "InvalidAccessKey",
        "AuthenticationFailed",
        "Unauthorized",
        "account_unavailable",
    )
}


def _collect_account_codes(value) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in {"code", "error_code"}:
                normalized = _ACCOUNT_CODES.get(str(item).casefold())
                if normalized:
                    found.add(normalized)
            found.update(_collect_account_codes(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(_collect_account_codes(item))
    return found


def _safe_message(message: str, *, code: str = "") -> str:
    """Persist only allowlisted provider codes plus a fixed operator summary."""
    raw = str(message or "").strip()
    parsed = None
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(raw)
            break
        except (ValueError, SyntaxError, TypeError, json.JSONDecodeError):
            continue
    codes = _collect_account_codes(parsed) if parsed is not None else set()
    for candidate in (code, *raw.replace(":", " ").replace(";", " ").split()):
        normalized = _ACCOUNT_CODES.get(str(candidate).strip("'\"{},[]").casefold())
        if normalized:
            codes.add(normalized)
    summary = "Seedance 账号需要人工处理"
    if codes:
        summary += "; provider_code=" + ",".join(sorted(codes))
    return summary


def _safe_reason_code(code: str) -> str:
    return _ACCOUNT_CODES.get(str(code or "").strip().casefold(), "account_unavailable")


def get_status(database: sqlite3.Connection, provider: str) -> dict:
    row = database.execute(
        "SELECT provider,status,reason_code,reason_message,blocked_at,cleared_at "
        "FROM provider_runtime WHERE provider=?",
        (str(provider).strip().lower(),),
    ).fetchone()
    if row is None:
        return {
            "provider": str(provider).strip().lower(),
            "status": AVAILABLE,
            "reason_code": None,
            "reason_message": None,
            "blocked_at": None,
            "cleared_at": None,
        }
    return dict(row)


def assert_available(database: sqlite3.Connection, provider: str) -> None:
    status = get_status(database, provider)
    if status["status"] != ACTION_REQUIRED:
        return
    # Import lazily so the generic persistence module does not own a parallel
    # provider-error taxonomy.
    from .clients.seedance import SeedanceAccountUnavailableError

    error = SeedanceAccountUnavailableError(
        status["reason_message"]
        or "Seedance 账号仍处于本地阻断状态，请先处理账号问题并人工解除。"
    )
    error.provider_code = status["reason_code"]
    error.code = "account_unavailable"
    error.error_code = error.code
    error.stable_code = error.code
    raise error


def block(
    database: sqlite3.Connection,
    provider: str,
    code: str,
    message: str,
) -> dict:
    normalized_provider = str(provider).strip().lower()
    database.execute(
        "INSERT INTO provider_runtime("
        "provider,status,reason_code,reason_message,blocked_at,cleared_at) "
        "VALUES(?,'action_required',?,?,CURRENT_TIMESTAMP,NULL) "
        "ON CONFLICT(provider) DO UPDATE SET "
        "status='action_required',reason_code=excluded.reason_code,"
        "reason_message=excluded.reason_message,"
        "blocked_at=CASE WHEN provider_runtime.status='action_required' "
        "THEN provider_runtime.blocked_at ELSE CURRENT_TIMESTAMP END,"
        "cleared_at=NULL",
        (
            normalized_provider,
            _safe_reason_code(code),
            _safe_message(message, code=str(code or "")),
        ),
    )
    database.commit()
    return get_status(database, normalized_provider)


def clear(database: sqlite3.Connection, provider: str) -> dict:
    normalized_provider = str(provider).strip().lower()
    database.execute(
        "INSERT INTO provider_runtime("
        "provider,status,reason_code,reason_message,blocked_at,cleared_at) "
        "VALUES(?,'available',NULL,NULL,NULL,CURRENT_TIMESTAMP) "
        "ON CONFLICT(provider) DO UPDATE SET "
        "status='available',reason_code=NULL,reason_message=NULL,"
        "cleared_at=CASE WHEN provider_runtime.status='available' "
        "THEN provider_runtime.cleared_at ELSE CURRENT_TIMESTAMP END",
        (normalized_provider,),
    )
    database.commit()
    return get_status(database, normalized_provider)
