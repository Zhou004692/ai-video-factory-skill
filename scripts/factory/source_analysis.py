"""OCR/visual observation and narrative-structure analysis for sources."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .clients import image_vision


STORY_ROLES = frozenset({
    "hook", "setup", "pain", "conflict", "turn", "proof",
    "catchphrase", "cta", "other",
})


def _text(value) -> str:
    if isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def normalize_frame_observation(raw: dict, *, frame_index: int,
                                timestamp_sec: float,
                                path: str | Path) -> dict:
    try:
        people_count = max(0, int(raw.get("people_count", raw.get("people", 0))))
    except (TypeError, ValueError):
        people_count = 0
    try:
        confidence = min(1.0, max(0.0, float(raw.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    objects = raw.get("objects") or []
    if not isinstance(objects, list):
        objects = [objects]
    return {
        "frame_index": int(frame_index),
        "timestamp_sec": round(max(0.0, float(timestamp_sec)), 3),
        "path": str(path),
        "ocr_text": _text(raw.get("ocr_text")),
        "visual": _text(raw.get("visual")),
        "people_count": people_count,
        "objects": [str(item).strip() for item in objects if str(item).strip()],
        "confidence": confidence,
    }


def normalize_structure_report(raw: dict, *, duration_sec: float,
                               observations: list[dict]) -> dict:
    duration = max(0.0, float(duration_sec))
    segments: list[dict] = []
    for segment in raw.get("segments") or []:
        role = str(segment.get("role") or "").strip().lower()
        if role not in STORY_ROLES:
            raise ValueError(f"unsupported story role: {role}")
        start = min(duration, max(0.0, float(segment.get("start_sec") or 0)))
        end = min(duration, max(start, float(segment.get("end_sec") or start)))
        if end <= start:
            continue
        segments.append({
            "role": role,
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "evidence": _text(segment.get("evidence")),
        })
    segments.sort(key=lambda item: (item["start_sec"], item["end_sec"]))
    return {
        "summary": _text(raw.get("summary")),
        "segments": segments,
        "catchphrases": [
            str(item).strip() for item in raw.get("catchphrases") or []
            if str(item).strip()
        ],
        "claims_to_verify": [
            str(item).strip() for item in raw.get("claims_to_verify") or []
            if str(item).strip()
        ],
        "visual_observation_count": len(observations),
    }


def _frame_prompt(timestamp: float) -> str:
    return (
        f"你正在观察一条短视频在 {timestamp:.3f} 秒的真实抽帧。只描述可见事实，"
        "不要推测人物身份、品牌或创作意图。逐字抄录清晰可读文字；无法确认则留空。"
        "只输出 JSON："
        '{"ocr_text":"可读文字，按行分隔",'
        '"visual":"主体、场景、构图和动作的客观描述",'
        '"people_count":0,"objects":["物件"],"confidence":0.0}'
    )


def _structure_prompt(content: str, metadata: dict,
                      observations: list[dict]) -> str:
    evidence = {
        "duration_sec": metadata.get("duration_sec") or 0,
        "transcript": content,
        "timed_utterances": metadata.get("utterances") or [],
        "frame_observations": observations,
    }
    return (
        "你只基于以下 ASR 与抽帧观察拆解短视频结构，不得补充不存在的事实。"
        "role 只能使用 hook/setup/pain/conflict/turn/proof/catchphrase/cta/other。"
        "无法确认的声明放入 claims_to_verify。只输出 JSON："
        '{"summary":"一句话结构摘要","segments":[{"role":"hook",'
        '"start_sec":0,"end_sec":1,"evidence":"原始证据"}],'
        '"catchphrases":["原句"],"claims_to_verify":["待核验声明"]}\n\n'
        + json.dumps(evidence, ensure_ascii=False)
    )


def analyze_source(db, source_id: str, *, max_frames: int = 8,
                   force: bool = False,
                   analyze_json: Callable = image_vision.analyze_json) -> dict:
    row = db.execute(
        "SELECT content, source_type, metadata_json FROM script_sources "
        "WHERE source_id=?", (source_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"script source not found: {source_id}")
    metadata = json.loads(row["metadata_json"] or "{}")
    if row["source_type"] not in {"video", "audio"}:
        raise ValueError("source analysis requires an audio or video source")
    cached = metadata.get("structure_analysis")
    if cached and not force:
        return cached
    usage = {"input_tokens": 0, "output_tokens": 0}
    observations: list[dict] = []
    for frame in (metadata.get("frames") or [])[:max(0, int(max_frames))]:
        path = Path(frame["path"])
        if not path.exists():
            continue
        raw, call_usage = analyze_json(
            _frame_prompt(float(frame.get("timestamp_sec") or 0)),
            image_path=path,
        )
        observations.append(normalize_frame_observation(
            raw, frame_index=int(frame["index"]),
            timestamp_sec=float(frame.get("timestamp_sec") or 0), path=path,
        ))
        usage["input_tokens"] += int(call_usage.get("input_tokens") or 0)
        usage["output_tokens"] += int(call_usage.get("output_tokens") or 0)
    raw_report, call_usage = analyze_json(
        _structure_prompt(str(row["content"]), metadata, observations)
    )
    usage["input_tokens"] += int(call_usage.get("input_tokens") or 0)
    usage["output_tokens"] += int(call_usage.get("output_tokens") or 0)
    report = normalize_structure_report(
        raw_report, duration_sec=float(metadata.get("duration_sec") or 0),
        observations=observations,
    )
    metadata["visual_observations"] = observations
    metadata["structure_analysis"] = report
    metadata["analysis_usage"] = usage
    db.execute(
        "UPDATE script_sources SET metadata_json=? WHERE source_id=?",
        (json.dumps(metadata, ensure_ascii=False), source_id),
    )
    db.commit()
    return report
