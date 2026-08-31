"""Immutable value objects shared by flow-guard components."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class FlowScenario:
    scenario_id: str
    profile: str
    pause: str
    pause_node: str
    resolution: str
    restart: bool
    provider_mode: str = "mock"
    max_paid_calls: int = 0

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    detail: str
    state: str = ""
    next_action: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class RepairResult:
    recipe_id: str
    applied: bool
    detail: str
    before: Mapping[str, object] = field(default_factory=dict)
    after: Mapping[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ExplorationReport:
    scenario_id: str
    status: str
    final_state: str
    observed_states: tuple[str, ...] = ()
    observed_actions: tuple[str, ...] = ()
    started_nodes: tuple[str, ...] = ()
    findings: tuple[Finding, ...] = ()
    repairs: tuple[RepairResult, ...] = ()
    provider_calls: int = 0
    paid_provider_calls: int = 0
    steps: int = 0
    semantic_checks: tuple[str, ...] = ()
    media_metrics: Mapping[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "findings": [item.as_dict() for item in self.findings],
            "repairs": [item.as_dict() for item in self.repairs],
        }
