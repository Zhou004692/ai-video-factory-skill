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

FAULT_NODE_TYPES = (
    "character_anchor",
    "scene_anchor",
    "master",
    "bgm",
    "local_compose",
    "final_review",
)
RESOLUTION_BY_PAUSE = {
    "none": "auto",
    "anchor_review": "approve",
    "master_review": "approve",
    "budget": "resume",
    "provider_action": "reconcile",
    "recoverable_failure": "repair",
}
PAUSE_NODE_TYPES = {
    "none": ("master",),
    "anchor_review": ("character_anchor", "scene_anchor"),
    "master_review": ("master",),
    "budget": FAULT_NODE_TYPES,
    "provider_action": FAULT_NODE_TYPES,
    "recoverable_failure": FAULT_NODE_TYPES,
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


def build_smoke_scenarios(
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


def build_incremental_scenarios() -> tuple[FlowScenario, ...]:
    """Keep one fast path for every effective branch value and node type."""
    values = (
        ("stable", "none", "master", False),
        ("enhanced", "anchor_review", "character_anchor", True),
        ("experimental", "anchor_review", "scene_anchor", False),
        ("stable", "master_review", "master", True),
        ("enhanced", "budget", "bgm", False),
        ("experimental", "provider_action", "local_compose", True),
        ("stable", "recoverable_failure", "final_review", False),
    )
    return tuple(
        FlowScenario(
            scenario_id=f"inc-{index:02d}",
            profile=profile,
            pause=pause,
            pause_node=node_type,
            resolution=RESOLUTION_BY_PAUSE[pause],
            restart=restart,
        )
        for index, (profile, pause, node_type, restart)
        in enumerate(values, 1)
    )


def build_scenarios() -> tuple[FlowScenario, ...]:
    """Build constraint-valid branch paths with one restart per path."""
    profiles = tuple(str(item) for item in BRANCH_DIMENSIONS["profile"])
    paths = tuple(
        (pause, node_type)
        for pause, node_types in PAUSE_NODE_TYPES.items()
        for node_type in node_types
    )
    values: list[tuple[str, str, str, bool]] = []
    for profile in profiles:
        values.extend(
            (profile, pause, node_type, False)
            for pause, node_type in paths
        )
    values.extend(
        (profiles[index % len(profiles)], pause, node_type, True)
        for index, (pause, node_type) in enumerate(paths)
    )
    return tuple(
        FlowScenario(
            scenario_id=f"r4-{index:03d}",
            profile=profile,
            pause=pause,
            pause_node=node_type,
            resolution=RESOLUTION_BY_PAUSE[pause],
            restart=restart,
        )
        for index, (profile, pause, node_type, restart)
        in enumerate(values, 1)
    )


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
