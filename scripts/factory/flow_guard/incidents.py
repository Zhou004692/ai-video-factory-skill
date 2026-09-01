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
    failure_signature: str
    detection_boundary: str
    expected_guards: tuple[str, ...]
    test_selectors: tuple[str, ...]


HISTORICAL_INCIDENTS = (
    HistoricalIncident(
        "test21",
        "视频片段手势与人物表演未被解析",
        "手势：无明显手势",
        "source_analysis → performance_ir",
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
        "说话人未确认 / 出镜人物未出现在人物表中",
        "source_performance_ir → script_normalization",
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
        "背景台词被强制分配给入镜人物",
        "script_approval → director_timeline → seedance_prompt",
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
        "情绪、语气、表情和动作退化为默认值",
        "performance_ir → creative_ir → localized_repair",
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
        "angle #1.story_arc is required / 多场景母片顺序错误",
        "topic_brief → script_approval → master_plan",
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
    HistoricalIncident(
        "frozen-runtime-master-drift",
        "冻结运行时母片与编译节点不一致",
        "frozen runtime masters do not match compiled master nodes",
        "production_graph_runner → frozen_contract_recovery",
        ("frozen_contract_checked", "unpaid_node_recompiled_before_retry"),
        (
            "tests.test_production_graph_runner.ProductionGraphRunnerTests."
            "test_runner_recovers_repaired_unpaid_contract_node",
            "tests.test_production_graph_runner.ProductionGraphRunnerTests."
            "test_runner_stops_before_handlers_when_frozen_inputs_diverge",
        ),
    ),
    HistoricalIncident(
        "equivalent-paid-graph-rebind",
        "等价修订误触付费生产图替换卡控",
        "paid production graph cannot be replaced by another spec",
        "production_service → production_graph.persist_graph",
        ("equivalent_spec_rebound", "paid_work_preserved"),
        (
            "tests.test_production_service.ProductionServiceTests."
            "test_compile_rebinds_equivalent_paid_graph_without_losing_paid_work",
        ),
    ),
    HistoricalIncident(
        "missing-candidate-preview",
        "候选母片没有预览文件时流程无法继续",
        "候选母片预览尚未就绪，请刷新后重试",
        "management_resume → candidate_review_projection",
        ("missing_preview_requeued_safely", "no_fake_review_artifact"),
        (
            "tests.test_management_console.ManagementServiceTests."
            "test_resume_failed_video_requeues_manual_review_without_preview_artifact",
        ),
    ),
    HistoricalIncident(
        "local-recompose-resume",
        "候选已接受但本地重组失败后无法自主继续",
        "local recompose failed: director shot references missing master",
        "candidate_accept → local_compose → resume_failed_video",
        ("accepted_decision_preserved", "local_only_resume"),
        (
            "tests.test_management_console.ManagementServiceTests."
            "test_accept_candidate_preserves_decision_when_local_recompose_fails",
            "tests.test_management_console.ManagementServiceTests."
            "test_resume_failed_all_ok_reruns_only_local_postproduction",
        ),
    ),
    HistoricalIncident(
        "reviewed-budget-node-reconcile",
        "远端母片已完成但本地节点仍停在预算确认",
        "reviewed master graph state is inconsistent: master:3=BUDGET_REQUIRED",
        "provider_reconcile → graph_node_projection",
        ("remote_result_supersedes_budget_pause", "review_state_converged"),
        (
            "tests.test_management_console.ManagementServiceTests."
            "test_graph_budget_node_reconciliation_converges_to_review_state",
        ),
    ),
    HistoricalIncident(
        "extra-meaningless-master",
        "母片计划无意义多出一个母片",
        "master_projection_mismatch: compiled/runtime/node masters differ",
        "production_spec → runtime_storyboard → graph_nodes",
        ("master_projection_consistent",),
        (
            "tests.test_flow_guard_semantics.FlowGuardSemanticInvariantTests."
            "test_detects_extra_or_missing_master_projection",
        ),
    ),
    HistoricalIncident(
        "director-shot-missing-master",
        "导演镜头引用不存在的母片",
        "local recompose failed: director shot #4 references missing master",
        "director_timeline → local_compose_preflight",
        ("shot_master_exists",),
        (
            "tests.test_flow_guard_semantics.FlowGuardSemanticInvariantTests."
            "test_detects_shot_referencing_missing_master",
        ),
    ),
    HistoricalIncident(
        "dialogue-master-empty-voiceover",
        "对白母片缺少台词导致生产中断",
        "dialogue master 1 has empty voiceover",
        "script_normalization → production_spec",
        ("dialogue_voiceover_present",),
        (
            "tests.test_flow_guard_semantics.FlowGuardSemanticInvariantTests."
            "test_detects_empty_dialogue_voiceover",
        ),
    ),
    HistoricalIncident(
        "unknown-character-or-speaker",
        "创作节拍引用人物表外的出镜者或说话人",
        "character_unknown / speaker_unknown: 人物未出现在人物表中",
        "script_approval → production_compiler",
        ("character_references_known", "speaker_references_known"),
        (
            "tests.test_flow_guard_semantics.FlowGuardSemanticInvariantTests."
            "test_detects_unknown_character_and_speaker",
        ),
    ),
    HistoricalIncident(
        "edit-shot-exceeds-master",
        "分镜时间越过母片边界",
        "edit shot 9 exceeds video master",
        "director_timeline → production_preflight",
        ("shot_within_master",),
        (
            "tests.test_flow_guard_semantics.FlowGuardSemanticInvariantTests."
            "test_detects_shot_exceeding_master_duration",
        ),
    ),
    HistoricalIncident(
        "master-dependency-missing",
        "母片依赖图指向不存在的母片",
        "master dependency references missing master",
        "master_dependency_graph → production_preflight",
        ("master_dependencies_exist",),
        (
            "tests.test_flow_guard_semantics.FlowGuardSemanticInvariantTests."
            "test_detects_dependency_referencing_missing_master",
        ),
    ),
    HistoricalIncident(
        "graph-edge-missing-node",
        "生产图边指向不存在的执行节点",
        "graph edge references missing node",
        "production_graph → execution_preflight",
        ("graph_edges_resolve",),
        (
            "tests.test_flow_guard_semantics.FlowGuardSemanticInvariantTests."
            "test_detects_graph_edge_referencing_missing_node",
        ),
    ),
)


def _manifest_errors(
    incidents: Sequence[HistoricalIncident],
) -> list[str]:
    errors: list[str] = []
    seen_ids: set[str] = set()
    for position, incident in enumerate(incidents, 1):
        incident_id = str(incident.incident_id or "").strip()
        if not incident_id:
            errors.append(f"incident #{position} has empty incident_id")
        elif incident_id in seen_ids:
            errors.append(f"duplicate incident_id: {incident_id}")
        seen_ids.add(incident_id)
        for field_name in ("title", "failure_signature", "detection_boundary"):
            if not str(getattr(incident, field_name, "") or "").strip():
                errors.append(f"{incident_id or position} has empty {field_name}")
        if not incident.expected_guards:
            errors.append(f"{incident_id or position} has no expected_guards")
        if not incident.test_selectors:
            errors.append(f"{incident_id or position} has no test_selectors")
    return errors


def run_historical_incident_replays(
    *,
    incidents: Sequence[HistoricalIncident] = HISTORICAL_INCIDENTS,
    command_runner: Callable[..., object] = subprocess.run,
) -> dict[str, object]:
    """Run each incident independently so failures stay attributable."""
    manifest_errors = _manifest_errors(incidents)
    if manifest_errors:
        return {
            "status": "failed",
            "incident_count": len(incidents),
            "passed": 0,
            "failed": len(incidents),
            "paid_provider_calls": 0,
            "manifest_errors": manifest_errors,
            "results": [],
        }
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
            "failure_signature": incident.failure_signature,
            "detection_boundary": incident.detection_boundary,
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
        "manifest_errors": [],
        "results": results,
    }
