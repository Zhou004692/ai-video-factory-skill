"""Stable mutable-state paths for replaceable AI Video Factory Skill code."""
from __future__ import annotations

import os
import json
import hashlib
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from typing import Mapping
import uuid

from runtime_filesystem import filesystem_path, iter_tree, sqlite_readonly_uri


APP_DIRECTORY = "ai-video-factory"
LEGACY_MIGRATION_MARKER = ".legacy-migration.json"
LEGACY_AUXILIARY_SUBTREES = (
    "assets",
    "character_assets",
    "character_previews",
    "acceptance",
    "management_uploads",
)


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
    source_connection = sqlite3.connect(filesystem_path(source), timeout=30)
    destination_connection = sqlite3.connect(filesystem_path(destination))
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
    database = sqlite3.connect(filesystem_path(database_path))
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
    if filesystem_path(left).stat().st_size != filesystem_path(right).stat().st_size:
        return False
    digests = []
    for path in (left, right):
        digest = hashlib.sha256()
        with filesystem_path(path).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        digests.append(digest.digest())
    return digests[0] == digests[1]


def _legacy_state_signature(
    source: Path,
    auxiliary_data: Path | None,
    *,
    version: int = 2,
) -> str:
    digest = hashlib.sha256()
    roots: list[tuple[str, Path]] = [("primary", source)]
    if auxiliary_data is not None:
        roots.extend(
            (f"auxiliary/{name}", auxiliary_data / name)
            for name in LEGACY_AUXILIARY_SUBTREES
        )
    for label, root in roots:
        if not filesystem_path(root).is_dir():
            continue
        for path in sorted(
            (item for item in iter_tree(root) if filesystem_path(item).is_file()),
            key=lambda item: item.relative_to(root).as_posix(),
        ):
            if path.name == ".gitkeep" or (version == 1 and path.name == "factory.db-shm") or (
                label == "primary" and path == source / "factory.db-shm"
            ):
                continue
            digest.update(label.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            with filesystem_path(path).open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            digest.update(b"\0")
    return digest.hexdigest()


def _legacy_migration_matches(
    destination: Path,
    source: Path,
    auxiliary_data: Path | None,
) -> bool:
    marker = destination / LEGACY_MIGRATION_MARKER
    if not filesystem_path(marker).is_file():
        return False
    try:
        payload = json.loads(filesystem_path(marker).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    expected_auxiliary = str(auxiliary_data.resolve()) if auxiliary_data else None
    if not isinstance(payload, dict) or (
        payload.get("source") != str(source.resolve())
        or payload.get("auxiliary") != expected_auxiliary
    ):
        return False
    if payload.get("version") == 2:
        return payload.get("signature") == _legacy_state_signature(source, auxiliary_data)
    if "version" in payload:
        return False
    # Unversioned markers use the original signature, which omitted all SHM
    # files. First require its known source to be unchanged, then verify the
    # nested sidecars the old copier could omit. Root live sidecars are covered
    # by SQLite's online backup and must not be compared as ordinary files.
    if payload.get("signature") != _legacy_state_signature(source, auxiliary_data, version=1):
        return False
    roots = [(source, destination)]
    if auxiliary_data is not None:
        roots.extend((auxiliary_data / name, destination / name) for name in LEGACY_AUXILIARY_SUBTREES)
    for original, copied in roots:
        if not filesystem_path(original).is_dir():
            continue
        for path in iter_tree(original):
            if path.name not in {"factory.db-wal", "factory.db-shm"}:
                continue
            if original == source and path.parent == source:
                continue
            if not filesystem_path(path).is_file():
                continue
            target = copied / path.relative_to(original)
            if not filesystem_path(target).is_file():
                issue = "缺失"
            elif not _same_file_content(path, target):
                issue = "冲突"
            else:
                continue
            raise RuntimeError(
                f"旧版迁移标记核验发现嵌套文件{issue}；已停止升级。"
                f"源文件：{path}；目标文件：{target}。"
                "源数据、目标数据及原标记均已保留；请先分别备份，由人工核对"
                "嵌套数据库及 WAL/SHM 的完整恢复方式，修复后重试。"
                "不要直接覆盖冲突文件或删除未知状态。"
            )
    upgraded = {**payload, "version": 2, "signature": _legacy_state_signature(source, auxiliary_data)}
    temporary = marker.with_name(marker.name + f".tmp-{uuid.uuid4().hex}")
    try:
        with filesystem_path(temporary).open("x", encoding="utf-8") as stream:
            json.dump(upgraded, stream, ensure_ascii=False, indent=2)
        os.replace(filesystem_path(temporary), filesystem_path(marker))
    finally:
        filesystem_path(temporary).unlink(missing_ok=True)
    return True


def _merge_legacy_subtree(source: Path, destination: Path) -> None:
    if not filesystem_path(source).is_dir():
        return
    for path in iter_tree(source):
        relative = path.relative_to(source)
        target = destination / relative
        if filesystem_path(path).is_dir():
            filesystem_path(target).mkdir(parents=True, exist_ok=True)
            continue
        filesystem_path(target.parent).mkdir(parents=True, exist_ok=True)
        if filesystem_path(target).exists():
            if not filesystem_path(target).is_file() or not _same_file_content(path, target):
                raise RuntimeError(f"旧运行数据存在冲突文件：{relative}")
            continue
        shutil.copy2(filesystem_path(path), filesystem_path(target))


def _promote_staging_directory(staging: Path, destination: Path) -> None:
    """Atomically promote a migration tree, tolerating transient Windows scans."""
    for attempt in range(8):
        try:
            os.replace(filesystem_path(staging), filesystem_path(destination))
            return
        except PermissionError:
            if filesystem_path(destination).exists() or attempt == 7:
                raise
            time.sleep(min(0.05 * (2 ** attempt), 0.4))


def _copy_legacy_data(
    source: Path,
    destination: Path,
    *,
    auxiliary_data: Path | None = None,
) -> None:
    staging = destination.with_name(f".{destination.name}.migrating-{uuid.uuid4().hex}")
    physical_source = filesystem_path(source)

    def ignore_primary_sidecars(directory: str, names: list[str]) -> set[str]:
        # Preserve nested verification files verbatim; the primary DB alone
        # receives a consistent online backup instead of copied WAL sidecars.
        if Path(directory) != physical_source:
            return set()
        return set(names) & {"factory.db-wal", "factory.db-shm"}

    try:
        if filesystem_path(source).is_dir():
            shutil.copytree(
                physical_source,
                filesystem_path(staging),
                ignore=ignore_primary_sidecars,
            )
        else:
            filesystem_path(staging).mkdir(parents=True)
        if auxiliary_data is not None and filesystem_path(auxiliary_data).is_dir():
            for name in LEGACY_AUXILIARY_SUBTREES:
                _merge_legacy_subtree(
                    auxiliary_data / name,
                    staging / name,
                )
        source_database = source / "factory.db"
        if filesystem_path(source_database).is_file():
            target_database = staging / "factory.db"
            filesystem_path(target_database).unlink(missing_ok=True)
            _copy_sqlite(source_database, target_database)
            _rebase_database_paths(target_database, source, destination)
            if auxiliary_data is not None:
                _rebase_database_paths(target_database, auxiliary_data, destination)
        filesystem_path(staging / LEGACY_MIGRATION_MARKER).write_text(
            json.dumps(
                {
                    "version": 2,
                    "source": str(source.resolve()),
                    "auxiliary": (
                        str(auxiliary_data.resolve()) if auxiliary_data else None
                    ),
                    "signature": _legacy_state_signature(source, auxiliary_data),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        _promote_staging_directory(staging, destination)
    except BaseException:
        # Only this invocation's generated sibling may be removed. Never follow
        # an unexpectedly substituted symlink/junction into another state tree.
        if filesystem_path(staging).exists():
            if staging.is_symlink() or staging.resolve() != staging.absolute():
                raise RuntimeError(f"迁移暂存目录发生变化，已保留供人工核对：{staging}")
            shutil.rmtree(filesystem_path(staging))
        raise


def _has_substantive_state(directory: Path) -> bool:
    if not filesystem_path(directory).is_dir():
        return False
    return any(
        filesystem_path(path).is_file() and path.name != ".gitkeep"
        for path in iter_tree(directory)
    )


def _external_database_diagnostic(directory: Path) -> str:
    """Describe observed empty schemas, without treating them as disposable."""
    database_path = directory / "factory.db"
    if not filesystem_path(database_path).is_file():
        return ""
    try:
        database = sqlite3.connect(sqlite_readonly_uri(database_path), uri=True, timeout=5)
        try:
            tables = database.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            for (name,) in tables:
                quoted = '"' + name.replace('"', '""') + '"'
                if database.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone():
                    return ""
        finally:
            database.close()
    except sqlite3.Error:
        return "外部 factory.db 无法只读核验，需人工检查数据库及其它文件。"
    return (
        "外部 factory.db 未发现业务行，可能仅有表结构（schema-only）；"
        "未知表结构及其它文件仍需人工核对，不能据此判定目录可丢弃。"
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
            for name in LEGACY_AUXILIARY_SUBTREES
        )
    )
    legacy_state_present = (
        _has_substantive_state(legacy_data) or auxiliary_state_present
    )
    completed_external_state = (data_dir / "runtime.json").is_file()
    migrated_legacy_unchanged = (
        completed_external_state
        and legacy_state_present
        and _legacy_migration_matches(
            data_dir,
            legacy_data,
            auxiliary_data if auxiliary_state_present else None,
        )
    )
    if (
        data_dir != legacy_data
        and legacy_state_present
        and data_dir.exists()
        and not empty_external_data
        and not migrated_legacy_unchanged
    ):
        raise RuntimeError(
            "旧 Skill 数据与未完成的外部运行数据同时存在；已停止升级，"
            f"避免覆盖有效任务。旧数据：{legacy_data}；外部数据：{data_dir}。"
            + _external_database_diagnostic(data_dir)
            + "两个目录均已保留；请先分别备份并人工核对任务、费用、资产及其它文件。"
            "仅在确认外部目录可移出后，由人工将其完整移至其它备份目录，"
            "保留旧数据原目录，再重试迁移；不要删除数据库或单独移除未知文件。"
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
