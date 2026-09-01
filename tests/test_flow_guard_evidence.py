"""Evidence-led coverage must not conflate simulated and integrated paths."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.flow_guard.evidence import (  # noqa: E402
    BROWSER_EVIDENCE_PREFIX,
    CoverageEvidence,
    parse_browser_evidence,
)


class FlowGuardEvidenceTests(unittest.TestCase):
    def test_evidence_sources_remain_separate(self):
        evidence = CoverageEvidence()
        evidence.record("graph_action", "review_final_video")
        evidence.record("ui_dispatch", "submit_publish")
        evidence.record("backend_action", "review_continuity_anchors")
        evidence.record("worker_node", "master")
        evidence.record("source_type", "video_replay")
        evidence.record("provider_response", "replay_success")

        payload = evidence.as_dict()

        self.assertEqual(payload["graph_actions"], ["review_final_video"])
        self.assertEqual(payload["ui_dispatch_actions"], ["submit_publish"])
        self.assertEqual(
            payload["integrated_backend_actions"],
            ["review_continuity_anchors"],
        )
        self.assertEqual(payload["worker_node_types"], ["master"])
        self.assertEqual(payload["source_types"], ["video_replay"])
        self.assertEqual(payload["provider_response_types"], ["replay_success"])

    def test_browser_evidence_requires_runtime_sentinel(self):
        payload, errors = parse_browser_evidence("browser suite passed")

        self.assertEqual(payload, CoverageEvidence().as_dict())
        self.assertEqual(errors, ["browser_evidence_missing"])

    def test_browser_evidence_parses_only_explicit_runtime_facts(self):
        stdout = (
            "browser suite passed\n"
            + BROWSER_EVIDENCE_PREFIX
            + '{"ui_dispatch_actions":["submit_publish"],'
              '"integrated_backend_actions":["review_final_video"],'
              '"worker_node_types":["master"],'
              '"source_types":["manual"],'
              '"provider_response_types":["replay_success"]}'
        )

        payload, errors = parse_browser_evidence(stdout)

        self.assertEqual(errors, [])
        self.assertEqual(payload["ui_dispatch_actions"], ["submit_publish"])
        self.assertEqual(
            payload["integrated_backend_actions"], ["review_final_video"]
        )
        self.assertNotIn("submit_publish", payload["graph_actions"])


if __name__ == "__main__":
    unittest.main()
