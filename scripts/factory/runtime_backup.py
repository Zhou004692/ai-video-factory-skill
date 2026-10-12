"""Recoverable application-state snapshots used during local upgrades."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

from runtime_filesystem import filesystem_path, sqlite_readonly_uri


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with filesystem_path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    """Create a transactionally consistent standalone SQLite snapshot."""
    source_connection = sqlite3.connect(filesystem_path(source), timeout=30)
    destination_connection = sqlite3.connect(filesystem_path(destination))
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def _restore_sqlite(snapshot: Path, target: Path) -> None:
    """Restore through SQLite so open WAL readers never see a replaced DB file."""
    source_connection = sqlite3.connect(
        sqlite_readonly_uri(snapshot), uri=True, timeout=30
    )
    target_connection = sqlite3.connect(filesystem_path(target), timeout=30)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    except sqlite3.DatabaseError as exc:
        message = str(exc).casefold()
        if "not a database" not in message and "malformed" not in message:
            raise
        target_connection.close()
        source_connection.close()
        filesystem_path(Path(f"{target}-wal")).unlink(missing_ok=True)
        filesystem_path(Path(f"{target}-shm")).unlink(missing_ok=True)
        shutil.copy2(filesystem_path(snapshot), filesystem_path(target))
        return
    finally:
        try:
            target_connection.close()
        finally:
            source_connection.close()


def create_upgrade_snapshot(
    *, root: Path, data_dir: Path, env_file: Path
) -> Path | None:
    targets = (
        ("environment", env_file, ".env"),
        ("database", data_dir / "factory.db", "factory.db"),
        ("character_assets", data_dir / "character_assets", "character_assets"),
        ("assets", data_dir / "assets", "assets"),
    )
    if not any(filesystem_path(source).exists() for _, source, _ in targets):
        return None

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = data_dir / "backups" / f"upgrade-{stamp}"
    filesystem_path(backup).mkdir(parents=True, exist_ok=False)
    manifest: dict[str, object] = {"version": 1, "items": []}
    items: list[dict[str, object]] = []
    for name, source, relative in targets:
        item: dict[str, object] = {
            "name": name,
            "source": str(source.resolve()),
            "snapshot": relative,
            "existed": filesystem_path(source).exists(),
            "kind": "directory" if filesystem_path(source).is_dir() else "file",
        }
        if filesystem_path(source).is_dir():
            shutil.copytree(filesystem_path(source), filesystem_path(backup / relative))
        elif filesystem_path(source).is_file():
            if name == "database":
                _snapshot_sqlite(source, backup / relative)
            else:
                shutil.copy2(filesystem_path(source), filesystem_path(backup / relative))
            item["sha256"] = _file_digest(backup / relative)
        items.append(item)
    manifest["items"] = items
    filesystem_path(backup / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return backup


def restore_upgrade_snapshot(backup: Path) -> None:
    manifest = json.loads(filesystem_path(backup / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest["items"]:
        target = Path(item["source"])
        snapshot = backup / item["snapshot"]
        if not item["existed"]:
            if filesystem_path(target).is_dir():
                shutil.rmtree(filesystem_path(target))
            else:
                filesystem_path(target).unlink(missing_ok=True)
            continue
        filesystem_path(target.parent).mkdir(parents=True, exist_ok=True)
        if item["kind"] == "directory":
            if filesystem_path(target).exists():
                shutil.rmtree(filesystem_path(target))
            shutil.copytree(filesystem_path(snapshot), filesystem_path(target))
        else:
            if item["name"] == "database":
                _restore_sqlite(snapshot, target)
            else:
                shutil.copy2(filesystem_path(snapshot), filesystem_path(target))
