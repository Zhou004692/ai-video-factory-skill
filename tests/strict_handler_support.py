"""Bounded transport replay with real FILE SQLite and local FFmpeg.

Only infrastructure/transport seams are replaced. Not OS/process-tree
containment; serial test processes must not run unrelated work in this scope.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
from unittest import mock

import httpx
import requests

from tests.strict_replay_support import StrictTransportReplay


FIXTURES = Path(__file__).parent / "fixtures" / "strict_transport"
MEDIA_SHA256 = "67e7dfbf127ad16fba7fbb1d8e2a9433d62c13533a67167abb86be32743fbfe0"


class StrictHandlerReplay(StrictTransportReplay):
    def __init__(self, *, contract=None):
        self.media_sha256 = MEDIA_SHA256
        fixture = deepcopy(contract) if contract is not None else json.loads(
            (FIXTURES / "character_anchor.json").read_text(encoding="utf-8")
        )
        for step in fixture["steps"]:
            response = step["response"]
            if "media_fixture" in response:
                if response.pop("media_fixture") != "anchor_red_media.json":
                    raise ValueError("unknown authored media fixture")
                media = json.loads((FIXTURES / "anchor_red_media.json").read_text(encoding="utf-8"))
                if media["sha256"] != MEDIA_SHA256:
                    raise ValueError("authored media SHA256 changed")
                response.update(body_base64=media["body_base64"], body_sha256=MEDIA_SHA256)
        super().__init__(fixture)
        self.processes = []
        self._connections = []
        self._temporary = None

    def _guard_subprocess(self, stack):
        original = subprocess.Popen.__init__

        def guarded(process, args, *positional, **kwargs):
            if positional or kwargs.get("shell") or kwargs.get("executable") or kwargs.get("env") is not None:
                self._fail("strict replay forbids subprocess launch overrides")
            if kwargs.get("cwd") is not None and Path(kwargs["cwd"]).resolve() != self.root:
                self._fail("strict replay forbids subprocess cwd")
            safe_args = self._media_command(args)
            self.processes.append({"executable": Path(safe_args[0]).name, "argv": safe_args[1:]})
            return original(process, safe_args, **kwargs)

        # Covers direct Popen and previously captured Windows asyncio subclass.
        stack.enter_context(mock.patch.object(subprocess.Popen, "__init__", guarded))

    def _local_path(self, value, suffixes):
        if not isinstance(value, str) or not Path(value).is_absolute():
            self._fail("strict replay forbids nonabsolute media path")
        path = Path(value).resolve()
        if not path.is_relative_to(self.root) or path.suffix not in suffixes:
            self._fail("strict replay forbids media path outside temporary root")
        return str(path)

    def _media_command(self, args):
        if not isinstance(args, (tuple, list)) or not args or not all(isinstance(x, str) for x in args):
            self._fail("strict replay forbids subprocess command")
        args = list(args)
        executable = Path(args[0])
        if not executable.is_absolute():
            self._fail("strict replay forbids noncanonical subprocess executable")
        command = args[1:]
        if executable.resolve() == self.ffmpeg:
            if (len(command) != 13 or command[:7] != ["-y", "-hide_banner", "-loglevel", "error", "-ss", "1.000", "-i"]
                    or command[8:12] != ["-frames:v", "1", "-q:v", "2"]):
                self._fail("strict replay forbids unapproved ffmpeg arguments")
            source = self._local_path(command[7], {".mp4"})
            target = self._local_path(command[12], {".jpg"})
            return [str(self.ffmpeg), *command[:6], "-protocol_whitelist", "file", "-f", "mov",
                    "-i", source, *command[8:12], target]
        if executable.resolve() == self.ffprobe:
            if len(command) != 8 or command[:7] != ["-v", "error", "-show_streams", "-show_format", "-of", "json", "-i"]:
                self._fail("strict replay forbids unapproved ffprobe arguments")
            source = self._local_path(command[7], {".mp4", ".jpg"})
            demuxer = "mov" if Path(source).suffix == ".mp4" else "image2"
            return [str(self.ffprobe), "-protocol_whitelist", "file", "-f", demuxer, *command[:7], source]
        self._fail("strict replay forbids unknown subprocess executable")

    def _requests_response(self, adapter, request, **kwargs):
        # TOS SDK signs and serializes normally. Only its final send is replayed.
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("TOS4-HMAC-SHA256 Credential=strict-test-ak/"):
            self._fail("strict replay TOS authentication mismatch")
        body = request.body
        if hasattr(body, "read"):
            body = body.read()
        response = self._respond(httpx.Request(
            request.method, request.url, headers=dict(request.headers), content=body or b"",
        ))
        result = requests.Response()
        result.status_code = response.status_code
        result.headers.update(response.headers)
        result._content = response.content
        result._content_consumed = True
        result.raw = io.BytesIO(response.content)
        result.url, result.request = request.url, request
        return result

    def __enter__(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="strict-anchor-")
        self.root = Path(self._temporary.name).resolve()
        self.ffmpeg = Path(shutil.which("ffmpeg") or "").resolve()
        self.ffprobe = Path(shutil.which("ffprobe") or "").resolve()
        if not self.ffmpeg.is_file() or not self.ffprobe.is_file():
            self._temporary.cleanup()
            raise RuntimeError("strict real handler replay requires local ffmpeg and ffprobe")
        try:
            super().__enter__()
            stack = self._stack
            # Register cleanup early; patches restore only after DB handles close.
            stack.callback(self._temporary.cleanup)
            from factory import db
            from factory.clients import tos
            from factory.steps import s3_shots

            self.assets = self.root / "assets"
            self.assets.mkdir()
            self.db_path = self.root / "factory.db"
            stack.enter_context(mock.patch.dict(os.environ, {
                "FACTORY_DATA_DIR": str(self.root), "FACTORY_DB_PATH": str(self.db_path),
                "VOLC_AK": "strict-test-ak", "VOLC_SK": "strict-test-sk",
                "TOS_BUCKET": "strict-bucket", "TOS_REGION": "cn-beijing",
                "TOS_ENDPOINT": "tos-cn-beijing.volces.com", "TOS_OBJECT_PREFIX": "strict-fixtures",
            }))
            stack.enter_context(mock.patch.object(db, "database_path", return_value=self.db_path))
            stack.enter_context(mock.patch.object(s3_shots, "ASSETS_DIR", self.assets))
            original_connect = sqlite3.connect

            def connect(path, *args, **kwargs):
                if Path(path).resolve() != self.db_path or kwargs.get("uri"):
                    self._fail("strict replay forbids database outside temporary root")
                connection = original_connect(path, *args, **kwargs)
                self._connections.append(connection)
                return connection

            stack.enter_context(mock.patch.object(sqlite3, "connect", connect))
            stack.callback(self._close_connections)
            stack.enter_context(mock.patch.object(tos, "time", SimpleNamespace(time=lambda: 1700000000)))
            # SDK DNS-cache setup starts a daemon and rewrites urllib3 globally.
            # Disable that infrastructure only; actual signed SDK requests stay.
            stack.enter_context(mock.patch.object(tos._tos.TosClientV2, "_open_dns_cache", return_value=False))
            def send(adapter, request, **kwargs):
                return self._requests_response(adapter, request, **kwargs)

            stack.enter_context(mock.patch.object(requests.adapters.HTTPAdapter, "send", send))
            stack.enter_context(mock.patch.object(asyncio, "sleep", new=mock.AsyncMock()))
            self.database = db.conn()
            return self
        except BaseException:
            if self._stack is not None:
                self._stack.close()
                self._stack = None
            self._temporary.cleanup()
            raise

    def _close_connections(self):
        for connection in self._connections:
            connection.close()

    async def probe(self, path):
        process = await asyncio.create_subprocess_exec(
            str(self.ffprobe), "-v", "error", "-show_streams", "-show_format", "-of", "json", "-i", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            self._fail("strict replay media probe failed: " + stderr.decode(errors="replace")[:200])
        return json.loads(stdout)

    def evidence(self):
        return {**super().evidence(), "scope": "in_process_transport_real_character_anchor",
                "local_media_processes": len(self.processes), "external_provider_fees_cents": 0,
                "simulated_accounting": True}
