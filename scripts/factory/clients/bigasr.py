"""Volcengine BigASR flash client used to verify Seedance native speech."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import time
import uuid

import httpx

from .. import provider_runtime


FLASH_URL = (
    "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
)
STANDARD_SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
STANDARD_QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
LEGACY_RESOURCE_ID = "volc.bigasr.auc_turbo"
SEED_ASR_2_RESOURCE_ID = "volc.seedasr.auc"


@dataclass(frozen=True)
class TimedUtterance:
    text: str
    start_ms: int
    end_ms: int
    speaker_id: str = ""
    speaker_confidence: float | None = None
    emotion: str = ""
    tone: str = ""
    pace: str = ""
    volume: str = ""


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
            additions = utterance.get("additions") or {}
            if not isinstance(additions, dict):
                additions = {}
            speaker_id = str(
                utterance.get("speaker_id")
                or utterance.get("voice_cluster_id")
                or utterance.get("spk_id")
                or utterance.get("speaker")
                or additions.get("speaker_id")
                or additions.get("speaker")
                or ""
            ).strip()
            confidence_raw = (
                utterance.get("speaker_confidence")
                if utterance.get("speaker_confidence") is not None
                else additions.get("speaker_confidence")
            )
            try:
                speaker_confidence = min(1.0, max(0.0, float(confidence_raw)))
            except (TypeError, ValueError):
                speaker_confidence = None
            utterances.append(TimedUtterance(
                text=text,
                start_ms=start_ms,
                end_ms=end_ms,
                speaker_id=speaker_id,
                speaker_confidence=speaker_confidence,
                emotion=str(
                    utterance.get("emotion") or additions.get("emotion") or ""
                ).strip(),
                tone=str(
                    utterance.get("tone") or additions.get("tone") or ""
                ).strip(),
                pace=str(
                    utterance.get("pace")
                    or utterance.get("speech_rate")
                    or additions.get("pace")
                    or additions.get("speech_rate")
                    or ""
                ).strip(),
                volume=str(
                    utterance.get("volume") or additions.get("volume") or ""
                ).strip(),
            ))
    return Transcription(
        text=str(result.get("text") or "").strip(),
        speech_start_ms=min(starts) if starts else None,
        speech_end_ms=max(ends) if ends else None,
        utterances=tuple(utterances),
    )


def parse_transcript(payload: dict) -> str:
    return parse_transcription(payload).text


def replay_request(audio_path: str | Path) -> dict:
    """Build a credential-free, content-addressed ASR fixture request."""
    path = Path(audio_path)
    return {
        "audio_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "format": path.suffix.lower().lstrip(".") or "wav",
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_speaker_info": True,
            "enable_emotion_detection": True,
            "show_volume": True,
            "show_speech_rate": True,
        },
    }


def transcribe_detailed(
    audio_path: str | Path,
    *,
    api_key: str | None = None,
    app_id: str | None = None,
    access_token: str | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: int = 120,
    poll_interval: float = 0.5,
    max_wait_seconds: int = 120,
) -> Transcription:
    audio_path = Path(audio_path)
    runtime = provider_runtime.from_environment()
    if runtime.mode == "replay":
        replay = runtime.lookup(
            "volc.asr.transcribe", replay_request(audio_path)
        )
        result = replay.get("result")
        if not isinstance(result, dict):
            raise provider_runtime.ReplayFixtureMissing(
                "ASR replay response requires a result object"
            )
        return parse_transcription({"result": result})

    explicit_legacy = bool(app_id and access_token)
    api_key = api_key or (
        None if explicit_legacy else os.environ.get("VOLC_ASR_API_KEY")
    )
    app_id = app_id or os.environ.get("VOLC_ASR_APPID") or os.environ.get(
        "VOLC_TTS_APPID"
    )
    access_token = (
        access_token
        or os.environ.get("VOLC_ASR_TOKEN")
        or os.environ.get("VOLC_TTS_TOKEN")
    )
    if not api_key and (not app_id or not access_token):
        raise RuntimeError(
            "VOLC_ASR_API_KEY or VOLC_ASR_APPID / VOLC_ASR_TOKEN not set"
        )

    request_id = str(uuid.uuid4())
    headers = {
        "X-Api-Resource-Id": (
            SEED_ASR_2_RESOURCE_ID if api_key else LEGACY_RESOURCE_ID
        ),
        "X-Api-Request-Id": request_id,
        "X-Api-Sequence": "-1",
    }
    if api_key:
        headers["X-Api-Key"] = api_key
    else:
        headers["X-Api-App-Key"] = app_id
        headers["X-Api-Access-Key"] = access_token
    request_options = {
        "model_name": "bigmodel",
        "enable_itn": True,
        "enable_speaker_info": True,
    }
    if api_key:
        # The standard submit/query API supports customer-service metadata.
        # Keep these fields off the flash request because the flash contract
        # explicitly removes emotion, volume and speech-rate options.
        request_options.update({
            "enable_emotion_detection": True,
            "show_volume": True,
            "show_speech_rate": True,
        })
    payload = {
        "user": {"uid": api_key or app_id},
        "audio": {
            "data": base64.b64encode(audio_path.read_bytes()).decode("ascii"),
            "format": audio_path.suffix.lower().lstrip(".") or "wav",
        },
        "request": request_options,
    }
    with httpx.Client(timeout=timeout, transport=transport) as client:
        if api_key:
            response = client.post(STANDARD_SUBMIT_URL, headers=headers, json=payload)
            response.raise_for_status()
            status = response.headers.get("X-Api-Status-Code", "20000000")
            if status != "20000000":
                raise RuntimeError(
                    f"SeedASR submit failed status={status}: "
                    f"{response.headers.get('X-Api-Message', '')}"
                )
            deadline = time.monotonic() + max_wait_seconds
            while True:
                response = client.post(STANDARD_QUERY_URL, headers=headers, json={})
                response.raise_for_status()
                status = response.headers.get("X-Api-Status-Code", "20000000")
                if status == "20000000":
                    break
                if status not in {"20000001", "20000002", "20000003"}:
                    raise RuntimeError(
                        f"SeedASR query failed status={status}: "
                        f"{response.headers.get('X-Api-Message', '')}"
                    )
                if time.monotonic() >= deadline:
                    raise RuntimeError("SeedASR query timed out")
                time.sleep(max(0.0, poll_interval))
        else:
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
    api_key: str | None = None,
    app_id: str | None = None,
    access_token: str | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: int = 120,
    poll_interval: float = 0.5,
    max_wait_seconds: int = 120,
) -> str:
    """Backward-compatible text-only transcription API."""
    return transcribe_detailed(
        audio_path,
        api_key=api_key,
        app_id=app_id,
        access_token=access_token,
        transport=transport,
        timeout=timeout,
        poll_interval=poll_interval,
        max_wait_seconds=max_wait_seconds,
    ).text
