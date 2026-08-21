"""Side-effect-free cost planning for native-master productions."""
from __future__ import annotations

import json
import os
import sqlite3

from . import (
    anchor_generation, asset_library, character_assets, master_dependencies, master_risk, production_config,
    reference_attention, scene_assets,
)
from .clients import seedance, volc_tts
from .video_routing import normalize_audio_mode


def compile_job_spec(
    db: sqlite3.Connection,
    job_id: str,
    root: str | os.PathLike[str],
    **kwargs,
):
    """Compile the immutable job contract through the planning boundary."""
    from .production_contract import compile_production_spec

    return compile_production_spec(db, job_id, root, **kwargs)


def _nonnegative_int(value: str | int | None, default: int) -> int:
    try:
        return max(0, int(value)) if value is not None else default
    except (TypeError, ValueError):
        return default


def _duration_bucket(duration_sec: float) -> str:
    """Return the stable duration dimension used by cost calibration."""
    if duration_sec <= 12:
        return "4-12s"
    if duration_sec <= 24:
        return "13-24s"
    return "25-30s"


def _reference_type(reference_kinds: set[str]) -> str:
    """Collapse the paid input mix into a small, stable calibration enum."""
    kinds = {kind for kind in reference_kinds if kind in {"audio", "image", "video"}}
    if not kinds:
        return "none"
    if len(kinds) == 1:
        return next(iter(kinds))
    return "mixed"


