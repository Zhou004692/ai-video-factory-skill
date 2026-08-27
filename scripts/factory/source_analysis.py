"""OCR/visual observation and narrative-structure analysis for sources."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from . import media_sources
from .clients import image_vision


STORY_ROLES = frozenset({
    "hook", "setup", "pain", "conflict", "turn", "proof",
    "catchphrase", "cta", "other",
})
DEFAULT_MAX_ANALYSIS_FRAMES = 16
STRUCTURE_MAX_TOKENS = 4000
SPEAKER_REVIEW_CONFIDENCE = 0.75


def _select_timeline_frames(frames: list[dict], *, max_frames: int) -> list[dict]:
    """Select representative frames across the complete source timeline."""
    items = list(frames or [])
    limit = max(0, int(max_frames))
    if not items or limit == 0:
        return []
    if len(items) <= limit:
        return items
    if limit == 1:
        return [items[len(items) // 2]]
    last = len(items) - 1
    indices = [round(position * last / (limit - 1)) for position in range(limit)]
    return [items[index] for index in indices]


def _select_analysis_frames(
    frames: list[dict], *, utterances: list[dict], max_frames: int,
) -> list[dict]:
    """Keep timeline coverage while reserving evidence at speech turns."""
    items = list(frames or [])
    limit = max(0, int(max_frames))
    if not items or limit == 0:
        return []
    if len(items) <= limit:
        return items
    if limit == 1:
        return [items[len(items) // 2]]

    selected = {0, len(items) - 1}
    duration = max(
        float(item.get("timestamp_sec") or 0) for item in items
    )
    speech_capacity = max(0, limit - len(selected))
    # Reserve one midpoint for every speech turn before spending remaining
    # capacity on early/late performance frames.  Flattening all evidence
    # points first can let one long/early utterance crowd later speakers out.
    turn_midpoints: list[float] = []
    for utterance in utterances or []:
        try:
            start = max(0.0, float(utterance.get("start_ms")) / 1000.0)
            end = min(
                duration,
                max(start, float(utterance.get("end_ms")) / 1000.0),
            )
        except (AttributeError, TypeError, ValueError):
            continue
        if end > start:
            turn_midpoints.append(round(start + (end - start) * 0.5, 3))
    if len(turn_midpoints) > speech_capacity:
        turn_midpoints = media_sources._evenly_select(
            turn_midpoints, speech_capacity
        )
    supplemental = media_sources.performance_evidence_timestamps(
        utterances, duration
    )
    evidence_times = list(dict.fromkeys([*turn_midpoints, *supplemental]))
    speech_indices: list[int] = []
    for timestamp in evidence_times:
        if len(speech_indices) >= speech_capacity:
            break
        nearest = min(
            range(len(items)),
            key=lambda index: abs(
                float(items[index].get("timestamp_sec") or 0) - timestamp
            ),
        )
        if nearest not in selected and nearest not in speech_indices:
            speech_indices.append(nearest)
    selected.update(speech_indices)

    remaining = limit - len(selected)
    if remaining > 0:
        candidates = [
            index for index in range(len(items)) if index not in selected
        ]
        if len(candidates) <= remaining:
            selected.update(candidates)
        elif remaining == 1:
            selected.add(candidates[len(candidates) // 2])
        else:
            last = len(candidates) - 1
            selected.update(
                candidates[round(position * last / (remaining - 1))]
                for position in range(remaining)
            )
    return [items[index] for index in sorted(selected)]


def _text(value) -> str:
    if isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _confidence(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(min(1.0, max(0.0, float(value))), 4)
    except (TypeError, ValueError):
        return None


def _normalize_people(raw_people) -> list[dict]:
    if not isinstance(raw_people, list):
        return []
    people: list[dict] = []
    for index, person in enumerate(raw_people, 1):
        if not isinstance(person, dict):
            continue
        track_id = _text(
            person.get("track_id") or person.get("person_id")
            or person.get("id")
        )
        normalized = {
            "track_id": track_id or f"frame-person-{index}",
            "appearance": _text(person.get("appearance")),
            "position": _text(person.get("position")),
            "mouth_activity": _text(person.get("mouth_activity")),
            "facial_expression": _text(person.get("facial_expression")),
            "gaze": _text(person.get("gaze")),
            "gesture": _text(person.get("gesture")),
            "body_action": _text(person.get("body_action")),
        }
        people.append(normalized)
    return people


def _normalize_character_tracks(raw_tracks) -> list[dict]:
    if not isinstance(raw_tracks, list):
        return []
    tracks: list[dict] = []
    for index, track in enumerate(raw_tracks, 1):
        if not isinstance(track, dict):
            continue
        track_id = _text(track.get("id") or track.get("track_id"))
        if not track_id:
            track_id = f"person-{index}"
        tracks.append({
            "id": track_id,
            "label": _text(track.get("label") or track.get("name")),
            "appearance": _text(track.get("appearance")),
            "voice_cluster_ids": [
                _text(item) for item in track.get("voice_cluster_ids") or []
                if _text(item)
            ],
            "confidence": _confidence(track.get("confidence")),
        })
    return tracks


_NON_EVIDENCE_MARKERS = (
    "无明显", "无可见", "未识别", "无法识别", "不确定", "看不清",
    "unclear", "unknown", "none", "not visible", "no visible",
)


def _is_positive_observation(value) -> bool:
    text = _text(value)
    lowered = text.lower()
    return bool(text) and not any(
        marker in lowered for marker in _NON_EVIDENCE_MARKERS
    )


def _observed_performance_values(
    observations: list[dict], *, start: float, end: float,
    frame_indices: list,
) -> dict[str, list[str]]:
    expected_indices = {
        int(item) for item in frame_indices if str(item).strip().isdigit()
    }
    values = {"gesture": [], "body_action": []}
    for observation in observations or []:
        try:
            frame_index = int(observation.get("frame_index") or -1)
            timestamp = float(observation.get("timestamp_sec") or 0)
        except (TypeError, ValueError):
            continue
        if frame_index not in expected_indices and not (start <= timestamp <= end):
            continue
        for person in observation.get("people") or []:
            if not isinstance(person, dict):
                continue
            for field in values:
                value = _text(person.get(field))
                if _is_positive_observation(value) and value not in values[field]:
                    values[field].append(value)
    return values


def normalize_frame_observation(raw: dict, *, frame_index: int,
                                timestamp_sec: float,
                                path: str | Path) -> dict:
    people = _normalize_people(raw.get("people"))
    try:
        raw_people_count = raw.get("people_count")
        if raw_people_count is None:
            raw_people_count = (
                len(people) if isinstance(raw.get("people"), list)
                else raw.get("people", 0)
            )
        people_count = max(0, int(raw_people_count))
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
        "people": people,
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
        characters = segment.get("characters") or []
        if not isinstance(characters, list):
            characters = [characters]
        frame_indices = segment.get("evidence_frame_indices") or []
        if not isinstance(frame_indices, list):
            frame_indices = [frame_indices]
        visible_character_ids = segment.get("visible_character_ids") or []
        if not isinstance(visible_character_ids, list):
            visible_character_ids = [visible_character_ids]
        speaker = _text(segment.get("speaker"))
        speaker_track_id = _text(segment.get("speaker_track_id"))
        voice_cluster_id = _text(
            segment.get("voice_cluster_id") or segment.get("speaker_cluster_id")
        )
        speaker_confidence = _confidence(segment.get("speaker_confidence"))
        speaker_resolved = bool(speaker or speaker_track_id)
        review_required = bool(segment.get("speaker_review_required")) or (
            not speaker_resolved
            or (
                speaker_confidence is not None
                and speaker_confidence < SPEAKER_REVIEW_CONFIDENCE
            )
        )
        observed = _observed_performance_values(
            observations,
            start=start,
            end=end,
            frame_indices=frame_indices,
        )
        gesture = _text(segment.get("gesture"))
        if observed["gesture"] and not _is_positive_observation(gesture):
            gesture = "；".join(observed["gesture"])
        body_action = _text(segment.get("body_action"))
        for value in observed["body_action"]:
            if value not in body_action:
                body_action = "；".join(filter(None, [body_action, value]))
        segments.append({
            "role": role,
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "scene": _text(segment.get("scene")),
            "characters": [
                str(item).strip() for item in characters if str(item).strip()
            ],
            "visible_character_ids": [
                _text(item) for item in visible_character_ids if _text(item)
            ],
            "speaker": speaker,
            "speaker_track_id": speaker_track_id,
            "voice_cluster_id": voice_cluster_id,
            "speaker_confidence": speaker_confidence,
            "speaker_review_required": review_required,
            "speaker_resolution_status": (
                _text(segment.get("speaker_resolution_status"))
                or ("resolved" if speaker_resolved else "unresolved")
            ),
            "action": _text(segment.get("action")),
            "camera": _text(segment.get("camera")),
            "dialogue": _text(segment.get("dialogue")),
            "emotion": _text(segment.get("emotion")),
            "tone": _text(segment.get("tone")),
            "pace": _text(segment.get("pace")),
            "volume": _text(segment.get("volume")),
            "pause": _text(segment.get("pause")),
            "facial_expression": _text(segment.get("facial_expression")),
            "gaze": _text(segment.get("gaze")),
            "gesture": gesture,
            "body_action": body_action,
            "on_screen_text": _text(segment.get("on_screen_text")),
            "audio": _text(segment.get("audio")),
            "evidence": _text(segment.get("evidence")),
            "evidence_frame_indices": [
                int(item) for item in frame_indices
                if str(item).strip().isdigit()
            ],
        })
    segments.sort(key=lambda item: (item["start_sec"], item["end_sec"]))
    return {
        "performance_ir_version": 2,
        "summary": _text(raw.get("summary")),
        "character_tracks": _normalize_character_tracks(
            raw.get("character_tracks")
        ),
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
        '"people_count":0,"people":[{"track_id":"仅限本帧内稳定编号",'
        '"appearance":"服装、发型和显著特征","position":"画面位置",'
        '"mouth_activity":"speaking/silent/unclear",'
        '"facial_expression":"可见表情","gaze":"视线方向",'
        '"gesture":"手势","body_action":"身体动作"}],'
        '"objects":["物件"],"confidence":0.0}'
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
        "你只基于以下 ASR 与抽帧观察还原短视频的完整可复用剧本，不得补充不存在的事实。"
        "role 只能使用 hook/setup/pain/conflict/turn/proof/catchphrase/cta/other。"
        "segments 必须覆盖整条视频时间线；每段都尽量还原场景、出镜人物、人物动作、"
        "运镜、原声台词、屏幕文字与环境声音。看不清或听不清的字段留空，不要猜测。"
        "同一分段要按时间顺序综合全部证据帧判断表演；任一证据帧出现明确手势时，"
        "gesture 必须描述手势动作或变化过程，不得概括为‘无明显手势’。"
        "先跨帧归并同一人物，输出 character_tracks；再把 ASR voice cluster 与可见人物轨迹"
        "对齐。speaker 表示该段台词的实际说话人；只有画面、口型或上下文证据足够时填写，"
        "无法判断时留空，不能把介绍他人的人误写成被介绍者。"
        "speech_mode 必须区分 on_screen（说话人入镜且口型可见）、off_screen（画外角色台词）"
        "与 narration（旁白）；画外音和旁白不能加入出镜人物 characters。"
        "同一人物在所有分段中必须使用完全相同的 speaker，不能因服装描述详略不同"
        "而拆成多个人物；优先使用简短稳定称谓，例如‘女销售’、‘海外客户’。"
        "无法确认的声明放入 claims_to_verify。只输出 JSON："
        '{"summary":"一句话结构摘要","character_tracks":[{"id":"person-a",'
        '"label":"稳定人物称谓","appearance":"跨帧稳定外观",'
        '"voice_cluster_ids":["voice-a"],"confidence":0.0}],'
        '"segments":[{"role":"hook",'
        '"start_sec":0,"end_sec":1,"scene":"地点与环境",'
        '"characters":["人物"],"speaker":"实际说话人或空字符串",'
        '"speech_mode":"on_screen/off_screen/narration",'
        '"visible_character_ids":["person-a"],"speaker_track_id":"person-a或空",'
        '"voice_cluster_id":"voice-a或空","speaker_confidence":0.0,'
        '"speaker_review_required":true,"action":"人物动作",'
        '"camera":"景别、机位与运动", "dialogue":"该段原声台词",'
        '"emotion":"情绪","tone":"语气","pace":"语速","volume":"音量",'
        '"pause":"停顿","facial_expression":"表情","gaze":"视线",'
        '"gesture":"手势","body_action":"身体动作",'
        '"on_screen_text":"画面文字", "audio":"环境声或音乐",'
        '"evidence":"原始证据", "evidence_frame_indices":[1]}],'
        '"catchphrases":["原句"],"claims_to_verify":["待核验声明"]}\n\n'
        + json.dumps(evidence, ensure_ascii=False)
    )


def analyze_source(db, source_id: str, *,
                   max_frames: int = DEFAULT_MAX_ANALYSIS_FRAMES,
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
    for frame in _select_analysis_frames(
            metadata.get("frames") or [],
            utterances=metadata.get("utterances") or [],
            max_frames=max_frames):
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
        _structure_prompt(str(row["content"]), metadata, observations),
        max_tokens=STRUCTURE_MAX_TOKENS,
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
