"""Traverse the persisted production graph and all user-facing pauses."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3

from .. import (
    creative_ir,
    db as factory_db,
    node_state_machine,
    production_compiler,
    production_executor,
    production_graph_runner,
)
from ..services import ProductionService
from ..steps import s6_compose
from .invariants import (
    SEMANTIC_CHECK_IDS,
    evaluate_media_artifact,
    evaluate_production_spec,
    evaluate_report,
)
from .mock_seedance import ScriptedSeedance
from .model import ExplorationReport, Finding, FlowScenario, RepairResult
from .repairs import apply_repair


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROFILE_FILES = {
    "stable": "stable_single_dialogue.json",
    "enhanced": "enhanced_two_person_continuity.json",
    "experimental": "experimental_multi_character_action.json",
}
TERMINAL_STATES = frozenset({"READY_TO_PUBLISH", "PUBLISHED"})


def _open_database(path: Path) -> sqlite3.Connection:
    database = sqlite3.connect(str(path))
    database.row_factory = sqlite3.Row
    return database


def _prepare_database(path: Path, scenario: FlowScenario) -> tuple[sqlite3.Connection, str]:
    database = _open_database(path)
    database.executescript(factory_db.DDL)
    profile_path = (
        PROJECT_ROOT / "tests" / "fixtures" / "production_profiles"
        / PROFILE_FILES[scenario.profile]
    )
    source = json.loads(profile_path.read_text(encoding="utf-8"))
    job_id = "J-" + scenario.scenario_id.upper().replace("_", "-")
    database.execute(
        "INSERT INTO jobs(job_id,topic_id,title,state,execution_engine,storyboard) "
        "VALUES(?,?,?,?,?,?)",
        (
            job_id,
            "T-" + scenario.scenario_id.upper(),
            f"R4 flow guard {scenario.scenario_id}",
            "PRODUCTION_RUNNING",
            "graph_v2",
            json.dumps(source, ensure_ascii=False),
        ),
    )
    compilation = production_compiler.compile_ir(
        creative_ir.from_storyboard(source),
        job_id=job_id,
        capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
        budget_cents=100_000,
    )
    if not compilation.compilable or compilation.spec is None:
        raise RuntimeError(
            f"flow guard profile did not compile: {scenario.profile}"
        )
    ProductionService(database, PROJECT_ROOT).persist_compiled_execution(
        compilation.spec
    )
    database.commit()
    return database, job_id


def _transition_rows(
    database: sqlite3.Connection,
    job_id: str,
    *,
    state: str,
    event: str,
    step: int,
    exclude_type: str | None = None,
    only_type: str | None = None,
) -> int:
    rows = database.execute(
        "SELECT node_id,node_type FROM production_nodes "
        "WHERE job_id=? AND state=? ORDER BY node_id",
        (job_id, state),
    ).fetchall()
    applied = 0
    for row in rows:
        node_type = str(row["node_type"])
        if exclude_type and node_type == exclude_type:
            continue
        if only_type and node_type != only_type:
            continue
        result = node_state_machine.transition_node(
            database,
            job_id,
            str(row["node_id"]),
            event,
            event_id=(
                f"{job_id}:{row['node_id']}:flow-guard:{step}:{event}"
            ),
            payload={"source": "flow_guard", "zero_cost": True},
        )
        applied += int(result.applied)
    return applied


def _apply_action(
    database: sqlite3.Connection,
    job_id: str,
    action: str,
    *,
    step: int,
    repairs: list[RepairResult],
) -> int:
    if action == "approve_continuity_anchors":
        applied = sum(
            _transition_rows(
                database,
                job_id,
                state="REVIEW_REQUIRED",
                event="approve",
                step=step,
                only_type=node_type,
            )
            for node_type in ("character_anchor", "scene_anchor")
        )
        database.execute(
            "UPDATE character_identity_anchors SET approval_state='APPROVED' "
            "WHERE job_id=? AND approval_state='PENDING_REVIEW'",
            (job_id,),
        )
        database.execute(
            "UPDATE scene_visual_anchors SET approval_state='APPROVED' "
            "WHERE job_id=? AND approval_state='PENDING_REVIEW'",
            (job_id,),
        )
        database.commit()
        return applied
    if action == "review_candidate":
        return _transition_rows(
            database,
            job_id,
            state="REVIEW_REQUIRED",
            event="approve",
            step=step,
            exclude_type="final_review",
        )
    if action == "review_final_video":
        return _transition_rows(
            database,
            job_id,
            state="REVIEW_REQUIRED",
            event="approve",
            step=step,
            only_type="final_review",
        )
    if action == "increase_job_budget":
        return _transition_rows(
            database,
            job_id,
            state="BUDGET_REQUIRED",
            event="resume",
            step=step,
        )
    if action == "reconcile_provider_task":
        return _transition_rows(
            database,
            job_id,
            state="ACTION_REQUIRED",
            event="resume",
            step=step,
        )
    if action == "preserve_and_diagnose":
        result = apply_repair(
            database, job_id, "retry_repaired_contract_nodes"
        )
        repairs.append(result)
        return int(result.applied)
    return 0


async def explore_scenario(
    scenario: FlowScenario,
    *,
    work_root: Path,
    max_steps: int = 32,
) -> ExplorationReport:
    """Run one isolated scenario until success or a reproducible dead end."""
    work_root = Path(work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    database_path = work_root / "flow-guard.sqlite"
    database, job_id = _prepare_database(database_path, scenario)
    available_types = {
        str(row[0]) for row in database.execute(
            "SELECT DISTINCT node_type FROM production_nodes WHERE job_id=?",
            (job_id,),
        ).fetchall()
    }
    provider = ScriptedSeedance(
        scenario,
        database=database,
        media_root=work_root / "assets",
        available_node_types=available_types,
    )
    states: list[str] = []
    actions: list[str] = []
    started_nodes: list[str] = []
    findings: list[Finding] = []
    repairs: list[RepairResult] = []
    restarted = False
    final_state = ""

    previous_assets_dir = s6_compose.ASSETS_DIR
    previous_database_path = os.environ.get("FACTORY_DB_PATH")
    s6_compose.ASSETS_DIR = (work_root / "assets").resolve()
    os.environ["FACTORY_DB_PATH"] = str(database_path)
    try:
        for step in range(1, max_steps + 1):
            result = await production_graph_runner.run_graph_job(
                database,
                job_id,
                configured_limit=8,
                handlers=provider.handlers(),
            )
            final_state = result.job_state
            states.append(result.job_state)
            started_nodes.extend(result.started_nodes)
            actions.append(result.next_action)
            if result.job_state in TERMINAL_STATES:
                break

            if scenario.restart and not restarted and result.next_action not in {
                "", "view_progress"
            }:
                database.close()
                database = _open_database(database_path)
                provider.rebind_database(database)
                restarted = True

            applied = _apply_action(
                database,
                job_id,
                result.next_action,
                step=step,
                repairs=repairs,
            )
            if not applied and result.next_action != "view_progress":
                failed_rows = database.execute(
                    "SELECT n.node_id,n.error_code,n.output_json,"
                    "COALESCE((SELECT e.payload_json FROM production_node_events e "
                    "WHERE e.job_id=n.job_id AND e.node_id=n.node_id "
                    "AND e.event='fail' ORDER BY e.created_at DESC LIMIT 1),'{}') "
                    "AS failure_json FROM production_nodes n "
                    "WHERE n.job_id=? AND n.state='FAILED' ORDER BY n.node_id",
                    (job_id,),
                ).fetchall()
                failure_detail = "; ".join(
                    f"{row['node_id']}: {row['error_code'] or 'unknown error'}"
                    f" {row['failure_json'] or row['output_json'] or ''}".rstrip()
                    for row in failed_rows
                )
                findings.append(Finding(
                    code="dead_end",
                    severity="error",
                    detail=(
                        f"no flow-guard resolution for {result.next_action}"
                        + (f"; {failure_detail}" if failure_detail else "")
                    ),
                    state=result.job_state,
                    next_action=result.next_action,
                ))
                break
        else:
            findings.append(Finding(
                code="step_limit_exceeded",
                severity="error",
                detail=f"scenario exceeded {max_steps} steps",
                state=final_state,
            ))

        spec_row = database.execute(
            "SELECT production_spec_json,final_path FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        try:
            production_spec = json.loads(
                str(spec_row["production_spec_json"] or "{}")
            ) if spec_row else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            production_spec = {}
        findings.extend(evaluate_production_spec(production_spec))
        media = evaluate_media_artifact(
            str(spec_row["final_path"] or "") if spec_row else ""
        )
        findings.extend(media["findings"])

        paid_calls = int(database.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE job_id=? AND cost_cents>0",
            (job_id,),
        ).fetchone()[0] or 0)
        preliminary = ExplorationReport(
            scenario_id=scenario.scenario_id,
            status="pending",
            final_state=final_state,
            observed_states=tuple(states),
            observed_actions=tuple(actions),
            started_nodes=tuple(dict.fromkeys(started_nodes)),
            findings=tuple(findings),
            repairs=tuple(repairs),
            provider_calls=len(provider.calls),
            paid_provider_calls=paid_calls,
            steps=len(states),
            semantic_checks=SEMANTIC_CHECK_IDS,
            media_metrics=media["metrics"],
        )
        evaluated = evaluate_report(preliminary, max_steps=max_steps)
        return ExplorationReport(
            **{
                **preliminary.as_dict(),
                "status": "passed" if not evaluated else "failed",
                "findings": evaluated,
                "repairs": tuple(repairs),
            }
        )
    finally:
        s6_compose.ASSETS_DIR = previous_assets_dir
        if previous_database_path is None:
            os.environ.pop("FACTORY_DB_PATH", None)
        else:
            os.environ["FACTORY_DB_PATH"] = previous_database_path
        database.close()
