"""First-principles production planning, reuse, and economics tests."""
from pathlib import Path
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


from factory import (  # noqa: E402
    asset_library,
    character_assets,
    creative_ir,
    db,
    master_risk,
    orchestrator,
    production_compiler,
    production_graph,
    production_metrics,
    production_planner,
    quality_canary,
)
from factory import prompts  # noqa: E402
from factory.steps import s1_storyboard, s3_shots  # noqa: E402
import cli as cli_module  # noqa: E402


class MasterRiskTests(unittest.TestCase):
    def test_long_single_speaker_low_action_master_is_medium_risk(self):
        report = master_risk.assess_master({
            "duration": 26,
            "speaker": "linxia",
            "dialogue": "暴雨还在继续，我们按计划完成最后一次安全检查",
            "performance_beats": ["抬头", "确认设备"],
        })

        self.assertEqual(report["level"], "medium")
        self.assertEqual(report["factors"]["speaking_role_count"], 1)

    def test_long_three_speaker_action_master_is_high_risk(self):
        report = master_risk.assess_master({
            "duration": 26,
            "dialogue_blocks": [
                {"speaker": "a", "text": "先断电"},
                {"speaker": "b", "text": "收到，正在固定吊篮"},
                {"speaker": "c", "text": "风速回落，可以继续"},
            ],
            "performance_beats": ["奔跑", "拉闸", "攀爬", "固定", "回望"],
            "scene_change_markers": ["楼顶", "吊篮"],
        })

        self.assertEqual(report["level"], "high")
        self.assertGreaterEqual(report["factors"]["speaking_role_count"], 3)

    def test_short_simple_master_is_low_risk(self):
        report = master_risk.assess_master({
            "duration": 12,
            "speaker": "host",
            "dialogue": "先确认安全，再开始作业",
            "performance_beats": ["点头"],
        })

        self.assertEqual(report["level"], "low")


class MinimumPaidMasterTests(unittest.TestCase):
    def test_legacy_storyboard_has_no_fixed_shot_or_broll_count_gate(self):
        storyboard = {
            "characters": [{
                "id": "host",
                "name": "Host",
                "gender": "female",
                "look": "A fictional equipment-rental consultant",
            }],
            "hook": "A visual evidence montage",
            "bgm": {"mood": "restrained", "prompt": "light rhythm"},
            "shots": [
                {
                    "idx": idx,
                    "type": "broll",
                    "duration": 4,
                    "prompt": f"Evidence detail {idx}",
                }
                for idx in range(1, 14)
            ],
        }

        s1_storyboard._validate(storyboard)

    def test_single_dialogue_master_is_valid_when_it_covers_the_whole_story(self):
        storyboard = {
            "production_profile": "standard",
            "production_mode": "native_master_local_director",
            "characters": [{
                "id": "host",
                "name": "Host",
                "gender": "female",
                "look": "A fictional equipment-rental consultant",
            }],
            "hook": "One master is enough",
            "bgm": {"mood": "restrained", "prompt": "light rhythm"},
            "masters": [{
                "idx": 1,
                "type": "dialogue",
                "speaker": "host",
                "render_mode": "presenter",
                "duration": 12,
                "scene": "equipment yard office",
                "action": "speaks naturally to camera",
                "voiceover": "A complete short explanation delivered once.",
            }],
            "shots": [
                {
                    "idx": idx,
                    "video_master_idx": 1,
                    "audio_master_idx": 1,
                    "video_start": start,
                    "audio_start": start,
                    "duration": 3,
                    "subtitle": f"Part {idx}",
                }
                for idx, start in enumerate((0, 3, 6, 9), start=1)
            ],
        }

        s1_storyboard._validate(storyboard)

    def test_storyboard_prompt_requires_fewest_paid_masters_and_asset_tags(self):
        prompt = prompts.storyboard_prompt(
            title="Equipment rental",
            track="B2B",
            duration=18,
        )

        self.assertNotIn("1—3 条", prompt)
        self.assertIn("不设置人为条数或整单总时长上限", prompt)
        self.assertIn("最少", prompt)
        self.assertIn("asset_tags", prompt)
        self.assertIn("素材库", prompt)


