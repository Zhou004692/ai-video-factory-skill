"""Pure flow-level invariants evaluated after every exploration run."""
from __future__ import annotations

from .model import ExplorationReport, Finding


TERMINAL_STATES = frozenset({"READY_TO_PUBLISH", "PUBLISHED"})


def evaluate_report(
    report: ExplorationReport,
    *,
    max_steps: int = 32,
) -> tuple[Finding, ...]:
    """Return deterministic blockers without mutating production evidence."""
    findings = list(report.findings)
    existing = {item.code for item in findings}

    def add(code: str, detail: str) -> None:
        if code not in existing:
            findings.append(Finding(
                code=code,
                severity="error",
                detail=detail,
                state=report.final_state,
                next_action=(
                    report.observed_actions[-1]
                    if report.observed_actions else ""
                ),
            ))
            existing.add(code)

    if report.final_state not in TERMINAL_STATES:
        add(
            "nonterminal_dead_end",
            "scenario stopped before reaching a publishable terminal state",
        )
    if report.paid_provider_calls:
        add(
            "paid_provider_call",
            f"offline explorer observed {report.paid_provider_calls} paid calls",
        )
    pairs = tuple(zip(report.observed_states, report.observed_actions))
    if len(pairs) != len(set(pairs)):
        add(
            "state_action_loop",
            "the same state and next action recurred without progress",
        )
    if report.steps >= max_steps and report.final_state not in TERMINAL_STATES:
        add(
            "step_limit_exceeded",
            f"scenario exhausted the {max_steps}-step safety boundary",
        )
    return tuple(findings)
