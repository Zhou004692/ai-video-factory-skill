"""Scripted zero-cost outcomes at Seedance-owned graph boundaries."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import sqlite3

from .. import full_flow_regression, production_adapters, production_graph_runner
from .model import FlowScenario


class ScriptedSeedance:
    """Return one deterministic pause/failure, then succeed on retry."""

    def __init__(
        self,
        scenario: FlowScenario,
        *,
        database: sqlite3.Connection,
        media_root: Path,
        available_node_types: set[str],
    ):
        self.scenario = scenario
        self.calls: list[tuple[str, str]] = []
        self._attempts: Counter[str] = Counter()
        self._media_root = Path(media_root).resolve()
        self._base_handlers = full_flow_regression.build_zero_cost_handlers(
            database, media_root=self._media_root
        )
        target = scenario.pause_node
        if target not in available_node_types:
            target = next(iter(sorted(available_node_types)))
        self.target_node_type = target

    def rebind_database(self, database: sqlite3.Connection) -> None:
        """Keep the provider script while simulating a service restart."""
        self._base_handlers = full_flow_regression.build_zero_cost_handlers(
            database, media_root=self._media_root
        )

    async def handle(self, row: sqlite3.Row):
        node_id = str(row["node_id"])
        node_type = str(row["node_type"])
        self.calls.append((node_id, node_type))
        self._attempts[node_id] += 1
        first_target_attempt = (
            node_type == self.target_node_type
            and self._attempts[node_id] == 1
        )

        if first_target_attempt:
            if self.scenario.pause == "budget":
                return production_graph_runner.NodeOutcome.require_budget({
                    "mock": True,
                    "next_estimate_cents": 100,
                    "budget_limit_cents": 0,
                })
            if self.scenario.pause == "provider_action":
                return production_graph_runner.NodeOutcome.require_action({
                    "mock": True,
                    "provider_task_id": f"mock-task-{node_id}",
                })
            if self.scenario.pause == "recoverable_failure":
                raise production_adapters.FrozenSpecMismatchError(
                    "flow guard injected unpaid frozen-spec mismatch"
                )

        outcome = await self._base_handlers[node_type](row)
        if first_target_attempt and self.scenario.pause == "master_review":
            output = (
                dict(outcome.output)
                if isinstance(outcome, production_graph_runner.NodeOutcome)
                else dict(outcome or {})
            )
            return production_graph_runner.NodeOutcome.require_review({
                **output,
                "mock": True,
                "kind": "master_review",
            })
        return outcome

    def handlers(self) -> dict[str, object]:
        return {
            node_type: self.handle for node_type in (
                "character_anchor",
                "scene_anchor",
                "master",
                "bgm",
                "local_compose",
                "final_review",
            )
        }
