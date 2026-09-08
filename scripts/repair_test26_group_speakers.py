"""Audited, offline-only repair for Test26 before any master submission.

Defaults to preview. --apply requires a SQLite backup directory. This is an
incident-specific migration, not an override of the paid graph replacement API.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from factory import creative_ir, production_adapters, production_compiler
from factory import production_contract, production_graph

JOB = "J-TOPIC-24f362fa1c315927"
DRAFT = "SCRIPT-e0a4a19549cfefd3"
OLD_HASH = "9d6d621f961e97318da2a4b5bf46d6e0c117133b28fa55aeee55aa7c9d59bd08"
IDS = ["wang_gong", "lin_jie", "zhang_zhuguan"]
GROUP = ",".join(IDS)
OBSOLETE = "character_anchor:" + GROUP


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def clean(value, key=""):
    """Replace exact references only; never rewrite dialogue or visual prose."""
    if isinstance(value, list):
        result = []
        for member in value:
            if isinstance(member, dict) and any(
                member.get(field) == expected for field, expected in (
                    ("id", GROUP), ("character_id", GROUP),
                    ("identity_key", "script:" + GROUP),
                )
            ):
                continue
            if key in {"cast", "speakers", "characters", "speaker_ids", "character_ids", "audio_speakers"}:
                expanded = IDS if member == GROUP else [clean(member)]
                for item in expanded:
                    if item not in result:
                        result.append(item)
            elif member == "identity:script:" + GROUP:
                result.extend("identity:script:" + item for item in IDS)
            else:
                result.append(clean(member))
        return result
    if isinstance(value, dict):
        result = {k: clean(v, k) for k, v in value.items()
                  if not (key == "character_mappings" and k == GROUP)}
        if value.get("speaker") == GROUP:
            result.update(speaker=IDS[0], speakers=IDS[:])
        if value.get("anchor_character") == GROUP:
            result["anchor_character"] = IDS[0]
        return result
    return value


def rows(database, table):
    column, identifier = ("draft_id", DRAFT) if table == "script_drafts" else ("job_id", JOB)
    return [dict(row) for row in database.execute(
        f"SELECT * FROM {table} WHERE {column}=? ORDER BY rowid", (identifier,)
    )]


TABLES = ("jobs", "script_drafts", "shots", "production_nodes", "production_edges",
          "production_node_events", "provider_calls", "character_identity_anchors", "scene_visual_anchors")


def snapshot(database):
    return {table: rows(database, table) for table in TABLES}


def prepare(database):
    before = snapshot(database)
    require(len(before["jobs"]) == len(before["script_drafts"]) == 1, "incident rows missing")
    job = before["jobs"][0]
    require(job["state"] == "ANCHOR_REVIEW_REQUIRED", "job no longer paused for anchor review")
    require(not job.get("production_worker_pid"), "worker is active")
    require(job["production_spec_hash"] == OLD_HASH, "frozen contract changed")
    require(before["script_drafts"][0]["approved_job_id"] == JOB, "draft binding changed")
    require(not any(int(row["shot_idx"] or 0) > 0 for row in before["provider_calls"]), "master already submitted")
    for row in before["shots"]:
        require(row["state"] == "PENDING" and not row["cost_cents"] and not row["video_path"], "shot has work")
    old = json.loads(job["production_spec_json"])
    corrected_ir = creative_ir.from_dict(clean(json.loads(job["creative_ir_json"])))
    compilation = production_compiler.compile_ir(
        corrected_ir, job_id=JOB,
        capabilities=production_compiler.ProviderCapabilities(**old["capabilities"]),
        budget_cents=old["budget_cents"], runtime_storyboard=clean(old["runtime_storyboard"]),
    )
    require(compilation.compilable, f"compiler blockers: {compilation.blockers}")
    spec = compilation.spec
    require([c["id"] for c in spec.characters] == IDS, "unexpected cast")
    require(GROUP not in encode(spec.as_dict()), "unresolved composite references")
    graph = production_graph.compile_graph(spec)
    old_nodes = {row["node_id"]: row for row in before["production_nodes"]}
    new_nodes = {node.node_id: node for node in graph.nodes}
    require(set(old_nodes) - set(new_nodes) == {OBSOLETE}, "unexpected removed nodes")
    require(not set(new_nodes) - set(old_nodes), "repair would add production nodes")
    require(len(old["masters"]) == len(spec.masters), "master count changed")
    for original, corrected in zip(old["masters"], spec.as_dict()["masters"]):
        for key in original:
            if key not in {"character_ids", "speaker_ids"}:
                require(encode(original[key]) == encode(corrected[key]), f"master {original['idx']} {key} changed")
    for node in graph.nodes:
        original = old_nodes[node.node_id]
        require(encode(json.loads(original["input_json"])) == encode(node.input), "node input changed")
        require(original["node_type"] == node.node_type, "node type changed")
        if node.node_type in {"character_anchor", "scene_anchor"}:
            require(original["state"] == "REVIEW_REQUIRED", "anchor state changed")
        else:
            require(original["state"] == "PENDING" and not original["provider_task_id"]
                    and not original["actual_cost_cents"], "downstream work already started")
    require(old_nodes[OBSOLETE]["state"] == "REVIEW_REQUIRED", "obsolete node state changed")
    return before, corrected_ir, spec, graph


def apply_repair(database, before, corrected_ir, spec, graph, backup_path):
    """Caller holds BEGIN IMMEDIATE; preserve paid evidence in the audit record."""
    require(snapshot(database) == before, "incident changed after backup; rerun preview")
    note = {
        "reason": "group speaker references incorrectly created a fourth identity",
        "backup": str(backup_path), "old_spec_hash": OLD_HASH, "new_spec_hash": spec.spec_hash,
        "archived_node": next(row for row in before["production_nodes"] if row["node_id"] == OBSOLETE),
        "archived_node_events": [row for row in before["production_node_events"] if row["node_id"] == OBSOLETE],
        "cost_cents_preserved": before["jobs"][0]["cost_cents"],
        "anchor_review": "existing images retained; human visual approval still required",
    }
    database.execute("INSERT INTO production_interventions(job_id,intervention_type,description) VALUES(?,?,?)",
                     (JOB, "group_speaker_reference_repair", encode(note)))
    database.execute("DELETE FROM production_node_events WHERE job_id=? AND node_id=?", (JOB, OBSOLETE))
    database.execute("DELETE FROM production_nodes WHERE job_id=? AND node_id=?", (JOB, OBSOLETE))
    database.execute("DELETE FROM production_edges WHERE job_id=?", (JOB,))
    for edge in graph.edges:
        database.execute("INSERT INTO production_edges(job_id,from_node_id,to_node_id,edge_type) VALUES(?,?,?,?)",
                         (JOB, *edge))
    # Keep original paid idempotency keys/task IDs; unpaid nodes have never submitted.
    database.execute("UPDATE production_nodes SET spec_hash=?,idempotency_key=CASE "
                     "WHEN provider_task_id IS NULL AND actual_cost_cents=0 THEN ? || ':' || node_id "
                     "ELSE idempotency_key END,updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
                     (spec.spec_hash, f"{JOB}:{spec.spec_hash}", JOB))
    production_adapters.materialize_frozen_execution_inputs(database, spec, commit=False)
    production_adapters.validate_frozen_execution_inputs(database, spec)
    production_contract.persist_production_spec(database, spec, commit=False)
    # Preserve mutable controls and all original editorial choices in the live board.
    storyboard = clean(json.loads(before["jobs"][0]["storyboard"]))
    storyboard["masters"] = deepcopy(spec.runtime_storyboard["masters"])
    storyboard["master_dependency_graph"] = deepcopy(spec.runtime_storyboard["master_dependency_graph"])
    database.execute("UPDATE jobs SET storyboard=?,characters_json=?,creative_ir_json=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
                     (encode(storyboard), encode(spec.characters), encode(corrected_ir.as_dict()), JOB))
    draft = before["script_drafts"][0]
    database.execute("UPDATE script_drafts SET script_json=?,storyboard_json=?,updated_at=CURRENT_TIMESTAMP WHERE draft_id=?",
                     (encode(clean(json.loads(draft["script_json"]))), encode(clean(json.loads(draft["storyboard_json"]))), DRAFT))
    after = snapshot(database)
    for table in ("provider_calls", "character_identity_anchors", "scene_visual_anchors"):
        require(before[table] == after[table], f"paid evidence changed: {table}")
    require(after["jobs"][0]["cost_cents"] == before["jobs"][0]["cost_cents"], "cost changed")
    require(after["jobs"][0]["state"] == "ANCHOR_REVIEW_REQUIRED", "job resumed unexpectedly")
    require(production_graph._existing_graph_is_equivalent(database, graph), "graph/spec differ")
    for row in after["production_nodes"]:
        production_adapters._frozen_spec(database, row)
    for master in spec.masters:
        production_adapters._required_scene_anchor_keys(json.loads(encode(spec.as_dict())), master.idx)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    source = sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    before, ir, spec, graph = prepare(source)
    print(encode({"job_id": JOB, "roles": len(spec.characters), "masters": len(spec.masters),
                  "removed_active_node": OBSOLETE, "cost_cents": before["jobs"][0]["cost_cents"],
                  "spec_hash": spec.spec_hash, "apply": args.apply}))
    if not args.apply:
        return
    require(args.backup_dir is not None, "--backup-dir is required for apply")
    directory = args.backup_dir.resolve() / datetime.now(timezone.utc).strftime("test26-group-%Y%m%dT%H%M%S%fZ")
    directory.mkdir(parents=True, exist_ok=False)
    backup_path = directory / "factory.db"
    with sqlite3.connect(backup_path) as backup:
        source.backup(backup)
        backup.row_factory = sqlite3.Row
        require(snapshot(backup) == before, "incident changed while backing up")
    (directory / "before.json").write_text(encode(before), encoding="utf-8")
    (directory / "corrected-spec.json").write_text(encode(spec.as_dict()), encoding="utf-8")
    source.close()
    database = sqlite3.connect(args.database.resolve())
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys=ON")
    try:
        database.execute("BEGIN IMMEDIATE")
        apply_repair(database, before, ir, spec, graph, backup_path)
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()
    print("Repair committed; backup: " + str(backup_path))


if __name__ == "__main__":
    main()
