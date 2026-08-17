"""Agent-only, non-billing bootstrap for AI Video Factory.

Ordinary users never run this file. The agent invokes it before a workflow,
then handles installation and configuration gaps through the conversation.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
VIDEO_KEYS = (
    "ARK_API_KEY",
    "VOLC_AK",
    "VOLC_SK",
    "TOS_BUCKET",
)


def _venv_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _configured(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text and not text.startswith("<") and not text.endswith("..."))


def _required_fields(profile: str, config: dict[str, str]) -> tuple[str, ...]:
    if profile == "learning":
        return ()
    if profile == "video":
        asr_ready = _configured(config.get("VOLC_ASR_API_KEY")) or all(
            _configured(config.get(key))
            for key in ("VOLC_ASR_APPID", "VOLC_ASR_TOKEN")
        )
        return VIDEO_KEYS if asr_ready else (*VIDEO_KEYS, "VOLC_ASR_API_KEY")
    provider = str(config.get("DEFAULT_LLM_PROVIDER") or "doubao").lower()
    return ("ANTHROPIC_API_KEY",) if provider == "claude" else ("ARK_API_KEY",)


def _run_doctor(root: Path, python: Path, profile: str) -> dict:
    result = subprocess.run(
        [str(python), str(root / "scripts" / "doctor.py"), "--profile", profile, "--json"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "profile": profile,
            "ready": False,
            "checks": {"doctor": {"ok": False, "detail": result.stderr.strip()}},
            "paid_provider_calls": 0,
        }


def bootstrap(
    root: Path = ROOT,
    *,
    profile: str = "script",
    ensure_install: bool = True,
    python_executable: Path | None = None,
) -> dict:
    """Prepare the runtime and return an agent-readable readiness report."""
    root = root.resolve()
    env_path = root / ".env"
    config = {**_read_env(env_path), **{k: v for k, v in os.environ.items() if v}}
    missing = [key for key in _required_fields(profile, config) if not _configured(config.get(key))]

    if not ensure_install:
        return {
            "profile": profile,
            "status": "configuration_required" if missing else "ready",
            "ready": not missing,
            "missing_fields": missing,
            "paid_provider_calls": 0,
        }

    runtime = _venv_python(root)
    install_needed = not runtime.is_file()
    if install_needed or not env_path.is_file() or not (root / "data").is_dir():
        from install import install

        install(
            root,
            create_venv=install_needed,
            install_dependencies=install_needed,
        )

    runtime = runtime if runtime.is_file() else (python_executable or Path(sys.executable))
    report = _run_doctor(root, runtime, profile)
    package_check = report.get("checks", {}).get("packages", {})
    if package_check and not package_check.get("ok"):
        from install import install

        install(root, create_venv=not _venv_python(root).is_file(), install_dependencies=True)
        runtime = _venv_python(root)
        report = _run_doctor(root, runtime, profile)

    config = {**_read_env(env_path), **{k: v for k, v in os.environ.items() if v}}
    missing = [key for key in _required_fields(profile, config) if not _configured(config.get(key))]
    failed_checks = [
        name for name, item in report.get("checks", {}).items() if not item.get("ok")
    ]
    if missing:
        status = "configuration_required"
    elif report.get("ready"):
        status = "ready"
    else:
        status = "runtime_required"
    return {
        **report,
        "status": status,
        "missing_fields": missing,
        "failed_checks": failed_checks,
        "runtime_python": str(runtime),
        "paid_provider_calls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("script", "video", "learning"), default="script")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = bootstrap(profile=args.profile)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"status={report['status']} ready={str(report['ready']).lower()} paid_provider_calls=0")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
