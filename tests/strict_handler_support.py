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
import re
import shutil
import sqlite3
import socket
import subprocess
import tempfile
from types import SimpleNamespace
from unittest import mock
import uuid

import httpx
import requests

from tests.strict_replay_support import StrictTransportReplay


FIXTURES = Path(__file__).parent / "fixtures" / "strict_transport"
MEDIA_SHA256 = "67e7dfbf127ad16fba7fbb1d8e2a9433d62c13533a67167abb86be32743fbfe0"
MASTER_MEDIA_SHA256 = "5e729fe4dd835115728487f6135f0f415a164cf042b8d4a593e6c6b220917ef6"
_MEDIA_FIXTURES = {
    "anchor_red_media.json": MEDIA_SHA256,
    "master_av_media.json": MASTER_MEDIA_SHA256,
}
_ORIGINAL_SOCKET_SEND = socket.socket.send


class StrictHandlerReplay(StrictTransportReplay):
    def __init__(self, *, contract=None):
        self.media_sha256 = MEDIA_SHA256
        fixture = deepcopy(contract) if contract is not None else json.loads(
            (FIXTURES / "character_anchor.json").read_text(encoding="utf-8")
        )
        for step in fixture["steps"]:
            response = step["response"]
            if "media_fixture" in response:
                media_name = response.pop("media_fixture")
                expected_sha = _MEDIA_FIXTURES.get(media_name)
                if expected_sha is None:
                    raise ValueError("unknown authored media fixture")
                media = json.loads((FIXTURES / media_name).read_text(encoding="utf-8"))
                if media["sha256"] != expected_sha:
                    raise ValueError("authored media SHA256 changed")
                response.update(body_base64=media["body_base64"], body_sha256=expected_sha)
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


