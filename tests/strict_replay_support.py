"""Fail-closed, in-process transport replay for real provider client tests.

Only HTTPX's default HTTP transport is replaced; clients still construct,
parse and validate their real requests and responses. Socket/DNS/subprocess
escapes are rejected. This is a serial test utility, not an OS sandbox or
evidence of browser/native-worker process-tree isolation.
"""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import base64
import hashlib
import json
import os
import socket
import subprocess
import threading
from unittest import mock

import httpx


class StrictReplayViolation(AssertionError):
    """The test left its recorded transport contract or isolation boundary."""


_SESSION_LOCK = threading.Lock()
_SYSTEM_ENV_KEYS = frozenset({
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
})
_TRANSPORT_ERRORS = {
    "ConnectError": httpx.ConnectError,
    "ReadTimeout": httpx.ReadTimeout,
    "RemoteProtocolError": httpx.RemoteProtocolError,
}


def _fingerprint(value: dict) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class StrictTransportReplay:
    def __init__(self, fixture: dict):
        self.fixture = deepcopy(fixture)
        if fixture.get("schema_version") != 1 or not fixture.get("fixture_id"):
            raise ValueError("strict replay requires schema_version=1 and fixture_id")
        if fixture.get("source_kind") not in {"authored_contract", "redacted_recording"}:
            raise ValueError("strict replay requires explicit fixture provenance")
        steps = self.fixture.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("strict replay requires non-empty recorded steps")
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("request"), dict):
                raise ValueError("strict replay requires recorded request objects")
            request = step["request"]
            if set(request) - {"method", "url", "json", "body_sha256", "headers"}:
                raise ValueError("strict replay request has unsupported fields")
            headers = request.get("headers")
            if not isinstance(headers, dict) or not all(
                isinstance(name, str) and name and name == name.lower()
                and (value is None or isinstance(value, str))
                for name, value in headers.items()
            ):
                raise ValueError("strict replay requires explicit lowercase header constraints")
            if not request.get("method") or not str(request.get("url", "")).startswith("https://"):
                raise ValueError("strict replay requires an explicit HTTPS request")
            if "json" in request and "body_sha256" in request:
                raise ValueError("strict replay request has ambiguous body")
            response = step.get("response")
            if not isinstance(response, dict):
                raise ValueError("strict replay requires an HTTP response")
            if "transport_error" in response:
                if set(response) != {"transport_error"} or response["transport_error"] not in _TRANSPORT_ERRORS:
                    raise ValueError("strict replay requires a known transport error without an HTTP body")
                continue
            if not isinstance(response.get("status_code"), int):
                raise ValueError("strict replay requires an HTTP status code")
            if sum(key in response for key in ("json", "text", "body_base64")) > 1:
                raise ValueError("strict replay response has ambiguous body")
            if "body_base64" in response:
                body = base64.b64decode(response["body_base64"], validate=True)
                if hashlib.sha256(body).hexdigest() != response.get("body_sha256"):
                    raise ValueError("strict replay response bytes do not match pinned SHA256")
        self.requests: list[dict] = []
        self._position = 0
        self._violations: list[str] = []
        self._stack: ExitStack | None = None
        self._used = False

    def _deny(self, boundary: str):
        def reject(*args, **kwargs):
            self._fail(f"strict replay forbids {boundary}")
        return reject

    def _fail(self, message: str):
        self._violations.append(message)
        raise StrictReplayViolation(message)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        actual = {"method": request.method, "url": str(request.url)}
        content = request.read()
        if content:
            if "application/json" in request.headers.get("content-type", ""):
                try:
                    actual["json"] = json.loads(content)
                except (ValueError, UnicodeDecodeError):
                    self._fail("strict replay request mismatch: invalid JSON")
            else:
                actual["body_sha256"] = hashlib.sha256(content).hexdigest()
        if self._position >= len(self.fixture["steps"]):
            self._fail("strict replay request mismatch: extra request")
        step = self.fixture["steps"][self._position]
        # Match protocol headers from the authored contract, not HTTPX's
        # version-dependent defaults. None explicitly requires absence.
        actual["headers"] = {
            name: request.headers.get(name) for name in step["request"]["headers"]
        }
        expected_hash, actual_hash = _fingerprint(step["request"]), _fingerprint(actual)
        if expected_hash != actual_hash:
            self._fail(
                f"strict replay request mismatch at step {self._position + 1}: "
                f"expected={expected_hash} actual={actual_hash}"
            )
        self.requests.append({
            "method": request.method,
            "route": f"{request.url.scheme}://{request.url.host}{request.url.path}",
            "request_hash": actual_hash,
            "header_names": sorted(request.headers.keys()),
        })
        self._position += 1
        response = step["response"]
        if "transport_error" in response:
            raise _TRANSPORT_ERRORS[response["transport_error"]](
                "recorded transport failure", request=request,
            )
        if "body_base64" in response:
            body = {"content": base64.b64decode(response["body_base64"], validate=True)}
        else:
            body = {"json": deepcopy(response["json"])} if "json" in response else {
                "text": str(response.get("text", "")),
            }
        return httpx.Response(
            response["status_code"], headers=response.get("headers", {}),
            request=request, **body,
        )

    def __enter__(self):
        if self._used or not _SESSION_LOCK.acquire(blocking=False):
            raise StrictReplayViolation("strict replay sessions must be single-use and serial")
        self._used = True
        stack = self._stack = ExitStack()
        stack.callback(_SESSION_LOCK.release)
        try:
            environment = {
                key: value for key, value in os.environ.items()
                if key.upper() in _SYSTEM_ENV_KEYS
            }
            environment.update({
                "FACTORY_PROVIDER_MODE": "live",
                "ARK_API_KEY": "strict-test-key",
                "VOLC_ASR_API_KEY": "strict-test-key",
            })
            stack.enter_context(mock.patch.dict(os.environ, environment, clear=True))
            for method in ("connect", "connect_ex", "sendto", "send", "sendall", "sendmsg"):
                if callable(getattr(socket.socket, method, None)):
                    stack.enter_context(mock.patch.object(socket.socket, method, self._deny(f"socket.{method}")))
            for method in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
                stack.enter_context(mock.patch.object(socket, method, self._deny(f"DNS.{method}")))
            self._guard_subprocess(stack)
            for method in dir(os):
                if (method.startswith(("spawn", "exec")) or method in {"system", "startfile", "popen"}) and callable(getattr(os, method)):
                    stack.enter_context(mock.patch.object(os, method, self._deny(f"os.{method}")))

            def sync_response(transport, request):
                return self._respond(request)

            async def async_response(transport, request):
                await request.aread()
                return self._respond(request)

            stack.enter_context(mock.patch.object(httpx.HTTPTransport, "handle_request", sync_response))
            stack.enter_context(mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", async_response))
            return self
        except BaseException:
            stack.close()
            self._stack = None
            raise

    def _guard_subprocess(self, stack):
        # Captured subclasses (notably asyncio.windows_utils.Popen) bypass a
        # patch of the module symbol, but still invoke this original __init__.
        stack.enter_context(mock.patch.object(subprocess.Popen, "__init__", self._deny("subprocess")))
        stack.enter_context(mock.patch.object(subprocess, "Popen", self._deny("subprocess")))

    def __exit__(self, exc_type, exc_value, traceback):
        assert self._stack is not None
        self._stack.close()
        self._stack = None
        if self._violations:
            raise StrictReplayViolation(self._violations[0]) from None
        if self._position != len(self.fixture["steps"]):
            self._fail("strict replay has unconsumed recorded requests")
        return False

    def evidence(self) -> dict:
        return {
            "fixture_id": self.fixture["fixture_id"],
            "source_kind": self.fixture["source_kind"],
            "scope": "in_process_http_transport",
            "http_requests_matched": self._position,
            "expected_requests": len(self.fixture["steps"]),
            "violations": len(self._violations),
            "content_quality_verified": False,
            "process_tree_verified": False,
        }
