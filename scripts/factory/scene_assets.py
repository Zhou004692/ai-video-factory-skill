"""Deterministic planning helpers for reusable scene consistency images."""
from __future__ import annotations

import hashlib
import re

from . import character_assets


_ENVIRONMENT = re.compile(
    r"(?:场景|地点)\s*[:：]\s*(.*?)"
    r"(?=(?:[；;]\s*(?:出镜人物|人物动作|景别与运镜|画面文字))|$)",
    re.DOTALL,
)


def _environment_description(master: dict) -> str:
    text = str(master.get("scene") or master.get("prompt") or "").strip()
    match = _ENVIRONMENT.search(text)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip(" ，,；;")


def _location_signature(description: str) -> str:
    location = re.split(r"[，,；;。\n]", description, maxsplit=1)[0]
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", location).casefold()


def _screen_positions(count: int) -> list[str]:
    presets = {
        1: ["screen-center"],
        2: ["screen-left", "screen-right"],
        3: ["screen-left", "screen-center", "screen-right"],
    }
    if count in presets:
        return presets[count]
    return [f"screen-slot-{index + 1}-of-{count}" for index in range(count)]


def _normalized_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def apply_continuity_metadata(
    storyboard: dict,
    characters: list[dict] | None = None,
) -> bool:
    """Materialize safe repeated-location and blocking metadata.

    Only source-style scene descriptions with an explicit ``场景：`` label
    participate.  This avoids guessing from generic prose while allowing the
    production-settings step to collapse script aliases that were mapped to
    the same fixed Volc identity.
    """
    masters = list(storyboard.get("masters") or [])
    runtime_characters = list(characters or storyboard.get("characters") or [])
    identity_groups: dict[str, dict] = {}
    alias_to_identity: dict[str, str] = {}
    for character in runtime_characters:
        identity_key = character_assets.character_identity_key(character)
        group = identity_groups.setdefault(identity_key, {
            "identity_key": identity_key,
            "display_name": str(
                character.get("name") or character.get("id") or identity_key
            ),
            "character_ids": [],
            "wardrobe": str(
                character.get("wardrobe") or character.get("costume")
                or character.get("look") or ""
            ).strip(),
        })
        if not group["wardrobe"]:
            group["wardrobe"] = str(
                character.get("wardrobe") or character.get("costume")
                or character.get("look") or ""
            ).strip()
        for alias in (
            character.get("id"), character.get("name"),
            character.get("script_character_name"),
        ):
            normalized = str(alias or "").strip()
            if not normalized:
                continue
            alias_to_identity[normalized.casefold()] = identity_key
            if normalized not in group["character_ids"]:
                group["character_ids"].append(normalized)

    candidates: dict[str, list[tuple[dict, str]]] = {}
    for master in masters:
        description = _environment_description(master)
        explicit_key = str(master.get("scene_anchor_key") or "").strip().casefold()
        signature = _location_signature(description)
        if explicit_key:
            group_key = f"explicit:{explicit_key}"
            description = str(
                master.get("scene_anchor_description") or description
                or master.get("scene") or ""
            ).strip()
        elif len(signature) >= 4:
            group_key = f"inferred:{signature}"
        else:
            group_key = f"single:{int(master.get('idx') or 0)}"
            description = str(master.get("scene") or "").strip()
        candidates.setdefault(group_key, []).append((master, description))

    changed = False
    for group_key, entries in candidates.items():
        if group_key.startswith("explicit:"):
            key = group_key.split(":", 1)[1]
        elif group_key.startswith("inferred:") and len(entries) >= 2:
            signature = group_key.split(":", 1)[1]
            key = f"scene-{hashlib.sha256(signature.encode('utf-8')).hexdigest()[:12]}"
        else:
            key = f"scene-shot-{int(entries[0][0].get('idx') or 0)}"
        description = entries[0][1]
        present_identities: list[str] = []
        for master, _ in entries:
            text = " ".join(
                str(master.get(field) or "")
                for field in ("speaker", "scene", "action")
            ).casefold()
            for alias, identity_key in alias_to_identity.items():
                if alias in text and identity_key not in present_identities:
                    present_identities.append(identity_key)
        positions = []
        for identity_key, screen_position in zip(
            present_identities, _screen_positions(len(present_identities))
        ):
            group = identity_groups[identity_key]
            positions.append({
                "identity_key": identity_key,
                "display_name": group["display_name"],
                "character_ids": list(group["character_ids"]),
                "screen_position": screen_position,
            })
        blocking = {
            "axis": "preserve-180-degree-axis",
            "positions": positions,
        }
        for master, _ in entries:
            text = " ".join(
                str(master.get(field) or "")
                for field in ("speaker", "scene", "action")
            ).casefold()
            present = [
                item for item in positions
                if any(
                    str(alias).casefold() in text
                    for alias in item.get("character_ids") or []
                )
            ] or positions
            contract = {
                "version": 1,
                "scene": {
                    "key": key,
                    "description": description,
                    "location_signature": _location_signature(description),
                    "time_of_day": str(master.get("time_of_day") or "").strip(),
                    "lighting": str(master.get("lighting") or "").strip(),
                },
                "characters": [
                    {
                        "identity_key": item["identity_key"],
                        "display_name": item["display_name"],
                        "wardrobe": identity_groups[item["identity_key"]]["wardrobe"],
                        "screen_position": item["screen_position"],
                    }
                    for item in present
                ],
                "props": _normalized_list(
                    master.get("props") or master.get("held_props") or []
                ),
                "camera": {
                    "axis": blocking["axis"],
                    "shot_type": str(master.get("shot_type") or "").strip(),
                    "movement": str(master.get("camera") or "").strip(),
                },
            }
            for field, value in (
                ("scene_anchor_key", key),
                ("scene_anchor_description", description),
                ("blocking_anchor", blocking),
                ("continuity_contract", contract),
            ):
                if master.get(field) != value:
                    master[field] = value
                    changed = True
    return changed


