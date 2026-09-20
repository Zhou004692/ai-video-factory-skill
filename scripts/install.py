"""Initialize the local runtime used by the AI Video Factory Skill."""
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
            install_dependencies: bool = True) -> dict:
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
