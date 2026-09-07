"""Production node transition and dependency tests."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import creative_ir, db as factory_db, node_state_machine, production_compiler, production_graph  # noqa: E402
from tests.fixtures.production_profiles import load_profile_fixture  # noqa: E402


class NodeStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(factory_db.DDL)
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) VALUES(?,?,?,?)",
            ("J-NODE", "T-NODE", "节点状态", "STORYBOARDED"),
        )
        source = load_profile_fixture("stable_single_dialogue")
        spec = production_compiler.compile_ir(
            creative_ir.from_storyboard(source),
            job_id="J-NODE",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
        ).spec
        production_graph.persist_graph(self.database, spec)

    def tearDown(self):
        self.database.close()

    def test_duplicate_node_event_is_applied_once(self):
        first = node_state_machine.transition_node(
            self.database, "J-NODE", "character_anchor:linxia",
            "start", event_id="event-1",
        )
        second = node_state_machine.transition_node(
            self.database, "J-NODE", "character_anchor:linxia",
            "start", event_id="event-1",
        )
        self.assertTrue(first.applied)
        self.assertFalse(second.applied)
        self.assertEqual(second.state, "RUNNING")

    def test_dependency_completion_promotes_master_to_ready(self):
        anchor = "character_anchor:linxia"
        node_state_machine.transition_node(
            self.database, "J-NODE", anchor, "start", event_id="start-anchor",
        )
        node_state_machine.transition_node(
            self.database, "J-NODE", anchor, "succeed", event_id="finish-anchor",
        )
        state = self.database.execute(
            "SELECT state FROM production_nodes "
            "WHERE job_id='J-NODE' AND node_id='master:1'"
        ).fetchone()[0]
        self.assertEqual(state, "READY")

    def test_invalid_transition_does_not_mutate_node(self):
        with self.assertRaisesRegex(ValueError, "invalid node transition"):
            node_state_machine.transition_node(
                self.database, "J-NODE", "master:1", "succeed",
                event_id="bad-event",
            )
        state = self.database.execute(
            "SELECT state FROM production_nodes "
            "WHERE job_id='J-NODE' AND node_id='master:1'"
        ).fetchone()[0]
        self.assertEqual(state, "PENDING")

    def test_anchor_dependency_resume_is_idempotent_without_completing_master(self):
        self.database.execute("UPDATE production_nodes SET state='REVIEW_REQUIRED' WHERE node_id='master:1'")
        before = dict(self.database.execute(
            "SELECT node_id,state FROM production_nodes WHERE node_id!='master:1'"
        ).fetchall())
        for expected_applied in (True, False):
            result = node_state_machine.transition_node(
                self.database, "J-NODE", "master:1", "resume_anchor_dependency",
                event_id="master:1:replacement-1", payload={"identity_key": "ai:linxia"},
            )
            self.assertEqual(result.applied, expected_applied)
            self.assertEqual(result.state, "READY")
        self.assertEqual(dict(self.database.execute(
            "SELECT node_id,state FROM production_nodes WHERE node_id!='master:1'"
        ).fetchall()), before)
        event = self.database.execute("SELECT * FROM production_node_events").fetchone()
        self.assertEqual((event["from_state"], event["to_state"]), ("REVIEW_REQUIRED", "READY"))
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM production_node_events").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
