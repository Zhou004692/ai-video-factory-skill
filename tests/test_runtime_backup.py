from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class RuntimeBackupTests(unittest.TestCase):
    def test_upgrade_snapshot_includes_committed_rows_still_in_wal(self):
        from factory.runtime_backup import create_upgrade_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skill"
            data_dir = Path(tmp) / "runtime"
            root.mkdir()
            data_dir.mkdir()
            env_file = data_dir / ".env"
            env_file.write_text("ARK_API_KEY=<required>\n", encoding="utf-8")
            database = data_dir / "factory.db"

            writer = sqlite3.connect(database)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE markers(value TEXT NOT NULL)")
                writer.commit()
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                writer.execute("INSERT INTO markers(value) VALUES ('from-wal')")
                writer.commit()

                self.assertTrue(database.with_name("factory.db-wal").is_file())
                backup = create_upgrade_snapshot(
                    root=root,
                    data_dir=data_dir,
                    env_file=env_file,
                )
            finally:
                writer.close()

            self.assertIsNotNone(backup)
            snapshot = sqlite3.connect(Path(backup) / "factory.db")
            try:
                values = [row[0] for row in snapshot.execute("SELECT value FROM markers")]
                integrity = snapshot.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                snapshot.close()

            self.assertEqual(values, ["from-wal"])
            self.assertEqual(integrity, "ok")

    def test_restore_removes_stale_sqlite_wal_sidecars(self):
        from factory.runtime_backup import (
            create_upgrade_snapshot,
            restore_upgrade_snapshot,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skill"
            data_dir = Path(tmp) / "runtime"
            root.mkdir()
            data_dir.mkdir()
            env_file = data_dir / ".env"
            env_file.write_text("ARK_API_KEY=<required>\n", encoding="utf-8")
            database = data_dir / "factory.db"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE markers(value TEXT NOT NULL)")
                connection.execute("INSERT INTO markers(value) VALUES ('original')")
                connection.commit()
            finally:
                connection.close()

            backup = create_upgrade_snapshot(
                root=root,
                data_dir=data_dir,
                env_file=env_file,
            )
            database.write_bytes(b"corrupted")
            wal = database.with_name("factory.db-wal")
            shm = database.with_name("factory.db-shm")
            wal.write_bytes(b"stale-wal")
            shm.write_bytes(b"stale-shm")

            restore_upgrade_snapshot(Path(backup))

            self.assertFalse(wal.exists())
            self.assertFalse(shm.exists())
            restored = sqlite3.connect(database)
            try:
                value = restored.execute("SELECT value FROM markers").fetchone()[0]
            finally:
                restored.close()
            self.assertEqual(value, "original")


if __name__ == "__main__":
    unittest.main()
