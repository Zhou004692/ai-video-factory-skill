"""Deterministic reference selection for Seedance 2.5 requests.

The provider accepts many inputs, but every additional material competes for
attention.  This module makes product priorities explicit and keeps the prompt
numbering aligned with the actual request order.
"""
from __future__ import annotations

from collections import Counter
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
    duration_sec: float = 0,
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
        "duration_sec": max(0.0, float(duration_sec or 0)),
        "required": bool(required),
        "priority": PRIORITY[duty],
    }


def select_manifest(
    candidates: Iterable[dict],
    *,
    max_video_references: int,
    max_total_references: int = 8,
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
    seen: set[str] = set()
    for _index, item in ordered:
        uri = str(item.get("uri") or "").strip()
        if not uri or uri in seen:
            continue
        seen.add(uri)
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
    return selected


def compile_prompt_duties(manifest: Iterable[dict]) -> list[str]:
    lines = []
    for item in manifest:
        label = KIND_LABEL[str(item["kind"])]
        index = int(item["material_index"])
        lines.append(f"{label}{index}{DUTY_TEXT[str(item['duty'])]}。")
    return lines