class StrictMasterComposeReplay(StrictHandlerReplay):
    """Strict transport plus an allow-list for the formal local media pipeline."""

    def __init__(self, *, contract=None, fail_operation=None):
        if contract is None:
            contract = json.loads(
                (FIXTURES / "master_compose.json").read_text(encoding="utf-8")
            )
        super().__init__(contract=contract)
        self.media_sha256 = MASTER_MEDIA_SHA256
        if fail_operation not in {None, "mix"}:
            raise ValueError("unknown authored local-media fault")
        self.fail_operation = fail_operation

    def _media_command(self, args):
        if not isinstance(args, (tuple, list)) or not args or not all(
            isinstance(value, str) for value in args
        ):
            self._fail("strict replay forbids subprocess command")
        args = list(args)
        executable = Path(args[0])
        if not executable.is_absolute():
            self._fail("strict replay forbids noncanonical subprocess executable")
        resolved = executable.resolve()
        command = args[1:]
        if resolved == self.ffprobe:
            return self._approved_ffprobe(command)
        if resolved == self.ffmpeg:
            return self._approved_ffmpeg(command)
        self._fail("strict replay forbids unknown subprocess executable")

    def _approved_ffprobe(self, command):
        prefix = ["-v", "error"]
        if command[:2] != prefix:
            self._fail("strict replay forbids unapproved ffprobe arguments")
        approved = (
            ["-show_entries", "format=duration", "-of",
             "default=noprint_wrappers=1:nokey=1"],
            ["-select_streams", "a", "-show_entries", "stream=codec_type",
             "-of", "default=noprint_wrappers=1"],
            ["-show_streams", "-show_format", "-of", "json", "-i"],
        )
        tail = command[2:]
        for signature in approved:
            if tail[:-1] == signature:
                source = self._local_path(tail[-1], {".mp4", ".mp3", ".wav"})
                return [str(self.ffprobe), *prefix, *signature, source]
        self._fail("strict replay forbids unapproved ffprobe arguments")

    def _approved_ffmpeg(self, command):
        base = ["-y", "-hide_banner", "-loglevel", "error"]
        if command[:4] != base:
            self._fail("strict replay forbids unapproved ffmpeg arguments")
        tail = command[4:]
        # BigASR source extraction.
        if (
            len(tail) == 8 and tail[0] == "-i"
            and tail[2:7] == ["-vn", "-ac", "1", "-ar", "16000"]
        ):
            source = self._local_path(tail[1], {".mp4"})
            target = self._local_path(tail[7], {".wav"})
            return [str(self.ffmpeg), *base, "-i", source, *tail[2:7], target]
        # Formal zero-cost BGM provider; still executes real local FFmpeg.
        if (
            len(tail) == 11
            and tail[:6] == ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "60"]
            and tail[6:10] == ["-q:a", "9", "-acodec", "libmp3lame"]
        ):
            target = self._local_path(tail[10], {".mp3"})
            return [str(self.ffmpeg), *base, *tail[:10], target]
        # Legacy one-master normalization used by the frozen formal graph.
        if tail[:1] == ["-i"] and tail.count("-i") == 1 and "-vf" in tail:
            self._validate_normalize(tail)
            return [str(self.ffmpeg), *base, *tail]
        # One-input concat generated by the formal concat_shots implementation.
        if tail[:1] == ["-i"] and tail.count("-i") == 1 and "-filter_complex" in tail:
            self._validate_concat(tail)
            return [str(self.ffmpeg), *base, *tail]
        # Final BGM mix: exact two local inputs, stream-copy video, AAC audio.
        if tail[:1] == ["-i"] and tail.count("-i") == 2 and "aloop=loop=-1:size=2e9" in " ".join(tail):
            self._validate_mix(tail)
            if self.fail_operation == "mix":
                failure_target = self.root / "authored-mix-failure.mp4"
                failure_target.mkdir()
                tail[-1] = str(failure_target)
            return [str(self.ffmpeg), *base, *tail]
        self._fail("strict replay forbids unapproved ffmpeg arguments: " + repr(tail))

    def _validate_normalize(self, tail):
        if len(tail) != 21 or tail[0] != "-i" or tail[2] != "-vf":
            self._fail("strict replay forbids malformed shot normalization")
        source = self._local_path(tail[1], {".mp4"})
        expected_tail = [
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-pix_fmt", "yuv420p",
        ]
        if tail[4:20] != expected_tail:
            self._fail("strict replay forbids altered shot normalization codecs")
        vf = tail[tail.index("-vf") + 1]
        prefix = (
            "setpts=PTS-STARTPTS,"
            "scale=1080:1920:force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,"
            "setsar=1,fps=24,"
        )
        textfiles = re.findall(r"textfile='([^']+)'", vf)
        if len(textfiles) != 2:
            self._fail("strict replay requires the authored two-line subtitle")
        for value in textfiles:
            windows_path = value.replace("\\:", ":").replace("/", "\\")
            self._local_path(windows_path, {".txt"})
        drawtext = (
            "drawtext=fontfile='C\\:/Windows/Fonts/msyhbd.ttc':"
            "textfile='{path}':fontcolor=white:fontsize=51:borderw=4:"
            "bordercolor=black@0.9:x=(w-text_w)/2:y=h*0.80{offset}:"
            "line_spacing=10"
        )
        expected_vf = ",".join((
            prefix.rstrip(","),
            drawtext.format(path=textfiles[0], offset="-34"),
            drawtext.format(path=textfiles[1], offset="+34"),
            "fade=t=in:st=0:d=0.15",
            "fade=t=out:st=3.85:d=0.15",
        ))
        if vf != expected_vf:
            self._fail("strict replay forbids altered shot normalization filter")
        target = self._local_path(tail[-1], {".mp4"})
        tail[1], tail[-1] = source, target

    def _validate_concat(self, tail):
        if len(tail) != 33:
            self._fail("strict replay forbids altered concat arguments")
        source = self._local_path(tail[1], {".mp4"})
        filt = tail[tail.index("-filter_complex") + 1]
        if filt != (
            "[0:v:0]fps=24,setpts=PTS-STARTPTS[v0];"
            "[0:a:0]aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS[a0];"
            "[v0][a0]concat=n=1:v=1:a=1[v][a]"
        ):
            self._fail("strict replay forbids altered concat graph")
        target = self._local_path(tail[-1], {".mp4"})
        expected = [
            "-i", source, "-filter_complex", filt,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-r", "24", "-fps_mode", "cfr", "-pix_fmt", "yuv420p",
            "-video_track_timescale", "12288", "-movflags", "+faststart", target,
        ]
        if tail != expected:
            self._fail("strict replay forbids altered concat arguments")
        tail[1], tail[-1] = source, target

    def _validate_mix(self, tail):
        if len(tail) != 19:
            self._fail("strict replay forbids altered BGM mix arguments")
        first_i = tail.index("-i")
        second_i = tail.index("-i", first_i + 1)
        video = self._local_path(tail[first_i + 1], {".mp4"})
        bgm = self._local_path(tail[second_i + 1], {".mp3"})
        filt = tail[tail.index("-filter_complex") + 1]
        if filt != (
            "[1:a]volume=-12.0dB,aloop=loop=-1:size=2e9[b];"
            "[0:a][b]amix=inputs=2:duration=first:dropout_transition=0[a]"
        ):
            self._fail("strict replay forbids altered BGM mix graph")
        target = self._local_path(tail[-1], {".mp4"})
        expected = [
            "-i", video, "-i", bgm, "-filter_complex", filt,
            "-map", "0:v", "-map", "[a]", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", target,
        ]
        if tail != expected:
            self._fail("strict replay forbids altered BGM mix arguments")
        tail[first_i + 1], tail[second_i + 1], tail[-1] = video, bgm, target

    def __enter__(self):
        super().__enter__()
        stack = self._stack
        try:
            from factory.clients import bigasr
            from factory.steps import s3_shots, s5_bgm, s6_compose

            self.assets = self.root / "data" / "assets"
            self.assets.mkdir(parents=True)
            stack.enter_context(mock.patch.object(s3_shots, "ASSETS_DIR", self.assets))
            stack.enter_context(mock.patch.object(s5_bgm, "ASSETS_DIR", self.assets))
            stack.enter_context(mock.patch.object(s6_compose, "ASSETS_DIR", self.assets))
            stack.enter_context(mock.patch.object(tempfile, "tempdir", str(self.root)))
            stack.enter_context(mock.patch.object(
                bigasr.uuid, "uuid4",
                return_value=uuid.UUID("11111111-1111-4111-8111-111111111111"),
            ))
            # ``asyncio.to_thread`` wakes the Windows proactor through its
            # private loopback socket.  Permit only that one-byte wakeup;
            # every other socket send remains denied by StrictTransportReplay.
            def proactor_wakeup(sock, data, *args, **kwargs):
                try:
                    local = sock.getsockname()
                    peer = sock.getpeername()
                except OSError:
                    self._fail("strict replay forbids socket.send")
                if (
                    data != b"\0" or args or kwargs
                    or local[0] not in {"127.0.0.1", "::1"}
                    or peer[0] not in {"127.0.0.1", "::1"}
                ):
                    self._fail("strict replay forbids socket.send")
                return _ORIGINAL_SOCKET_SEND(sock, data)

            stack.enter_context(mock.patch.object(socket.socket, "send", proactor_wakeup))
            stack.enter_context(mock.patch.dict(os.environ, {
                "ARK_VIDEO_QA_MODEL": "strict-video-qa-model",
                "DEFAULT_MUSIC_PROVIDER": "silent",
                "APPEND_CTA": "false",
                "GEN_THUMBNAIL": "false",
                "GEN_MARKETING_THUMBNAIL": "false",
                "TEMP": str(self.root),
                "TMP": str(self.root),
            }))
            return self
        except BaseException:
            self.__exit__(*__import__("sys").exc_info())
            raise

    def evidence(self):
        return {
            **super().evidence(),
            "scope": "in_process_transport_real_master_compose",
        }
