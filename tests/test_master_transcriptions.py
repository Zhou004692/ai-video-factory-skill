from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory import db as factory_db, master_transcriptions  # noqa: E402
from factory.clients import bigasr  # noqa: E402


class MasterTranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(factory_db.DDL)
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) VALUES(?,?,?,?)",
            ("J-ASR", "T-ASR", "derived ASR", "PRODUCTION_RUNNING"),
        )
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.database.close()
        self.temporary.cleanup()

    def test_artifacts_are_bound_to_media_bytes_without_overwriting_siblings(self):
        first = self.root / "master-first.mp4"
        second = self.root / "master-second.mp4"
        first.write_bytes(b"authored first master")
        second.write_bytes(b"authored replacement master")
        original = bigasr.Transcription(
            "original", 0, 700,
            (bigasr.TimedUtterance("original", 0, 700, "narrator"),),
        )
        replacement = bigasr.Transcription("replacement", 0, 900)

        master_transcriptions.persist(
            self.database, "J-ASR", 1, first, original,
        )
        master_transcriptions.persist(
            self.database, "J-ASR", 1, second, replacement,
        )
        self.database.commit()

        self.assertEqual(
            master_transcriptions.load_matching(
                self.database, "J-ASR", 1, first,
            ).text,
            "original",
        )
        self.assertEqual(
            master_transcriptions.load_matching(
                self.database, "J-ASR", 1, second,
            ).text,
            "replacement",
        )
        self.assertEqual(self.database.execute(
            "SELECT COUNT(*) FROM master_transcriptions "
            "WHERE job_id='J-ASR' AND master_idx=1"
        ).fetchone()[0], 2)

        self.database.execute(
            "UPDATE master_transcriptions SET transcription_json='{' "
            "WHERE job_id='J-ASR' AND master_idx=1 AND media_sha256=?",
            (master_transcriptions.media_sha256(second),),
        )
        self.database.commit()
        self.assertIsNone(master_transcriptions.load_matching(
            self.database, "J-ASR", 1, second,
        ))
        self.assertEqual(
            master_transcriptions.load_matching(
                self.database, "J-ASR", 1, first,
            ).text,
            "original",
        )

        second.write_bytes(b"different untranscribed bytes")
        self.assertIsNone(master_transcriptions.load_matching(
            self.database, "J-ASR", 1, second,
        ))

    def test_connect_adds_derived_table_to_an_existing_database(self):
        path = self.root / "existing.db"
        existing = sqlite3.connect(path)
        existing.executescript(factory_db.DDL)
        existing.execute("DROP TABLE master_transcriptions")
        existing.commit()
        existing.close()

        with mock.patch.dict(
            "os.environ", {"FACTORY_DB_PATH": str(path)}, clear=False,
        ):
            upgraded = factory_db.conn()
        try:
            self.assertIsNotNone(upgraded.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='master_transcriptions'"
            ).fetchone())
            columns = [
                row[1] for row in upgraded.execute(
                    "PRAGMA table_info(master_transcriptions)"
                ).fetchall()
            ]
            self.assertEqual(
                columns,
                ["job_id", "master_idx", "media_sha256",
                 "transcription_json", "created_at", "updated_at"],
            )
        finally:
            upgraded.close()


if __name__ == "__main__":
    unittest.main()
