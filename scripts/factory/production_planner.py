"""Side-effect-free cost planning for native-master productions."""
from __future__ import annotations

import json
import os
import sqlite3

from . import asset_library, character_assets, scene_assets
from .clients import seedance, seedream_45


def _nonnegative_int(value: str | int | None, default: int) -> int:
    try:
        return max(0, int(value)) if value is not None else default
    except (TypeError, ValueError):
        return default


def analyze_storyboard(
    db: sqlite3.Connection,
    storyboard: dict,
    *,
    budget_cents: int | None = None,
    resolution: str | None = None,
    completed_master_indexes: set[int] | None = None,
    completed_identity_keys: set[str] | None = None,
    completed_scene_keys: set[str] | None = None,
    current_cost_cents: int = 0,
) -> dict:
    """Estimate paid Seedance work without submitting or mutating anything."""
    masters = list(storyboard.get("masters") or [])
    resolved_budget = _nonnegative_int(
        budget_cents
        if budget_cents is not None
        else os.getenv("SEEDANCE_JOB_BUDGET_CENTS"),
        2500,
    )
    resolved_resolution = resolution or os.getenv(
        "ARK_VIDEO_RESOLUTION", "720p"
    )

    characters = list(storyboard.get("characters") or [])
    characters_by_id = {
        str(character.get("id") or ""): character
        for character in characters
        if str(character.get("id") or "")
    }
    makeup_plan = character_assets.character_makeup_plan(characters)
    completed_identity_keys = completed_identity_keys or set()
    paid_identity_anchors: list[dict] = []
    for entry in makeup_plan:
        if entry["identity_key"] in completed_identity_keys:
            entry["status"] = "ready"
            entry["requires_paid_call"] = False
        if not entry["requires_paid_call"]:
            continue
        if entry["anchor_mode"] == "static_image":
            entry["estimated_cost_cents"] = seedream_45.estimate_cost_cents()
        else:
            entry["estimated_cost_cents"] = seedance.estimate_cost_cents(
                entry["duration_sec"], resolved_resolution
            )
        paid_identity_anchors.append(entry)

    completed_scene_keys = {
        str(key).casefold() for key in (completed_scene_keys or set())
    }
    paid_scene_anchors = []
    for entry in scene_assets.recurring_scene_anchors(storyboard):
        if entry["anchor_key"] in completed_scene_keys:
            continue
        entry = dict(entry)
        entry["estimated_cost_cents"] = seedream_45.estimate_cost_cents()
        paid_scene_anchors.append(entry)

    generated_anchor_duration = {
        entry["identity_key"]: float(entry["duration_sec"])
        for entry in makeup_plan
        if entry["anchor_mode"] == "dynamic_video"
    }

    library_hits: list[dict] = []
    paid_masters: list[dict] = []
    warnings: list[str] = []
    completed = completed_master_indexes or set()
    dialogue_anchor_master: dict[str, tuple[int, float]] = {}
    scene_anchor_master: dict[str, tuple[int, float]] = {}

    for master in masters:
        idx = int(master.get("idx") or 0)
        editorial_duration = float(master.get("duration") or 0)
        try:
            generation_duration = seedance.normalize_generation_duration(
                editorial_duration
            )
        except seedance.SeedanceParameterError as exc:
            raise seedance.SeedanceParameterError(
                f"母片 #{idx} 的编辑时长 {editorial_duration:g} 秒无法提交：{exc}"
            ) from exc
        master_type = master.get("type") or "unknown"
        render_mode = str(master.get("render_mode") or "presenter").lower()
        speaker_id = str(
            master.get("speaker_id") or master.get("speaker") or ""
        ).strip()
        reference_duration = 0.0
        speaker_character = characters_by_id.get(speaker_id)
        speaker_identity = (
            character_assets.character_identity_key(speaker_character)
            if speaker_character else f"speaker:{speaker_id.casefold()}"
        )
        reference_masters: dict[int, float] = {}
        if (
            master_type == "dialogue"
            and speaker_id
            and render_mode in {
                "presenter", "digital_human", "cinematic_character"
            }
        ):
            previous_speaker_master = dialogue_anchor_master.get(
                speaker_identity
            )
            if previous_speaker_master:
                reference_masters[previous_speaker_master[0]] = (
                    previous_speaker_master[1]
                )
            else:
                reference_duration = generated_anchor_duration.get(
                    speaker_identity, 0.0
                )
            scene_key = str(
                master.get("scene_anchor_key") or ""
            ).strip().casefold()
            previous_scene_master = scene_anchor_master.get(scene_key)
            if scene_key and previous_scene_master:
                reference_masters[previous_scene_master[0]] = (
                    previous_scene_master[1]
                )
            reference_duration += sum(reference_masters.values())
            dialogue_anchor_master[speaker_identity] = (
                idx, generation_duration
            )
            if scene_key:
                scene_anchor_master[scene_key] = (idx, generation_duration)

        # Supporting characters need the same visual identity references as
        # the speaking character.  Use structured cast when present, otherwise
        # match script-role names in the restored scene/action description.
        text = " ".join(
            str(master.get(field) or "")
            for field in ("scene", "action", "scene_dynamics", "visual_intent")
        ).casefold()
        explicit_cast = master.get("cast") or []
        explicit_names = {
            str(
                item.get("id") or item.get("name")
                or item.get("script_character_name") or ""
            ).strip().casefold()
            if isinstance(item, dict) else str(item or "").strip().casefold()
            for item in explicit_cast
        }
        supporting_keys: set[str] = set()
        for character_id, cast_character in characters_by_id.items():
            if character_id == speaker_id:
                continue
            names = {
                str(value).strip().casefold()
                for value in (
                    character_id,
                    cast_character.get("name"),
                    cast_character.get("script_character_name"),
                )
                if str(value or "").strip()
            }
            appears = (
                bool(names & explicit_names)
                if explicit_names else any(name in text for name in names)
            )
            if appears:
                supporting_keys.add(
                    character_assets.character_identity_key(cast_character)
                )
        reference_duration += sum(
            generated_anchor_duration.get(identity_key, 0.0)
            for identity_key in supporting_keys
            if identity_key != speaker_identity
        )
        if idx in completed:
            continue
        if master_type == "broll":
            tags = list(master.get("asset_tags") or [])
            if not tags:
                warnings.append(
                    f"master {idx}: B-roll lacks asset_tags; it will require a paid generation"
                )
            match = asset_library.find_broll_match(
                db,
                tags=tags,
                min_duration_sec=editorial_duration,
                orientation=master.get("orientation") or "9:16",
            )
            if match is not None:
                asset_tags = set(json.loads(match["tags_json"] or "[]"))
                query_tags = {str(tag).strip() for tag in tags if str(tag).strip()}
                score = (
                    len(query_tags & asset_tags) / len(query_tags)
                    if query_tags
                    else 0.0
                )
                library_hits.append(
                    {
                        "master_idx": idx,
                        "asset_id": match["asset_id"],
                        "path": match["path"],
                        "match_score": score,
                    }
                )
                continue
            if tags:
                warnings.append(
                    f"master {idx}: no approved reusable B-roll matches asset_tags"
                )

        paid_master = {
            "master_idx": idx,
            "type": master_type,
            "duration_sec": generation_duration,
            "editorial_duration_sec": editorial_duration,
            "generation_duration_sec": generation_duration,
            "estimated_cost_cents": seedance.estimate_cost_cents(
                generation_duration + reference_duration, resolved_resolution
            ),
        }
        if reference_duration:
            paid_master["reference_duration_sec"] = reference_duration
        paid_masters.append(paid_master)

    identity_anchor_cost = sum(
        item["estimated_cost_cents"] for item in paid_identity_anchors
    )
    scene_anchor_cost = sum(
        item["estimated_cost_cents"] for item in paid_scene_anchors
    )
    estimated_cost = (
        sum(item["estimated_cost_cents"] for item in paid_masters)
        + identity_anchor_cost
        + scene_anchor_cost
    )
    projected_total_cost = max(0, int(current_cost_cents or 0)) + estimated_cost
    paid_duration = sum(item["duration_sec"] for item in paid_masters)
    reasons = [
        f"{len(library_hits)} B-roll master(s) will reuse approved local assets"
    ] if library_hits else []
    if len(masters) == 1:
        reasons.append("storyboard already uses the minimum one-master shape")

    return {
        "total_masters": len(masters),
        "paid_master_count": len(paid_masters),
        "estimated_paid_calls": (
            len(paid_masters) + len(paid_identity_anchors)
            + len(paid_scene_anchors)
        ),
        "paid_identity_anchor_count": len(paid_identity_anchors),
        "identity_anchor_cost_cents": identity_anchor_cost,
        "paid_scene_anchor_count": len(paid_scene_anchors),
        "scene_anchor_cost_cents": scene_anchor_cost,
        "paid_duration_sec": paid_duration,
        "estimated_cost_cents": estimated_cost,
        "current_cost_cents": max(0, int(current_cost_cents or 0)),
        "projected_total_cost_cents": projected_total_cost,
        "budget_cents": resolved_budget,
        "budget_ok": projected_total_cost <= resolved_budget,
        "resolution": resolved_resolution,
        "library_hits": library_hits,
        "paid_masters": paid_masters,
        "paid_identity_anchors": paid_identity_anchors,
        "paid_scene_anchors": paid_scene_anchors,
        "character_makeup_plan": makeup_plan,
        "warnings": warnings,
        "reduction_reasons": reasons,
    }