def _voice_preflight(
    db: sqlite3.Connection, masters: list[dict], characters_by_id: dict[str, dict],
    *, audio_mode: str,
) -> dict:
    """Describe fixed-role TTS work without querying the remote catalog."""
    if audio_mode == "seedance_native":
        return {
            "mode": audio_mode,
            "roles": [],
            "speaking_role_count": 0,
            "bound_role_count": 0,
            "auto_match_role_count": 0,
            "unknown_speakers": [],
            "estimated_tts_calls": 0,
            "estimated_tts_cost_cents": 0,
        }
    bindings: dict[str, dict] = {}
    table_exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='character_voice_bindings'"
    ).fetchone()
    if table_exists:
        bindings = {
            str(row["character_id"]): dict(row)
            for row in db.execute(
                "SELECT * FROM character_voice_bindings WHERE status='Active'"
            ).fetchall()
        }
    segments: list[dict] = []
    speaking_ids: list[str] = []
    for master in masters:
        if str(master.get("type") or master.get("narrative_type") or "").casefold() != "dialogue":
            continue
        beats = [
            beat for beat in (master.get("performance_beats") or [])
            if isinstance(beat, dict) and str(beat.get("dialogue") or "").strip()
        ]
        if not beats and str(master.get("voiceover") or "").strip():
            beats = [{
                "speaker": master.get("speaker") or master.get("speaker_id"),
                "dialogue": master.get("voiceover"),
            }]
        for beat in beats:
            character_id = str(
                beat.get("speaker")
                or master.get("speaker")
                or master.get("speaker_id")
                or ""
            ).strip()
            text = str(beat.get("dialogue") or "").strip()
            if character_id and character_id not in speaking_ids:
                speaking_ids.append(character_id)
            segments.append({
                "master_idx": int(master.get("idx") or 0),
                "character_id": character_id,
                "text_chars": len(text),
                "estimated_cost_cents": volc_tts.estimate_cost_cents(text),
            })
    roles = []
    for character_id in speaking_ids:
        character = characters_by_id.get(character_id) or {}
        binding = bindings.get(character_id) or {}
        embedded_voice_type = str(character.get("voice_type") or "").strip()
        voice_type = str(binding.get("voice_type") or embedded_voice_type).strip()
        roles.append({
            "character_id": character_id,
            "display_name": str(
                character.get("script_character_name")
                or character.get("name")
                or character_id
            ),
            "status": "bound" if voice_type else "auto_match_pending",
            "voice_type": voice_type,
            "voice_name": str(binding.get("voice_name") or voice_type),
            "source": str(binding.get("source") or ("storyboard" if voice_type else "")),
        })
    unknown_speakers = sorted({
        item["character_id"] for item in segments
        if not item["character_id"] or item["character_id"] not in characters_by_id
    })
    return {
        "mode": audio_mode,
        "roles": roles,
        "speaking_role_count": len(roles),
        "bound_role_count": sum(item["status"] == "bound" for item in roles),
        "auto_match_role_count": sum(
            item["status"] == "auto_match_pending" for item in roles
        ),
        "unknown_speakers": unknown_speakers,
        "estimated_tts_calls": len(segments),
        "estimated_tts_cost_cents": sum(
            item["estimated_cost_cents"] for item in segments
        ),
    }


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
    settings = storyboard.get("production_settings") or {}
    frozen_snapshot = settings.get(production_config.GENERATION_SNAPSHOT_KEY)
    configured_budget = settings.get(
        "budget_cents", settings.get("job_budget_cents")
    )
    if configured_budget is None and isinstance(frozen_snapshot, dict):
        configured_budget = frozen_snapshot.get("job_budget_cents")
    resolved_budget = _nonnegative_int(
        budget_cents
        if budget_cents is not None
        else (
            configured_budget
            if configured_budget is not None
            else os.getenv("SEEDANCE_JOB_BUDGET_CENTS")
        ),
        2500,
    )
    if frozen_snapshot:
        snapshot = production_config.generation_snapshot(storyboard)
    else:
        legacy_settings = dict(settings)
        if resolution:
            legacy_settings["resolution"] = resolution
        snapshot = production_config.freeze_generation_snapshot(legacy_settings)
    resolved_resolution = snapshot["resolution"]

    characters = list(storyboard.get("characters") or [])
    characters_by_id = {
        str(character.get("id") or ""): character
        for character in characters
        if str(character.get("id") or "")
    }
    audio_mode = normalize_audio_mode(
        (storyboard.get("production_settings") or {}).get("audio_mode")
        or os.getenv("DIALOGUE_AUDIO_MODE")
    )
    voice_plan = _voice_preflight(
        db, masters, characters_by_id, audio_mode=audio_mode
    )
    makeup_plan = anchor_generation.apply_character_generator(
        character_assets.character_makeup_plan(characters),
        storyboard,
        resolution=resolved_resolution,
    )
    completed_identity_keys = completed_identity_keys or set()
    paid_identity_anchors: list[dict] = []
    for entry in makeup_plan:
        if entry["identity_key"] in completed_identity_keys:
            entry["status"] = "ready"
            entry["requires_paid_call"] = False
        if not entry["requires_paid_call"]:
            continue
        paid_identity_anchors.append(entry)

    completed_scene_keys = {
        str(key).casefold() for key in (completed_scene_keys or set())
    }
    paid_scene_anchors = []
    for entry in scene_assets.recurring_scene_anchors(storyboard):
        if entry["anchor_key"] in completed_scene_keys:
            continue
        entry = anchor_generation.decorate_scene_anchor(
            entry, storyboard, resolution=resolved_resolution
        )
        paid_scene_anchors.append(entry)

    generated_anchor_duration = {
        entry["identity_key"]: float(entry["duration_sec"])
        for entry in makeup_plan
        if entry["anchor_mode"] == "dynamic_video"
    }
    identity_anchor_modes = {
        str(entry["identity_key"]): str(entry.get("anchor_mode") or "")
        for entry in makeup_plan
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
        reference_kinds: set[str] = set()
        planned_reference_manifest: list[dict] = []

        def plan_reference(kind: str, duty: str, identity_key: str = "") -> None:
            item = {
                "kind": kind,
                "duty": duty,
                "identity_key": identity_key,
                "required": True,
                "priority": reference_attention.PRIORITY[duty],
            }
            signature = (kind, duty, identity_key)
            if signature not in {
                (entry["kind"], entry["duty"], entry["identity_key"])
                for entry in planned_reference_manifest
            }:
                planned_reference_manifest.append(item)
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
            anchor_mode = identity_anchor_modes.get(speaker_identity)
            if anchor_mode == "dynamic_video":
                reference_kinds.add("video")
                plan_reference("video", "speaker_identity", speaker_identity)
            elif anchor_mode in {"static_image", "volc_asset"}:
                reference_kinds.add("image")
                plan_reference("image", "speaker_identity", speaker_identity)
            if previous_speaker_master:
                reference_masters[previous_speaker_master[0]] = (
                    previous_speaker_master[1]
                )
                reference_kinds.add("video")
                plan_reference("video", "speaker_identity", speaker_identity)
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
                reference_kinds.add("video")
                plan_reference("video", "scene_continuity", scene_key)
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
        for identity_key in sorted(supporting_keys):
            if identity_key == speaker_identity:
                continue
            anchor_mode = identity_anchor_modes.get(identity_key)
            if anchor_mode == "dynamic_video":
                reference_kinds.add("video")
                plan_reference("video", "supporting_identity", identity_key)
            elif anchor_mode in {"static_image", "volc_asset"}:
                reference_kinds.add("image")
                plan_reference("image", "supporting_identity", identity_key)
        if master_type == "dialogue" and audio_mode != "seedance_native":
            reference_kinds.add("audio")
            plan_reference("audio", "voice_continuity", speaker_identity)
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
            "resolution": resolved_resolution,
            "duration_bucket": _duration_bucket(generation_duration),
            "reference_type": _reference_type(reference_kinds),
            "reference_manifest": sorted(
                planned_reference_manifest,
                key=lambda item: item["priority"],
            ),
            "estimated_cost_cents": seedance.estimate_cost_cents(
                generation_duration + reference_duration, resolved_resolution
            ),
            "quality_risk": master_risk.assess_master(master),
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
        + voice_plan["estimated_tts_cost_cents"]
    )
    projected_total_cost = max(0, int(current_cost_cents or 0)) + estimated_cost
    paid_duration = sum(item["duration_sec"] for item in paid_masters)
    reasons = [
        f"{len(library_hits)} B-roll master(s) will reuse approved local assets"
    ] if library_hits else []
    if len(masters) == 1:
        reasons.append("storyboard already uses the minimum one-master shape")

    high_risk_master_indexes = [
        int(item["master_idx"])
        for item in paid_masters
        if item.get("quality_risk", {}).get("level") == "high"
    ]
    return {
        "generation_snapshot": snapshot,
        "master_dependency_graph": (
            storyboard.get("master_dependency_graph")
            or master_dependencies.build_master_dependency_graph(masters)
        ),
        "total_masters": len(masters),
        "paid_master_count": len(paid_masters),
        "estimated_paid_calls": (
            len(paid_masters) + len(paid_identity_anchors)
            + len(paid_scene_anchors)
            + voice_plan["estimated_tts_calls"]
        ),
        "estimated_tts_calls": voice_plan["estimated_tts_calls"],
        "estimated_tts_cost_cents": voice_plan["estimated_tts_cost_cents"],
        "voice_plan": voice_plan,
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
        "high_risk_master_indexes": high_risk_master_indexes,
        "requires_long_master_confirmation": bool(high_risk_master_indexes),
        "paid_identity_anchors": paid_identity_anchors,
        "paid_scene_anchors": paid_scene_anchors,
        "character_makeup_plan": makeup_plan,
        "warnings": warnings,
        "reduction_reasons": reasons,
    }
