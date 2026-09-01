"""Runtime evidence accounting for Flow Guard coverage reports."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Iterable, Mapping


BROWSER_EVIDENCE_PREFIX = "FLOW_GUARD_EVIDENCE="

_FIELDS = (
    "graph_actions",
    "ui_dispatch_actions",
    "integrated_backend_actions",
    "worker_node_types",
    "source_types",
    "provider_response_types",
)

_KIND_TO_FIELD = {
    "graph_action": "graph_actions",
    "ui_dispatch": "ui_dispatch_actions",
    "backend_action": "integrated_backend_actions",
    "worker_node": "worker_node_types",
    "source_type": "source_types",
    "provider_response": "provider_response_types",
}


@dataclass
class CoverageEvidence:
    """Keep evidence boundaries separate so one path cannot credit another."""

    graph_actions: set[str] = field(default_factory=set)
    ui_dispatch_actions: set[str] = field(default_factory=set)
    integrated_backend_actions: set[str] = field(default_factory=set)
    worker_node_types: set[str] = field(default_factory=set)
    source_types: set[str] = field(default_factory=set)
    provider_response_types: set[str] = field(default_factory=set)

    def record(self, kind: str, value: object) -> None:
        field_name = _KIND_TO_FIELD.get(str(kind))
        normalized = str(value or "").strip()
        if not field_name:
            raise ValueError(f"unknown coverage evidence kind: {kind}")
        if normalized:
            getattr(self, field_name).add(normalized)

    def merge(self, payload: Mapping[str, object] | None) -> None:
        for field_name in _FIELDS:
            raw = (payload or {}).get(field_name, [])
            if isinstance(raw, str):
                values: Iterable[object] = (raw,)
            elif isinstance(raw, Iterable):
                values = raw
            else:
                continue
            target = getattr(self, field_name)
            target.update(
                normalized for value in values
                if (normalized := str(value or "").strip())
            )

    def as_dict(self) -> dict[str, list[str]]:
        return {
            field_name: sorted(getattr(self, field_name))
            for field_name in _FIELDS
        }


def parse_browser_evidence(
    stdout: str,
) -> tuple[dict[str, list[str]], list[str]]:
    """Parse an explicit browser-runtime sentinel, failing closed if absent."""
    candidates = [
        line[len(BROWSER_EVIDENCE_PREFIX):].strip()
        for line in str(stdout or "").splitlines()
        if line.startswith(BROWSER_EVIDENCE_PREFIX)
    ]
    empty = CoverageEvidence().as_dict()
    if not candidates:
        return empty, ["browser_evidence_missing"]
    try:
        raw = json.loads(candidates[-1])
        if not isinstance(raw, dict):
            raise TypeError("browser evidence must be an object")
        evidence = CoverageEvidence()
        evidence.merge(raw)
        return evidence.as_dict(), []
    except (json.JSONDecodeError, TypeError, ValueError):
        return empty, ["browser_evidence_invalid"]
