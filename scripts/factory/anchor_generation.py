"""Selectable providers for character and scene continuity anchors.

Seedream produces a static image directly.  Seedance produces a short silent
native clip; the pipeline extracts a stable frame for preview and, for scene
anchors, for the downstream image-reference contract.
"""
from __future__ import annotations

import os

from .clients import seedance, seedream_45


SEEDREAM_5 = "seedream_5_0"
SEEDANCE_25 = "seedance_2_5"
SUPPORTED_GENERATORS = (SEEDREAM_5, SEEDANCE_25)
GENERATOR_OPTIONS = (
    {"value": SEEDREAM_5, "label": "Seedream 5.0 · 静态锚点图"},
    {"value": SEEDANCE_25, "label": "Seedance 2.5 · 动态母片抽帧"},
)

_ALIASES = {
    "seedream": SEEDREAM_5,
    "seedream5": SEEDREAM_5,
    "seedream_5": SEEDREAM_5,
    "seedream_5_0": SEEDREAM_5,
    "seedance": SEEDANCE_25,
    "seedance25": SEEDANCE_25,
    "seedance_2_5": SEEDANCE_25,
}


def normalize_generator(value: object, *, strict: bool = False) -> str:
    text = str(value or "").strip().lower().replace(".", "_").replace("-", "_")
    if not text:
        return SEEDANCE_25
    normalized = _ALIASES.get(text)
    if normalized:
        return normalized
    if strict:
        raise ValueError(
            "锚点生成模型无效，仅支持 seedream_5_0 或 seedance_2_5"
        )
    return SEEDANCE_25


def selected_generator(storyboard: dict, kind: str) -> str:
    settings = storyboard.get("production_settings") or {}
    key = f"{kind}_anchor_generator"
    env_key = f"{kind.upper()}_ANCHOR_GENERATOR"
    return normalize_generator(settings.get(key) or os.getenv(env_key))


def model_id(generator: str, kind: str) -> str:
    if normalize_generator(generator) == SEEDANCE_25:
        return os.getenv(
            "ARK_VIDEO_PRIMARY_MODEL", "doubao-seedance-2-5-260628"
        )
    env_key = (
        "ARK_IDENTITY_IMAGE_MODEL" if kind == "character"
        else "ARK_SCENE_IMAGE_MODEL"
    )
    return os.getenv(env_key, "doubao-seedream-5-0-260128")


def estimated_cost_cents(generator: str, resolution: str = "720p") -> int:
    if normalize_generator(generator) == SEEDANCE_25:
        return seedance.estimate_cost_cents(4, resolution)
    return seedream_45.estimate_cost_cents()


def apply_character_generator(
    plan: list[dict], storyboard: dict, *, resolution: str = "720p"
) -> list[dict]:
    selected = selected_generator(storyboard, "character")
    result: list[dict] = []
    for raw in plan:
        entry = dict(raw)
        if entry.get("source") == "seedream_identity_image":
            entry["generator"] = selected
            entry["model"] = model_id(selected, "character")
            if selected == SEEDANCE_25:
                entry["source"] = "seedance_identity_anchor"
                entry["anchor_mode"] = "dynamic_video"
                entry["duration_sec"] = 4
            else:
                entry["anchor_mode"] = "static_image"
                entry["duration_sec"] = 0
        elif entry.get("source") == "seedance_identity_master":
            # Local custom portraits stay on the privacy-safe native route.
            entry["generator"] = SEEDANCE_25
            entry["model"] = model_id(SEEDANCE_25, "character")
        else:
            entry["generator"] = "volc_asset"
            entry["model"] = ""
        if entry.get("requires_paid_call"):
            entry["estimated_cost_cents"] = estimated_cost_cents(
                entry["generator"], resolution
            )
        result.append(entry)
    return result


def decorate_scene_anchor(
    spec: dict, storyboard: dict, *, resolution: str = "720p"
) -> dict:
    generator = selected_generator(storyboard, "scene")
    return {
        **spec,
        "generator": generator,
        "model": model_id(generator, "scene"),
        "estimated_cost_cents": estimated_cost_cents(generator, resolution),
    }
