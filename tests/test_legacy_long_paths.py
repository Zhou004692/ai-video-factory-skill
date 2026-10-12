"""Offline regressions for legacy trees containing nested verification installs."""
from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def disk(path: Path) -> Path:
    """Fixture I/O must work before the production long-path fix exists."""
    value = str(path.absolute())
    if os.name == "nt":
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return Path(value)


@contextmanager
def disposable_tree():
    root = Path(tempfile.mkdtemp(prefix="avf-legacy-")).resolve()
    try:
        yield root
    finally:
        # The fixture is our newly created absolute temp directory, never user state.
        assert root.parent == Path(tempfile.gettempdir()).resolve()
        shutil.rmtree(disk(root))


def long_asset(data: Path) -> Path:
    path = data / "verification" / "distribution-audit-20260908" / "fresh user install" / "ai-video-factory" / ".venv" / "Lib" / "site-packages"
    while len(str(path / "paid.mp4")) < 350:
        path /= "nested-package-with-long-module-name"
    return path / "paid.mp4"


def paid_database(data: Path, asset: Path) -> sqlite3.Connection:
    disk(data).mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(data / "factory.db")
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("PRAGMA wal_autocheckpoint=0")
    database.execute("CREATE TABLE paid_tasks(id INTEGER PRIMARY KEY, provider_id TEXT, cost REAL, path TEXT, payload TEXT)")
    database.commit()
    database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database.execute("INSERT INTO paid_tasks VALUES(1, 'paid-remote-task', 12.75, ?, ?)", (str(asset), json.dumps({"assets": [str(asset)], "cost": 12.75})))
    database.commit()
    return database


