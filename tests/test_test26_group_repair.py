"""The incident migration may change future references, never paid evidence."""
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import repair_test26_group_speakers as repair
from factory import db, creative_ir, production_compiler, production_graph, production_adapters


class Test26RepairTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(db.DDL)
        self.database.execute("ALTER TABLE jobs ADD COLUMN characters_json TEXT")
        self.addCleanup(self.database.close)
        characters = [{"id": item, "name": item} for item in [*repair.IDS, repair.GROUP]]
        ir = creative_ir.from_dict({"characters": characters, "beats": [{
            "beat_id": "beat-1", "scene": "shared studio", "speakers": [repair.GROUP],
            "dialogue": "Shared line", "action": "All three speak together",
            "extensions": {"cast": repair.IDS + [repair.GROUP], "duration_sec": 6, "source_master_idx": 1},
        }]})
        old = production_compiler.compile_ir(ir, job_id=repair.JOB, budget_cents=10000,
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            runtime_storyboard={"characters": characters}).spec
        self.assertIsNotNone(old)
        self.database.execute("INSERT INTO jobs(job_id,topic_id,title,state,storyboard,characters_json,creative_ir_json,production_spec_json,production_spec_hash,production_spec_version,execution_engine,cost_cents) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (repair.JOB, "topic", "group", "ANCHOR_REVIEW_REQUIRED", repair.encode(old.runtime_storyboard),
             repair.encode(characters), repair.encode(ir.as_dict()), repair.encode(old.as_dict()), old.spec_hash, 2, "graph_v2", 1088))
        production_graph.persist_graph(self.database, old)
        production_adapters.materialize_frozen_execution_inputs(self.database, old)
        self.database.execute("UPDATE production_nodes SET state='PENDING' WHERE job_id=?", (repair.JOB,))
        self.database.execute("UPDATE production_nodes SET state='REVIEW_REQUIRED',provider_task_id='paid-'||node_id,actual_cost_cents=272 WHERE node_type IN ('character_anchor','scene_anchor')")
        self.database.execute("INSERT INTO script_drafts(draft_id,brief_id,variant_index,request_fingerprint,script_json,storyboard_json,approved_job_id,status) VALUES(?,?,?,?,?,?,?,?)",
            (repair.DRAFT, "brief", 0, "test", repair.encode({"characters": characters[:3], "beats": [{"speaker": repair.GROUP, "dialogue": "Shared line"}]}), repair.encode(old.runtime_storyboard), repair.JOB, "approved"))
        self.database.execute("INSERT INTO production_node_events(job_id,event_id,node_id,event,from_state,to_state) VALUES(?,?,?,?,?,?)",
            (repair.JOB, "paid-obsolete", repair.OBSOLETE, "completed", "RUNNING", "REVIEW_REQUIRED"))
        self.database.commit()
        patcher = patch.object(repair, "OLD_HASH", old.spec_hash)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_success_archives_obsolete_paid_evidence_and_preserves_other_paid_nodes(self):
        before, ir, spec, graph = repair.prepare(self.database)
        self.database.execute("BEGIN IMMEDIATE")
        repair.apply_repair(self.database, before, ir, spec, graph, Path("backup.db"))
        self.database.commit()
        after = repair.snapshot(self.database)
        self.assertEqual(len(json.loads(after["jobs"][0]["characters_json"])), 3)
        for previous in before["production_nodes"]:
            if previous["node_id"] == repair.OBSOLETE or not previous["actual_cost_cents"]:
                continue
            current = next(row for row in after["production_nodes"] if row["node_id"] == previous["node_id"])
            for key in ("provider_task_id", "actual_cost_cents", "idempotency_key", "input_json", "state"):
                self.assertEqual(current[key], previous[key])
        audit = json.loads(self.database.execute("SELECT description FROM production_interventions").fetchone()[0])
        self.assertEqual(audit["archived_node"]["actual_cost_cents"], 272)
        self.assertEqual(len(audit["archived_node_events"]), 1)
        with self.assertRaisesRegex(ValueError, "contract changed"):
            repair.prepare(self.database)

    def test_active_job_is_rejected(self):
        self.database.execute("UPDATE jobs SET state='PRODUCTION_RUNNING'")
        with self.assertRaisesRegex(ValueError, "no longer paused"):
            repair.prepare(self.database)

    def test_submitted_master_is_rejected(self):
        self.database.execute("UPDATE production_nodes SET provider_task_id='remote-master' WHERE node_type='master'")
        with self.assertRaisesRegex(ValueError, "downstream work"):
            repair.prepare(self.database)

    def test_changed_snapshot_is_rejected_before_writes(self):
        before, ir, spec, graph = repair.prepare(self.database)
        self.database.execute("UPDATE jobs SET cost_cents=2000")
        with self.assertRaisesRegex(ValueError, "changed after backup"):
            repair.apply_repair(self.database, before, ir, spec, graph, Path("backup.db"))
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM production_interventions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
