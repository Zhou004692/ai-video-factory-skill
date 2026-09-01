"""Human-readable Flow Guard report rendering and persistence."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import flow_guard as flow_guard_cli  # noqa: E402
from factory.flow_guard.html_report import render_html_report  # noqa: E402


def sample_payload(*, status: str = "passed") -> dict[str, object]:
    return {
        "generated_at": "2026-09-01T02:18:42+00:00",
        "provider_mode": "seedance_2_5_mock",
        "status": status,
        "duration_sec": 1485.211,
        "scenario_count": 88,
        "passed": 87 if status == "failed" else 88,
        "failed": 1 if status == "failed" else 0,
        "journey_count": 15,
        "journeys_passed": 15,
        "journeys_failed": 0,
        "paid_provider_calls": 0,
        "historical_mutation_count": 9,
        "historical_mutations_killed": 9,
        "historical_mutation_score_pct": 100.0,
        "unique_trace_count": 24,
        "duplicate_trace_scenarios": 64,
        "coverage": {
            "semantic_check_count": 9,
            "semantic_checks": ["shot_within_master", "final_media_decodable"],
            "executed_node_type_count": 6,
            "executed_node_types": ["master", "local_compose"],
            "dispatch_contract_action_count": 24,
            "dispatch_contract_coverage_pct": 100.0,
            "executed_console_action_count": 11,
            "console_action_count": 24,
            "console_action_coverage_pct": 45.83,
            "missing_console_actions": ["retry_failed_job"],
            "graph_actions": ["review_final_video"],
            "ui_dispatch_actions": ["submit_publish"],
            "integrated_backend_actions": ["review_continuity_anchors"],
            "worker_node_types": ["master"],
            "source_types": ["manual"],
            "provider_response_types": ["replay_success"],
        },
        "mutation_calibration": {
            "status": "passed",
            "mutation_count": 9,
            "killed": 9,
            "survived": 0,
            "score_pct": 100.0,
            "results": [{
                "mutation_id": "missing_shot_master",
                "expected_code": "shot_master_missing",
                "killed": True,
                "observed_codes": ["shot_master_missing"],
            }],
        },
        "historical_incident_replays": {
            "status": "passed",
            "incident_count": 5,
            "passed": 5,
            "failed": 0,
            "results": [{
                "incident_id": "test21",
                "title": "视频手势与说话人解析",
                "status": "passed",
                "expected_guards": ["gesture_preserved", "speaker_bound"],
                "test_count": 2,
            }],
        },
        "video_source_golden": {
            "status": "passed",
            "source_type": "video_replay",
            "analysis_frame_count": 3,
            "passed_guards": [
                "gesture_preserved", "emotion_preserved",
                "speaker_cluster_preserved",
            ],
            "failed_guards": [],
        },
        "production_shadow_audit": {
            "status": "passed",
            "database_path": "data/factory.db",
            "audited_job_count": 24,
            "action_required_job_count": 1,
            "review_required_job_count": 2,
            "regression_codes": [],
            "jobs": [{
                "job_id": "J-PROD-25", "state": "ACTION_REQUIRED",
                "paused_node_count": 1, "regression_codes": [],
            }],
        },
        "journey_suite": {
            "journeys": [{
                "scenario_id": "manual_three_person_multiscene",
                "status": "passed",
                "final_state": "READY_TO_PUBLISH",
                "character_count": 3,
                "scene_count": 3,
                "background_voiceover_master_count": 0,
                "semantic_findings": [],
                "structural_findings": [],
                "media_findings": [],
            }],
            "browser_suite": {
                "journeys": [{
                    "scenario_id": "management_fault_decisions",
                    "status": "passed",
                    "scenario_count": 11,
                    "exit_code": 0,
                }],
            },
        },
        "reports": [{
            "scenario_id": "unsafe <script>alert(1)</script>",
            "status": status,
            "final_state": "FAILED" if status == "failed" else "READY_TO_PUBLISH",
            "findings": ([{
                "code": "shot_master_missing",
                "message": "director shot references missing master",
            }] if status == "failed" else []),
            "repairs": [],
        }],
    }


class FlowGuardHtmlReportTests(unittest.TestCase):
    def test_renders_readable_summary_coverage_journeys_and_mutations(self):
        html = render_html_report(sample_payload())

        self.assertIn("Flow Guard 零费用回归报告", html)
        self.assertIn("88 / 88", html)
        self.assertIn("15 / 15", html)
        self.assertIn("9 / 9", html)
        self.assertIn("24 分 45 秒", html)
        self.assertIn("覆盖与缺口", html)
        self.assertIn("Graph 模拟动作", html)
        self.assertIn("UI 实际派发", html)
        self.assertIn("后端真实处理", html)
        self.assertIn("Worker 实际节点", html)
        self.assertNotIn("浏览器动态动作", html)
        self.assertIn("retry_failed_job", html)
        self.assertIn("manual_three_person_multiscene", html)
        self.assertIn("management_fault_decisions", html)
        self.assertIn("missing_shot_master", html)
        self.assertIn("历史事故逐条回放", html)
        self.assertIn("test21", html)
        self.assertIn("gesture_preserved", html)
        self.assertIn("本地视频表演金标", html)
        self.assertIn("speaker_cluster_preserved", html)
        self.assertIn("正式库只读影子审计", html)
        self.assertIn("J-PROD-25", html)
        self.assertIn('href="latest.json"', html)

    def test_renders_failed_scenario_and_escapes_report_content(self):
        html = render_html_report(sample_payload(status="failed"))

        self.assertIn("失败路径与阻塞", html)
        self.assertIn("shot_master_missing", html)
        self.assertIn("unsafe &lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("unsafe <script>", html)

    def test_write_report_persists_json_and_sibling_html(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "flow-result.json"
            flow_guard_cli._write_report(report_path, sample_payload())
            html_path = report_path.with_suffix(".html")

            self.assertTrue(report_path.exists())
            self.assertTrue(html_path.exists())
            self.assertEqual(
                json.loads(report_path.read_text(encoding="utf-8"))["status"],
                "passed",
            )
            html = html_path.read_text(encoding="utf-8")
            self.assertIn('href="flow-result.json"', html)
            self.assertIn("Flow Guard 零费用回归报告", html)


if __name__ == "__main__":
    unittest.main()
