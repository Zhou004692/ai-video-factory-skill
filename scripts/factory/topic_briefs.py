"""Source scoring and human-approved creative briefs before production."""
from __future__ import annotations

from difflib import SequenceMatcher
import hashlib
import json
import re
from typing import Callable

from .clients import doubao_llm
from . import creative_learning


SCORE_FIELDS = (
    "relevance", "story_potential", "evidence_quality", "production_fit",
)
BRIEF_PROMPT_VERSION = "creative-brief-v1"


def _text(value) -> str:
    return str(value or "").strip()


def _string_list(value) -> list[str]:
    return [str(item).strip() for item in (value or []) if str(item).strip()]


def normalize_brief(raw: dict) -> dict:
    score_raw = raw.get("source_score") or {}
    score: dict = {}
    for field in SCORE_FIELDS:
        try:
            value = float(score_raw[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"source_score.{field} is required") from exc
        if not 0 <= value <= 100:
            raise ValueError(f"source_score.{field} must be between 0 and 100")
        score[field] = value
    score["overall"] = round(sum(score.values()) / len(SCORE_FIELDS), 1)
    score["reason"] = _text(score_raw.get("reason"))

    raw_angles = raw.get("angles") or []
    if len(raw_angles) != 3:
        raise ValueError("creative brief requires exactly 3 angles")
    angles: list[dict] = []
    for index, angle in enumerate(raw_angles):
        normalized = {
            "title": _text(angle.get("title")),
            "hook": _text(angle.get("hook")),
            "promise": _text(angle.get("promise")),
            "story_arc": _text(angle.get("story_arc")),
            "evidence": _string_list(angle.get("evidence")),
            "risk_notes": _string_list(angle.get("risk_notes")),
            "production_notes": _text(angle.get("production_notes")),
        }
        creative_tags = angle.get("creative_tags")
        if creative_tags is not None:
            normalized["creative_tags"] = {
                field: _text(creative_tags.get(field))
                for field in creative_learning.VOCABULARY
            }
            for field, value in normalized["creative_tags"].items():
                if value not in creative_learning.VOCABULARY[field]:
                    raise ValueError(
                        f"angle #{index + 1}.creative_tags.{field} "
                        f"must be one of {sorted(creative_learning.VOCABULARY[field])}"
                    )
        for field in ("title", "hook", "promise", "story_arc",
                      "production_notes"):
            if not normalized[field]:
                raise ValueError(f"angle #{index + 1}.{field} is required")
        if not normalized["evidence"]:
            raise ValueError(f"angle #{index + 1}.evidence is required")
        angles.append(normalized)
    recommended = int(raw.get("recommended_index", -1))
    if recommended not in range(3):
        raise ValueError("recommended_index must be 0, 1 or 2")
    return {
        "audience": _text(raw.get("audience")),
        "source_score": score,
        "angles": angles,
        "recommended_index": recommended,
        "recommendation_reason": _text(raw.get("recommendation_reason")),
    }


def _canonical_title(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value.lower())


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(
        None, _canonical_title(left), _canonical_title(right)
    ).ratio()


def find_similar_topic(db, title: str, *, threshold: float = 0.72) -> dict | None:
    candidates: list[dict] = []
    for row in db.execute("SELECT job_id, title FROM jobs").fetchall():
        candidates.append({
            "kind": "job", "id": row["job_id"], "title": row["title"]
        })
    for row in db.execute(
        "SELECT brief_id, recommended_title FROM topic_briefs "
        "WHERE status IN ('candidate','approved')"
    ).fetchall():
        candidates.append({
            "kind": "brief", "id": row["brief_id"],
            "title": row["recommended_title"],
        })
    best = None
    for candidate in candidates:
        similarity = _similarity(title, candidate["title"])
        if best is None or similarity > best["similarity"]:
            best = {**candidate, "similarity": round(similarity, 4)}
    return best if best and best["similarity"] >= threshold else None


def _brief_prompt(sources: list[dict]) -> str:
    return (
        "你是设备租赁行业短视频创意总监。只基于来源内容与机器分析提出三个明显"
        "不同的创意角度，不得照抄来源标题，不得编造事实、金额、客户或产品能力。"
        "每个角度必须引用来源中已有证据，并说明适合 Seedance 2.5 原生母片 + "
        "本地导演剪辑的生产方式。评分 0—100。只输出 JSON："
        '{"audience":"目标受众","source_score":{"relevance":0,'
        '"story_potential":0,"evidence_quality":0,"production_fit":0,'
        '"reason":"理由"},"angles":[{"title":"选题标题","hook":"前三秒",'
        '"promise":"观众收益","story_arc":"故事弧",'
        '"evidence":["来源原句或观察"],"risk_notes":["风险"],'
        '"production_notes":"母片与剪辑建议","creative_tags":{'
        '"hook_type":"pain_point","narrative_pattern":"problem_solution",'
        '"proof_type":"source_quote","cta_type":"consult"}}],'
        '"recommended_index":0,'
        '"recommendation_reason":"推荐理由"}\n\n来源：'
        + json.dumps(sources, ensure_ascii=False)
    )


def _request_fingerprint(sources: list[dict]) -> str:
    payload = {
        "version": BRIEF_PROMPT_VERSION,
        "sources": [
            {
                "source_id": source["source_id"],
                "fingerprint": source["fingerprint"],
                "structure_analysis": source["structure_analysis"],
            }
            for source in sources
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cached_brief(db, request_fingerprint: str) -> dict | None:
    row = db.execute(
        "SELECT brief_id, status, brief_json, duplicate_json "
        "FROM topic_briefs WHERE request_fingerprint=? "
        "ORDER BY created_at DESC LIMIT 1",
        (request_fingerprint,),
    ).fetchone()
    if not row:
        return None
    return {
        "brief_id": row["brief_id"],
        "status": row["status"],
        "brief": json.loads(row["brief_json"]),
        "duplicate_warning": (
            json.loads(row["duplicate_json"]) if row["duplicate_json"] else None
        ),
        "cached": True,
    }


def generate_brief(db, source_ids: list[str], *, force: bool = False,
                   generate_json: Callable = doubao_llm.complete_json) -> dict:
    unique_ids = sorted(dict.fromkeys(
        str(item).strip() for item in source_ids if str(item).strip()
    ))
    if not unique_ids:
        raise ValueError("at least one source_id is required")
    sources: list[dict] = []
    for source_id in unique_ids:
        row = db.execute(
            "SELECT source_id, fingerprint, title, source_uri, content, "
            "metadata_json "
            "FROM script_sources WHERE source_id=?", (source_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"script source not found: {source_id}")
        metadata = json.loads(row["metadata_json"] or "{}")
        sources.append({
            "source_id": row["source_id"],
            "fingerprint": row["fingerprint"], "title": row["title"],
            "source_uri": row["source_uri"], "content": row["content"],
            "structure_analysis": metadata.get("structure_analysis") or {},
        })
    request_fingerprint = _request_fingerprint(sources)
    if not force:
        cached = _cached_brief(db, request_fingerprint)
        if cached:
            return cached
    raw, usage = generate_json(_brief_prompt(sources), max_tokens=3000)
    brief = normalize_brief(raw)
    recommended_title = brief["angles"][brief["recommended_index"]]["title"]
    duplicate = find_similar_topic(db, recommended_title)
    identity = json.dumps({"sources": unique_ids, "brief": brief},
                          ensure_ascii=False, sort_keys=True)
    brief_id = "BRIEF-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    db.execute(
        "INSERT OR IGNORE INTO topic_briefs(brief_id, request_fingerprint, "
        "status, recommended_title, brief_json, duplicate_json, input_tokens, "
        "output_tokens, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
        (
            brief_id, request_fingerprint, "candidate", recommended_title,
            json.dumps(brief, ensure_ascii=False),
            json.dumps(duplicate, ensure_ascii=False) if duplicate else None,
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
        ),
    )
    for source_id in unique_ids:
        db.execute(
            "INSERT OR IGNORE INTO topic_brief_sources(brief_id, source_id) "
            "VALUES(?,?)", (brief_id, source_id),
        )
    for angle_index, angle in enumerate(brief["angles"]):
        tags = angle.get("creative_tags")
        if tags:
            creative_learning.tag_angle(
                db, brief_id, angle_index=angle_index,
                hook_type=tags["hook_type"],
                narrative_pattern=tags["narrative_pattern"],
                proof_type=tags["proof_type"], cta_type=tags["cta_type"],
                notes="generated_with_brief",
            )
    db.commit()
    return {
        "brief_id": brief_id, "status": "candidate", "brief": brief,
        "duplicate_warning": duplicate, "cached": False,
    }


def approve_brief(db, brief_id: str, *, topic_id: str, track: str = "",
                  target_audience: str = "", priority: int = 3,
                  angle_index: int | None = None) -> str:
    row = db.execute(
        "SELECT status, recommended_title, brief_json, approved_topic_id, "
        "approved_job_id, approved_angle_index "
        "FROM topic_briefs WHERE brief_id=?", (brief_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"topic brief not found: {brief_id}")
    topic_id = _text(topic_id)
    if not topic_id:
        raise ValueError("topic_id is required")
    brief = json.loads(row["brief_json"])
    selected_index = (
        int(brief["recommended_index"])
        if angle_index is None else int(angle_index)
    )
    if selected_index not in range(len(brief["angles"])):
        raise ValueError("angle_index must select one of the three angles")
    selected_title = brief["angles"][selected_index]["title"]
    job_id = f"J-{topic_id}"
    if row["approved_job_id"]:
        if row["approved_topic_id"] != topic_id:
            raise ValueError("brief is already approved with another topic_id")
        if row["approved_angle_index"] != selected_index:
            raise ValueError("brief is already approved with another angle")
        return str(row["approved_job_id"])
    existing = db.execute(
        "SELECT title FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if existing and existing["title"] != selected_title:
        raise ValueError(f"job {job_id} already exists with another title")
    db.execute(
        "INSERT OR IGNORE INTO jobs(job_id, topic_id, title, track, "
        "target_audience, priority) VALUES(?,?,?,?,?,?)",
        (job_id, topic_id, selected_title, track,
         target_audience, int(priority)),
    )
    sources = db.execute(
        "SELECT source_id FROM topic_brief_sources WHERE brief_id=?",
        (brief_id,),
    ).fetchall()
    for source in sources:
        db.execute(
            "INSERT OR IGNORE INTO topic_script_sources(topic_id, source_id) "
            "VALUES(?,?)", (topic_id, source["source_id"]),
        )
    db.execute(
        "UPDATE topic_briefs SET status='approved', approved_topic_id=?, "
        "approved_job_id=?, approved_angle_index=?, "
        "updated_at=CURRENT_TIMESTAMP WHERE brief_id=?",
        (topic_id, job_id, selected_index, brief_id),
    )
    db.commit()
    return job_id
