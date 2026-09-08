"""Management-facing Production Spec v2 preflight tests."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import character_assets, db as factory_db, production_config, production_executor  # noqa: E402
from factory.steps import s3_shots  # noqa: E402
from factory.management_api import (  # noqa: E402
    ManagementService,
    _production_stage_projection,
)
from tests.fixtures.production_profiles import load_profile_fixture  # noqa: E402


class ManagementCompilerApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "data").mkdir()
        (self.root / "templates").mkdir()
        (self.root / "templates" / "env.example").write_text("", encoding="utf-8")
        (self.root / ".env").write_text("", encoding="utf-8")
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(factory_db.DDL)
        self.service = ManagementService(self.database, self.root)

    def tearDown(self):
        self.database.close()
        self.tempdir.cleanup()

    def test_complex_content_preflight_explains_adaptation_without_paid_call(self):
        source = load_profile_fixture("experimental_multi_character_action")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) VALUES(?,?,?,?,?)",
            ("J-UI", "T-UI", source["title"], "STORYBOARDED", json.dumps(source)),
        )
        self.database.commit()
        result = self.service.compile_video_job("J-UI")
        self.assertTrue(result["compilable"])
        self.assertEqual(result["mode"], "experimental")
        self.assertEqual(
            set(result["required_content"]), set(result["preserved_content"])
        )
        self.assertTrue(result["adaptation_actions"])
        self.assertTrue(result["nodes"])
        self.assertEqual(
            self.database.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0],
            0,
        )
        graph = self.service.production_graph_detail("J-UI")
        self.assertEqual(len(graph["nodes"]), len(result["nodes"]))

    def test_management_ui_exposes_business_preflight_not_technical_contract(self):
        source = (PROJECT_ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("不会删减必须保留内容", source)
        self.assertIn("系统适配动作", source)
        self.assertIn("/compile", source)
        self.assertIn("productionStageCards", source)
        self.assertIn("production-stage-grid", source)
        self.assertIn("production-technical", source)
        self.assertIn("请先修复生产准备", source)


    def test_worker_command_uses_graph_runner_only_for_explicit_v2_job(self):
        self.database.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state,execution_engine) "
            "VALUES(?,?,?,?,?)",
            (
                ("J-LEGACY", "T-L", "legacy", "PRODUCTION_QUEUED", "legacy"),
                ("J-GRAPH", "T-G", "graph", "PRODUCTION_QUEUED", "graph_v2"),
            ),
        )
        self.database.commit()

        legacy = self.service._production_worker_command("J-LEGACY")
        graph = self.service._production_worker_command("J-GRAPH")

        self.assertIn("produce-approved", legacy)
        self.assertNotIn("produce-graph", legacy)
        self.assertIn("produce-graph", graph)
        self.assertNotIn("produce-approved", graph)

    def test_internal_engine_assignment_is_prepaid_and_storyboard_only(self):
        source = load_profile_fixture("stable_single_dialogue")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,?,?)",
            (
                "J-ASSIGN", "T-ASSIGN", "assign", "STORYBOARDED",
                json.dumps(source),
            ),
        )
        self.database.commit()

        result = self.service.assign_execution_engine(
            "J-ASSIGN", "graph_v2"
        )

        self.assertEqual(result["execution_engine"], "graph_v2")
        self.assertTrue(result["compilable"])
        self.assertEqual(
            self.database.execute(
                "SELECT execution_engine FROM jobs WHERE job_id='J-ASSIGN'"
            ).fetchone()[0],
            "graph_v2",
        )
        self.database.execute(
            "INSERT INTO provider_calls("
            "task_id,job_id,shot_idx,provider,cost_cents) "
            "VALUES('paid-assign','J-ASSIGN',1,'seedance',1)"
        )
        self.database.commit()
        with self.assertRaisesRegex(ValueError, "paid work"):
            self.service.assign_execution_engine("J-ASSIGN", "legacy")

    def test_start_freezes_and_persists_v2_graph_for_explicit_graph_job(self):
        source = load_profile_fixture("stable_single_dialogue")
        source["characters"] = [
            {**character, "generation_mode": "ai_generated"}
            for character in source["characters"]
        ]
        beat = source["beats"][0]
        source["masters"] = [{
            "idx": 1,
            "type": "dialogue",
            "speaker": beat["speaker"],
            "scene": beat["scene"],
            "action": beat["action"],
            "voiceover": beat["dialogue"],
            "duration": source["target_duration_sec"],
            "cast": [beat["speaker"]],
        }]
        source["production_settings"] = {"budget_cents": 10000}
        self.database.execute(
            "INSERT INTO jobs("
            "job_id,topic_id,title,state,storyboard,characters_json,"
            "execution_engine) VALUES(?,?,?,?,?,?,?)",
            (
                "J-START-GRAPH", "T-SG", source["title"], "STORYBOARDED",
                json.dumps(source), json.dumps(source["characters"]),
                "graph_v2",
            ),
        )
        self.database.execute(
            "INSERT INTO shots("
            "job_id,idx,duration_sec,shot_type,camera,prompt,voiceover,"
            "subtitle,state) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "J-START-GRAPH", 1, source["target_duration_sec"],
                "dialogue", "medium", beat["scene"], beat["dialogue"],
                beat["dialogue"], "PENDING",
            ),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,state) VALUES(?,?,?,?)",
            ("J-START-GRAPH", 2, 4, "PENDING"),
        )
        self.database.commit()

        with mock.patch.object(
            self.service, "dispatch_production_queue",
            return_value={"dispatched_jobs": []},
        ):
            result = self.service.start_video_job(
                "J-START-GRAPH", confirmed=True,
            )

        row = self.database.execute(
            "SELECT state,production_spec_version,production_spec_hash "
            "FROM jobs WHERE job_id='J-START-GRAPH'"
        ).fetchone()
        self.assertEqual(result["status"], "queued")
        self.assertEqual(row["state"], "PRODUCTION_QUEUED")
        self.assertEqual(row["production_spec_version"], 2)
        self.assertTrue(row["production_spec_hash"])
        self.assertEqual(
            [item["idx"] for item in self.database.execute(
                "SELECT idx FROM shots WHERE job_id='J-START-GRAPH' ORDER BY idx"
            ).fetchall()],
            [1],
        )
        executable_storyboard = json.loads(self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id='J-START-GRAPH'"
        ).fetchone()[0])
        self.assertEqual(
            [master["idx"] for master in executable_storyboard["masters"]],
            [1],
        )
        self.assertTrue(executable_storyboard["shots"])
        self.assertEqual(
            {
                shot["video_master_idx"]
                for shot in executable_storyboard["shots"]
            },
            {1},
        )
        self.assertGreater(
            self.database.execute(
                "SELECT COUNT(*) FROM production_nodes "
                "WHERE job_id='J-START-GRAPH'"
            ).fetchone()[0],
            0,
        )

    def test_anchor_approval_resumes_graph_dependencies(self):
        self.database.execute(
            "INSERT INTO jobs("
            "job_id,topic_id,title,state,execution_engine) VALUES(?,?,?,?,?)",
            (
                "J-ANCHOR-GRAPH", "T-AG", "anchors",
                "ANCHOR_REVIEW_REQUIRED", "graph_v2",
            ),
        )
        for node_id, node_type in (
            ("character_anchor:linxia", "character_anchor"),
            ("scene_anchor:yard", "scene_anchor"),
        ):
            self.database.execute(
                "INSERT INTO production_nodes("
                "job_id,node_id,node_type,paid,state,spec_hash,input_json,"
                "idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "J-ANCHOR-GRAPH", node_id, node_type, 1,
                    "REVIEW_REQUIRED", "spec-a", "{}", f"idem-{node_id}",
                ),
            )
        self.database.execute(
            "INSERT INTO production_nodes("
            "job_id,node_id,node_type,paid,state,spec_hash,input_json,"
            "idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
            (
                "J-ANCHOR-GRAPH", "master:1", "master", 1, "PENDING",
                "spec-a", "{}", "idem-master-a",
            ),
        )
        self.database.executemany(
            "INSERT INTO production_edges("
            "job_id,from_node_id,to_node_id,edge_type) VALUES(?,?,?,?)",
            (
                (
                    "J-ANCHOR-GRAPH", "character_anchor:linxia", "master:1",
                    "identity_reference",
                ),
                (
                    "J-ANCHOR-GRAPH", "scene_anchor:yard", "master:1",
                    "scene_reference",
                ),
            ),
        )
        # A regenerated anchor may already have an approval event from the
        # previous paid artifact. The new provider task still needs its own
        # approval transition.
        for node_id in ("character_anchor:linxia", "scene_anchor:yard"):
            self.database.execute(
                "UPDATE production_nodes SET provider_task_id=? WHERE job_id=? "
                "AND node_id=?",
                (f"task-new-{node_id}", "J-ANCHOR-GRAPH", node_id),
            )
            self.database.execute(
                "INSERT INTO production_node_events("
                "job_id,event_id,node_id,event,from_state,to_state,payload_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    "J-ANCHOR-GRAPH",
                    f"J-ANCHOR-GRAPH:{node_id}:anchor-approved",
                    node_id, "approve", "REVIEW_REQUIRED", "SUCCEEDED", "{}",
                ),
            )
        self.database.execute(
            "INSERT INTO character_identity_anchors("
            "job_id,identity_key,character_id,display_name,source_type,state,"
            "approval_state) VALUES(?,?,?,?,?,'READY','PENDING_REVIEW')",
            (
                "J-ANCHOR-GRAPH", "ai:linxia", "linxia", "林夏",
                "seedance_identity",
            ),
        )
        self.database.execute(
            "INSERT INTO scene_visual_anchors("
            "job_id,anchor_key,description,master_indexes,state,approval_state) "
            "VALUES(?,?,?,?, 'READY','PENDING_REVIEW')",
            ("J-ANCHOR-GRAPH", "yard", "yard", "[1]"),
        )
        self.database.commit()

        with mock.patch.object(
            self.service, "dispatch_production_queue",
            return_value={"dispatched_jobs": []},
        ):
            self.service.approve_continuity_anchors("J-ANCHOR-GRAPH")

        states = {
            row["node_id"]: row["state"]
            for row in self.database.execute(
                "SELECT node_id,state FROM production_nodes "
                "WHERE job_id='J-ANCHOR-GRAPH'"
            ).fetchall()
        }
        self.assertEqual(states["character_anchor:linxia"], "SUCCEEDED")
        self.assertEqual(states["scene_anchor:yard"], "SUCCEEDED")
        self.assertEqual(states["master:1"], "READY")

    def test_fallback_approval_resumes_only_marked_dependent_master_and_reuses_artifact(self):
        character = {"id": "linxia", "name": "Lin", "generation_mode": "ai_generated", "look": "adult"}
        identity_key = character_assets.character_identity_key(character)
        storyboard = {"characters": [character], "production_settings": {
            "character_anchor_generator": "seedream_5_0",
        }}
        model = production_config.generation_snapshot(storyboard)["model"]
        video_path = self.root / "approved-fallback.mp4"
        # Loader-only sentinel: this test validates reuse, not media decoding.
        video_path.write_bytes(b"existing-provider-artifact")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,execution_engine,storyboard,cost_cents) "
            "VALUES('J-FB-RESUME','T','fallback','PRODUCTION_RUNNING','graph_v2',?,272)",
            (json.dumps(storyboard),),
        )
        for node_id, node_type, state, error_code in (
            ("character_anchor:linxia", "character_anchor", "SUCCEEDED", None),
            ("master:1", "master", "READY", None),
            ("master:2", "master", "REVIEW_REQUIRED", "ordinary_candidate_review"),
            ("master:3", "master", "REVIEW_REQUIRED", "continuity_anchor_review_pending"),
            ("local_compose", "local_compose", "PENDING", None),
        ):
            self.database.execute(
                "INSERT INTO production_nodes(job_id,node_id,node_type,state,error_code,spec_hash,idempotency_key) "
                "VALUES('J-FB-RESUME',?,?,?,?,'spec',?)", (node_id, node_type, state, error_code, node_id),
            )
        self.database.executemany(
            "INSERT INTO production_edges(job_id,from_node_id,to_node_id,edge_type) "
            "VALUES('J-FB-RESUME',?,?,?)",
            [("character_anchor:linxia", "master:1", "identity_reference"),
             ("character_anchor:linxia", "master:2", "identity_reference"),
             ("master:1", "local_compose", "video")],
        )
        self.database.execute(
            "INSERT INTO character_identity_anchors(job_id,identity_key,character_id,display_name,source_type,"
            "state,approval_state,generator,model,video_path,video_url,cost_cents) "
            "VALUES('J-FB-RESUME',?,'linxia','Lin','seedance_identity_fallback','READY','PENDING_REVIEW',"
            "'seedance_2_5',?,?, 'https://example.invalid/fallback-1.mp4',272)",
            (identity_key, model, str(video_path)),
        )
        self.database.commit()

        async def dependency_handler(_node):
            # Real loader/approval gate produces the durable exception marker.
            await s3_shots._ensure_character_identity_anchors(
                "J-FB-RESUME", [character], storyboard=storyboard, existing_only=True,
            )

        with mock.patch.object(s3_shots, "conn", return_value=self.database):
            wave = asyncio.run(production_executor.run_ready_wave(
                self.database, "J-FB-RESUME", configured_limit=1, handlers={"master": dependency_handler},
            ))
        self.assertEqual(wave.review_required, ("master:1",))
        self.assertEqual(self.database.execute(
            "SELECT error_code FROM production_nodes WHERE node_id='master:1'"
        ).fetchone()[0], "continuity_anchor_review_pending")
        self.database.execute("UPDATE jobs SET state='ANCHOR_REVIEW_REQUIRED' WHERE job_id='J-FB-RESUME'")
        self.database.commit()
        with mock.patch.object(self.service, "dispatch_production_queue", return_value={"dispatched_jobs": []}):
            self.service.approve_continuity_anchors("J-FB-RESUME", note="Approve changed identity method")
        states = dict(self.database.execute("SELECT node_id,state FROM production_nodes").fetchall())
        self.assertEqual(states, {"character_anchor:linxia": "SUCCEEDED", "master:1": "READY",
                                 "master:2": "REVIEW_REQUIRED", "master:3": "REVIEW_REQUIRED",
                                 "local_compose": "PENDING"})
        cleared = self.database.execute(
            "SELECT error_code,next_action FROM production_nodes WHERE node_id='master:1'"
        ).fetchone()
        self.assertEqual(tuple(cleared), (None, None))
        anchor = self.database.execute("SELECT * FROM character_identity_anchors").fetchone()
        self.assertEqual(anchor["approval_state"], "APPROVED")
        self.assertTrue(anchor["approved_at"])
        resumed = self.database.execute(
            "SELECT * FROM production_node_events WHERE event='resume_anchor_dependency'"
        ).fetchall()
        self.assertEqual(len(resumed), 1)
        self.assertEqual((resumed[0]["from_state"], resumed[0]["to_state"]), ("REVIEW_REQUIRED", "READY"))
        self.assertIn(identity_key, resumed[0]["payload_json"])

        async def resumed_handler(_node):
            anchors = await s3_shots._ensure_character_identity_anchors(
                "J-FB-RESUME", [character], storyboard=storyboard, existing_only=True,
            )
            s3_shots._assert_continuity_anchors_approved(self.database, "J-FB-RESUME")
            self.assertEqual(anchors[identity_key].video_url, "https://example.invalid/fallback-1.mp4")
            # Stop at the next paid scene boundary: not a generated-scene claim.
            return production_executor.NodeOutcome.require_review({"test_boundary": "scene_submission"})

        with mock.patch.object(s3_shots, "conn", return_value=self.database), mock.patch.object(
            s3_shots, "_generate_seedance_video", new_callable=mock.AsyncMock,
        ) as generate:
            wave = asyncio.run(production_executor.run_ready_wave(
                self.database, "J-FB-RESUME", configured_limit=1, handlers={"master": resumed_handler},
            ))
        self.assertEqual(wave.started, ("master:1",))
        self.assertEqual(wave.review_required, ("master:1",))
        generate.assert_not_awaited()
        self.assertEqual(self.database.execute("SELECT cost_cents FROM jobs").fetchone()[0], 272)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0], 0)
        # A repeated approval of the same already-approved identity must not
        # resume this now-ordinary review or create another dependency event.
        self.database.execute("UPDATE jobs SET state='ANCHOR_REVIEW_REQUIRED'")
        self.database.commit()
        with mock.patch.object(self.service, "dispatch_production_queue", return_value={"dispatched_jobs": []}):
            self.service.approve_continuity_anchors("J-FB-RESUME")
        self.assertEqual(self.database.execute(
            "SELECT state FROM production_nodes WHERE node_id='master:1'"
        ).fetchone()[0], "REVIEW_REQUIRED")
        self.assertEqual(self.database.execute(
            "SELECT COUNT(*) FROM production_node_events WHERE event='resume_anchor_dependency'"
        ).fetchone()[0], 1)
        # A new paid artifact may overwrite the same object URL and local path.
        # Its cumulative billed cost distinguishes it from the prior approval.
        self.database.execute("UPDATE character_identity_anchors SET approval_state='PENDING_REVIEW',"
                              "cost_cents=cost_cents+272")
        self.database.execute("UPDATE production_nodes SET error_code='continuity_anchor_review_pending' "
                              "WHERE node_id='master:1'")
        self.database.execute("UPDATE jobs SET state='ANCHOR_REVIEW_REQUIRED'")
        self.database.commit()
        with mock.patch.object(self.service, "dispatch_production_queue", return_value={"dispatched_jobs": []}):
            self.service.approve_continuity_anchors("J-FB-RESUME")
        self.assertEqual(self.database.execute(
            "SELECT state FROM production_nodes WHERE node_id='master:1'"
        ).fetchone()[0], "READY")
        self.assertEqual(self.database.execute(
            "SELECT COUNT(*) FROM production_node_events WHERE event='resume_anchor_dependency'"
        ).fetchone()[0], 2)

    def test_accepting_master_candidate_completes_graph_node_and_unblocks_next(self):
        job_id = "J-MASTER-REVIEW-GRAPH"
        candidate = (
            self.root / "data" / "assets" / job_id / "shots" / "review"
            / "01-candidate.mp4"
        )
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"accepted-candidate")
        self.database.execute(
            "INSERT INTO jobs("
            "job_id,topic_id,title,state,storyboard,execution_engine) "
            "VALUES(?,?,?,?,?,?)",
            (
                job_id, "T-MRG", "master review graph",
                "PRODUCTION_REVIEW_REQUIRED", "{}", "graph_v2",
            ),
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,state,video_qa,error) "
            "VALUES(?,?,?,?,?)",
            (
                (
                    job_id, 1, "REVIEW_REQUIRED",
                    json.dumps({
                        "status": "manual_review_required",
                        "candidate_path": str(candidate),
                        "candidate_url": "https://cdn.example/accepted.mp4",
                    }),
                    "manual review",
                ),
                (job_id, 2, "PENDING", None, None),
            ),
        )
        self.database.executemany(
            "INSERT INTO production_nodes("
            "job_id,node_id,node_type,paid,state,spec_hash,input_json,"
            "idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
            (
                (
                    job_id, "master:1", "master", 1, "REVIEW_REQUIRED",
                    "spec-review", "{}", "idem-master-review-1",
                ),
                (
                    job_id, "master:2", "master", 1, "PENDING",
                    "spec-review", "{}", "idem-master-review-2",
                ),
            ),
        )
        self.database.execute(
            "INSERT INTO production_edges("
            "job_id,from_node_id,to_node_id,edge_type) VALUES(?,?,?,?)",
            (job_id, "master:1", "master:2", "master_sequence"),
        )
        self.database.commit()

        with mock.patch.object(
            self.service, "dispatch_production_queue",
            return_value={"dispatched_jobs": [job_id]},
        ) as dispatch:
            result = self.service.review_master_candidate(
                job_id, 1, decision="accept_candidate"
            )

        dispatch.assert_called_once_with()
        self.assertEqual(result["state"], "PRODUCTION_QUEUED")
        states = {
            row["node_id"]: row["state"]
            for row in self.database.execute(
                "SELECT node_id,state FROM production_nodes "
                "WHERE job_id=? ORDER BY node_id", (job_id,)
            ).fetchall()
        }
        self.assertEqual(states, {"master:1": "SUCCEEDED", "master:2": "READY"})
        event = self.database.execute(
            "SELECT event,from_state,to_state FROM production_node_events "
            "WHERE job_id=? AND node_id='master:1'", (job_id,)
        ).fetchone()
        self.assertEqual(dict(event), {
            "event": "approve",
            "from_state": "REVIEW_REQUIRED",
            "to_state": "SUCCEEDED",
        })

    def _seed_graph_regeneration(self, *, committed=False):
        job_id = 'J-GRAPH-REGENERATE'
        candidate = self.root / 'data' / 'assets' / job_id / 'shots' / 'review' / '01.mp4'
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b'rejected-candidate')
        qa = {'status': 'manual_review_required', 'candidate_path': str(candidate)}
        if committed:
            qa.update(status='regenerate_committed', review_operation={
                'operation_id': 'committed-redo', 'decision': 'regenerate_master',
                'phase': 'committed',
            })
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,execution_engine,cost_cents) "
            "VALUES(?,?,'redo','PRODUCTION_REVIEW_REQUIRED','{}','graph_v2',944)",
            (job_id, 'T-REDO'),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,cost_cents,video_qa) VALUES(?,1,?,944,?)",
            (job_id, 'PENDING' if committed else 'REVIEW_REQUIRED', json.dumps(qa)),
        )
        self.database.executemany(
            "INSERT INTO production_nodes(job_id,node_id,node_type,paid,state,spec_hash,"
            "input_json,idempotency_key,provider_task_id,actual_cost_cents,output_json) "
            "VALUES(?,?,'master',1,?,'frozen-redo','{}',?,?,?,?)",
            [(job_id, 'master:1', 'REVIEW_REQUIRED', 'old-paid-attempt', 'old-task', 944,
              json.dumps({'candidate_path': str(candidate)})),
             (job_id, 'master:2', 'PENDING', 'downstream', None, 0, '{}')],
        )
        self.database.execute(
            "INSERT INTO production_edges(job_id,from_node_id,to_node_id,edge_type) "
            "VALUES(?,'master:1','master:2','master_sequence')", (job_id,),
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,cost_cents) "
            "VALUES('old-task',?,1,'seedance','master',944)", (job_id,),
        )
        self.database.commit()
        return job_id

    def _assert_graph_regeneration(self, job_id):
        node = self.database.execute(
            "SELECT * FROM production_nodes WHERE job_id=? AND node_id='master:1'", (job_id,),
        ).fetchone()
        self.assertEqual(node['state'], 'READY')
        self.assertIsNone(node['provider_task_id'])
        self.assertEqual(json.loads(node['output_json']), {})
        self.assertNotEqual(node['idempotency_key'], 'old-paid-attempt')
        self.assertEqual(node['actual_cost_cents'], 944)
        self.assertEqual(self.database.execute(
            "SELECT cost_cents FROM provider_calls WHERE task_id='old-task'"
        ).fetchone()[0], 944)
        self.assertEqual(self.database.execute(
            "SELECT state FROM production_nodes WHERE job_id=? AND node_id='master:2'", (job_id,),
        ).fetchone()[0], 'PENDING')
        self.assertEqual(self.database.execute(
            "SELECT COUNT(*) FROM production_node_events WHERE job_id=? AND event='regenerate'", (job_id,),
        ).fetchone()[0], 1)

    def test_regenerate_candidate_requeues_graph_and_keeps_paid_history(self):
        job_id = self._seed_graph_regeneration()
        with mock.patch.object(self.service, 'dispatch_production_queue', return_value={'dispatched_jobs': []}):
            result = self.service.review_master_candidate(job_id, 1, decision='regenerate_master')
        self.assertEqual(result['state'], 'PRODUCTION_QUEUED')
        self._assert_graph_regeneration(job_id)

    def test_startup_syncs_committed_regeneration_before_clearing_operation(self):
        job_id = self._seed_graph_regeneration(committed=True)
        self.service._recover_review_operations()
        self.service._recover_review_operations()
        self._assert_graph_regeneration(job_id)

    def test_resume_reports_unrepaired_graph_failure_without_clearing_state(self):
        job_id = self._seed_graph_regeneration(committed=True)
        self.database.execute("UPDATE jobs SET state='FAILED' WHERE job_id=?", (job_id,))
        self.database.execute(
            "UPDATE production_nodes SET state='FAILED',provider_task_id=NULL,actual_cost_cents=0,"
            "output_json='{}' WHERE job_id=? AND node_id='master:1'", (job_id,),
        )
        self.database.execute(
            "INSERT INTO production_node_events(job_id,event_id,node_id,event,from_state,to_state,payload_json) "
            "VALUES(?,'reference-failed','master:1','fail','RUNNING','FAILED',?)",
            (job_id, json.dumps({'error': 'reference attention budget exceeded'})),
        )
        self.database.commit()
        with self.assertRaisesRegex(ValueError, 'reference attention budget exceeded'):
            self.service.resume_failed_video(job_id)
        self.assertEqual(self.database.execute(
            "SELECT state FROM jobs WHERE job_id=?", (job_id,),
        ).fetchone()[0], 'FAILED')

    def test_committed_regeneration_recovery_does_not_reset_a_new_running_attempt(self):
        job_id = self._seed_graph_regeneration(committed=True)
        self.service._sync_graph_master_review_decision(
            job_id, 1, decision='regenerate_master', operation_id='committed-redo',
        )
        self.database.execute(
            "UPDATE production_nodes SET state='RUNNING',lease_id='new-lease',provider_task_id='new-task' "
            "WHERE job_id=? AND node_id='master:1'", (job_id,),
        )
        self.database.commit()
        self.service._recover_review_operations()
        row = self.database.execute(
            "SELECT state,lease_id,provider_task_id FROM production_nodes WHERE job_id=? AND node_id='master:1'", (job_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ('RUNNING', 'new-lease', 'new-task'))

    def test_startup_repairs_committed_candidate_with_stale_graph_review_state(self):
        job_id = "J-MASTER-REVIEW-RECOVERY"
        active = self.root / "data" / "assets" / job_id / "shots" / "01.mp4"
        active.parent.mkdir(parents=True)
        active.write_bytes(b"already-accepted")
        operation = {
            "operation_id": "accept-before-crash",
            "decision": "accept_candidate",
            "phase": "committed",
            "active_path": str(active),
        }
        self.database.execute(
            "INSERT INTO jobs("
            "job_id,topic_id,title,state,storyboard,execution_engine) "
            "VALUES(?,?,?,?,?,?)",
            (
                job_id, "T-MRR", "review recovery",
                "PRODUCTION_REVIEW_REQUIRED", "{}", "graph_v2",
            ),
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa) "
            "VALUES(?,?,?,?,?)",
            (
                (
                    job_id, 1, "OK", str(active),
                    json.dumps({
                        "status": "candidate_accepted_pending",
                        "human_decision": "accept_candidate",
                        "review_operation": operation,
                    }),
                ),
                (job_id, 2, "PENDING", None, None),
            ),
        )
        self.database.executemany(
            "INSERT INTO production_nodes("
            "job_id,node_id,node_type,paid,state,spec_hash,input_json,"
            "idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
            (
                (
                    job_id, "master:1", "master", 1, "REVIEW_REQUIRED",
                    "spec-recovery", "{}", "idem-master-recovery-1",
                ),
                (
                    job_id, "master:2", "master", 1, "PENDING",
                    "spec-recovery", "{}", "idem-master-recovery-2",
                ),
            ),
        )
        self.database.execute(
            "INSERT INTO production_edges("
            "job_id,from_node_id,to_node_id,edge_type) VALUES(?,?,?,?)",
            (job_id, "master:1", "master:2", "master_sequence"),
        )
        self.database.commit()

        self.service._recover_review_operations()

        job = self.database.execute(
            "SELECT state,error FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        self.assertEqual(dict(job), {"state": "PRODUCTION_QUEUED", "error": None})
        states = {
            row["node_id"]: row["state"]
            for row in self.database.execute(
                "SELECT node_id,state FROM production_nodes WHERE job_id=?",
                (job_id,),
            ).fetchall()
        }
        self.assertEqual(states, {"master:1": "SUCCEEDED", "master:2": "READY"})

    def test_last_graph_candidate_queues_frozen_compose_instead_of_legacy_recompose(self):
        job_id = "J-LAST-GRAPH-REVIEW"
        asset_root = self.root / "data" / "assets" / job_id / "shots"
        candidate = asset_root / "review" / "02-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"accepted-candidate")
        first = asset_root / "01.mp4"
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"first-master")
        stale_storyboard = {
            "production_mode": "native_master_local_director",
            "masters": [
                {"idx": 1, "type": "dialogue", "duration": 6},
                {"idx": 2, "type": "dialogue", "duration": 6},
                {"idx": 3, "type": "broll", "duration": 6},
            ],
            "shots": [{
                "idx": 4, "video_master_idx": 3,
                "audio_master_idx": 2, "duration": 6,
            }],
        }
        frozen_storyboard = {
            "production_mode": "native_master_local_director",
            "masters": [
                {"idx": 1, "type": "dialogue", "duration": 6},
                {"idx": 2, "type": "dialogue", "duration": 6},
            ],
        }
        self.database.execute(
            "INSERT INTO jobs("
            "job_id,topic_id,title,state,storyboard,execution_engine,"
            "production_spec_json,production_spec_hash,production_spec_version) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                job_id, "T-LGR", "last graph review",
                "PRODUCTION_REVIEW_REQUIRED", json.dumps(stale_storyboard),
                "graph_v2", json.dumps({
                    "version": 2, "job_id": job_id,
                    "spec_hash": "spec-last-review",
                    "runtime_storyboard": frozen_storyboard,
                }), "spec-last-review", 2,
            ),
        )
        self.database.executemany(
            "INSERT INTO shots(job_id,idx,state,video_path,video_qa,error) "
            "VALUES(?,?,?,?,?,?)",
            (
                (job_id, 1, "OK", str(first), None, None),
                (
                    job_id, 2, "REVIEW_REQUIRED", None,
                    json.dumps({
                        "status": "manual_review_required",
                        "candidate_path": str(candidate),
                    }),
                    "manual review",
                ),
            ),
        )
        self.database.executemany(
            "INSERT INTO production_nodes("
            "job_id,node_id,node_type,paid,state,spec_hash,input_json,"
            "idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
            (
                (
                    job_id, "master:1", "master", 1, "SUCCEEDED",
                    "spec-last-review", '{"master_idx":1}', "idem-lgr-1",
                ),
                (
                    job_id, "master:2", "master", 1, "REVIEW_REQUIRED",
                    "spec-last-review", '{"master_idx":2}', "idem-lgr-2",
                ),
                (
                    job_id, "local_compose", "local_compose", 0, "PENDING",
                    "spec-last-review", "{}", "idem-lgr-compose",
                ),
            ),
        )
        self.database.execute(
            "INSERT INTO production_edges("
            "job_id,from_node_id,to_node_id,edge_type) VALUES(?,?,?,?)",
            (job_id, "master:2", "local_compose", "master_to_compose"),
        )
        self.database.commit()

        with mock.patch.object(
            self.service, "dispatch_production_queue",
            return_value={"dispatched_jobs": [job_id]},
        ) as dispatch, mock.patch(
            "factory.management_api.s6_compose.run"
        ) as legacy_compose:
            result = self.service.review_master_candidate(
                job_id, 2, decision="accept_candidate"
            )

        legacy_compose.assert_not_called()
        dispatch.assert_called_once_with()
        self.assertEqual(result["state"], "PRODUCTION_QUEUED")

    def test_video_projection_uses_frozen_graph_scene_anchor_set(self):
        storyboard = {
            "production_profile": "standard",
            "masters": [
                {
                    "idx": 1,
                    "scene": "yard",
                    "scene_anchor_key": "scene-a",
                    "site_anchor_key": "shared-site",
                },
                {
                    "idx": 2,
                    "scene": "control room",
                    "scene_anchor_key": "scene-b",
                    "site_anchor_key": "shared-site",
                },
            ],
        }
        production_spec = {
            "version": 2,
            "runtime_storyboard": storyboard,
        }
        self.database.execute(
            "INSERT INTO jobs("
            "job_id,topic_id,title,state,storyboard,execution_engine,"
            "production_spec_json,production_spec_version,production_spec_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "J-SCENE-PROJECTION", "T-SP", "scene projection",
                "PRODUCTION_QUEUED", json.dumps(storyboard), "graph_v2",
                json.dumps(production_spec), 2, "spec-scene-projection",
            ),
        )
        for scene_key in ("scene-a", "scene-b"):
            self.database.execute(
                "INSERT INTO production_nodes("
                "job_id,node_id,node_type,paid,state,spec_hash,input_json,"
                "idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "J-SCENE-PROJECTION", f"scene_anchor:{scene_key}",
                    "scene_anchor", 1, "READY", "spec-scene-projection",
                    json.dumps({"scene_key": scene_key}),
                    f"idem-{scene_key}",
                ),
            )
        self.database.commit()

        video = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-SCENE-PROJECTION"
        )

        self.assertEqual(
            {item["anchor_key"] for item in video["scene_anchor_plan"]},
            {"scene-a", "scene-b"},
        )

    def test_video_projection_uses_compiled_master_plan_for_graph_v2(self):
        source_storyboard = {
            "production_profile": "standard",
            "masters": [
                {"idx": 1, "type": "dialogue", "duration": 10},
                {"idx": 2, "type": "dialogue", "duration": 10},
                {"idx": 3, "type": "broll", "duration": 8},
            ],
            "director_plan": {
                "strategy": "dialogue_plus_broll",
                "dialogue_master_count": 2,
                "broll_master_count": 1,
                "paid_master_count": 3,
            },
        }
        runtime_storyboard = {
            **source_storyboard,
            "masters": source_storyboard["masters"][:2],
            "director_plan": {
                "strategy": "dialogue_plus_broll",
                "dialogue_master_count": 2,
                "broll_master_count": 1,
                "paid_master_count": 3,
            },
        }
        self.database.execute(
            "INSERT INTO jobs("
            "job_id,topic_id,title,state,storyboard,execution_engine,"
            "production_spec_json,production_spec_version,production_spec_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "J-MASTER-PROJECTION", "T-MP", "master projection",
                "PRODUCTION_QUEUED", json.dumps(source_storyboard), "graph_v2",
                json.dumps({"version": 2, "runtime_storyboard": runtime_storyboard}),
                2, "spec-master-projection",
            ),
        )
        self.database.commit()

        video = next(
            item for item in self.service.videos()["jobs"]
            if item["job_id"] == "J-MASTER-PROJECTION"
        )

        self.assertEqual([item["idx"] for item in video["planned_masters"]], [1, 2])
        self.assertEqual(video["director_plan"]["paid_master_count"], 2)
        self.assertEqual(video["director_plan"]["broll_master_count"], 0)

    def test_final_video_review_completes_graph_review_node(self):
        final = self.root / "data" / "graph-final.mp4"
        final.write_bytes(b"video")
        self.database.execute(
            "INSERT INTO jobs("
            "job_id,topic_id,title,state,final_path,execution_engine) "
            "VALUES(?,?,?,?,?,?)",
            (
                "J-FINAL-GRAPH", "T-FG", "graph final", "COMPOSED",
                str(final), "graph_v2",
            ),
        )
        self.database.execute(
            "INSERT INTO production_nodes("
            "job_id,node_id,node_type,paid,state,spec_hash,input_json,"
            "idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
            (
                "J-FINAL-GRAPH", "final_review", "final_review", 0,
                "REVIEW_REQUIRED", "spec-final", "{}", "idem-final-review",
            ),
        )
        self.database.commit()

        result = self.service.review_video(
            "J-FINAL-GRAPH", decision="approved", notes="all checks passed",
            checks={
                key: True for key in (
                    "identity", "lip_sync", "subtitle_sync",
                    "visual_integrity", "clean_frame", "editing_rhythm",
                )
            },
        )

        node = self.database.execute(
            "SELECT state FROM production_nodes "
            "WHERE job_id='J-FINAL-GRAPH' AND node_id='final_review'"
        ).fetchone()
        job = self.database.execute(
            "SELECT state FROM jobs WHERE job_id='J-FINAL-GRAPH'"
        ).fetchone()
        self.assertTrue(result["publish_unlocked"])
        self.assertEqual(node["state"], "SUCCEEDED")
        self.assertEqual(job["state"], "READY_TO_PUBLISH")

    def test_graph_final_review_rejects_failed_or_stale_node_atomically(self):
        checks = {
            key: True for key in (
                "identity", "lip_sync", "subtitle_sync",
                "visual_integrity", "clean_frame", "editing_rhythm",
            )
        }
        for node_state in ("FAILED", "SUCCEEDED"):
            job_id = f"J-FINAL-{node_state}"
            final = self.root / "data" / f"{node_state.lower()}.mp4"
            final.write_bytes(b"video")
            self.database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state,final_path,"
                "execution_engine) VALUES(?,?,?,?,?,'graph_v2')",
                (job_id, "T-F", node_state, "COMPOSED", str(final)),
            )
            self.database.execute(
                "INSERT INTO production_nodes(job_id,node_id,node_type,paid,"
                "state,spec_hash,input_json,idempotency_key) "
                "VALUES(?,'final_review','final_review',0,?,'spec','{}',?)",
                (job_id, node_state, f"idem-{node_state}"),
            )
            self.database.execute(
                "INSERT INTO shots(job_id,idx,state,video_qa) VALUES(?,1,'OK',?)",
                (job_id, json.dumps({
                    "status": "manual_review_required",
                    "reasons": ["qa_error"],
                })),
            )
        self.database.commit()

        for node_state in ("FAILED", "SUCCEEDED"):
            job_id = f"J-FINAL-{node_state}"
            with self.subTest(node_state=node_state), self.assertRaisesRegex(
                ValueError, "final_review"
            ):
                self.service.review_video(
                    job_id, decision="approved", checks=checks,
                )
            self.assertEqual(self.database.execute(
                "SELECT state FROM jobs WHERE job_id=?", (job_id,),
            ).fetchone()[0], "COMPOSED")
            self.assertEqual(self.database.execute(
                "SELECT COUNT(*) FROM review_events WHERE job_id=?", (job_id,),
            ).fetchone()[0], 0)
            self.assertEqual(self.database.execute(
                "SELECT COUNT(*) FROM production_node_events WHERE job_id=?",
                (job_id,),
            ).fetchone()[0], 0)
            qa = json.loads(self.database.execute(
                "SELECT video_qa FROM shots WHERE job_id=? AND idx=1",
                (job_id,),
            ).fetchone()[0])
            self.assertEqual(qa["status"], "manual_review_required")
            self.assertNotIn("human_decision", qa)

    def test_opposite_graph_review_decisions_are_serialized_to_one_commit(self):
        database_path = self.root / "review-race.db"
        final = self.root / "data" / "review-race.mp4"
        final.write_bytes(b"video")
        database = sqlite3.connect(database_path)
        database.row_factory = sqlite3.Row
        database.executescript(factory_db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,final_path,"
            "execution_engine) VALUES(?,?,?,?,?,'graph_v2')",
            ("J-REVIEW-RACE", "T-R", "race", "COMPOSED", str(final)),
        )
        database.execute(
            "INSERT INTO production_nodes(job_id,node_id,node_type,paid,state,"
            "spec_hash,input_json,idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
            ("J-REVIEW-RACE", "final_review", "final_review", 0,
             "REVIEW_REQUIRED", "spec", "{}", "idem-review-race"),
        )
        database.commit()
        database.close()
        barrier = threading.Barrier(2)
        checks = {
            key: True for key in (
                "identity", "lip_sync", "subtitle_sync",
                "visual_integrity", "clean_frame", "editing_rhythm",
            )
        }

        contenders = []
        for decision in ("approved", "local_recut"):
            connection = sqlite3.connect(
                database_path, timeout=30, check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            # Schema setup is intentionally outside the race. Only the review
            # transaction itself is concurrent.
            contenders.append((
                ManagementService(connection, self.root), connection, decision,
            ))

        def decide(service, decision):
            barrier.wait(timeout=10)
            return service.review_video(
                "J-REVIEW-RACE", decision=decision,
                checks=checks, notes=("recut" if decision != "approved" else ""),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(decide, service, decision)
                       for service, _connection, decision in contenders]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=30))
                except ValueError as exc:
                    outcomes.append(exc)
        for _service, connection, _decision in contenders:
            connection.close()

        self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, ValueError) for item in outcomes), 1)
        verify = sqlite3.connect(database_path)
        verify.row_factory = sqlite3.Row
        try:
            self.assertEqual(verify.execute(
                "SELECT COUNT(*) FROM review_events WHERE job_id='J-REVIEW-RACE'"
            ).fetchone()[0], 1)
            self.assertEqual(verify.execute(
                "SELECT COUNT(*) FROM production_node_events "
                "WHERE job_id='J-REVIEW-RACE'"
            ).fetchone()[0], 1)
            node_state = verify.execute(
                "SELECT state FROM production_nodes WHERE job_id='J-REVIEW-RACE' "
                "AND node_id='final_review'"
            ).fetchone()[0]
            job_state = verify.execute(
                "SELECT state FROM jobs WHERE job_id='J-REVIEW-RACE'"
            ).fetchone()[0]
            self.assertIn((node_state, job_state), {
                ("SUCCEEDED", "READY_TO_PUBLISH"),
                ("FAILED", "FAILED"),
            })
            committed = next(item for item in outcomes if isinstance(item, dict))
            self.assertEqual(
                committed["publish_unlocked"],
                job_state == "READY_TO_PUBLISH",
            )
        finally:
            verify.close()

    def test_video_detail_projects_graph_into_five_business_stages(self):
        self.database.execute(
            "INSERT INTO jobs("
            "job_id,topic_id,title,state,storyboard,execution_engine) "
            "VALUES(?,?,?,?,?,?)",
            (
                "J-STAGES", "T-STAGES", "stage projection",
                "PRODUCTION_RUNNING", json.dumps({"characters": []}),
                "graph_v2",
            ),
        )
        nodes = (
            ("character_anchor:a", "character_anchor", "SUCCEEDED"),
            ("scene_anchor:yard", "scene_anchor", "SUCCEEDED"),
            ("master:1", "master", "RUNNING"),
            ("local_compose", "local_compose", "PENDING"),
            ("final_review", "final_review", "PENDING"),
        )
        for node_id, node_type, state in nodes:
            self.database.execute(
                "INSERT INTO production_nodes("
                "job_id,node_id,node_type,paid,state,spec_hash,input_json,"
                "idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "J-STAGES", node_id, node_type,
                    int(node_type in {"character_anchor", "scene_anchor", "master"}),
                    state, "spec-stages", "{}", f"idem-{node_id}",
                ),
            )
        self.database.commit()

        job = self.service.videos()["jobs"][0]

        self.assertEqual(job["execution_engine"], "graph_v2")
        self.assertEqual(
            [stage["id"] for stage in job["production_stages"]],
            ["character", "scene", "master", "compose", "review"],
        )
        self.assertEqual(job["production_stages"][2]["state"], "RUNNING")
        self.assertEqual(len(job["production_graph_nodes"]), 5)

    def test_business_stage_surfaces_failure_before_ready_work(self):
        stages = _production_stage_projection([
            {"node_type": "master", "state": "FAILED"},
            {"node_type": "master", "state": "READY"},
        ])

        master = next(stage for stage in stages if stage["id"] == "master")
        self.assertEqual(master["state"], "FAILED")

    def test_overview_exposes_graph_v2_rollout_gate(self):
        rollout = {
            "total_real_jobs": 2,
            "switch_gate": {
                "eligible": False,
                "failed": ["total_real_jobs"],
            },
        }
        with mock.patch(
            "factory.management_api.production_metrics.production_v2_summary",
            return_value=rollout,
        ):
            result = self.service.overview()

        self.assertEqual(
            result["reliability"]["production_compiler_v2"], rollout
        )


if __name__ == "__main__":
    unittest.main()
