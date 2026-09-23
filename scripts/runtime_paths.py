"""Stable mutable-state paths for replaceable AI Video Factory Skill code."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Mapping
import uuid


APP_DIRECTORY = "ai-video-factory"


def runtime_home(
    root: Path,
    environ: Mapping[str, str] | None = None,
) -> Path:
    values = dict(os.environ if environ is None else environ)
    explicit = str(values.get("FACTORY_RUNTIME_HOME") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.name == "nt":
        base = str(values.get("LOCALAPPDATA") or values.get("APPDATA") or "").strip()
        parent = Path(base).expanduser() if base else Path(root).resolve().parent
        return (parent / "AI Video Factory").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "AI Video Factory").resolve()
    base = str(values.get("XDG_STATE_HOME") or "").strip()
    parent = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return (parent / APP_DIRECTORY).resolve()


def environment_file(
    root: Path,
    environ: Mapping[str, str] | None = None,
) -> Path:
    values = dict(os.environ if environ is None else environ)
    explicit = str(values.get("FACTORY_ENV_FILE") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return runtime_home(root, values) / ".env"


def existing_environment_file(
    root: Path,
    environ: Mapping[str, str] | None = None,
) -> Path:
    external = environment_file(root, environ)
    legacy = Path(root).resolve() / ".env"
    if not external.is_file() and legacy.is_file():
        return legacy
    return external


def read_environment_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in raw_line or raw_line.lstrip().startswith("#"):
            continue
        key, value = raw_line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def resolve_data_dir(
    root: Path,
    environ: Mapping[str, str] | None = None,
) -> Path:
    values = dict(os.environ if environ is None else environ)
    configured = str(values.get("FACTORY_DATA_DIR") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = runtime_home(root, values) / path
        return path.resolve()
    return (runtime_home(root, values) / "data").resolve()


def _copy_sqlite(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(source, timeout=30)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def _copy_legacy_data(source: Path, destination: Path) -> None:
    staging = destination.with_name(f".{destination.name}.migrating-{uuid.uuid4().hex}")
    try:
        shutil.copytree(
            source,
            staging,
            ignore=shutil.ignore_patterns("factory.db-wal", "factory.db-shm"),
        )
        source_database = source / "factory.db"
        if source_database.is_file():
            target_database = staging / "factory.db"
            target_database.unlink(missing_ok=True)
            _copy_sqlite(source_database, target_database)
        staging.replace(destination)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def prepare_runtime_state(
    root: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Create stable state paths and copy legacy in-Skill state once."""
    root = Path(root).resolve()
    values = dict(os.environ if environ is None else environ)
    home = runtime_home(root, values)
    home.mkdir(parents=True, exist_ok=True)
    env_file = environment_file(root, values)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_env = root / ".env"
    env_migrated = False
    if not env_file.exists() and legacy_env.is_file() and env_file != legacy_env:
        shutil.copy2(legacy_env, env_file)
        env_migrated = True

    combined = {
        **read_environment_file(env_file),
        **{key: value for key, value in values.items() if str(value).strip()},
    }
    data_dir = resolve_data_dir(root, combined)
    configured_data = str(combined.get("FACTORY_DATA_DIR") or "").strip()
    legacy_data = Path(configured_data).expanduser() if configured_data else root / "data"
    if not legacy_data.is_absolute():
        legacy_data = root / legacy_data
    legacy_data = legacy_data.resolve()
    data_migrated = False
    empty_external_data = data_dir.is_dir() and not any(data_dir.iterdir())
    if (
        data_dir != legacy_data.resolve()
        and legacy_data.is_dir()
        and (not data_dir.exists() or empty_external_data)
    ):
        data_dir.parent.mkdir(parents=True, exist_ok=True)
        if empty_external_data:
            data_dir.rmdir()
        _copy_legacy_data(legacy_data, data_dir)
        data_migrated = True
    else:
        data_dir.mkdir(parents=True, exist_ok=True)

    return {
        "runtime_home": home,
        "env_file": env_file,
        "data_dir": data_dir,
        "env_migrated": env_migrated,
        "data_migrated": data_migrated,
    }
