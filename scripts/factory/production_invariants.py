"""Audit graph-v2 invariants and repair disposable projections safely.

The frozen Production Spec and persisted node graph are authoritative.  This
module may repair mutable UI/execution projections, but it never recreates a
paid provider task, rewrites a paid node, or deletes generated media.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from . import production_compiler


class ProductionInvariantError(RuntimeError):
    """Raised before execution when frozen paid inputs are inconsistent."""

    stable_code = "execution_contract_invalid"

    def __init__(self, report: "InvariantReport"):
        self.report = report
        codes = ", ".join(issue.code for issue in report.blockers)
        super().__init__(f"production invariants failed: {codes}")


@dataclass(frozen=True)
class InvariantIssue:
    code: str
    severity: str
    subject: str
    message: str


@dataclass(frozen=True)
class InvariantReport:
    job_id: str
    issues: tuple[InvariantIssue, ...]
    repaired_codes: tuple[str, ...] = ()

    @property
    def blockers(self) -> tuple[InvariantIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "blocking")

    @property
    def repairs(self) -> tuple[InvariantIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "repair")


def _json_object(raw: object) -> dict | None:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _master_indexes(storyboard: dict) -> set[int]:
    result: set[int] = set()
    for master in storyboard.get("masters") or []:
        if not isinstance(master, dict):
            continue
        try:
            idx = int(master.get("idx") or 0)
        except (TypeError, ValueError):
            continue
        if idx > 0:
            result.add(idx)
    return result


def _director_references(storyboard: dict) -> set[int]:
    result: set[int] = set()
    for shot in storyboard.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        for key in ("video_master_idx", "audio_master_idx"):
            try:
                idx = int(shot.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if idx > 0:
                result.add(idx)
    return result


def _graph_master_indexes(database: sqlite3.Connection, job_id: str) -> set[int]:
    result: set[int] = set()
    for row in database.execute(
        "SELECT node_id FROM production_nodes WHERE job_id=? "
        "AND node_type='master'",
        (job_id,),
    ).fetchall():
        try:
            result.add(int(str(row["node_id"]).split(":", 1)[1]))
        except (IndexError, TypeError, ValueError):
            continue
    return result


def _master_work_is_protected(
    database: sqlite3.Connection, job_id: str,
) -> bool:
    """Return whether rebuilding shot projections could lose paid work."""
    if database.execute(
        "SELECT 1 FROM shots WHERE job_id=? AND ("
        "state IN ('RUNNING','OK','REVIEW_REQUIRED','REVIEW_DECIDING') "
        "OR cost_cents>0 OR COALESCE(video_path,'')<>'') LIMIT 1",
        (job_id,),
    ).fetchone():
        return True
    if database.execute(
        "SELECT 1 FROM provider_calls WHERE job_id=? AND shot_idx>0 LIMIT 1",
        (job_id,),
    ).fetchone():
        return True
    return bool(database.execute(
        "SELECT 1 FROM production_nodes WHERE job_id=? AND node_type='master' "
        "AND (state IN ('RUNNING','SUCCEEDED','REVIEW_REQUIRED',"
        "'ACTION_REQUIRED','BUDGET_REQUIRED') OR actual_cost_cents>0 "
        "OR COALESCE(provider_task_id,'')<>'') LIMIT 1",
        (job_id,),
    ).fetchone())


def _rebuild_unpaid_master_rows(
    database: sqlite3.Connection, job_id: str, frozen: dict,
) -> None:
    """Recreate the disposable legacy shot projection from frozen masters."""
    available_columns = {
        str(row["name"])
        for row in database.execute("PRAGMA table_info(shots)").fetchall()
    }
    ordered_fields = [
        "job_id", "idx", "duration_sec", "shot_type", "camera", "prompt",
        "voiceover", "subtitle", "state", "narrative_type", "speaker_id",
        "anchor_character",
    ]
    fields = [field for field in ordered_fields if field in available_columns]
    masters = []
    for item in frozen.get("masters") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("idx") or 0)
        except (TypeError, ValueError):
            continue
        if idx > 0:
            masters.append(item)
    database.execute("DELETE FROM shots WHERE job_id=?", (job_id,))
    for master in sorted(masters, key=lambda item: int(item.get("idx") or 0)):
        values = {
            "job_id": job_id,
            "idx": int(master.get("idx") or 0),
            "duration_sec": float(
                master.get("duration") or master.get("duration_sec") or 0
            ),
            "shot_type": str(master.get("shot_type") or ""),
            "camera": str(master.get("camera") or ""),
            "prompt": str(master.get("scene") or master.get("prompt") or ""),
            "voiceover": str(master.get("voiceover") or ""),
            "subtitle": str(
                master.get("subtitle") or master.get("voiceover") or ""
            ),
            "state": "PENDING",
            "narrative_type": str(master.get("type") or "dialogue"),
            "speaker_id": master.get("speaker"),
            "anchor_character": master.get("anchor_character"),
        }
        database.execute(
            f"INSERT INTO shots({','.join(fields)}) "
            f"VALUES({','.join('?' for _ in fields)})",
            tuple(values[field] for field in fields),
        )


def audit(database: sqlite3.Connection, job_id: str) -> InvariantReport:
    """Return every deterministic graph-v2 consistency issue at once."""
    row = database.execute(
        "SELECT execution_engine,production_spec_json,storyboard,final_path "
        "FROM jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"video job not found: {job_id}")
    if str(row["execution_engine"] or "legacy") != "graph_v2":
        return InvariantReport(job_id, ())

    issues: list[InvariantIssue] = []
    spec = _json_object(row["production_spec_json"])
    if spec and spec.get('nodes'):
        from . import native_visual_repair
        for subject in native_visual_repair.graph_errors(database,job_id,spec):
            issues.append(InvariantIssue('native_repair_authorization_invalid','blocking',subject,'native repair graph addition is not authorized'))
    frozen = spec.get("runtime_storyboard") if spec else None
    if not isinstance(frozen, dict):
        issues.append(InvariantIssue(
            "frozen_spec_missing", "blocking", job_id,
            "冻结生产规格缺失或格式无效",
        ))
        return InvariantReport(job_id, tuple(issues))

    frozen_ids = _master_indexes(frozen)
    graph_ids = _graph_master_indexes(database, job_id)
    shot_ids = {
        int(item["idx"])
        for item in database.execute(
            "SELECT idx FROM shots WHERE job_id=?", (job_id,),
        ).fetchall()
    }
    if not frozen_ids or graph_ids != frozen_ids:
        issues.append(InvariantIssue(
            "frozen_graph_master_mismatch", "blocking", job_id,
            "冻结母片与生产图母片集合不一致",
        ))
    if shot_ids != graph_ids:
        protected = _master_work_is_protected(database, job_id)
        issues.append(InvariantIssue(
            (
                "materialized_master_mismatch" if protected
                else "materialized_master_stale"
            ),
            "blocking" if protected else "repair", job_id,
            "母片执行行与生产图母片集合不一致",
        ))

    mutable = _json_object(row["storyboard"])
    mutable_ids = _master_indexes(mutable or {})
    references = _director_references(mutable or {})
    if (
        mutable is None
        or mutable_ids != frozen_ids
        or not (mutable.get("shots") or [])
        or not references.issubset(frozen_ids)
    ):
        issues.append(InvariantIssue(
            "mutable_storyboard_stale", "repair", job_id,
            "可变故事板与冻结生产规格不一致，可安全重建",
        ))

    completed = database.execute(
        "SELECT n.node_id,s.video_path FROM production_nodes n "
        "LEFT JOIN shots s ON s.job_id=n.job_id "
        "AND s.idx=CAST(SUBSTR(n.node_id,8) AS INTEGER) "
        "WHERE n.job_id=? AND n.node_type='master' AND n.state='SUCCEEDED'",
        (job_id,),
    ).fetchall()
    for item in completed:
        path = Path(str(item["video_path"] or "")).expanduser()
        if not str(item["video_path"] or "").strip() or not path.is_file():
            issues.append(InvariantIssue(
                "completed_master_file_missing", "blocking",
                str(item["node_id"]), "已完成母片的媒体文件不存在",
            ))

    compose = database.execute(
        "SELECT state FROM production_nodes WHERE job_id=? "
        "AND node_id='local_compose'",
        (job_id,),
    ).fetchone()
    if compose and str(compose["state"]) == "SUCCEEDED":
        final_path = Path(str(row["final_path"] or "")).expanduser()
        if not str(row["final_path"] or "").strip() or not final_path.is_file():
            issues.append(InvariantIssue(
                "completed_output_file_missing", "blocking", job_id,
                "本地剪辑已完成但成片文件不存在",
            ))
    return InvariantReport(job_id, tuple(issues))


def repair_safe_projections(
    database: sqlite3.Connection, job_id: str,
) -> InvariantReport:
    """Repair only reproducible projections; never alter paid execution facts."""
    before = audit(database, job_id)
    repaired: list[str] = []
    if any(issue.code == "mutable_storyboard_stale" for issue in before.repairs):
        row = database.execute(
            "SELECT production_spec_json FROM jobs WHERE job_id=?", (job_id,),
        ).fetchone()
        spec = _json_object(row["production_spec_json"] if row else None) or {}
        frozen = spec.get("runtime_storyboard")
        if isinstance(frozen, dict):
            executable = production_compiler.ensure_director_timeline(frozen)
            database.execute(
                "UPDATE jobs SET storyboard=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=?",
                (json.dumps(executable, ensure_ascii=False), job_id),
            )
            database.execute(
                "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
                (
                    job_id, "production_invariant_repair", "INFO",
                    "已根据冻结生产规格重建可变故事板与导演时间线",
                ),
            )
            repaired.append("mutable_storyboard_stale")
            database.commit()
    if any(
        issue.code == "materialized_master_stale" for issue in before.repairs
    ):
        row = database.execute(
            "SELECT production_spec_json FROM jobs WHERE job_id=?", (job_id,),
        ).fetchone()
        spec = _json_object(row["production_spec_json"] if row else None) or {}
        frozen = spec.get("runtime_storyboard")
        if isinstance(frozen, dict) and not _master_work_is_protected(
            database, job_id,
        ):
            _rebuild_unpaid_master_rows(database, job_id, frozen)
            database.execute(
                "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
                (
                    job_id, "production_invariant_repair", "INFO",
                    "已根据冻结生产图重建未付费母片执行投影",
                ),
            )
            repaired.append("materialized_master_stale")
            database.commit()
    after = audit(database, job_id)
    return InvariantReport(job_id, after.issues, tuple(repaired))


def repair_all_safe(database: sqlite3.Connection) -> int:
    """Repair every graph-v2 projection during service startup."""
    repaired = 0
    rows = database.execute(
        "SELECT job_id FROM jobs WHERE execution_engine='graph_v2'"
    ).fetchall()
    for row in rows:
        report = repair_safe_projections(database, str(row["job_id"]))
        repaired += len(report.repaired_codes)
    return repaired
