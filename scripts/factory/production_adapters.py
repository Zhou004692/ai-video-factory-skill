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
    storyboard = deepcopy(spec["runtime_storyboard"])
    characters = deepcopy(list(spec.get("characters") or []))
    await s3_shots.run(
        str(node["job_id"]),
        storyboard_override=storyboard,
        characters_override=characters,
        target_master_indexes={master_idx},
        prepare_anchors=False,
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