def original_signature(source: Path, auxiliary: Path | None = None) -> str:
    """212dc9b signature algorithm, independent of the production implementation."""
    import runtime_paths

    digest = hashlib.sha256()
    roots = [("primary", source)]
    if auxiliary is not None:
        roots.extend((f"auxiliary/{name}", auxiliary / name) for name in runtime_paths.LEGACY_AUXILIARY_SUBTREES)
    for label, root in roots:
        if not disk(root).is_dir():
            continue
        for path in sorted((p for p in disk(root).rglob("*") if p.is_file()), key=lambda p: p.relative_to(disk(root)).as_posix()):
            if path.name in {".gitkeep", "factory.db-shm"}:
                continue
            digest.update(label.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.relative_to(disk(root)).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def old_migration_fixture(tmp: Path):
    import runtime_paths

    root, home = tmp / "skill", tmp / "external"
    source, target = root / "data", home / "data"
    asset = long_asset(source)
    disk(asset.parent).mkdir(parents=True)
    disk(asset).write_bytes(b"paid")
    for name in ("factory.db-wal", "factory.db-shm"):
        disk(asset.parent / name).write_bytes(b"nested-" + name.encode())
    writer = paid_database(source, asset)
    writer.close()
    shutil.copytree(disk(source), disk(target))
    marker = target / runtime_paths.LEGACY_MIGRATION_MARKER
    marker.write_text(json.dumps({"source": str(source), "auxiliary": None, "signature": original_signature(source)}), encoding="utf-8")
    (target / "runtime.json").write_text("{}", encoding="utf-8")
    return root, home, source, target, asset, marker


class LegacyLongPathTests(unittest.TestCase):
    def test_original_marker_with_complete_nested_sidecars_upgrades_and_repeats(self):
        import runtime_paths

        with disposable_tree() as tmp:
            root, home, source, target, asset, marker = old_migration_fixture(tmp)
            self.assertEqual(original_signature(source), runtime_paths._legacy_state_signature(source, None, version=1))
            with patch("runtime_paths.os.replace", wraps=os.replace) as replace:
                result = runtime_paths.prepare_runtime_state(root, {"FACTORY_RUNTIME_HOME": str(home)})
            self.assertFalse(result["data_migrated"])
            upgraded = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(upgraded["version"], 2)
            self.assertEqual(upgraded["signature"], runtime_paths._legacy_state_signature(source, None))
            self.assertTrue(any(Path(call.args[1]).name == marker.name for call in replace.call_args_list))
            repeated = runtime_paths.prepare_runtime_state(root, {"FACTORY_RUNTIME_HOME": str(home)})
            self.assertFalse(repeated["data_migrated"])
            self.assertFalse(list(target.glob(marker.name + ".tmp-*")))

    def test_original_marker_atomic_upgrade_failure_preserves_marker_and_retries(self):
        import runtime_paths

        with disposable_tree() as tmp:
            root, home, source, target, asset, marker = old_migration_fixture(tmp)
            before = marker.read_bytes()
            with patch("runtime_paths.os.replace", side_effect=PermissionError("injected marker write interruption")):
                with self.assertRaisesRegex(PermissionError, "injected"):
                    runtime_paths.prepare_runtime_state(root, {"FACTORY_RUNTIME_HOME": str(home)})
            self.assertEqual(marker.read_bytes(), before)
            self.assertFalse(list(target.glob(marker.name + ".tmp-*")))
            self.assertEqual(disk(asset).read_bytes(), b"paid")
            runtime_paths.prepare_runtime_state(root, {"FACTORY_RUNTIME_HOME": str(home)})
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["version"], 2)

    def test_original_marker_excludes_root_live_sidecars_from_destination_comparison(self):
        import runtime_paths

        with disposable_tree() as tmp:
            root, home, source, target, asset, marker = old_migration_fixture(tmp)
            writer = sqlite3.connect(source / "factory.db")
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("UPDATE paid_tasks SET provider_id='paid-root-wal'")
                writer.commit()
                self.assertTrue((source / "factory.db-wal").is_file())
                payload = json.loads(marker.read_text(encoding="utf-8"))
                payload["signature"] = original_signature(source)
                marker.write_text(json.dumps(payload), encoding="utf-8")
                self.assertFalse((target / "factory.db-wal").exists())
                runtime_paths.prepare_runtime_state(root, {"FACTORY_RUNTIME_HOME": str(home)})
                self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["version"], 2)
            finally:
                writer.close()

    def test_original_marker_missing_or_conflicting_nested_sidecar_is_actionable(self):
        import runtime_paths

        for sidecar, problem in (("factory.db-wal", "missing"), ("factory.db-shm", "missing"), ("factory.db-wal", "conflict"), ("factory.db-shm", "conflict")):
            with self.subTest(sidecar=sidecar, problem=problem), disposable_tree() as tmp:
                root, home, source, target, asset, marker = old_migration_fixture(tmp)
                copied = target / asset.parent.relative_to(source) / sidecar
                if problem == "missing":
                    disk(copied).unlink()
                else:
                    disk(copied).write_bytes(b"different-paid-state")
                before = marker.read_bytes()
                with self.assertRaises(RuntimeError) as raised:
                    runtime_paths.prepare_runtime_state(root, {"FACTORY_RUNTIME_HOME": str(home)})
                for expected in (str(asset.parent / sidecar), str(copied), "备份", "人工", "重试", "缺失" if problem == "missing" else "冲突"):
                    self.assertIn(expected, str(raised.exception))
                self.assertEqual(marker.read_bytes(), before)
                self.assertEqual(disk(asset).read_bytes(), b"paid")
                if problem == "missing":
                    self.assertFalse(disk(copied).exists())
                else:
                    self.assertEqual(disk(copied).read_bytes(), b"different-paid-state")

    def test_original_marker_does_not_trust_changed_paid_source_or_wrong_path(self):
        import runtime_paths

        for mutation in ("paid-asset", "paid-db", "source-path", "auxiliary-path", "unknown-version"):
            with self.subTest(mutation=mutation), disposable_tree() as tmp:
                root, home, source, target, asset, marker = old_migration_fixture(tmp)
                if mutation == "paid-asset":
                    disk(asset).write_bytes(b"new-paid-output")
                elif mutation == "paid-db":
                    db = sqlite3.connect(source / "factory.db")
                    try:
                        db.execute("UPDATE paid_tasks SET cost=99.25")
                        db.commit()
                    finally:
                        db.close()
                else:
                    payload = json.loads(marker.read_text(encoding="utf-8"))
                    payload[{"source-path": "source", "auxiliary-path": "auxiliary", "unknown-version": "version"}[mutation]] = 999 if mutation == "unknown-version" else str(tmp / "unrelated")
                    marker.write_text(json.dumps(payload), encoding="utf-8")
                before = marker.read_bytes()
                with self.assertRaisesRegex(RuntimeError, "同时存在"):
                    runtime_paths.prepare_runtime_state(root, {"FACTORY_RUNTIME_HOME": str(home)})
                self.assertEqual(marker.read_bytes(), before)

    def test_migration_preserves_long_tree_wal_paid_rows_and_logical_paths(self):
        import runtime_paths

        with disposable_tree() as tmp:
            root, home = tmp / "skill", tmp / "external"
            source = root / "data"
            asset = long_asset(source)
            disk(asset.parent).mkdir(parents=True)
            disk(asset).write_bytes(b"already-paid-media")
            # Nested verification installs are retained wholesale. Only the
            # active primary DB sidecars are replaced by its online backup.
            disk(asset.parent / "factory.db-wal").write_bytes(b"nested-verification-wal")
            disk(asset.parent / "factory.db-shm").write_bytes(b"nested-verification-shm")
            writer = paid_database(source, asset)
            try:
                source_rows = writer.execute("SELECT id,provider_id,cost FROM paid_tasks").fetchall()
                self.assertTrue((source / "factory.db-wal").is_file())
                result = runtime_paths.prepare_runtime_state(root, {"FACTORY_RUNTIME_HOME": str(home)})
                target = Path(result["data_dir"])
                copied = target / asset.relative_to(source)
                self.assertEqual(disk(copied).read_bytes(), b"already-paid-media")
                self.assertEqual(disk(asset).read_bytes(), b"already-paid-media")
                for sidecar in ("factory.db-wal", "factory.db-shm"):
                    self.assertEqual(disk(copied.parent / sidecar).read_bytes(), disk(asset.parent / sidecar).read_bytes())
                migrated = sqlite3.connect(target / "factory.db")
                try:
                    self.assertEqual(migrated.execute("SELECT id,provider_id,cost FROM paid_tasks").fetchall(), source_rows)
                    row = migrated.execute("SELECT path,payload FROM paid_tasks").fetchone()
                    self.assertEqual(migrated.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                finally:
                    migrated.close()
                self.assertEqual(row[0], str(copied))
                self.assertEqual(json.loads(row[1])["assets"], [str(copied)])
                self.assertEqual(writer.execute("SELECT id,provider_id,cost FROM paid_tasks").fetchall(), source_rows)
                self.assertEqual(writer.execute("SELECT path FROM paid_tasks").fetchone()[0], str(asset))
                marker = json.loads((target / runtime_paths.LEGACY_MIGRATION_MARKER).read_text(encoding="utf-8"))
                self.assertEqual(marker["version"], 2)
                self.assertEqual(marker["source"], str(source))
                self.assertEqual(marker["signature"], runtime_paths._legacy_state_signature(source, None))
                self.assertNotIn("\\\\?\\", json.dumps(marker))
                (target / "runtime.json").write_text("{}", encoding="utf-8")
                repeated = runtime_paths.prepare_runtime_state(root, {"FACTORY_RUNTIME_HOME": str(home)})
                self.assertFalse(repeated["data_migrated"])
            finally:
                writer.close()

    def test_interrupted_long_tree_copy_cleans_only_own_staging_and_retry_works(self):
        import runtime_paths

        with disposable_tree() as tmp:
            root, home = tmp / "skill", tmp / "external"
            source = root / "data"
            asset = long_asset(source)
            disk(asset.parent).mkdir(parents=True)
            disk(asset).write_bytes(b"paid")
            (source / "paid-task.txt").write_text("already paid", encoding="utf-8")
            writer = paid_database(source, asset)
            writer.close()
            original_database = (source / "factory.db").read_bytes()
            home.mkdir()
            unrelated = home / ".data.migrating-unrelated"
            unrelated.mkdir()
            (unrelated / "keep.txt").write_text("preserve", encoding="utf-8")
            original = shutil.copytree
            def interrupted(*args, **kwargs):
                original(*args, **kwargs)
                raise KeyboardInterrupt("injected copy interruption")
            with patch("runtime_paths.shutil.copytree", side_effect=interrupted):
                with self.assertRaisesRegex(KeyboardInterrupt, "injected"):
                    runtime_paths.prepare_runtime_state(root, {"FACTORY_RUNTIME_HOME": str(home)})
            self.assertEqual(list(home.glob(".data.migrating-*")), [unrelated])
            self.assertFalse((home / "data").exists())
            self.assertEqual(disk(asset).read_bytes(), b"paid")
            self.assertEqual((source / "factory.db").read_bytes(), original_database)
            result = runtime_paths.prepare_runtime_state(root, {"FACTORY_RUNTIME_HOME": str(home)})
            self.assertTrue(result["data_migrated"])
            self.assertEqual(disk(home / "data" / asset.relative_to(source)).read_bytes(), b"paid")
            migrated = sqlite3.connect(home / "data" / "factory.db")
            try:
                self.assertEqual(migrated.execute("SELECT provider_id,cost FROM paid_tasks").fetchone(), ("paid-remote-task", 12.75))
            finally:
                migrated.close()

    def test_long_file_alone_is_substantive_and_participates_in_signature(self):
        import runtime_paths

        with disposable_tree() as tmp:
            source = tmp / "skill" / "data"
            asset = long_asset(source)
            disk(asset.parent).mkdir(parents=True)
            disk(asset).write_bytes(b"paid")
            self.assertTrue(runtime_paths._has_substantive_state(source))
            before = runtime_paths._legacy_state_signature(source, None)
            disk(asset).write_bytes(b"paid-updated")
            self.assertNotEqual(before, runtime_paths._legacy_state_signature(source, None))

    def test_nested_verification_shm_is_included_in_migration_signature(self):
        import runtime_paths

        with disposable_tree() as tmp:
            source = tmp / "skill" / "data"
            sidecar = long_asset(source).parent / "factory.db-shm"
            disk(sidecar.parent).mkdir(parents=True)
            disk(sidecar).write_bytes(b"nested-before")
            before = runtime_paths._legacy_state_signature(source, None)
            disk(sidecar).write_bytes(b"nested-after")
            self.assertNotEqual(before, runtime_paths._legacy_state_signature(source, None))

    def test_schema_only_external_database_is_diagnosed_without_replacing_it(self):
        import runtime_paths

        with disposable_tree() as tmp:
            root, home = tmp / "skill", tmp / "external"
            source, target = root / "data", home / "data"
            source.mkdir(parents=True)
            (source / "paid.mp4").write_bytes(b"paid")
            target.mkdir(parents=True)
            database = sqlite3.connect(target / "factory.db")
            database.execute("CREATE TABLE unfamiliar_schema(value TEXT)")
            database.commit()
            database.close()
            unknown_file = target / "unrecognized-state.bin"
            unknown_file.write_bytes(b"must-inspect-before-moving")
            before = (target / "factory.db").read_bytes()
            with self.assertRaises(RuntimeError) as raised:
                runtime_paths.prepare_runtime_state(root, {"FACTORY_RUNTIME_HOME": str(home)})
            message = str(raised.exception)
            for expected in (str(source), str(target), "schema-only", "备份", "人工", "重试"):
                self.assertIn(expected, message)
            self.assertEqual((target / "factory.db").read_bytes(), before)
            self.assertEqual((source / "paid.mp4").read_bytes(), b"paid")
            self.assertEqual(unknown_file.read_bytes(), b"must-inspect-before-moving")
            self.assertFalse(list(home.glob(".data.migrating-*")))

    def test_two_populated_databases_remain_untouched(self):
        import runtime_paths

        with disposable_tree() as tmp:
            root, home = tmp / "skill", tmp / "external"
            source, target = root / "data", home / "data"
            connections = []
            try:
                for directory, price in ((source, 12.75), (target, 99.25)):
                    db = paid_database(directory, directory / "paid.mp4")
                    connections.append(db)
                    db.execute("UPDATE paid_tasks SET cost=?", (price,))
                    db.commit()
                with self.assertRaisesRegex(RuntimeError, "同时存在") as raised:
                    runtime_paths.prepare_runtime_state(root, {"FACTORY_RUNTIME_HOME": str(home)})
                self.assertNotIn("schema-only", str(raised.exception))
                self.assertEqual([db.execute("SELECT cost FROM paid_tasks").fetchone()[0] for db in connections], [12.75, 99.25])
            finally:
                for db in connections:
                    db.close()

    def test_packaged_bootstrap_can_import_helper_and_repeat_offline(self):
        from distribute_skill import build_distribution

        with disposable_tree() as tmp:
            home = tmp / "external"
            with patch.dict(os.environ, {"FACTORY_RUNTIME_HOME": str(home)}, clear=False):
                package = build_distribution(ROOT, tmp / "package")
            asset = long_asset(package / "data")
            disk(asset.parent).mkdir(parents=True)
            disk(asset).write_bytes(b"packaged-paid")
            writer = paid_database(package / "data", asset)
            writer.close()
            code = """
import json, pathlib, socket, sys
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path.cwd() / 'scripts'))
import runtime_filesystem
from bootstrap import bootstrap, _venv_python
root = pathlib.Path.cwd()
runtime = _venv_python(root)
runtime.parent.mkdir(parents=True)
runtime.write_bytes(b'offline-runtime')
with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')), patch('bootstrap._run_doctor', return_value={'ready': True, 'checks': {}, 'paid_provider_calls': 0}):
    first = bootstrap(root, profile='learning')
    second = bootstrap(root, profile='learning')
assert first['paid_provider_calls'] == second['paid_provider_calls'] == 0
assert first['ready'] and second['ready']
print('offline-bootstrap-ok')
"""
            environment = {**os.environ, "FACTORY_RUNTIME_HOME": str(home), "FACTORY_DATA_DIR": "", "FACTORY_ENV_FILE": "", "FACTORY_DB_PATH": ""}
            result = subprocess.run([sys.executable, "-c", code], cwd=package, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("offline-bootstrap-ok", result.stdout)
            self.assertEqual(disk(home / "data" / asset.relative_to(package / "data")).read_bytes(), b"packaged-paid")

    @unittest.skipUnless(os.name == "nt", "Windows physical path forms")
    def test_physical_path_supports_unc_and_is_idempotent(self):
        from runtime_filesystem import filesystem_path

        ordinary = Path(r"C:\runtime\assets\paid.mp4")
        unc = Path(r"\\offline-server\share\assets\paid.mp4")
        self.assertEqual(str(filesystem_path(ordinary)), "\\\\?\\" + str(ordinary))
        self.assertEqual(str(filesystem_path(unc)), "\\\\?\\UNC\\" + str(unc)[2:])
        self.assertEqual(filesystem_path(filesystem_path(ordinary)), filesystem_path(ordinary))
        self.assertEqual(filesystem_path(filesystem_path(unc)), filesystem_path(unc))


if __name__ == "__main__":
    unittest.main()
