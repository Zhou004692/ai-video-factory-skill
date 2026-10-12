"""Observed content proof, distinct from authored intent and speech timing.

Automatic proof carries blind events and references into them. Human proof is
accepted only when a caller has independently verified its controlled record;
the provenance field alone never grants that authority.
"""
from copy import deepcopy
import hashlib
import math
from pathlib import Path
from .visual_contract import normalize_requirements, requirement_set_hash

MIN_CONFIDENCE = .65

class ContentReviewRequired(RuntimeError):
    stable_code = code = error_code = "content_review_required"
    pause_state = "PRODUCTION_REVIEW_REQUIRED"
    non_retryable = True

    def __init__(self, report):
        self.content_result = self.report = report
        super().__init__("Visual content needs review: " + ", ".join(report["reasons"]))

def asset_fingerprint(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None

def valid_interval(item, duration_sec):
    if not isinstance(item, dict):
        return False
    start, end, duration = (_number(item.get("start_sec")), _number(item.get("end_sec")), _number(duration_sec))
    return (start is not None and end is not None and duration is not None
            and 0 <= start < end <= duration and duration > 0)

def _valid_event(item, duration):
    confidence = _number(item.get("confidence")) if isinstance(item, dict) else None
    return (valid_interval(item, duration) and isinstance(item.get("observed_description"), str)
            and bool(item["observed_description"].strip()) and confidence is not None
            and MIN_CONFIDENCE <= confidence <= 1)

def _event_map(events, duration):
    result, duplicate, seen = {}, set(), set()
    for item in events if isinstance(events, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            continue
        if item["id"] in seen:
            duplicate.add(item["id"])
        seen.add(item["id"])
        if _valid_event(item, duration):
            result[item["id"]] = item
    return {key: value for key, value in result.items() if key not in duplicate}

def bind_observations(requirements, raw, *, asset_sha256, duration_sec):
    """Bind semantic statuses to exact blind observations, never planned times."""
    requirements = normalize_requirements(requirements)
    events = deepcopy(raw.get("observed_visual_events") or [])
    event_map = _event_map(events, duration_sec)
    source = (raw.get("visual_comparison") or {}).get("requirement_evidence") or []
    items = []
    for item in source if isinstance(source, list) else []:
        if not isinstance(item, dict):
            continue
        copy = {"id": item.get("id"), "status": item.get("status"), "event_refs": deepcopy(item.get("event_refs")),
                "provenance": "automatic"}
        refs = item.get("event_refs")
        if isinstance(refs, list) and refs and all(isinstance(ref, str) and ref in event_map for ref in refs):
            # Separate referenced events become separate intervals: no invented
            # envelope is allowed to imply an unobserved action in between.
            if len(refs) == 1:
                event = event_map[refs[0]]
                copy.update({key: event[key] for key in ("start_sec", "end_sec", "observed_description", "confidence")})
        items.append(copy)
    return {"version": 1, "asset_sha256": asset_sha256,
            "requirement_set_hash": requirement_set_hash(requirements), "duration_sec": duration_sec,
            "provenance": "automatic", "observed_visual_events": events, "items": items}

def evaluate(requirements, evidence, *, asset_sha256, duration_sec):
    """Validate automatic evidence. Payload provenance never authorizes review."""
    return _evaluate(requirements, evidence, asset_sha256=asset_sha256, duration_sec=duration_sec, human=False)

def evaluate_human(requirements, evidence, *, asset_sha256, duration_sec):
    """Validate an independently authorized review record at a controlled boundary.

    Callers must establish reviewer authority before calling this API. The
    automatic path never dispatches here based on JSON provenance.
    """
    return _evaluate(requirements, evidence, asset_sha256=asset_sha256, duration_sec=duration_sec, human=True)

def _evaluate(requirements, evidence, *, asset_sha256, duration_sec, human):
    """Fail closed on stale/unknown/malformed proof. Return JSON-safe report.

    A human waiver must contain a nonempty reason.
    """
    requirements = normalize_requirements(requirements)
    wanted_hash = requirement_set_hash(requirements)
    evidence = evidence if isinstance(evidence, dict) else {}
    reasons = []
    if requirements and (not asset_sha256 or evidence.get("asset_sha256") != asset_sha256
                         or evidence.get("requirement_set_hash") != wanted_hash):
        reasons.append("content_stale_evidence")
    provenance = evidence.get("provenance")
    trusted_human = human and provenance == "human"
    if requirements and provenance != "automatic" and not trusted_human:
        reasons.append("content_unverified_provenance")
    event_map = _event_map(evidence.get("observed_visual_events"), duration_sec)
    source = evidence.get("items")
    by_id, invalid = {}, False
    known = {req["id"] for req in requirements}
    for item in source if isinstance(source, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] not in known or item["id"] in by_id:
            invalid = True
            continue
        by_id[item["id"]] = item
    if invalid:
        reasons.append("content_invalid_evidence")
    output = []
    for req in requirements:
        item = deepcopy(by_id.get(req["id"]) or {"id": req["id"]})
        status = item.get("status")
        satisfied = status == "satisfied" and (
            valid_interval(item, duration_sec) and isinstance(item.get("observed_description"), str)
            and bool(item["observed_description"].strip()) if trusted_human else _valid_event(item, duration_sec))
        if satisfied and not trusted_human:
            refs = item.get("event_refs")
            satisfied = (isinstance(refs, list) and len(refs) == 1 and isinstance(refs[0], str)
                         and refs[0] in event_map and all(item.get(key) == event_map[refs[0]].get(key)
                            for key in ("start_sec", "end_sec", "observed_description", "confidence")))
        waived = trusted_human and status == "not_applicable" and isinstance(item.get("reason"), str) and bool(item["reason"].strip())
        if not satisfied and not waived:
            status = "conflicting" if status == "conflicting" else "insufficient_evidence"
            reasons.append("content_conflicting" if status == "conflicting" else "content_insufficient_evidence")
        item["status"] = status
        # Do not persist nonfinite/provider junk into review JSON.
        for key in ("start_sec", "end_sec", "confidence"):
            item[key] = _number(item.get(key))
        output.append(item)
    return {"version": 1, "passed": not reasons, "asset_sha256": asset_sha256,
            "requirement_set_hash": wanted_hash, "items": output, "reasons": list(dict.fromkeys(reasons))}

def retention_report(storyboard, masters, *, verified_human_master_ids=()):
    """Check full observed action intervals against the final picture edits."""
    requirements = normalize_requirements(storyboard.get("visual_requirements") or [])
    definitions = {req["id"]: req for req in requirements}
    report = {"version": 1, "passed": True, "requirement_set_hash": requirement_set_hash(requirements),
              "master_hashes": {}, "kept_intervals": [], "items": [], "reasons": []}
    if not requirements:
        return report
    if storyboard.get("requirement_set_hash", report["requirement_set_hash"]) != report["requirement_set_hash"]:
        report["reasons"].append("content_stale_requirements")
    intervals = {}
    for edit in storyboard.get("shots") or []:
        start, duration = _number(edit.get("video_start", 0)), _number(edit.get("duration"))
        idx = edit.get("video_master_idx")
        master = masters.get(idx, {})
        actual = _number(master.get("duration_sec"))
        if start is None or duration is None or actual is None or not 0 <= start < start + duration <= actual:
            report["reasons"].append("content_invalid_edit_interval")
            continue
        intervals.setdefault(idx, []).append((start, start + duration))
        report["kept_intervals"].append({"video_master_idx": idx, "edit_idx": edit.get("idx"), "start_sec": start, "end_sec": start + duration})
    proved = set()
    for master_plan in storyboard.get("masters") or []:
        reqs = master_plan.get("visual_requirements") or (master_plan.get("visual_plan") or {}).get("visual_requirements") or []
        if not reqs:
            continue
        idx = master_plan["idx"]; master = masters.get(idx, {})
        if any(definitions.get(req["id"]) != req for req in normalize_requirements(reqs)):
            report["reasons"].append("content_stale_requirements")
        validator = evaluate_human if idx in verified_human_master_ids else evaluate
        proof = validator(reqs, master.get("evidence"), asset_sha256=master.get("asset_sha256"),
            duration_sec=master.get("duration_sec"))
        report["master_hashes"][str(idx)] = master.get("asset_sha256")
        report["reasons"].extend(proof["reasons"])
        merged = []
        for start, end in sorted(intervals.get(idx, [])):
            if merged and start <= merged[-1][1] + .001:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        for item in proof["items"]:
            kept = item["status"] == "not_applicable" and proof["passed"]
            if item["status"] == "satisfied" and proof["passed"]:
                kept = any(start <= item["start_sec"] and end >= item["end_sec"] for start, end in merged)
                if not kept:
                    report["reasons"].append("content_event_cropped")
            if kept:
                proved.add(item["id"])
            report["items"].append({**item, "video_master_idx": idx, "retained": kept})
    if {req["id"] for req in requirements} - proved:
        report["reasons"].append("content_insufficient_evidence")
    report["reasons"] = list(dict.fromkeys(report["reasons"]))
    report["passed"] = not report["reasons"]
    return report
