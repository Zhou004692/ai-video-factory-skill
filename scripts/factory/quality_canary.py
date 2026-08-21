"""Immutable evidence contract for authorized real-provider quality canaries."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import SKILL_ROOT, production_config


PROFILE_PATH = SKILL_ROOT / "references" / "quality_canary_scripts.json"
MANUAL_SCORE_FIELDS = (
    "character_consistency",
    "scene_continuity",
    "staging",
    "performance",
    "lip_sync",
    "subtitle_sync",
    "readable_text",
    "editing_rhythm",
)


def load_frozen_profiles(path: Path = PROFILE_PATH) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    profiles = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(profiles, list) or len(profiles) != 3:
        raise ValueError("quality canary contract must contain exactly three profiles")
    return deepcopy(profiles)


def _canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _profile(profile_id: str) -> dict[str, Any]:
    for profile in load_frozen_profiles():
        if str(profile.get("id") or "") == profile_id:
            return profile
    raise ValueError(f"unknown quality canary profile: {profile_id}")


def prepare_run_contract(profile_id: str) -> dict[str, Any]:
    """Return the immutable snapshot fields that must be persisted pre-submit."""
    profile = _profile(profile_id)
    return {
        "quality_canary_profile_id": profile_id,
        "quality_canary_contract_sha256": _canonical_digest(profile),
        "quality_canary_frozen_input": deepcopy(profile),
        "job_budget_cents": int(profile["budget_cents"]),
        "max_auto_retries": int(profile["max_auto_retries"]),
    }


def prepare_explicit_run(
    profile_id: str,
    *,
    confirmed: bool,
    budget_cents: int,
) -> dict[str, Any]:
    """Prepare a real-provider canary only after explicit, independent consent."""
    if not confirmed:
        raise ValueError("real-provider canary requires explicit confirmation")
    contract = prepare_run_contract(profile_id)
    confirmed_budget = int(budget_cents)
    if confirmed_budget <= 0:
        raise ValueError("real-provider canary requires a positive independent budget")
    if confirmed_budget < int(contract["job_budget_cents"]):
        raise ValueError("confirmed canary budget is below the frozen profile budget")
    return {
        **contract,
        "confirmed_budget_cents": confirmed_budget,
        "launch_mode": "manual_confirmed",
        "scheduler_allowed": False,
        "uses_production_real_assets": False,
    }


def build_isolated_storyboard(profile_id: str, contract: Mapping[str, Any]) -> dict[str, Any]:
    """Build a fixed fictional storyboard without reading production identities."""
    profile = _profile(profile_id)
    characters = []
    character_id_by_name: dict[str, str] = {}
    for index, name in enumerate(profile["characters"], start=1):
        character_id = f"canary_character_{index}"
        character_id_by_name[str(name)] = character_id
        characters.append({
            "id": character_id,
            "name": str(name),
            "gender": "unspecified",
            "identity_type": "custom",
            "generation_mode": "ai_generated",
            "look": (
                "虚构成年服务团队成员，形象自然可信；仅用于隔离质量验收，"
                "不得读取或匹配生产角色资产"
            ),
        })
    timeline = list(profile.get("script") or [])
    if not timeline:
        raise ValueError("quality canary profile script is empty")
    dialogue_blocks = []
    edits = []
    voiceover_parts = []
    for index, beat in enumerate(timeline, start=1):
        raw_range = str(beat.get("time") or "").split("-", 1)
        if len(raw_range) != 2:
            raise ValueError("quality canary profile time range is invalid")
        start, end = (float(part) for part in raw_range)
        duration = end - start
        if duration <= 0:
            raise ValueError("quality canary profile time range is invalid")
        speaker = character_id_by_name.get(str(beat.get("speaker") or ""))
        if not speaker:
            raise ValueError("quality canary profile references an unknown speaker")
        dialogue = str(beat.get("dialogue") or "").strip()
        dialogue_blocks.append({"speaker": speaker, "text": dialogue})
        voiceover_parts.append(dialogue)
        edits.append({
            "idx": index,
            "video_master_idx": 1,
            "audio_master_idx": 1,
            "video_start": start,
            "audio_start": start,
            "duration": duration,
            "subtitle": dialogue,
        })
    primary = dialogue_blocks[0]["speaker"]
    production_settings = {
        "budget_cents": int(contract["confirmed_budget_cents"]),
        "max_auto_retries": int(contract["max_auto_retries"]),
        "auto_retry_budget_cents": 0,
        "max_concurrent_masters": 1,
        "audio_mode": "seedance_native",
    }
    generation_snapshot = production_config.freeze_generation_snapshot(
        production_settings
    )
    generation_snapshot.update({
        "quality_canary_profile_id": profile_id,
        "quality_canary_frozen_input": deepcopy(
            contract["quality_canary_frozen_input"]
        ),
        "quality_canary_contract_sha256": str(
            contract["quality_canary_contract_sha256"]
        ),
    })
    production_settings[production_config.GENERATION_SNAPSHOT_KEY] = (
        generation_snapshot
    )
    return {
        "production_profile": "standard",
        "production_mode": "native_master_local_director",
        "quality_canary": deepcopy(dict(contract)),
        "production_settings": production_settings,
        "characters": characters,
        "hook": "固定短剧本真实 Provider 质量金丝雀",
        "masters": [{
            "idx": 1,
            "type": "dialogue",
            "speaker": primary,
            "speakers": [character["id"] for character in characters],
            "render_mode": "cinematic_character",
            "duration": float(profile["duration_sec"]),
            "scene": str(profile["scene"]),
            "action": "；".join(str(beat.get("performance") or "") for beat in timeline),
            "voiceover": " ".join(voiceover_parts),
            "dialogue_blocks": dialogue_blocks,
        }],
        "shots": edits,
        "bgm": {"mood": "restrained", "prompt": "克制、轻量、不覆盖对白"},
    }


def evaluate_release_gate(
    daily_metrics: Iterable[Mapping[str, Any]],
    *,
    required_days: int = 7,
    minimum_first_round_rate: float = 0.95,
) -> dict[str, Any]:
    """Evaluate the safety gate from consecutive, structured daily summaries."""
    normalized = sorted(
        (
            dict(row) for row in daily_metrics
            if "created_jobs" not in row or int(row.get("created_jobs") or 0) > 0
        ),
        key=lambda row: str(row.get("date") or ""),
    )
    recent = normalized[-required_days:]
    reasons: list[str] = []
    if any(int(row.get("duplicate_paid_submissions") or 0) > 0 for row in recent):
        reasons.append("duplicate_paid_submission")
    if any(int(row.get("unknown_stuck_jobs") or 0) > 0 for row in recent):
        reasons.append("unknown_stuck_job")
    if any(
        int(row.get("deterministic_post_paid_failures") or 0) > 0
        for row in recent
    ):
        reasons.append("deterministic_post_paid_failure")
    if reasons:
        return {
            "status": "freeze", "reasons": reasons,
            "observed_days": len(recent), "required_days": required_days,
        }
    parsed_dates: list[date] = []
    for row in recent:
        try:
            parsed_dates.append(date.fromisoformat(str(row.get("date") or "")))
        except ValueError:
            parsed_dates = []
            break
    consecutive = len(parsed_dates) == required_days and all(
        current - previous == timedelta(days=1)
        for previous, current in zip(parsed_dates, parsed_dates[1:])
    )
    if not consecutive:
        return {
            "status": "insufficient_data", "reasons": ["seven_consecutive_days_required"],
            "observed_days": len(recent), "required_days": required_days,
        }
    below_target = [
        str(row.get("date") or "") for row in recent
        if float(row.get("first_round_progression_rate") or 0) < minimum_first_round_rate
    ]
    if below_target:
        return {
            "status": "freeze", "reasons": ["first_round_progression_below_target"],
            "below_target_dates": below_target,
            "observed_days": len(recent), "required_days": required_days,
        }
    return {
        "status": "pass", "reasons": [],
        "observed_days": len(recent), "required_days": required_days,
    }


def _usage(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _portable_path(value: object, root: Path) -> str | None:
    if not value:
        return None
    try:
        path = Path(str(value)).resolve()
        return path.relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _validated_scores(scores: Mapping[str, object] | None) -> dict[str, float] | None:
    if scores is None:
        return None
    missing = [key for key in MANUAL_SCORE_FIELDS if key not in scores]
    if missing:
        raise ValueError(f"manual scores missing: {', '.join(missing)}")
    result: dict[str, float] = {}
    for key in MANUAL_SCORE_FIELDS:
        score = float(scores[key])
        if not 0 <= score <= 5:
            raise ValueError(f"manual score {key} must be between 0 and 5")
        result[key] = score
    return result


def _matching_profile(
    job: Mapping[str, Any], requested_profile_id: str | None = None
) -> str | None:
    settings = job.get("production_settings") or {}
    snapshot = settings.get("generation_snapshot") or {}
    claimed = str(snapshot.get("quality_canary_profile_id") or "").strip()
    if requested_profile_id and claimed != requested_profile_id:
        return None
    profile_id = requested_profile_id or claimed
    if not profile_id:
        return None
    try:
        profile = _profile(profile_id)
    except ValueError:
        return None
    frozen = snapshot.get("quality_canary_frozen_input")
    signature = str(snapshot.get("quality_canary_contract_sha256") or "")
    if not isinstance(frozen, Mapping):
        return None
    if _canonical_digest(frozen) != _canonical_digest(profile):
        return None
    if signature != _canonical_digest(profile):
        return None
    duration = float(job.get("total_master_duration_sec") or 0)
    character_names = [
        str(item.get("script_character_name") or item.get("name") or "").strip()
        for item in (job.get("characters") or [])
        if isinstance(item, Mapping)
    ]
    if abs(duration - float(profile["duration_sec"])) > 0.25:
        return None
    if character_names != list(profile["characters"]):
        return None
    return profile_id


def build_acceptance_record(
    job: Mapping[str, Any],
    provider_calls: Iterable[Mapping[str, Any]],
    *,
    root: Path,
    run_id: str,
    profile_id: str | None = None,
    manual_scores: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Build a portable record without signed output URLs or invented scores."""
    scores = _validated_scores(manual_scores)
    matched = _matching_profile(job, profile_id)
    calls = []
    for call in provider_calls:
        usage = _usage(call.get("usage_json"))
        calls.append({
            "task_id": str(call.get("task_id") or ""),
            "shot_idx": int(call.get("shot_idx") or 0),
            "provider": str(call.get("provider") or ""),
            "intent": str(call.get("intent") or ""),
            "cost_cents": int(call.get("cost_cents") or 0),
            "status": str(usage.get("status") or ""),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "reference_manifest": deepcopy(usage.get("reference_manifest") or []),
        })
    findings = []
    for shot in job.get("shots") or []:
        reasons = list(shot.get("quality_reasons") or [])
        decision = str(shot.get("human_decision") or "")
        if reasons or decision:
            findings.append({
                "master_idx": int(shot.get("idx") or 0),
                "state": str(shot.get("state") or ""),
                "quality_status": str(shot.get("quality_status") or ""),
                "quality_reasons": reasons,
                "human_decision": decision,
            })
    settings = deepcopy(job.get("production_settings") or {})
    snapshot = deepcopy(settings.get("generation_snapshot") or {})
    return {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "classification": "canary" if matched else "observational",
        "profile_id": matched,
        "completion_status": "complete" if scores is not None else "evidence_only",
        "job": {
            "job_id": str(job.get("job_id") or ""),
            "title": str(job.get("title") or ""),
            "state": str(job.get("state") or ""),
            "duration_sec": float(job.get("total_master_duration_sec") or 0),
            "final_artifact": _portable_path(job.get("final_path"), root),
        },
        "frozen_input": {
            "storyboard": deepcopy(job.get("storyboard") or {}),
            "characters": deepcopy(job.get("characters") or []),
            "character_anchors": deepcopy(job.get("character_makeup_plan") or []),
            "scene_anchors": deepcopy(job.get("scene_anchor_plan") or []),
            "orientation": settings.get("orientation"),
            "audio_mode": settings.get("audio_mode"),
            "generation_snapshot": snapshot,
        },
        "provider_tasks": calls,
        "cost": {
            "actual_cents": int(job.get("cost_cents") or 0),
            "tracked_provider_cents": sum(item["cost_cents"] for item in calls),
            "frozen_budget_cents": int(snapshot.get("job_budget_cents") or 0),
            "max_auto_retries": int(snapshot.get("max_auto_retries") or 0),
        },
        "quality": {
            "final_review": deepcopy(job.get("latest_review")),
            "shot_findings": findings,
            "manual_scores": scores,
        },
    }


def write_acceptance_record(root: Path, record: Mapping[str, Any]) -> Path:
    run_id = str(record.get("run_id") or "").strip()
    if not run_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in run_id):
        raise ValueError("run_id must contain only letters, numbers, dash or underscore")
    target = root / "data" / "acceptance" / run_id / "acceptance.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(dict(record), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target
