import csv
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import db as db_module
from factory import orchestrator, prompts
from factory.steps import s2_character, s3_shots
import inject_storyboard


class StyleFreeTopicInputTests(unittest.TestCase):
    def test_csv_without_style_imports_without_a_default_preset(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "topics.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "topic_id",
                        "title",
                        "track",
                        "target_audience",
                        "priority",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "topic_id": "STYLEFREE01",
                    "title": "设备场地真实日常",
                    "track": "工程设备",
                    "target_audience": "设备租赁客户",
                    "priority": "5",
                })

            with patch.object(db_module, "_DB_PATH", tmp_path / "factory.db"):
                database = db_module.conn()
                try:
                    with patch.object(orchestrator, "conn", return_value=database):
                        self.assertEqual(
                            orchestrator._import_topics(str(csv_path)), 1
                        )
                    row = database.execute(
                        "SELECT style FROM jobs WHERE job_id='J-STYLEFREE01'"
                    ).fetchone()
                finally:
                    database.close()

            self.assertIsNone(row["style"])

    def test_storyboard_prompt_does_not_require_a_style_argument(self):
        prompt = prompts.storyboard_prompt(
            title="设备场地真实日常",
            track="工程设备",
            duration=30,
        )

        self.assertNotIn("风格底色", prompt)

    def test_manual_storyboard_injection_does_not_require_style_prefix(self):
        storyboard = {
            "characters": [],
            "narrator": {"voice_id": "test"},
            "shots": [
                {"idx": idx, "type": "broll", "prompt": "自然场景"}
                for idx in range(1, 6)
            ],
        }

        self.assertTrue(hasattr(inject_storyboard, "_validate_storyboard"))
        inject_storyboard._validate_storyboard(storyboard)

    def test_manual_injection_uses_masters_as_paid_generation_units(self):
        storyboard = {
            "production_profile": "standard",
            "production_mode": "native_master_local_director",
            "characters": [],
            "masters": [
                {"idx": 1, "type": "dialogue"},
                {"idx": 2, "type": "broll"},
            ],
            "shots": [
                {"idx": idx, "video_master_idx": 1, "duration": 4}
                for idx in range(1, 5)
            ],
        }

        self.assertTrue(hasattr(inject_storyboard, "_generation_units"))
        self.assertEqual(
            [unit["idx"] for unit in inject_storyboard._generation_units(storyboard)],
            [1, 2],
        )
        self.assertEqual(inject_storyboard._validate_storyboard(storyboard), 1)

    def test_manual_injection_accepts_one_master_director_plan(self):
        storyboard = {
            "production_profile": "standard",
            "production_mode": "native_master_local_director",
            "characters": [],
            "masters": [{"idx": 1, "type": "dialogue"}],
            "shots": [{"idx": 1, "video_master_idx": 1, "duration": 4}],
        }

        self.assertEqual(inject_storyboard._validate_storyboard(storyboard), 0)

    def test_manual_injection_preserves_ai_identity_metadata(self):
        runtime = inject_storyboard._runtime_character({
            "id": "linlan",
            "name": "林岚",
            "script_character_name": "林岚",
            "gender": "female",
            "generation_mode": "ai_generated",
            "identity_type": "fictional",
            "look": "虚构设备工程师",
        })

        self.assertEqual(runtime["generation_mode"], "ai_generated")
        self.assertEqual(runtime["script_character_name"], "林岚")
        self.assertEqual(runtime["identity_type"], "fictional")

    def test_runtime_prompt_builders_have_no_global_style_parameter(self):
        builders = (
            s2_character._build_prompt,
            s2_character._build_broll_prompt,
            s2_character._build_scene_voiceover_prompt,
            s3_shots._build_scene_voiceover_video_prompt,
            s3_shots._build_broll_video_prompt,
        )

        for builder in builders:
            with self.subTest(builder=builder.__name__):
                self.assertNotIn(
                    "style_prefix", inspect.signature(builder).parameters
                )

    def test_global_style_lookup_is_not_part_of_runtime_prompts(self):
        self.assertFalse(hasattr(prompts, "style_prefix"))
        self.assertFalse(hasattr(prompts, "load_styles"))
        self.assertNotIn(
            "style_code",
            inspect.signature(prompts.storyboard_prompt).parameters,
        )

    def test_manual_step_runner_forces_utf8_on_windows(self):
        source = (PROJECT_ROOT / "scripts" / "run_step.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('sys.stdout.reconfigure(encoding="utf-8"', source)
        self.assertIn('sys.stderr.reconfigure(encoding="utf-8"', source)


if __name__ == "__main__":
    unittest.main()
