"""Continuously explore production branches with Seedance 2.5 mocks only."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time

from factory.flow_guard.runner import run_matrix, source_fingerprint
from factory.flow_guard.scenarios import (
    build_incremental_scenarios,
    build_scenarios,
    build_smoke_scenarios,
)
from factory.flow_guard.html_report import render_html_report
from factory.flow_guard.scheduler import (
    RunDecision,
    changed_source_paths,
    choose_run,
    managed_workspace,
    parse_report_full_timestamp,
    resolve_workspace_root,
    source_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]
WATCH_PATHS = (
    ROOT / "scripts" / "factory",
    ROOT / "scripts" / "flow_guard.py",
    ROOT / "scripts" / "management_server.py",
    ROOT / "scripts" / "management_ui",
    ROOT / "tests" / "fixtures" / "production_profiles",
)


def _select_scenarios(max_scenarios: int | None, suite: str = "full"):
    if suite == "incremental":
        scenarios = build_incremental_scenarios()
    elif suite == "smoke":
        scenarios = build_smoke_scenarios()
    else:
        scenarios = build_scenarios()
    return scenarios[:max_scenarios] if max_scenarios else scenarios


def _write_report(path: Path, summary: dict[str, object]) -> None:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    execution = summary.get("execution") or {}
    mode = (
        str(execution.get("mode") or "full").lower()
        if isinstance(execution, dict) else "full"
    )
    mode = mode if mode in {"full", "incremental"} else "full"
    mode_path = path.with_name(f"{path.stem}-{mode}{path.suffix}")
    full_path = path.with_name(f"{path.stem}-full{path.suffix}")
    incremental_path = path.with_name(
        f"{path.stem}-incremental{path.suffix}"
    )
    report_artifacts = {
        "latest_html": path.with_suffix(".html").name,
    }
    if mode == "full" or full_path.exists():
        report_artifacts["full_html"] = full_path.with_suffix(".html").name
    if mode == "incremental" or incremental_path.exists():
        report_artifacts["incremental_html"] = (
            incremental_path.with_suffix(".html").name
        )
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider_mode": "seedance_2_5_mock",
        "report_artifacts": report_artifacts,
        **summary,
    }
    for target in dict.fromkeys((mode_path, path)):
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        target.with_suffix(".html").write_text(
            render_html_report(payload, json_filename=target.name),
            encoding="utf-8",
        )


def _manual_decision(arguments: argparse.Namespace) -> RunDecision:
    suite = str(arguments.suite)
    return RunDecision(
        suite=suite,
        mode="full" if suite == "full" else "incremental",
        reason="手动执行完整回归" if suite == "full" else "手动执行快速回归",
    )


def run_once(
    arguments: argparse.Namespace, *, decision: RunDecision | None = None,
) -> int:
    decision = decision or _manual_decision(arguments)
    scenarios = _select_scenarios(arguments.max_scenarios, decision.suite)
    try:
        workspace_root = resolve_workspace_root(
            explicit=arguments.temp_root,
            minimum_free_bytes=int(arguments.min_free_gb * 1024**3),
        )
    except RuntimeError as error:
        print(f"FLOW_GUARD_WORKSPACE_ERROR {error}")
        return 2
    with managed_workspace(workspace_root) as directory:
        summary = asyncio.run(run_matrix(
            scenarios,
            work_root=directory,
            max_steps=arguments.max_steps,
            include_journeys=decision.suite == "full",
            include_incidents=decision.include_incidents,
            include_shadow_audit=True,
        ))
    summary = {
        **summary,
        "execution": {
            "mode": decision.mode,
            "suite": decision.suite,
            "reason": decision.reason,
            "incident_backed": decision.include_incidents,
            "full_baseline_at": (
                decision.full_baseline_at.isoformat()
                if decision.full_baseline_at else None
            ),
            "workspace_root": str(workspace_root),
            "minimum_free_gb": arguments.min_free_gb,
            "source_fingerprint": source_fingerprint(WATCH_PATHS),
        },
    }
    assurance = summary.get("assurance")
    if isinstance(assurance, dict):
        assurance["full_baseline_at"] = (
            decision.full_baseline_at.isoformat()
            if decision.full_baseline_at else None
        )
    if not arguments.no_write_report:
        _write_report(arguments.report, summary)
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    if summary["status"] in {"passed", "partial", "attention"}:
        verdict = {
            "passed": "FLOW_GUARD_OK",
            "partial": "FLOW_GUARD_PARTIAL",
            "attention": "FLOW_GUARD_ATTENTION",
        }[str(summary["status"])]
        print(
            f"{verdict} provider=seedance_2_5_mock "
            f"scenarios={summary['scenario_count']} "
            f"journeys={summary['journey_count']} "
            f"mutation_score={summary['historical_mutation_score_pct']} "
            "paid_provider_calls=0"
        )
        return 0
    print("FLOW_GUARD_FAILED reproducible blockers were recorded")
    return 1


def _load_report(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def watch(arguments: argparse.Namespace) -> int:
    """Run adaptive checks after changes and periodically force the Full gate."""
    previous = source_snapshot(WATCH_PATHS)
    previous_report = _load_report(arguments.report)
    last_full_at = parse_report_full_timestamp(previous_report)
    previous_execution = previous_report.get("execution") or {}
    reported_fingerprint = (
        previous_execution.get("source_fingerprint")
        if isinstance(previous_execution, dict) else None
    )
    source_changed_while_stopped = (
        reported_fingerprint != source_fingerprint(WATCH_PATHS)
    )
    full_interval = timedelta(hours=arguments.full_interval_hours)
    next_full_attempt_at = datetime.now(timezone.utc)
    first = True
    while True:
        now = datetime.now(timezone.utc)
        current = source_snapshot(WATCH_PATHS)
        changed = changed_source_paths(previous, current, root=ROOT)
        full_due = (
            last_full_at is None or now - last_full_at >= full_interval
        )
        if first or changed or (full_due and now >= next_full_attempt_at):
            decision = (
                RunDecision(
                    "full", "full",
                    "源码指纹与最新报告不一致，停机期间可能发生变更",
                )
                if first and source_changed_while_stopped
                else choose_run(
                    changed,
                    last_full_at=last_full_at,
                    now=now,
                    full_interval=full_interval,
                )
            )
            result = run_once(arguments, decision=decision)
            previous = current
            first = False
            if decision.mode == "full":
                if result == 0:
                    last_full_at = now
                retry_delay = min(full_interval, timedelta(hours=1))
                next_full_attempt_at = now + retry_delay
            if arguments.once:
                return result
        if arguments.once:
            return 0
        time.sleep(max(5.0, arguments.interval))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "watch"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument(
            "--suite", choices=("incremental", "smoke", "full"), default="full"
        )
        subparser.add_argument("--max-scenarios", type=int, default=None)
        subparser.add_argument("--max-steps", type=int, default=32)
        subparser.add_argument(
            "--report",
            type=Path,
            default=ROOT / "data" / "flow_guard" / "latest.json",
        )
        subparser.add_argument("--no-write-report", action="store_true")
        subparser.add_argument("--interval", type=float, default=30.0)
        subparser.add_argument(
            "--temp-root", type=Path,
            default=(
                Path(os.environ["AI_VIDEO_FLOW_GUARD_TEMP_ROOT"])
                if os.environ.get("AI_VIDEO_FLOW_GUARD_TEMP_ROOT") else None
            ),
            help="disposable media workspace root",
        )
        subparser.add_argument(
            "--min-free-gb", type=float, default=4.0,
            help="minimum free space required before a run",
        )
        subparser.add_argument(
            "--full-interval-hours", type=float, default=24.0,
            help="maximum time between complete production gates in watch mode",
        )
        subparser.add_argument("--once", action="store_true")
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    if arguments.max_scenarios is not None and arguments.max_scenarios < 1:
        raise SystemExit("--max-scenarios must be positive")
    if arguments.min_free_gb < 0:
        raise SystemExit("--min-free-gb must not be negative")
    if arguments.full_interval_hours <= 0:
        raise SystemExit("--full-interval-hours must be positive")
    return watch(arguments) if arguments.command == "watch" else run_once(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
