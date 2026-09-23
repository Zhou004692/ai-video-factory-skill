import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory import transport_diagnostics  # noqa: E402


class TransportDiagnosticsTests(unittest.TestCase):
    def test_default_targets_are_read_only_and_cover_api_upload_and_download(self):
        targets = transport_diagnostics.default_targets({
            "TOS_ENDPOINT": "tos-cn-beijing.volces.com",
        })

        self.assertEqual(
            [target.target_id for target in targets],
            ["ark_api", "tos_upload", "provider_download"],
        )
        self.assertTrue(all(target.method == "HEAD" for target in targets))
        self.assertTrue(all(target.port == 443 for target in targets))

    def test_smoke_check_no_longer_posts_model_requests(self):
        source = (ROOT / "scripts" / "smoke_check.py").read_text(encoding="utf-8")

        self.assertIn("transport_diagnostics.run_preflight", source)
        self.assertNotIn(".post(", source)
        self.assertNotIn("duration\": 31", source)

    def test_probe_reports_each_phase_without_echoing_proxy_credentials(self):
        target = transport_diagnostics.ProbeTarget(
            "ark_api", "模型 API", "ark.cn-beijing.volces.com", "/api/v3"
        )

        report = transport_diagnostics.probe_target(
            target,
            environment={
                "HTTPS_PROXY": "http://private-user:private-pass@127.0.0.1:7897"
            },
            direct_probe=lambda *_args, **_kwargs: [
                {"phase": "dns", "status": "ok", "latency_ms": 1},
                {"phase": "tcp", "status": "ok", "latency_ms": 2},
                {"phase": "tls", "status": "ok", "latency_ms": 3},
                {"phase": "http", "status": "ok", "latency_ms": 4,
                 "http_status": 403},
            ],
            route_probe=lambda *_args, **_kwargs: {
                "phase": "application_route", "status": "ok",
                "latency_ms": 5, "http_status": 403,
            },
        )

        self.assertTrue(report["ready"])
        self.assertEqual(
            [phase["phase"] for phase in report["phases"]],
            ["dns", "tcp", "tls", "http", "application_route"],
        )
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertIn("127.0.0.1:7897", encoded)
        self.assertNotIn("private-user", encoded)
        self.assertNotIn("private-pass", encoded)

    def test_dns_failure_stops_without_claiming_a_request_was_submitted(self):
        target = transport_diagnostics.ProbeTarget(
            "ark_api", "模型 API", "ark.cn-beijing.volces.com", "/api/v3"
        )

        def fail(*_args, **_kwargs):
            raise transport_diagnostics.TransportProbeFailure(
                "dns", socket.gaierror("private resolver detail")
            )

        report = transport_diagnostics.probe_target(
            target,
            environment={},
            direct_probe=fail,
        )

        self.assertFalse(report["ready"])
        self.assertEqual(report["failure"]["phase"], "dns")
        self.assertEqual(report["failure"]["reason_code"], "dns_resolution_failed")
        self.assertEqual(report["failure"]["submit_state"], "not_submitted")
        self.assertNotIn("private resolver detail", json.dumps(report))

    def test_exception_diagnostic_distinguishes_safe_retry_from_unknown_submit(self):
        endpoint = "https://ark.cn-beijing.volces.com/api/v3/tasks?X-Signature=secret"
        before_submit = transport_diagnostics.exception_diagnostic(
            httpx.ConnectTimeout("private timeout"),
            stage="submit_connect",
            endpoint=endpoint,
            submit_result_unknown=False,
        )
        after_submit = transport_diagnostics.exception_diagnostic(
            httpx.ReadTimeout("private read timeout"),
            stage="submit_response",
            endpoint=endpoint,
            submit_result_unknown=True,
        )

        self.assertEqual(before_submit["phase"], "tcp")
        self.assertEqual(before_submit["submit_state"], "not_submitted")
        self.assertTrue(before_submit["retryable"])
        self.assertEqual(after_submit["phase"], "http_response")
        self.assertEqual(after_submit["submit_state"], "unknown")
        self.assertFalse(after_submit["retryable"])
        encoded = json.dumps([before_submit, after_submit])
        self.assertNotIn("secret", encoded)
        self.assertNotIn("private", encoded)

    def test_exception_diagnostic_tolerates_a_malformed_endpoint(self):
        diagnostic = transport_diagnostics.exception_diagnostic(
            httpx.ConnectError("private detail"),
            stage="submit_connect",
            endpoint="https://[invalid-host/private-token",
            submit_result_unknown=False,
        )

        self.assertEqual(diagnostic["host"], "unknown")
        self.assertNotIn("private", json.dumps(diagnostic))

    def test_seedance_submission_error_carries_the_same_sanitized_evidence(self):
        from factory.clients import seedance

        error = seedance._submission_transport_error(
            httpx.ConnectError("C:/Users/private/.env")
        )

        self.assertFalse(error.submit_result_unknown)
        self.assertEqual(error.transport_diagnostic["phase"], "tcp")
        self.assertEqual(
            error.transport_diagnostic["host"], "ark.cn-beijing.volces.com"
        )
        self.assertNotIn("private", json.dumps(error.transport_diagnostic))

    def test_preflight_is_zero_cost_and_persists_only_sanitized_report(self):
        targets = (
            transport_diagnostics.ProbeTarget(
                "ark_api", "模型 API", "ark.cn-beijing.volces.com", "/api/v3"
            ),
        )
        calls = []

        def probe(target, **_kwargs):
            calls.append(target.target_id)
            return {
                "target_id": target.target_id,
                "label": target.label,
                "host": target.host,
                "ready": True,
                "status": "ok",
                "phases": [],
            }

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "latest.json"
            report = transport_diagnostics.run_preflight(
                targets=targets,
                probe=probe,
                report_path=path,
                environment={"ARK_API_KEY": "must-not-be-persisted"},
            )

            self.assertEqual(calls, ["ark_api"])
            self.assertTrue(report["ready"])
            self.assertEqual(report["paid_provider_calls"], 0)
            self.assertFalse(report["provider_task_created"])
            persisted = path.read_text(encoding="utf-8")
            self.assertNotIn("must-not-be-persisted", persisted)
            self.assertEqual(
                transport_diagnostics.load_report(path)["diagnostic_id"],
                report["diagnostic_id"],
            )

    def test_video_qa_transport_error_keeps_structured_safe_evidence(self):
        from factory.clients import video_qa

        error = video_qa._transport_error(
            httpx.ConnectError("C:/Users/private/.env"),
            stage="qa_submit",
        )

        self.assertEqual(error.stable_code, "transient_network")
        self.assertEqual(error.transport_diagnostic["phase"], "tcp")
        self.assertEqual(error.transport_diagnostic["submit_state"], "not_submitted")
        self.assertNotIn("private", json.dumps(error.transport_diagnostic))

    def test_qa_transport_diagnostic_is_persisted_with_manual_review_candidate(self):
        from factory import db
        from factory.steps import s3_shots

        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state) VALUES(?,?,?,'PENDING')",
            ("J-QA-DIAG", "T-1", "qa diagnostic"),
        )
        database.execute(
            "INSERT INTO shots(job_id,idx,state) VALUES(?,1,'GENERATING')",
            ("J-QA-DIAG",),
        )
        database.commit()
        diagnostic = {
            "stage": "qa_submit", "phase": "tcp",
            "host": "ark.cn-beijing.volces.com",
            "reason_code": "tcp_connect_failed",
            "submit_state": "not_submitted", "retryable": True,
        }
        with tempfile.TemporaryDirectory() as folder:
            candidate = Path(folder) / "candidate.mp4"
            candidate.write_bytes(b"candidate")
            error = s3_shots.SeedanceQualityError(
                "automatic QA unavailable",
                cost_cents=0,
                provider_calls=(),
                reasons=("qa_error",),
                candidate_path=candidate,
                qa_evidence={"status": "qa_error", "transport": diagnostic},
            )
            with patch.object(
                s3_shots, "controlled_candidate_path", return_value=candidate
            ):
                s3_shots._record_failed_generation(database, "J-QA-DIAG", 1, error)

        payload = json.loads(database.execute(
            "SELECT video_qa FROM shots WHERE job_id='J-QA-DIAG' AND idx=1"
        ).fetchone()[0])
        self.assertEqual(payload["diagnostic"]["transport"], diagnostic)
        self.assertNotIn("automatic QA unavailable", json.dumps(payload))

    def test_management_start_blocks_before_paid_handler_when_preflight_fails(self):
        from factory import db
        from factory.management_api import ManagementService

        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        production_service = Mock()
        production_read_model = Mock()
        with tempfile.TemporaryDirectory() as folder:
            service = ManagementService(
                database,
                Path(folder),
                data_dir=Path(folder) / "data",
                production_service=production_service,
                production_read_model=production_read_model,
                transport_preflight_enabled=True,
            )
            failed = {
                "diagnostic_id": "transport-failed",
                "ready": False,
                "paid_provider_calls": 0,
                "provider_task_created": False,
                "targets": [],
            }
            with patch.object(
                service, "run_transport_preflight", return_value=failed
            ):
                with self.assertRaises(transport_diagnostics.TransportPreflightError):
                    service.start_video_job("J-BLOCKED", confirmed=True)

        production_service.start.assert_not_called()

    def test_management_health_exposes_last_persisted_transport_report(self):
        from factory import db
        from factory.management_api import ManagementService

        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        with tempfile.TemporaryDirectory() as folder:
            data_dir = Path(folder) / "data"
            service = ManagementService(database, Path(folder), data_dir=data_dir)
            report = {
                "diagnostic_id": "transport-ok",
                "checked_at": "2026-09-23T00:00:00+00:00",
                "ready": True,
                "paid_provider_calls": 0,
                "provider_task_created": False,
                "targets": [],
            }
            path = service.transport_diagnostic_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report), encoding="utf-8")

            health = service.production_health(supervisor_alive=False)

        self.assertEqual(health["transport_preflight"], report)


if __name__ == "__main__":
    unittest.main()
