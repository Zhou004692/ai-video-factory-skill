"""First-principles production planning, reuse, and economics tests."""
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


from factory import (  # noqa: E402
    asset_library,
    db,
    orchestrator,
    production_metrics,
    production_planner,
)
from factory import prompts  # noqa: E402
from factory.steps import s1_storyboard, s3_shots  # noqa: E402
import cli as cli_module  # noqa: E402


class MinimumPaidMasterTests(unittest.TestCase):
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

        self.assertIn("1—3", prompt)
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
                    ["plan", str(path), "--budget", "1700"],
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


class ProductionPlannerTests(unittest.TestCase):
    def setUp(self):
        self.memory_db = sqlite3.connect(":memory:")
        self.memory_db.row_factory = sqlite3.Row
        self.memory_db.executescript(db.DDL)

    def tearDown(self):
        self.memory_db.close()

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
        self.assertEqual(report["estimated_cost_cents"], 816)
        self.assertFalse(report["budget_ok"])

    def test_plan_flags_broll_without_tags_as_a_paid_risk(self):
        report = production_planner.analyze_storyboard(
            self.memory_db,
            {"masters": [{"idx": 1, "type": "broll", "duration": 6}]},
            budget_cents=500,
            resolution="720p",
        )

        self.assertEqual(report["paid_master_count"], 1)
        self.assertEqual(report["estimated_cost_cents"], 612)
        self.assertTrue(any("asset_tags" in item for item in report["warnings"]))

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
