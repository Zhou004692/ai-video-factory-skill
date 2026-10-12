"""Zero-cost full management workflow journeys for Flow Guard."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Callable, Iterator, Mapping, Sequence

from .. import db as factory_db, full_flow_regression
from ..management_api import ManagementService
from .evidence import CoverageEvidence, parse_browser_evidence
from .invariants import evaluate_media_artifact, evaluate_production_spec


PROJECT_ROOT = Path(__file__).resolve().parents[3]

FULL_JOURNEY_SCENARIOS = (
    {
        "scenario_id": "manual_three_person_multiscene",
        "source": {
            "type": "manual",
            "title": "白天高空车跨场景抢修",
            "content": (
                "林夏在调度中心收到故障告警，高远在设备场复检备用高空车，"
                "周工在白天施工现场隔离故障。三人分别发言并完成复工闭环。"
            ),
        },
        "expect": {"minimum_characters": 3, "minimum_scenes": 3},
    },
    {
        "scenario_id": "manual_dialogue_handoff",
        "source": {
            "type": "manual",
            "title": "高空车交付前复检",
            "content": (
                "项目负责人询问设备能否按时交付，维修工程师确认支腿、"
                "液压和安全装置已经复检，调度负责人说明运输车辆已到位。"
            ),
        },
        "expect": {"minimum_characters": 3, "minimum_scenes": 3},
    },
    {
        "scenario_id": "manual_background_voiceover",
        "source": {
            "type": "manual",
            "title": "高空车安全作业说明",
            "content": (
                "画外音介绍白天施工现场的安全边界，三名现场人员依次完成"
                "设备检查、作业确认和升降测试，背景解说不绑定入镜人物。"
            ),
        },
        "expect": {
            "minimum_characters": 3,
            "minimum_scenes": 3,
            "background_voiceover": True,
        },
    },
)


@contextmanager
def _isolated_environment(values: Mapping[str, str]) -> Iterator[None]:
    missing = object()
    previous = {key: os.environ.get(key, missing) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is missing:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)


def _run_journey(
    work_root: Path, scenario: Mapping[str, object]
) -> dict[str, object]:
    scenario_id = str(scenario.get("scenario_id") or "journey")
    root = (Path(work_root) / scenario_id).resolve()
    data_root = root / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    templates = root / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "env.example").write_text(
        "FACTORY_PROVIDER_MODE=live\nPRODUCTION_EXECUTION_ENGINE=legacy\n",
        encoding="utf-8",
    )
    (root / ".env").write_text(
        "FACTORY_PROVIDER_MODE=replay\n"
        "PRODUCTION_EXECUTION_ENGINE=graph_v2\n"
        "DIALOGUE_AUDIO_MODE=seedance_native\n"
        "APPEND_CTA=false\n"
        "GEN_THUMBNAIL=false\n"
        "GEN_MARKETING_THUMBNAIL=false\n",
        encoding="utf-8",
    )
    database_path = data_root / "factory.db"
    database = sqlite3.connect(str(database_path))
    database.row_factory = sqlite3.Row
    database.executescript(factory_db.DDL)
    service = ManagementService(database, root, worker_mode="external")
    environment = {
        "FACTORY_DB_PATH": str(database_path),
        "FACTORY_PROVIDER_MODE": "replay",
        "PRODUCTION_EXECUTION_ENGINE": "graph_v2",
        "APPEND_CTA": "false",
        "GEN_THUMBNAIL": "false",
        "GEN_MARKETING_THUMBNAIL": "false",
    }
    try:
        with _isolated_environment(environment):
            report = full_flow_regression.run_source_to_ready_to_publish(
                service,
                dict(scenario),
                fixture_root=data_root / "replay",
                media_root=data_root / "assets",
            )
        row = database.execute(
            "SELECT production_spec_json FROM jobs WHERE job_id=?",
            (report["job_id"],),
        ).fetchone()
        spec = json.loads(str(row["production_spec_json"] or "{}"))
        semantic_codes = [
            item.code for item in evaluate_production_spec(spec)
        ]
        runtime = spec.get("runtime_storyboard") or {}
        runtime_masters = runtime.get("masters") or []
        character_count = len(spec.get("characters") or [])
        scene_count = len({
            str(master.get("scene_anchor_key") or "")
            for master in runtime_masters
            if str(master.get("scene_anchor_key") or "")
        })
        background_voiceover_count = sum(
            bool(str(master.get("voiceover") or "").strip())
            and not (master.get("cast") or [])
            and not str(master.get("speaker") or "").strip()
            and not (master.get("speakers") or [])
            for master in runtime_masters
        )
        expectations = scenario.get("expect") or {}
        structural_codes = []
        if character_count < int(expectations.get("minimum_characters") or 0):
            structural_codes.append("journey_character_coverage_missing")
        if scene_count < int(expectations.get("minimum_scenes") or 0):
            structural_codes.append("journey_scene_coverage_missing")
        if (
            expectations.get("background_voiceover")
            and background_voiceover_count < 1
        ):
            structural_codes.append("background_voiceover_forced_onscreen")
        paid_calls = int(database.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE cost_cents>0"
        ).fetchone()[0] or 0)
        final_path = Path(str(report.get("final_path") or ""))
        media = evaluate_media_artifact(final_path, perceptual=True)
        media_codes = [item.code for item in media["findings"]]
        required_stages = {
            "source", "brief", "scripts", "approved", "compiled",
            "anchors", "anchor_review", "masters", "local_compose",
            "final_review", "ready_to_publish",
        }
        completed = set(report.get("completed_stages") or [])
        passed = (
            report.get("job_state") == "READY_TO_PUBLISH"
            and int(report.get("external_cost_cents") or 0) == 0
            and paid_calls == 0
            and bool(report.get("all_nodes_succeeded"))
            and final_path.is_file()
            and not semantic_codes
            and not structural_codes
            and not media_codes
            and required_stages <= completed
        )
        return {
            "scenario_id": scenario_id,
            "status": "passed" if passed else "failed",
            "final_state": str(report.get("job_state") or ""),
            "completed_stages": list(report.get("completed_stages") or []),
            "paid_provider_calls": paid_calls,
            "final_media_exists": final_path.is_file(),
            "all_nodes_succeeded": bool(report.get("all_nodes_succeeded")),
            "semantic_findings": semantic_codes,
            "structural_findings": structural_codes,
            "character_count": character_count,
            "scene_count": scene_count,
            "background_voiceover_master_count": background_voiceover_count,
            "media_findings": media_codes,
            "media_metrics": media["metrics"],
        }
    except Exception as exc:  # recorded as reproducible journey evidence
        return {
            "scenario_id": scenario_id,
            "status": "failed",
            "final_state": "ERROR",
            "paid_provider_calls": int(database.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE cost_cents>0"
            ).fetchone()[0] or 0),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    finally:
        database.close()


def run_management_browser_suite(
    *, command_runner: Callable[..., object] = subprocess.run,
) -> dict[str, object]:
    """Run the real zero-cost management UI journey and failure decisions."""
    specifications = (
        (
            "management_full_flow",
            PROJECT_ROOT / "tests" / "management_full_flow_browser_check.py",
            1,
            (),
        ),
        (
            "management_fault_decisions",
            PROJECT_ROOT / "tests" / "management_fault_browser_check.py",
            26,
            ("--release-gate",),
        ),
        (
            "management_action_backend",
            PROJECT_ROOT / "tests" / "management_action_backend_check.py",
            7,
            (),
        ),
        (
            "creative_delivery_browser_restart",
            PROJECT_ROOT / "tests" / "creative_delivery_browser_check.py",
            1,
            (),
        ),
    )
    evidence = []
    coverage = CoverageEvidence()
    passed = 0
    failed = 0
    for (
        scenario_id, script, scenario_count, arguments
    ) in specifications:
        try:
            process = command_runner(
                [sys.executable, str(script), *arguments],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=900,
            )
            exit_code = int(getattr(process, "returncode", 1))
            stdout = str(getattr(process, "stdout", "") or "")
            stderr = str(getattr(process, "stderr", "") or "")
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            stdout = str(exc.stdout or "")
            stderr = f"browser suite timed out after {exc.timeout} seconds"
        suite_evidence, evidence_errors = parse_browser_evidence(stdout)
        status = (
            "passed"
            if exit_code == 0 and not evidence_errors
            else "failed"
        )
        if status == "passed":
            passed += scenario_count
            coverage.merge(suite_evidence)
        else:
            failed += scenario_count
        evidence.append({
            "scenario_id": scenario_id,
            "status": status,
            "scenario_count": scenario_count,
            "exit_code": exit_code,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": stderr[-2000:],
            "evidence_errors": evidence_errors,
            "coverage_evidence": suite_evidence,
        })
    coverage_payload = coverage.as_dict()
    executed_actions = sorted(set(
        coverage_payload["ui_dispatch_actions"]
        + coverage_payload["integrated_backend_actions"]
    ))
    return {
        "status": "passed" if not failed else "failed",
        "scenario_count": passed + failed,
        "passed": passed,
        "failed": failed,
        "paid_provider_calls": 0,
        "coverage_evidence": coverage_payload,
        "executed_console_actions": executed_actions,
        "journeys": evidence,
    }


def run_full_journey_suite(
    work_root: Path,
    *,
    scenarios: Sequence[Mapping[str, object]] = FULL_JOURNEY_SCENARIOS,
    include_browser: bool = True,
    browser_runner: Callable[[], dict[str, object]] | None = None,
) -> dict[str, object]:
    """Execute source-to-publishable journeys through the real service API."""
    journeys = [
        _run_journey(Path(work_root), scenario) for scenario in scenarios
    ]
    service_failed = sum(item["status"] != "passed" for item in journeys)
    service_paid_calls = sum(
        int(item.get("paid_provider_calls") or 0) for item in journeys
    )
    browser_suite = None
    if include_browser:
        browser_suite = (browser_runner or run_management_browser_suite)()
    browser_count = int((browser_suite or {}).get("scenario_count") or 0)
    browser_passed = int((browser_suite or {}).get("passed") or 0)
    browser_failed = int((browser_suite or {}).get("failed") or 0)
    browser_paid_calls = int(
        (browser_suite or {}).get("paid_provider_calls") or 0
    )
    failed = service_failed + browser_failed
    paid_calls = service_paid_calls + browser_paid_calls
    journey_count = len(journeys) + browser_count
    coverage = CoverageEvidence()
    for scenario in scenarios:
        source = scenario.get("source") or {}
        coverage.record("source_type", source.get("type") or "unknown")
        coverage.record("provider_response", "replay_success")
    coverage.merge((browser_suite or {}).get("coverage_evidence") or {})
    coverage_payload = coverage.as_dict()
    executed_actions = sorted(set(
        (browser_suite or {}).get("executed_console_actions") or []
    ) | set(coverage_payload["ui_dispatch_actions"])
      | set(coverage_payload["integrated_backend_actions"]))
    return {
        "status": "passed" if not failed and not paid_calls else "failed",
        "journey_count": journey_count,
        "passed": len(journeys) - service_failed + browser_passed,
        "failed": failed,
        "paid_provider_calls": paid_calls,
        "journeys": journeys,
        "browser_suite": browser_suite,
        "coverage_evidence": coverage_payload,
        "executed_console_actions": executed_actions,
    }
