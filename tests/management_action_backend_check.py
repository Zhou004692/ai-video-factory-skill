"""Zero-cost integrated management-action proof used by Flow Guard Full."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from factory.flow_guard.evidence import BROWSER_EVIDENCE_PREFIX  # noqa: E402


ACTION_TESTS = (
    "tests.test_management_console.ManagementServiceTests."
    "test_confirm_video_budget_resumes_only_the_unpaid_budget_paused_node",
    "tests.test_management_console.ManagementServiceTests."
    "test_failed_job_can_rebuild_safe_director_plan_without_losing_cost_history",
    "tests.test_management_console.ManagementServiceTests."
    "test_resume_failed_video_preserves_completed_masters",
    "tests.test_management_console.ManagementServiceTests."
    "test_approve_continuity_anchors_queues_job_after_every_anchor_is_ready",
    "tests.test_management_console.ManagementServiceTests."
    "test_accept_candidate_queues_only_remaining_non_ok_masters",
    "tests.test_management_console.ManagementServiceTests."
    "test_rejected_job_reconciles_late_provider_cost_without_resuming_production",
    "tests.test_flow_guard_management_action_backend."
    "FlowGuardManagementActionBackendTests."
    "test_execution_contract_repair_persists_queue_and_dispatches_worker",
)

BACKEND_ACTIONS = {
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
    "review_master_candidates",
    "revise_output_prompt",
}

WORKER_ACTIONS = {
    "confirm_budget",
    "increase_job_budget",
    "repair_execution_contract",
    "resume_failed_video",
    "resume_local_postproduction",
    "retry_identity_anchor",
    "review_continuity_anchors",
}


def main() -> int:
    subprocess.run(
        [sys.executable, "-m", "unittest", "-v", *ACTION_TESTS],
        cwd=PROJECT_ROOT,
        check=True,
    )
    print(BROWSER_EVIDENCE_PREFIX + json.dumps({
        "integrated_backend_actions": sorted(BACKEND_ACTIONS),
        "worker_actions": sorted(WORKER_ACTIONS),
        "provider_response_types": ["mocked_service_integrations"],
    }, sort_keys=True))
    print("management action backend check: PASS (7 action contracts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
