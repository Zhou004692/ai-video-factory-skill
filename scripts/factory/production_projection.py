"""Read-only projection and reconciliation for v2 production nodes."""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
import json
import uuid

from . import node_state_machine, provider_tasks

TERMINAL_STATES = frozenset({'PUBLISHED', 'REJECTED', 'REJECTED_PENDING_RECONCILIATION'})


PRIORITY = (
    "ACTION_REQUIRED", "BUDGET_REQUIRED", "FAILED", "REVIEW_REQUIRED", "RUNNING",
    "READY", "PENDING", "SUCCEEDED",
)

_SHOT_TO_NODE = {
    "PENDING": "PENDING",
    "GENERATING": "RUNNING",
    "RUNNING": "RUNNING",
    "OK": "SUCCEEDED",
    "REVIEW_REQUIRED": "REVIEW_REQUIRED",
    "REVIEW_DECIDING": "REVIEW_REQUIRED",
    "FAILED": "FAILED",
    "BUDGET_REQUIRED": "BUDGET_REQUIRED",
}


@dataclass(frozen=True)
class ProjectionReport:
    job_id: str
    current_job_state: str
    projected_job_state: str
    next_action: str
    differences: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "current_job_state": self.current_job_state,
            "projected_job_state": self.projected_job_state,
            "next_action": self.next_action,
            "differences": self.differences,
        }


def _projected_state(
    database: sqlite3.Connection, job_id: str, states: set[str],
) -> tuple[str, str]:
    if provider_tasks.unresolved_task_ids(database, job_id):
        return "PROVIDER_ACTION_REQUIRED", "reconcile_provider_task"
    local_compose = database.execute(
        "SELECT state FROM production_nodes WHERE job_id=? AND node_id='local_compose'",
        (job_id,),
    ).fetchone()
    final_review = database.execute(
        "SELECT state FROM production_nodes WHERE job_id=? AND node_id='final_review'",
        (job_id,),
    ).fetchone()
    if final_review and final_review["state"] == "SUCCEEDED":
        return "READY_TO_PUBLISH", "publish_video"
    if final_review and final_review["state"] in {
        "ACTION_REQUIRED", "BUDGET_REQUIRED", "FAILED",
    }:
        return {
            "ACTION_REQUIRED": (
                "PROVIDER_ACTION_REQUIRED", "reconcile_provider_task",
            ),
            "BUDGET_REQUIRED": (
                "BUDGET_CONFIRMATION_REQUIRED", "increase_job_budget",
            ),
            "FAILED": ("FAILED", "preserve_and_diagnose"),
        }[str(final_review["state"])]
    if local_compose and local_compose["state"] == "SUCCEEDED":
        return "COMPOSED", "review_final_video"
    if local_compose and local_compose['state'] == 'REVIEW_REQUIRED' and database.execute(
        "SELECT 1 FROM production_nodes WHERE job_id=? AND node_id='local_compose' AND error_code='content_review_required'", (job_id,)
    ).fetchone():
        return 'PRODUCTION_REVIEW_REQUIRED', 'review_visual_content'
    anchor_rows = database.execute(
        "SELECT node_type,state FROM production_nodes WHERE job_id=? "
        "AND node_type IN ('character_anchor','scene_anchor')",
        (job_id,),
    ).fetchall()
    review_types = {
        str(row["node_type"])
        for row in anchor_rows
        if str(row["state"]) == "REVIEW_REQUIRED"
    }
    anchor_states = {str(row["state"]) for row in anchor_rows}
    anchor_generation_complete = not anchor_states.intersection({
        "ACTION_REQUIRED", "BUDGET_REQUIRED", "FAILED", "RUNNING", "READY", "PENDING",
    })
    if (
        review_types
        and review_types.issubset({"character_anchor", "scene_anchor"})
        and anchor_generation_complete
    ):
        return "ANCHOR_REVIEW_REQUIRED", "approve_continuity_anchors"
    selected = next((state for state in PRIORITY if state in states), "PENDING")
    if selected == "REVIEW_REQUIRED" and database.execute(
        "SELECT 1 FROM character_identity_anchors a WHERE a.job_id=? "
        "AND a.source_type='seedance_identity_fallback' AND a.state='READY' "
        "AND a.approval_state!='APPROVED' AND EXISTS ("
        "SELECT 1 FROM production_nodes n WHERE n.job_id=a.job_id "
        "AND n.node_type='master' AND n.state='REVIEW_REQUIRED' "
        "AND n.error_code='continuity_anchor_review_pending') LIMIT 1",
        (job_id,),
    ).fetchone():
        # Replacement is created while a master runs, so the original anchor
        # node may already be SUCCEEDED. Only an explicit dependency pause,
        # not an unrelated candidate review, belongs to this replacement.
        return "ANCHOR_REVIEW_REQUIRED", "approve_continuity_anchors"
    return {
        "ACTION_REQUIRED": ("PROVIDER_ACTION_REQUIRED", "reconcile_provider_task"),
        "BUDGET_REQUIRED": (
            "BUDGET_CONFIRMATION_REQUIRED", "increase_job_budget",
        ),
        "REVIEW_REQUIRED": ("PRODUCTION_REVIEW_REQUIRED", "review_candidate"),
        "RUNNING": ("PRODUCTION_RUNNING", "view_progress"),
        "READY": ("PRODUCTION_QUEUED", "view_progress"),
        "FAILED": ("FAILED", "preserve_and_diagnose"),
        "PENDING": ("RECOVERY_REQUIRED", "preserve_and_diagnose"),
        "SUCCEEDED": ("RECOVERY_REQUIRED", "preserve_and_diagnose"),
    }[selected]


