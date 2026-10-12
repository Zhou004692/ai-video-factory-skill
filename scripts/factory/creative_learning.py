"""Controlled creative tags and evidence-gated strategy learning."""
from __future__ import annotations

import json
import sqlite3

from . import performance_feedback


VOCABULARY = {
    "hook_type": {
        "pain_point", "contrarian", "question", "result_first",
        "identity_callout", "scene_open", "number_claim",
    },
    "narrative_pattern": {
        "problem_solution", "misconception_correction", "before_after",
        "case_story", "checklist", "comparison", "process_demo",
    },
    "proof_type": {
        "source_quote", "visual_demo", "data_point", "case_evidence",
        "expert_explanation", "none",
    },
    "cta_type": {
        "consult", "comment", "follow", "save", "share", "none",
    },
}


def _controlled(field: str, value: str) -> str:
    value = str(value or "").strip()
    if value not in VOCABULARY[field]:
        raise ValueError(
            f"{field} must be one of {sorted(VOCABULARY[field])}"
        )
    return value


def observed_tag(tags, dimension: str) -> str | None:
    """A missing or invalid immutable tag is unknown for that dimension."""
    value = tags.get(dimension) if isinstance(tags, dict) else None
    return value if isinstance(value, str) and value in VOCABULARY[dimension] else None


def tag_angle(
    db: sqlite3.Connection,
    brief_id: str,
    *,
    angle_index: int,
    hook_type: str,
    narrative_pattern: str,
    proof_type: str,
    cta_type: str,
    notes: str | None = None,
) -> None:
    """Upsert controlled tags for one candidate angle without editing brief JSON."""
    row = db.execute(
        "SELECT brief_json FROM topic_briefs WHERE brief_id=?", (brief_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"topic brief not found: {brief_id}")
    brief = json.loads(row["brief_json"])
    if int(angle_index) not in range(len(brief.get("angles") or [])):
        raise ValueError("angle_index must select one of the brief angles")
    values = {
        "hook_type": hook_type, "narrative_pattern": narrative_pattern,
        "proof_type": proof_type, "cta_type": cta_type,
    }
    normalized = {
        field: _controlled(field, value) for field, value in values.items()
    }
    db.execute(
        "INSERT INTO creative_angle_tags(brief_id, angle_index, hook_type, "
        "narrative_pattern, proof_type, cta_type, notes) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(brief_id, angle_index) DO UPDATE SET "
        "hook_type=excluded.hook_type, "
        "narrative_pattern=excluded.narrative_pattern, "
        "proof_type=excluded.proof_type, cta_type=excluded.cta_type, "
        "notes=excluded.notes, updated_at=CURRENT_TIMESTAMP",
        (
            brief_id, int(angle_index), normalized["hook_type"],
            normalized["narrative_pattern"], normalized["proof_type"],
            normalized["cta_type"], str(notes).strip() if notes else None,
        ),
    )
    db.commit()


def _aggregate_creatives(creatives: list[dict]) -> dict:
    views = sum(item['views'] for item in creatives if item.get('views') is not None)
    result = {'sample_count':len(creatives), 'views':views, 'coverage':{}}
    for metric in ('completion_rate','engagement_rate','follow_rate','lead_rate'):
        eligible = [item for item in creatives if item.get(metric) is not None and item.get('views') is not None]
        weights = [(item, (item.get('coverage') or {}).get(metric,{}).get('views',item['views'])) for item in eligible]
        denominator = sum(weight for _,weight in weights)
        result[metric] = sum(item[metric]*weight for item,weight in weights)/denominator if denominator else None
        result['coverage'][metric] = {'samples':len(eligible), 'views':denominator}
    return result


def strategy_report(
    db: sqlite3.Connection,
    *,
    platform: str,
    window_hours: float,
    min_views: int = 100,
    min_samples: int = 2,
    min_window_coverage: float = 0.8,
) -> dict:
    """Aggregate controlled creative variables from comparable creatives."""
    if min_samples < 1:
        raise ValueError("min_samples must be >= 1")
    base = performance_feedback.learning_report(
        db, platform=platform, window_hours=window_hours,
        min_views=min_views, min_window_coverage=min_window_coverage,
    )
    untagged = [creative["job_id"] for creative in base["creatives"]
                if not any(observed_tag(creative.get("tags"), dimension)
                           for dimension in VOCABULARY)]

    dimensions: list[dict] = []
    for dimension in VOCABULARY:
        groups: dict[str, list[dict]] = {}
        unknown: list[dict] = []
        for creative in base["creatives"]:
            value = observed_tag(creative.get("tags"), dimension)
            if value is None:
                unknown.append({"job_id": creative["job_id"],
                                "publication_id": creative.get("publication_id"),
                                "reason": "creative_tag_unknown"})
            else:
                groups.setdefault(value, []).append(creative)
        values = [
            {"value": value, **_aggregate_creatives(creatives)}
            for value, creatives in groups.items()
        ]
        values.sort(key=lambda item: (-item["sample_count"], item["value"]))
        eligible = [item for item in values if item["sample_count"] >= min_samples and item["coverage"]["completion_rate"]["samples"] >= min_samples]
        insufficient = [item for item in values if item["sample_count"] < min_samples or item["coverage"]["completion_rate"]["samples"] < min_samples]
        eligible.sort(
            key=lambda item: (-item["completion_rate"], -item["views"], item["value"])
        )
        recommendation = (
            f"candidate: {eligible[0]['value']}; validate with another cohort"
            if eligible else "insufficient samples; do not recommend"
        )
        dimensions.append({
            "dimension": dimension, "eligible": eligible,
            "insufficient": insufficient, "recommendation": recommendation,
            "unknown": unknown,
            "coverage": {"known_samples": sum(len(rows) for rows in groups.values()),
                         "unknown_samples": len(unknown)},
        })
    return {
        "platform": base["platform"], "window_hours": base["window_hours"],
        "min_views": min_views, "min_samples": min_samples,
        "recommendation_policy": (
            "Candidates are descriptive, not causal or auto-applied."
        ),
        "dimensions": dimensions, "untagged": sorted(untagged),
        "excluded": base["excluded"],
    }
