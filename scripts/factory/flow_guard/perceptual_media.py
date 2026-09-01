"""Offline final-media perceptual checks and mutation calibration.

The corpus is generated locally and never calls a model provider.  A passing
result means both that a clean moving clip is accepted and that four known bad
media mutations are independently detected.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

from ..media_runtime import resolve_binary


_BLACK_RE = re.compile(
    r"black_start:(?P<start>-?[\d.]+)\s+black_end:(?P<end>-?[\d.]+)"
    r"\s+black_duration:(?P<duration>[\d.]+)"
)
_FREEZE_TOKEN_RE = re.compile(
    r"freeze_(?P<kind>start|end|duration):\s*(?P<value>-?[\d.]+)"
)


def parse_blackdetect(output: str) -> tuple[dict[str, float], ...]:
    """Parse FFmpeg blackdetect evidence without depending on log prefixes."""
    return tuple(
        {
            "start": float(match.group("start")),
            "end": float(match.group("end")),
            "duration": float(match.group("duration")),
        }
        for match in _BLACK_RE.finditer(output or "")
    )


def parse_freezedetect(output: str) -> tuple[dict[str, float], ...]:
    """Pair FFmpeg freezedetect start/duration/end messages."""
    events: list[dict[str, float]] = []
    current: dict[str, float] = {}
    for match in _FREEZE_TOKEN_RE.finditer(output or ""):
        kind = match.group("kind")
        current[kind] = float(match.group("value"))
        if kind == "end" and {"start", "duration", "end"} <= current.keys():
            events.append({
                "start": current["start"],
                "end": current["end"],
                "duration": current["duration"],
            })
            current = {}
    return tuple(events)


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _probe(path: Path) -> dict[str, object]:
    completed = _run([
        str(resolve_binary("ffprobe")), "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,duration,avg_frame_rate",
        "-of", "json", str(path),
    ])
    return json.loads(completed.stdout or "{}")


def _frame_timestamps(path: Path) -> tuple[float, ...]:
    completed = _run([
        str(resolve_binary("ffprobe")), "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "frame=best_effort_timestamp_time",
        "-of", "csv=p=0", str(path),
    ])
    values: list[float] = []
    for raw in completed.stdout.splitlines():
        try:
            values.append(float(raw.strip().split(",", 1)[0]))
        except (TypeError, ValueError):
            continue
    return tuple(values)


def _filter_output(path: Path, filter_spec: str) -> str:
    completed = subprocess.run(
        [
            str(resolve_binary("ffmpeg")), "-hide_banner", "-nostdin",
            "-i", str(path), "-vf", filter_spec, "-an", "-f", "null", "-",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return (completed.stdout or "") + "\n" + (completed.stderr or "")


def analyze_perceptual_media(path: Path) -> dict[str, object]:
    """Detect perceptual defects that decode-only ffprobe checks miss."""
    media_path = Path(path).resolve()
    payload = _probe(media_path)
    streams = payload.get("streams") or []
    video = next(
        (item for item in streams if item.get("codec_type") == "video"), {}
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"), {}
    )
    raw_rate = str(video.get("avg_frame_rate") or "0/1")
    try:
        numerator, denominator = raw_rate.split("/", 1)
        fps = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    timestamps = _frame_timestamps(media_path)
    expected_interval = 1.0 / fps if fps > 0 else 0.04
    gap_threshold = max(0.15, expected_interval * 3.0)
    timestamp_gaps = [
        round(current - previous, 6)
        for previous, current in zip(timestamps, timestamps[1:])
        if current - previous > gap_threshold
    ]
    black_segments = [
        item for item in parse_blackdetect(
            _filter_output(media_path, "blackdetect=d=0.35:pic_th=0.98")
        )
        if item["duration"] >= 0.35
    ]
    freeze_segments = [
        item for item in parse_freezedetect(
            _filter_output(media_path, "freezedetect=n=-50dB:d=0.55")
        )
        if item["duration"] >= 0.55
    ]

    def duration(stream: object) -> float:
        try:
            return float((stream or {}).get("duration") or 0)
        except (AttributeError, TypeError, ValueError):
            return 0.0

    video_duration = duration(video)
    audio_duration = duration(audio)
    codes: list[str] = []
    if black_segments:
        codes.append("final_media_black_segment")
    if freeze_segments:
        codes.append("final_media_freeze_segment")
    if timestamp_gaps:
        codes.append("final_media_frame_cadence_gap")
    if video_duration and audio_duration and abs(video_duration - audio_duration) > 0.35:
        codes.append("final_media_av_duration_mismatch")
    return {
        "status": "passed" if not codes else "failed",
        "finding_codes": sorted(codes),
        "metrics": {
            "fps": round(fps, 3),
            "video_duration_sec": round(video_duration, 3),
            "audio_duration_sec": round(audio_duration, 3),
            "frame_count": len(timestamps),
            "max_timestamp_gap_sec": max(timestamp_gaps, default=0),
            "black_segments": black_segments,
            "freeze_segments": freeze_segments,
        },
    }


def _generate_fixture(path: Path, mutation: str) -> Path:
    ffmpeg = str(resolve_binary("ffmpeg"))
    common = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    if mutation == "freeze_segment":
        command = common + [
            "-f", "lavfi", "-i",
            "testsrc2=size=320x180:rate=25:duration=1[t0];"
            "color=c=red:size=320x180:rate=25:duration=0.8[still];"
            "testsrc2=size=320x180:rate=25:duration=1[t1];"
            "[t0][still][t1]concat=n=3:v=1:a=0",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2.8",
        ]
    elif mutation == "black_segment":
        command = common + [
            "-f", "lavfi", "-i",
            "testsrc2=size=320x180:rate=25:duration=1[t0];"
            "color=c=black:size=320x180:rate=25:duration=0.8[b];"
            "testsrc2=size=320x180:rate=25:duration=1[t1];"
            "[t0][b][t1]concat=n=3:v=1:a=0",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2.8",
        ]
    elif mutation == "timestamp_gap":
        command = common + [
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-vf", "select='not(between(t,1,1.5))'", "-fps_mode", "vfr",
        ]
    elif mutation == "av_mismatch":
        command = common + [
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        ]
    else:
        command = common + [
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        ]
    command += [
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
        "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-movflags", "+faststart", str(path),
    ]
    _run(command)
    return path


def run_perceptual_media_golden(work_root: Path) -> dict[str, object]:
    """Calibrate media detectors against clean and deliberately bad clips."""
    root = Path(work_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    clean = analyze_perceptual_media(_generate_fixture(root / "clean.mp4", "clean"))
    expected = {
        "freeze_segment": "final_media_freeze_segment",
        "black_segment": "final_media_black_segment",
        "timestamp_gap": "final_media_frame_cadence_gap",
        "av_mismatch": "final_media_av_duration_mismatch",
    }
    results = []
    for mutation_id, expected_code in expected.items():
        analysis = analyze_perceptual_media(
            _generate_fixture(root / f"{mutation_id}.mp4", mutation_id)
        )
        observed = analysis["finding_codes"]
        results.append({
            "mutation_id": mutation_id,
            "expected_code": expected_code,
            "killed": expected_code in observed,
            "observed_codes": observed,
            "metrics": analysis["metrics"],
        })
    killed = sum(bool(item["killed"]) for item in results)
    survived = len(results) - killed
    clean_findings = clean["finding_codes"]
    return {
        "status": "passed" if not clean_findings and not survived else "failed",
        "paid_provider_calls": 0,
        "clean_findings": clean_findings,
        "clean_metrics": clean["metrics"],
        "mutation_count": len(results),
        "killed": killed,
        "survived": survived,
        "score_pct": round(killed / len(results) * 100, 2),
        "results": results,
    }