def reconcile(database: sqlite3.Connection, job_id: str) -> ProjectionReport:
    job = database.execute(
        "SELECT state FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if job is None:
        raise ValueError(f"video job not found: {job_id}")
    current_state = str(job["state"])
    if current_state in TERMINAL_STATES:
        return ProjectionReport(
            job_id=job_id,
            current_job_state=current_state,
            projected_job_state=current_state,
            next_action=("view_publication" if current_state == "PUBLISHED" else
                         "reconcile_rejected_provider_cost" if current_state == "REJECTED_PENDING_RECONCILIATION" else "none"),
            differences=(),
        )
    nodes = database.execute(
        "SELECT node_id,node_type,state FROM production_nodes WHERE job_id=?",
        (job_id,),
    ).fetchall()
    if not nodes:
        raise ValueError(f"production graph not found: {job_id}")
    projected, action = _projected_state(
        database, job_id, {str(row["state"]) for row in nodes},
    )
    differences: list[str] = []
    if current_state != projected:
        differences.append(f"job_state:{job['state']}!={projected}")
    node_by_id = {str(row["node_id"]): str(row["state"]) for row in nodes}
    for shot in database.execute(
        "SELECT idx,state FROM shots WHERE job_id=? ORDER BY idx", (job_id,)
    ).fetchall():
        node_id = f"master:{int(shot['idx'])}"
        node_state = node_by_id.get(node_id)
        expected = _SHOT_TO_NODE.get(str(shot["state"]))
        if node_state is not None and expected and node_state != expected:
            differences.append(
                f"shot_node:{int(shot['idx'])}:{shot['state']}!={node_state}"
            )
    return ProjectionReport(
        job_id=job_id,
        current_job_state=current_state,
        projected_job_state=projected,
        next_action=action,
        differences=tuple(differences),
    )


def persist(database: sqlite3.Connection, job_id: str) -> ProjectionReport:
    """Commit the graph-derived job projection without reviving termination."""
    with node_state_machine.atomic(database):
        report = reconcile(database, job_id)
        if report.current_job_state not in TERMINAL_STATES:
            database.execute(
                "UPDATE jobs SET state=?,production_worker_pid=CASE WHEN ?='PRODUCTION_RUNNING' "
                "THEN production_worker_pid ELSE NULL END,updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=? AND state=?",
                (report.projected_job_state, report.projected_job_state, job_id, report.current_job_state),
            )
            if report.current_job_state != report.projected_job_state:
                database.execute(
                    "INSERT INTO job_transitions(event_id,job_id,event,from_state,to_state,payload_json) VALUES(?,?,?,?,?,?)",
                    (f"{job_id}:graph-projection:{uuid.uuid4().hex}", job_id, "graph_projected",
                     report.current_job_state, report.projected_job_state,
                     json.dumps({"next_action": report.next_action, "differences": report.differences})),
                )
        return report


def mirror_legacy_execution(database: sqlite3.Connection, job_id: str) -> None:
    """Mirror durable legacy states inside the caller's transaction."""
    for shot in database.execute(
        "SELECT idx,state FROM shots WHERE job_id=?", (job_id,)
    ).fetchall():
        target = _SHOT_TO_NODE.get(str(shot["state"]))
        if target:
            database.execute(
                "UPDATE production_nodes SET state=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=? AND node_id=?",
                (target, job_id, f"master:{int(shot['idx'])}"),
            )
    job = database.execute(
        "SELECT state FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if not job:
        return
    job_state = str(job["state"])
    if job_state in {"COMPOSED", "READY_TO_PUBLISH", "PUBLISHED"}:
        database.execute(
            "UPDATE production_nodes SET state='SUCCEEDED',"
            "updated_at=CURRENT_TIMESTAMP WHERE job_id=? "
            "AND node_id='local_compose'",
            (job_id,),
        )
    if job_state in {"READY_TO_PUBLISH", "PUBLISHED"}:
        database.execute(
            "UPDATE production_nodes SET state='SUCCEEDED',"
            "updated_at=CURRENT_TIMESTAMP WHERE job_id=? "
            "AND node_id='final_review'",
            (job_id,),
        )
    unresolved = provider_tasks.unresolved_task_ids(database, job_id)
    if unresolved:
        database.execute(
            "UPDATE production_nodes SET state='ACTION_REQUIRED',"
            "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND state='RUNNING'",
            (job_id,),
        )


def consistency_reports(
    database: sqlite3.Connection, *, limit: int = 100,
) -> tuple[ProjectionReport, ...]:
    rows = database.execute(
        "SELECT DISTINCT job_id FROM production_nodes ORDER BY job_id LIMIT ?",
        (max(1, min(int(limit), 500)),),
    ).fetchall()
    return tuple(reconcile(database, str(row["job_id"])) for row in rows)
