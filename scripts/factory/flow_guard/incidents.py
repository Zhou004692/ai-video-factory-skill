"""Explicit zero-cost replays for user-visible production regressions."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import Callable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class HistoricalIncident:
    incident_id: str
    title: str
    expected_guards: tuple[str, ...]
    test_selectors: tuple[str, ...]


HISTORICAL_INCIDENTS = (
    HistoricalIncident(
        "test21",
        "视频片段手势与人物表演未被解析",
        ("gesture_preserved", "visible_performance_preserved"),
        (
            "tests.test_script_sources.SourceAnalysisTests."
            "test_structure_uses_positive_observed_gesture_over_negative_summary",
            "tests.test_source_performance_ir.SourcePerformanceIRTests."
            "test_frame_observation_preserves_visible_person_performance",
        ),
    ),
    HistoricalIncident(
        "test22",
        "说话人无法归属并生成空台词或未知人物",
        ("speaker_review_required", "no_first_character_guess"),
        (
            "tests.test_source_performance_ir.SourcePerformanceIRTests."
            "test_structure_report_marks_unresolved_speaker_for_review",
            "tests.test_source_performance_ir.SourcePerformanceIRTests."
            "test_reconstruction_never_guesses_first_visible_character",
            "tests.test_script_drafts.ScriptDraftGenerationTests."
            "test_normalization_rejects_missing_speaker_instead_of_adding_presenter",
        ),
    ),
    HistoricalIncident(
        "test23",
        "背景台词被强制入镜且分镜越过母片边界",
        ("offscreen_voice_stays_offscreen", "shot_within_master"),
        (
            "tests.test_script_drafts.ScriptDraftGenerationTests."
            "test_director_validation_still_rejects_real_master_overrun",
            "tests.test_script_drafts.ScriptDraftApprovalTests."
            "test_offscreen_dialogue_is_not_promoted_to_an_on_screen_character",
            "tests.test_video_upgrade.Seedance25DialogueTests."
            "test_cinematic_prompt_keeps_offscreen_voice_out_of_frame_and_preserves_performance",
        ),
    ),
    HistoricalIncident(
        "test24",
        "人物表情语气丢失与局部修复边界失真",
        ("performance_direction_preserved", "localized_repair_bounded"),
        (
            "tests.test_source_performance_ir.SourcePerformanceIRTests."
            "test_script_normalization_and_creative_ir_keep_performance_direction",
            "tests.test_incident_replays.IncidentReplayTests."
            "test_all_incidents_replay_through_current_production_boundaries",
        ),
    ),
    HistoricalIncident(
        "test25",
        "简报故事弧缺失与多场景母片顺序错误",
        ("story_arc_repaired", "scene_order_preserved"),
        (
            "tests.test_script_sources.TopicBriefTests."
            "test_generate_brief_repairs_missing_story_arc_from_angle_content",
            "tests.test_script_drafts.ScriptDraftApprovalTests."
            "test_broll_is_split_by_story_scene_and_carries_only_that_scene_cast",
            "tests.test_video_upgrade.SeedanceGenerationGateTests."
            "test_same_scene_broll_masters_stay_sequential",
        ),
    ),
)


def run_historical_incident_replays(
    *,
    incidents: Sequence[HistoricalIncident] = HISTORICAL_INCIDENTS,
    command_runner: Callable[..., object] = subprocess.run,
) -> dict[str, object]:
    """Run each incident independently so failures stay attributable."""
    results: list[dict[str, object]] = []
    for incident in incidents:
        try:
            process = command_runner(
                [
                    sys.executable, "-m", "unittest", "-q",
                    *incident.test_selectors,
                ],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=300,
            )
            exit_code = int(getattr(process, "returncode", 1))
            stdout = str(getattr(process, "stdout", "") or "")
            stderr = str(getattr(process, "stderr", "") or "")
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            stdout = str(exc.stdout or "")
            stderr = f"incident replay timed out after {exc.timeout} seconds"
        results.append({
            "incident_id": incident.incident_id,
            "title": incident.title,
            "status": "passed" if exit_code == 0 else "failed",
            "expected_guards": list(incident.expected_guards),
            "test_count": len(incident.test_selectors),
            "test_selectors": list(incident.test_selectors),
            "exit_code": exit_code,
            "paid_provider_calls": 0,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": stderr[-2000:],
        })
    failed = sum(item["status"] != "passed" for item in results)
    return {
        "status": "passed" if not failed else "failed",
        "incident_count": len(results),
        "passed": len(results) - failed,
        "failed": failed,
        "paid_provider_calls": 0,
        "results": results,
    }
