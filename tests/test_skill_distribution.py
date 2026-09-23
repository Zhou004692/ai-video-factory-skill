from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class SkillDistributionTests(unittest.TestCase):
    def test_distributed_template_defaults_to_seedance_native_audio(self):
        template = (ROOT / "templates" / "env.example").read_text(
            encoding="utf-8"
        )

        self.assertIn("DIALOGUE_AUDIO_MODE=seedance_native", template)

    def test_distributed_template_defaults_anchor_generation_to_seedance_25(self):
        template = (ROOT / "templates" / "env.example").read_text(
            encoding="utf-8"
        )

        self.assertIn("CHARACTER_ANCHOR_GENERATOR=seedance_2_5", template)
        self.assertIn("SCENE_ANCHOR_GENERATOR=seedance_2_5", template)

    def test_build_distribution_contains_runtime_but_excludes_private_state(self):
        from distribute_skill import audit_distribution, build_distribution

        with tempfile.TemporaryDirectory() as tmp:
            package = build_distribution(ROOT, Path(tmp))

            self.assertEqual(package.name, "ai-video-factory")
            for relative in (
                "VERSION",
                "CHANGELOG.md",
                "SKILL.md",
                "agents/openai.yaml",
                "scripts/cli.py",
                "scripts/bootstrap.py",
                "scripts/distribute_skill.py",
                "scripts/management_server.py",
                "scripts/open_management.ps1",
                "scripts/management_ui/index.html",
                "scripts/management_ui/styles.css",
                "scripts/management_ui/app.js",
                "scripts/doctor.py",
                "scripts/install.py",
                "scripts/factory/script_drafts.py",
                "templates/env.example",
                "references/characters/roster.yaml",
                "references/quality_canary_scripts.json",
            ):
                self.assertTrue((package / relative).is_file(), relative)

            for relative in (
                ".env",
                ".git",
                ".venv",
                "data/factory.db",
                "docs",
                "diagrams",
                "tests",
                "references/characters/sales_xiaoneng",
                "SETUP_NEW_MACHINE.md",
                "安装 AI 视频工厂.cmd",
                "打开 AI 视频工厂.cmd",
                "scripts/install.ps1",
                "scripts/install_desktop_launcher.ps1",
                "scripts/broll_demo_test.py",
                "scripts/smoke_check.py",
                "scripts/factory/experiments",
                "references/prompts/topic_cases.md",
                "references/prompts/topic_library.md",
            ):
                self.assertFalse((package / relative).exists(), relative)

            roster = (package / "references/characters/roster.yaml").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("小玲", roster)
            self.assertNotIn("sales_xiaoneng", roster)

            report = audit_distribution(package)
            self.assertTrue(report["ok"], json.dumps(report, ensure_ascii=False))

    def test_audit_rejects_secrets_and_machine_bound_paths(self):
        from distribute_skill import audit_distribution

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "ai-video-factory"
            package.mkdir()
            (package / "bad.txt").write_text(
                "ARK_API_KEY=secret-value\nC:\\Users\\someone\\private.png",
                encoding="utf-8",
            )

            report = audit_distribution(package)

            self.assertFalse(report["ok"])
            rules = {finding["rule"] for finding in report["findings"]}
            self.assertIn("credential-assignment", rules)
            self.assertIn("machine-bound-path", rules)

    def test_audit_rejects_structured_secrets_bearer_tokens_and_jwts(self):
        from distribute_skill import audit_distribution

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "ai-video-factory"
            package.mkdir()
            (package / "bad.json").write_text(
                json.dumps({"ARK_API_KEY": "actual-secret-value"}),
                encoding="utf-8",
            )
            (package / "bad.yaml").write_text(
                "VOLC_SK: actual-secret-value\n", encoding="utf-8"
            )
            (package / "bad.py").write_text(
                "api_key = 'actual-secret-value'\n"
                "authorization = 'Bearer abc.def.ghi-secret'\n"
                "session = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature'\n",
                encoding="utf-8",
            )

            report = audit_distribution(package)

            self.assertFalse(report["ok"])
            rules = {finding["rule"] for finding in report["findings"]}
            self.assertIn("credential-assignment", rules)
            self.assertIn("bearer-token", rules)
            self.assertIn("jwt-token", rules)

    def test_audit_rejects_private_media_and_any_local_database(self):
        from distribute_skill import audit_distribution

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "ai-video-factory"
            (package / "references" / "characters").mkdir(parents=True)
            (package / "data").mkdir()
            (package / "references" / "characters" / "person.jpg").write_bytes(
                b"not-a-real-image"
            )
            (package / "data" / "local.sqlite3").write_bytes(b"SQLite format 3")

            report = audit_distribution(package)

            self.assertFalse(report["ok"])
            rules = {finding["rule"] for finding in report["findings"]}
            self.assertIn("private-media", rules)
            self.assertIn("local-database", rules)

    def test_doctor_supports_script_only_and_video_profiles_without_network(self):
        from doctor import diagnose

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / ".env").write_text("ARK_API_KEY=configured\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {"FACTORY_RUNTIME_HOME": str(root / "runtime-home")},
                clear=False,
            ):
                script = diagnose(root, profile="script")
                video = diagnose(root, profile="video")

            self.assertTrue(script["ready"], script)
            self.assertEqual(script["paid_provider_calls"], 0)
            self.assertFalse(video["ready"], video)
            self.assertFalse(video["checks"]["credentials"]["ok"])

    def test_doctor_accepts_claude_as_the_script_provider(self):
        from doctor import diagnose

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / ".env").write_text(
                "DEFAULT_LLM_PROVIDER=claude\nANTHROPIC_API_KEY=configured\n",
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {
                    "DEFAULT_LLM_PROVIDER": "claude",
                    "ANTHROPIC_API_KEY": "configured",
                    "FACTORY_RUNTIME_HOME": str(root / "runtime-home"),
                },
                clear=False,
            ):
                report = diagnose(root, profile="script")

            self.assertTrue(report["ready"], report)
            self.assertEqual(
                report["checks"]["credentials"]["detail"]["provider"],
                "claude",
            )

    def test_doctor_video_profile_accepts_new_asr_api_key(self):
        from doctor import diagnose

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / ".env").write_text(
                "ARK_API_KEY=ark\nVOLC_TTS_APPID=tts-app\n"
                "VOLC_TTS_API_KEY=tts-key\nVOLC_ASR_API_KEY=asr-key\n"
                "VOLC_AK=ak\nVOLC_SK=sk\nTOS_BUCKET=bucket\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"FACTORY_RUNTIME_HOME": str(root / "runtime-home")},
                clear=False,
            ), patch("doctor.shutil.which", return_value="binary"):
                report = diagnose(root, profile="video")

        self.assertTrue(report["checks"]["credentials"]["ok"])

    def test_doctor_video_native_audio_mode_does_not_require_tts(self):
        from doctor import diagnose

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / ".env").write_text(
                "DIALOGUE_AUDIO_MODE=seedance_native\n"
                "ARK_API_KEY=ark\nVOLC_AK=ak\nVOLC_SK=sk\nTOS_BUCKET=bucket\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"FACTORY_RUNTIME_HOME": str(root / "runtime-home")},
                clear=False,
            ), patch("doctor.shutil.which", return_value="binary"):
                report = diagnose(root, profile="video")

        self.assertTrue(report["checks"]["credentials"]["ok"], report)
        self.assertNotIn(
            "VOLC_TTS_APPID", report["checks"]["credentials"]["detail"]
        )

    def test_learning_profile_needs_no_model_credentials(self):
        from doctor import diagnose

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / ".env").write_text("PUBLISH_MODE=manual\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {"FACTORY_RUNTIME_HOME": str(root / "runtime-home")},
                clear=False,
            ):
                report = diagnose(root, profile="learning")

            self.assertTrue(report["ready"], report)
            self.assertEqual(report["paid_provider_calls"], 0)

    def test_bootstrap_reports_missing_configuration_without_paid_calls(self):
        from bootstrap import bootstrap

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / ".env").write_text(
                "DEFAULT_LLM_PROVIDER=doubao\nARK_API_KEY=<required>\n",
                encoding="utf-8",
            )

            with patch.dict("os.environ", {}, clear=True):
                report = bootstrap(
                    root,
                    profile="script",
                    ensure_install=False,
                    python_executable=Path(sys.executable),
                )

            self.assertEqual(report["status"], "configuration_required")
            self.assertEqual(report["missing_fields"], ["ARK_API_KEY"])
            self.assertEqual(report["paid_provider_calls"], 0)
            self.assertNotIn("SRC-", json.dumps(report))

    def test_bootstrap_video_native_audio_mode_does_not_request_tts(self):
        from bootstrap import bootstrap

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / ".env").write_text(
                "DIALOGUE_AUDIO_MODE=seedance_native\n"
                "ARK_API_KEY=ark\nVOLC_AK=ak\nVOLC_SK=sk\nTOS_BUCKET=bucket\n",
                encoding="utf-8",
            )

            with patch.dict("os.environ", {}, clear=True):
                report = bootstrap(
                    root,
                    profile="video",
                    ensure_install=False,
                    python_executable=Path(sys.executable),
                )

        self.assertTrue(report["ready"], report)
        self.assertNotIn("VOLC_TTS_APPID", report["missing_fields"])

    def test_skill_runtime_initializer_preserves_existing_env_and_initializes_runtime(self):
        from install import install

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"FACTORY_RUNTIME_HOME": str(Path(tmp) / "runtime-home")},
            clear=False,
        ):
            package = Path(tmp) / "ai-video-factory"
            (package / "templates").mkdir(parents=True)
            (package / "scripts" / "factory").mkdir(parents=True)
            (package / "templates" / "env.example").write_text(
                "ARK_API_KEY=<required>\n", encoding="utf-8"
            )
            (package / "scripts" / "factory" / "__init__.py").write_text(
                "", encoding="utf-8"
            )
            (package / ".env").write_text("KEEP=1\n", encoding="utf-8")

            first = install(package, create_venv=False, install_dependencies=False)
            second = install(package, create_venv=False, install_dependencies=False)

            external_env = Path(first["env_file"])
            self.assertEqual(external_env.read_text(encoding="utf-8"), "KEEP=1\n")
            self.assertTrue(Path(first["data_dir"]).is_dir())
            self.assertFalse((package / "data").exists())
            self.assertFalse(first["env_created"])
            self.assertFalse(second["env_created"])

    def test_clean_package_passes_audit_and_cli_import_smoke(self):
        from distribute_skill import audit_distribution, build_distribution

        with tempfile.TemporaryDirectory() as tmp:
            package = build_distribution(ROOT, Path(tmp))
            report = audit_distribution(package)
            self.assertTrue(report["ok"], json.dumps(report, ensure_ascii=False))

            result = subprocess.run(
                [sys.executable, str(package / "scripts" / "cli.py"), "--help"],
                cwd=package,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("script-generate", result.stdout)

    def test_build_archive_wraps_one_clean_skill_directory(self):
        from distribute_skill import build_archive

        with tempfile.TemporaryDirectory() as tmp:
            archive = build_archive(ROOT, Path(tmp))

            version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
            self.assertEqual(archive.name, f"ai-video-factory-{version}.zip")
            self.assertTrue(archive.is_file())
            with zipfile.ZipFile(archive) as bundle:
                names = set(bundle.namelist())
                self.assertIn("ai-video-factory/SKILL.md", names)
                self.assertIn("ai-video-factory/agents/openai.yaml", names)
                self.assertNotIn("ai-video-factory/.env", names)
                self.assertFalse(any("sales_xiaoneng" in name for name in names))


if __name__ == "__main__":
    unittest.main()
