"""Read-only eligibility checks for GET-only paid master recovery."""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3


@dataclass(frozen=True)
class RecoveryCandidate:
    index: int
    node_id: str
    task_id: str
    output_json: str


@dataclass(frozen=True)
class RecoveryAssessment:
    eligible: bool
    reason: str
    candidates: tuple[RecoveryCandidate, ...] = ()

    @property
    def indexes(self) -> tuple[int, ...]:
        return tuple(item.index for item in self.candidates)


_INCOMPLETE_REASON = (
    "任务包含已付费母片，但部分待恢复母片尚未生成或缺少匹配的完成任务；"
    "请先核对母片任务，不能重新提交"
)
_DAMAGED_REASON = "已付费母片任务证据损坏，不能重新提交"
_UNCONFIRMED_REASON = "已付费母片任务未确认完成，不能重新提交"


def assess(
    database: sqlite3.Connection,
    job_id: str,
    indexes: list[int] | tuple[int, ...] | None = None,
) -> RecoveryAssessment | None:
    """Return a fail-closed recovery assessment without mutating task state.

    ``None`` means this is not a paid-master download recovery case. Once any
    unfinished master contains paid evidence, every unfinished master must be
    bound to a completed Seedance task owned by the same frozen node.
    """
    job = database.execute(
        "SELECT state,execution_engine,production_spec_hash FROM jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if not job or job["execution_engine"] != "graph_v2" or job["state"] != "FAILED":
        return None
    if indexes is None:
        target_rows = database.execute(
            "SELECT idx,state,cost_cents FROM shots WHERE job_id=? AND state!='OK' "
            "ORDER BY idx",
            (job_id,),
        ).fetchall()
    else:
        normalized = tuple(sorted({int(value) for value in indexes}))
        if not normalized:
            return None
        placeholders = ",".join("?" for _ in normalized)
        target_rows = database.execute(
            f"SELECT idx,state,cost_cents FROM shots WHERE job_id=? "
            f"AND idx IN ({placeholders}) ORDER BY idx",
            (job_id, *normalized),
        ).fetchall()
        if tuple(int(row["idx"]) for row in target_rows) != normalized:
            return RecoveryAssessment(False, _INCOMPLETE_REASON)
    if not target_rows:
        return None

    inspected: list[tuple[sqlite3.Row, sqlite3.Row | None, list[sqlite3.Row]]] = []
    has_paid_evidence = False
    for shot in target_rows:
        idx = int(shot["idx"])
        node = database.execute(
            "SELECT node_id,node_type,state,spec_hash,provider_task_id,"
            "actual_cost_cents,output_json FROM production_nodes "
            "WHERE job_id=? AND node_id=?",
            (job_id, f"master:{idx}"),
        ).fetchone()
        calls = database.execute(
            "SELECT task_id,shot_idx,provider,usage_json,cost_cents "
            "FROM provider_calls WHERE job_id=? AND shot_idx=? ORDER BY rowid",
            (job_id, idx),
        ).fetchall()
        has_paid_evidence = has_paid_evidence or bool(
            int(shot["cost_cents"] or 0) > 0
            or (node and (
                str(node["provider_task_id"] or "").strip()
                or int(node["actual_cost_cents"] or 0) > 0
            ))
            or calls
        )
        inspected.append((shot, node, calls))
    if not has_paid_evidence:
        return None

    candidates: list[RecoveryCandidate] = []
    for shot, node, calls in inspected:
        idx = int(shot["idx"])
        if (
            node is None
            or node["node_type"] != "master"
            or node["state"] != "FAILED"
            or node["spec_hash"] != job["production_spec_hash"]
        ):
            return RecoveryAssessment(False, _INCOMPLETE_REASON)
        task_id = str(node["provider_task_id"] or "").strip()
        matches = [
            row for row in calls
            if str(row["task_id"] or "").strip() == task_id
            and int(row["shot_idx"] or 0) == idx
            and row["provider"] == "seedance"
        ]
        if not task_id or len(matches) != 1:
            return RecoveryAssessment(False, _INCOMPLETE_REASON)
        try:
            usage = json.loads(matches[0]["usage_json"] or "{}")
        except (TypeError, ValueError):
            return RecoveryAssessment(False, _DAMAGED_REASON)
        if not isinstance(usage, dict):
            return RecoveryAssessment(False, _DAMAGED_REASON)
        if usage.get("status") != "completed":
            return RecoveryAssessment(False, _UNCONFIRMED_REASON)
        try:
            output = json.loads(node["output_json"] or "{}")
        except (TypeError, ValueError):
            return RecoveryAssessment(False, _DAMAGED_REASON)
        if not isinstance(output, dict):
            return RecoveryAssessment(False, _DAMAGED_REASON)
        candidates.append(RecoveryCandidate(
            index=idx,
            node_id=str(node["node_id"]),
            task_id=task_id,
            output_json=str(node["output_json"] or "{}"),
        ))
    return RecoveryAssessment(
        True,
        "已完成的远端母片仅需重新下载；恢复过程不会再次提交或计费",
        tuple(candidates),
    )
