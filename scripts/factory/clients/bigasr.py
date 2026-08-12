"""Volcengine BigASR flash client used to verify Seedance native speech."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import os
from pathlib import Path
import uuid

import httpx


FLASH_URL = (
    "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
)
RESOURCE_ID = "volc.bigasr.auc_turbo"


@dataclass(frozen=True)
class TimedUtterance:
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class Transcription:
    text: str
    speech_start_ms: int | None = None
    speech_end_ms: int | None = None
    utterances: tuple[TimedUtterance, ...] = ()


def parse_transcription(payload: dict) -> Transcription:
    result = payload.get("result") or {}
    starts: list[int] = []
    ends: list[int] = []
    utterances: list[TimedUtterance] = []
    for utterance in result.get("utterances") or []:
        try:
            start_ms = int(utterance["start_time"])
            end_ms = int(utterance["end_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if end_ms <= start_ms:
            continue
        starts.append(start_ms)
        ends.append(end_ms)
        text = str(utterance.get("text") or "").strip()
        if text:
            utterances.append(TimedUtterance(text, start_ms, end_ms))
    return Transcription(
        text=str(result.get("text") or "").strip(),
        speech_start_ms=min(starts) if starts else None,
        speech_end_ms=max(ends) if ends else None,
        utterances=tuple(utterances),
    )


def parse_transcript(payload: dict) -> str:
    return parse_transcription(payload).text


def transcribe_detailed(
    audio_path: str | Path,
    *,
    app_id: str | None = None,
    access_token: str | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: int = 120,
) -> Transcription:
    app_id = app_id or os.environ.get("VOLC_ASR_APPID") or os.environ.get(
        "VOLC_TTS_APPID"
    )
    access_token = (
        access_token
        or os.environ.get("VOLC_ASR_TOKEN")
        or os.environ.get("VOLC_TTS_TOKEN")
    )
    if not app_id or not access_token:
        raise RuntimeError("VOLC_ASR_APPID / VOLC_ASR_TOKEN not set")

    audio_path = Path(audio_path)
    headers = {
        "X-Api-App-Key": app_id,
        "X-Api-Access-Key": access_token,
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Api-Sequence": "-1",
    }
    payload = {
        "user": {"uid": app_id},
        "audio": {
            "data": base64.b64encode(audio_path.read_bytes()).decode("ascii"),
            "format": audio_path.suffix.lower().lstrip(".") or "wav",
        },
        "request": {"model_name": "bigmodel", "enable_itn": True},
    }
    with httpx.Client(timeout=timeout, transport=transport) as client:
        response = client.post(FLASH_URL, headers=headers, json=payload)
    response.raise_for_status()
    status = response.headers.get("X-Api-Status-Code", "20000000")
    if status != "20000000":
        raise RuntimeError(
            f"BigASR failed status={status}: "
            f"{response.headers.get('X-Api-Message', '')}"
        )
    return parse_transcription(response.json())


def transcribe(
    audio_path: str | Path,
    *,
    app_id: str | None = None,
    access_token: str | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: int = 120,
) -> str:
    """Backward-compatible text-only transcription API."""
    return transcribe_detailed(
        audio_path,
        app_id=app_id,
        access_token=access_token,
        transport=transport,
        timeout=timeout,
    ).text