def recurring_scene_anchors(storyboard: dict) -> list[dict]:
    """Return explicit scene keys used by at least two different masters.

    The storyboard model assigns ``scene_anchor_key``.  We deliberately do
    not guess from prose: false matches lock unrelated locations into the same
    composition and waste a paid image call.
    """
    grouped: dict[str, dict] = {}
    for master in storyboard.get("masters") or []:
        key = str(master.get("scene_anchor_key") or "").strip().casefold()
        idx = int(master.get("idx") or 0)
        if not key or not idx:
            continue
        item = grouped.setdefault(key, {
            "anchor_key": key,
            "description": str(
                master.get("scene_anchor_description")
                or master.get("scene") or ""
            ).strip(),
            "master_indexes": [],
            "blocking_anchor": master.get("blocking_anchor") or {},
        })
        if idx not in item["master_indexes"]:
            item["master_indexes"].append(idx)
        if not item["description"] and master.get("scene"):
            item["description"] = str(master["scene"]).strip()
    return [
        item for item in grouped.values()
        if len(item["master_indexes"]) >= 2
    ]


def anchor_token(anchor_key: str) -> str:
    readable = re.sub(r"[^a-z0-9]+", "-", anchor_key.casefold()).strip("-")
    digest = hashlib.sha256(anchor_key.encode("utf-8")).hexdigest()[:12]
    return f"{readable[:28]}-{digest}" if readable else digest


def scene_anchor_prompt(spec: dict) -> str:
    return "\n".join([
        "SCENE CONTINUITY / STATIC LOCATION REFERENCE.",
        f"Location key: {spec['anchor_key']}.",
        f"Locked environment: {spec['description']}",
        "Photorealistic vertical wide establishing frame of the empty location.",
        "Lock architecture, equipment placement, materials, color palette, "
        "weather, time of day and practical lighting.",
        "No people, no faces, no text, no logo, no watermark, no collage.",
        "This image is a spatial continuity reference, not a finished shot.",
    ])
