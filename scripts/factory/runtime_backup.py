"""Recoverable application-state snapshots used during local upgrades."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    """Create a transactionally consistent standalone SQLite snapshot."""
    source_connection = sqlite3.connect(source, timeout=30)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def _restore_sqlite(snapshot: Path, target: Path) -> None:
    """Restore through SQLite so open WAL readers never see a replaced DB file."""
    source_connection = sqlite3.connect(
        f"file:{snapshot.as_posix()}?mode=ro", uri=True, timeout=30
    )
    target_connection = sqlite3.connect(target, timeout=30)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    except sqlite3.DatabaseError as exc:
        message = str(exc).casefold()
        if "not a database" not in message and "malformed" not in message:
            raise
        target_connection.close()
        source_connection.close()
        Path(f"{target}-wal").unlink(missing_ok=True)
        Path(f"{target}-shm").unlink(missing_ok=True)
        shutil.copy2(snapshot, target)
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
    if not any(source.exists() for _, source, _ in targets):
        return None

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = data_dir / "backups" / f"upgrade-{stamp}"
    backup.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, object] = {"version": 1, "items": []}
    items: list[dict[str, object]] = []
    for name, source, relative in targets:
        item: dict[str, object] = {
            "name": name,
            "source": str(source.resolve()),
            "snapshot": relative,
            "existed": source.exists(),
            "kind": "directory" if source.is_dir() else "file",
        }
        if source.is_dir():
            shutil.copytree(source, backup / relative)
        elif source.is_file():
            if name == "database":
                _snapshot_sqlite(source, backup / relative)
            else:
                shutil.copy2(source, backup / relative)
            item["sha256"] = _file_digest(backup / relative)
        items.append(item)
    manifest["items"] = items
    (backup / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return backup


def restore_upgrade_snapshot(backup: Path) -> None:
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest["items"]:
        target = Path(item["source"])
        snapshot = backup / item["snapshot"]
        if not item["existed"]:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if item["kind"] == "directory":
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(snapshot, target)
        else:
            if item["name"] == "database":
                _restore_sqlite(snapshot, target)
            else:
                shutil.copy2(snapshot, target)
