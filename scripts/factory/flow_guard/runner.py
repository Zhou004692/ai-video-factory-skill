"""Matrix execution and evidence aggregation for continuous flow guard runs."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import time
from typing import Awaitable, Callable, Sequence

from .explorer import explore_scenario
from .evidence import CoverageEvidence
from .journeys import run_full_journey_suite
from .management_actions import (
    BACKEND_VIDEO_ACTION_IDS,
    console_dispatch_action_ids,
    normalize_flow_action,
)
from .model import ExplorationReport, FlowScenario
from .mutations import run_historical_mutation_calibration


Explorer = Callable[..., Awaitable[ExplorationReport]]
JourneyRunner = Callable[[Path], dict[str, object]]


def source_fingerprint(paths: Sequence[Path]) -> str:
    """Hash relevant source contents so watch mode runs only after changes."""
    digest = hashlib.sha256()
    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path).resolve()
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(
                item for item in path.rglob("*")
                if item.is_file() and item.suffix in {
                    ".py", ".js", ".json", ".html", ".css"
                }
            )
    for path in sorted(set(files), key=lambda item: item.as_posix()):
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


async def run_matrix(
    scenarios: Sequence[FlowScenario],
    *,
    work_root: Path,
    explorer: Explorer = explore_scenario,
    max_steps: int = 32,
    include_journeys: bool = False,
    journey_runner: JourneyRunner = run_full_journey_suite,
) -> dict[str, object]:
    """Run scenarios in isolation and return a stable machine-readable result."""
    started_at = time.perf_counter()
    unsafe = [
        scenario.scenario_id for scenario in scenarios
        if scenario.provider_mode != "mock" or scenario.max_paid_calls != 0
    ]
    if unsafe:
        raise ValueError(
            "flow guard accepts mock-only, zero-paid-call scenarios: "
            + ", ".join(unsafe)
        )
    work_root = Path(work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    reports: list[ExplorationReport] = []
    for index, scenario in enumerate(scenarios, 1):
        scenario_root = work_root / f"{scenario.scenario_id}-{index:03d}"
        report = await explorer(
            scenario,
            work_root=scenario_root,
            max_steps=max_steps,
        )
        reports.append(report)
    failed = sum(report.status != "passed" for report in reports)
    journey = (
        await asyncio.to_thread(journey_runner, work_root / "journeys")
        if include_journeys
        else {
            "status": "skipped",
            "journey_count": 0,
            "passed": 0,
            "failed": 0,
            "paid_provider_calls": 0,
            "journeys": [],
        }
    )
    graph_paid_calls = sum(report.paid_provider_calls for report in reports)
    paid_calls = graph_paid_calls + int(journey["paid_provider_calls"])
    mutation = run_historical_mutation_calibration()
    traces = {
        (
            report.observed_states,
            report.observed_actions,
            tuple(
                node_id.split(":", 1)[0]
                for node_id in report.started_nodes
            ),
        )
        for report in reports
    }
    semantic_checks = {
        check for report in reports for check in report.semantic_checks
    }
    coverage_evidence = CoverageEvidence()
    graph_actions = {
        normalize_flow_action(action)
        for report in reports
        for action in report.observed_actions
        if normalize_flow_action(action) in BACKEND_VIDEO_ACTION_IDS
    }
    for action in graph_actions:
        coverage_evidence.record("graph_action", action)
    coverage_evidence.merge(journey.get("coverage_evidence") or {})
    evidence_payload = coverage_evidence.as_dict()
    executed_actions = set(graph_actions)
    executed_actions.update(evidence_payload["ui_dispatch_actions"])
    executed_actions.update(evidence_payload["integrated_backend_actions"])
    if not journey.get("coverage_evidence"):
        executed_actions.update(
            action
            for action in journey.get("executed_console_actions", [])
            if action in BACKEND_VIDEO_ACTION_IDS
        )
    missing_actions = BACKEND_VIDEO_ACTION_IDS - executed_actions
    dispatch_actions = console_dispatch_action_ids()
    missing_dispatch_actions = BACKEND_VIDEO_ACTION_IDS - dispatch_actions
    node_types = {
        node_id.split(":", 1)[0]
        for report in reports for node_id in report.started_nodes
    }
    return {
        "status": (
            "passed"
            if (
                not failed
                and not paid_calls
                and mutation["status"] == "passed"
                and journey["status"] in {"passed", "skipped"}
                and not missing_dispatch_actions
            )
            else "failed"
        ),
        "scenario_count": len(reports),
        "passed": len(reports) - failed,
        "failed": failed,
        "provider_calls": sum(report.provider_calls for report in reports),
        "paid_provider_calls": paid_calls,
        "graph_paid_provider_calls": graph_paid_calls,
        "journey_count": journey["journey_count"],
        "journeys_passed": journey["passed"],
        "journeys_failed": journey["failed"],
        "journey_suite": journey,
        "historical_mutation_count": mutation["mutation_count"],
        "historical_mutations_killed": mutation["killed"],
        "historical_mutations_survived": mutation["survived"],
        "historical_mutation_score_pct": mutation["score_pct"],
        "mutation_calibration": mutation,
        "unique_trace_count": len(traces),
        "duplicate_trace_scenarios": len(reports) - len(traces),
        "duration_sec": round(time.perf_counter() - started_at, 3),
        "coverage": {
            **evidence_payload,
            "graph_action_count": len(evidence_payload["graph_actions"]),
            "ui_dispatch_action_count": len(
                evidence_payload["ui_dispatch_actions"]
            ),
            "integrated_backend_action_count": len(
                evidence_payload["integrated_backend_actions"]
            ),
            "worker_node_type_count": len(
                evidence_payload["worker_node_types"]
            ),
            "source_type_count": len(evidence_payload["source_types"]),
            "provider_response_type_count": len(
                evidence_payload["provider_response_types"]
            ),
            "semantic_check_count": len(semantic_checks),
            "semantic_checks": sorted(semantic_checks),
            "executed_node_type_count": len(node_types),
            "executed_node_types": sorted(node_types),
            "console_action_count": len(BACKEND_VIDEO_ACTION_IDS),
            "dispatch_contract_action_count": len(dispatch_actions),
            "missing_dispatch_actions": sorted(missing_dispatch_actions),
            "dispatch_contract_coverage_pct": round(
                len(BACKEND_VIDEO_ACTION_IDS & dispatch_actions)
                / len(BACKEND_VIDEO_ACTION_IDS) * 100,
                2,
            ),
            "executed_console_action_count": len(executed_actions),
            "executed_console_actions": sorted(executed_actions),
            "missing_console_actions": sorted(missing_actions),
            "console_action_coverage_pct": round(
                len(executed_actions) / len(BACKEND_VIDEO_ACTION_IDS) * 100,
                2,
            ),
        },
        "reports": [
            {
                "scenario": scenario.as_dict(),
                **report.as_dict(),
            }
            for scenario, report in zip(scenarios, reports)
        ],
    }
