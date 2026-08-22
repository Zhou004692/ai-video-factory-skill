"""Explicit master dependency graph for safe bounded concurrency."""
from __future__ import annotations

from collections.abc import Iterable, Mapping


GRAPH_VERSION = 1


def _normalized_key(prefix: str, value: object) -> str:
    token = str(value or "").strip().casefold()
    return f"{prefix}:{token}" if token else ""


def _continuity_keys(master: Mapping[str, object]) -> list[str]:
    contract = master.get("continuity_contract")
    contract = contract if isinstance(contract, Mapping) else {}
    scene = contract.get("scene")
    scene = scene if isinstance(scene, Mapping) else {}
    keys = {
        _normalized_key(
            "scene", master.get("scene_anchor_key") or scene.get("key")
        )
    }
    characters = contract.get("characters")
    has_contract_identity = False
    if isinstance(characters, list):
        for character in characters:
            if isinstance(character, Mapping):
                identity_key = _normalized_key(
                    "identity", character.get("identity_key")
                )
                if identity_key:
                    has_contract_identity = True
                    keys.add(identity_key)
    if not has_contract_identity:
        keys.add(_normalized_key("identity", master.get("speaker")))
    keys.add(_normalized_key("identity", master.get("anchor_character")))
    cast = master.get("cast")
    if isinstance(cast, list):
        for character in cast:
            if isinstance(character, Mapping):
                identity = (
                    character.get("identity_key")
                    or character.get("id")
                    or character.get("name")
                )
            else:
                identity = character
            keys.add(_normalized_key("identity", identity))
    return sorted(key for key in keys if key)


def _explicit_dependencies(master: Mapping[str, object]) -> list[int]:
    raw = master.get("depends_on") or []
    values = raw if isinstance(raw, (list, tuple, set)) else [raw]
    dependencies: list[int] = []
    for value in values:
        try:
            idx = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("master depends_on must contain integer indexes") from exc
        if idx > 0 and idx not in dependencies:
            dependencies.append(idx)
    return sorted(dependencies)


def build_master_dependency_graph(masters: Iterable[Mapping[str, object]]) -> dict:
    """Link each continuity key to its nearest prior master.

    The graph is deliberately small: transitive dependencies are represented
    by the nearest predecessor, which preserves continuity without serializing
    unrelated scenes or characters.
    """
    ordered = sorted((dict(master) for master in masters), key=lambda item: int(item.get("idx") or 0))
    indexes = [int(master.get("idx") or 0) for master in ordered]
    if any(idx <= 0 for idx in indexes) or len(indexes) != len(set(indexes)):
        raise ValueError("master indexes must be unique positive integers")
    known = set(indexes)
    last_by_key: dict[str, int] = {}
    nodes: list[dict] = []
    for master, idx in zip(ordered, indexes):
        reasons: dict[int, set[str]] = {}
        for dependency in _explicit_dependencies(master):
            if dependency not in known or dependency >= idx:
                raise ValueError("master depends_on must reference an earlier master")
            reasons.setdefault(dependency, set()).add("explicit")
        keys = _continuity_keys(master)
        for key in keys:
            previous = last_by_key.get(key)
            if previous is not None:
                reasons.setdefault(previous, set()).add(key)
            last_by_key[key] = idx
        nodes.append({
            "idx": idx,
            "continuity_keys": keys,
            "depends_on": sorted(reasons),
            "dependency_reasons": {
                str(dependency): sorted(values)
                for dependency, values in sorted(reasons.items())
            },
        })
    graph = {"version": GRAPH_VERSION, "nodes": nodes}
    graph["waves"] = _all_waves(graph)
    return graph


def ready_master_wave(
    graph: Mapping[str, object],
    *,
    pending_indexes: set[int],
    blocked_indexes: set[int] | None = None,
) -> tuple[list[int], dict[int, list[int]]]:
    """Return the currently executable indexes and review-blocked indexes."""
    blocked_indexes = blocked_indexes or set()
    ready: list[int] = []
    blocked: dict[int, list[int]] = {}
    for node in graph.get("nodes") or []:
        idx = int(node["idx"])
        if idx not in pending_indexes:
            continue
        dependencies = [int(value) for value in node.get("depends_on") or []]
        review_dependencies = sorted(set(dependencies) & blocked_indexes)
        if review_dependencies:
            blocked[idx] = review_dependencies
            continue
        if not (set(dependencies) & pending_indexes):
            ready.append(idx)
    return sorted(ready), blocked


def _all_waves(graph: Mapping[str, object]) -> list[list[int]]:
    remaining = {int(node["idx"]) for node in graph.get("nodes") or []}
    waves: list[list[int]] = []
    while remaining:
        ready, _ = ready_master_wave(graph, pending_indexes=remaining)
        if not ready:
            raise ValueError("master dependency graph contains a cycle")
        waves.append(ready)
        remaining.difference_update(ready)
    return waves
