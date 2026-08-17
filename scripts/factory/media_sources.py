"""Local audio/video ingestion into the traceable script-source library."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil

from . import DATA_DIR
from .clients import bigasr, ffmpeg
from . import script_sources


AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
DEFAULT_ARCHIVE_DIR = DATA_DIR / "script_sources" / "media"


@dataclass(frozen=True)
class MediaIngestResult:
    source_id: str
    created: bool
    media_sha256: str
    archived_path: Path
    frame_paths: tuple[Path, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sample_timestamps(duration: float, interval: float) -> list[float]:
    duration = max(0.0, float(duration))
    interval = max(1.0, float(interval))
    values: list[float] = []
    current = 0.0
    while current < duration:
        values.append(round(current, 3))
        current += interval
    if not values or duration - values[-1] >= 0.5:
        values.append(round(max(0.0, duration - 0.2), 1))
    return values


def _utterance_metadata(transcription: bigasr.Transcription) -> list[dict]:
    return [
        {"text": item.text, "start_ms": item.start_ms, "end_ms": item.end_ms}
        for item in transcription.utterances
    ]


async def ingest_media(
        db, path: str | Path, *, title: str | None = None,
        topic_id: str | None = None,
        archive_dir: str | Path = DEFAULT_ARCHIVE_DIR,
        frame_interval_sec: float = 4.0,
        asr_api_key: str | None = None,
        asr_app_id: str | None = None,
        asr_access_token: str | None = None) -> MediaIngestResult:
    source_path = Path(path).resolve()
    suffix = source_path.suffix.lower()
    if suffix not in MEDIA_EXTENSIONS:
        raise ValueError("unsupported media source file")
    media_sha256 = _sha256(source_path)
    existing = db.execute(
        "SELECT source_id, metadata_json FROM script_sources "
        "WHERE json_extract(metadata_json, '$.media_sha256')=?",
        (media_sha256,),
    ).fetchone()
    if existing:
        source_id = str(existing["source_id"])
        if topic_id:
            script_sources.link_source_to_topic(db, source_id, topic_id)
        metadata = json.loads(existing["metadata_json"] or "{}")
        return MediaIngestResult(
            source_id, False, media_sha256,
            Path(metadata.get("archived_path") or source_path),
            tuple(Path(item["path"]) for item in metadata.get("frames") or []),
        )

    media_type = "video" if suffix in VIDEO_EXTENSIONS else "audio"
    archive_root = Path(archive_dir).resolve() / media_sha256[:16]
    archive_created = not archive_root.exists()
    archive_root.mkdir(parents=True, exist_ok=True)
    archived_path = archive_root / f"source{suffix}"
    if not archived_path.exists():
        shutil.copy2(source_path, archived_path)

    duration: float | None = None
    frame_paths: list[Path] = []
    timestamps: list[float] = []
    try:
        if media_type == "video":
            duration = await ffmpeg.probe_duration(archived_path)
            audio_path = archive_root / "audio.wav"
            await ffmpeg.extract_audio_wav(archived_path, audio_path)
            timestamps = _sample_timestamps(duration, frame_interval_sec)
            frame_paths = await ffmpeg.extract_frames(
                archived_path, archive_root / "frames", timestamps
            )
        else:
            audio_path = archived_path

        transcription = await asyncio.to_thread(
            bigasr.transcribe_detailed, audio_path,
            api_key=asr_api_key, app_id=asr_app_id,
            access_token=asr_access_token,
        )
        if not transcription.text.strip():
            raise ValueError("media source contains no recognizable speech")
    except Exception:
        if archive_created and archive_root.exists():
            shutil.rmtree(archive_root, ignore_errors=True)
        raise

    metadata = {
        "media_sha256": media_sha256,
        "original_path": str(source_path),
        "archived_path": str(archived_path),
        "duration_sec": duration,
        "speech_start_ms": transcription.speech_start_ms,
        "speech_end_ms": transcription.speech_end_ms,
        "utterances": _utterance_metadata(transcription),
        "frames": [
            {"path": str(frame), "index": index,
             "timestamp_sec": timestamps[index - 1]}
            for index, frame in enumerate(frame_paths, start=1)
        ],
    }
    ingested = script_sources.ingest_text(
        db, transcription.text, title=title or source_path.stem,
        source_uri=str(source_path), source_type=media_type, metadata=metadata,
        deduplication_key=f"media:{media_sha256}",
    )
    if topic_id:
        script_sources.link_source_to_topic(db, ingested.source_id, topic_id)
    return MediaIngestResult(
        ingested.source_id, ingested.created, media_sha256, archived_path,
        tuple(frame_paths),
    )
