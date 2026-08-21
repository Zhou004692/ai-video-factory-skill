"""Sanitized replays for production incidents that previously blocked delivery."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory import db, production_contract, provider_tasks, subtitles  # noqa: E402
from factory.video_routing import precision_motion_reason  # noqa: E402


FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "incidents"
EXPECTED_FIXTURES = {
    "unknown_speaker.json",
    "overlong_master.json",
    "precision_motion.json",
    "provider_owner_conflict.json",
    "subtitle_tail_cut.json",
}


class IncidentReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(db.DDL)

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def _fixtures(self) -> list[dict]:
        names = {path.name for path in FIXTURE_ROOT.glob("*.json")}
        self.assertEqual(names, EXPECTED_FIXTURES)
        return [
            json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))
            for name in sorted(names)
        ]

    def test_incident_fixtures_are_minimal_and_contain_no_credentials(self):
        for fixture in self._fixtures():
            with self.subTest(incident=fixture["incident_id"]):
                self.assertEqual(
                    set(fixture), {"incident_id", "kind", "input", "expected"}
                )
                serialized = json.dumps(fixture, ensure_ascii=False).casefold()
                for forbidden in (
                    "authorization", "api_key", "access_token", "bearer ",
                    "secret_key", "asset://",
                ):
                    self.assertNotIn(forbidden, serialized)

    def test_all_incidents_replay_through_current_production_boundaries(self):
        handlers = {
            "production_contract": self._replay_contract,
            "precision_motion": self._replay_precision_motion,
            "provider_owner": self._replay_provider_owner,
            "subtitle_timeline": self._replay_subtitle_timeline,
        }
        for fixture in self._fixtures():
            with self.subTest(incident=fixture["incident_id"]):
                handlers[fixture["kind"]](fixture)

    def _replay_contract(self, fixture: dict) -> None:
        job_id = f"J-INCIDENT-{fixture['incident_id']}"
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,'STORYBOARDED','{}')",
            (job_id, "T-INCIDENT", fixture["incident_id"]),
        )
        self.database.commit()

        result = production_contract.compile_production_spec(
            self.database,
            job_id,
            self.root,
            storyboard_override=fixture["input"]["storyboard"],
            persist=False,
        )

        self.assertFalse(result.payable)
        self.assertIn(
            fixture["expected"]["issue_code"],
            [issue.code for issue in result.issues],
        )
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE job_id=?", (job_id,)
            ).fetchone()[0],
            0,
        )

    def _replay_precision_motion(self, fixture: dict) -> None:
        reason = precision_motion_reason(fixture["input"]["shot"])
        self.assertTrue(reason)
        self.assertIn(fixture["expected"]["reason_contains"], reason)

    def _replay_provider_owner(self, fixture: dict) -> None:
        original = fixture["input"]["original"]
        conflict = fixture["input"]["conflict"]
        provider_tasks.upsert_final_call(
            self.database, **original, status="completed", cost_cents=10,
        )
        with self.assertRaises(provider_tasks.ProviderTaskGateError):
            provider_tasks.upsert_final_call(
                self.database, **conflict, status="completed", cost_cents=99,
            )
        row = self.database.execute(
            "SELECT job_id,shot_idx,provider,intent,cost_cents "
            "FROM provider_calls WHERE task_id=?", (original["task_id"],)
        ).fetchone()
        self.assertEqual(tuple(row), (
            original["job_id"], original["shot_idx"], original["provider"],
            original["intent"], 10,
        ))

    def _replay_subtitle_timeline(self, fixture: dict) -> None:
        source = fixture["input"]
        cues = subtitles.timed_cues(
            source["text"], source["start"], source["end"],
            max_chars=source["max_chars"],
        )
        self.assertGreaterEqual(len(cues), fixture["expected"]["min_cues"])
        self.assertEqual(cues[0]["start"], source["start"])
        self.assertEqual(cues[-1]["end"], source["end"])
        self.assertTrue(all(
            left["end"] <= right["start"]
            for left, right in zip(cues, cues[1:])
        ))


if __name__ == "__main__":
    unittest.main()
