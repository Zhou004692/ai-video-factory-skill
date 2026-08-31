"""Continuously explore production branches with Seedance 2.5 mocks only."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time

from factory.flow_guard.runner import run_matrix, source_fingerprint
from factory.flow_guard.scenarios import build_scenarios, build_smoke_scenarios


ROOT = Path(__file__).resolve().parents[1]
WATCH_PATHS = (
    ROOT / "scripts" / "factory",
    ROOT / "scripts" / "management_ui",
    ROOT / "tests" / "fixtures" / "production_profiles",
)


def _select_scenarios(max_scenarios: int | None, suite: str = "full"):
    scenarios = (
        build_smoke_scenarios() if suite == "smoke" else build_scenarios()
    )
    return scenarios[:max_scenarios] if max_scenarios else scenarios


def _write_report(path: Path, summary: dict[str, object]) -> None:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider_mode": "seedance_2_5_mock",
        **summary,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_once(arguments: argparse.Namespace) -> int:
    scenarios = _select_scenarios(arguments.max_scenarios, arguments.suite)
    with tempfile.TemporaryDirectory(prefix="ai-video-flow-guard-") as directory:
        summary = asyncio.run(run_matrix(
            scenarios,
            work_root=Path(directory),
            max_steps=arguments.max_steps,
            include_journeys=arguments.suite == "full",
        ))
    if not arguments.no_write_report:
        _write_report(arguments.report, summary)
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    if summary["status"] == "passed":
        print(
            "FLOW_GUARD_OK provider=seedance_2_5_mock "
            f"scenarios={summary['scenario_count']} "
            f"journeys={summary['journey_count']} "
            f"mutation_score={summary['historical_mutation_score_pct']} "
            "paid_provider_calls=0"
        )
        return 0
    print("FLOW_GUARD_FAILED reproducible blockers were recorded")
    return 1


def watch(arguments: argparse.Namespace) -> int:
    """Run once, then rerun whenever relevant production code changes."""
    previous = ""
    while True:
        current = source_fingerprint(WATCH_PATHS)
        if current != previous:
            result = run_once(arguments)
            previous = current
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
            "--suite", choices=("smoke", "full"), default="full"
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
        subparser.add_argument("--once", action="store_true")
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    if arguments.max_scenarios is not None and arguments.max_scenarios < 1:
        raise SystemExit("--max-scenarios must be positive")
    return watch(arguments) if arguments.command == "watch" else run_once(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
