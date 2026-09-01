"""Deterministic local-video golden corpus for performance reconstruction."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .. import db as factory_db
from .. import full_flow_regression, script_drafts, script_sources, source_analysis
from ..clients import bigasr


def _golden_transcription() -> bigasr.Transcription:
    return bigasr.parse_transcription({
        "result": {
            "text": "设备租赁，狗都不干！那应该怎么解决？",
            "utterances": [
                {
                    "text": "设备租赁，狗都不干！",
                    "start_time": 0,
                    "end_time": 3900,
                    "speaker_id": "voice-a",
                    "speaker_confidence": 0.98,
                    "emotion": "frustrated",
                    "tone": "提高音量",
                },
                {
                    "text": "那应该怎么解决？",
                    "start_time": 4000,
                    "end_time": 7900,
                    "speaker_id": "voice-b",
                    "speaker_confidence": 0.96,
                    "emotion": "curious",
                    "tone": "questioning",
                },
            ],
        },
    })


def run_video_performance_golden(work_root: Path) -> dict[str, object]:
    """Exercise video evidence normalization and creative reconstruction."""
    root = Path(work_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    video_path = full_flow_regression._fixture_video(
        root / "source.mp4", duration_sec=8, index=1
    )
    frame_specs = (
        (1, 0.0, "挥手", "张嘴强调，眉头皱起"),
        (2, 2.0, "挥手", "语速快，面部紧张"),
        (3, 6.0, "", "画面人物保持安静"),
    )
    frames = []
    frame_results = []
    for index, timestamp, gesture, expression in frame_specs:
        path = full_flow_regression._fixture_image(
            root / f"frame-{index:03d}.png",
            color=("0xD9E7E0" if index < 3 else "0xB7C8D8"),
        )
        frames.append({
            "index": index,
            "timestamp_sec": timestamp,
            "path": str(path),
        })
        frame_results.append({
            "visual": "女销售面对镜头" if index < 3 else "客户未入镜",
            "people_count": 1 if index < 3 else 0,
            "people": ([{
                "track_id": "person-a",
                "appearance": "女销售",
                "mouth_activity": "speaking" if index < 3 else "silent",
                "facial_expression": expression,
                "gaze": "朝向镜头",
                "gesture": gesture,
                "body_action": "上身前倾强调观点" if index < 3 else "",
            }] if index < 3 else []),
            "confidence": 0.98,
        })

    transcription = _golden_transcription()
    metadata = {
        "archived_path": str(video_path),
        "duration_sec": 8.0,
        "frames": frames,
        "utterances": [
            {
                "text": item.text,
                "start_ms": item.start_ms,
                "end_ms": item.end_ms,
                "speaker_id": item.speaker_id,
                "speaker_confidence": item.speaker_confidence,
                "emotion": item.emotion,
                "tone": item.tone,
            }
            for item in transcription.utterances
        ],
    }
    structure_result = {
        "summary": "女销售以强烈情绪开场，画外客户追问",
        "character_tracks": [{
            "id": "person-a",
            "label": "女销售",
            "appearance": "正面面对镜头",
            "voice_cluster_ids": ["voice-a"],
            "confidence": 0.98,
        }],
        "segments": [
            {
                "role": "hook", "start_sec": 0, "end_sec": 4,
                "scene": "新年主题直播间", "characters": ["女销售"],
                "speaker": "女销售", "speech_mode": "on_screen",
                "visible_character_ids": ["person-a"],
                "speaker_track_id": "person-a",
                "voice_cluster_id": "voice-a", "speaker_confidence": 0.98,
                "dialogue": "设备租赁，狗都不干！",
                "emotion": "frustrated", "tone": "提高音量",
                "gesture": "无明显手势", "facial_expression": "眉头皱起",
                "evidence_frame_indices": [1, 2],
            },
            {
                "role": "conflict", "start_sec": 4, "end_sec": 8,
                "scene": "新年主题直播间", "characters": [],
                "speaker": "客户", "speech_mode": "off_screen",
                "visible_character_ids": [], "speaker_track_id": "",
                "voice_cluster_id": "voice-b", "speaker_confidence": 0.96,
                "dialogue": "那应该怎么解决？",
                "emotion": "curious", "tone": "questioning",
                "evidence_frame_indices": [3],
            },
        ],
        "catchphrases": ["设备租赁，狗都不干！"],
        "claims_to_verify": [],
    }
    responses = iter([*frame_results, structure_result])

    def analyze_json(*args, **kwargs):
        return next(responses), {"input_tokens": 0, "output_tokens": 0}

    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.executescript(factory_db.DDL)
    try:
        ingested = script_sources.ingest_text(
            database,
            transcription.text,
            title="本地视频表演金标",
            source_uri=str(video_path),
            source_type="video",
        )
        database.execute(
            "UPDATE script_sources SET metadata_json=? WHERE source_id=?",
            (json.dumps(metadata, ensure_ascii=False), ingested.source_id),
        )
        database.commit()
        report = source_analysis.analyze_source(
            database,
            ingested.source_id,
            force=True,
            analyze_json=analyze_json,
        )
        source = {
            "source_id": ingested.source_id,
            "source_type": "video",
            "title": "本地视频表演金标",
            "structure_analysis": report,
        }
        reconstruction = script_drafts._reconstruction_variant([source])
        persisted_metadata = json.loads(database.execute(
            "SELECT metadata_json FROM script_sources WHERE source_id=?",
            (ingested.source_id,),
        ).fetchone()[0] or "{}")
        segments = report.get("segments") or []
        beats = reconstruction.get("beats") or []
        first = segments[0] if segments else {}
        second = segments[1] if len(segments) > 1 else {}
        guards = {
            "gesture_preserved": first.get("gesture") == "挥手",
            "emotion_preserved": first.get("emotion") == "frustrated",
            "tone_preserved": first.get("tone") == "提高音量",
            "speaker_cluster_preserved": (
                first.get("voice_cluster_id") == "voice-a"
                and second.get("voice_cluster_id") == "voice-b"
            ),
            "speaker_bound": first.get("speaker") == "女销售",
            "offscreen_voice_stays_offscreen": (
                second.get("speech_mode") == "off_screen"
                and second.get("characters") == []
                and len(beats) > 1
                and beats[1].get("speech_mode") == "off_screen"
                and not beats[1].get("characters")
            ),
            "source_media_decodable": video_path.is_file(),
        }
        passed_guards = sorted(key for key, passed in guards.items() if passed)
        failed_guards = sorted(key for key, passed in guards.items() if not passed)
        paid_calls = int(database.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE cost_cents > 0"
        ).fetchone()[0] or 0)
        return {
            "status": "passed" if not failed_guards and not paid_calls else "failed",
            "source_type": "video_replay",
            "provider_response_type": "replay_vision_asr",
            "paid_provider_calls": paid_calls,
            "analysis_frame_count": len(
                persisted_metadata.get("visual_observations") or []
            ),
            "passed_guards": passed_guards,
            "failed_guards": failed_guards,
            "segments": segments,
            "reconstruction_beats": beats,
        }
    finally:
        database.close()
