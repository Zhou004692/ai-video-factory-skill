"""Shared structured-output recovery tests for all LLM clients."""
from __future__ import annotations

from pathlib import Path
import json
import hashlib
import os
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


from factory.clients.claude import _extract_json  # noqa: E402
from factory.clients import doubao_llm  # noqa: E402


class StructuredJsonRecoveryTests(unittest.TestCase):
    def test_repairs_common_model_json_syntax_damage_locally(self):
        damaged = '{"title":"第一行\n第二行" "count":3,}'

        parsed = _extract_json(damaged)

        self.assertEqual(parsed, {"title": "第一行\n第二行", "count": 3})

    def test_balances_truncated_container_before_business_validation(self):
        truncated = '{"variants":[{"title":"A","beats":[]}]'

        parsed = _extract_json(truncated)

        self.assertEqual(
            parsed, {"variants": [{"title": "A", "beats": []}]}
        )

    def test_doubao_reports_length_finish_as_truncation(self):
        body = {
            "choices": [{
                "finish_reason": "length",
                "message": {"content": '{"private_source_text":"must-not-log-this"'},
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }

        class Response:
            status_code = 200

            def json(self):
                return body

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, *args, **kwargs):
                return Response()

        with patch.dict(os.environ, {"ARK_API_KEY": "test-key"}), patch(
            "factory.clients.doubao_llm.httpx.Client", return_value=Client()
        ):
            with self.assertLogs("factory.clients.doubao_llm", level="WARNING") as logs:
                with self.assertRaisesRegex(json.JSONDecodeError, "truncated") as raised:
                    doubao_llm.complete_json("private-prompt", max_tokens=100)

        diagnostic = raised.exception.response_diagnostic
        self.assertEqual(diagnostic["kind"], "truncated")
        self.assertEqual(diagnostic["finish_reason"], "length")
        self.assertEqual(diagnostic["max_tokens"], 100)
        self.assertEqual(diagnostic["output_tokens"], 20)
        self.assertEqual(diagnostic["output_sha256"], hashlib.sha256(
            body["choices"][0]["message"]["content"].encode("utf-8")
        ).hexdigest())
        self.assertNotIn("must-not-log-this", " ".join(logs.output))
        self.assertNotIn("private-prompt", " ".join(logs.output))
        self.assertNotIn("test-key", " ".join(logs.output))

    def test_doubao_distinguishes_syntax_errors_without_logging_source(self):
        text = '{"private_source_text":"must-not-log-this", "broken": @}'
        body = {
            "choices": [{"finish_reason": "stop", "message": {"content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }
        class Response:
            status_code = 200
            def json(self):
                return body
        class Client:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def post(self, *args, **kwargs):
                return Response()
        with patch.dict(os.environ, {"ARK_API_KEY": "test-key"}), patch(
            "factory.clients.doubao_llm.httpx.Client", return_value=Client()
        ), self.assertLogs("factory.clients.doubao_llm", level="WARNING") as logs:
            with self.assertRaises(json.JSONDecodeError) as raised:
                doubao_llm.complete_json("private-prompt", max_tokens=12000)
        diagnostic = raised.exception.response_diagnostic
        self.assertEqual(diagnostic["kind"], "syntax")
        self.assertEqual(diagnostic["finish_reason"], "stop")
        self.assertEqual(diagnostic["max_tokens"], 12000)
        self.assertGreater(diagnostic["column"], 0)
        self.assertNotIn("must-not-log-this", " ".join(logs.output))
        self.assertNotIn("private-prompt", " ".join(logs.output))
        self.assertNotIn("test-key", " ".join(logs.output))

    def test_doubao_uses_configurable_timeout_for_complex_script_generation(self):
        observed_timeouts = []
        body = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"variants":[]}'},
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }

        class Response:
            status_code = 200

            def json(self):
                return body

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, *args, **kwargs):
                return Response()

        def client_factory(*, timeout):
            observed_timeouts.append(timeout)
            return Client()

        with patch.dict(
            os.environ,
            {"ARK_API_KEY": "test-key", "ARK_LLM_TIMEOUT_SEC": "300"},
        ), patch(
            "factory.clients.doubao_llm.httpx.Client",
            side_effect=client_factory,
        ):
            doubao_llm.complete_json("prompt", max_tokens=6000)

        self.assertEqual(observed_timeouts, [300.0])

    def test_doubao_distinguishes_invalid_http_json_from_script_json(self):
        class Response:
            status_code = 200
            text = "private-invalid-http-response"
            def json(self):
                raise json.JSONDecodeError("Expecting value", self.text, 0)
        class Client:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def post(self, *args, **kwargs):
                return Response()
        with patch.dict(os.environ, {"ARK_API_KEY": "test-key"}), patch(
            "factory.clients.doubao_llm.httpx.Client", return_value=Client()
        ), self.assertLogs("factory.clients.doubao_llm", level="WARNING") as logs:
            with self.assertRaises(json.JSONDecodeError) as raised:
                doubao_llm.complete_json("private-prompt")
        self.assertEqual(raised.exception.response_diagnostic["kind"], "response_json")
        self.assertNotIn(Response.text, " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