class BrollAssetLibraryTests(unittest.TestCase):
    def setUp(self):
        self.memory_db = sqlite3.connect(":memory:")
        self.memory_db.row_factory = sqlite3.Row
        self.memory_db.executescript(db.DDL)

    def tearDown(self):
        self.memory_db.close()

    def test_reuses_best_approved_rights_safe_asset_and_records_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weak = root / "generic.mp4"
            best = root / "excavator.mp4"
            rejected = root / "unlicensed.mp4"
            for path in (weak, best, rejected):
                path.write_bytes(b"video")

            asset_library.register_asset(
                self.memory_db,
                asset_id="generic-yard",
                path=weak,
                tags=["设备", "工地"],
                duration_sec=12,
            )
            asset_library.register_asset(
                self.memory_db,
                asset_id="excavator-yard",
                path=best,
                tags=["挖掘机", "设备", "工地"],
                duration_sec=10,
            )
            asset_library.register_asset(
                self.memory_db,
                asset_id="unlicensed-perfect",
                path=rejected,
                tags=["挖掘机", "设备", "工地"],
                duration_sec=20,
                rights_status="unknown",
            )
            self.memory_db.execute(
                "INSERT INTO shots(job_id, idx, shot_type, duration_sec) "
                "VALUES('J-1', 2, 'broll', 6)"
            )

            reused = asset_library.reuse_broll_assets(
                self.memory_db,
                "J-1",
                [{
                    "idx": 2,
                    "type": "broll",
                    "duration": 6,
                    "asset_tags": ["挖掘机", "设备", "工地"],
                }],
            )

        self.assertEqual(reused, {2: str(best)})
        shot = self.memory_db.execute(
            "SELECT state, provider, video_path, cost_cents FROM shots "
            "WHERE job_id='J-1' AND idx=2"
        ).fetchone()
        self.assertEqual(dict(shot), {
            "state": "OK",
            "provider": "asset_library",
            "video_path": str(best),
            "cost_cents": 0,
        })
        usage = self.memory_db.execute(
            "SELECT asset_id, job_id, master_idx FROM asset_usages"
        ).fetchone()
        self.assertEqual(tuple(usage), ("excavator-yard", "J-1", 2))

    def test_cli_can_register_and_search_broll_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "yard.mp4"
            video.write_bytes(b"video")
            runner = CliRunner()
            with patch.object(cli_module, "conn", return_value=self.memory_db):
                added = runner.invoke(
                    cli_module.cli,
                    [
                        "asset-add",
                        str(video),
                        "--asset-id",
                        "yard-001",
                        "--tag",
                        "挖掘机",
                        "--tag",
                        "工地",
                        "--duration",
                        "8",
                    ],
                )
                searched = runner.invoke(
                    cli_module.cli, ["assets", "--tag", "挖掘机"]
                )

        self.assertEqual(added.exit_code, 0, added.output)
        self.assertEqual(searched.exit_code, 0, searched.output)
        self.assertIn("yard-001", searched.output)
        self.assertIn("挖掘机", searched.output)


class BrollPipelineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_s3_reuses_library_before_any_seedance_submission(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        memory_db.execute("ALTER TABLE jobs ADD COLUMN characters_json TEXT")
        memory_db.execute("ALTER TABLE shots ADD COLUMN narrative_type TEXT")
        memory_db.execute("ALTER TABLE shots ADD COLUMN speaker_id TEXT")
        memory_db.execute("ALTER TABLE shots ADD COLUMN anchor_character TEXT")
        storyboard = {
            "production_mode": "native_master_local_director",
            "masters": [{
                "idx": 1,
                "type": "broll",
                "duration": 6,
                "asset_tags": ["挖掘机", "工地"],
            }],
            "shots": [],
        }
        memory_db.execute(
            "INSERT INTO jobs(job_id, topic_id, title, storyboard, "
            "characters_json) VALUES(?,?,?,?,?)",
            ("J-LIB", "T-LIB", "Reuse", json.dumps(storyboard), "[]"),
        )
        memory_db.execute(
            "INSERT INTO shots(job_id, idx, duration_sec, narrative_type) "
            "VALUES('J-LIB', 1, 6, 'broll')"
        )
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "approved.mp4"
            video.write_bytes(b"video")
            asset_library.register_asset(
                memory_db,
                asset_id="approved-broll",
                path=video,
                tags=["挖掘机", "工地"],
                duration_sec=8,
            )
            with patch.object(s3_shots, "conn", return_value=memory_db), patch.object(
                s3_shots,
                "_generate_seedance_video",
                new=AsyncMock(side_effect=AssertionError("must not submit")),
            ) as generate:
                await s3_shots.run("J-LIB")

        generate.assert_not_awaited()
        row = memory_db.execute(
            "SELECT state, provider FROM shots WHERE job_id='J-LIB' AND idx=1"
        ).fetchone()
        self.assertEqual(tuple(row), ("OK", "asset_library"))
        memory_db.close()


class ProductionMetricsTests(unittest.TestCase):
    def setUp(self):
        self.memory_db = sqlite3.connect(":memory:")
        self.memory_db.row_factory = sqlite3.Row
        self.memory_db.executescript(db.DDL)

    def tearDown(self):
        self.memory_db.close()

    def test_metrics_measure_approved_output_not_generation_success(self):
        for idx in range(1, 4):
            self.memory_db.execute(
                "INSERT INTO jobs(job_id, topic_id, title, state) VALUES(?,?,?,?)",
                (f"J-{idx}", f"T-{idx}", f"Topic {idx}", "COMPOSED"),
            )
        self.memory_db.executemany(
            "INSERT INTO provider_calls(task_id, job_id, shot_idx, provider, "
            "cost_cents) VALUES(?,?,?,?,?)",
            [
                ("task-1", "J-1", 1, "seedance", 800),
                ("task-2", "J-2", 1, "seedance", 200),
            ],
        )
        production_metrics.record_review(
            self.memory_db, "J-1", "local_recut", manual_minutes=5
        )
        production_metrics.record_review(
            self.memory_db, "J-1", "approved", manual_minutes=3
        )
        production_metrics.record_review(
            self.memory_db, "J-2", "paid_regen", manual_minutes=4
        )

        metrics = production_metrics.summarize(self.memory_db)

        self.assertEqual(metrics["created_jobs"], 3)
        self.assertEqual(metrics["approved_jobs"], 1)
        self.assertAlmostEqual(metrics["qualified_yield_rate"], 1 / 3)
        self.assertEqual(metrics["model_cost_cents"], 1000)
        self.assertEqual(metrics["cost_per_approved_cents"], 1000)
        self.assertEqual(metrics["manual_minutes"], 12)
        self.assertEqual(metrics["paid_calls_per_approved"], 2)
        self.assertEqual(metrics["zero_cost_repair_rate"], 0.5)

    def test_schema_and_metrics_use_one_batch_cohort_for_every_measure(self):
        job_columns = {
            row[1] for row in self.memory_db.execute("PRAGMA table_info(jobs)")
        }
        review_columns = {
            row[1]
            for row in self.memory_db.execute("PRAGMA table_info(review_events)")
        }
        self.assertIn("batch_id", job_columns)
        self.assertIn("first_pass", review_columns)

        jobs = [
            ("J-A1", "T-A1", "Alpha approved", "B-ALPHA", "2026-08-12 09:00:00"),
            ("J-A2", "T-A2", "Alpha rejected", "B-ALPHA", "2026-08-12 09:05:00"),
            ("J-B1", "T-B1", "Old other batch", "B-BETA", "2026-08-11 09:00:00"),
        ]
        self.memory_db.executemany(
            "INSERT INTO jobs(job_id, topic_id, title, batch_id, created_at) "
            "VALUES(?,?,?,?,?)",
            jobs,
        )
        self.memory_db.executemany(
            "INSERT INTO provider_calls(task_id, job_id, shot_idx, provider, "
            "cost_cents) VALUES(?,?,?,?,?)",
            [
                ("task-a", "J-A1", 1, "seedance", 800),
                ("task-b", "J-B1", 1, "seedance", 200),
            ],
        )
        production_metrics.record_review(
            self.memory_db,
            "J-A1",
            "approved",
            manual_minutes=2,
            first_pass=True,
        )
        production_metrics.record_review(
            self.memory_db,
            "J-A2",
            "rejected",
            manual_minutes=1,
            first_pass=False,
        )
        production_metrics.record_review(
            self.memory_db,
            "J-B1",
            "approved",
            manual_minutes=10,
            first_pass=False,
        )

        by_batch = production_metrics.summarize(
            self.memory_db, batch_id="B-ALPHA"
        )
        by_date = production_metrics.summarize(
            self.memory_db, since="2026-08-12"
        )

        for report in (by_batch, by_date):
            self.assertEqual(report["created_jobs"], 2)
            self.assertEqual(report["approved_jobs"], 1)
            self.assertEqual(report["model_cost_cents"], 800)
            self.assertEqual(report["manual_minutes"], 3)
            self.assertEqual(report["first_pass_rate"], 1.0)

    def test_cli_records_review_and_prints_factory_metrics(self):
        self.memory_db.execute(
            "INSERT INTO jobs(job_id, topic_id, title, state, batch_id) "
            "VALUES('J-CLI','T-CLI','CLI topic','COMPOSED','B-CLI')"
        )
        runner = CliRunner()
        with patch.object(cli_module, "conn", return_value=self.memory_db):
            review = runner.invoke(
                cli_module.cli,
                [
                    "review", "J-CLI", "approved", "--minutes", "2.5",
                    "--first-pass",
                ],
            )
            report = runner.invoke(
                cli_module.cli, ["metrics", "--batch", "B-CLI"]
            )

        self.assertEqual(review.exit_code, 0, review.output)
        self.assertIn("approved", review.output)
        self.assertEqual(report.exit_code, 0, report.output)
        self.assertIn("合格成片率", report.output)
        self.assertIn("100.0%", report.output)
        self.assertIn("B-CLI", report.output)

    def test_cli_plan_prints_cost_without_mutating_provider_calls(self):
        storyboard = {
            "production_profile": "standard",
            "production_mode": "native_master_local_director",
            "characters": [{
                "id": "host",
                "name": "Host",
                "gender": "female",
                "look": "A fictional rental consultant",
            }],
            "hook": "Cost before generation",
            "bgm": {"mood": "restrained", "prompt": "light rhythm"},
            "masters": [{
                "idx": 1,
                "type": "dialogue",
                "speaker": "host",
                "render_mode": "presenter",
                "duration": 8,
                "scene": "equipment yard",
                "action": "speaks to camera",
                "voiceover": "Plan before paying for the video.",
            }],
            "shots": [
                {
                    "idx": idx,
                    "video_master_idx": 1,
                    "audio_master_idx": 1,
                    "video_start": start,
                    "audio_start": start,
                    "duration": 2,
                    "subtitle": f"Part {idx}",
                }
                for idx, start in enumerate((0, 2, 4, 6), start=1)
            ],
        }
        runner = CliRunner()
        with runner.isolated_filesystem():
            path = Path("storyboard.json")
            path.write_text(json.dumps(storyboard), encoding="utf-8")
            with patch.object(cli_module, "conn", return_value=self.memory_db):
                result = runner.invoke(
                    cli_module.cli,
                    ["plan", str(path), "--budget", "3000"],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("paid masters: 1/1", result.output)
        self.assertIn("AI identity makeup anchors: 1", result.output)
        self.assertIn("estimated cost", result.output)
        self.assertIn("within budget", result.output)
        calls = self.memory_db.execute(
            "SELECT COUNT(*) FROM provider_calls"
        ).fetchone()[0]
        self.assertEqual(calls, 0)

    def test_reliability_summary_uses_only_structured_evidence(self):
        jobs = [
            ("J-PASS", "T-1", "pass", "COMPOSED", 100, None,
             "2026-08-20 09:00:00", "2026-08-20 09:01:00"),
            ("J-REVIEW", "T-2", "review", "PRODUCTION_REVIEW_REQUIRED", 200, None,
             "2026-08-20 10:00:00", "2026-08-20 10:03:00"),
            ("J-PAID-FAIL", "T-3", "paid fail", "FAILED", 300,
             "input_privacy_rejected", "2026-08-20 11:00:00", "2026-08-20 11:05:00"),
            ("J-STUCK", "T-4", "stuck", "RECOVERY_REQUIRED", 400, None,
             "2026-08-20 12:00:00", "2026-08-20 12:10:00"),
        ]
        self.memory_db.executemany(
            "INSERT INTO jobs(job_id,topic_id,title,state,cost_cents,failure_code,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            jobs,
        )
        self.memory_db.executemany(
            "INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,"
            "cost_cents,usage_json) VALUES(?,?,?,?,?,?,?)",
            [
                ("task-pass", "J-PASS", 1, "seedance", "generate", 100, "{}"),
                ("task-review", "J-REVIEW", 1, "seedance", "generate", 200, "{}"),
                ("task-policy", "J-PAID-FAIL", 1, "seedance", "generate", 300, "{}"),
                ("task-dup", "J-STUCK", 1, "seedance", "generate", 400,
                 json.dumps({"duplicate_submission": True})),
            ],
        )
        self.memory_db.executemany(
            "INSERT INTO job_transitions(event_id,job_id,event,from_state,to_state) "
            "VALUES(?,?,?,?,?)",
            [
                ("e-review", "J-REVIEW", "review_required", "RUNNING", "PRODUCTION_REVIEW_REQUIRED"),
                ("e-recovery-ok", "J-PASS", "recovery_requeue", "RECOVERY_REQUIRED", "QUEUED"),
                ("e-recovery-fail", "J-STUCK", "recovery_required", "RUNNING", "RECOVERY_REQUIRED"),
            ],
        )
        production_metrics.record_review(
            self.memory_db, "J-PASS", "approved", first_pass=True,
        )
        production_metrics.record_review(
            self.memory_db, "J-REVIEW", "local_recut", first_pass=False,
        )

        report = production_metrics.reliability_summary(
            self.memory_db,
            since="2026-08-20 00:00:00",
            now=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )

        self.assertEqual(report["created_jobs"], 4)
        self.assertEqual(report["first_round_eligible_jobs"], 3)
        self.assertAlmostEqual(report["first_round_progression_rate"], 1 / 3)
        self.assertEqual(report["deterministic_post_paid_failures"], 1)
        self.assertEqual(report["unknown_stuck_jobs"], 1)
        self.assertAlmostEqual(report["recovery_success_rate"], 0.5)
        self.assertAlmostEqual(report["manual_review_rate"], 0.25)
        self.assertEqual(report["duplicate_paid_submissions"], 1)
        self.assertEqual(report["duration_seconds"]["p50"], 240.0)
        self.assertEqual(report["duration_seconds"]["p95"], 555.0)
        self.assertEqual(report["cost_cents"]["p50"], 250)
        self.assertEqual(report["cost_cents"]["p95"], 385)

    def test_release_gate_needs_seven_clean_days_and_freezes_on_any_safety_event(self):
        clean = [
            {
                "date": f"2026-08-{day:02d}",
                "first_round_progression_rate": 0.96,
                "duplicate_paid_submissions": 0,
                "unknown_stuck_jobs": 0,
                "deterministic_post_paid_failures": 0,
            }
            for day in range(14, 21)
        ]
        self.assertEqual(
            quality_canary.evaluate_release_gate(clean)["status"], "pass"
        )
        self.assertEqual(
            quality_canary.evaluate_release_gate(clean[:6])["status"],
            "insufficient_data",
        )
        unsafe = [dict(row) for row in clean]
        unsafe[-1]["duplicate_paid_submissions"] = 1
        frozen = quality_canary.evaluate_release_gate(unsafe)
        self.assertEqual(frozen["status"], "freeze")
        self.assertIn("duplicate_paid_submission", frozen["reasons"])

    def test_real_provider_canary_requires_explicit_confirmation_and_own_budget(self):
        with self.assertRaisesRegex(ValueError, "explicit confirmation"):
            quality_canary.prepare_explicit_run(
                "single-speaker-8s-v1", confirmed=False, budget_cents=100,
            )
        with self.assertRaisesRegex(ValueError, "budget"):
            quality_canary.prepare_explicit_run(
                "single-speaker-8s-v1", confirmed=True, budget_cents=0,
            )

        contract = quality_canary.prepare_explicit_run(
            "single-speaker-8s-v1", confirmed=True, budget_cents=2000,
        )

        self.assertEqual(contract["launch_mode"], "manual_confirmed")
        self.assertFalse(contract["scheduler_allowed"])
        self.assertFalse(contract["uses_production_real_assets"])
        self.assertEqual(contract["job_budget_cents"], 1500)
        self.assertEqual(contract["confirmed_budget_cents"], 2000)

    def test_frozen_canaries_merge_multi_person_and_multi_scene_into_one_shape(self):
        profiles = quality_canary.load_frozen_profiles()

        self.assertEqual(len(profiles), 3)
        self.assertEqual(
            {profile["id"] for profile in profiles},
            {
                "single-speaker-8s-v1",
                "two-person-continuous-18s-v1",
                "multi-person-multi-scene-cinematic-30s-v1",
            },
        )

    def test_combined_cinematic_canary_builds_three_people_across_two_scenes(self):
        contract = quality_canary.prepare_explicit_run(
            "multi-person-multi-scene-cinematic-30s-v1",
            confirmed=True,
            budget_cents=6000,
        )

        storyboard = quality_canary.build_isolated_storyboard(
            "multi-person-multi-scene-cinematic-30s-v1", contract
        )

        self.assertEqual(len(storyboard["characters"]), 3)
        self.assertEqual(
            {item["id"] for item in storyboard["characters"]},
            {"canary_character_1", "canary_character_2", "canary_character_3"},
        )
        self.assertEqual(len(storyboard["masters"]), 2)
        self.assertEqual(
            [master["duration"] for master in storyboard["masters"]],
            [15.0, 15.0],
        )
        self.assertNotEqual(
            storyboard["masters"][0]["scene"],
            storyboard["masters"][1]["scene"],
        )
        self.assertEqual(
            [shot["video_master_idx"] for shot in storyboard["shots"]],
            [1, 1, 1, 2, 2, 2],
        )
        self.assertEqual(
            [shot["video_start"] for shot in storyboard["shots"]],
            [0.0, 5.0, 10.0, 0.0, 5.0, 10.0],
        )
        self.assertEqual(
            [set(master["speakers"]) for master in storyboard["masters"]],
            [
                {"canary_character_1", "canary_character_2", "canary_character_3"},
                {"canary_character_1", "canary_character_2", "canary_character_3"},
            ],
        )
        self.assertEqual(
            {speaker for master in storyboard["masters"] for speaker in master["speakers"]},
            {"canary_character_1", "canary_character_2", "canary_character_3"},
        )

    def test_two_person_canary_freezes_both_identity_dependencies_and_request_duties(self):
        contract = quality_canary.prepare_explicit_run(
            "two-person-continuous-18s-v1",
            confirmed=True,
            budget_cents=3000,
        )
        storyboard = quality_canary.build_isolated_storyboard(
            "two-person-continuous-18s-v1", contract
        )
        creative = creative_ir.from_storyboard(storyboard)
        compilation = production_compiler.compile_ir(
            creative,
            job_id="J-CANARY-ZERO-COST",
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=3000,
            runtime_storyboard=storyboard,
        )
        graph = production_graph.compile_graph(compilation.spec)
        master = storyboard["masters"][0]
        character_by_id = {
            item["id"]: item for item in storyboard["characters"]
        }
        primary = character_by_id[master["speaker"]]
        cast = s3_shots._scene_cast_characters(
            master, character_by_id, master["speaker"]
        )
        anchors = {
            character_assets.character_identity_key(character):
                s3_shots.DialogueAnchor(
                    image_reference=f"https://assets/{character['id']}.png",
                    anchor_mode="static_image",
                )
            for character in storyboard["characters"]
        }
        request = s3_shots._dialogue_request_kwargs(
            {**master, "duration_sec": master["duration"]},
            None,
            character=primary,
            cast_characters=cast,
            identity_anchors=anchors,
        )

        required_ids = {item["id"] for item in storyboard["characters"]}
        self.assertEqual(
            s3_shots._required_character_ids(storyboard["masters"]),
            required_ids,
        )
        self.assertEqual(
            {
                node_id for node_id in graph.ancestors("master:1")
                if node_id.startswith("character_anchor:")
            },
            {f"character_anchor:{character_id}" for character_id in required_ids},
        )
        self.assertEqual(
            {item["id"] for item in cast}, required_ids - {primary["id"]}
        )
        self.assertEqual(
            [item["duty"] for item in request["reference_manifest"]],
            ["speaker_identity", "supporting_identity"],
        )

    def test_canary_scorecard_measures_reviewed_first_pass_quality_by_profile(self):
        def record(run_id, *, first_pass, lip_sync, completion="complete"):
            scores = {field: 4.5 for field in quality_canary.MANUAL_SCORE_FIELDS}
            scores["lip_sync"] = lip_sync
            return {
                "run_id": run_id,
                "classification": "canary",
                "profile_id": "single-speaker-8s-v1",
                "completion_status": completion,
                "cost": {"actual_cents": 1000},
                "quality": {
                    "final_review": {
                        "decision": "approved" if first_pass else "local_recut",
                        "first_pass": int(first_pass),
                    },
                    "manual_scores": scores if completion == "complete" else None,
                },
            }

        scorecard = quality_canary.summarize_acceptance_records(
            [
                record("pass", first_pass=True, lip_sync=4.5),
                record("repair", first_pass=False, lip_sync=2.5),
                record(
                    "evidence-only",
                    first_pass=False,
                    lip_sync=0,
                    completion="evidence_only",
                ),
            ],
            minimum_reviewed_runs=2,
            minimum_first_pass_rate=0.5,
        )

        profile = next(
            item for item in scorecard["profiles"]
            if item["profile_id"] == "single-speaker-8s-v1"
        )
        self.assertEqual(profile["runs"], 3)
        self.assertEqual(profile["reviewed_runs"], 2)
        self.assertEqual(profile["first_pass_usable_runs"], 1)
        self.assertEqual(profile["first_pass_usable_rate"], 0.5)
        self.assertEqual(profile["below_target_dimensions"], {"lip_sync": 1})
        self.assertEqual(profile["status"], "pass")
        self.assertEqual(scorecard["top_failure_dimensions"][0]["dimension"], "lip_sync")

    def test_canary_scorecard_ignores_observational_and_unrecognized_records(self):
        records = [{
            "run_id": "observed",
            "classification": "observational",
            "profile_id": "single-speaker-8s-v1",
            "completion_status": "complete",
            "quality": {"manual_scores": {
                field: 0 for field in quality_canary.MANUAL_SCORE_FIELDS
            }},
        }, {
            "run_id": "unknown",
            "classification": "canary",
            "profile_id": "unknown-profile",
            "completion_status": "complete",
            "quality": {"manual_scores": {
                field: 5 for field in quality_canary.MANUAL_SCORE_FIELDS
            }},
        }]

        scorecard = quality_canary.summarize_acceptance_records(records)

        self.assertEqual(scorecard["ignored_records"], 2)
        self.assertEqual(scorecard["reviewed_runs"], 0)
        self.assertEqual(scorecard["status"], "no_data")

    def test_management_ui_renders_seven_and_thirty_day_reliability(self):
        javascript = (PROJECT_ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8",
        )

        self.assertIn("function renderReliability", javascript)
        self.assertIn("7 天可靠性", javascript)
        self.assertIn("30 天趋势", javascript)
        self.assertIn("重复付费", javascript)
        self.assertIn("未知卡住", javascript)
        self.assertIn("人工质量金丝雀", javascript)
        self.assertIn("/api/quality-canary/jobs", javascript)
        self.assertIn("quality-canary-confirm", javascript)


class ProductionPlannerTests(unittest.TestCase):
    def setUp(self):
        self.anchor_generators = patch.dict(os.environ, {
            "CHARACTER_ANCHOR_GENERATOR": "seedream_5_0",
            "SCENE_ANCHOR_GENERATOR": "seedream_5_0",
        })
        self.anchor_generators.start()
        self.memory_db = sqlite3.connect(":memory:")
        self.memory_db.row_factory = sqlite3.Row
        self.memory_db.executescript(db.DDL)

    def tearDown(self):
        self.memory_db.close()
        self.anchor_generators.stop()

    def test_plan_reuses_broll_and_prices_only_paid_masters(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "yard.mp4"
            video.write_bytes(b"video")
            asset_library.register_asset(
                self.memory_db,
                asset_id="yard-001",
                path=video,
                tags=["excavator", "equipment-yard"],
                duration_sec=8,
            )
            report = production_planner.analyze_storyboard(
                self.memory_db,
                {
                    "masters": [
                        {"idx": 1, "type": "dialogue", "duration": 8},
                        {
                            "idx": 2,
                            "type": "broll",
                            "duration": 6,
                            "asset_tags": ["excavator", "equipment-yard"],
                        },
                    ]
                },
                budget_cents=500,
                resolution="720p",
            )

        self.assertEqual(report["total_masters"], 2)
        self.assertEqual(report["paid_master_count"], 1)
        self.assertEqual(report["library_hits"][0]["asset_id"], "yard-001")
        self.assertEqual(report["estimated_cost_cents"], 1224)
        self.assertFalse(report["budget_ok"])

    def test_plan_flags_broll_without_tags_as_a_paid_risk(self):
        report = production_planner.analyze_storyboard(
            self.memory_db,
            {"masters": [{"idx": 1, "type": "broll", "duration": 6}]},
            budget_cents=500,
            resolution="720p",
        )

        self.assertEqual(report["paid_master_count"], 1)
        self.assertEqual(report["estimated_cost_cents"], 918)
        self.assertTrue(any("asset_tags" in item for item in report["warnings"]))

    def test_legacy_preflight_snapshot_matches_the_explicit_resolution(self):
        with patch.dict(os.environ, {"ARK_VIDEO_RESOLUTION": "720p"}):
            report = production_planner.analyze_storyboard(
                self.memory_db,
                {"masters": [{"idx": 1, "type": "broll", "duration": 4}]},
                budget_cents=2500,
                resolution="480p",
            )

        self.assertEqual(report["generation_snapshot"]["resolution"], "480p")

    def test_plan_estimates_only_remaining_masters_and_projects_total_cost(self):
        report = production_planner.analyze_storyboard(
            self.memory_db,
            {
                "masters": [
                    {"idx": 1, "type": "dialogue", "duration": 23},
                    {"idx": 2, "type": "dialogue", "duration": 5},
                    {"idx": 3, "type": "dialogue", "duration": 12},
                ]
            },
            budget_cents=2500,
            resolution="480p",
            completed_master_indexes={1, 2},
            current_cost_cents=1889,
        )

        self.assertEqual(report["estimated_paid_calls"], 1)
        self.assertEqual(report["estimated_cost_cents"], 816)
        self.assertEqual(report["current_cost_cents"], 1889)
        self.assertEqual(report["projected_total_cost_cents"], 2705)
        self.assertFalse(report["budget_ok"])

    def test_plan_prices_followup_dialogue_reference_input_duration(self):
        report = production_planner.analyze_storyboard(
            self.memory_db,
            {
                "masters": [
                    {
                        "idx": 1,
                        "type": "dialogue",
                        "speaker_id": "host",
                        "render_mode": "presenter",
                        "duration": 30,
                    },
                    {
                        "idx": 2,
                        "type": "dialogue",
                        "speaker_id": "host",
                        "render_mode": "presenter",
                        "duration": 6,
                    },
                    {
                        "idx": 3,
                        "type": "broll",
                        "duration": 12,
                    },
                ]
            },
            budget_cents=6000,
            resolution="480p",
        )

        self.assertEqual(
            [item["estimated_cost_cents"] for item in report["paid_masters"]],
            [2040, 2448, 816],
        )
        self.assertEqual(report["paid_masters"][1]["reference_duration_sec"], 30)
        self.assertEqual(report["estimated_cost_cents"], 5304)

    def test_paid_master_estimate_persists_calibration_dimensions(self):
        report = production_planner.analyze_storyboard(
            self.memory_db,
            {
                "characters": [{
                    "id": "host",
                    "name": "Host",
                    "generation_mode": "ai_generated",
                    "look": "a fictional presenter",
                }],
                "masters": [
                    {
                        "idx": 1, "type": "dialogue", "duration": 18,
                        "speaker_id": "host", "render_mode": "presenter",
                    },
                    {
                        "idx": 2, "type": "dialogue", "duration": 6,
                        "speaker_id": "host", "render_mode": "presenter",
                    },
                    {"idx": 3, "type": "broll", "duration": 26},
                ],
            },
            budget_cents=20000,
            resolution="720p",
        )

        first, followup, long_broll = report["paid_masters"]
        self.assertEqual(first["resolution"], "720p")
        self.assertEqual(first["duration_bucket"], "13-24s")
        self.assertEqual(first["reference_type"], "image")
        self.assertEqual(
            [item["duty"] for item in first["reference_manifest"]],
            ["speaker_identity"],
        )
        self.assertEqual(followup["duration_bucket"], "4-12s")
        self.assertEqual(followup["reference_type"], "mixed")
        self.assertEqual(
            [(item["kind"], item["duty"]) for item in followup["reference_manifest"]],
            [
                ("image", "speaker_identity"),
                ("video", "speaker_identity"),
            ],
        )
        self.assertEqual(long_broll["duration_bucket"], "25-30s")
        self.assertEqual(long_broll["reference_type"], "none")

    def test_plan_prices_short_editorial_masters_at_provider_minimum(self):
        report = production_planner.analyze_storyboard(
            self.memory_db,
            {
                "masters": [
                    {"idx": 1, "type": "dialogue", "duration": 0.4},
                    {"idx": 2, "type": "dialogue", "duration": 3.33},
                ]
            },
            budget_cents=1000,
            resolution="480p",
        )

        self.assertEqual(report["paid_duration_sec"], 8)
        self.assertEqual(report["estimated_cost_cents"], 544)
        self.assertEqual(
            [item["editorial_duration_sec"] for item in report["paid_masters"]],
            [0.4, 3.33],
        )
        self.assertEqual(
            [item["generation_duration_sec"] for item in report["paid_masters"]],
            [4, 4],
        )

    def test_plan_prices_ai_makeup_anchor_before_scene_generation(self):
        report = production_planner.analyze_storyboard(
            self.memory_db,
            {
                "characters": [{
                    "id": "customer",
                    "name": "Customer",
                    "generation_mode": "ai_generated",
                    "look": "A young customer in a grey sweater",
                }],
                "masters": [{
                    "idx": 1,
                    "type": "dialogue",
                    "speaker": "customer",
                    "render_mode": "cinematic_character",
                    "duration": 4,
                    "scene": "Customer speaks beside rented equipment",
                }],
            },
            budget_cents=2000,
            resolution="480p",
        )

        self.assertEqual(report["paid_identity_anchor_count"], 1)
        self.assertEqual(report["estimated_paid_calls"], 2)
        self.assertEqual(report["identity_anchor_cost_cents"], 29)
        self.assertEqual(
            report["paid_masters"][0].get("reference_duration_sec", 0), 0
        )
        self.assertEqual(report["estimated_cost_cents"], 301)
        self.assertEqual(
            report["character_makeup_plan"][0]["source"],
            "seedream_identity_image",
        )

    def test_plan_prices_one_seedream_anchor_for_repeated_core_scene(self):
        report = production_planner.analyze_storyboard(
            self.memory_db,
            {
                "characters": [],
                "masters": [
                    {
                        "idx": 1, "type": "dialogue", "duration": 4,
                        "scene": "同一设备检修车间，晨光",
                        "scene_anchor_key": "repair-bay-a",
                    },
                    {
                        "idx": 2, "type": "dialogue", "duration": 4,
                        "scene": "同一设备检修车间，晨光",
                        "scene_anchor_key": "repair-bay-a",
                    },
                    {
                        "idx": 3, "type": "broll", "duration": 4,
                        "scene": "一次性的城市道路",
                    },
                ],
            },
            budget_cents=5000,
        )

        self.assertEqual(report["paid_scene_anchor_count"], 1)
        self.assertEqual(report["scene_anchor_cost_cents"], 29)
        self.assertEqual(report["paid_scene_anchors"][0]["master_indexes"], [1, 2])

    def test_plan_prices_previous_master_reference_across_scene_speakers(self):
        characters = [
            {
                "id": "sales", "name": "Sales",
                "generation_mode": "fixed_asset",
                "identity_type": "virtual_person",
                "seedance_asset_uri": "asset://asset-sales",
            },
            {
                "id": "customer", "name": "Customer",
                "generation_mode": "fixed_asset",
                "identity_type": "virtual_person",
                "seedance_asset_uri": "asset://asset-customer",
            },
        ]
        report = production_planner.analyze_storyboard(
            self.memory_db,
            {
                "characters": characters,
                "masters": [
                    {
                        "idx": 1, "type": "dialogue", "speaker": "sales",
                        "render_mode": "cinematic_character", "duration": 4,
                        "scene_anchor_key": "equipment-yard",
                    },
                    {
                        "idx": 2, "type": "dialogue", "speaker": "sales",
                        "render_mode": "cinematic_character", "duration": 4,
                        "scene_anchor_key": "equipment-yard",
                    },
                    {
                        "idx": 3, "type": "dialogue", "speaker": "customer",
                        "render_mode": "cinematic_character", "duration": 4,
                        "scene_anchor_key": "equipment-yard",
                    },
                ],
            },
            budget_cents=5000,
            resolution="480p",
        )

        self.assertEqual(
            [item.get("reference_duration_sec", 0)
             for item in report["paid_masters"]],
            [0, 4, 4],
        )
        self.assertEqual(report["estimated_cost_cents"], 1389)


class BatchImportTests(unittest.TestCase):
    def test_import_assigns_batch_id_for_cohort_metrics(self):
        memory_db = sqlite3.connect(":memory:")
        memory_db.row_factory = sqlite3.Row
        memory_db.executescript(db.DDL)
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "topics.csv"
            csv_path.write_text(
                "topic_id,title,priority\nT-1,One,3\n", encoding="utf-8"
            )
            with patch.object(orchestrator, "conn", return_value=memory_db):
                count = orchestrator._import_topics(
                    str(csv_path), batch_id="B-IMPORT"
                )

        self.assertEqual(count, 1)
        batch_id = memory_db.execute(
            "SELECT batch_id FROM jobs WHERE job_id='J-T-1'"
        ).fetchone()[0]
        self.assertEqual(batch_id, "B-IMPORT")
        memory_db.close()


if __name__ == "__main__":
    unittest.main()
