"""Run the deterministic, zero-cost release regression gate.

The gate deliberately removes provider credentials, forces replay mode and
installs a process-wide socket guard in every child process.  A fixture miss
therefore fails closed instead of silently falling back to a paid provider.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
FULL_FLOW_FIXTURES = ROOT / "tests" / "fixtures" / "full_flow"
INCIDENT_FIXTURES = ROOT / "tests" / "fixtures" / "incidents"

LEVEL_COMMANDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "r0": ((
        "-m", "unittest",
        "tests.test_zero_cost_release_gate",
        "tests.test_provider_runtime",
        "tests.test_provider_replay",
        "tests.test_full_flow_fixtures",
        "tests.test_runtime_profile",
    ),),
    "r1": ((
        "-m", "unittest",
        "tests.test_production_compiler",
        "tests.test_frozen_scene_dependencies",
        "tests.test_production_graph",
        "tests.test_production_graph_runner",
        "tests.test_incident_replays",
    ),),
    "r2": (
        ("-m", "unittest", "tests.test_full_flow_regression"),
        ("tests/management_full_flow_browser_check.py",),
    ),
    "r3": (("-m", "unittest", "tests.test_full_flow_recovery"),),
    "r4": (
        (
            "-m", "unittest",
            "tests.test_flow_guard_scenarios",
            "tests.test_flow_guard_diagnostics",
            "tests.test_flow_guard_management_actions",
            "tests.test_flow_guard_runner",
        ),
        (
            "scripts/flow_guard.py", "run", "--suite", "smoke",
            "--max-scenarios", "6",
            "--no-write-report",
        ),
    ),
}

_SECRET_MARKERS = (
    "API_KEY", "ACCESS_KEY", "SECRET", "TOKEN", "PASSWORD",
    "AUTHORIZATION",
)


def tree_signature(path: Path) -> str:
    """Return a stable content signature for a file tree or missing path."""
    resolved = Path(path)
    if not resolved.exists():
        return "missing"
    digest = hashlib.sha256()
    if resolved.is_file():
        digest.update(b"file\0")
        digest.update(resolved.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(resolved.read_bytes())
        return f"sha256:{digest.hexdigest()}"
    digest.update(b"tree\0")
    for child in sorted(
        (item for item in resolved.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(resolved).as_posix(),
    ):
        relative = child.relative_to(resolved).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(child.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        with child.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def build_summary(
    *,
    status: str,
    paid_provider_calls: int,
    external_requests: int,
    scenarios: int,
    invariant_issues: int,
) -> dict[str, object]:
    return {
        "status": status,
        "paid_provider_calls": int(paid_provider_calls),
        "external_requests": int(external_requests),
        "scenarios": int(scenarios),
        "invariant_issues": int(invariant_issues),
    }


def _scenario_count() -> int:
    full_flow = sum(
        1 for path in FULL_FLOW_FIXTURES.glob("*/manifest.json")
        if path.is_file()
    )
    incidents = sum(1 for path in INCIDENT_FIXTURES.glob("*.json") if path.is_file())
    return full_flow + incidents


def _guard_source() -> str:
    return r'''"""Injected by zero_cost_release_gate; blocks non-local sockets."""
import json
import os
import socket

_original_connect = socket.socket.connect
_telemetry = os.environ.get("FACTORY_ZERO_COST_SOCKET_TELEMETRY", "")
_local_hosts = {"127.0.0.1", "::1", "localhost"}

def _record(address):
    if not _telemetry:
        return
    with open(_telemetry, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"address": repr(address)}) + "\n")

def _guarded_connect(self, address):
    if isinstance(address, tuple) and address:
        host = str(address[0]).strip("[]").casefold()
        if host not in _local_hosts:
            _record(address)
            raise RuntimeError(
                "zero-cost release gate blocked external socket before connect: "
                + repr(address)
            )
    return _original_connect(self, address)

socket.socket.connect = _guarded_connect
'''


def _safe_environment(guard_root: Path, telemetry_path: Path) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in _SECRET_MARKERS)
    }
    existing_pythonpath = environment.get("PYTHONPATH", "")
    pythonpath = [str(guard_root), str(ROOT), str(ROOT / "scripts")]
    if existing_pythonpath:
        pythonpath.append(existing_pythonpath)
    environment.update({
        "PYTHONPATH": os.pathsep.join(pythonpath),
        "FACTORY_PROVIDER_MODE": "replay",
        "FACTORY_REPLAY_DISABLE_WORKERS": "1",
        "FACTORY_ZERO_COST_SOCKET_TELEMETRY": str(telemetry_path),
        "PYTHONUTF8": "1",
    })
    return environment


def _selected_levels(level: str) -> Iterable[str]:
    if level == "all":
        return LEVEL_COMMANDS.keys()
    requested = int(level[1:])
    return tuple(name for name in LEVEL_COMMANDS if int(name[1:]) <= requested)


def _run_command(arguments: tuple[str, ...], environment: dict[str, str]) -> float:
    command = (sys.executable, *arguments)
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    elapsed = time.monotonic() - started
    if completed.stdout:
        print(completed.stdout.rstrip(), flush=True)
    if completed.returncode:
        raise RuntimeError(
            f"zero-cost command failed ({completed.returncode}): "
            + " ".join(command)
        )
    return elapsed


def run_gate(level: str = "all") -> dict[str, object]:
    production_root = Path(
        os.environ.get("FACTORY_PRODUCTION_ROOT", str(ROOT))
    ).expanduser().resolve()
    protected_paths = (
        production_root / "data" / "factory.db",
        production_root / "data" / "assets",
    )
    before = {str(path): tree_signature(path) for path in protected_paths}
    scenarios = _scenario_count()
    if scenarios != 10:
        raise RuntimeError(
            f"zero-cost scenario matrix must contain exactly 10 cases, got {scenarios}"
        )

    with tempfile.TemporaryDirectory(prefix="zero-cost-release-gate-") as directory:
        guard_root = Path(directory)
        telemetry_path = guard_root / "external-requests.jsonl"
        (guard_root / "sitecustomize.py").write_text(
            _guard_source(), encoding="utf-8"
        )
        environment = _safe_environment(guard_root, telemetry_path)
        for selected in _selected_levels(level):
            print(f"ZERO_COST_LEVEL_START {selected.upper()}", flush=True)
            elapsed = 0.0
            for command in LEVEL_COMMANDS[selected]:
                elapsed += _run_command(command, environment)
            print(
                f"ZERO_COST_LEVEL_OK {selected.upper()} elapsed_sec={elapsed:.3f}",
                flush=True,
            )
        external_requests = (
            len(telemetry_path.read_text(encoding="utf-8").splitlines())
            if telemetry_path.exists() else 0
        )

    after = {str(path): tree_signature(path) for path in protected_paths}
    invariant_issues = sum(before[path] != after[path] for path in before)
    summary = build_summary(
        status=(
            "passed" if external_requests == 0 and invariant_issues == 0
            else "failed"
        ),
        paid_provider_calls=0,
        external_requests=external_requests,
        scenarios=scenarios,
        invariant_issues=invariant_issues,
    )
    if summary["status"] != "passed":
        raise RuntimeError(json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--level", choices=("r0", "r1", "r2", "r3", "r4", "all"), default="all",
        help="Run through this regression level (default: all/R4).",
    )
    arguments = parser.parse_args()
    try:
        summary = run_gate(arguments.level)
    except Exception as error:  # release gate must always emit a final verdict
        failure = build_summary(
            status="failed",
            paid_provider_calls=0,
            external_requests=0,
            scenarios=_scenario_count(),
            invariant_issues=1,
        )
        print(json.dumps(failure, ensure_ascii=False, separators=(",", ":")))
        print(f"ZERO_COST_RELEASE_GATE_FAILED {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    print("ZERO_COST_RELEASE_GATE_OK paid_provider_calls=0 external_requests=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
