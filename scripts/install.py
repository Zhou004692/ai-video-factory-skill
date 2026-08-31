"""Cross-platform installer for the AI Video Factory skill runtime."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

from factory.runtime_backup import create_upgrade_snapshot, restore_upgrade_snapshot
from factory.runtime_profile import resolve_runtime_profile


ROOT = Path(__file__).resolve().parents[1]


WINDOWS_LAUNCHER = """@echo off
start "" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0scripts\\open_management.ps1"
exit /b 0
"""

PORTABLE_LAUNCHER = """#!/usr/bin/env sh
set -eu
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYTHON="$ROOT_DIR/.venv/bin/python"
else
  PYTHON="$(command -v python3 || command -v python || true)"
  if [ -z "$PYTHON" ]; then
    echo "AI Video Factory requires Python 3.11 or newer." >&2
    exit 1
  fi
  "$PYTHON" "$ROOT_DIR/scripts/install.py" --root "$ROOT_DIR" >/dev/null
  PYTHON="$ROOT_DIR/.venv/bin/python"
fi
"$PYTHON" "$ROOT_DIR/scripts/install.py" --root "$ROOT_DIR" --no-venv --no-deps >/dev/null
START_PAGE=overview
if [ ! -f "$ROOT_DIR/.env" ] || grep -q '=<required>' "$ROOT_DIR/.env"; then
  START_PAGE=config
