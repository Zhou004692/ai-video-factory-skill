"""Production compilation and gray-control application service."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sqlite3
from typing import Callable

from .. import (
    production_adapters, production_contract, production_graph,
    production_planner,
)
from ..clients import music


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


class ProductionService:
    """Own pre-paid compilation and execution-engine assignment decisions."""

    def __init__(
        self,
        database: sqlite3.Connection,
        root: Path,
        *,
        start_handler: Callable[..., dict] | None = None,
        resume_handler: Callable[..., dict] | None = None,
        review_handler: Callable[..., dict] | None = None,
        reconcile_handler: Callable[..., dict] | None = None,
    ):
        self.database = database
        self.root = Path(root).resolve()
        self.env_path = self.root / ".env"
        self._start_handler = start_handler
        self._resume_handler = resume_handler
        self._review_handler = review_handler
        self._reconcile_handler = reconcile_handler

    def start(
        self,
        job_id: str,
        *,
        confirmed: bool,
        approved_budget_cents: int | None = None,
        high_risk_confirmed: bool = False,
    ) -> dict:
        """Enter the frozen production graph through one control boundary."""
        if self._start_handler is None:
            raise RuntimeError("production start handler is not configured")
        return self._start_handler(
            job_id,
            confirmed=confirmed,
            approved_budget_cents=approved_budget_cents,
            high_risk_confirmed=high_risk_confirmed,
        )

    def resume(self, job_id: str) -> dict:
        """Resume one paused job through the production control boundary."""
        if self._resume_handler is None:
            raise RuntimeError("production resume handler is not configured")
        return self._resume_handler(job_id)

    def review(
        self,
        job_id: str,
        *,
        decision: str,
        manual_minutes: float = 0,
        notes: str = "",
        first_pass: bool | None = None,
        checks: dict[str, bool] | None = None,
    ) -> dict:
        """Persist the final human decision through one control boundary."""
        if self._review_handler is None:
            raise RuntimeError("production review handler is not configured")
        return self._review_handler(
            job_id,
            decision=decision,
            manual_minutes=manual_minutes,
            notes=notes,
            first_pass=first_pass,
            checks=checks,
        )

    def reconcile(self, job_id: str, task_id: str) -> dict:
        """Reconcile one known remote task without creating a new submit."""
        if self._reconcile_handler is None:
            raise RuntimeError("provider reconciliation handler is not configured")
        return self._reconcile_handler(job_id, task_id)

    def assign_execution_engine(self, job_id: str, engine: str) -> dict:
        """Select an engine only while the job is unpaid and storyboarded."""
        target = str(engine or "").strip()
        if target not in {"legacy", "graph_v2"}:
            raise ValueError("execution engine must be legacy or graph_v2")
        row = self.database.execute(
            "SELECT state,cost_cents FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise ValueError("video job not found")
        paid_calls = int(self.database.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE job_id=?", (job_id,)
        ).fetchone()[0] or 0)
        if paid_calls or int(row["cost_cents"] or 0) > 0:
            raise ValueError("execution engine cannot change after paid work")
        if str(row["state"]) != "STORYBOARDED":
            raise ValueError(
                "execution engine can only change before production starts"
            )
        compilation = None
        if target == "graph_v2":
            compilation = self.compile(job_id)
            if not compilation.get("compilable"):
                raise ValueError(
                    "Production Spec v2 must compile before graph assignment"
                )
        self.database.execute(
            "UPDATE jobs SET execution_engine=?,updated_at=CURRENT_TIMESTAMP "
            "WHERE job_id=? AND state='STORYBOARDED'",
            (target, job_id),
        )
        self.database.commit()
        return {
            "job_id": job_id,
            "execution_engine": target,
            "compilable": bool(compilation and compilation.get("compilable")),
            "spec_hash": (
                str(compilation.get("spec_hash") or "")
                if compilation else None
            ),
        }

    def compile(self, job_id: str) -> dict:
        """Compile and explain v2 production without a provider submission."""
        job = self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not job:
            raise ValueError("视频任务不存在")
        try:
            runtime_storyboard = json.loads(job["storyboard"] or "{}")
        except json.JSONDecodeError:
            runtime_storyboard = {}
        runtime_settings = runtime_storyboard.setdefault(
            "production_settings", {}
        )
        runtime_settings.setdefault(
            "music_provider",
            music.resolve_provider(configured=_read_env(self.env_path)),
        )
        compilation = production_planner.compile_job_spec(
            self.database,
            job_id,
            self.root,
            storyboard_override=runtime_storyboard,
            persist=False,
            target_version=2,
        )
        issue_payload = [{
            "code": issue.code,
            "severity": issue.severity,
            "stage": issue.stage,
            "subject": issue.subject,
            "message": issue.message,
        } for issue in compilation.issues]
        if compilation.spec is None:
            return {
                "job_id": job_id,
                "compilable": False,
                "blockers": [
                    issue for issue in issue_payload
                    if issue["severity"] == "blocking"
                ],
                "decisions": [
                    issue for issue in issue_payload
                    if issue["severity"] == "decision"
                ],
                "required_content": list(compilation.required_content),
            }
        spec = compilation.spec
        self.persist_compiled_execution(spec)
        adaptation_actions = list(dict.fromkeys(
            action for master in spec.masters
            for action in master.adaptation_actions
        ))
        preserved = list(dict.fromkeys(
            item for master in spec.masters
            for item in master.preserved_requirements
        ))
        node_counts = Counter(node.node_type for node in spec.nodes)
        return {
            "job_id": job_id,
            "compilable": True,
            "spec_hash": spec.spec_hash,
            "version": spec.version,
            "mode": spec.mode,
            "reliability_commitment": spec.reliability_commitment,
            "complexity": dict(spec.complexity),
            "required_content": list(compilation.required_content),
            "preserved_content": preserved,
            "adaptation_actions": adaptation_actions,
            "blockers": [],
            "decisions": [
                issue for issue in issue_payload
                if issue["severity"] == "decision"
            ],
            "estimated_cost_cents": spec.estimated_cost_cents,
            "budget_cents": spec.budget_cents,
            "master_count": len(spec.masters),
            "node_counts": dict(node_counts),
            "nodes": [
                {
                    "node_id": node.node_id,
                    "node_type": node.node_type,
                    "paid": node.paid,
                    "estimated_cost_cents": node.estimated_cost_cents,
                    "quality_gates": list(node.quality_gates),
                }
                for node in spec.nodes
            ],
            "edges": [
                {
                    "from": edge.from_node_id,
                    "to": edge.to_node_id,
                    "type": edge.edge_type,
                }
                for edge in spec.edges
            ],
        }

    def persist_compiled_execution(self, spec) -> None:
        """Commit the graph, executable rows and frozen job spec as one unit."""
        graph = production_graph.compile_graph(spec)
        production_graph.assert_graph_replaceable(self.database, graph)
        with self.database:
            production_adapters.materialize_frozen_execution_inputs(
                self.database, spec, commit=False,
            )
            production_adapters.validate_frozen_execution_inputs(
                self.database, spec,
            )
            production_graph.persist_graph(
                self.database, spec, commit=False,
            )
            production_contract.persist_production_spec(
                self.database, spec, commit=False,
            )
