"""Read-only projections for graph-based video production."""
from __future__ import annotations

import sqlite3


class ProductionReadModel:
    """Expose production graph details without mutating orchestration state."""

    def __init__(self, database: sqlite3.Connection):
        self.database = database

    def graph_detail(self, job_id: str) -> dict:
        nodes = [dict(row) for row in self.database.execute(
            "SELECT node_id,node_type,state,paid,estimated_cost_cents,"
            "actual_cost_cents,provider_task_id,error_code,next_action "
            "FROM production_nodes WHERE job_id=? ORDER BY node_type,node_id",
            (job_id,),
        ).fetchall()]
        if not nodes:
            raise ValueError("该任务尚未完成生产预检")
        edges = [dict(row) for row in self.database.execute(
            "SELECT from_node_id,to_node_id,edge_type FROM production_edges "
            "WHERE job_id=? ORDER BY from_node_id,to_node_id",
            (job_id,),
        ).fetchall()]
        return {"job_id": job_id, "nodes": nodes, "edges": edges}
