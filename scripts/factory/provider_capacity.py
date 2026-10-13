"""Persisted AIMD capacity learning for paid Provider submissions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import sqlite3


class ProviderCapacityCooldownError(RuntimeError):
    """A definite rate-limit response temporarily closed new submissions."""

    non_retryable = True
    retryable = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _limit(value: object) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _ensure_row(database: sqlite3.Connection, provider: str,
                configured_limit: int) -> None:
    database.execute(
        "INSERT OR IGNORE INTO provider_capacity("
        "provider,configured_limit,learned_limit,success_streak,last_reason) "
        "VALUES(?,?,?,?,?)",
        (provider, configured_limit, configured_limit, 0, "initial_config"),
    )
    database.execute(
        "UPDATE provider_capacity SET configured_limit=?,updated_at=CURRENT_TIMESTAMP "
        "WHERE provider=?",
        (configured_limit, provider),
    )


def _payload(row: sqlite3.Row, configured_limit: int) -> dict:
    learned = _limit(row["learned_limit"])
    configured = _limit(configured_limit)
    cooldown = _parse_timestamp(row["cooldown_until"])
    return {
        "provider": str(row["provider"]),
        "configured_limit": configured,
        "learned_limit": learned,
        "effective_limit": min(configured, learned),
        "success_streak": max(0, int(row["success_streak"] or 0)),
        "cooldown_until": _timestamp(cooldown),
        "cooldown_active": bool(cooldown and cooldown > _utcnow()),
        "last_reason": str(row["last_reason"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def status(database: sqlite3.Connection, provider: str, *,
           configured_limit: int, persist: bool = True) -> dict:
    configured = _limit(configured_limit)
    if persist:
        _ensure_row(database, provider, configured)
        database.commit()
    row = database.execute(
        "SELECT * FROM provider_capacity WHERE provider=?", (provider,)
    ).fetchone()
    if row is None:
        row = {'provider': provider, 'learned_limit': configured, 'success_streak': 0,
               'cooldown_until': None, 'last_reason': 'initial_config', 'updated_at': ''}
    return _payload(row, configured)


def effective_limit(database: sqlite3.Connection, provider: str, *,
                    configured_limit: int) -> int:
    return int(status(
        database, provider, configured_limit=configured_limit
    )["effective_limit"])


def assert_submit_allowed(database: sqlite3.Connection, provider: str) -> None:
    row = database.execute(
        "SELECT cooldown_until FROM provider_capacity WHERE provider=?",
        (provider,),
    ).fetchone()
    cooldown = _parse_timestamp(row["cooldown_until"]) if row else None
    if cooldown and cooldown > _utcnow():
        raise ProviderCapacityCooldownError(
            "Seedance 并发额度刚触发限流，系统已暂停新的提交；"
            "请等待冷却后从失败任务继续，已完成母片不会重做。"
        )


def record_rate_limit(database: sqlite3.Connection, provider: str, *,
                      configured_limit: int,
                      cooldown_seconds: int = 60) -> dict:
    configured = _limit(configured_limit)
    database.execute("BEGIN IMMEDIATE")
    try:
        _ensure_row(database, provider, configured)
        row = database.execute(
            "SELECT learned_limit FROM provider_capacity WHERE provider=?",
            (provider,),
        ).fetchone()
        learned = min(configured, _limit(row["learned_limit"]))
        reduced = max(1, int(math.ceil(learned / 2)))
        cooldown = _utcnow() + timedelta(seconds=max(0, cooldown_seconds))
        database.execute(
            "UPDATE provider_capacity SET learned_limit=?,success_streak=0,"
            "cooldown_until=?,last_reason='definite_rate_limit',"
            "updated_at=CURRENT_TIMESTAMP WHERE provider=?",
            (reduced, _timestamp(cooldown), provider),
        )
        database.commit()
    except BaseException:
        database.rollback()
        raise
    return status(database, provider, configured_limit=configured)


def record_wave_success(database: sqlite3.Connection, provider: str, *,
                        configured_limit: int) -> dict:
    configured = _limit(configured_limit)
    database.execute("BEGIN IMMEDIATE")
    try:
        _ensure_row(database, provider, configured)
        row = database.execute(
            "SELECT learned_limit,success_streak,cooldown_until "
            "FROM provider_capacity WHERE provider=?", (provider,)
        ).fetchone()
        learned = _limit(row["learned_limit"])
        cooldown = _parse_timestamp(row["cooldown_until"])
        if cooldown and cooldown > _utcnow():
            database.commit()
            return status(database, provider, configured_limit=configured)
        streak = max(0, int(row["success_streak"] or 0)) + 1
        reason = "clean_wave"
        if streak >= 5:
            learned = min(configured, learned + 1)
            streak = 0
            reason = "clean_wave_recovery"
        database.execute(
            "UPDATE provider_capacity SET learned_limit=?,success_streak=?,"
            "cooldown_until=NULL,last_reason=?,updated_at=CURRENT_TIMESTAMP "
            "WHERE provider=?",
            (learned, streak, reason, provider),
        )
        database.commit()
    except BaseException:
        database.rollback()
        raise
    return status(database, provider, configured_limit=configured)


def record_ambiguous_submission(database: sqlite3.Connection, provider: str,
                                *, configured_limit: int) -> dict:
    """Observation only: an ambiguous submission must not tune capacity."""
    return status(database, provider, configured_limit=configured_limit)
