"""Fail-closed registration of the bounded browser/CLI recovery evidence."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from factory.flow_guard import strict_recovery
from factory.flow_guard.assurance import build_evidence_boundaries


def evidence():
    return {
        "status": "passed", "scope": "strict_master_download_browser_recovery",
        "ui_actions": ["resume_failed_video", "review_final_video"],
        "posts": ["/api/videos/J-strict-master/resume", "/api/videos/J-strict-master/review"],
        "workers": [{
            "pid": pid, "launcher_pid": pid, "phase": phase, "status": "passed",
            "http_requests_matched": count, "expected_requests": count,
            "violations": 0, "process_tree_verified": False,
            "worker_entrypoint": "cli.py produce-graph --dispatched",
            "job_state": "FAILED" if phase == "corrupt" else "COMPOSED",
            "requests": [{"method": "POST" if phase == "corrupt" and i == 0 else "GET",
                          "route": "/contents/generations/tasks" if phase == "corrupt" and i == 0 else "/fixture"}
                         for i in range(count)],
        } for pid, phase, count in [(101, "corrupt", 4), (102, "recover", 9)]],
        "paid_provider_calls": 0, "external_requests": 0,
        "final_state": "READY_TO_PUBLISH", "simulated_cost_cents": 7, "media_http_statuses": [206],
        "recognition_accuracy": "not_run", "actual_content_quality": "not_run",
    }


class StrictRecoveryRunnerTests(unittest.TestCase):
    def invoke(self, payload=None, *, returncode=0, stdout=None):
        output = stdout if stdout is not None else strict_recovery.MARKER + json.dumps(payload or evidence())
        with tempfile.TemporaryDirectory() as directory, patch.object(
            strict_recovery, "_run_browser_subprocess",
            return_value=subprocess.CompletedProcess([], returncode, output, ""),
        ) as run:
            result = strict_recovery.run_strict_master_recovery(Path(directory))
        return result, run.call_args

    def test_pass_binds_source_and_keeps_bounded_scope(self):
        result, call = self.invoke()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["source_fingerprint"]), 64)
        self.assertFalse(result["process_tree_verified"])
        self.assertEqual(result["strict_transport_replay"], "not_run")
        self.assertIn("--work-root", call.args[0])
        self.assertGreater(call.kwargs["timeout"], 0)

    def test_parent_secrets_and_python_hooks_are_not_inherited(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "PYTHONPATH": "hook", "FACTORY_FLOW_GUARD_ZERO_COST_WORKER": "1", "HTTP_PROXY": "proxy"}):
            _, call = self.invoke()
        env = call.kwargs["env"]
        for key in ["OPENAI_API_KEY", "PYTHONPATH", "FACTORY_FLOW_GUARD_ZERO_COST_WORKER", "HTTP_PROXY"]:
            self.assertNotIn(key, env)

    def test_windows_redirector_parent_binds_worker_to_launcher(self):
        payload = evidence()
        for worker in payload["workers"]:
            worker["launcher_pid"] += 1000
            worker["parent_pid"] = worker["launcher_pid"]
        self.assertEqual(self.invoke(payload)[0]["status"], "passed")
        payload["workers"][0]["parent_pid"] = 1
        self.assertEqual(self.invoke(payload)[0]["status"], "failed")

    def test_missing_duplicate_or_invalid_marker_and_exit_fail(self):
        for output, code in [("", 0), (strict_recovery.MARKER + "{}", 0),
                             (strict_recovery.MARKER + "{bad", 0),
                             (strict_recovery.MARKER + json.dumps(evidence()), 1),
                             ((strict_recovery.MARKER + json.dumps(evidence()) + "\n") * 2, 0)]:
            with self.subTest(output=output, code=code):
                self.assertEqual(self.invoke(stdout=output, returncode=code)[0]["status"], "failed")

    def test_contradictory_or_incomplete_evidence_fails(self):
        changes = [
            lambda p: p.update(paid_provider_calls=1),
            lambda p: p.update(external_requests=1),
            lambda p: p.update(ui_actions=[]),
            lambda p: p.update(posts=["/api/videos/J-strict-master/produce"]),
            lambda p: p.update(actual_content_quality="passed"),
            lambda p: p["workers"][1].update(pid=101),
            lambda p: p["workers"][1].update(http_requests_matched=0),
            lambda p: p["workers"][1].update(violations=["escape"]),
            lambda p: p["workers"][1].update(process_tree_verified=True),
            lambda p: p["workers"][1].update(requests=[]),
            lambda p: p.update(media_http_statuses=[]),
            lambda p: p.update(final_state="COMPOSED"),
            lambda p: p.update(simulated_cost_cents=14),
        ]
        for change in changes:
            payload = copy.deepcopy(evidence())
            change(payload)
            with self.subTest(payload=payload):
                self.assertEqual(self.invoke(payload)[0]["status"], "failed")

    def test_timeout_fails_with_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            strict_recovery, "_run_browser_subprocess", side_effect=subprocess.TimeoutExpired("browser", 1)
        ):
            result = strict_recovery.run_strict_master_recovery(Path(directory))
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["errors"])

    def test_source_change_during_replay_fails(self):
        with patch.object(strict_recovery, "_source_fingerprint", side_effect=["a" * 64, "b" * 64]):
            result, _ = self.invoke()
        self.assertEqual(result["status"], "failed")
        self.assertIn("source changed during browser replay", result["errors"])

    def test_injected_runner_cannot_establish_builtin_provenance(self):
        result, _ = self.invoke()
        for runner, expected in [(strict_recovery.run_strict_master_recovery, "passed"), (lambda _: result, "unknown")]:
            boundaries = build_evidence_boundaries({"strict_master_recovery": result}, {"strict_master_recovery": runner})
            self.assertEqual(boundaries["dimensions"]["strict_master_recovery"]["status"], expected)
            self.assertEqual(boundaries["dimensions"]["strict_transport_replay"]["status"], "not_run")


class StrictRecoverySubprocessTests(unittest.TestCase):
    def test_success_captures_output_and_launches_hidden_without_cleanup(self):
        process = Mock(pid=4321, returncode=0)
        process.communicate.return_value = ("evidence", "diagnostic")
        with patch.object(strict_recovery, "_IS_WINDOWS", True), patch.object(
            strict_recovery.subprocess, "Popen", return_value=process
        ) as launch, patch.object(strict_recovery.subprocess, "run") as cleanup:
            completed = strict_recovery._run_browser_subprocess(["python", "browser.py"], cwd=ROOT, env={"PATH": "safe"}, timeout=2)
        self.assertEqual((completed.returncode, completed.stdout, completed.stderr), (0, "evidence", "diagnostic"))
        self.assertEqual(launch.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertEqual(launch.call_args.kwargs["env"], {"PATH": "safe"})
        cleanup.assert_not_called()

    def test_timeout_kills_only_the_owned_windows_tree_then_fails(self):
        process = Mock(pid=4321, returncode=1)
        process.communicate.side_effect = [subprocess.TimeoutExpired("browser", 2), ("partial", "timeout")]
        with patch.object(strict_recovery, "_IS_WINDOWS", True), patch.object(
            strict_recovery.subprocess, "Popen", return_value=process
        ), patch.object(strict_recovery.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as cleanup:
            with self.assertRaises(subprocess.TimeoutExpired):
                strict_recovery._run_browser_subprocess(["python", "browser.py"], cwd=ROOT, env={}, timeout=2)
        command = cleanup.call_args.args[0]
        self.assertEqual(command[1:], ["/PID", "4321", "/T", "/F"])
        self.assertTrue(Path(command[0]).is_absolute())
        self.assertEqual(Path(command[0]).parts[-2:], ("System32", "taskkill.exe"))
        self.assertEqual(cleanup.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertLessEqual(cleanup.call_args.kwargs["timeout"], 15)
        self.assertEqual(process.communicate.call_count, 2)
        self.assertLessEqual(process.communicate.call_args.kwargs["timeout"], 10)

    def test_failed_tree_cleanup_falls_back_to_owned_process_and_keeps_failure(self):
        process = Mock(pid=4321, returncode=1)
        process.communicate.side_effect = [subprocess.TimeoutExpired("browser", 2), ("", "")]
        with patch.object(strict_recovery, "_IS_WINDOWS", True), patch.object(
            strict_recovery.subprocess, "Popen", return_value=process
        ), patch.object(strict_recovery.subprocess, "run", side_effect=OSError("taskkill unavailable")):
            with self.assertRaises(subprocess.TimeoutExpired) as failure:
                strict_recovery._run_browser_subprocess(["python", "browser.py"], cwd=ROOT, env={}, timeout=2)
        process.kill.assert_called_once_with()
        self.assertTrue(failure.exception.cleanup_errors)

    @unittest.skipUnless(os.name == "nt", "Windows process-tree cleanup regression")
    def test_real_timeout_terminates_own_python_descendants(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "owned_tree.py"
            script.write_text(
                "import os, subprocess, sys, time\n"
                "from pathlib import Path\n"
                "root = Path(sys.argv[1])\n"
                "child = len(sys.argv) > 2\n"
                "(root / ('child.pid' if child else 'root.pid')).write_text(str(os.getpid()))\n"
                "if not child:\n"
                "    proc = subprocess.Popen([sys.executable, __file__, str(root), '--child'], creationflags=0x08000000)\n"
                "    (root / 'launcher.pid').write_text(str(proc.pid))\n"
                "time.sleep(60)\n", encoding="utf-8",
            )
            env = {k: v for k, v in os.environ.items() if k.upper() in strict_recovery._ENV_KEYS}
            with self.assertRaises(subprocess.TimeoutExpired) as failure:
                strict_recovery._run_browser_subprocess([sys.executable, str(script), str(root)], cwd=root, env=env, timeout=5)
            for filename in ("root.pid", "launcher.pid", "child.pid"):
                pid = int((root / filename).read_text())
                handle = kernel.OpenProcess(0x100000, False, pid)
                if handle:
                    try:
                        self.assertEqual(kernel.WaitForSingleObject(handle, 0), 0, f"owned process {pid} survived timeout: {failure.exception.cleanup_errors}")
                    finally:
                        kernel.CloseHandle(handle)


class StrictRecoveryMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def matrix(self, *, include_journeys, check):
        from factory.flow_guard.runner import run_matrix
        journey = lambda _: {"status": "passed", "paid_provider_calls": 0, "journey_count": 0, "passed": 0, "failed": 0}
        with tempfile.TemporaryDirectory() as directory, patch("factory.flow_guard.runner.evaluate_action_evidence", return_value=[{"complete": True}]):
            return await run_matrix(
                [], work_root=Path(directory), include_journeys=include_journeys,
                journey_runner=journey, strict_recovery_runner=check,
                include_incidents=False, include_video_golden=False,
                include_shadow_audit=False, include_perceptual_media=False,
            )

    async def test_full_requires_strict_runner_and_fails_closed(self):
        for result in ({"status": "failed"}, {"status": "not_run"}, None,
                       {"status": "passed", "paid_provider_calls": 0, "external_requests": 0}):
            payload = await self.matrix(include_journeys=True, check=lambda _: result)
            self.assertEqual(payload["status"], "failed")
        def broken(_):
            raise RuntimeError("missing browser")
        payload = await self.matrix(include_journeys=True, check=broken)
        self.assertEqual(payload["status"], "failed")

    async def test_incremental_explicitly_not_run(self):
        def forbidden(_):
            raise AssertionError("smoke must not launch browser")
        payload = await self.matrix(include_journeys=False, check=forbidden)
        self.assertEqual(payload["strict_master_recovery"]["status"], "not_run")
        self.assertIn("strict_master_recovery", payload["assurance"]["skipped_required_checks"])

    async def test_custom_valid_result_runs_but_provenance_remains_unknown(self):
        result, _ = StrictRecoveryRunnerTests().invoke()
        payload = await self.matrix(include_journeys=True, check=lambda _: result)
        self.assertEqual(payload["strict_master_recovery"]["status"], "passed")
        self.assertEqual(payload["evidence_boundaries"]["dimensions"]["strict_master_recovery"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
