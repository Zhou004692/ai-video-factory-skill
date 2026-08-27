"""Shared structured-output recovery tests for all LLM clients."""
from __future__ import annotations

from pathlib import Path
import json
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
                "message": {"content": '{"variants":['},
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
            with self.assertRaisesRegex(json.JSONDecodeError, "truncated"):
                doubao_llm.complete_json("prompt", max_tokens=100)

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


if __name__ == "__main__":
    unittest.main()
