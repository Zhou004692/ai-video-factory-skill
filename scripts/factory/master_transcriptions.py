"""Media-bound ASR artifacts derived from completed production masters."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sqlite3

from .clients import bigasr


def media_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _payload(transcription: bigasr.Transcription) -> dict:
    return {
        "text": transcription.text,
        "speech_start_ms": transcription.speech_start_ms,
        "speech_end_ms": transcription.speech_end_ms,
        "utterances": [{
            "text": item.text,
            "start_ms": item.start_ms,
            "end_ms": item.end_ms,
            "speaker_id": item.speaker_id,
            "speaker_confidence": item.speaker_confidence,
            "emotion": item.emotion,
            "tone": item.tone,
            "pace": item.pace,
            "volume": item.volume,
        } for item in transcription.utterances],
    }


def _timestamp(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative integer")
    number = float(value)
    if not math.isfinite(number) or not number.is_integer() or number < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return int(number)


def _confidence(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("speaker_confidence must be finite")
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError("speaker_confidence must be between zero and one")
    return number


def _parse(raw: str) -> bigasr.Transcription:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("master transcription payload must be an object")
    utterances = []
    for item in payload.get("utterances") or []:
        if not isinstance(item, dict):
            raise ValueError("master transcription utterance must be an object")
        start_ms = _timestamp(item["start_ms"], "utterance.start_ms")
        end_ms = _timestamp(item["end_ms"], "utterance.end_ms")
        if end_ms <= start_ms:
            raise ValueError("utterance.end_ms must be after start_ms")
        utterances.append(bigasr.TimedUtterance(
            text=str(item.get("text") or ""),
            start_ms=start_ms,
            end_ms=end_ms,
            speaker_id=str(item.get("speaker_id") or ""),
            speaker_confidence=_confidence(item.get("speaker_confidence")),
            emotion=str(item.get("emotion") or ""),
            tone=str(item.get("tone") or ""),
            pace=str(item.get("pace") or ""),
            volume=str(item.get("volume") or ""),
        ))
    speech_start_ms = (
        None if payload.get("speech_start_ms") is None
        else _timestamp(payload["speech_start_ms"], "speech_start_ms")
    )
    speech_end_ms = (
        None if payload.get("speech_end_ms") is None
        else _timestamp(payload["speech_end_ms"], "speech_end_ms")
    )
    if (
        speech_start_ms is not None and speech_end_ms is not None
        and speech_end_ms < speech_start_ms
    ):
        raise ValueError("speech_end_ms must not precede speech_start_ms")
    return bigasr.Transcription(
        text=str(payload.get("text") or ""),
        speech_start_ms=speech_start_ms,
        speech_end_ms=speech_end_ms,
        utterances=tuple(utterances),
    )


def persist(
    database: sqlite3.Connection,
    job_id: str,
    master_idx: int,
    media_path: str | Path,
    transcription: bigasr.Transcription,
) -> None:
    """Bind structured ASR to exact media bytes inside caller transaction."""
    database.execute(
        "INSERT INTO master_transcriptions(job_id,master_idx,media_sha256,"
        "transcription_json) VALUES(?,?,?,?) "
        "ON CONFLICT(job_id,master_idx,media_sha256) DO UPDATE SET "
        "transcription_json=excluded.transcription_json,"
        "updated_at=CURRENT_TIMESTAMP",
        (
            job_id,
            int(master_idx),
            media_sha256(media_path),
            json.dumps(_payload(transcription), ensure_ascii=False, sort_keys=True),
        ),
    )


def load_matching(
    database: sqlite3.Connection,
    job_id: str,
    master_idx: int,
    media_path: str | Path,
) -> bigasr.Transcription | None:
    """Return ASR only when it belongs to the current master media bytes."""
    current_sha256 = media_sha256(media_path)
    row = database.execute(
        "SELECT transcription_json FROM master_transcriptions "
        "WHERE job_id=? AND master_idx=? AND media_sha256=?",
        (job_id, int(master_idx), current_sha256),
    ).fetchone()
    if row is None:
        return None
    try:
        return _parse(str(row["transcription_json"]))
    except (
        KeyError, TypeError, ValueError, OverflowError, json.JSONDecodeError,
    ):
        return None
