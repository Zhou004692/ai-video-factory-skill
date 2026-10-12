"""Authored visual intent only; no observations, provider actions or QA verdicts.

visual_requirements are explicit visible actions, not factual source claims.
Definitions retain unknown fields. Links carry source_beat_id and the actual
video master/edit interval separately from source_ids (evidence provenance).
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Mapping

REFERENCE_CHOICES = frozenset({
    "text", "multimodal", "strict_frame", "storyboard", "motion_reference",
    "native_edit", "extend",
})
CONTRACT_FIELDS = (
    "beat_id", "id", "source_beat_id", "camera_intent", "must_preserve",
    "must_preserve_details", "may_adapt", "may_adapt_details",
    "visual_requirements", "reference_control", "source_ids",
)


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value.strip()


def normalize_requirements(value: object, *, allowed_source_ids=None) -> list[dict]:
    """Validate explicit definitions, preserving full objects and unknown keys."""
    if not isinstance(value, (list, tuple)):
        raise ValueError("visual_requirements must be a list of objects")
    result, seen = [], {}
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("visual_requirements entries must be objects")
        item = deepcopy(dict(raw))
        item["id"] = _required_text(item.get("id"), "visual requirement id")
        item["description"] = _required_text(item.get("description"), "visual requirement description")
        if "source_ids" in item:
            if not isinstance(item["source_ids"], (list, tuple)):
                raise ValueError("visual requirement source_ids must be a list")
            item["source_ids"] = list(dict.fromkeys(
                _required_text(s, "visual requirement source_id") for s in item["source_ids"]
            ))
            if allowed_source_ids is not None and set(item["source_ids"]) - set(allowed_source_ids):
                raise ValueError("visual requirement references unknown source_id")
        if item["id"] in seen and seen[item["id"]] != item:
            raise ValueError(f"conflicting visual requirement id: {item['id']}")
        if item["id"] not in seen:
            result.append(item)
        seen[item["id"]] = item
    return result


def requirement_set_hash(requirements: object) -> str:
    """Stable hash of complete definitions; ordering is not evidence identity."""
    normalized = sorted(normalize_requirements(requirements), key=lambda r: r["id"])
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_beats(beats, *, allowed_source_ids=None) -> None:
    """Reject duplicate authored IDs and conflicting requirement definitions."""
    seen_ids, definitions, preserved = set(), [], {}
    for beat in beats:
        if not isinstance(beat, Mapping):
            raise ValueError("beat must be an object")
        aliases = [beat[key] for key in ("id", "beat_id") if key in beat]
        aliases = [_required_text(a, "beat id") for a in aliases]
        if len(set(aliases)) > 1:
            raise ValueError("conflicting id and beat_id")
        if aliases:
            if aliases[0] in seen_ids:
                raise ValueError(f"duplicate beat id: {aliases[0]}")
            seen_ids.add(aliases[0])
        extensions = beat.get("extensions") or {}
        for field in ("must_preserve", "may_adapt"):
            raw = beat.get(f"{field}_details", extensions.get(f"{field}_details", beat.get(field, [])))
            raw = [raw] if isinstance(raw, str) else raw
            if not isinstance(raw, (list, tuple)):
                raise ValueError(f"{field} must be a list")
            for item in raw:
                if isinstance(item, Mapping):
                    key = _required_text(item.get("id"), f"{field} id")
                    _required_text(item.get("description"), f"{field} description")
                    previous = preserved.get((field, key))
                    if previous is not None and previous != dict(item):
                        raise ValueError(f"conflicting {field} id: {key}")
                    preserved[(field, key)] = deepcopy(dict(item))
                else:
                    _required_text(item, field)
        raw_requirements = beat.get("visual_requirements", extensions.get("visual_requirements", []))
        definitions.extend(normalize_requirements(raw_requirements, allowed_source_ids=allowed_source_ids))
        control = beat.get("reference_control", extensions.get("reference_control"))
        if control is not None:
            if not isinstance(control, Mapping) or control.get("choice") not in REFERENCE_CHOICES:
                raise ValueError("reference_control has invalid choice")
            _required_text(control.get("rationale"), "reference_control rationale")
    normalize_requirements(definitions, allowed_source_ids=allowed_source_ids)


def project_fields(beat: Mapping) -> dict:
    """Copy visual fields without sharing mutable normalized-script objects."""
    return {key: deepcopy(beat[key]) for key in CONTRACT_FIELDS if key in beat}


def build_visual_plan(master: Mapping, shots=()) -> dict:
    """Build an intent plan with per-beat and actual video/edit requirement links.

    Reference choices are authored plans. The text default neither generates
    references nor changes budgets, routes or model reference limits.
    """
    from .creative_ir import normalize_speech_mode
    master_idx = int(master.get("idx") or 0)
    entries, definitions, links = [], [], []
    for index, beat in enumerate(master.get("performance_beats") or [], 1):
        requirements = normalize_requirements(beat.get("visual_requirements", []))
        source_id = str(beat.get("source_beat_id") or beat.get("beat_id") or beat.get("id") or f"master-{master_idx}-beat-{index}")
        mode = normalize_speech_mode(beat.get("speech_mode"), speaker=str(beat.get("speaker") or ""))
        entry = {
            "unit_id": str(beat.get("beat_id") or beat.get("id") or source_id),
            "source_beat_id": source_id, "video_master_idx": master_idx,
            "start": float(beat.get("start") or 0),
            "duration": float(beat.get("duration") or 0),
            "fragment_index": int(beat.get("fragment_index") or 1),
            "fragment_count": int(beat.get("fragment_count") or 1),
            "subject": deepcopy(beat.get("characters") or beat.get("cast") or master.get("cast") or []),
            "action": str(beat.get("visual_intent") or beat.get("action") or ""),
            "camera_intent": str(beat.get("camera_intent") or master.get("camera") or ""),
            "speech_mode": mode,
            "coverage": "visible_speaking_mouth" if mode == "on_screen" and str(beat.get("dialogue") or "").strip() else "action_or_silent_reaction",
            "must_preserve": deepcopy(beat.get("must_preserve_details", beat.get("must_preserve", []))),
            "may_adapt": deepcopy(beat.get("may_adapt_details", beat.get("may_adapt", []))),
            "visual_requirement_ids": [r["id"] for r in requirements],
            "reference_control": deepcopy(beat.get("reference_control") or {
                "choice": "text", "rationale": "No additional reference/control authored; use textual direction"}),
        }
        entries.append(entry)
        definitions.extend(requirements)
        for req in requirements:
            links.append({**deepcopy(req), "source_beat_id": source_id,
                          "unit_id": entry["unit_id"], "fragment_index": entry["fragment_index"], "fragment_count": entry["fragment_count"],
                          "video_master_idx": master_idx, "start": entry["start"], "duration": entry["duration"]})
    for shot in shots:
        if int(shot.get("video_master_idx") or 0) != master_idx:
            continue
        requirements = normalize_requirements(shot.get("visual_requirements", []))
        definitions.extend(requirements)
        for req in requirements:
            links.append({**deepcopy(req), "source_beat_id": str(shot.get("source_beat_id") or shot.get("beat_id") or ""),
                          "unit_id": str(shot.get("beat_id") or ""), "fragment_index": int(shot.get("fragment_index") or 1), "fragment_count": int(shot.get("fragment_count") or 1),
                          "video_master_idx": master_idx, "edit_idx": shot.get("idx"),
                          "video_start": float(shot.get("video_start") or 0), "duration": float(shot.get("duration") or 0)})
    definitions = normalize_requirements(definitions)
    return {"version": 1, "intent_only": True, "requirement_set_hash": requirement_set_hash(definitions),
            "visual_requirements": definitions, "beats": entries, "requirement_links": links}


def attach_visual_plans(storyboard: dict) -> None:
    """Attach plans only to a newly constructed runtime copy, never paid state."""
    storyboard.pop("visual_requirements", None)
    storyboard.pop("requirement_set_hash", None)
    all_requirements = []
    for master in storyboard.get("masters") or []:
        master.pop("visual_plan", None)
        master.pop("visual_requirements", None)
        if not any(any(beat.get(field) for field in ("camera_intent", "must_preserve", "may_adapt", "visual_requirements", "reference_control"))
                   for beat in master.get("performance_beats") or []):
            continue
        plan = build_visual_plan(master, storyboard.get("shots") or [])
        master["visual_plan"] = plan
        master["visual_requirements"] = deepcopy(plan["visual_requirements"])
        all_requirements.extend(plan["visual_requirements"])
    if all_requirements:
        storyboard["visual_requirements"] = normalize_requirements(all_requirements)
        storyboard["requirement_set_hash"] = requirement_set_hash(all_requirements)
