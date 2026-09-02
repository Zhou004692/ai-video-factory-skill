"""Read-only audit of recent production jobs using current invariants."""
from __future__ import annotations

from pathlib import Path
import sqlite3

from .. import db as factory_db, production_invariants, production_projection
from .management_actions import (
    BACKEND_VIDEO_ACTION_IDS,
    console_dispatch_action_ids,
    normalize_flow_action,
)


PAUSED_STATES = frozenset({
    "ACTION_REQUIRED", "BUDGET_REQUIRED", "REVIEW_REQUIRED",
})
JOB_STATE_RECOVERY_ACTIONS = {
    "ANCHOR_REVIEW_REQUIRED": "review_continuity_anchors",
    "BLOCKED": "preserve_and_diagnose",
    "BUDGET_CONFIRMATION_REQUIRED": "increase_job_budget",
    "FAILED": "preserve_and_diagnose",
    "PRODUCTION_PAUSED": "resume_failed_video",
    "PRODUCTION_REVIEW_REQUIRED": "review_master_candidates",
    "PROVIDER_ACTION_REQUIRED": "reconcile_provider_task",
    "REJECTED_PENDING_RECONCILIATION": "reconcile_rejected_provider_cost",
}


def _recovery_action(
    database: sqlite3.Connection,
    job_id: str,
    job_state: str,
    paused: list[dict[str, object]],
) -> tuple[str, str]:
    raw_actions = {
        str(item.get("next_action") or "").strip()
        for item in paused
        if str(item.get("next_action") or "").strip()
    }
    normalized_raw = {normalize_flow_action(action) for action in raw_actions}
    unknown_raw = normalized_raw - BACKEND_VIDEO_ACTION_IDS
    if unknown_raw:
        return sorted(unknown_raw)[0], "unknown_persisted_action"
    if normalized_raw:
        action = sorted(normalized_raw)[0]
    elif job_state in JOB_STATE_RECOVERY_ACTIONS:
        action = JOB_STATE_RECOVERY_ACTIONS[job_state]
    else:
        try:
            projection = production_projection.reconcile(database, job_id)
            action = normalize_flow_action(projection.next_action)
        except (ValueError, sqlite3.Error):
            action = ""
    if not action:
        return "", "missing_recovery_action"
    if action not in BACKEND_VIDEO_ACTION_IDS:
        return action, "unknown_recovery_action"
    if action not in console_dispatch_action_ids():
        return action, "management_ui_action_missing"
    return action, ""


def audit_production_shadow(
    database_path: Path | None = None,
    *,
    recent_limit: int = 100,
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
            "actionable_pause_job_count": 0,
            "unactionable_pause_job_count": 0,
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
            "SELECT job_id,title,state,execution_engine,error,updated_at "
            "FROM jobs ORDER BY updated_at DESC LIMIT ?",
            (max(1, int(recent_limit)),),
        ).fetchall()
        rows: list[dict[str, object]] = []
        regression_codes: set[str] = set()
        action_required_jobs = 0
        review_required_jobs = 0
        actionable_pause_jobs = 0
        unactionable_pause_jobs = 0
        for job in jobs:
            job_id = str(job["job_id"])
            job_state = str(job["state"] or "")
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
            if action_count or job_state in {
                "PROVIDER_ACTION_REQUIRED", "BUDGET_CONFIRMATION_REQUIRED",
                "REJECTED_PENDING_RECONCILIATION",
            }:
                action_required_jobs += 1
            if review_count or job_state in {
                "ANCHOR_REVIEW_REQUIRED", "PRODUCTION_REVIEW_REQUIRED",
            }:
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
            recovery_action = ""
            availability_status = "clear"
            availability_reason = ""
            availability_findings: list[dict[str, str]] = []
            if paused or job_state in JOB_STATE_RECOVERY_ACTIONS:
                recovery_action, availability_reason = _recovery_action(
                    database, job_id, job_state, paused,
                )
                if availability_reason:
                    availability_status = "blocked"
                    unactionable_pause_jobs += 1
                    job_regressions = sorted({
                        *job_regressions, "production_pause_unactionable",
                    })
                    availability_findings.append({
                        "code": "production_pause_unactionable",
                        "severity": "blocking",
                        "message": availability_reason,
                    })
                else:
                    availability_status = "actionable"
                    actionable_pause_jobs += 1
                    availability_findings.append({
                        "code": "production_pause_actionable",
                        "severity": "attention",
                        "message": recovery_action,
                    })
            regression_codes.update(job_regressions)
            rows.append({
                "job_id": job_id,
                "title": str(job["title"] or ""),
                "state": job_state,
                "error": str(job["error"] or ""),
                "execution_engine": str(job["execution_engine"] or "legacy"),
                "updated_at": str(job["updated_at"] or ""),
                "paused_node_count": len(paused),
                "paused_nodes": paused,
                "regression_codes": job_regressions,
                "repairable_codes": sorted({
                    issue.code for issue in issues
                    if issue.severity == "repair"
                }),
                "availability_status": availability_status,
                "availability_reason": availability_reason,
                "recovery_action": recovery_action,
                "availability_findings": availability_findings,
            })
        return {
            "status": (
                "failed" if regression_codes
                else "attention" if actionable_pause_jobs
                else "passed"
            ),
            "skip_reason": "",
            "database_path": str(path),
            "audited_job_count": len(rows),
            "action_required_job_count": action_required_jobs,
            "review_required_job_count": review_required_jobs,
            "actionable_pause_job_count": actionable_pause_jobs,
            "unactionable_pause_job_count": unactionable_pause_jobs,
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
            "actionable_pause_job_count": 0,
            "unactionable_pause_job_count": 0,
            "regression_codes": ["shadow_audit_unreadable"],
            "error": str(exc),
            "write_attempts": 0,
            "jobs": [],
        }
    finally:
        if database is not None:
            database.close()
