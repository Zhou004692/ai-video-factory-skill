"""Zero-cost, state-isolated regression gate for Skill replacement upgrades."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.zero_cost_release_gate import _SECRET_MARKERS, _guard_source


RUNTIME_OVERRIDES = (
    "FACTORY_RUNTIME_HOME",
    "FACTORY_DATA_DIR",
    "FACTORY_ENV_FILE",
    "FACTORY_DB_PATH",
)

TEST_MODULES = (
    "tests.test_runtime_backup",
    "tests.test_runtime_profile",
    "tests.test_fresh_install",
    "tests.test_skill_distribution",
)


def _isolated_environment(
    guard_root: Path,
    runtime_home: Path,
    telemetry_path: Path,
) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in _SECRET_MARKERS)
        and key not in RUNTIME_OVERRIDES
    }
    existing_pythonpath = environment.get("PYTHONPATH", "")
    pythonpath = [str(guard_root), str(ROOT), str(ROOT / "scripts")]
    if existing_pythonpath:
        pythonpath.append(existing_pythonpath)
    environment.update({
        "PYTHONPATH": os.pathsep.join(pythonpath),
        "PYTHONUTF8": "1",
        "FACTORY_RUNTIME_HOME": str(runtime_home),
        "FACTORY_PROVIDER_MODE": "replay",
        "FACTORY_REPLAY_DISABLE_WORKERS": "1",
        "FACTORY_ZERO_COST_SOCKET_TELEMETRY": str(telemetry_path),
    })
    return environment


def main() -> int:
    with tempfile.TemporaryDirectory(
        prefix="ai-video-factory-update-gate-"
    ) as directory:
        guard_root = Path(directory).resolve()
        runtime_home = guard_root / "runtime-home"
        telemetry_path = guard_root / "external-requests.jsonl"
        (guard_root / "sitecustomize.py").write_text(
            _guard_source(), encoding="utf-8"
        )
        environment = _isolated_environment(
            guard_root,
            runtime_home,
            telemetry_path,
        )
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", *TEST_MODULES],
            cwd=ROOT,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if completed.stdout:
            print(completed.stdout.rstrip(), flush=True)
        external_requests = 0
        if telemetry_path.is_file():
            external_requests = sum(
                1 for line in telemetry_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines() if line.strip()
            )
        if completed.returncode or external_requests:
            if external_requests:
                print(
                    f"Skill update gate blocked {external_requests} external request(s)",
                    file=sys.stderr,
                )
            return 1
        resolved_home = Path(environment["FACTORY_RUNTIME_HOME"]).resolve()
        if resolved_home != runtime_home or not resolved_home.is_relative_to(guard_root):
            print("Skill update gate runtime isolation failed", file=sys.stderr)
            return 1
    print(
        "SKILL_UPDATE_RELEASE_GATE_OK "
        "paid_provider_calls=0 external_requests=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
