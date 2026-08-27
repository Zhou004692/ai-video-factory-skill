"""Local audio/video ingestion into the traceable script-source library."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import uuid

from . import DATA_DIR
from .clients import bigasr, ffmpeg
from . import script_sources


AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
DEFAULT_ARCHIVE_DIR = DATA_DIR / "script_sources" / "media"
DEFAULT_MAX_EXTRACTION_FRAMES = 48
# Keep an early, midpoint and late observation inside every speech turn.  One
# midpoint frame alone misses gesture preparation/release; early/late alone
# can miss the peak facial expression that carries the line.
PERFORMANCE_SAMPLE_RATIOS = (0.35, 0.5, 0.85)


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


def _evenly_select(values: list[float], limit: int) -> list[float]:
    if limit <= 0:
        return []
    if len(values) <= limit:
        return values
    if limit == 1:
        return [values[len(values) // 2]]
    last = len(values) - 1
    indices = [round(position * last / (limit - 1)) for position in range(limit)]
    return [values[index] for index in indices]


def performance_evidence_timestamps(
    utterances, duration: float,
) -> list[float]:
    """Return early/late evidence times so transient performance is observable."""
    duration = max(0.0, float(duration))
    values: set[float] = set()
    for utterance in utterances or []:
        if isinstance(utterance, dict):
            start_raw = utterance.get("start_ms")
            end_raw = utterance.get("end_ms")
        else:
            start_raw = getattr(utterance, "start_ms", None)
            end_raw = getattr(utterance, "end_ms", None)
        try:
            start = max(0.0, float(start_raw) / 1000.0)
            end = min(duration, max(start, float(end_raw) / 1000.0))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        for ratio in PERFORMANCE_SAMPLE_RATIOS:
            values.add(round(start + (end - start) * ratio, 3))
    return sorted(values)


def _performance_timestamps(
    duration: float,
    interval: float,
    transcription: bigasr.Transcription,
    *,
    max_frames: int = DEFAULT_MAX_EXTRACTION_FRAMES,
) -> list[float]:
    """Combine timeline coverage with speech-turn evidence frames."""
    duration = max(0.0, float(duration))
    limit = max(1, int(max_frames))
    uniform = _sample_timestamps(duration, interval)
    performance_points = performance_evidence_timestamps(
        transcription.utterances, duration
    )
    if len(performance_points) >= limit:
        return sorted(_evenly_select(performance_points, limit))
    evidence = set(performance_points)
    remaining = limit - len(evidence)
    for timestamp in _evenly_select(
        [item for item in uniform if item not in evidence], remaining
    ):
        evidence.add(timestamp)
    return sorted(evidence)


async def refresh_analysis_frames(
    db, source_id: str, *, frame_interval_sec: float = 4.0,
    max_frames: int = DEFAULT_MAX_EXTRACTION_FRAMES,
) -> list[dict]:
    """Rebuild temporal evidence frames for an existing archived video source."""
    row = db.execute(
        "SELECT source_type,metadata_json FROM script_sources WHERE source_id=?",
        (str(source_id or "").strip(),),
    ).fetchone()
    if not row:
        raise ValueError("script source not found")
    if str(row["source_type"] or "") != "video":
        return []
    metadata = json.loads(row["metadata_json"] or "{}")
    archived_path = Path(str(metadata.get("archived_path") or "")).resolve()
    if not archived_path.is_file():
        raise ValueError("archived video source is missing")
    duration = max(0.0, float(metadata.get("duration_sec") or 0))
    performance_points = performance_evidence_timestamps(
        metadata.get("utterances") or [], duration
    )
    uniform = _sample_timestamps(duration, frame_interval_sec)
    timestamps = sorted(set(performance_points + uniform))
    if len(timestamps) > max(1, int(max_frames)):
        timestamps = sorted(_evenly_select(timestamps, max_frames))
    existing = metadata.get("frames") or []
    output_dir = (
        Path(str(existing[0].get("path"))).resolve().parent
        if existing and existing[0].get("path")
        else archived_path.parent / "frames"
    )
    frame_paths = await ffmpeg.extract_frames(
        archived_path, output_dir, timestamps
    )
    frames = [
        {"path": str(frame), "index": index,
         "timestamp_sec": timestamps[index - 1]}
        for index, frame in enumerate(frame_paths, start=1)
    ]
    metadata["frames"] = frames
    metadata.pop("visual_observations", None)
    metadata.pop("structure_analysis", None)
    metadata.pop("analysis_usage", None)
    db.execute(
        "UPDATE script_sources SET metadata_json=? WHERE source_id=?",
        (json.dumps(metadata, ensure_ascii=False), str(source_id).strip()),
    )
    db.commit()
    return frames


def _utterance_metadata(transcription: bigasr.Transcription) -> list[dict]:
    result: list[dict] = []
    for item in transcription.utterances:
        metadata = {
            "text": item.text,
            "start_ms": item.start_ms,
            "end_ms": item.end_ms,
        }
        for field in ("speaker_id", "emotion", "tone", "pace", "volume"):
            value = getattr(item, field, "")
            if value:
                metadata[field] = value
        confidence = getattr(item, "speaker_confidence", None)
        if confidence is not None:
            metadata["speaker_confidence"] = confidence
        result.append(metadata)
    return result


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
    ingest_id = uuid.uuid4().hex
    media_type = "video" if suffix in VIDEO_EXTENSIONS else "audio"
    archive_root = (
        Path(archive_dir).resolve() / media_sha256[:16] / ingest_id[:12]
    )
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
        else:
            audio_path = archived_path

        transcription = await asyncio.to_thread(
            bigasr.transcribe_detailed, audio_path,
            api_key=asr_api_key, app_id=asr_app_id,
            access_token=asr_access_token,
        )
        if not transcription.text.strip():
            raise ValueError("media source contains no recognizable speech")
        if media_type == "video":
            timestamps = _performance_timestamps(
                duration or 0.0, frame_interval_sec, transcription
            )
            frame_paths = await ffmpeg.extract_frames(
                archived_path, archive_root / "frames", timestamps
            )
    except Exception:
        if archive_created and archive_root.exists():
            shutil.rmtree(archive_root, ignore_errors=True)
        raise

    metadata = {
        "ingest_id": ingest_id,
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
        deduplication_key=f"media:{media_sha256}:{ingest_id}",
    )
    if topic_id:
        script_sources.link_source_to_topic(db, ingested.source_id, topic_id)
    return MediaIngestResult(
        ingested.source_id, ingested.created, media_sha256, archived_path,
        tuple(frame_paths),
    )
