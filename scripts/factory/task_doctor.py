"""Diagnose video tasks and apply bounded, zero-cost repairs.

The doctor never starts a worker, submits a provider request, changes a budget,
accepts a paid candidate, or deletes media.  It repairs only reproducible local
projections and expired local execution leases, then diagnoses the task again.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import uuid

from . import (
    production_executor,
    production_invariants,
    production_projection,
    provider_tasks,
)


LEASE_TIMEOUT_SEC = 60
TERMINAL_JOB_STATES = {"PUBLISHED", "REJECTED"}

_INVARIANT_LABELS = {
    "frozen_spec_missing": (
        "冻结生产规格缺失",
        "任务缺少可作为付费执行依据的 Production Spec，不能自动重建。",
    ),
    "frozen_graph_master_mismatch": (
        "冻结规格与生产图不一致",
        "冻结母片集合与执行节点不一致，自动覆盖可能破坏已付费事实。",
    ),
    "materialized_master_mismatch": (
        "母片执行记录与生产图不一致",
        "已有付费或产物证据，必须人工核对后再处理。",
    ),
    "materialized_master_stale": (
        "母片执行投影需要重建",
        "未发生付费，可根据冻结生产规格安全重建母片执行记录。",
    ),
    "mutable_storyboard_stale": (
        "导演时间线投影已过期",
        "可根据冻结生产规格安全重建可变故事板与导演时间线。",
    ),
    "completed_master_file_missing": (
        "已完成母片文件缺失",
        "数据库记录为完成，但本地母片文件不存在，需人工选择恢复来源或重做。",
    ),
    "completed_output_file_missing": (
        "成片文件缺失",
        "本地合成记录为完成，但成片文件不存在，需重新执行本地合成或恢复文件。",
    ),
}


def ensure_schema(database: sqlite3.Connection) -> None:
    database.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_doctor_runs (
            run_id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            requested_actions_json TEXT NOT NULL DEFAULT '[]',
            applied_actions_json TEXT NOT NULL DEFAULT '[]',
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_task_doctor_runs_entity
            ON task_doctor_runs(entity_type, entity_id, created_at);
        """
    )
    database.commit()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(raw: object) -> datetime:
    try:
        value = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return value.replace(tzinfo=value.tzinfo or timezone.utc)


def _action(
    action_id: str,
    label: str,
    detail: str,
    *,
    kind: str = "automatic",
) -> dict:
    return {
        "id": action_id,
        "label": label,
        "detail": detail,
        "kind": kind,
        "paid": False,
        "destructive": False,
    }


def _finding(
    code: str,
    severity: str,
    category: str,
    title: str,
    detail: str,
    *,
    repair_kind: str,
    action_id: str | None = None,
) -> dict:
    return {
        "code": code,
        "severity": severity,
        "category": category,
        "title": title,
        "detail": detail,
        "repair_kind": repair_kind,
        "action_id": action_id,
    }


