"""Side-effect-free cost planning for native-master productions."""
from __future__ import annotations

import json
import os
import sqlite3

from . import asset_library
from .clients import seedance


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

    library_hits: list[dict] = []
    paid_masters: list[dict] = []
    warnings: list[str] = []

    for master in masters:
        idx = int(master.get("idx") or 0)
        duration = float(master.get("duration") or 0)
        master_type = master.get("type") or "unknown"
        if master_type == "broll":
            tags = list(master.get("asset_tags") or [])
            if not tags:
                warnings.append(
                    f"master {idx}: B-roll lacks asset_tags; it will require a paid generation"
                )
            match = asset_library.find_broll_match(
                db,
                tags=tags,
                min_duration_sec=duration,
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

        paid_masters.append(
            {
                "master_idx": idx,
                "type": master_type,
                "duration_sec": duration,
                "estimated_cost_cents": seedance.estimate_cost_cents(
                    duration, resolved_resolution
                ),
            }
        )

    estimated_cost = sum(item["estimated_cost_cents"] for item in paid_masters)
    paid_duration = sum(item["duration_sec"] for item in paid_masters)
    reasons = [
        f"{len(library_hits)} B-roll master(s) will reuse approved local assets"
    ] if library_hits else []
    if len(masters) == 1:
        reasons.append("storyboard already uses the minimum one-master shape")

    return {
        "total_masters": len(masters),
        "paid_master_count": len(paid_masters),
        "estimated_paid_calls": len(paid_masters),
        "paid_duration_sec": paid_duration,
        "estimated_cost_cents": estimated_cost,
        "budget_cents": resolved_budget,
        "budget_ok": estimated_cost <= resolved_budget,
        "resolution": resolved_resolution,
        "library_hits": library_hits,
        "paid_masters": paid_masters,
        "warnings": warnings,
        "reduction_reasons": reasons,
    }
