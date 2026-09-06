"""Frozen graph -> real S3 preflight/request evidence, not a paid/media E2E.

Only local infrastructure paths and the final provider boundary are replaced.
Character approval is an explicit test fixture; scene assets are never invented
for a stable graph that did not schedule them.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import (  # noqa: E402
    character_assets, creative_ir, db, production_adapters,
    production_compiler, production_config, production_graph, scene_assets,
)
from factory.steps import s3_shots  # noqa: E402


class FrozenSceneDependencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="frozen-scene-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

        def tracked_connection():
            database = db.conn()
            self.addCleanup(database.close)
            return database

        for patcher in (
            mock.patch.object(db, "database_path", return_value=self.root / "factory.db"),
            mock.patch.object(s3_shots, "conn", side_effect=tracked_connection),
            mock.patch.object(s3_shots, "ASSETS_DIR", self.root / "assets"),
            mock.patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.anchor_path = self.root / "approved-character.png"
        self.anchor_path.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jz1kAAAAASUVORK5CYII="
        ))
        self.database = tracked_connection()

    def prepare(self, *, approval="APPROVED", extra_beats=()):
        character = {"id": "worker", "name": "Worker", "generation_mode": "ai_generated"}
        source = {
            "characters": [character],
            "production_settings": {
                "character_anchor_generator": "seedream_5_0",
                "scene_anchor_generator": "seedream_5_0",
                "generation_snapshot": production_config.freeze_generation_snapshot(
                    {"audio_mode": "seedance_native", "qa_enabled": True}, env={},
                ),
            },
            "beats": [{
                "id": "yard-1", "scene": "yard", "duration_sec": 8,
                "cast": ["worker"], "speaker": "customer_voice",
                "speech_mode": "off_screen", "dialogue": "Ready?",
                "action": "worker checks machine", "tone": "curious",
            }, *extra_beats],
        }
        result = production_compiler.compile_ir(
            creative_ir.from_storyboard(source), job_id="J-frozen-scene",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000, runtime_storyboard=source,
        )
        self.assertTrue(result.compilable, result.issues)
        spec = result.spec
        self.payload = json.loads(json.dumps(spec.as_dict()))
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,production_spec_json,"
            "production_spec_hash,production_spec_version,execution_engine) "
            "VALUES(?,?,?,'PRODUCTION_RUNNING',?,?,?,2,'graph_v2')",
            (spec.job_id, "T-scene", "Frozen scene preflight",
             json.dumps(spec.runtime_storyboard), json.dumps(self.payload), spec.spec_hash),
        )
        production_adapters.materialize_frozen_execution_inputs(self.database, spec)
        production_graph.persist_graph(self.database, spec)
        self.database.execute(
            "INSERT INTO character_identity_anchors(job_id,identity_key,character_id,"
            "display_name,source_type,state,anchor_mode,image_path,image_url,"
            "generator,approval_state) VALUES(?,?,?,?,?,'READY','static_image',?,?,?,?)",
            (spec.job_id, character_assets.character_identity_key(character), "worker",
             "Worker", "seedream_identity_image", str(self.anchor_path),
             "https://example.invalid/worker.png", "seedream_5_0", approval),
        )
        self.database.commit()
        self.node = self.database.execute(
            "SELECT * FROM production_nodes WHERE job_id=? AND node_id='master:1'",
            (spec.job_id,),
        ).fetchone()
        return spec

    async def request_at_boundary(self):
        with mock.patch.object(
            s3_shots, "_generate_seedance_video",
            new=mock.AsyncMock(side_effect=RuntimeError("LOCAL_PROVIDER_BOUNDARY")),
        ) as generate:
            with self.assertRaisesRegex(RuntimeError, "LOCAL_PROVIDER_BOUNDARY"):
                await production_adapters.run_master_node(self.database, self.node)
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(self.database.execute("SELECT count(*) FROM provider_calls").fetchone()[0], 0)
        return generate.await_args.kwargs

    def save_payload(self):
        self.database.execute(
            "UPDATE jobs SET production_spec_json=? WHERE job_id=?",
            (json.dumps(self.payload), self.node["job_id"]),
        )
        self.database.commit()

    def add_scene_dependency(self, key):
        self.payload["nodes"].append({
            "node_id": f"scene_anchor:{key}", "node_type": "scene_anchor",
            "input": {"scene_key": key},
        })
        self.payload["edges"].append({
            "from_node_id": f"scene_anchor:{key}", "to_node_id": "master:1",
            "edge_type": "scene_reference",
        })
        self.save_payload()

    def add_scene_asset(self, key, *, approval="APPROVED", generator="seedream_5_0"):
        self.database.execute(
            "INSERT INTO scene_visual_anchors(job_id,anchor_key,state,image_path,"
            "image_url,generator,approval_state) VALUES(?,?,'READY',?,?,?,?)",
            (self.node["job_id"], key, str(self.anchor_path),
             f"https://example.invalid/{key}.png", generator, approval),
        )
        self.database.commit()

    async def assert_blocked(self, error_type, message):
        with mock.patch.object(s3_shots, "_generate_seedance_video", new=mock.AsyncMock()) as generate:
            with self.assertRaisesRegex(error_type, message):
                await production_adapters.run_master_node(self.database, self.node)
        generate.assert_not_awaited()

    async def test_stable_single_scene_identity_without_graph_dependency_reaches_request(self):
        spec = self.prepare()
        self.assertEqual(spec.mode, "stable")
        self.assertFalse(any(node.node_type == "scene_anchor" for node in spec.nodes))
        self.assertEqual(len(spec.masters), 1)
        scene_key = spec.runtime_storyboard["masters"][0]["scene_anchor_key"]
        self.assertTrue(scene_key.startswith("scene:"))
        request = await self.request_at_boundary()
        self.assertEqual(request["request_kwargs"]["references"], ["https://example.invalid/worker.png"])
        self.assertEqual(request["expected_text"], "Ready?")
        self.assertFalse(request["forbid_people"])
        self.assertFalse(request["require_lip_sync"])
        self.assertTrue(request["route"].generate_audio)
        self.assertTrue(request["route"].qa_required)
        self.assertIn("customer_voice", request["prompt"])
        self.assertIn("curious", request["prompt"])
        self.assertEqual(self.payload["runtime_storyboard"]["masters"][0]["scene_anchor_key"], scene_key)
        self.assertEqual(self.database.execute("SELECT count(*) FROM scene_visual_anchors").fetchone()[0], 0)

    async def test_empty_scene_dependencies_keep_character_review_required(self):
        self.prepare(approval="PENDING_REVIEW")
        await self.assert_blocked(s3_shots.ContinuityAnchorReviewPendingError, ".*")

    async def test_explicit_scene_dependency_missing_asset_blocks(self):
        self.prepare()
        key = self.payload["runtime_storyboard"]["masters"][0]["scene_anchor_key"]
        self.add_scene_dependency(key)
        await self.assert_blocked(RuntimeError, "frozen scene anchor is not ready")

    async def test_explicit_scene_dependency_wrong_generator_blocks(self):
        self.prepare()
        key = self.payload["runtime_storyboard"]["masters"][0]["scene_anchor_key"]
        self.add_scene_dependency(key)
        self.add_scene_asset(key, generator="seedance_2_5")
        await self.assert_blocked(RuntimeError, "frozen scene anchor is not ready")

    async def test_explicit_scene_dependency_pending_review_blocks(self):
        self.prepare()
        key = self.payload["runtime_storyboard"]["masters"][0]["scene_anchor_key"]
        self.add_scene_dependency(key)
        self.add_scene_asset(key, approval="PENDING_REVIEW")
        await self.assert_blocked(s3_shots.ContinuityAnchorReviewPendingError, ".*")

    async def test_approved_scene_dependency_is_in_request(self):
        self.prepare()
        key = self.payload["runtime_storyboard"]["masters"][0]["scene_anchor_key"]
        self.add_scene_dependency(key)
        self.add_scene_asset(key)
        request = await self.request_at_boundary()
        self.assertEqual(request["request_kwargs"]["references"][0], f"https://example.invalid/{key}.png")

    async def test_current_empty_dependencies_do_not_pull_other_masters_recurring_scene(self):
        self.prepare()
        # Add two other masters sharing a scene; only master:1 is dispatched.
        self.payload["nodes"].append({"node_id": "scene_anchor:recurring", "node_type": "scene_anchor",
                                       "input": {"scene_key": "recurring"}})
        for index in (2, 3):
            self.payload["runtime_storyboard"]["masters"].append({
                "idx": index, "scene_anchor_key": "recurring", "scene": "room",
            })
            self.payload["nodes"].append({"node_id": f"master:{index}", "node_type": "master",
                                           "input": {"master_idx": index}})
            self.payload["edges"].append({"from_node_id": "scene_anchor:recurring",
                                           "to_node_id": f"master:{index}", "edge_type": "scene_reference"})
        self.save_payload()
        self.assertTrue(scene_assets.recurring_scene_anchors(self.payload["runtime_storyboard"]))
        request = await self.request_at_boundary()
        self.assertEqual(request["request_kwargs"]["references"], ["https://example.invalid/worker.png"])

    async def test_site_only_dependency_loads_site_without_exact_scene(self):
        self.prepare()
        master = self.payload["runtime_storyboard"]["masters"][0]
        master["site_anchor_key"] = "site:project"
        master["site_anchor_description"] = "Shared industrial site"
        self.add_scene_dependency("site:project")
        self.add_scene_asset("site:project")
        request = await self.request_at_boundary()
        self.assertEqual(request["request_kwargs"]["references"][0], "https://example.invalid/site:project.png")
        self.assertIn("SITE", scene_assets.scene_anchor_prompt(
            scene_assets.required_scene_anchors({"masters": [master]}, {"site:project"})[0]
        ))
        self.assertEqual(scene_assets.required_scene_anchors(
            {"masters": [master]}, {"site:project"},
        )[0]["blocking_anchor"], {})

    async def test_both_dependencies_preserve_exact_scene_reference_preference(self):
        self.prepare()
        master = self.payload["runtime_storyboard"]["masters"][0]
        master["site_anchor_key"] = "site:project"
        for key in (master["scene_anchor_key"], "site:project"):
            self.add_scene_dependency(key)
            self.add_scene_asset(key)
        request = await self.request_at_boundary()
        self.assertEqual(request["request_kwargs"]["references"][0],
                         f"https://example.invalid/{master['scene_anchor_key']}.png")
        self.assertNotIn("https://example.invalid/site:project.png", request["request_kwargs"]["references"])

    async def test_malformed_frozen_graph_blocks_before_s3(self):
        self.prepare()
        self.payload.pop("edges")
        self.save_payload()
        with mock.patch.object(s3_shots, "run", new=mock.AsyncMock()) as run:
            with self.assertRaises(production_adapters.FrozenSpecMismatchError):
                await production_adapters.run_master_node(self.database, self.node)
        run.assert_not_awaited()

    async def test_scene_loader_explicit_empty_skips_recurrence_but_none_keeps_legacy(self):
        self.prepare()
        storyboard = {"masters": [
            {"idx": index, "scene_anchor_key": "recurring", "scene": "room"}
            for index in (1, 2)
        ]}
        resolved = await s3_shots._ensure_scene_visual_anchors(
            self.node["job_id"], storyboard, anchor_keys=set(), existing_only=True,
        )
        self.assertEqual(resolved, {})
        with self.assertRaisesRegex(RuntimeError, "frozen scene anchor is not ready: recurring"):
            await s3_shots._ensure_scene_visual_anchors(
                self.node["job_id"], storyboard, existing_only=True,
            )

    async def test_scene_loader_unknown_explicit_key_cannot_silently_disappear(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, "scene anchor.*resolve"):
            await s3_shots._ensure_scene_visual_anchors(
                self.node["job_id"], self.payload["runtime_storyboard"],
                anchor_keys={"unknown"}, existing_only=True,
            )

    async def test_legacy_run_none_keeps_recurring_fallback_when_target_has_no_keys(self):
        self.prepare()
        storyboard = deepcopy(self.payload["runtime_storyboard"])
        storyboard["masters"][0].pop("scene_anchor_key")
        storyboard["masters"].extend([
            {"idx": index, "scene_anchor_key": "recurring", "scene": "room"}
            for index in (2, 3)
        ])
        with self.assertRaisesRegex(RuntimeError, "frozen scene anchor is not ready: recurring"):
            await s3_shots.run(
                self.node["job_id"], storyboard_override=storyboard,
                characters_override=self.payload["characters"],
                target_master_indexes={1}, prepare_anchors=False,
            )

    async def test_explicit_s3_keys_must_resolve_for_selected_master(self):
        self.prepare()
        storyboard = deepcopy(self.payload["runtime_storyboard"])
        storyboard["masters"].append({"idx": 2, "scene_anchor_key": "other", "scene": "room"})
        with mock.patch.object(s3_shots, "_generate_seedance_video", new=mock.AsyncMock()) as generate:
            with self.assertRaisesRegex(ValueError, "scene anchor.*resolve"):
                await s3_shots.run(
                    self.node["job_id"], storyboard_override=storyboard,
                    characters_override=self.payload["characters"],
                    target_master_indexes={1}, prepare_anchors=False,
                    required_scene_anchor_keys={"other"},
                )
        generate.assert_not_awaited()


class FrozenSceneGraphValidationTests(unittest.TestCase):
    def graph(self):
        return {
            "runtime_storyboard": {"masters": [{
                "idx": 1, "scene_anchor_key": "scene:yard", "site_anchor_key": "site:project",
            }]},
            "nodes": [{"node_id": "master:1", "node_type": "master", "input": {"master_idx": 1}}],
            "edges": [],
        }

    def dependency(self, graph, key="scene:yard"):
        graph["nodes"].append({"node_id": f"scene_anchor:{key}", "node_type": "scene_anchor",
                               "input": {"scene_key": key}})
        graph["edges"].append({"from_node_id": f"scene_anchor:{key}", "to_node_id": "master:1",
                               "edge_type": "scene_reference"})

    def extract(self, graph):
        return production_adapters._required_scene_anchor_keys(graph, 1)

    def test_valid_empty_site_and_both_dependencies(self):
        graph = self.graph()
        self.assertEqual(self.extract(graph), set())
        self.dependency(graph, "site:project")
        self.assertEqual(self.extract(graph), {"site:project"})
        self.dependency(graph)
        self.assertEqual(self.extract(graph), {"site:project", "scene:yard"})

    def test_missing_or_malformed_collections_fail_closed(self):
        for field in ("nodes", "edges"):
            for value in (None, {}, "", [None]):
                with self.subTest(field=field, value=value):
                    graph = self.graph()
                    graph[field] = value
                    with self.assertRaises(production_adapters.FrozenSpecMismatchError):
                        self.extract(graph)
            graph = self.graph()
            graph.pop(field)
            with self.assertRaises(production_adapters.FrozenSpecMismatchError):
                self.extract(graph)

    def test_duplicate_or_missing_master_and_malformed_input_fail_closed(self):
        mutations = (
            lambda g: g["nodes"].append(deepcopy(g["nodes"][0])),
            lambda g: g["nodes"].clear(),
            lambda g: g["nodes"][0].update(input=None),
            lambda g: g["nodes"][0]["input"].update(master_idx=2),
            lambda g: g["nodes"][0].update(node_id="master:2"),
            lambda g: g["runtime_storyboard"]["masters"].clear(),
            lambda g: g["runtime_storyboard"]["masters"].append(deepcopy(g["runtime_storyboard"]["masters"][0])),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                graph = self.graph()
                mutate(graph)
                with self.assertRaises(production_adapters.FrozenSpecMismatchError):
                    self.extract(graph)

    def test_missing_dangling_mistyped_or_mismatched_scene_dependency_fails_closed(self):
        mutations = (
            lambda g: g["edges"].clear(),
            lambda g: g["edges"][0].update(edge_type="identity_reference"),
            lambda g: g["edges"][0].update(from_node_id="missing"),
            lambda g: g["edges"][0].update(to_node_id="missing"),
            lambda g: g["edges"][0].pop("edge_type"),
            lambda g: g["nodes"][1].update(node_type="character_anchor"),
            lambda g: g["nodes"][1]["input"].update(scene_key=""),
            lambda g: g["nodes"][1]["input"].update(scene_key="scene:unknown"),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                graph = self.graph()
                self.dependency(graph)
                mutate(graph)
                with self.assertRaises(production_adapters.FrozenSpecMismatchError):
                    self.extract(graph)

    def test_incoming_unknown_scene_key_and_noncanonical_node_id_fail_closed(self):
        graph = self.graph()
        self.dependency(graph, "scene:other")
        with self.assertRaises(production_adapters.FrozenSpecMismatchError):
            self.extract(graph)
        graph = self.graph()
        self.dependency(graph)
        graph["nodes"][1]["node_id"] = "alias"
        graph["edges"][0]["from_node_id"] = "alias"
        with self.assertRaises(production_adapters.FrozenSpecMismatchError):
            self.extract(graph)

    def test_each_canonical_scene_node_requires_its_own_reference_edge(self):
        graph = self.graph()
        self.dependency(graph)
        graph["edges"].clear()
        # A second spelling resolving to the same asset cannot excuse an
        # orphaned canonical node in the frozen graph.
        self.dependency(graph, "Scene:Yard")
        with self.assertRaises(production_adapters.FrozenSpecMismatchError):
            self.extract(graph)


if __name__ == "__main__":
    unittest.main()