def _repairable_contract_nodes(
    database: sqlite3.Connection, job_id: str,
) -> list[str]:
    result: list[str] = []
    rows = database.execute(
        "SELECT node_id,node_type,error_code,provider_task_id,"
        "actual_cost_cents,output_json FROM production_nodes "
        "WHERE job_id=? AND state='FAILED' "
        "AND node_type IN ('character_anchor','scene_anchor','master')",
        (job_id,),
    ).fetchall()
    for row in rows:
        if (
            str(row["provider_task_id"] or "").strip()
            or int(row["actual_cost_cents"] or 0)
        ):
            continue
        try:
            output = json.loads(row["output_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            output = {"invalid": True}
        if output:
            continue
        failure = database.execute(
            "SELECT payload_json FROM production_node_events "
            "WHERE job_id=? AND node_id=? AND event='fail' "
            "ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (job_id, row["node_id"]),
        ).fetchone()
        try:
            payload = json.loads(failure["payload_json"] or "{}") if failure else {}
        except (TypeError, json.JSONDecodeError):
            payload = {}
        error_code = str(row["error_code"] or "")
        error_message = str(payload.get("error") or "")
        repairable = error_code == "FrozenSpecMismatchError" or (
            str(row["node_type"]) == "scene_anchor"
            and error_code == "RuntimeError"
            and error_message.startswith("scene anchor is not ready:")
        ) or (
            str(row["node_type"]) == "master"
            and error_code == "RuntimeError"
            and error_message.startswith("frozen scene anchor is not ready:")
        )
        if repairable:
            result.append(str(row["node_id"]))
    return result


class TaskDoctor:
    """Read task evidence and apply only deterministic local repairs."""

    def __init__(self, database: sqlite3.Connection, root: Path):
        self.database = database
        self.root = Path(root).resolve()
        ensure_schema(database)

    def _audit_history(self, job_id: str) -> list[dict]:
        rows = self.database.execute(
            "SELECT run_id,requested_actions_json,applied_actions_json,status,"
            "created_at FROM task_doctor_runs "
            "WHERE entity_type='video' AND entity_id=? "
            "ORDER BY created_at DESC,rowid DESC LIMIT 10",
            (job_id,),
        ).fetchall()
        result: list[dict] = []
        for row in rows:
            item = dict(row)
            for source, target in (
                ("requested_actions_json", "requested_actions"),
                ("applied_actions_json", "applied_actions"),
            ):
                try:
                    item[target] = json.loads(item.pop(source) or "[]")
                except (TypeError, json.JSONDecodeError):
                    item[target] = []
            result.append(item)
        return result

    def diagnose_video(self, job_id: str) -> dict:
        job = self.database.execute(
            "SELECT job_id,title,state,error,failure_code,execution_engine,"
            "final_path FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise ValueError(f"video job not found: {job_id}")

        findings: list[dict] = []
        safe_actions: dict[str, dict] = {}
        confirmation_actions: dict[str, dict] = {}
        external_actions: dict[str, dict] = {}
        invariant_blocked = False

        invariant_report = production_invariants.audit(self.database, job_id)
        for issue in invariant_report.issues:
            title, detail = _INVARIANT_LABELS.get(
                issue.code, (issue.code, issue.message)
            )
            if issue.severity == "repair":
                action_id = "repair_safe_projections"
                safe_actions[action_id] = _action(
                    action_id,
                    "重建本地执行投影",
                    "仅重建可由冻结规格再生的数据，不改写付费事实。",
                )
                repair_kind = "automatic"
                severity = "warning"
            else:
                invariant_blocked = True
                action_id = "inspect_execution_evidence"
                confirmation_actions[action_id] = _action(
                    action_id,
                    "人工核对生产证据",
                    "存在已付费或不可再生证据，系统不会自动覆盖。",
                    kind="confirmation",
                )
                repair_kind = "confirmation"
                severity = "error"
            findings.append(_finding(
                issue.code, severity, "production_graph", title, detail,
                repair_kind=repair_kind, action_id=action_id,
            ))

        now = _utc_now()
        cutoff = now - timedelta(seconds=LEASE_TIMEOUT_SEC)
        expired_nodes = [
            str(row["node_id"])
            for row in self.database.execute(
                "SELECT node_id,heartbeat_at FROM production_nodes "
                "WHERE job_id=? AND state='RUNNING'",
                (job_id,),
            ).fetchall()
            if _parse_time(row["heartbeat_at"]) <= cutoff
        ]
        if expired_nodes:
            action_id = "recover_expired_leases"
            safe_actions[action_id] = _action(
                action_id,
                "恢复中断的执行节点",
                "回收过期本地租约；已有远端 taskId 的节点只转入核对，不会重提。",
            )
            findings.append(_finding(
                "expired_execution_lease", "error", "worker",
                "执行节点心跳已过期",
                "发现中断节点：" + "、".join(expired_nodes),
                repair_kind="automatic", action_id=action_id,
            ))

        contract_nodes = _repairable_contract_nodes(self.database, job_id)
        if contract_nodes:
            action_id = "retry_repaired_contract_nodes"
            safe_actions[action_id] = _action(
                action_id,
                "应用已知契约修复",
                "仅重排未付费、无远端任务、无产物的已知实现缺陷节点。",
            )
            findings.append(_finding(
                "known_contract_failure", "error", "execution_contract",
                "命中已修复的本地执行缺陷",
                "可安全恢复节点：" + "、".join(contract_nodes),
                repair_kind="automatic", action_id=action_id,
            ))

        node_count = int(self.database.execute(
            "SELECT COUNT(*) FROM production_nodes WHERE job_id=?", (job_id,)
        ).fetchone()[0] or 0)
        if node_count and not invariant_blocked:
            projection = production_projection.reconcile(self.database, job_id)
            if projection.differences:
                action_id = "reconcile_state_projection"
                safe_actions[action_id] = _action(
                    action_id,
                    "校准任务业务状态",
                    "按持久化节点事实更新任务状态，不启动 worker。",
                )
                findings.append(_finding(
                    "state_projection_stale", "warning", "state",
                    "页面任务状态与执行事实不一致",
                    "；".join(projection.differences),
                    repair_kind="automatic", action_id=action_id,
                ))

        unresolved = provider_tasks.unresolved_task_ids(self.database, job_id)
        if unresolved:
            action_id = "reconcile_provider_tasks"
            external_actions[action_id] = _action(
                action_id,
                "核对原远端任务",
                "只查询已有 taskId；需模型账号可用，不会创建新任务。",
                kind="external",
            )
            findings.append(_finding(
                "provider_task_unresolved", "error", "provider",
                "远端任务状态尚未核对",
                "待核对 taskId：" + "、".join(unresolved[:5]),
                repair_kind="external", action_id=action_id,
            ))

        paused_nodes = self.database.execute(
            "SELECT node_id,state,provider_task_id,next_action "
            "FROM production_nodes WHERE job_id=? "
            "AND state IN ('ACTION_REQUIRED','BUDGET_REQUIRED','REVIEW_REQUIRED') "
            "ORDER BY node_id",
            (job_id,),
        ).fetchall()
        action_nodes = [
            row for row in paused_nodes if str(row["state"]) == "ACTION_REQUIRED"
        ]
        if action_nodes and not unresolved:
            action_id = "reconcile_provider_tasks"
            external_actions[action_id] = _action(
                action_id,
                "核对原远端任务",
                "只查询已有 taskId；缺少 taskId 时需人工核对模型控制台。",
                kind="external",
            )
            task_ids = [
                str(row["provider_task_id"] or row["node_id"])
                for row in action_nodes
            ]
            findings.append(_finding(
                "provider_action_required", "error", "provider",
                "执行节点等待外部任务核对",
                "待处理：" + "、".join(task_ids[:5]),
                repair_kind="external", action_id=action_id,
            ))

        budget_nodes = [
            str(row["node_id"]) for row in paused_nodes
            if str(row["state"]) == "BUDGET_REQUIRED"
        ]
        if budget_nodes:
            action_id = "confirm_job_budget"
            confirmation_actions[action_id] = _action(
                action_id,
                "确认任务预算",
                "预算调整后可能继续产生模型费用，因此不属于自动修复。",
                kind="confirmation",
            )
            findings.append(_finding(
                "budget_confirmation_required", "warning", "provider",
                "任务已在扣费前暂停",
                "等待预算确认的节点：" + "、".join(budget_nodes),
                repair_kind="confirmation", action_id=action_id,
            ))

        review_nodes = [
            str(row["node_id"]) for row in paused_nodes
            if str(row["state"]) == "REVIEW_REQUIRED"
        ]
        if review_nodes:
            action_id = "review_generated_candidates"
            confirmation_actions[action_id] = _action(
                action_id,
                "人工复核候选产物",
                "接受、重做或替换产物都必须由用户明确决定。",
                kind="confirmation",
            )
            findings.append(_finding(
                "candidate_review_required", "warning", "production_graph",
                "存在待人工复核的生成结果",
                "待复核节点：" + "、".join(review_nodes),
                repair_kind="confirmation", action_id=action_id,
            ))

        final_path = str(job["final_path"] or "").strip()
        if str(job["state"]) in {"COMPOSED", "READY_TO_PUBLISH", "PUBLISHED"} and (
            not final_path or not Path(final_path).expanduser().is_file()
        ):
            action_id = "rebuild_local_output"
            confirmation_actions[action_id] = _action(
                action_id,
                "重新执行本地合成",
                "需要先核对母片文件完整性；不会自动替换当前交付物。",
                kind="confirmation",
            )
            findings.append(_finding(
                "final_media_missing", "error", "media",
                "成片文件不可用",
                "任务状态显示成片已完成，但本地文件不存在。",
                repair_kind="confirmation", action_id=action_id,
            ))

        failure_code = str(job["failure_code"] or "").strip()
        failure_message = str(job["error"] or "").strip()
        if str(job["state"]) in {"FAILED", "BLOCKED", "RECOVERY_REQUIRED"} and not (
            safe_actions or external_actions or confirmation_actions
        ):
            action_id = "inspect_failure_evidence"
            confirmation_actions[action_id] = _action(
                action_id,
                "查看失败证据",
                "该错误不在自动修复白名单内，保留现场并由人工决定下一步。",
                kind="confirmation",
            )
            findings.append(_finding(
                failure_code or "unclassified_failure", "error", "unknown",
                "任务失败原因尚不能自动修复",
                failure_message or "缺少结构化失败原因。",
                repair_kind="confirmation", action_id=action_id,
            ))

        if safe_actions:
            status = "repairable"
            headline = f"发现 {len(safe_actions)} 项可安全修复问题"
        elif external_actions:
            status = "external_blocked"
            headline = "需要先处理外部账号或远端任务"
        elif confirmation_actions:
            status = "action_required"
            headline = "需要人工确认后继续"
        else:
            status = "healthy"
            headline = "未发现可自动修复的阻塞问题"

        return {
            "entity_type": "video",
            "entity_id": job_id,
            "title": str(job["title"] or job_id),
            "task_state": str(job["state"]),
            "status": status,
            "headline": headline,
            "checked_at": now.isoformat(),
            "findings": findings,
            "safe_actions": list(safe_actions.values()),
            "confirmation_actions": list(confirmation_actions.values()),
            "external_actions": list(external_actions.values()),
            "audit_history": self._audit_history(job_id),
        }

    @staticmethod
    def _snapshot(report: dict) -> dict:
        return {key: value for key, value in report.items() if key != "audit_history"}

    def _apply_action(self, job_id: str, action_id: str) -> dict:
        if action_id == "repair_safe_projections":
            report = production_invariants.repair_safe_projections(
                self.database, job_id
            )
            return {"repaired_codes": list(report.repaired_codes)}
        if action_id == "recover_expired_leases":
            return {
                "recovered_nodes": production_executor.recover_expired_nodes(
                    self.database,
                    job_id,
                    lease_timeout_sec=LEASE_TIMEOUT_SEC,
                )
            }
        if action_id == "retry_repaired_contract_nodes":
            return {
                "recovered_nodes": list(
                    production_executor.retry_repaired_contract_nodes(
                        self.database, job_id
                    )
                )
            }
        if action_id == "reconcile_state_projection":
            projection = production_projection.reconcile(self.database, job_id)
            if projection.current_job_state in TERMINAL_JOB_STATES:
                return {"state": projection.current_job_state, "changed": False}
            self.database.execute(
                "UPDATE jobs SET state=?,error=NULL,failure_code=NULL,"
                "updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
                (projection.projected_job_state, job_id),
            )
            self.database.commit()
            return {"state": projection.projected_job_state, "changed": True}
        raise ValueError(f"unsupported safe repair action: {action_id}")

    def repair_video(
        self, job_id: str, action_ids: list[str] | tuple[str, ...] | None = None
    ) -> dict:
        before = self.diagnose_video(job_id)
        requested = [str(item) for item in (action_ids or []) if str(item)]
        automatic_all = action_ids is None
        applied: list[dict] = []
        completed_ids: set[str] = set()

        for _ in range(4):
            current = self.diagnose_video(job_id)
            available = {
                str(item["id"]): item for item in current["safe_actions"]
            }
            if automatic_all:
                selected = [
                    action_id for action_id in available
                    if action_id not in completed_ids
                ]
            else:
                unknown = [
                    action_id for action_id in requested
                    if action_id not in available and action_id not in completed_ids
                ]
                if unknown:
                    raise ValueError(
                        "repair action is not currently safe: " + ", ".join(unknown)
                    )
                selected = [
                    action_id for action_id in requested
                    if action_id in available and action_id not in completed_ids
                ]
            if not selected:
                break
            for action_id in selected:
                outcome = self._apply_action(job_id, action_id)
                applied.append({"id": action_id, "outcome": outcome})
                completed_ids.add(action_id)
            if not automatic_all:
                break

        after = self.diagnose_video(job_id)
        run_id = f"doctor-{uuid.uuid4().hex}"
        status = "repaired" if applied else "no_change"
        self.database.execute(
            "INSERT INTO task_doctor_runs("
            "run_id,entity_type,entity_id,requested_actions_json,"
            "applied_actions_json,before_json,after_json,status) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                run_id,
                "video",
                job_id,
                json.dumps(requested, ensure_ascii=False),
                json.dumps(applied, ensure_ascii=False),
                json.dumps(self._snapshot(before), ensure_ascii=False),
                json.dumps(self._snapshot(after), ensure_ascii=False),
                status,
            ),
        )
        self.database.execute(
            "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
            (
                job_id,
                "task_doctor",
                "INFO",
                (
                    "诊断修复已完成：" + "、".join(
                        item["id"] for item in applied
                    )
                    if applied else "诊断完成：没有可执行的安全修复"
                ),
            ),
        )
        self.database.commit()
        return {
            "audit_id": run_id,
            "status": status,
            "requested_actions": requested,
            "applied_actions": applied,
            "before": before,
            "after": after,
        }
