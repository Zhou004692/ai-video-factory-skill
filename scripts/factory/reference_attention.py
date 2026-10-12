"""Deterministic reference selection for Seedance 2.5 requests.

The provider accepts many inputs, but every additional material competes for
attention.  This module makes product priorities explicit and keeps the prompt
numbering aligned with the actual request order.
"""
from __future__ import annotations

from collections import Counter
import math
from typing import Iterable


PRIORITY = {
    "scene_continuity": 10,
    "site_continuity": 15,
    "speaker_identity": 20,
    "supporting_identity": 30,
    "voice_continuity": 40,
    "style_continuity": 50,
}

DUTY_TEXT = {
    "scene_continuity": "只负责同场景空间连续性、站位、视线方向和镜头轴线，不负责人物身份",
    "site_continuity": "只负责同一项目现场的建筑语言、材质、设备体系和安全标识体系，不锁定具体子空间布局、站位、天气、时段或机位",
    "speaker_identity": "只负责主说话人身份、五官、视觉年龄、发型和体型，不负责照搬背景与服装",
    "supporting_identity": "只负责同场配角身份、五官、视觉年龄、发型和体型，不负责新增台词",
    "voice_continuity": "只负责声线、语速和情绪连续性，不得复述参考音频中的旧台词",
    "style_continuity": "只负责色彩、光线或摄影风格，不负责人物身份和剧情内容",
}

KIND_LABEL = {"video": "视频", "audio": "音频", "image": "图片"}


def candidate(
    uri: str,
    *,
    kind: str,
    duty: str,
    identity_key: str = "",
    duration_sec: float | None = None,
    required: bool = True,
) -> dict:
    uri = str(uri or "").strip()
    kind = str(kind or "").strip().casefold()
    duty = str(duty or "").strip()
    if not uri:
        raise ValueError("参考素材 URI 不能为空")
    if kind not in KIND_LABEL:
        raise ValueError(f"不支持的参考素材类型：{kind or 'empty'}")
    if duty not in PRIORITY:
        raise ValueError(f"不支持的参考素材职责：{duty or 'empty'}")
    return {
        "uri": uri,
        "kind": kind,
        "duty": duty,
        "identity_key": str(identity_key or ""),
        "duration_sec": known_duration(duration_sec),
        "required": bool(required),
        "priority": PRIORITY[duty],
    }


def known_duration(value: object) -> float | None:
    """Zero, malformed and nonfinite values are unavailable, never evidence."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def validate_durations(
    manifest: Iterable[dict], *, max_video_duration_sec: float | None = 30,
    max_audio_duration_sec: float | None = 30, require_known: bool = False,
) -> None:
    items = list(manifest)
    for kind, maximum in (("video", max_video_duration_sec),
                          ("audio", max_audio_duration_sec)):
        materials = [item for item in items if item.get("kind") == kind]
        total = sum(known_duration(item.get("duration_sec")) or 0 for item in materials)
        if maximum is not None and total > maximum:
            raise ValueError(
                f"reference {kind} aggregate duration {total:g}s exceeds max {maximum:g}s; "
                "split the scene before paid generation"
            )
        if require_known:
            for index, item in enumerate(materials, 1):
                if known_duration(item.get("duration_sec")) is None:
                    raise ValueError(
                        f"reference duration unavailable for {kind} material {index}; "
                        "provide verified duration metadata or a readable controlled local file "
                        "before paid generation"
                    )


def sanitized_manifest(manifest: Iterable[dict]) -> list[dict]:
    """Keep only bounded product labels and durations in provider audit."""
    return [{
        "kind": item.get("kind") if item.get("kind") in KIND_LABEL else "",
        "duty": item.get("duty") if item.get("duty") in PRIORITY else "",
        "material_index": int(item.get("material_index") or 0),
        "duration_sec": known_duration(item.get("duration_sec")),
    } for item in manifest if isinstance(item, dict)]


def select_manifest(
    candidates: Iterable[dict],
    *,
    max_video_references: int,
    max_total_references: int = 8,
    max_video_duration_sec: float = 30,
    max_audio_duration_sec: float = 30,
) -> list[dict]:
    """Return an immutable request manifest ordered by product attention.

    Required materials are never silently discarded. Optional low-priority
    materials may be omitted once the bounded attention budget is exhausted.
    """
    if max_video_references <= 0 or max_total_references <= 0:
        raise ValueError("参考素材注意力预算必须为正整数")
    ordered = sorted(
        enumerate(dict(item) for item in candidates),
        key=lambda pair: (int(pair[1].get("priority") or 999), pair[0]),
    )
    deduped: list[dict] = []
    seen: dict[tuple[str, str], dict] = {}
    for _index, item in ordered:
        uri = str(item.get("uri") or "").strip()
        if not uri:
            continue
        material_key = (str(item.get("kind") or ""), uri)
        if material_key in seen:
            previous = seen[material_key]
            previous["required"] = previous.get("required", True) or item.get("required", True)
            values = [known_duration(x.get("duration_sec")) for x in (previous, item)]
            previous["duration_sec"] = max((x for x in values if x is not None), default=None)
            continue
        seen[material_key] = item
        deduped.append(item)
    required_videos = sum(
        item.get("kind") == "video" and item.get("required", True)
        for item in deduped
    )
    required_total = sum(item.get("required", True) for item in deduped)
    if required_videos > max_video_references or required_total > max_total_references:
        raise ValueError(
            "必需参考素材超过产品参考素材注意力预算；请拆分场景、减少同场人物，"
            "或先生成独立连续性母片后再提交付费生成"
        )
    selected: list[dict] = []
    video_count = 0
    for item in deduped:
        is_video = item.get("kind") == "video"
        within_budget = (
            len(selected) < max_total_references
            and (not is_video or video_count < max_video_references)
        )
        if not within_budget:
            if item.get("required", True):
                raise ValueError(
                    "必需参考素材超过产品参考素材注意力预算；请拆分场景后重试"
                )
            continue
        selected.append(dict(item))
        if is_video:
            video_count += 1
    counters: Counter[str] = Counter()
    for item in selected:
        counters[item["kind"]] += 1
        item["material_index"] = counters[item["kind"]]
    validate_durations(
        selected, max_video_duration_sec=max_video_duration_sec,
        max_audio_duration_sec=max_audio_duration_sec,
    )
    return selected


def compile_prompt_duties(manifest: Iterable[dict]) -> list[str]:
    lines = []
    for item in manifest:
        label = KIND_LABEL[str(item["kind"])]
        index = int(item["material_index"])
        lines.append(f"{label}{index}{DUTY_TEXT[str(item['duty'])]}。")
    return lines
