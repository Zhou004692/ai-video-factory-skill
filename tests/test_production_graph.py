"""Persistent production graph contract tests."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import creative_ir, db as factory_db, production_compiler, production_graph  # noqa: E402
from tests.fixtures.production_profiles import load_profile_fixture  # noqa: E402


class ProductionGraphTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(factory_db.DDL)
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) VALUES(?,?,?,?)",
            ("J-GRAPH", "T-GRAPH", "生产图", "STORYBOARDED"),
        )
        self.database.commit()

    def tearDown(self):
        self.database.close()

    def spec(self):
        source = load_profile_fixture("enhanced_two_person_continuity")
        return production_compiler.compile_ir(
            creative_ir.from_storyboard(source),
            job_id="J-GRAPH",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
        ).spec

    def test_every_paid_master_depends_on_required_anchors(self):
        graph = production_graph.compile_graph(self.spec())
        master = graph.node("master:1")
        ancestors = graph.ancestors(master.node_id)
        self.assertIn("character_anchor:linxia", ancestors)
        self.assertIn("scene_anchor:equipment_yard", ancestors)

    def test_persist_is_atomic_and_idempotent_for_same_spec(self):
        spec = self.spec()
        first = production_graph.persist_graph(self.database, spec)
        second = production_graph.persist_graph(self.database, spec)
        self.assertEqual(first.spec_hash, second.spec_hash)
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM production_nodes WHERE job_id='J-GRAPH'"
            ).fetchone()[0],
            len(spec.nodes),
        )
        ready = {
            row["node_id"] for row in self.database.execute(
                "SELECT node_id FROM production_nodes "
                "WHERE job_id='J-GRAPH' AND state='READY'"
            )
        }
        self.assertIn("character_anchor:linxia", ready)

    def test_equivalent_spec_revision_rebinds_paid_graph_without_reset(self):
        spec = self.spec()
        production_graph.persist_graph(self.database, spec)
        self.database.execute(
            "UPDATE production_nodes SET actual_cost_cents=10,state='SUCCEEDED' "
            "WHERE job_id='J-GRAPH' AND node_type='master'"
        )
        self.database.commit()
        production_graph.persist_graph(
            self.database, replace(spec, spec_hash="different"),
        )
        rows = self.database.execute(
            "SELECT state,actual_cost_cents,spec_hash FROM production_nodes "
            "WHERE job_id='J-GRAPH' AND node_type='master'"
        ).fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(row["state"] == "SUCCEEDED" for row in rows))
        self.assertTrue(all(row["actual_cost_cents"] == 10 for row in rows))
        self.assertTrue(all(row["spec_hash"] == "different" for row in rows))

    def test_materially_different_spec_cannot_delete_paid_graph(self):
        spec = self.spec()
        production_graph.persist_graph(self.database, spec)
        self.database.execute(
            "UPDATE production_nodes SET actual_cost_cents=10,state='SUCCEEDED' "
            "WHERE job_id='J-GRAPH' AND node_type='master'"
        )
        self.database.commit()
        changed_node = replace(
            spec.nodes[0], input={**spec.nodes[0].input, "character_id": "other"}
        )
        changed = replace(
            spec, spec_hash="materially-different",
            nodes=(changed_node, *spec.nodes[1:]),
        )
        with self.assertRaisesRegex(ValueError, "paid production graph"):
            production_graph.persist_graph(self.database, changed)

    def test_cycle_and_unknown_edge_are_rejected(self):
        spec = self.spec()
        edge_type = type(spec.edges[0])
        cyclic = replace(spec, edges=spec.edges + (
            edge_type("local_compose", "master:1", "invalid_cycle"),
        ))
        with self.assertRaisesRegex(ValueError, "cycle"):
            production_graph.compile_graph(cyclic)
        unknown = replace(spec, edges=spec.edges + (
            edge_type("missing", "master:1", "invalid"),
        ))
        with self.assertRaisesRegex(ValueError, "unknown node"):
            production_graph.compile_graph(unknown)


if __name__ == "__main__":
    unittest.main()
