"""Deterministic advisory quality risk for long Seedance masters."""
from __future__ import annotations

import re


def _dialogue_blocks(master: dict) -> list[dict]:
    raw = master.get("dialogue_blocks") or master.get("dialogues") or []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _dialogue_text(master: dict, blocks: list[dict]) -> str:
    values = [
        str(item.get("text") or item.get("dialogue") or "")
        for item in blocks
    ]
    if not any(values):
        values = [str(
            master.get("dialogue") or master.get("spoken_text")
            or master.get("voiceover") or ""
        )]
    return " ".join(value.strip() for value in values if value.strip())


def assess_master(master: dict) -> dict:
    """Return an advisory risk level; never reject a valid master."""
    duration = max(0.1, float(
        master.get("duration") or master.get("duration_sec") or 0.1
    ))
    blocks = _dialogue_blocks(master)
    dialogue = _dialogue_text(master, blocks)
    spoken_chars = len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", dialogue))
    dialogue_chars_per_sec = round(spoken_chars / duration, 2)
    roles = {
        str(item.get("speaker") or item.get("role") or "").strip().casefold()
        for item in blocks
        if str(item.get("speaker") or item.get("role") or "").strip()
    }
    fallback_speaker = str(
        master.get("speaker_id") or master.get("speaker") or ""
    ).strip().casefold()
    if not roles and fallback_speaker:
        roles.add(fallback_speaker)
    performance = master.get("performance_beats") or master.get("actions") or []
    performance_count = len(performance) if isinstance(performance, list) else 0
    scene_markers = master.get("scene_change_markers") or []
    scene_change_count = len(scene_markers) if isinstance(scene_markers, list) else 0
    if not scene_change_count:
        scene_text = " ".join(str(master.get(key) or "") for key in (
            "scene", "scene_dynamics", "visual_intent"
        ))
        scene_change_count = len(re.findall(
            r"转场|切到|切换|场景变化|镜头来到|随后来到", scene_text
        ))

    score = 0
    if duration >= 25:
        score += 2
    elif duration > 15:
        score += 1
    if dialogue_chars_per_sec > 6:
        score += 2
    elif dialogue_chars_per_sec > 3.5:
        score += 1
    role_count = len(roles)
    if role_count >= 3:
        score += 2
    elif role_count == 2:
        score += 1
    if performance_count >= 5:
        score += 2
    elif performance_count >= 3:
        score += 1
    if scene_change_count >= 2:
        score += 2
    elif scene_change_count == 1:
        score += 1

    level = "high" if score >= 5 else "medium" if score >= 2 else "low"
    return {
        "level": level,
        "score": score,
        "factors": {
            "duration_sec": duration,
            "dialogue_chars_per_sec": dialogue_chars_per_sec,
            "speaking_role_count": role_count,
            "performance_beat_count": performance_count,
            "scene_change_marker_count": scene_change_count,
        },
        "recommendation": {
            "low": "结构简单，可按当前母片直接生成",
            "medium": "建议重点验收台词完整性、动作节奏和首尾连续性",
            "high": "长时长且表演复杂；建议拆分叙事边界，或确认接受较高返工风险后继续",
        }[level],
    }
