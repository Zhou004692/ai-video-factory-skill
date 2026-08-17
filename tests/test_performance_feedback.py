"""Published-video feedback and source/creative learning tests."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import cli as cli_module  # noqa: E402
from factory import creative_learning, db, performance_feedback  # noqa: E402


class PerformanceFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.memory_db = sqlite3.connect(":memory:")
        self.memory_db.row_factory = sqlite3.Row
        self.memory_db.executescript(db.DDL)
        self.memory_db.execute(
            "INSERT INTO jobs(job_id, topic_id, title, state) "
            "VALUES('J-FEEDBACK','T-FEEDBACK','租赁销售别再靠腿跑','PUBLISHED')"
        )
        brief = {
            "source_score": {"overall": 86.5},
            "recommended_index": 0,
            "angles": [
                {"title": "推荐案"},
                {"title": "人工选择的第二案"},
                {"title": "第三案"},
            ],
        }
        self.memory_db.execute(
            "INSERT INTO topic_briefs(brief_id, status, recommended_title, "
            "brief_json, approved_topic_id, approved_job_id, "
            "approved_angle_index) VALUES(?,?,?,?,?,?,?)",
            (
                "BRIEF-FEEDBACK", "approved", "推荐案",
                json.dumps(brief, ensure_ascii=False), "T-FEEDBACK",
                "J-FEEDBACK", 1,
            ),
        )
        for source_id, title in (("SRC-A", "访谈"), ("SRC-B", "竞品视频")):
            self.memory_db.execute(
                "INSERT INTO script_sources(source_id, fingerprint, "
                "source_type, title, content) VALUES(?,?,?,?,?)",
                (source_id, f"fp-{source_id}", "text", title, title),
            )
            self.memory_db.execute(
                "INSERT INTO topic_brief_sources(brief_id, source_id) "
                "VALUES('BRIEF-FEEDBACK',?)", (source_id,),
            )
        self.memory_db.commit()

    def tearDown(self):
        self.memory_db.close()

    def test_record_snapshot_derives_completion_and_is_idempotent(self):
        snapshot_id = performance_feedback.record_snapshot(
            self.memory_db, "J-FEEDBACK", platform="weixin_channels",
            captured_at="2026-08-12T12:00:00+08:00", views=200,
            completed_views=80, likes=10, comments=2, shares=3, saves=5,
            follows=4, leads=2,
        )
        repeated_id = performance_feedback.record_snapshot(
            self.memory_db, "J-FEEDBACK", platform="weixin_channels",
            captured_at="2026-08-12T12:00:00+08:00", views=200,
            completed_views=100, likes=12,
        )

        self.assertEqual(snapshot_id, repeated_id)
        row = self.memory_db.execute(
            "SELECT * FROM performance_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        self.assertEqual(row["completion_rate"], 0.5)
        self.assertEqual(row["likes"], 12)
        self.assertEqual(
            self.memory_db.execute(
                "SELECT COUNT(*) FROM performance_snapshots"
            ).fetchone()[0],
            1,
        )

    def test_equivalent_timezone_instants_share_one_snapshot(self):
        first = performance_feedback.record_snapshot(
            self.memory_db, "J-FEEDBACK", platform="douyin",
            captured_at="2026-08-12T12:00:00+08:00", views=100,
        )
        repeated = performance_feedback.record_snapshot(
            self.memory_db, "J-FEEDBACK", platform="douyin",
            captured_at="2026-08-12T04:00:00Z", views=200,
        )

        self.assertEqual(first, repeated)
        row = self.memory_db.execute(
            "SELECT captured_at, views FROM performance_snapshots"
        ).fetchone()
        self.assertEqual(row["captured_at"], "2026-08-12T04:00:00+00:00")
        self.assertEqual(row["views"], 200)
        self.assertEqual(
            self.memory_db.execute(
                "SELECT COUNT(*) FROM performance_snapshots"
            ).fetchone()[0],
            1,
        )

    def test_record_snapshot_rejects_invalid_or_inconsistent_metrics(self):
        with self.assertRaisesRegex(ValueError, "job .* not found"):
            performance_feedback.record_snapshot(
                self.memory_db, "J-MISSING", platform="douyin", views=1,
            )
        with self.assertRaisesRegex(ValueError, "completion_rate"):
            performance_feedback.record_snapshot(
                self.memory_db, "J-FEEDBACK", platform="douyin", views=10,
                completion_rate=1.2,
            )
        with self.assertRaisesRegex(ValueError, "does not match"):
            performance_feedback.record_snapshot(
                self.memory_db, "J-FEEDBACK", platform="douyin", views=100,
                completed_views=20, completion_rate=0.8,
            )
        with self.assertRaisesRegex(ValueError, "likes"):
            performance_feedback.record_snapshot(
                self.memory_db, "J-FEEDBACK", platform="douyin", views=10,
                likes=-1,
            )
        self.memory_db.execute(
            "INSERT INTO jobs(job_id, topic_id, title, state) "
            "VALUES('J-DRAFT','T-DRAFT','未发布成片','COMPOSED')"
        )
        self.memory_db.commit()
        with self.assertRaisesRegex(ValueError, "not published"):
            performance_feedback.record_snapshot(
                self.memory_db, "J-DRAFT", platform="douyin", views=10,
            )

    def test_learning_report_uses_latest_snapshot_per_job_and_platform(self):
        performance_feedback.record_snapshot(
            self.memory_db, "J-FEEDBACK", platform="weixin_channels",
            captured_at="2026-08-12T10:00:00+08:00", views=100,
            completion_rate=0.4, likes=10,
        )
        performance_feedback.record_snapshot(
            self.memory_db, "J-FEEDBACK", platform="weixin_channels",
            captured_at="2026-08-13T10:00:00+08:00", views=200,
            completion_rate=0.5, average_watch_sec=12, likes=20, comments=5,
            follows=4, leads=2,
        )
        performance_feedback.record_snapshot(
            self.memory_db, "J-FEEDBACK", platform="douyin",
            captured_at="2026-08-13T11:00:00+08:00", views=300,
            completion_rate=0.6, average_watch_sec=18, likes=20, comments=5, shares=5,
            follows=6, leads=3,
        )

        report = performance_feedback.learning_report(self.memory_db)

        creative = report["creatives"][0]
        self.assertEqual(creative["selected_angle_index"], 1)
        self.assertEqual(creative["selected_title"], "人工选择的第二案")
        self.assertEqual(creative["views"], 500)
        self.assertAlmostEqual(creative["completion_rate"], 0.56)
        self.assertAlmostEqual(creative["engagement_rate"], 0.11)
        self.assertAlmostEqual(creative["follow_rate"], 0.02)
        self.assertAlmostEqual(creative["lead_rate"], 0.01)
        self.assertAlmostEqual(creative["average_watch_sec"], 15.6)
        self.assertEqual(len(report["sources"]), 2)
        self.assertEqual(report["sources"][0]["attribution"], "shared_exposure")
        self.assertEqual(report["score_bands"][0]["band"], "80-89")
        self.assertEqual(report["score_bands"][0]["views"], 500)

    def test_source_report_aggregates_shared_source_across_creatives(self):
        self.memory_db.execute(
            "INSERT INTO jobs(job_id, topic_id, title, state) "
            "VALUES('J-FEEDBACK-2','T-FEEDBACK-2','第二条成片','PUBLISHED')"
        )
        brief = {
            "source_score": {"overall": 75}, "recommended_index": 0,
            "angles": [{"title": "第二条选题"}, {"title": "备选"},
                       {"title": "备选三"}],
        }
        self.memory_db.execute(
            "INSERT INTO topic_briefs(brief_id, status, recommended_title, "
            "brief_json, approved_topic_id, approved_job_id, "
            "approved_angle_index) VALUES(?,?,?,?,?,?,?)",
            (
                "BRIEF-FEEDBACK-2", "approved", "第二条选题",
                json.dumps(brief, ensure_ascii=False), "T-FEEDBACK-2",
                "J-FEEDBACK-2", 0,
            ),
        )
        self.memory_db.execute(
            "INSERT INTO topic_brief_sources(brief_id, source_id) "
            "VALUES('BRIEF-FEEDBACK-2','SRC-A')"
        )
        self.memory_db.commit()
        performance_feedback.record_snapshot(
            self.memory_db, "J-FEEDBACK", platform="douyin", views=200,
            completion_rate=0.5,
        )
        performance_feedback.record_snapshot(
            self.memory_db, "J-FEEDBACK-2", platform="douyin", views=100,
            completion_rate=0.8,
        )

        report = performance_feedback.learning_report(self.memory_db)
        source_a = next(
            item for item in report["sources"] if item["source_id"] == "SRC-A"
        )

        self.assertEqual(len(report["sources"]), 2)
        self.assertEqual(source_a["views"], 300)
        self.assertEqual(source_a["creative_count"], 2)
        self.assertAlmostEqual(source_a["completion_rate"], 0.6)

    def test_learning_report_excludes_low_view_creatives(self):
        performance_feedback.record_snapshot(
            self.memory_db, "J-FEEDBACK", platform="douyin", views=99,
            completion_rate=0.8,
        )

        report = performance_feedback.learning_report(
            self.memory_db, min_views=100
        )

        self.assertEqual(report["creatives"], [])
        self.assertEqual(report["sources"], [])
        self.assertEqual(report["score_bands"], [])

    def test_cli_records_snapshot_and_prints_learning_report(self):
        runner = CliRunner()
        with patch.object(cli_module, "conn", return_value=self.memory_db):
            recorded = runner.invoke(
                cli_module.cli,
                [
                    "performance-add", "J-FEEDBACK", "--platform", "douyin",
                    "--captured-at", "2026-08-12T12:00:00+08:00",
                    "--views", "200", "--completion-rate", "0.5",
                    "--likes", "10", "--leads", "2",
                ],
            )
            report = runner.invoke(
                cli_module.cli, ["learning-report", "--min-views", "100"]
            )

        self.assertEqual(recorded.exit_code, 0, recorded.output)
        self.assertIn("snapshot recorded", recorded.output)
        self.assertEqual(report.exit_code, 0, report.output)
        self.assertIn("人工选择的第二案", report.output)
        self.assertIn("5000", report.output)  # completion rate in basis points

    def test_csv_import_accepts_chinese_headers_percentages_and_commas(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "douyin.csv"
            csv_path.write_text(
                "任务ID,平台,统计时间,播放量,完播率,平均观看时长,点赞数,评论数,转发数,收藏数,新增关注,线索数\n"
                'J-FEEDBACK,抖音,2026-08-12T20:00:00+08:00,"1,200",46%,13.8,80,12,9,21,17,3\n',
                encoding="utf-8-sig",
            )

            result = performance_feedback.import_csv(
                self.memory_db, csv_path
            )

        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["inserted"], 1)
        row = self.memory_db.execute(
            "SELECT * FROM performance_snapshots"
        ).fetchone()
        self.assertEqual(row["platform"], "douyin")
        self.assertEqual(row["views"], 1200)
        self.assertEqual(row["completion_rate"], 0.46)
        self.assertEqual(row["average_watch_sec"], 13.8)
        self.assertEqual(row["follows"], 17)

    def test_csv_import_uses_defaults_and_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "channels.csv"
            csv_path.write_text(
                "job_id,views,completed_views,likes\n"
                "J-FEEDBACK,200,80,10\n",
                encoding="utf-8",
            )

            result = performance_feedback.import_csv(
                self.memory_db, csv_path, default_platform="weixin_channels",
                default_captured_at="2026-08-12T20:00:00+08:00",
                dry_run=True,
            )

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(
            self.memory_db.execute(
                "SELECT COUNT(*) FROM performance_snapshots"
            ).fetchone()[0],
            0,
        )

    def test_csv_import_is_atomic_when_any_row_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "invalid.csv"
            csv_path.write_text(
                "job_id,platform,captured_at,views,completion_rate\n"
                "J-FEEDBACK,douyin,2026-08-12T20:00:00+08:00,200,50%\n"
                "J-MISSING,douyin,2026-08-12T20:00:00+08:00,300,60%\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "row 3.*J-MISSING"):
                performance_feedback.import_csv(self.memory_db, csv_path)

        self.assertEqual(
            self.memory_db.execute(
                "SELECT COUNT(*) FROM performance_snapshots"
            ).fetchone()[0],
            0,
        )

    def test_csv_reimport_updates_same_snapshot_instead_of_duplicating(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "repeat.csv"
            csv_path.write_text(
                "job_id,platform,captured_at,views\n"
                "J-FEEDBACK,douyin,2026-08-12T20:00:00+08:00,200\n",
                encoding="utf-8",
            )
            first = performance_feedback.import_csv(self.memory_db, csv_path)
            csv_path.write_text(
                "job_id,platform,captured_at,views\n"
                "J-FEEDBACK,douyin,2026-08-12T20:00:00+08:00,300\n",
                encoding="utf-8",
            )
            repeated = performance_feedback.import_csv(self.memory_db, csv_path)

        self.assertEqual(first["inserted"], 1)
        self.assertEqual(repeated["inserted"], 0)
        self.assertEqual(repeated["updated"], 1)
        row = self.memory_db.execute(
            "SELECT views FROM performance_snapshots"
        ).fetchone()
        self.assertEqual(row["views"], 300)
        self.assertEqual(
            self.memory_db.execute(
                "SELECT COUNT(*) FROM performance_snapshots"
            ).fetchone()[0],
            1,
        )

    def test_csv_duplicate_identity_uses_last_row_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "duplicate.csv"
            csv_path.write_text(
                "job_id,platform,captured_at,views\n"
                "J-FEEDBACK,douyin,2026-08-12T20:00:00+08:00,200\n"
                "J-FEEDBACK,douyin,2026-08-12T20:00:00+08:00,300\n",
                encoding="utf-8",
            )

            result = performance_feedback.import_csv(self.memory_db, csv_path)

        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["unique_snapshots"], 1)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(
            self.memory_db.execute(
                "SELECT views FROM performance_snapshots"
            ).fetchone()[0],
            300,
        )

    def test_csv_import_requires_explicit_snapshot_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "missing-time.csv"
            csv_path.write_text(
                "job_id,platform,views\nJ-FEEDBACK,douyin,200\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "row 2.*captured_at"):
                performance_feedback.import_csv(self.memory_db, csv_path)

    def test_cli_imports_performance_csv_with_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "cli.csv"
            csv_path.write_text(
                "任务ID,播放量,完播率\nJ-FEEDBACK,500,55%\n",
                encoding="utf-8-sig",
            )
            runner = CliRunner()
            with patch.object(cli_module, "conn", return_value=self.memory_db):
                invoked = runner.invoke(
                    cli_module.cli,
                    [
                        "performance-import", str(csv_path),
                        "--platform", "douyin", "--captured-at",
                        "2026-08-12T20:00:00+08:00", "--dry-run",
                    ],
                )

        self.assertEqual(invoked.exit_code, 0, invoked.output)
        self.assertIn("validated=1", invoked.output)
        self.assertIn("dry-run", invoked.output)
        self.assertEqual(
            self.memory_db.execute(
                "SELECT COUNT(*) FROM performance_snapshots"
            ).fetchone()[0],
            0,
        )

    def test_register_publication_is_idempotent_and_rejects_conflicts(self):
        publication_id = performance_feedback.register_publication(
            self.memory_db, "J-FEEDBACK", platform="douyin",
            item_id="740001", item_url="https://www.douyin.com/video/740001",
            title="租赁销售别再靠腿跑",
            published_at="2026-08-12T08:00:00+08:00",
        )
        repeated = performance_feedback.register_publication(
            self.memory_db, "J-FEEDBACK", platform="抖音",
            item_id="740001", item_url="https://www.douyin.com/video/740001",
            title="更新后的平台标题",
            published_at="2026-08-12T08:00:00+08:00",
        )

        self.assertEqual(publication_id, repeated)
        row = self.memory_db.execute(
            "SELECT * FROM platform_publications WHERE publication_id=?",
            (publication_id,),
        ).fetchone()
        self.assertEqual(row["platform"], "douyin")
        self.assertEqual(row["title"], "更新后的平台标题")
        self.assertEqual(row["published_at"], "2026-08-12T00:00:00+00:00")
        self.memory_db.execute(
            "INSERT INTO jobs(job_id, topic_id, title, state) "
            "VALUES('J-OTHER','T-OTHER','其他成片','PUBLISHED')"
        )
        self.memory_db.commit()
        with self.assertRaisesRegex(ValueError, "already mapped"):
            performance_feedback.register_publication(
                self.memory_db, "J-OTHER", platform="douyin",
                item_id="740001",
            )

    def test_performance_due_tasks_create_distinct_24_72_168_hour_windows(self):
        performance_feedback.register_publication(
            self.memory_db, "J-FEEDBACK", platform="douyin",
            item_id="740001", published_at="2026-08-12T00:00:00Z",
        )

        at_73_hours = performance_feedback.performance_due_tasks(
            self.memory_db, now="2026-08-15T01:00:00Z"
        )
        self.assertEqual(
            [item["window_hours"] for item in at_73_hours], [24, 72]
        )

        performance_feedback.record_snapshot(
            self.memory_db, "J-FEEDBACK", platform="douyin",
            captured_at="2026-08-13T06:00:00Z", views=100,
        )
        remaining = performance_feedback.performance_due_tasks(
            self.memory_db, now="2026-08-15T01:00:00Z"
        )
        self.assertEqual([item["window_hours"] for item in remaining], [72])

        performance_feedback.record_snapshot(
            self.memory_db, "J-FEEDBACK", platform="douyin",
            captured_at="2026-08-15T08:00:00Z", views=200,
        )
        final_window = performance_feedback.performance_due_tasks(
            self.memory_db, now="2026-08-19T01:00:00Z"
        )
        self.assertEqual([item["window_hours"] for item in final_window], [168])

    def test_management_ui_labels_performance_windows_as_manual_todos(self):
        html = (PROJECT_ROOT / "scripts" / "management_ui" / "index.html").read_text(
            encoding="utf-8"
        )
        js = (PROJECT_ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="performance-due"', html)
        self.assertIn("24 小时", js)
        self.assertIn("录入绩效", js)
        self.assertNotIn("自动生成绩效", js)

    def test_one_job_has_only_one_canonical_publication_per_platform(self):
        performance_feedback.register_publication(
            self.memory_db, "J-FEEDBACK", platform="douyin",
            item_id="740001",
        )

        with self.assertRaisesRegex(ValueError, "canonical publication"):
            performance_feedback.register_publication(
                self.memory_db, "J-FEEDBACK", platform="douyin",
                item_id="740002",
            )

    def test_douyin_native_export_resolves_job_by_item_id(self):
        performance_feedback.register_publication(
            self.memory_db, "J-FEEDBACK", platform="douyin",
            item_id="740001",
        )
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "douyin-native.csv"
            csv_path.write_text(
                "作品ID,作品标题,播放次数,完播率,平均播放时长,点赞量,评论量,分享量,收藏量,涨粉量\n"
                '740001,租赁销售别再靠腿跑,"1,500",48%,14.2,90,13,11,23,18\n',
                encoding="utf-8-sig",
            )

            result = performance_feedback.import_platform_csv(
                self.memory_db, csv_path, platform="douyin",
                captured_at="2026-08-13T20:00:00+08:00",
            )

        self.assertEqual(result["matched"], 1)
        row = self.memory_db.execute(
            "SELECT * FROM performance_snapshots"
        ).fetchone()
        self.assertEqual(row["job_id"], "J-FEEDBACK")
        self.assertEqual(row["views"], 1500)
        self.assertEqual(row["completion_rate"], 0.48)
        self.assertEqual(row["follows"], 18)
        metadata = json.loads(row["metadata_json"])
        self.assertEqual(metadata["platform_item_id"], "740001")
        self.assertEqual(metadata["adapter"], "douyin")

    def test_weixin_native_export_uses_its_header_aliases(self):
        performance_feedback.register_publication(
            self.memory_db, "J-FEEDBACK", platform="weixin_channels",
            item_id="WX-9001",
        )
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "channels-native.csv"
            csv_path.write_text(
                "视频ID,视频描述,播放量,完播率,平均观看时长(秒),点赞次数,评论次数,转发次数,收藏次数,新增关注人数\n"
                "WX-9001,设备租赁口播,800,52%,16.5,50,8,7,14,10\n",
                encoding="utf-8-sig",
            )

            result = performance_feedback.import_platform_csv(
                self.memory_db, csv_path, platform="视频号",
                captured_at="2026-08-13T20:00:00+08:00",
            )

        self.assertEqual(result["matched"], 1)
        row = self.memory_db.execute(
            "SELECT * FROM performance_snapshots"
        ).fetchone()
        self.assertEqual(row["platform"], "weixin_channels")
        self.assertEqual(row["average_watch_sec"], 16.5)
        self.assertEqual(row["shares"], 7)

    def test_native_export_is_atomic_when_item_mapping_is_missing(self):
        performance_feedback.register_publication(
            self.memory_db, "J-FEEDBACK", platform="douyin",
            item_id="740001",
        )
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "unmapped.csv"
            csv_path.write_text(
                "作品ID,播放次数,完播率\n"
                "740001,100,40%\n"
                "740999,200,50%\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "row 3.*740999.*not mapped"):
                performance_feedback.import_platform_csv(
                    self.memory_db, csv_path, platform="douyin",
                    captured_at="2026-08-13T20:00:00+08:00",
                )

        self.assertEqual(
            self.memory_db.execute(
                "SELECT COUNT(*) FROM performance_snapshots"
            ).fetchone()[0],
            0,
        )

    def test_cli_registers_publication_and_preflights_native_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "native-cli.csv"
            csv_path.write_text(
                "作品ID,播放次数,完播率\n740001,600,57%\n",
                encoding="utf-8",
            )
            runner = CliRunner()
            with patch.object(cli_module, "conn", return_value=self.memory_db):
                mapped = runner.invoke(
                    cli_module.cli,
                    ["publication-map", "J-FEEDBACK", "--platform", "douyin",
                     "--item-id", "740001", "--published-at",
                     "2026-08-12T08:00:00+08:00"],
                )
                imported = runner.invoke(
                    cli_module.cli,
                    ["performance-import-platform", str(csv_path),
                     "--platform", "douyin", "--captured-at",
                     "2026-08-13T20:00:00+08:00", "--dry-run"],
                )

        self.assertEqual(mapped.exit_code, 0, mapped.output)
        self.assertIn("publication mapped", mapped.output)
        self.assertEqual(imported.exit_code, 0, imported.output)
        self.assertIn("matched=1", imported.output)
        self.assertIn("dry-run", imported.output)
        self.assertEqual(
            self.memory_db.execute(
                "SELECT COUNT(*) FROM performance_snapshots"
            ).fetchone()[0],
            0,
        )

    def test_windowed_report_uses_last_snapshot_before_window_end(self):
        performance_feedback.register_publication(
            self.memory_db, "J-FEEDBACK", platform="douyin",
            item_id="740001", published_at="2026-08-12T00:00:00Z",
        )
        for captured_at, views in (
            ("2026-08-12T12:00:00Z", 100),
            ("2026-08-12T20:00:00Z", 200),
            ("2026-08-13T06:00:00Z", 500),
        ):
            performance_feedback.record_snapshot(
                self.memory_db, "J-FEEDBACK", platform="douyin",
                captured_at=captured_at, views=views, completion_rate=0.5,
            )

        report = performance_feedback.learning_report(
            self.memory_db, platform="douyin", window_hours=24,
            min_window_coverage=0.8,
        )

        self.assertEqual(report["platform"], "douyin")
        self.assertEqual(report["window_hours"], 24)
        self.assertEqual(report["creatives"][0]["views"], 200)
        self.assertEqual(report["creatives"][0]["snapshot_count"], 1)
        self.assertEqual(report["excluded"], [])

    def test_windowed_report_excludes_immature_and_missing_publish_time(self):
        performance_feedback.register_publication(
            self.memory_db, "J-FEEDBACK", platform="douyin",
            item_id="740001", published_at="2026-08-12T00:00:00Z",
        )
        performance_feedback.record_snapshot(
            self.memory_db, "J-FEEDBACK", platform="douyin",
            captured_at="2026-08-12T12:00:00Z", views=200,
            completion_rate=0.5,
        )
        self.memory_db.execute(
            "INSERT INTO jobs(job_id, topic_id, title, state) "
            "VALUES('J-NO-TIME','T-NO-TIME','无发布时间','PUBLISHED')"
        )
        brief = {
            "source_score": {"overall": 70}, "recommended_index": 0,
            "angles": [{"title": "无时间"}, {"title": "二"}, {"title": "三"}],
        }
        self.memory_db.execute(
            "INSERT INTO topic_briefs(brief_id, status, recommended_title, "
            "brief_json, approved_topic_id, approved_job_id, "
            "approved_angle_index) VALUES(?,?,?,?,?,?,?)",
            ("BRIEF-NO-TIME", "approved", "无时间",
             json.dumps(brief, ensure_ascii=False), "T-NO-TIME", "J-NO-TIME", 0),
        )
        self.memory_db.commit()
        performance_feedback.register_publication(
            self.memory_db, "J-NO-TIME", platform="douyin", item_id="740002",
        )
        performance_feedback.record_snapshot(
            self.memory_db, "J-NO-TIME", platform="douyin",
            captured_at="2026-08-13T12:00:00Z", views=300,
            completion_rate=0.5,
        )

        report = performance_feedback.learning_report(
            self.memory_db, platform="douyin", window_hours=24,
            min_window_coverage=0.8,
        )

        self.assertEqual(report["creatives"], [])
        reasons = {item["job_id"]: item["reason"] for item in report["excluded"]}
        self.assertEqual(reasons["J-FEEDBACK"], "window_not_mature")
        self.assertEqual(reasons["J-NO-TIME"], "published_at_missing")

    def test_platform_filter_keeps_only_requested_platform(self):
        performance_feedback.record_snapshot(
            self.memory_db, "J-FEEDBACK", platform="douyin", views=200,
            completion_rate=0.4,
        )
        performance_feedback.record_snapshot(
            self.memory_db, "J-FEEDBACK", platform="weixin_channels", views=500,
            completion_rate=0.8,
        )

        report = performance_feedback.learning_report(
            self.memory_db, platform="douyin"
        )

        self.assertEqual(report["creatives"][0]["views"], 200)

    def test_windowed_report_requires_one_platform(self):
        with self.assertRaisesRegex(ValueError, "platform is required"):
            performance_feedback.learning_report(
                self.memory_db, window_hours=24
            )

    def test_cli_learning_report_accepts_platform_and_window(self):
        runner = CliRunner()
        with patch.object(cli_module, "conn", return_value=self.memory_db), \
                patch.object(
                    cli_module.performance_feedback, "learning_report",
                    return_value={"creatives": [], "sources": [],
                                  "score_bands": [], "excluded": []},
                ) as report:
            invoked = runner.invoke(
                cli_module.cli,
                ["learning-report", "--platform", "douyin", "--window-hours",
                 "72", "--min-window-coverage", "0.8"],
            )

        self.assertEqual(invoked.exit_code, 0, invoked.output)
        self.assertEqual(report.call_args.kwargs["platform"], "douyin")
        self.assertEqual(report.call_args.kwargs["window_hours"], 72)
        self.assertEqual(report.call_args.kwargs["min_window_coverage"], 0.8)

    def test_creative_tags_use_controlled_vocabulary_and_are_idempotent(self):
        creative_learning.tag_angle(
            self.memory_db, "BRIEF-FEEDBACK", angle_index=1,
            hook_type="pain_point", narrative_pattern="problem_solution",
            proof_type="source_quote", cta_type="consult",
            notes="人工确认",
        )
        creative_learning.tag_angle(
            self.memory_db, "BRIEF-FEEDBACK", angle_index=1,
            hook_type="contrarian", narrative_pattern="problem_solution",
            proof_type="source_quote", cta_type="consult",
        )

        row = self.memory_db.execute(
            "SELECT * FROM creative_angle_tags WHERE brief_id=? AND angle_index=?",
            ("BRIEF-FEEDBACK", 1),
        ).fetchone()
        self.assertEqual(row["hook_type"], "contrarian")
        self.assertEqual(
            self.memory_db.execute(
                "SELECT COUNT(*) FROM creative_angle_tags"
            ).fetchone()[0], 1,
        )
        with self.assertRaisesRegex(ValueError, "hook_type"):
            creative_learning.tag_angle(
                self.memory_db, "BRIEF-FEEDBACK", angle_index=1,
                hook_type="magic_hook", narrative_pattern="problem_solution",
                proof_type="source_quote", cta_type="consult",
            )
        with self.assertRaisesRegex(ValueError, "angle_index"):
            creative_learning.tag_angle(
                self.memory_db, "BRIEF-FEEDBACK", angle_index=3,
                hook_type="pain_point", narrative_pattern="problem_solution",
                proof_type="source_quote", cta_type="consult",
            )

    def _add_tagged_job(self, suffix, *, title, views, completion_rate,
                        hook_type, narrative_pattern="problem_solution"):
        job_id = f"J-TAG-{suffix}"
        topic_id = f"T-TAG-{suffix}"
        brief_id = f"BRIEF-TAG-{suffix}"
        self.memory_db.execute(
            "INSERT INTO jobs(job_id, topic_id, title, state) VALUES(?,?,?,'PUBLISHED')",
            (job_id, topic_id, title),
        )
        brief = {
            "source_score": {"overall": 80}, "recommended_index": 0,
            "angles": [{"title": title}, {"title": "备选二"}, {"title": "备选三"}],
        }
        self.memory_db.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json,"
            "approved_topic_id,approved_job_id,approved_angle_index) "
            "VALUES(?,'approved',?,?,?,?,0)",
            (brief_id, title, json.dumps(brief, ensure_ascii=False), topic_id, job_id),
        )
        self.memory_db.commit()
        creative_learning.tag_angle(
            self.memory_db, brief_id, angle_index=0,
            hook_type=hook_type, narrative_pattern=narrative_pattern,
            proof_type="source_quote", cta_type="consult",
        )
        performance_feedback.register_publication(
            self.memory_db, job_id, platform="douyin", item_id=f"ITEM-{suffix}",
            published_at="2026-08-12T00:00:00Z",
        )
        performance_feedback.record_snapshot(
            self.memory_db, job_id, platform="douyin",
            captured_at="2026-08-12T22:00:00Z", views=views,
            completion_rate=completion_rate,
        )
        return job_id

    def test_strategy_report_aggregates_only_selected_angle_tags(self):
        self._add_tagged_job(
            "A", title="痛点一", views=200, completion_rate=0.5,
            hook_type="pain_point",
        )
        self._add_tagged_job(
            "B", title="痛点二", views=300, completion_rate=0.7,
            hook_type="pain_point",
        )
        self._add_tagged_job(
            "C", title="反常识", views=500, completion_rate=0.8,
            hook_type="contrarian", narrative_pattern="misconception_correction",
        )

        report = creative_learning.strategy_report(
            self.memory_db, platform="douyin", window_hours=24,
            min_views=100, min_samples=2,
        )

        hook = next(item for item in report["dimensions"] if item["dimension"] == "hook_type")
        self.assertEqual(len(hook["eligible"]), 1)
        self.assertEqual(hook["eligible"][0]["value"], "pain_point")
        self.assertEqual(hook["eligible"][0]["sample_count"], 2)
        self.assertEqual(hook["eligible"][0]["views"], 500)
        self.assertAlmostEqual(hook["eligible"][0]["completion_rate"], 0.62)
        self.assertEqual(hook["insufficient"][0]["value"], "contrarian")
        self.assertEqual(report["untagged"], [])
        self.assertIn("candidate", hook["recommendation"])

    def test_strategy_report_lists_untagged_selected_creatives(self):
        performance_feedback.register_publication(
            self.memory_db, "J-FEEDBACK", platform="douyin", item_id="740001",
            published_at="2026-08-12T00:00:00Z",
        )
        performance_feedback.record_snapshot(
            self.memory_db, "J-FEEDBACK", platform="douyin",
            captured_at="2026-08-12T22:00:00Z", views=200,
            completion_rate=0.5,
        )

        report = creative_learning.strategy_report(
            self.memory_db, platform="douyin", window_hours=24,
            min_views=100, min_samples=1,
        )

        self.assertEqual(report["untagged"], ["J-FEEDBACK"])
        self.assertTrue(all(not item["eligible"] for item in report["dimensions"]))

    def test_cli_tags_angle_and_prints_strategy_report(self):
        runner = CliRunner()
        with patch.object(cli_module, "conn", return_value=self.memory_db):
            tagged = runner.invoke(
                cli_module.cli,
                ["creative-tag", "BRIEF-FEEDBACK", "--angle", "2",
                 "--hook-type", "pain_point", "--narrative-pattern",
                 "problem_solution", "--proof-type", "source_quote",
                 "--cta-type", "consult"],
            )
            report = runner.invoke(
                cli_module.cli,
                ["strategy-report", "--platform", "douyin", "--window-hours",
                 "24", "--min-samples", "2"],
            )

        self.assertEqual(tagged.exit_code, 0, tagged.output)
        self.assertIn("creative tagged", tagged.output)
        self.assertEqual(report.exit_code, 0, report.output)
        self.assertIn("dimensions", report.output)


if __name__ == "__main__":
    unittest.main()