fi
exec "$PYTHON" "$ROOT_DIR/scripts/management_server.py" --start-page "$START_PAGE"
"""


def _venv_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _read_environment(env_file: Path) -> dict[str, str]:
    values = dict(os.environ)
    if not env_file.is_file():
        return values
    for raw_line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in raw_line or raw_line.lstrip().startswith("#"):
            continue
        key, value = raw_line.split("=", 1)
        values.setdefault(key.strip(), value.strip().strip("'\""))
    return values


def _initialize_database(root: Path) -> bool:
    db_module = root / "scripts" / "factory" / "db.py"
    if not db_module.is_file():
        return False
    env = _read_environment(root / ".env")
    profile = resolve_runtime_profile(root=root, environ=env)
    database = profile.data_dir / "factory.db"
    env["FACTORY_DB_PATH"] = str(database)
    env["FACTORY_DATA_DIR"] = str(profile.data_dir)
    command = (
        "from factory.db import conn; "
        "database = conn(); database.close()"
    )
    subprocess.run(
        [sys.executable, "-c", command],
        cwd=root,
        env={**env, "PYTHONPATH": str(root / "scripts")},
        check=True,
        capture_output=True,
        text=True,
    )
    return database.is_file()


def _write_launchers(root: Path) -> list[str]:
    launchers = {
        root / "打开 AI 视频工厂.cmd": WINDOWS_LAUNCHER,
        root / "open-ai-video-factory.sh": PORTABLE_LAUNCHER,
    }
    for path, content in launchers.items():
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        if existing != content:
            path.write_text(content, encoding="utf-8", newline="\n")
        if path.suffix == ".sh":
            path.chmod(path.stat().st_mode | 0o111)
    return [str(path) for path in launchers]


def install_windows_desktop_launcher(
    root: Path = ROOT, *, desktop_directory: Path | None = None
) -> str | None:
    """Install the idempotent Windows desktop entry and return its path."""
    if sys.platform != "win32":
        return None
    script = root.resolve() / "scripts" / "install_desktop_launcher.ps1"
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-SkillRoot",
        str(root.resolve()),
    ]
    if desktop_directory is not None:
        command.extend(("-DesktopDirectory", str(desktop_directory.resolve())))
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _configuration_required(env_file: Path) -> bool:
    if not env_file.is_file():
        return True
    values: dict[str, str] = {}
    for raw_line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in raw_line or raw_line.lstrip().startswith("#"):
            continue
        key, value = raw_line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    provider = values.get("DEFAULT_LLM_PROVIDER", "doubao").lower()
    required = "ANTHROPIC_API_KEY" if provider == "claude" else "ARK_API_KEY"
    value = values.get(required, "")
    return not value or value.startswith("<") or value.endswith("...")


def _installed_version(metadata_file: Path) -> str | None:
    if not metadata_file.is_file():
        return None
    try:
        payload = json.loads(metadata_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = str(payload.get("installed_version") or "").strip()
    return value or None


def _write_runtime_metadata(metadata_file: Path, version: str) -> None:
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = metadata_file.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"installed_version": version}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(metadata_file)


def install(root: Path = ROOT, *, create_venv: bool = True,
            install_dependencies: bool = True,
            create_desktop_launcher: bool | None = None) -> dict:
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required")
    root = root.resolve()
    env_file = root / ".env"
    initial_values = _read_environment(env_file)
    profile = resolve_runtime_profile(root=root, environ=initial_values)
    data_dir = profile.data_dir
    version_file = root / "VERSION"
    current_version = (
        version_file.read_text(encoding="utf-8").strip()
        if version_file.is_file()
        else "development"
    )
    runtime_metadata = data_dir / "runtime.json"
    has_existing_state = any((
        env_file.exists(),
        (data_dir / "factory.db").exists(),
        (data_dir / "character_assets").exists(),
        (data_dir / "assets").exists(),
    ))
    upgrade_needed = (
        has_existing_state
        and _installed_version(runtime_metadata) != current_version
    )
    upgrade_backup = (
        create_upgrade_snapshot(root=root, data_dir=data_dir, env_file=env_file)
        if upgrade_needed
        else None
    )
    env_created = False
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        if not env_file.exists():
            shutil.copy2(root / "templates" / "env.example", env_file)
            env_created = True

        venv_created = False
        if create_venv and not _venv_python(root).exists():
            venv.EnvBuilder(with_pip=True).create(root / ".venv")
            venv_created = True

        if install_dependencies:
            python = _venv_python(root)
            if not python.exists():
                raise RuntimeError("virtual environment is missing; remove --no-venv")
            subprocess.run(
                [str(python), "-m", "pip", "install", "-r", str(root / "scripts" / "requirements.txt")],
                check=True,
            )

        database_initialized = _initialize_database(root)
        launchers = _write_launchers(root)
        if create_desktop_launcher is None:
            create_desktop_launcher = (
                sys.platform == "win32" and root == ROOT.resolve()
            )
        desktop_launcher = (
            install_windows_desktop_launcher(root)
            if create_desktop_launcher
            else None
        )
        configuration_required = _configuration_required(env_file)
        _write_runtime_metadata(runtime_metadata, current_version)
    except BaseException:
        if upgrade_backup is not None:
            restore_upgrade_snapshot(upgrade_backup)
        elif env_created:
            env_file.unlink(missing_ok=True)
        raise

    return {
        "root": str(root),
        "version": current_version,
        "env_created": env_created,
        "venv_created": venv_created,
        "data_dir": str(data_dir),
        "runtime": profile.as_public_dict(),
        "upgrade_backup": str(upgrade_backup) if upgrade_backup else None,
        "database_initialized": database_initialized,
        "launchers": launchers,
        "desktop_launcher": desktop_launcher,
        "status": "configuration_required" if configuration_required else "ready",
        "start_page": "config" if configuration_required else "overview",
        "paid_provider_calls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--no-venv", action="store_true")
    parser.add_argument("--no-deps", action="store_true")
    parser.add_argument("--no-desktop-launcher", action="store_true")
    args = parser.parse_args()
    result = install(
        args.root,
        create_venv=not args.no_venv,
        install_dependencies=not args.no_deps,
        create_desktop_launcher=False,
    )
    result["desktop_launcher"] = (
        install_windows_desktop_launcher(args.root)
        if not args.no_desktop_launcher
        else None
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
