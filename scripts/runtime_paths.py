"""Stable mutable-state paths for replaceable AI Video Factory Skill code."""
from __future__ import annotations

import os
import json
import hashlib
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


def _write_environment_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    replacement = f"{key}={value}"
    updated: list[str] = []
    replaced = False
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            current, _separator, _raw = line.partition("=")
            if current.strip() == key:
                if not replaced:
                    updated.append(replacement)
                    replaced = True
                continue
        updated.append(line)
    if not replaced:
        updated.append(replacement)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(updated) + "\n", encoding="utf-8")
    temporary.replace(path)


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


def _rebase_path_string(value: str, source: Path, destination: Path) -> str:
    candidate = Path(value)
    if not candidate.is_absolute():
        return value
    try:
        relative = candidate.resolve().relative_to(source)
    except (OSError, ValueError):
        return value
    return str(destination / relative)


def _rebase_json_paths(value: object, source: Path, destination: Path) -> object:
    if isinstance(value, str):
        return _rebase_path_string(value, source, destination)
    if isinstance(value, list):
        return [_rebase_json_paths(item, source, destination) for item in value]
    if isinstance(value, dict):
        return {
            key: _rebase_json_paths(item, source, destination)
            for key, item in value.items()
        }
    return value


def _rebase_database_paths(
    database_path: Path,
    source_data_dir: Path,
    destination_data_dir: Path,
) -> None:
    """Rebase only absolute paths rooted in the replaceable legacy data tree."""
    source = source_data_dir.resolve()
    destination = destination_data_dir.resolve()
    database = sqlite3.connect(database_path)
    database.row_factory = sqlite3.Row
    try:
        tables = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for table_row in tables:
            table = str(table_row["name"])
            quoted_table = '"' + table.replace('"', '""') + '"'
            columns = database.execute(f"PRAGMA table_info({quoted_table})").fetchall()
            text_columns = [
                str(column["name"])
                for column in columns
                if "TEXT" in str(column["type"] or "").upper()
            ]
            if not text_columns:
                continue
            quoted_columns = [
                '"' + name.replace('"', '""') + '"' for name in text_columns
            ]
            rows = database.execute(
                f"SELECT rowid AS __runtime_rowid__,{','.join(quoted_columns)} "
                f"FROM {quoted_table}"
            ).fetchall()
            for row in rows:
                updates: dict[str, str] = {}
                for column in text_columns:
                    raw = row[column]
                    if not isinstance(raw, str) or not raw:
                        continue
                    rebased = _rebase_path_string(raw, source, destination)
                    if rebased == raw and raw.lstrip().startswith(("{", "[")):
                        try:
                            decoded = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            pass
                        else:
                            updated = _rebase_json_paths(decoded, source, destination)
                            if updated != decoded:
                                rebased = json.dumps(
                                    updated,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                    if rebased != raw:
                        updates[column] = rebased
                if not updates:
                    continue
                assignments = ",".join(
                    f"{quoted_columns[text_columns.index(name)]}=?"
                    for name in updates
                )
                database.execute(
                    f"UPDATE {quoted_table} SET {assignments} WHERE rowid=?",
                    (*updates.values(), row["__runtime_rowid__"]),
                )
        database.commit()
    finally:
        database.close()


def _same_file_content(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    digests = []
    for path in (left, right):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        digests.append(digest.digest())
    return digests[0] == digests[1]


def _merge_legacy_subtree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        return
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not target.is_file() or not _same_file_content(path, target):
                raise RuntimeError(f"旧运行数据存在冲突文件：{relative}")
            continue
        shutil.copy2(path, target)


def _copy_legacy_data(
    source: Path,
    destination: Path,
    *,
    auxiliary_data: Path | None = None,
) -> None:
    staging = destination.with_name(f".{destination.name}.migrating-{uuid.uuid4().hex}")
    try:
        if source.is_dir():
            shutil.copytree(
                source,
                staging,
                ignore=shutil.ignore_patterns("factory.db-wal", "factory.db-shm"),
            )
        else:
            staging.mkdir(parents=True)
        if auxiliary_data is not None and auxiliary_data.is_dir():
            for name in (
                "assets",
                "character_assets",
                "acceptance",
                "management_uploads",
            ):
                _merge_legacy_subtree(
                    auxiliary_data / name,
                    staging / name,
                )
        source_database = source / "factory.db"
        if source_database.is_file():
            target_database = staging / "factory.db"
            target_database.unlink(missing_ok=True)
            _copy_sqlite(source_database, target_database)
            _rebase_database_paths(target_database, source, destination)
            if auxiliary_data is not None:
                _rebase_database_paths(target_database, auxiliary_data, destination)
        staging.replace(destination)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _has_substantive_state(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    return any(
        path.is_file() and path.name != ".gitkeep"
        for path in directory.rglob("*")
    )


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

    file_values = read_environment_file(env_file)
    combined = {
        **file_values,
        **{key: value for key, value in values.items() if str(value).strip()},
    }
    data_dir = resolve_data_dir(root, combined)
    configured_data = str(combined.get("FACTORY_DATA_DIR") or "").strip()
    legacy_data = Path(configured_data).expanduser() if configured_data else root / "data"
    if not legacy_data.is_absolute():
        legacy_data = root / legacy_data
    legacy_data = legacy_data.resolve()
    if data_dir.is_relative_to(root):
        process_override = str(values.get("FACTORY_DATA_DIR") or "").strip()
        if process_override:
            raise RuntimeError(
                "FACTORY_DATA_DIR 指向可替换的 Skill 目录；"
                "请改为 Skill 目录之外的绝对路径后重试。"
            )
        data_dir = (home / "data").resolve()
        if env_file.is_file():
            _write_environment_value(env_file, "FACTORY_DATA_DIR", str(data_dir))
    hardcoded_legacy_data = (root / "data").resolve()
    auxiliary_data = (
        hardcoded_legacy_data if hardcoded_legacy_data != legacy_data else None
    )
    data_migrated = False
    empty_external_data = data_dir.is_dir() and not any(data_dir.iterdir())
    auxiliary_state_present = bool(
        auxiliary_data
        and any(
            _has_substantive_state(auxiliary_data / name)
            for name in (
                "assets",
                "character_assets",
                "acceptance",
                "management_uploads",
            )
        )
    )
    legacy_state_present = (
        _has_substantive_state(legacy_data) or auxiliary_state_present
    )
    completed_external_state = (data_dir / "runtime.json").is_file()
    if (
        data_dir != legacy_data
        and legacy_state_present
        and data_dir.exists()
        and not empty_external_data
        and not completed_external_state
    ):
        raise RuntimeError(
            "旧 Skill 数据与未完成的外部运行数据同时存在；已停止升级，"
            "请先核对两个目录，避免覆盖有效任务。"
        )
    if (
        data_dir != legacy_data
        and legacy_state_present
        and (not data_dir.exists() or empty_external_data)
    ):
        data_dir.parent.mkdir(parents=True, exist_ok=True)
        if empty_external_data:
            data_dir.rmdir()
        _copy_legacy_data(
            legacy_data,
            data_dir,
            auxiliary_data=auxiliary_data if auxiliary_state_present else None,
        )
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
