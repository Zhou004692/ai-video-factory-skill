"""Validated, persistent execution graph for Production Spec v2."""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Mapping

from .production_compiler import ProductionSpecV2


@dataclass(frozen=True)
class GraphNode:
    node_id: str
    node_type: str
    paid: bool
    estimated_cost_cents: int
    input: Mapping[str, object]


@dataclass(frozen=True)
class ProductionGraph:
    job_id: str
    spec_hash: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[tuple[str, str, str], ...]

    def node(self, node_id: str) -> GraphNode:
        return next(node for node in self.nodes if node.node_id == node_id)

    def ancestors(self, node_id: str) -> set[str]:
        parents: dict[str, set[str]] = {}
        for source, target, _ in self.edges:
            parents.setdefault(target, set()).add(source)
        result: set[str] = set()
        stack = list(parents.get(node_id, ()))
        while stack:
            current = stack.pop()
            if current in result:
                continue
            result.add(current)
            stack.extend(parents.get(current, ()))
        return result


def compile_graph(spec: ProductionSpecV2) -> ProductionGraph:
    node_ids = [node.node_id for node in spec.nodes]
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("production graph contains duplicate node ids")
    known = set(node_ids)
    edges = tuple(
        (edge.from_node_id, edge.to_node_id, edge.edge_type)
        for edge in spec.edges
    )
    for source, target, _ in edges:
        if source not in known or target not in known:
            raise ValueError(
                f"production graph edge references unknown node: {source}->{target}"
            )
    children: dict[str, list[str]] = {node_id: [] for node_id in known}
    indegree = {node_id: 0 for node_id in known}
    for source, target, _ in edges:
        children[source].append(target)
        indegree[target] += 1
    queue = [node_id for node_id, count in indegree.items() if count == 0]
    visited = 0
    while queue:
        current = queue.pop()
        visited += 1
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(known):
        raise ValueError("production graph contains a dependency cycle")
    graph_nodes = tuple(GraphNode(
        node_id=node.node_id,
        node_type=node.node_type,
        paid=bool(node.paid),
        estimated_cost_cents=int(node.estimated_cost_cents),
        input=dict(node.input),
    ) for node in spec.nodes)
    return ProductionGraph(spec.job_id, spec.spec_hash, graph_nodes, edges)


def assert_graph_replaceable(
    database: sqlite3.Connection, graph: ProductionGraph,
) -> bool:
    """Fail before mutation when a different graph already owns paid work.

    Returns ``False`` when the exact graph is already persisted, otherwise
    ``True``.  Callers use this preflight to keep the job's frozen spec and the
    durable graph in one transaction.
    """
    existing = database.execute(
        "SELECT spec_hash,actual_cost_cents,state FROM production_nodes "
        "WHERE job_id=?",
        (graph.job_id,),
    ).fetchall()
    if existing and all(row["spec_hash"] == graph.spec_hash for row in existing):
        return False
    if existing and _existing_graph_is_equivalent(database, graph):
        return True
    if existing and any(
        int(row["actual_cost_cents"] or 0) > 0
        or row["state"] in {"RUNNING", "SUCCEEDED", "REVIEW_REQUIRED"}
        for row in existing
    ):
        raise ValueError("paid production graph cannot be replaced by another spec")
    return True


def _existing_graph_is_equivalent(
    database: sqlite3.Connection, graph: ProductionGraph,
) -> bool:
    """Compare executable topology and inputs while ignoring revision hash."""
    rows = database.execute(
        "SELECT node_id,node_type,paid,estimated_cost_cents,input_json "
        "FROM production_nodes WHERE job_id=?",
        (graph.job_id,),
    ).fetchall()
    if len(rows) != len(graph.nodes):
        return False
    existing_nodes = {
        (
            str(row["node_id"]), str(row["node_type"]), int(row["paid"] or 0),
            int(row["estimated_cost_cents"] or 0),
            json.dumps(
                json.loads(row["input_json"] or "{}"), ensure_ascii=False,
                sort_keys=True, separators=(",", ":"),
            ),
        )
        for row in rows
    }
    incoming_nodes = {
        (
            node.node_id, node.node_type, int(node.paid),
            int(node.estimated_cost_cents),
            json.dumps(
                dict(node.input), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ),
        )
        for node in graph.nodes
    }
    if existing_nodes != incoming_nodes:
        return False
    existing_edges = {
        (str(row["from_node_id"]), str(row["to_node_id"]), str(row["edge_type"]))
        for row in database.execute(
            "SELECT from_node_id,to_node_id,edge_type FROM production_edges "
            "WHERE job_id=?", (graph.job_id,),
        ).fetchall()
    }
    return existing_edges == set(graph.edges)


def persist_graph(
    database: sqlite3.Connection, spec: ProductionSpecV2, *, commit: bool = True,
) -> ProductionGraph:
    """Persist a validated graph; same spec is idempotent.

    ``commit=False`` is reserved for the production persistence boundary so
    graph rows, materialized shots and the frozen job spec commit together.
    """
    graph = compile_graph(spec)
    if not assert_graph_replaceable(database, graph):
        return graph
    existing = database.execute(
        "SELECT 1 FROM production_nodes WHERE job_id=? LIMIT 1",
        (graph.job_id,),
    ).fetchone()
    if existing and _existing_graph_is_equivalent(database, graph):
        def rebind() -> None:
            database.execute(
                "UPDATE production_nodes SET spec_hash=?,"
                "idempotency_key=CASE WHEN actual_cost_cents=0 "
                "AND COALESCE(provider_task_id,'')='' "
                "AND state IN ('PENDING','READY','FAILED') "
                "THEN ? || ':' || node_id ELSE idempotency_key END,"
                "updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
                (
                    graph.spec_hash,
                    f"{graph.job_id}:{graph.spec_hash}",
                    graph.job_id,
                ),
            )
        if commit:
            with database:
                rebind()
        else:
            rebind()
        return graph
    incoming = {target for _, target, _ in graph.edges}
    def write() -> None:
        database.execute(
            "DELETE FROM production_node_events WHERE job_id=?", (graph.job_id,)
        )
        database.execute(
            "DELETE FROM production_edges WHERE job_id=?", (graph.job_id,)
        )
        database.execute(
            "DELETE FROM production_nodes WHERE job_id=?", (graph.job_id,)
        )
        for node in graph.nodes:
            database.execute(
                "INSERT INTO production_nodes("
                "job_id,node_id,node_type,paid,state,spec_hash,input_json,"
                "idempotency_key,estimated_cost_cents) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    graph.job_id, node.node_id, node.node_type, int(node.paid),
                    "PENDING" if node.node_id in incoming else "READY",
                    graph.spec_hash,
                    json.dumps(node.input, ensure_ascii=False, sort_keys=True),
                    f"{graph.job_id}:{graph.spec_hash}:{node.node_id}",
                    node.estimated_cost_cents,
                ),
            )
        database.executemany(
            "INSERT INTO production_edges("
            "job_id,from_node_id,to_node_id,edge_type) VALUES(?,?,?,?)",
            ((graph.job_id, source, target, edge_type)
             for source, target, edge_type in graph.edges),
        )
    if commit:
        with database:
            write()
    else:
        write()
    return graph
