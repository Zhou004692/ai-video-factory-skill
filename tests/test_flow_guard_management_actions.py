"""Every backend video pause must have one reachable console action."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.flow_guard.management_actions import (  # noqa: E402
    BACKEND_VIDEO_ACTION_IDS,
    ui_video_action_ids,
)


class FlowGuardManagementActionTests(unittest.TestCase):
    def test_all_backend_actions_are_dispatched_by_the_console(self):
        source = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        supported = ui_video_action_ids(source)

        self.assertEqual(BACKEND_VIDEO_ACTION_IDS - supported, set())

    def test_diagnostic_action_opens_the_task_doctor(self):
        source = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )
        dispatcher = source.split("function runVideoNextAction", 1)[1].split(
            "function ", 1
        )[0]

        self.assertIn(
            "action==='preserve_and_diagnose'", dispatcher
        )
        self.assertIn("openTaskDoctor(job)", dispatcher)


if __name__ == "__main__":
    unittest.main()
