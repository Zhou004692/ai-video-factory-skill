"""Matrix execution and evidence aggregation for continuous flow guard runs."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from .explorer import explore_scenario
from .model import ExplorationReport, FlowScenario


Explorer = Callable[..., Awaitable[ExplorationReport]]


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
) -> dict[str, object]:
    """Run scenarios in isolation and return a stable machine-readable result."""
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
    paid_calls = sum(report.paid_provider_calls for report in reports)
    return {
        "status": "passed" if not failed and not paid_calls else "failed",
        "scenario_count": len(reports),
        "passed": len(reports) - failed,
        "failed": failed,
        "provider_calls": sum(report.provider_calls for report in reports),
        "paid_provider_calls": paid_calls,
        "reports": [
            {
                "scenario": scenario.as_dict(),
                **report.as_dict(),
            }
            for scenario, report in zip(scenarios, reports)
        ],
    }
