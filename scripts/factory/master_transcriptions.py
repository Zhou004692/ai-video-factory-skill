"""Media-bound ASR artifacts derived from completed production masters."""
from __future__ import annotations

import hashlib
import json
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


def _parse(raw: str) -> bigasr.Transcription:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("master transcription payload must be an object")
    utterances = []
    for item in payload.get("utterances") or []:
        if not isinstance(item, dict):
            raise ValueError("master transcription utterance must be an object")
        utterances.append(bigasr.TimedUtterance(
            text=str(item.get("text") or ""),
            start_ms=int(item["start_ms"]),
            end_ms=int(item["end_ms"]),
            speaker_id=str(item.get("speaker_id") or ""),
            speaker_confidence=(
                None if item.get("speaker_confidence") is None
                else float(item["speaker_confidence"])
            ),
            emotion=str(item.get("emotion") or ""),
            tone=str(item.get("tone") or ""),
            pace=str(item.get("pace") or ""),
            volume=str(item.get("volume") or ""),
        ))
    return bigasr.Transcription(
        text=str(payload.get("text") or ""),
        speech_start_ms=(
            None if payload.get("speech_start_ms") is None
            else int(payload["speech_start_ms"])
        ),
        speech_end_ms=(
            None if payload.get("speech_end_ms") is None
            else int(payload["speech_end_ms"])
        ),
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
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
