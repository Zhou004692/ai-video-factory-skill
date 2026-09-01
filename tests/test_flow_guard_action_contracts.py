"""Evidence contracts for every management-console production action."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.flow_guard.management_actions import (  # noqa: E402
    ACTION_EVIDENCE_REQUIREMENTS,
    BACKEND_VIDEO_ACTION_IDS,
    evaluate_action_evidence,
)


class FlowGuardActionContractTests(unittest.TestCase):
    def test_every_action_requires_ui_and_state_changes_require_stronger_evidence(self):
        self.assertEqual(
            set(ACTION_EVIDENCE_REQUIREMENTS), BACKEND_VIDEO_ACTION_IDS
        )
        self.assertTrue(all(
            "ui_dispatch" in requirements
            for requirements in ACTION_EVIDENCE_REQUIREMENTS.values()
        ))
        self.assertEqual(
            ACTION_EVIDENCE_REQUIREMENTS["confirm_budget"],
            frozenset({"ui_dispatch", "backend_action", "worker_action"}),
        )
        self.assertEqual(
            ACTION_EVIDENCE_REQUIREMENTS["repair_execution_contract"],
            frozenset({"ui_dispatch", "backend_action", "worker_action"}),
        )
        self.assertEqual(
            ACTION_EVIDENCE_REQUIREMENTS["replace_private_reference"],
            frozenset({"ui_dispatch", "backend_action"}),
        )
        self.assertEqual(
            ACTION_EVIDENCE_REQUIREMENTS["view_provider_account_guide"],
            frozenset({"ui_dispatch"}),
        )

    def test_evaluator_reports_exact_missing_evidence_per_action(self):
        result = evaluate_action_evidence({
            "ui_dispatch_actions": ["confirm_budget"],
            "integrated_backend_actions": ["confirm_budget"],
            "worker_actions": [],
        })
        budget = next(
            item for item in result if item["action_id"] == "confirm_budget"
        )
        guide = next(
            item for item in result
            if item["action_id"] == "view_provider_account_guide"
        )

        self.assertFalse(budget["complete"])
        self.assertEqual(budget["missing"], ["worker_action"])
        self.assertFalse(guide["complete"])
        self.assertEqual(guide["missing"], ["ui_dispatch"])


if __name__ == "__main__":
    unittest.main()
