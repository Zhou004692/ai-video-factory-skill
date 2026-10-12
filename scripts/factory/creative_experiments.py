"""Pre-registered, single-variable creative experiments."""
from __future__ import annotations

import hashlib
import json
import sqlite3

from . import creative_learning, performance_feedback


PRIMARY_METRICS = {
    "completion_rate", "engagement_rate", "follow_rate", "lead_rate",
}
ARMS = {"control", "treatment"}


def _required(name: str, value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def create_experiment(
    db: sqlite3.Connection,
    *,
    name: str,
    platform: str,
    window_hours: float,
    variable_dimension: str,
    control_value: str,
    treatment_value: str,
    primary_metric: str,
    hypothesis: str,
    audience: str,
    production_profile: str,
    min_views: int = 100,
    min_jobs_per_arm: int = 2,
    planned_jobs_per_arm: int = 2,
    minimum_effect: float = 0.0,
    min_window_coverage: float = 0.8,
) -> str:
    """Create an immutable experiment contract; identical contracts are idempotent."""
    name = _required("name", name)
    platform = performance_feedback._platform(platform)
    variable_dimension = str(variable_dimension or "").strip()
    if variable_dimension not in creative_learning.VOCABULARY:
        raise ValueError(
            "variable_dimension must be one of "
            f"{sorted(creative_learning.VOCABULARY)}"
        )
    control_value = creative_learning._controlled(
        variable_dimension, control_value,
    )
    treatment_value = creative_learning._controlled(
        variable_dimension, treatment_value,
    )
    if control_value == treatment_value:
        raise ValueError("control_value and treatment_value must be different")
    primary_metric = str(primary_metric or "").strip()
    if primary_metric not in PRIMARY_METRICS:
        raise ValueError(
            f"primary_metric must be one of {sorted(PRIMARY_METRICS)}"
        )
    window_hours = float(window_hours)
    min_window_coverage = float(min_window_coverage)
    min_views = int(min_views)
    min_jobs_per_arm = int(min_jobs_per_arm)
    planned_jobs_per_arm = int(planned_jobs_per_arm)
    minimum_effect = float(minimum_effect)
    if window_hours <= 0:
        raise ValueError("window_hours must be > 0")
    if not 0 < min_window_coverage <= 1:
        raise ValueError("min_window_coverage must be between 0 and 1")
    if min_views < 0:
        raise ValueError("min_views must be >= 0")
    if min_jobs_per_arm < 1:
        raise ValueError("min_jobs_per_arm must be >= 1")
    if planned_jobs_per_arm < min_jobs_per_arm:
        raise ValueError("planned_jobs_per_arm must be >= min_jobs_per_arm")
    if minimum_effect < 0:
        raise ValueError("minimum_effect must be >= 0")
    contract = {
        "name": name, "platform": platform, "window_hours": window_hours,
        "min_window_coverage": min_window_coverage,
        "variable_dimension": variable_dimension,
        "control_value": control_value,
        "treatment_value": treatment_value,
        "primary_metric": primary_metric,
        "hypothesis": _required("hypothesis", hypothesis),
        "audience": _required("audience", audience),
        "production_profile": _required(
            "production_profile", production_profile,
        ),
        "min_views": min_views, "min_jobs_per_arm": min_jobs_per_arm,
        "planned_jobs_per_arm": planned_jobs_per_arm,
        "minimum_effect": minimum_effect,
    }
    identity = json.dumps(
        contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    experiment_id = "EXP-" + hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:16]
    db.execute(
        "INSERT OR IGNORE INTO creative_experiments("
        "experiment_id,name,platform,window_hours,min_window_coverage,"
        "variable_dimension,control_value,treatment_value,primary_metric,"
        "hypothesis,audience,production_profile,min_views,min_jobs_per_arm,"
        "planned_jobs_per_arm,minimum_effect) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (experiment_id, *contract.values()),
    )
    db.commit()
    return experiment_id


def _job_context(
    db: sqlite3.Connection, job_id: str, platform: str,
) -> tuple[sqlite3.Row, dict, dict]:
    row = db.execute(
        "SELECT j.target_audience,j.storyboard,b.brief_id,"
        "b.approved_angle_index FROM jobs j "
        "JOIN topic_briefs b ON b.approved_job_id=j.job_id "
        "WHERE j.job_id=? AND b.status='approved'",
        (job_id,),
    ).fetchone()
    if not row:
        raise ValueError(
            f"job {job_id} needs an approved brief"
        )
    tag = db.execute(
        "SELECT hook_type,narrative_pattern,proof_type,cta_type "
        "FROM creative_angle_tags WHERE brief_id=? AND angle_index=?",
        (row["brief_id"], row["approved_angle_index"]),
    ).fetchone()
    if not tag:
        raise ValueError(f"job {job_id} selected angle needs creative tags")
    try:
        storyboard = json.loads(row["storyboard"] or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"job {job_id} storyboard is invalid JSON") from exc
    return row, dict(tag), storyboard


def _slot_payload(row: sqlite3.Row, experiment: sqlite3.Row) -> dict:
    value = (
        experiment["control_value"] if row["arm"] == "control"
        else experiment["treatment_value"]
    )
    return {
        "slot_id": row["slot_id"], "experiment_id": row["experiment_id"],
        "subject_key": row["subject_key"], "arm": row["arm"],
        "creative_value": value,
        "allocation_method": row["allocation_method"],
        "allocation_hash": row["allocation_hash"],
        "bound_job_id": row["bound_job_id"],
    }


def allocate_slot(
    db: sqlite3.Connection, experiment_id: str, *, subject_key: str,
) -> dict:
    """Allocate an idempotent, balanced arm before creative production."""
    subject_key = _required("subject_key", subject_key)
    experiment = db.execute(
        "SELECT * FROM creative_experiments WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone()
    if not experiment:
        raise ValueError(f"creative experiment not found: {experiment_id}")
    existing = db.execute(
        "SELECT * FROM creative_experiment_slots WHERE experiment_id=? "
        "AND subject_key=?", (experiment_id, subject_key),
    ).fetchone()
    if existing:
        return _slot_payload(existing, experiment)
    counts = {"control": 0, "treatment": 0}
    for row in db.execute(
        "SELECT arm,COUNT(*) AS count FROM creative_experiment_slots "
        "WHERE experiment_id=? GROUP BY arm", (experiment_id,),
    ).fetchall():
        counts[row["arm"]] = int(row["count"])
    quota = int(experiment["planned_jobs_per_arm"])
    if counts["control"] >= quota and counts["treatment"] >= quota:
        raise ValueError("experiment has reached its planned slot quota")
    seed = hashlib.sha256(
        f"{experiment_id}\0{subject_key}".encode("utf-8")
    ).hexdigest()
    if counts["control"] >= quota:
        arm = "treatment"
    elif counts["treatment"] >= quota:
        arm = "control"
    elif counts["control"] < counts["treatment"]:
        arm = "control"
    elif counts["treatment"] < counts["control"]:
        arm = "treatment"
    else:
        arm = "control" if int(seed[-1], 16) % 2 == 0 else "treatment"
    allocation_hash = hashlib.sha256(
        f"{experiment_id}\0{subject_key}\0{arm}\0{seed}".encode("utf-8")
    ).hexdigest()
    slot_id = "SLOT-" + hashlib.sha256(
        f"{experiment_id}\0{subject_key}".encode("utf-8")
    ).hexdigest()[:16]
    db.execute(
        "INSERT INTO creative_experiment_slots(slot_id,experiment_id,"
        "subject_key,arm,allocation_hash) VALUES(?,?,?,?,?)",
        (slot_id, experiment_id, subject_key, arm, allocation_hash),
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM creative_experiment_slots WHERE slot_id=?", (slot_id,),
    ).fetchone()
    return _slot_payload(row, experiment)


def _assign_job(
    db: sqlite3.Connection, experiment_id: str, job_id: str, *, arm: str,
    assignment_method: str, allocation_token: str | None,
) -> None:
    """Shared assignment validation for manual and system-issued slots."""
    arm = str(arm or "").strip()
    if arm not in ARMS:
        raise ValueError("arm must be control or treatment")
    experiment = db.execute(
        "SELECT * FROM creative_experiments WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone()
    if not experiment:
        raise ValueError(f"creative experiment not found: {experiment_id}")
    existing = db.execute(
        "SELECT arm,assignment_method,allocation_token "
        "FROM creative_experiment_jobs WHERE experiment_id=? AND job_id=?",
        (experiment_id, job_id),
    ).fetchone()
    if existing:
        if (
            existing["arm"] != arm
            or existing["assignment_method"] != assignment_method
            or existing["allocation_token"] != allocation_token
        ):
            raise ValueError(f"job {job_id} already has a different assignment")
        return
    arm_count = db.execute(
        "SELECT COUNT(*) FROM creative_experiment_jobs "
        "WHERE experiment_id=? AND arm=?", (experiment_id, arm),
    ).fetchone()[0]
    if int(arm_count) >= int(experiment["planned_jobs_per_arm"]):
        raise ValueError(f"{arm} has reached its planned arm quota")
    has_result = db.execute(
        "SELECT 1 FROM performance_snapshots WHERE job_id=? AND platform=? "
        "LIMIT 1", (job_id, experiment["platform"]),
    ).fetchone()
    if has_result:
        raise ValueError("experiment assignment must happen before performance results")
    row, tags, storyboard = _job_context(db, job_id, experiment["platform"])
    if str(row["target_audience"] or "").strip() != experiment["audience"]:
        raise ValueError(f"job {job_id} audience does not match experiment audience")
    if storyboard.get("production_profile") != experiment["production_profile"]:
        raise ValueError(f"job {job_id} production_profile does not match experiment")
    expected = (
        experiment["control_value"] if arm == "control"
        else experiment["treatment_value"]
    )
    dimension = experiment["variable_dimension"]
    if tags[dimension] != expected:
        raise ValueError(f"{arm} expects {dimension}={expected}; got {tags[dimension]}")
    fixed_tags = {key: value for key, value in tags.items() if key != dimension}
    if experiment["fixed_tags_json"]:
        if fixed_tags != json.loads(experiment["fixed_tags_json"]):
            raise ValueError(f"job {job_id} fixed creative tags do not match experiment")
    else:
        db.execute(
            "UPDATE creative_experiments SET fixed_tags_json=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE experiment_id=?",
            (json.dumps(fixed_tags, ensure_ascii=False, sort_keys=True), experiment_id),
        )
    db.execute(
        "INSERT INTO creative_experiment_jobs(experiment_id,job_id,arm,"
        "assignment_method,allocation_token) VALUES(?,?,?,?,?)",
        (experiment_id, job_id, arm, assignment_method, allocation_token),
    )
    db.execute(
        "UPDATE creative_experiments SET status='running', "
        "updated_at=CURRENT_TIMESTAMP WHERE experiment_id=?", (experiment_id,),
    )


def bind_slot(db: sqlite3.Connection, slot_id: str, job_id: str) -> None:
    """Bind a produced job to its previously issued system assignment."""
    slot = db.execute(
        "SELECT * FROM creative_experiment_slots WHERE slot_id=?", (slot_id,),
    ).fetchone()
    if not slot:
        raise ValueError(f"creative experiment slot not found: {slot_id}")
    if slot["bound_job_id"]:
        if slot["bound_job_id"] != job_id:
            raise ValueError(f"slot {slot_id} is already bound to {slot['bound_job_id']}")
        return
    published = db.execute(
        "SELECT 1 FROM platform_publications WHERE job_id=? LIMIT 1", (job_id,),
    ).fetchone()
    if published:
        raise ValueError("system experiment slot must be bound before publication")
    _assign_job(
        db, slot["experiment_id"], job_id, arm=slot["arm"],
        assignment_method="system_balanced", allocation_token=slot_id,
    )
    db.execute(
        "UPDATE creative_experiment_slots SET bound_job_id=?, "
        "updated_at=CURRENT_TIMESTAMP WHERE slot_id=?", (job_id, slot_id),
    )
    db.commit()


def assign_job(
    db: sqlite3.Connection, experiment_id: str, job_id: str, *, arm: str,
) -> None:
    """Assign before metrics exist and enforce the single-variable contract."""
    _assign_job(
        db, experiment_id, job_id, arm=arm,
        assignment_method="manual", allocation_token=None,
    )
    db.commit()


def experiment_report(db: sqlite3.Connection, experiment_id: str) -> dict:
    """Evaluate only the pre-registered metric and comparable time window."""
    experiment = db.execute(
        "SELECT * FROM creative_experiments WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone()
    if not experiment:
        raise ValueError(f"creative experiment not found: {experiment_id}")
    assignments = db.execute(
        "SELECT job_id,arm,assignment_method FROM creative_experiment_jobs "
        "WHERE experiment_id=? ORDER BY job_id", (experiment_id,),
    ).fetchall()
    assigned = {row["job_id"]: row["arm"] for row in assignments}
    system_allocated = sum(
        row["assignment_method"] == "system_balanced" for row in assignments
    )
    manual = len(assignments) - system_allocated
    base = performance_feedback.learning_report(
        db, platform=experiment["platform"],
        window_hours=float(experiment["window_hours"]),
        min_views=int(experiment["min_views"]),
        min_window_coverage=float(experiment["min_window_coverage"]),
    )
    current = {row['job_id']:row['publication_id'] for row in db.execute(
        'SELECT job_id,publication_id FROM platform_publications WHERE platform=? AND is_current=1',(experiment['platform'],))}
    base['creatives'] = [item for item in base['creatives'] if item.get('publication_id') == current.get(item['job_id'])]
    by_arm: dict[str, list[dict]] = {"control": [], "treatment": []}
    eligible_ids: set[str] = set()
    for creative in base["creatives"]:
        arm = assigned.get(creative["job_id"])
        if arm:
            by_arm[arm].append(creative)
            eligible_ids.add(creative["job_id"])
    arms: dict[str, dict] = {}
    for arm in ("control", "treatment"):
        expected_value = (
            experiment["control_value"] if arm == "control"
            else experiment["treatment_value"]
        )
        arms[arm] = {
            "creative_value": expected_value,
            "assigned_count": sum(value == arm for value in assigned.values()),
            **creative_learning._aggregate_creatives(by_arm[arm]),
        }
    exclusions_by_job = {
        item["job_id"]: item for item in base["excluded"]
        if item["job_id"] in assigned
    }
    excluded = []
    for job_id, arm in assigned.items():
        if job_id in eligible_ids:
            continue
        detail = exclusions_by_job.get(job_id, {
            "job_id": job_id, "platform": experiment["platform"],
            "reason": "below_min_views_or_missing_approved_brief",
        })
        excluded.append({"arm": arm, **detail})

    creative_by_job = {
        creative["job_id"]: creative for creative in base["creatives"]
    }
    base_exclusion_by_job = {
        item["job_id"]: item for item in base["excluded"]
    }
    slots = db.execute(
        "SELECT slot_id,subject_key,arm,bound_job_id "
        "FROM creative_experiment_slots WHERE experiment_id=? "
        "ORDER BY created_at,slot_id", (experiment_id,),
    ).fetchall()
    attrition: list[dict] = []
    for slot in slots:
        common = {
            "slot_id": slot["slot_id"], "subject_key": slot["subject_key"],
            "arm": slot["arm"], "job_id": slot["bound_job_id"],
        }
        job_id = slot["bound_job_id"]
        if not job_id:
            attrition.append({**common, "reason": "slot_unbound"})
            continue
        publication = db.execute(
            "SELECT publication_id,published_at FROM platform_publications "
            "WHERE job_id=? AND platform=? AND is_current=1",
            (job_id, experiment["platform"]),
        ).fetchone()
        if not publication:
            attrition.append({**common, "reason": "publication_missing"})
            continue
        snapshot_count = db.execute(
            "SELECT COUNT(*) FROM performance_snapshots "
            "WHERE publication_id=?",
            (publication["publication_id"],),
        ).fetchone()[0]
        if not snapshot_count:
            attrition.append({**common, "reason": "snapshot_missing"})
            continue
        if job_id in creative_by_job:
            continue
        window_exclusion = base_exclusion_by_job.get(job_id)
        if window_exclusion:
            attrition.append({
                **common, "reason": window_exclusion["reason"],
                **{
                    key: value for key, value in window_exclusion.items()
                    if key not in {"job_id", "platform", "reason"}
                },
            })
        else:
            attrition.append({**common, "reason": "below_min_views"})

    enough = all(
        arms[arm]["sample_count"] >= int(experiment["min_jobs_per_arm"])
        and arms[arm]["coverage"][experiment["primary_metric"]]["samples"] >= int(experiment["min_jobs_per_arm"])
        and arms[arm][experiment["primary_metric"]] is not None
        for arm in ARMS
    )
    absolute_delta = None
    relative_lift = None
    directional_assessment = "insufficient_data"
    if enough:
        metric = experiment["primary_metric"]
        control = float(arms["control"][metric])
        treatment = float(arms["treatment"][metric])
        absolute_delta = treatment - control
        relative_lift = absolute_delta / control if control else None
        threshold = float(experiment["minimum_effect"])
        if absolute_delta > 0 and absolute_delta >= threshold:
            directional_assessment = "treatment_candidate"
        elif absolute_delta < 0 and absolute_delta <= -threshold:
            directional_assessment = "control_candidate"
        else:
            directional_assessment = "no_material_difference"
    system_cohort = bool(slots)
    planned_slots = (
        int(experiment["planned_jobs_per_arm"]) * 2 if system_cohort else 0
    )
    plan_fully_issued = not system_cohort or len(slots) == planned_slots
    cohort_complete = plan_fully_issued and not attrition
    assessment = (
        directional_assessment if cohort_complete else "cohort_incomplete"
    )
    contract_fields = (
        "name", "platform", "window_hours", "min_window_coverage",
        "variable_dimension", "control_value", "treatment_value",
        "primary_metric", "hypothesis", "audience", "production_profile",
        "min_views", "min_jobs_per_arm", "planned_jobs_per_arm",
        "minimum_effect",
    )
    return {
        "experiment_id": experiment_id, "status": experiment["status"],
        "contract": {field: experiment[field] for field in contract_fields},
        "fixed_tags": json.loads(experiment["fixed_tags_json"] or "{}"),
        "primary_metric": experiment["primary_metric"], "arms": arms,
        "absolute_delta": absolute_delta, "relative_lift": relative_lift,
        "assessment": assessment,
        "directional_assessment": directional_assessment,
        "cohort_audit": {
            "cohort_type": "system_slots" if system_cohort else "legacy_manual",
            "planned_slots": planned_slots,
            "issued_slots": len(slots),
            "unissued_slots": max(planned_slots - len(slots), 0),
            "plan_fully_issued": plan_fully_issued,
            "eligible_slots": len(slots) - len(attrition),
            "complete": cohort_complete,
            "attrition": attrition,
        },
        "allocation_audit": {
            "system_allocated": system_allocated, "manual": manual,
            "evidence_grade": (
                "controlled_directional"
                if system_allocated > 0 and manual == 0
                else "observational_directional"
            ),
        },
        "evidence_policy": (
            "Result is directional, based on a pre-registered cohort; it is "
            "not statistical proof of causality and is never auto-applied."
        ),
        "excluded": sorted(excluded, key=lambda item: item["job_id"]),
    }
