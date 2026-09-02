"""Adaptive scheduling and workspace safety for continuous Flow Guard."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.flow_guard.scheduler import (  # noqa: E402
    choose_run,
    parse_report_full_timestamp,
    prune_stale_workspaces,
    resolve_workspace_root,
)
import flow_guard as flow_guard_cli  # noqa: E402


class FlowGuardSchedulingTests(unittest.TestCase):
    def test_ordinary_pipeline_change_uses_incident_backed_incremental_run(self):
        now = datetime(2026, 9, 2, 8, tzinfo=timezone.utc)

        decision = choose_run(
            ("scripts/factory/source_analysis.py",),
            last_full_at=now - timedelta(hours=2),
            now=now,
            full_interval=timedelta(hours=24),
        )

        self.assertEqual(decision.suite, "incremental")
        self.assertEqual(decision.mode, "incremental")
        self.assertTrue(decision.include_incidents)
        self.assertEqual(decision.full_baseline_at, now - timedelta(hours=2))
        self.assertIn("source_analysis.py", decision.reason)

    def test_incremental_report_preserves_full_baseline_across_watcher_restart(self):
        baseline = datetime(2026, 9, 2, 6, tzinfo=timezone.utc)
        payload = {
            "generated_at": "2026-09-02T08:00:00+00:00",
            "status": "passed", "paid_provider_calls": 0,
            "journey_count": 0,
            "execution": {
                "mode": "incremental", "suite": "incremental",
                "full_baseline_at": baseline.isoformat(),
            },
        }

        self.assertEqual(parse_report_full_timestamp(payload), baseline)

    def test_guard_or_management_ui_change_forces_full_run(self):
        now = datetime(2026, 9, 2, 8, tzinfo=timezone.utc)
        for changed in (
            "scripts/factory/flow_guard/runner.py",
            "scripts/management_ui/app.js",
            "scripts/factory/production_graph.py",
            "scripts/factory/production_compiler.py",
            "scripts/management_server.py",
        ):
            with self.subTest(changed=changed):
                decision = choose_run(
                    (changed,),
                    last_full_at=now - timedelta(minutes=5),
                    now=now,
                    full_interval=timedelta(hours=24),
                )
                self.assertEqual(decision.suite, "full")
                self.assertEqual(decision.mode, "full")

    def test_elapsed_full_interval_forces_full_even_without_changes(self):
        now = datetime(2026, 9, 2, 8, tzinfo=timezone.utc)

        decision = choose_run(
            (),
            last_full_at=now - timedelta(hours=25),
            now=now,
            full_interval=timedelta(hours=24),
        )

        self.assertEqual(decision.suite, "full")
        self.assertIn("24", decision.reason)


class FlowGuardWorkspaceTests(unittest.TestCase):
    def test_explicit_workspace_fails_before_run_when_space_is_insufficient(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "factory.flow_guard.scheduler.shutil.disk_usage",
            return_value=mock.Mock(free=512 * 1024 * 1024),
        ):
            with self.assertRaisesRegex(RuntimeError, "AI_VIDEO_FLOW_GUARD_TEMP_ROOT"):
                resolve_workspace_root(
                    explicit=Path(directory),
                    minimum_free_bytes=4 * 1024**3,
                )

    def test_auto_workspace_selects_volume_with_enough_headroom(self):
        with tempfile.TemporaryDirectory() as low, tempfile.TemporaryDirectory() as high:
            free_by_root = {
                Path(low).resolve(): 1 * 1024**3,
                Path(high).resolve(): 20 * 1024**3,
            }

            def fake_usage(path):
                resolved = Path(path).resolve()
                return mock.Mock(free=free_by_root[resolved])

            with mock.patch(
                "factory.flow_guard.scheduler.candidate_volume_roots",
                return_value=(Path(low), Path(high)),
            ), mock.patch(
                "factory.flow_guard.scheduler.shutil.disk_usage",
                side_effect=fake_usage,
            ):
                selected = resolve_workspace_root(
                    minimum_free_bytes=4 * 1024**3,
                )

            self.assertEqual(selected.parent, Path(high).resolve())
            self.assertEqual(selected.name, "ai-video-flow-guard")

    def test_prunes_only_dead_owned_run_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dead = root / "run-999999-dead"
            live = root / "run-123-live"
            unrelated = root / "user-content"
            for path in (dead, live, unrelated):
                path.mkdir()
                (path / "artifact.bin").write_bytes(b"generated")

            with mock.patch(
                "factory.flow_guard.scheduler.process_is_alive",
                side_effect=lambda pid: pid == 123,
            ):
                removed = prune_stale_workspaces(root)

            self.assertEqual(removed, (dead.resolve(),))
            self.assertFalse(dead.exists())
            self.assertTrue(live.exists())
            self.assertTrue(unrelated.exists())


class FlowGuardCliIntegrationTests(unittest.TestCase):
    def test_incremental_run_keeps_incident_replays_and_records_execution_mode(self):
        summary = {
            "status": "passed", "scenario_count": 7,
            "journey_count": 0, "historical_mutation_score_pct": 100.0,
            "paid_provider_calls": 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "latest.json"
            arguments = flow_guard_cli.build_parser().parse_args([
                "run", "--suite", "incremental", "--report", str(report),
                "--temp-root", directory, "--min-free-gb", "0",
            ])
            decision = choose_run(
                ("scripts/factory/source_analysis.py",),
                last_full_at=datetime.now(timezone.utc),
            )
            with mock.patch(
                "flow_guard.run_matrix", new=mock.AsyncMock(return_value=summary),
            ) as runner:
                result = flow_guard_cli.run_once(arguments, decision=decision)

            kwargs = runner.await_args.kwargs
            self.assertTrue(kwargs["include_incidents"])
            self.assertTrue(kwargs["include_shadow_audit"])
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["execution"]["mode"], "incremental")
            self.assertEqual(payload["execution"]["suite"], "incremental")
            self.assertEqual(result, 0)

    def test_latest_report_preserves_separate_full_and_incremental_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "latest.json"
            flow_guard_cli._write_report(report, {
                "status": "passed",
                "execution": {"mode": "full", "suite": "full"},
            })
            flow_guard_cli._write_report(report, {
                "status": "partial",
                "execution": {"mode": "incremental", "suite": "incremental"},
            })

            latest = json.loads(report.read_text(encoding="utf-8"))
            full = json.loads(
                report.with_name("latest-full.json").read_text(encoding="utf-8")
            )
            incremental = json.loads(
                report.with_name("latest-incremental.json").read_text(encoding="utf-8")
            )

        self.assertEqual(latest["status"], "partial")
        self.assertEqual(full["execution"]["mode"], "full")
        self.assertEqual(full["status"], "passed")
        self.assertEqual(incremental["execution"]["mode"], "incremental")

    def test_incremental_report_does_not_link_to_missing_full_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "latest.json"
            flow_guard_cli._write_report(report, {
                "status": "partial",
                "execution": {"mode": "incremental", "suite": "incremental"},
            })

            html = report.with_suffix(".html").read_text(encoding="utf-8")

        self.assertNotIn('href="latest-full.html"', html)

    def test_watch_once_uses_recent_full_report_as_incremental_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "latest.json"
            report.write_text(json.dumps({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "status": "passed", "paid_provider_calls": 0,
                "journey_count": 37,
                "execution": {
                    "mode": "full", "suite": "full",
                    "source_fingerprint": "current-source",
                },
            }), encoding="utf-8")
            arguments = flow_guard_cli.build_parser().parse_args([
                "watch", "--once", "--report", str(report),
                "--temp-root", directory, "--min-free-gb", "0",
            ])
            with mock.patch(
                "flow_guard.source_snapshot", return_value={},
            ), mock.patch(
                "flow_guard.source_fingerprint", return_value="current-source",
            ), mock.patch("flow_guard.run_once", return_value=0) as run:
                result = flow_guard_cli.watch(arguments)

            decision = run.call_args.kwargs["decision"]
            self.assertEqual(decision.mode, "incremental")
            self.assertEqual(decision.suite, "incremental")
            self.assertEqual(result, 0)

    def test_watch_restart_forces_full_when_report_source_fingerprint_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "latest.json"
            report.write_text(json.dumps({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "status": "passed", "paid_provider_calls": 0,
                "journey_count": 37,
                "execution": {
                    "mode": "full", "suite": "full",
                    "source_fingerprint": "old-source",
                },
            }), encoding="utf-8")
            arguments = flow_guard_cli.build_parser().parse_args([
                "watch", "--once", "--report", str(report),
                "--temp-root", directory, "--min-free-gb", "0",
            ])
            with mock.patch(
                "flow_guard.source_snapshot", return_value={},
            ), mock.patch(
                "flow_guard.source_fingerprint", return_value="new-source",
            ), mock.patch("flow_guard.run_once", return_value=0) as run:
                flow_guard_cli.watch(arguments)

            decision = run.call_args.kwargs["decision"]
            self.assertEqual(decision.mode, "full")
            self.assertIn("指纹", decision.reason)


if __name__ == "__main__":
    unittest.main()
