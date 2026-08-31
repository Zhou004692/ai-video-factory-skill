"""Explicit allowlist of bounded, local and zero-cost repair recipes."""
from __future__ import annotations

import sqlite3

from .. import production_executor, production_invariants
from .model import RepairResult


_ALLOWED_RECIPE_IDS = (
    "repair_safe_projections",
    "recover_expired_leases",
    "retry_repaired_contract_nodes",
)


def allowed_recipe_ids() -> tuple[str, ...]:
    return _ALLOWED_RECIPE_IDS


def apply_repair(
    database: sqlite3.Connection,
    job_id: str,
    recipe_id: str,
) -> RepairResult:
    """Apply one allowlisted repair and reject every expansive mutation."""
    if recipe_id not in _ALLOWED_RECIPE_IDS:
        raise ValueError(f"flow guard repair is not allowlisted: {recipe_id}")

    if recipe_id == "repair_safe_projections":
        report = production_invariants.repair_safe_projections(database, job_id)
        repaired = list(report.repaired_codes)
        blockers = [issue.code for issue in report.blockers]
        return RepairResult(
            recipe_id=recipe_id,
            applied=bool(repaired),
            detail=(
                f"repaired {', '.join(repaired)}" if repaired
                else "no safe projection repair was needed"
            ),
            after={"repaired_codes": repaired, "blockers": blockers},
        )

    if recipe_id == "recover_expired_leases":
        recovered = production_executor.recover_expired_nodes(
            database, job_id
        )
        return RepairResult(
            recipe_id=recipe_id,
            applied=bool(recovered),
            detail=f"recovered {len(recovered)} expired execution leases",
            after={"recovered_nodes": dict(recovered)},
        )

    retried = production_executor.retry_repaired_contract_nodes(
        database, job_id
    )
    return RepairResult(
        recipe_id=recipe_id,
        applied=bool(retried),
        detail=f"requeued {len(retried)} repaired contract nodes",
        after={"retried_nodes": list(retried)},
    )
