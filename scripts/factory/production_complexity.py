"""Deterministic, explainable production complexity classification."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Mapping

from .creative_ir import CreativeIR, lip_sync_required, visible_cast


FAST_ACTION_MARKERS = (
    "奔跑", "追逐", "搏斗", "爆炸", "跳跃", "快步", "穿过", "围拢",
    "扣紧", "升起", "冲向", "快速", "旋转", "坠落", "回望",
)


@dataclass(frozen=True)
class ComplexityReport:
    score: int
    mode: str
    content_blocked: bool
    dimensions: Mapping[str, int | float]
    weighted_dimensions: Mapping[str, int]
    risk_codes: tuple[str, ...]
    recommendations: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "score": self.score,
            "mode": self.mode,
            "content_blocked": self.content_blocked,
            "dimensions": dict(self.dimensions),
            "weighted_dimensions": dict(self.weighted_dimensions),
            "risk_codes": self.risk_codes,
            "recommendations": self.recommendations,
        }


def classify_mode(score: int) -> str:
    if score <= 35:
        return "stable"
    if score <= 70:
        return "enhanced"
    return "experimental"


def _scene_key(beat, index: int) -> str:
    explicit = str(
        beat.extensions.get("scene_id")
        or beat.extensions.get("scene_anchor_key")
        or ""
    ).strip().casefold()
    return explicit or str(beat.scene or f"scene-{index}").strip().casefold()


def _dialogue_density(creative: CreativeIR) -> float:
    dialogue_chars = sum(len(beat.dialogue) for beat in creative.beats)
    duration = creative.target_duration_sec
    if not duration:
        duration = sum(
            float(beat.extensions.get("duration_sec") or 0)
            for beat in creative.beats
        )
    duration = duration or max(1, len(creative.beats) * 4)
    return round(dialogue_chars / float(duration), 3)


def analyze(creative: CreativeIR) -> ComplexityReport:
    casts = [visible_cast(beat) for beat in creative.beats]
    appearances = Counter(character for cast in casts for character in set(cast))
    scene_keys = {
        _scene_key(beat, index)
        for index, beat in enumerate(creative.beats, 1)
    }
    character_count = len(creative.characters)
    max_on_screen = max((len(cast) for cast in casts), default=0)
    scene_count = len(scene_keys)
    cross_master_characters = sum(
        1 for count in appearances.values() if count > 1
    )
    dialogue_beats = sum(
        1 for beat in creative.beats if lip_sync_required(beat)
    )
    fast_action_count = sum(
        1 for beat in creative.beats
        if any(marker in beat.action for marker in FAST_ACTION_MARKERS)
    )
    continuity_edges = sum(
        1 for left, right in zip(casts, casts[1:])
        if set(left) & set(right)
    )
    density = _dialogue_density(creative)

    dimensions: dict[str, int | float] = {
        "character_count": character_count,
        "max_on_screen_characters": max_on_screen,
        "scene_count": scene_count,
        "cross_master_character_count": cross_master_characters,
        "dialogue_density": density,
        "fast_action_count": fast_action_count,
        "lip_sync_required_count": dialogue_beats,
        "continuity_edge_count": continuity_edges,
    }
    weighted = {
        "character_count": max(0, character_count - 1) * 8,
        "max_on_screen_characters": max(0, max_on_screen - 1) * 12,
        "scene_count": max(0, scene_count - 1) * 6,
        "cross_master_character_count": cross_master_characters * 5,
        "dialogue_density": 10 if density > 4.5 else 5 if density > 3.5 else 0,
        "fast_action_count": min(12, fast_action_count * 3),
        "lip_sync_required_count": min(12, dialogue_beats * 3),
        "continuity_edge_count": min(12, continuity_edges * 3),
    }
    score = sum(weighted.values())
    risks: list[str] = []
    recommendations: list[str] = []
    if cross_master_characters:
        risks.append("cross_master_character")
        recommendations.append("跨母片人物复用同一身份锚点并按依赖链调度")
    if scene_count > 1:
        risks.append("multiple_scenes")
        recommendations.append("为重复核心空间生成场景锚点，跨空间使用显式转场")
    if max_on_screen > 2:
        risks.append("multi_character_staging")
        recommendations.append("多人同镜保持身份与空间连续性，按剧情设计动态走位，必要时拆为正反打和反应镜头")
    if fast_action_count:
        risks.append("action_complexity")
        recommendations.append("复杂动作拆为连续动作节点，后一节点继承前一节点结果")
    if density > 3.5:
        risks.append("dense_dialogue")
        recommendations.append("按完整语义停顿拆分对白，不压缩或删除原台词")
    if dialogue_beats:
        risks.append("lip_sync_required")
        recommendations.append("为对白节点启用台词完整性、口型和字幕同步质检")
    return ComplexityReport(
        score=score,
        mode=classify_mode(score),
        content_blocked=False,
        dimensions=dimensions,
        weighted_dimensions=weighted,
        risk_codes=tuple(risks),
        recommendations=tuple(dict.fromkeys(recommendations)),
    )
