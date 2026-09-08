"""Job-level worker loop for the persisted Production Graph v2."""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Mapping

from . import production_executor, production_invariants, production_projection
from .testing import fault_injection


NodeOutcome = production_executor.NodeOutcome
NodeHandler = production_executor.NodeHandler


@dataclass(frozen=True)
class GraphRunResult:
    job_id: str
    job_state: str
    next_action: str
    waves: int
    started_nodes: tuple[str, ...]
    recovered_nodes: Mapping[str, str]


async def run_graph_job(
    database: sqlite3.Connection,
    job_id: str,
    *,
    configured_limit: int,
    handlers: Mapping[str, NodeHandler],
) -> GraphRunResult:
    """Drain currently runnable nodes and project one durable business state."""
    job = database.execute(
        "SELECT execution_engine,production_spec_version,production_spec_hash "
        "FROM jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if job is None:
        raise ValueError(f"video job not found: {job_id}")
    if str(job["execution_engine"] or "legacy") != "graph_v2":
        raise ValueError(f"video job is not assigned to graph_v2: {job_id}")
    if int(job["production_spec_version"] or 0) != 2:
        raise ValueError(f"video job has no frozen Production Spec v2: {job_id}")
    if not str(job["production_spec_hash"] or "").strip():
        raise ValueError(f"video job has no frozen spec hash: {job_id}")

    # The frozen spec and persisted graph are the only paid-execution
    # authority.  Repair disposable projections first, then stop before any
    # provider call when a non-repairable divergence remains.
    invariant_report = production_invariants.repair_safe_projections(
        database, job_id,
    )
    if invariant_report.blockers:
        raise production_invariants.ProductionInvariantError(invariant_report)

    recovered = production_executor.recover_expired_nodes(database, job_id)
    with database:
        production_executor.release_resolved_provider_gates(database, job_id)
    for node_id in production_executor.retry_repaired_contract_nodes(
        database, job_id,
    ):
        recovered[node_id] = "READY"
    if recovered:
        fault_injection.hit(
            database,
            "after_service_restart",
            job_id=job_id,
        )
    node_count = int(database.execute(
        "SELECT COUNT(*) FROM production_nodes WHERE job_id=?", (job_id,)
    ).fetchone()[0] or 0)
    if not node_count:
        raise ValueError(f"production graph not found: {job_id}")

    started: list[str] = []
    waves = 0
    while waves <= node_count:
        ready = production_executor.ready_nodes(
            database, job_id, configured_limit=configured_limit,
        )
        if not ready:
            break
        wave = await production_executor.run_ready_wave(
            database,
            job_id,
            configured_limit=configured_limit,
            handlers=handlers,
        )
        if not wave.started:
            break
        waves += 1
        started.extend(wave.started)
        fault_injection.hit(
            database,
            "after_node_update_before_job_projection",
            job_id=job_id,
        )

    report = production_projection.persist(database, job_id)
    return GraphRunResult(
        job_id=job_id,
        job_state=report.projected_job_state,
        next_action=report.next_action,
        waves=waves,
        started_nodes=tuple(started),
        recovered_nodes=dict(recovered),
    )
