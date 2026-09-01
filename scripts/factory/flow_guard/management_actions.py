"""Contract between backend video pauses and management-console actions."""
from __future__ import annotations

import re
from pathlib import Path


BACKEND_VIDEO_ACTION_IDS = {
    "configure_music_provider",
    "confirm_budget",
    "increase_job_budget",
    "preserve_and_diagnose",
    "publish_video",
    "recompile_production_spec",
    "reconcile_provider_task",
    "reconcile_rejected_provider_cost",
    "repair_execution_contract",
    "replace_copyright_reference",
    "replace_private_reference",
    "resume_failed_video",
    "resume_local_postproduction",
    "retry_automatically",
    "retry_failed_job",
    "retry_identity_anchor",
    "review_continuity_anchors",
    "review_final_video",
    "review_master_candidates",
    "review_quality_failure",
    "revise_output_prompt",
    "submit_publish",
    "view_progress",
    "view_provider_account_guide",
}

# Evidence is action-scoped. A browser click proves only UI reachability; it
# must not be used as evidence that a state-changing endpoint persisted data or
# that production work was actually scheduled.
_BACKEND_EVIDENCE_ACTIONS = {
    "confirm_budget",
    "increase_job_budget",
    "recompile_production_spec",
    "reconcile_provider_task",
    "reconcile_rejected_provider_cost",
    "repair_execution_contract",
    "replace_copyright_reference",
    "replace_private_reference",
    "resume_failed_video",
    "resume_local_postproduction",
    "retry_failed_job",
    "retry_identity_anchor",
    "review_continuity_anchors",
    "review_final_video",
    "review_master_candidates",
    "revise_output_prompt",
    "submit_publish",
}

_WORKER_EVIDENCE_ACTIONS = {
    "confirm_budget",
    "increase_job_budget",
    "repair_execution_contract",
    "resume_failed_video",
    "resume_local_postproduction",
    "retry_identity_anchor",
    "review_continuity_anchors",
}

ACTION_EVIDENCE_REQUIREMENTS = {
    action_id: frozenset(
        {"ui_dispatch"}
        | ({"backend_action"} if action_id in _BACKEND_EVIDENCE_ACTIONS else set())
        | ({"worker_action"} if action_id in _WORKER_EVIDENCE_ACTIONS else set())
    )
    for action_id in BACKEND_VIDEO_ACTION_IDS
}

_EVIDENCE_FIELD_BY_REQUIREMENT = {
    "ui_dispatch": "ui_dispatch_actions",
    "backend_action": "integrated_backend_actions",
    "worker_action": "worker_actions",
}

FLOW_TO_CONSOLE_ACTION = {
    "approve_continuity_anchors": "review_continuity_anchors",
    "review_candidate": "review_master_candidates",
}

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def normalize_flow_action(action_id: str) -> str:
    """Translate persisted Graph V2 actions to management-console actions."""
    action_id = str(action_id or "").strip()
    return FLOW_TO_CONSOLE_ACTION.get(action_id, action_id)


def ui_video_action_ids(source: str) -> set[str]:
    """Extract actions explicitly dispatched by runVideoNextAction()."""
    marker = "function runVideoNextAction"
    if marker not in source:
        return set()
    dispatcher = source.split(marker, 1)[1].split("function ", 1)[0]
    direct = set(re.findall(r"action==='([a-z_]+)'", dispatcher))
    grouped: set[str] = set()
    for body in re.findall(
        r"\[([^\]]+)\]\.includes\(action\)", dispatcher
    ):
        grouped.update(re.findall(r"'([a-z_]+)'", body))
    return direct | grouped


def console_dispatch_action_ids() -> set[str]:
    """Read the shipped console dispatcher instead of a duplicated manifest."""
    source = (
        PROJECT_ROOT / "scripts" / "management_ui" / "app.js"
    ).read_text(encoding="utf-8")
    return ui_video_action_ids(source)


def evaluate_action_evidence(
    evidence: dict[str, object] | None,
) -> list[dict[str, object]]:
    """Return the exact observed and missing proof for every console action."""
    payload = evidence or {}
    observed_by_kind = {
        requirement: {
            str(value).strip()
            for value in payload.get(field_name, []) or []
            if str(value).strip()
        }
        for requirement, field_name in _EVIDENCE_FIELD_BY_REQUIREMENT.items()
    }
    results: list[dict[str, object]] = []
    for action_id in sorted(ACTION_EVIDENCE_REQUIREMENTS):
        required = ACTION_EVIDENCE_REQUIREMENTS[action_id]
        observed = {
            requirement for requirement in required
            if action_id in observed_by_kind[requirement]
        }
        missing = required - observed
        results.append({
            "action_id": action_id,
            "required": sorted(required),
            "observed": sorted(observed),
            "missing": sorted(missing),
            "complete": not missing,
        })
    return results
