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


ROOT = Path(__file__).resolve().parents[1]


WINDOWS_LAUNCHER = """@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\\open_management.ps1"
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


def _initialize_database(root: Path) -> bool:
    db_module = root / "scripts" / "factory" / "db.py"
    if not db_module.is_file():
        return False
    database = root / "data" / "factory.db"
    env = os.environ.copy()
    env["FACTORY_DB_PATH"] = str(database)
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
        if not path.exists():
            path.write_text(content, encoding="utf-8", newline="\n")
        if path.suffix == ".sh":
            path.chmod(path.stat().st_mode | 0o111)
    return [str(path) for path in launchers]


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


def install(root: Path = ROOT, *, create_venv: bool = True,
            install_dependencies: bool = True) -> dict:
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required")
    root = root.resolve()
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env_file = root / ".env"
    env_created = False
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
    configuration_required = _configuration_required(env_file)

    return {
        "root": str(root),
        "env_created": env_created,
        "venv_created": venv_created,
        "data_dir": str(data_dir),
        "database_initialized": database_initialized,
        "launchers": launchers,
        "status": "configuration_required" if configuration_required else "ready",
        "start_page": "config" if configuration_required else "overview",
        "paid_provider_calls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--no-venv", action="store_true")
    parser.add_argument("--no-deps", action="store_true")
    args = parser.parse_args()
    result = install(
        args.root,
        create_venv=not args.no_venv,
        install_dependencies=not args.no_deps,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
