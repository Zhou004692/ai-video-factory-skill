"""Contract between backend video pauses and management-console actions."""
from __future__ import annotations

import re


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
