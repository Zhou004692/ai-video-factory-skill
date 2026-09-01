"""Read-only audit of recent production jobs using current invariants."""
from __future__ import annotations

from pathlib import Path
import sqlite3

from .. import db as factory_db, production_invariants


PAUSED_STATES = frozenset({
    "ACTION_REQUIRED", "BUDGET_REQUIRED", "REVIEW_REQUIRED",
})


def audit_production_shadow(
    database_path: Path | None = None,
    *,
    recent_limit: int = 30,
) -> dict[str, object]:
    """Audit recent real jobs without initializing or repairing the service."""
    path = Path(database_path or factory_db.database_path()).resolve()
    if not path.is_file():
        return {
            "status": "skipped",
            "skip_reason": "production_db_missing",
            "database_path": str(path),
            "audited_job_count": 0,
            "action_required_job_count": 0,
            "review_required_job_count": 0,
            "regression_codes": [],
            "write_attempts": 0,
            "jobs": [],
        }
    database: sqlite3.Connection | None = None
    try:
        database = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=10,
        )
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA query_only=ON")
        jobs = database.execute(
            "SELECT job_id,title,state,execution_engine,updated_at "
            "FROM jobs ORDER BY updated_at DESC LIMIT ?",
            (max(1, int(recent_limit)),),
        ).fetchall()
        rows: list[dict[str, object]] = []
        regression_codes: set[str] = set()
        action_required_jobs = 0
        review_required_jobs = 0
        for job in jobs:
            job_id = str(job["job_id"])
            nodes = database.execute(
                "SELECT node_id,node_type,state,error_code,next_action,"
                "provider_task_id FROM production_nodes WHERE job_id=? "
                "ORDER BY node_id",
                (job_id,),
            ).fetchall()
            action_count = sum(
                str(node["state"]) == "ACTION_REQUIRED" for node in nodes
            )
            review_count = sum(
                str(node["state"]) == "REVIEW_REQUIRED" for node in nodes
            )
            paused = [
                {
                    "node_id": str(node["node_id"]),
                    "node_type": str(node["node_type"]),
                    "state": str(node["state"]),
                    "error_code": str(node["error_code"] or ""),
                    "next_action": str(node["next_action"] or ""),
                    "has_provider_task": bool(node["provider_task_id"]),
                }
                for node in nodes if str(node["state"]) in PAUSED_STATES
            ]
            if action_count:
                action_required_jobs += 1
            if review_count:
                review_required_jobs += 1
            issues = []
            if str(job["execution_engine"] or "legacy") == "graph_v2":
                issues = list(
                    production_invariants.audit(database, job_id).issues
                )
            job_regressions = sorted({
                issue.code for issue in issues
                if issue.severity == "blocking"
            })
            regression_codes.update(job_regressions)
            rows.append({
                "job_id": job_id,
                "title": str(job["title"] or ""),
                "state": str(job["state"] or ""),
                "execution_engine": str(job["execution_engine"] or "legacy"),
                "updated_at": str(job["updated_at"] or ""),
                "paused_node_count": len(paused),
                "paused_nodes": paused,
                "regression_codes": job_regressions,
                "repairable_codes": sorted({
                    issue.code for issue in issues
                    if issue.severity == "repair"
                }),
            })
        return {
            "status": "failed" if regression_codes else "passed",
            "skip_reason": "",
            "database_path": str(path),
            "audited_job_count": len(rows),
            "action_required_job_count": action_required_jobs,
            "review_required_job_count": review_required_jobs,
            "regression_codes": sorted(regression_codes),
            "write_attempts": 0,
            "jobs": rows,
        }
    except sqlite3.Error as exc:
        return {
            "status": "failed",
            "skip_reason": "production_db_unreadable",
            "database_path": str(path),
            "audited_job_count": 0,
            "action_required_job_count": 0,
            "review_required_job_count": 0,
            "regression_codes": ["shadow_audit_unreadable"],
            "error": str(exc),
            "write_attempts": 0,
            "jobs": [],
        }
    finally:
        if database is not None:
            database.close()
