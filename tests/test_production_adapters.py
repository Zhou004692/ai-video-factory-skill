"""Frozen-input adapters over the proven legacy production steps."""
from __future__ import annotations

import json
import inspect
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest import mock
from click.testing import CliRunner


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import (  # noqa: E402
    db as factory_db, production_adapters, production_invariants,
)
from factory import creative_ir, production_compiler  # noqa: E402
from factory.steps import s3_shots  # noqa: E402
from scripts import cli as cli_module  # noqa: E402


class ProductionAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(factory_db.DDL)
        self.storyboard = {
            "title": "frozen",
            "characters": [
                {"id": "linxia", "name": "林夏", "generation_mode": "ai_generated"}
            ],
            "masters": [
                {
                    "idx": 1, "type": "dialogue", "speaker": "linxia",
                    "scene_anchor_key": "yard", "scene": "设备堆场",
                    "duration": 8,
                }
            ],
            "production_settings": {"budget_cents": 10000},
        }
        spec = {
            "version": 2,
            "job_id": "J-ADAPTER",
            "spec_hash": "spec-adapter",
            "characters": self.storyboard["characters"],
            "runtime_storyboard": self.storyboard,
        }
        self.database.execute(
            "INSERT INTO jobs("
            "job_id,topic_id,title,state,storyboard,"
            "production_spec_json,production_spec_hash,production_spec_version,"
            "execution_engine) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "J-ADAPTER", "T-A", "adapter", "PRODUCTION_RUNNING",
                json.dumps({"title": "mutated"}),
                json.dumps(spec), "spec-adapter", 2, "graph_v2",
            ),
        )
        self.database.execute(
            "INSERT INTO production_nodes("
            "job_id,node_id,node_type,paid,state,spec_hash,input_json,"
            "idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
            (
                "J-ADAPTER", "master:1", "master", 1, "RUNNING",
                "spec-adapter", json.dumps({"master_idx": 1}), "idem-master",
            ),
        )
        self.database.commit()

    def tearDown(self):
        self.database.close()

    async def test_master_adapter_uses_frozen_spec_not_mutable_job_storyboard(self):
        row = self.database.execute(
            "SELECT * FROM production_nodes WHERE job_id='J-ADAPTER' "
            "AND node_id='master:1'"
        ).fetchone()
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_path) VALUES(?,?,?,?)",
            ("J-ADAPTER", 1, "OK", "master-1.mp4"),
        )
        self.database.commit()
        with mock.patch(
            "factory.production_adapters.s3_shots.run",
            new=mock.AsyncMock(),
        ) as run:
            output = await production_adapters.run_master_node(
                self.database, row,
            )

        run.assert_awaited_once_with(
            "J-ADAPTER",
            storyboard_override=self.storyboard,
            characters_override=self.storyboard["characters"],
            target_master_indexes={1},
            prepare_anchors=False,
        )
        self.assertEqual(output["master_idx"], 1)

    async def test_master_adapter_projects_paid_candidate_to_review(self):
        row = self.database.execute(
            "SELECT * FROM production_nodes WHERE job_id='J-ADAPTER' "
            "AND node_id='master:1'"
        ).fetchone()
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state,video_qa) VALUES(?,?,?,?)",
            (
                "J-ADAPTER", 1, "REVIEW_REQUIRED",
                json.dumps({"candidate_path": "candidate.mp4"}),
            ),
        )
        self.database.commit()

        with mock.patch(
            "factory.production_adapters.s3_shots.run",
            new=mock.AsyncMock(),
        ):
            outcome = await production_adapters.run_master_node(
                self.database, row,
            )

        self.assertEqual(outcome.event, "require_review")
        self.assertEqual(outcome.output["candidate_path"], "candidate.mp4")

    async def test_local_compose_adapter_uses_frozen_director_timeline(self):
        frozen = {
            **self.storyboard,
            "production_mode": "native_master_local_director",
            "masters": [{
                **self.storyboard["masters"][0],
                "performance_beats": [{
                    "beat": "hook", "source_beat_id": "beat-1",
                    "duration": 8, "dialogue": "ready",
                    "visual_intent": "walk into the yard",
                }],
            }],
        }
        spec = json.loads(self.database.execute(
            "SELECT production_spec_json FROM jobs WHERE job_id='J-ADAPTER'"
        ).fetchone()[0])
        spec["runtime_storyboard"] = frozen
        self.database.execute(
            "UPDATE jobs SET production_spec_json=? WHERE job_id='J-ADAPTER'",
            (json.dumps(spec),),
        )
        self.database.execute(
            "INSERT INTO production_nodes("
            "job_id,node_id,node_type,paid,state,spec_hash,input_json,"
            "idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
            (
                "J-ADAPTER", "local_compose", "local_compose", 0,
                "RUNNING", "spec-adapter", "{}", "idem-compose",
            ),
        )
        self.database.commit()
        row = self.database.execute(
            "SELECT * FROM production_nodes WHERE job_id='J-ADAPTER' "
            "AND node_id='local_compose'"
        ).fetchone()

        with mock.patch(
            "factory.production_adapters.s6_compose.run",
            new=mock.AsyncMock(),
        ) as compose:
            with mock.patch.object(
                production_adapters,
                "_job_final_path",
                return_value="final.mp4",
                create=True,
            ):
                self.database.execute(
                    "UPDATE jobs SET final_path='final.mp4' "
                    "WHERE job_id='J-ADAPTER'"
                )
                output = await production_adapters.run_local_compose_node(
                    self.database, row,
                )

        compose.assert_awaited_once()
        composed_storyboard = compose.await_args.kwargs["storyboard_override"]
        self.assertEqual(len(composed_storyboard["shots"]), 1)
        self.assertEqual(composed_storyboard["shots"][0]["video_master_idx"], 1)
        self.assertEqual(output["final_path"], "final.mp4")

    async def test_adapter_rejects_node_whose_hash_differs_from_frozen_job(self):
        self.database.execute(
            "UPDATE production_nodes SET spec_hash='wrong' "
            "WHERE job_id='J-ADAPTER' AND node_id='master:1'"
        )
        self.database.commit()
        row = self.database.execute(
            "SELECT * FROM production_nodes WHERE job_id='J-ADAPTER' "
            "AND node_id='master:1'"
        ).fetchone()

        with self.assertRaises(production_adapters.FrozenSpecMismatchError):
            await production_adapters.run_master_node(self.database, row)

    async def test_generated_character_anchor_pauses_for_human_review(self):
        self.database.execute(
            "INSERT INTO production_nodes("
            "job_id,node_id,node_type,paid,state,spec_hash,input_json,"
            "idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
            (
                "J-ADAPTER", "character_anchor:linxia", "character_anchor",
                1, "RUNNING", "spec-adapter",
                json.dumps({"character_id": "linxia"}), "idem-character",
            ),
        )
        self.database.execute(
            "INSERT INTO character_identity_anchors("
            "job_id,identity_key,character_id,display_name,source_type,state,"
            "approval_state) VALUES(?,?,?,?,?,?,?)",
            (
                "J-ADAPTER", "ai:linxia", "linxia", "林夏",
                "seedance_identity", "READY", "PENDING_REVIEW",
            ),
        )
        self.database.commit()
        row = self.database.execute(
            "SELECT * FROM production_nodes WHERE job_id='J-ADAPTER' "
            "AND node_id='character_anchor:linxia'"
        ).fetchone()

        with mock.patch(
            "factory.production_adapters.s3_shots._ensure_character_identity_anchors",
            new=mock.AsyncMock(return_value={}),
        ) as ensure:
            outcome = await production_adapters.run_character_anchor_node(
                self.database, row,
            )

        self.assertEqual(outcome.event, "require_review")
        ensure.assert_awaited_once_with(
            "J-ADAPTER",
            self.storyboard["characters"],
            storyboard=self.storyboard,
        )

    def test_handler_registry_covers_every_compiled_node_type(self):
        handlers = production_adapters.build_handlers(self.database)
        self.assertEqual(
            set(handlers),
            {
                "character_anchor", "scene_anchor", "master", "bgm",
                "local_compose", "final_review",
            },
        )

    def test_s3_step_accepts_frozen_subset_contract(self):
        parameters = inspect.signature(s3_shots.run).parameters
        self.assertIn("storyboard_override", parameters)
        self.assertIn("characters_override", parameters)
        self.assertIn("target_master_indexes", parameters)
        self.assertIn("prepare_anchors", parameters)

    def test_cli_exposes_internal_graph_worker_command(self):
        result = CliRunner().invoke(cli_module.cli, ["produce-graph", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("JOB_ID", result.output)

    def test_graph_cli_persists_safe_contract_failure_before_exit(self):
        report = production_invariants.InvariantReport(
            "J-ADAPTER",
            (
                production_invariants.InvariantIssue(
                    "materialized_master_mismatch", "blocking", "master:99",
                    "private runtime detail",
                ),
            ),
        )
        error = production_invariants.ProductionInvariantError(report)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FACTORY_REQUIRE_V2_CONTRACT", None)
            with (
                mock.patch.object(cli_module, "conn", return_value=self.database),
                mock.patch.object(
                    cli_module.production_graph_runner,
                    "run_graph_job",
                    new=mock.AsyncMock(side_effect=error),
                ),
            ):
                result = CliRunner().invoke(
                    cli_module.cli,
                    ["produce-graph", "J-ADAPTER", "--dispatched"],
                )
            self.assertNotIn("FACTORY_REQUIRE_V2_CONTRACT", os.environ)

        self.assertNotEqual(result.exit_code, 0)
        row = self.database.execute(
            "SELECT state,error,failure_code,production_worker_pid "
            "FROM jobs WHERE job_id='J-ADAPTER'"
        ).fetchone()
        self.assertEqual(row["state"], "FAILED")
        self.assertEqual(row["failure_code"], "execution_contract_invalid")
        self.assertNotIn("master:99", str(row["error"]))
        self.assertIsNone(row["production_worker_pid"])

    def test_graph_start_rejects_unmaterialized_compiled_masters(self):
        creative = creative_ir.from_storyboard({
            "title": "not materialized",
            "characters": [{"id": "linxia"}],
            "beats": [{
                "id": "beat-1", "scene": "yard", "speaker": "linxia",
                "dialogue": "ready",
            }],
        })
        spec = production_compiler.compile_ir(
            creative,
            job_id="J-ADAPTER",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
            runtime_storyboard={"title": "not materialized", "masters": []},
        ).spec

        with self.assertRaises(production_adapters.FrozenSpecMismatchError):
            production_adapters.validate_frozen_execution_inputs(
                self.database, spec,
            )

    def test_unpaid_legacy_shots_can_align_after_paid_anchor_work(self):
        source = {
            "title": "compiled runtime",
            "characters": [{"id": "linxia"}],
            "beats": [
                {
                    "id": "beat-1", "scene_id": "room-a",
                    "scene": "room a", "speaker": "linxia",
                    "dialogue": "first line", "duration_sec": 8,
                },
                {
                    "id": "beat-2", "scene_id": "room-a",
                    "scene": "room a", "speaker": "linxia",
                    "dialogue": "second line", "duration_sec": 8,
                },
            ],
        }
        spec = production_compiler.compile_ir(
            creative_ir.from_storyboard(source),
            job_id="J-ADAPTER",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
            runtime_storyboard=source,
        ).spec
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state) VALUES(?,?,?)",
            ("J-ADAPTER", 1, "PENDING"),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state) VALUES(?,?,?)",
            ("J-ADAPTER", 2, "PENDING"),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state) VALUES(?,?,?)",
            ("J-ADAPTER", 3, "PENDING"),
        )
        self.database.execute(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,cost_cents) "
            "VALUES(?,?,?,?,?)",
            ("anchor-task", "J-ADAPTER", -2001, "seedance_scene", 272),
        )
        self.database.execute(
            "UPDATE jobs SET cost_cents=272 WHERE job_id='J-ADAPTER'"
        )
        self.database.commit()

        self.assertTrue(hasattr(
            production_adapters, "materialize_frozen_execution_inputs",
        ))
        production_adapters.materialize_frozen_execution_inputs(
            self.database, spec,
        )

        rows = self.database.execute(
            "SELECT idx,voiceover,state FROM shots WHERE job_id=? ORDER BY idx",
            ("J-ADAPTER",),
        ).fetchall()
        self.assertEqual([row["idx"] for row in rows], [1])
        self.assertEqual(rows[0]["voiceover"], "first linesecond line")
        self.assertTrue(all(row["state"] == "PENDING" for row in rows))
        production_adapters.validate_frozen_execution_inputs(
            self.database, spec,
        )

    def test_paid_shot_rows_are_never_rewritten_to_a_new_compiled_shape(self):
        source = {
            "title": "paid protection",
            "characters": [{"id": "linxia"}],
            "beats": [{
                "id": "beat-1", "scene_id": "room-a",
                "scene": "room a", "speaker": "linxia",
                "dialogue": "one compiled master", "duration_sec": 8,
            }],
        }
        spec = production_compiler.compile_ir(
            creative_ir.from_storyboard(source),
            job_id="J-ADAPTER",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000,
            runtime_storyboard=source,
        ).spec
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state) VALUES(?,?,?)",
            ("J-ADAPTER", 1, "OK"),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,state) VALUES(?,?,?)",
            ("J-ADAPTER", 2, "PENDING"),
        )
        self.database.commit()

        with self.assertRaises(production_adapters.FrozenSpecMismatchError):
            production_adapters.materialize_frozen_execution_inputs(
                self.database, spec,
            )

        indexes = [
            row["idx"] for row in self.database.execute(
                "SELECT idx FROM shots WHERE job_id=? ORDER BY idx",
                ("J-ADAPTER",),
            ).fetchall()
        ]
        self.assertEqual(indexes, [1, 2])


if __name__ == "__main__":
    unittest.main()
