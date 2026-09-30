"""Isolated subprocess probe for durable provider and candidate recovery tests.

Only test-authored provider results are supplied; no paid submit or external
network request is permitted from this process.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory.management_api import ManagementService


def _media():
    fixture = ROOT / "tests" / "fixtures" / "strict_transport" / "master_av_media.json"
    return base64.b64decode(json.loads(fixture.read_text(encoding="utf-8"))["body_base64"])


async def _frames(_video, target, _timestamps):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    frame = target / "frame-001.jpg"
    frame.write_bytes(b"validated-preview")
    return [frame]


def main():
    mode, database_path, data_dir, job_id, task_id, report_path = sys.argv[1:]
    database_path = Path(database_path).resolve()
    data_dir = Path(data_dir).resolve()
    report_path = Path(report_path).resolve()
    if not database_path.is_file() or not data_dir.is_dir() \
            or not report_path.parent.is_relative_to(data_dir):
        raise RuntimeError("recovery probe is outside its isolated data root")
    database = sqlite3.connect(str(database_path))
    database.row_factory = sqlite3.Row
    service = ManagementService(database, ROOT, worker_mode="external",
                                data_dir=data_dir, env_path=data_dir / "isolated.env")
    original_connect = socket.socket.connect

    def deny_external(connection, address):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(connection, address)
        raise AssertionError("recovery probe attempted external network")

    with mock.patch.object(socket.socket, "connect", deny_external), \
         mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS denied")), \
         mock.patch("factory.clients.seedance.text_to_video", side_effect=AssertionError("paid POST denied")):
        if mode == "anchor":
            inspected = {"status": "completed", "video": b"paid-media",
                         "video_url": "https://example.invalid/known-anchor.mp4",
                         "cost_cents": 272, "usage": {"completion_tokens": 38830}}
            with mock.patch("factory.management_api.seedance.inspect_task",
                            new=mock.AsyncMock(return_value=inspected)), \
                 mock.patch("factory.clients.ffmpeg.extract_frames", side_effect=_frames), \
                 mock.patch("factory.clients.tos.upload", return_value="https://example.invalid/anchor"):
                result = service.reconcile_provider_task(job_id, task_id)
        elif mode == "master":
            inspected = {"status": "completed", "video": _media(),
                         "video_url": "https://example.invalid/known-master.mp4",
                         "cost_cents": 7, "usage": {"completion_tokens": 1}}
            with mock.patch("factory.management_api.seedance.inspect_task",
                            new=mock.AsyncMock(return_value=inspected)):
                result = service.reconcile_provider_task(job_id, task_id)
        elif mode == "accept_candidate_crash":
            original_mkdir = Path.mkdir
            active_parent = (data_dir / "assets" / job_id / "shots").resolve()

            def crash_after_claim(path, *args, **kwargs):
                if path.resolve() == active_parent:
                    report_path.write_text(json.dumps({"pid": os.getpid(),
                                                       "mode": mode,
                                                       "crash_phase": "review_claimed"}),
                                           encoding="utf-8")
                    os._exit(89)
                return original_mkdir(path, *args, **kwargs)

            with mock.patch.object(Path, "mkdir", crash_after_claim):
                service.review_master_candidate(job_id, 1, decision="accept_candidate")
            raise AssertionError("review crash point was not reached")
        elif mode == "accept_candidate":
            result = service.review_master_candidate(job_id, 1, decision="accept_candidate")
        else:
            raise RuntimeError("unknown recovery probe mode")
    report_path.write_text(json.dumps({"pid": os.getpid(), "mode": mode,
                                       "result": result}, ensure_ascii=False), encoding="utf-8")
    database.close()


if __name__ == "__main__":
    main()
