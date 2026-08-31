"""Bounded combinatorial coverage for workflow and failure branches."""
from __future__ import annotations

from itertools import combinations, product
from typing import Mapping, Sequence

from .model import FlowScenario


BRANCH_DIMENSIONS: dict[str, tuple[object, ...]] = {
    "profile": ("stable", "enhanced", "experimental"),
    "pause": (
        "none",
        "anchor_review",
        "master_review",
        "budget",
        "provider_action",
        "recoverable_failure",
    ),
    "pause_node": (
        "character_anchor",
        "scene_anchor",
        "master",
        "local_compose",
        "final_review",
    ),
    "resolution": ("auto", "approve", "resume", "reconcile", "repair"),
    "restart": (False, True),
}


def _pairs(values: Mapping[str, object]) -> set[tuple[object, ...]]:
    keys = tuple(values)
    return {
        (left, values[left], right, values[right])
        for left, right in combinations(keys, 2)
    }


def _required_pairs(
    dimensions: Mapping[str, Sequence[object]],
) -> set[tuple[object, ...]]:
    keys = tuple(dimensions)
    return {
        (left, left_value, right, right_value)
        for left, right in combinations(keys, 2)
        for left_value in dimensions[left]
        for right_value in dimensions[right]
    }


def _scenario_values(
    dimensions: Mapping[str, Sequence[object]],
) -> tuple[dict[str, object], ...]:
    keys = tuple(dimensions)
    candidates = [
        dict(zip(keys, values))
        for values in product(*(dimensions[key] for key in keys))
    ]
    uncovered = _required_pairs(dimensions)
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
            raise AssertionError("flow scenario generator made no progress")
        selected.append(best)
        uncovered -= covered
        candidates.remove(best)
    return tuple(selected)


def build_scenarios(
    dimensions: Mapping[str, Sequence[object]] = BRANCH_DIMENSIONS,
) -> tuple[FlowScenario, ...]:
    result: list[FlowScenario] = []
    for index, values in enumerate(_scenario_values(dimensions), 1):
        result.append(FlowScenario(
            scenario_id=f"r4-{index:03d}",
            profile=str(values["profile"]),
            pause=str(values["pause"]),
            pause_node=str(values["pause_node"]),
            resolution=str(values["resolution"]),
            restart=bool(values["restart"]),
        ))
    return tuple(result)


def all_pairs_covered(
    scenarios: Sequence[FlowScenario],
    dimensions: Mapping[str, Sequence[object]] = BRANCH_DIMENSIONS,
) -> bool:
    actual = set().union(*(
        _pairs({
            "profile": scenario.profile,
            "pause": scenario.pause,
            "pause_node": scenario.pause_node,
            "resolution": scenario.resolution,
            "restart": scenario.restart,
        })
        for scenario in scenarios
    ))
    return _required_pairs(dimensions) <= actual
