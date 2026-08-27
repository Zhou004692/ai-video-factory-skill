"""Orthogonal creative dimensions for compiler release coverage."""
from __future__ import annotations

from itertools import combinations, product
from typing import Mapping, Sequence


DIMENSIONS = {
    "cast": ("single", "double", "triple"),
    "scene": ("single", "continuous", "multi_scene"),
    "narrative": ("dialogue", "voiceover", "mixed"),
    "identity": ("fixed", "ai", "mixed"),
    "duration": (8, 18, 30, 60),
    "dependency": ("independent", "serial_continuity"),
}


def _pairs(scenario: Mapping[str, object]) -> set[tuple]:
    keys = tuple(scenario)
    return {
        (left, scenario[left], right, scenario[right])
        for left, right in combinations(keys, 2)
    }


def build_pairwise_scenarios(
    dimensions: Mapping[str, Sequence[object]] = DIMENSIONS,
) -> tuple[dict[str, object], ...]:
    """Deterministic greedy set cover over the small Cartesian domain."""
    keys = tuple(dimensions)
    candidates = [
        dict(zip(keys, values))
        for values in product(*(dimensions[key] for key in keys))
    ]
    uncovered = {
        (left, left_value, right, right_value)
        for left, right in combinations(keys, 2)
        for left_value in dimensions[left]
        for right_value in dimensions[right]
    }
    selected: list[dict[str, object]] = []
    while uncovered:
        best = max(
            candidates,
            key=lambda candidate: (
                len(_pairs(candidate) & uncovered),
                tuple(str(candidate[key]) for key in keys),
            ),
        )
        covered = _pairs(best) & uncovered
        if not covered:
            raise AssertionError("pairwise scenario generator made no progress")
        selected.append(best)
        uncovered -= covered
        candidates.remove(best)
    return tuple(selected)


def all_pairs_covered(
    scenarios: Sequence[Mapping[str, object]],
    dimensions: Mapping[str, Sequence[object]] = DIMENSIONS,
) -> bool:
    keys = tuple(dimensions)
    required = {
        (left, left_value, right, right_value)
        for left, right in combinations(keys, 2)
        for left_value in dimensions[left]
        for right_value in dimensions[right]
    }
    actual = set().union(*(_pairs(scenario) for scenario in scenarios))
    return required <= actual


def scenario_storyboard(scenario: Mapping[str, object], index: int) -> dict:
    cast_count = {"single": 1, "double": 2, "triple": 3}[scenario["cast"]]
    characters = [{
        "id": f"role-{number}",
        "name": f"角色{number}",
        "identity_type": (
            "real_person" if scenario["identity"] == "fixed"
            else "real_person" if scenario["identity"] == "mixed" and number == 1
            else "ai_generated"
        ),
    } for number in range(1, cast_count + 1)]
    duration = int(scenario["duration"])
    beat_count = 2 if duration > 30 or scenario["scene"] == "multi_scene" else 1
    beats = []
    for beat_index in range(beat_count):
        if scenario["scene"] == "multi_scene":
            scene_id = f"space-{beat_index + 1}"
        else:
            scene_id = "same-space"
        if scenario["dependency"] == "serial_continuity":
            cast = [character["id"] for character in characters]
        else:
            cast = [characters[beat_index % len(characters)]["id"]]
        narrative = scenario["narrative"]
        has_dialogue = narrative == "dialogue" or (
            narrative == "mixed" and beat_index % 2 == 0
        )
        speaker = cast[0] if has_dialogue else ""
        beats.append({
            "id": f"scenario-{index}-beat-{beat_index + 1}",
            "kind": "dialogue" if has_dialogue else "voiceover",
            "scene_id": scene_id,
            "scene": f"设备服务空间 {scene_id}",
            "characters": cast,
            "speaker": speaker,
            "dialogue": "确认设备状态，按计划完成交付" if has_dialogue else "",
            "action": "人物检查设备并沿安全动线移动",
            "duration_sec": round(duration / beat_count, 2),
            "must_preserve": [f"scenario-{index}-requirement-{beat_index + 1}"],
            "may_adapt": ["景别", "剪辑点"],
        })
    return {
        "title": f"正交生产场景 {index}",
        "target_duration_sec": duration,
        "characters": characters,
        "beats": beats,
    }
