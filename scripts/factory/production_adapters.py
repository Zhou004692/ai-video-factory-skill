"""Adapters from immutable Production Graph nodes to proven production steps."""
from __future__ import annotations

from copy import deepcopy
import json
import sqlite3

from . import production_compiler, production_executor
from .steps import s3_shots, s5_bgm, s6_compose


class FrozenSpecMismatchError(RuntimeError):
    """A node no longer belongs to the job's frozen production contract."""

    pre_submit = True
    non_retryable = True


_MUTABLE_RUNTIME_SNAPSHOT_FIELDS = (
    "job_budget_cents",
    "max_auto_retries",
    "auto_retry_budget_cents",
)


def _overlay_mutable_runtime_controls(
    database: sqlite3.Connection,
    job_id: str,
    frozen_storyboard: dict,
) -> dict:
    """Overlay the paid-call control plane without mutating frozen creative IR.

    Production Spec v2 freezes what will be generated. Budget confirmation and
    retry consent are operator controls that can legitimately change after that
    freeze. Keeping them inside the immutable spec made resumed nodes execute
    with stale limits, so only this explicit allow-list is read from the live
    job at execution time.
    """
    runtime = deepcopy(frozen_storyboard)
    row = database.execute(
        "SELECT storyboard FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if row is None:
        return runtime
    try:
        live_storyboard = json.loads(row["storyboard"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return runtime
    live_settings = live_storyboard.get("production_settings")
    if not isinstance(live_settings, dict):
        return runtime
    runtime_settings = runtime.setdefault("production_settings", {})
    if not isinstance(runtime_settings, dict):
        runtime_settings = {}
        runtime["production_settings"] = runtime_settings
    if "budget_cents" in live_settings:
        runtime_settings["budget_cents"] = live_settings["budget_cents"]
    live_snapshot = live_settings.get("generation_snapshot")
    if not isinstance(live_snapshot, dict):
        return runtime
    runtime_snapshot = runtime_settings.setdefault("generation_snapshot", {})
    if not isinstance(runtime_snapshot, dict):
        runtime_snapshot = {}
        runtime_settings["generation_snapshot"] = runtime_snapshot
    for field in _MUTABLE_RUNTIME_SNAPSHOT_FIELDS:
        if field in live_snapshot:
            runtime_snapshot[field] = live_snapshot[field]
    return runtime


def _runtime_master_rows(spec) -> list[dict]:
    storyboard = dict(getattr(spec, "runtime_storyboard", {}) or {})
    return [
        dict(unit) for unit in (storyboard.get("masters") or [])
        if isinstance(unit, dict)
    ]


def materialize_frozen_execution_inputs(
    database: sqlite3.Connection, spec, *, commit: bool = True,
) -> None:
    """Atomically project an unpaid frozen contract into executable rows.

    Existing paid or accepted work is never rewritten. A mismatch after paid
    work remains a hard failure because silently renumbering those rows could
    purchase or compose the wrong master.
    """
    runtime_units = _runtime_master_rows(spec)
    runtime_indexes = {int(unit.get("idx") or 0) for unit in runtime_units}
    compiled_indexes = {int(master.idx) for master in spec.masters}
    if not compiled_indexes or runtime_indexes != compiled_indexes:
        raise FrozenSpecMismatchError(
            "frozen runtime masters do not match compiled master nodes"
        )
    existing_rows = database.execute(
        "SELECT idx,state,cost_cents,video_path FROM shots WHERE job_id=?",
        (spec.job_id,),
    ).fetchall()
    existing_indexes = {int(row["idx"]) for row in existing_rows}
    protected = any(
        str(row["state"] or "") in {"RUNNING", "OK", "REVIEW_REQUIRED"}
        or int(row["cost_cents"] or 0) > 0
        or bool(str(row["video_path"] or "").strip())
        for row in existing_rows
    )
    protected = protected or bool(database.execute(
        "SELECT 1 FROM provider_calls WHERE job_id=? AND shot_idx>0 LIMIT 1",
        (spec.job_id,),
    ).fetchone())
    if protected:
        if existing_indexes != compiled_indexes:
            raise FrozenSpecMismatchError(
                "paid materialized shot rows do not match frozen master nodes"
            )
        return
    available_columns = {
        str(row["name"])
        for row in database.execute("PRAGMA table_info(shots)").fetchall()
    }
    ordered_fields = [
        "job_id", "idx", "duration_sec", "shot_type", "camera", "prompt",
        "voiceover", "subtitle", "state", "narrative_type", "speaker_id",
        "anchor_character",
    ]
    fields = [field for field in ordered_fields if field in available_columns]
    database.execute("DELETE FROM shots WHERE job_id=?", (spec.job_id,))
    for unit in sorted(runtime_units, key=lambda item: int(item.get("idx") or 0)):
        values = {
            "job_id": spec.job_id,
            "idx": int(unit.get("idx") or 0),
            "duration_sec": float(
                unit.get("duration") or unit.get("duration_sec") or 0
            ),
            "shot_type": str(unit.get("shot_type") or ""),
            "camera": str(unit.get("camera") or ""),
            "prompt": str(unit.get("scene") or unit.get("prompt") or ""),
            "voiceover": str(unit.get("voiceover") or ""),
            "subtitle": str(unit.get("subtitle") or unit.get("voiceover") or ""),
            "state": "PENDING",
            "narrative_type": str(unit.get("type") or "dialogue"),
            "speaker_id": unit.get("speaker"),
            "anchor_character": unit.get("anchor_character"),
        }
        placeholders = ",".join("?" for _ in fields)
        database.execute(
            f"INSERT INTO shots({','.join(fields)}) VALUES({placeholders})",
            tuple(values[field] for field in fields),
        )
    if commit:
        database.commit()


def validate_frozen_execution_inputs(
    database: sqlite3.Connection, spec,
) -> None:
    """Reject a graph before payment when frozen masters are not runnable."""
    runtime_units = _runtime_master_rows(spec)
    runtime_indexes = {
        int(unit.get("idx") or 0)
        for unit in runtime_units if isinstance(unit, dict)
    }
    compiled_indexes = {int(master.idx) for master in spec.masters}
    if not compiled_indexes or runtime_indexes != compiled_indexes:
        raise FrozenSpecMismatchError(
            "frozen runtime masters do not match compiled master nodes"
        )
    database_indexes = {
        int(row["idx"])
        for row in database.execute(
            "SELECT idx FROM shots WHERE job_id=?", (spec.job_id,)
        ).fetchall()
    }
    if database_indexes != compiled_indexes:
        raise FrozenSpecMismatchError(
            "materialized shot rows do not match frozen master nodes"
        )


def _frozen_spec(
    database: sqlite3.Connection, node: sqlite3.Row,
) -> tuple[dict, dict]:
    job_id = str(node["job_id"])
    row = database.execute(
        "SELECT production_spec_json,production_spec_hash,"
        "production_spec_version,execution_engine FROM jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise FrozenSpecMismatchError(f"video job not found: {job_id}")
    if str(row["execution_engine"] or "legacy") != "graph_v2":
        raise FrozenSpecMismatchError("node job is not assigned to graph_v2")
    if int(row["production_spec_version"] or 0) != 2:
        raise FrozenSpecMismatchError("node job has no Production Spec v2")
    spec_hash = str(row["production_spec_hash"] or "")
    if not spec_hash or spec_hash != str(node["spec_hash"] or ""):
        raise FrozenSpecMismatchError("node spec hash differs from frozen job")
    try:
        spec = json.loads(row["production_spec_json"] or "{}")
        node_input = json.loads(node["input_json"] or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise FrozenSpecMismatchError("frozen production input is invalid") from exc
    if str(spec.get("spec_hash") or "") != spec_hash:
        raise FrozenSpecMismatchError("serialized production spec hash differs")
    if not isinstance(spec.get("runtime_storyboard"), dict):
        raise FrozenSpecMismatchError("frozen production storyboard is missing")
    return spec, node_input if isinstance(node_input, dict) else {}


def _required_scene_anchor_keys(spec: dict, master_idx: int) -> set[str]:
    """Read one master's asset requirements from the frozen graph, not identity.

    A stable singleton keeps its scene identity without a paid scene node.
    Missing graph structure is not evidence that dependencies are empty.
    """
    nodes, edges = spec.get("nodes"), spec.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise FrozenSpecMismatchError("frozen graph nodes/edges are missing or invalid")
    by_id: dict[str, dict] = {}
    for item in nodes:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("node_id"), str)
            or not item["node_id"].strip()
            or not isinstance(item.get("node_type"), str)
            or not item["node_type"].strip()
            or not isinstance(item.get("input"), dict)
        ):
            raise FrozenSpecMismatchError("frozen graph node is invalid")
        if item["node_id"] in by_id:
            raise FrozenSpecMismatchError("frozen graph node ids are not unique")
        by_id[item["node_id"]] = item
    for edge in edges:
        if not isinstance(edge, dict) or any(
            not isinstance(edge.get(field), str) or not edge[field].strip()
            for field in ("from_node_id", "to_node_id", "edge_type")
        ):
            raise FrozenSpecMismatchError("frozen graph edge is invalid")
        if edge["from_node_id"] not in by_id or edge["to_node_id"] not in by_id:
            raise FrozenSpecMismatchError("frozen graph edge endpoint is missing")
    master_id = f"master:{master_idx}"
    masters = [item for item in nodes if item["node_type"] == "master"
               and type(item["input"].get("master_idx")) is int
               and item["input"]["master_idx"] == master_idx]
    if len(masters) != 1 or masters[0]["node_id"] != master_id:
        raise FrozenSpecMismatchError("current master is missing or mismatched in frozen graph")
    runtime_units = spec.get("runtime_storyboard", {}).get("masters")
    if not isinstance(runtime_units, list) or any(not isinstance(unit, dict) for unit in runtime_units):
        raise FrozenSpecMismatchError("frozen runtime masters are invalid")
    current = [unit for unit in runtime_units if type(unit.get("idx")) is int and unit["idx"] == master_idx]
    if len(current) != 1:
        raise FrozenSpecMismatchError("current frozen runtime master is missing or duplicated")
    identity_keys = {
        str(current[0].get(field) or "").strip().casefold()
        for field in ("scene_anchor_key", "site_anchor_key")
        if current[0].get(field)
    }
    required: set[str] = set()
    incoming = [edge for edge in edges if edge["to_node_id"] == master_id]
    for edge in incoming:
        if edge["edge_type"] != "scene_reference":
            continue
        source = by_id[edge["from_node_id"]]
        key = source["input"].get("scene_key")
        if (
            source["node_type"] != "scene_anchor"
            or not isinstance(key, str) or not key.strip()
            or source["node_id"] != f"scene_anchor:{key}"
            or key.strip().casefold() not in identity_keys
        ):
            raise FrozenSpecMismatchError("frozen scene reference does not match current master")
        required.add(key.strip().casefold())
    # A canonical node for this scene/site may not lose its reference edge.
    # Also reject a renamed/mistyped node instead of treating it as no asset.
    for item in nodes:
        key = item["input"].get("scene_key")
        relevant = item["node_id"] in {f"scene_anchor:{value}" for value in identity_keys}
        relevant = relevant or (item["node_type"] == "scene_anchor" and isinstance(key, str)
                                and key.strip().casefold() in identity_keys)
        if not relevant:
            continue
        if (item["node_type"] != "scene_anchor" or not isinstance(key, str)
                or item["node_id"] != f"scene_anchor:{key}"
                or key.strip().casefold() not in required
                or not any(edge["from_node_id"] == item["node_id"] for edge in incoming)
                or any(edge["from_node_id"] == item["node_id"] and edge["edge_type"] != "scene_reference"
                       for edge in incoming)):
            raise FrozenSpecMismatchError("frozen scene anchor is missing its scene_reference edge")
    return required


async def run_master_node(
    database: sqlite3.Connection, node: sqlite3.Row,
) -> production_executor.NodeOutcome | dict[str, object]:
    spec, node_input = _frozen_spec(database, node)
    try:
        master_idx = int(node_input.get("master_idx") or 0)
    except (TypeError, ValueError) as exc:
        raise FrozenSpecMismatchError("master node has no valid index") from exc
    if master_idx <= 0 or str(node["node_id"]) != f"master:{master_idx}":
        raise FrozenSpecMismatchError("master node id and frozen input disagree")
    required_scene_keys = _required_scene_anchor_keys(spec, master_idx)
    storyboard = _overlay_mutable_runtime_controls(
        database,
        str(node["job_id"]),
        spec["runtime_storyboard"],
    )
    characters = deepcopy(list(spec.get("characters") or []))
    await s3_shots.run(
        str(node["job_id"]),
        storyboard_override=storyboard,
        characters_override=characters,
        target_master_indexes={master_idx},
        prepare_anchors=False,
        required_scene_anchor_keys=required_scene_keys,
    )
    shot = database.execute(
        "SELECT state,video_path,video_qa,error FROM shots "
        "WHERE job_id=? AND idx=?",
        (node["job_id"], master_idx),
    ).fetchone()
    if shot is None:
        raise FrozenSpecMismatchError(
            f"materialized master row disappeared: {master_idx}"
        )
    shot_state = str(shot["state"] or "")
    output: dict[str, object] = {"master_idx": master_idx}
    if str(shot["video_path"] or "").strip():
        output["video_path"] = str(shot["video_path"])
    if shot_state == "OK":
        return output
    if shot_state in {"REVIEW_REQUIRED", "REVIEW_DECIDING"}:
        try:
            qa = json.loads(shot["video_qa"] or "{}")
        except (TypeError, json.JSONDecodeError):
            qa = {}
        if isinstance(qa, dict):
            output.update({
                key: qa[key] for key in (
                    "candidate_path", "candidate_url", "reasons", "status",
                ) if qa.get(key) not in (None, "")
            })
        return production_executor.NodeOutcome.require_review(output)
    raise RuntimeError(
        f"master {master_idx} finished without usable output: "
        f"state={shot_state}; error={str(shot['error'] or '')[:300]}"
    )
    return {"master_idx": master_idx}


async def run_character_anchor_node(
    database: sqlite3.Connection, node: sqlite3.Row,
) -> production_executor.NodeOutcome:
    spec, node_input = _frozen_spec(database, node)
    character_id = str(node_input.get("character_id") or "").strip()
    character = next((
        deepcopy(item) for item in (spec.get("characters") or [])
        if isinstance(item, dict) and str(item.get("id") or "") == character_id
    ), None)
    if character is None:
        raise FrozenSpecMismatchError(
            f"frozen character is missing: {character_id}"
        )
    storyboard = deepcopy(spec["runtime_storyboard"])
    await s3_shots._ensure_character_identity_anchors(
        str(node["job_id"]), [character], storyboard=storyboard,
    )
    anchor = database.execute(
        "SELECT identity_key,state,approval_state,source_type,image_path,"
        "video_path FROM character_identity_anchors "
        "WHERE job_id=? AND character_id=? ORDER BY updated_at DESC LIMIT 1",
        (node["job_id"], character_id),
    ).fetchone()
    if anchor is None or str(anchor["state"]) != "READY":
        raise RuntimeError(f"character anchor is not ready: {character_id}")
    output = {
        "character_id": character_id,
        "identity_key": str(anchor["identity_key"]),
        "source_type": str(anchor["source_type"]),
        "image_path": str(anchor["image_path"] or ""),
        "video_path": str(anchor["video_path"] or ""),
    }
    if str(anchor["approval_state"] or "") == "APPROVED":
        return production_executor.NodeOutcome.succeed(output)
    return production_executor.NodeOutcome.require_review(output)


async def run_scene_anchor_node(
    database: sqlite3.Connection, node: sqlite3.Row,
) -> production_executor.NodeOutcome:
    spec, node_input = _frozen_spec(database, node)
    scene_key = str(node_input.get("scene_key") or "").strip().casefold()
    if not scene_key:
        raise FrozenSpecMismatchError("scene anchor node has no key")
    storyboard = deepcopy(spec["runtime_storyboard"])
    await s3_shots._ensure_scene_visual_anchors(
        str(node["job_id"]), storyboard, anchor_keys={scene_key},
    )
    anchor = database.execute(
        "SELECT state,approval_state,image_path,video_path "
        "FROM scene_visual_anchors WHERE job_id=? AND anchor_key=?",
        (node["job_id"], scene_key),
    ).fetchone()
    if anchor is None or str(anchor["state"]) != "READY":
        raise RuntimeError(f"scene anchor is not ready: {scene_key}")
    output = {
        "scene_key": scene_key,
        "image_path": str(anchor["image_path"] or ""),
        "video_path": str(anchor["video_path"] or ""),
    }
    if str(anchor["approval_state"] or "") == "APPROVED":
        return production_executor.NodeOutcome.succeed(output)
    return production_executor.NodeOutcome.require_review(output)


async def run_local_compose_node(
    database: sqlite3.Connection, node: sqlite3.Row,
) -> dict[str, object]:
    spec, _node_input = _frozen_spec(database, node)
    job_id = str(node["job_id"])
    storyboard = production_compiler.ensure_director_timeline(
        spec["runtime_storyboard"]
    )
    from . import visual_repair
    storyboard = visual_repair.adapter_storyboard(database, job_id, storyboard, spec['spec_hash'])
    await s6_compose.run(job_id, storyboard_override=storyboard)
    row = database.execute(
        "SELECT final_path FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if row is None or not str(row["final_path"] or "").strip():
        raise RuntimeError("local composition produced no final video")
    return {"final_path": str(row["final_path"])}


async def run_bgm_node(
    database: sqlite3.Connection, node: sqlite3.Row,
) -> dict[str, object]:
    _spec, node_input = _frozen_spec(database, node)
    provider = str(node_input.get("provider") or "").strip()
    if not provider:
        raise FrozenSpecMismatchError("bgm node has no frozen provider")
    job_id = str(node["job_id"])
    await s5_bgm.run(job_id, provider=provider)
    row = database.execute(
        "SELECT bgm_path FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if row is None or not str(row["bgm_path"] or "").strip():
        raise RuntimeError("bgm node produced no audio path")
    return {"provider": provider, "bgm_path": str(row["bgm_path"])}


async def run_final_review_node(
    database: sqlite3.Connection, node: sqlite3.Row,
) -> production_executor.NodeOutcome:
    _frozen_spec(database, node)
    return production_executor.NodeOutcome.require_review({
        "review_type": "final_video",
    })


def build_handlers(database: sqlite3.Connection):
    """Return handlers bound to the worker's durable database connection."""
    return {
        "character_anchor": (
            lambda node: run_character_anchor_node(database, node)
        ),
        "scene_anchor": lambda node: run_scene_anchor_node(database, node),
        "master": lambda node: run_master_node(database, node),
        "bgm": lambda node: run_bgm_node(database, node),
        "local_compose": lambda node: run_local_compose_node(database, node),
        "final_review": lambda node: run_final_review_node(database, node),
    }
