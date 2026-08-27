"""Windows desktop deployment release gate with no paid provider calls."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
UNIT_TARGETS = (
    "tests.test_production_profile_baseline",
    "tests.test_production_scenario_matrix",
    "tests.test_runtime_profile",
    "tests.test_cross_platform_media_runtime",
    "tests.test_fresh_install",
    "tests.test_skill_distribution",
    "tests.test_management_workflows",
)


def _run(command: list[str], *, cwd: Path = ROOT, timeout: int = 240) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=os.environ.copy(),
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if completed.returncode:
        raise SystemExit(completed.returncode)


def _clean_package_acceptance() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from distribute_skill import audit_distribution, build_archive, build_distribution
    from install import install

    with tempfile.TemporaryDirectory(prefix="ai-video-windows-release-") as tmp:
        output = Path(tmp)
        package = build_distribution(ROOT, output)
        report = audit_distribution(package)
        if not report["ok"]:
            raise AssertionError(json.dumps(report, ensure_ascii=False))
        installed = install(
            package, create_venv=False, install_dependencies=False
        )
        if installed["paid_provider_calls"] != 0:
            raise AssertionError("installer must never call a paid provider")
        if not (Path(installed["data_dir"]) / "factory.db").is_file():
            raise AssertionError("fresh install did not initialize SQLite")
        archive = build_archive(ROOT, output / "archive")
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        if archive.name != f"ai-video-factory-{version}.zip":
            raise AssertionError("distribution archive is not versioned")

        doctor = subprocess.run(
            [
                sys.executable,
                str(package / "scripts" / "doctor.py"),
                "--profile",
                "learning",
                "--json",
            ],
            cwd=package,
            env={**os.environ, "PYTHONPATH": str(package / "scripts")},
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if doctor.returncode:
            raise AssertionError(
                "fresh-package doctor failed:\n"
                f"stdout={doctor.stdout}\nstderr={doctor.stderr}"
            )
        diagnosis = json.loads(doctor.stdout)
        if not diagnosis["ready"] or diagnosis["paid_provider_calls"] != 0:
            raise AssertionError(json.dumps(diagnosis, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-browser", action="store_true", help="run only deterministic unit/package gates"
    )
    args = parser.parse_args()

    _run([sys.executable, "-m", "unittest", *UNIT_TARGETS])
    _clean_package_acceptance()
    if not args.skip_browser:
        _run([sys.executable, "tests/management_workflow_browser_check.py"])
        _run(
            [sys.executable, "tests/management_fault_browser_check.py", "--release-gate"],
            timeout=360,
        )
    print("WINDOWS_DESKTOP_RELEASE_GATE_OK paid_provider_calls=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
