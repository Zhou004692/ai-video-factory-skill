"""Traceable complete-script candidates generated from one approved brief."""
from __future__ import annotations

import hashlib
import json
from typing import Callable

from .clients import doubao_llm


PROMPT_VERSION = "complete-script-v2"
BEAT_ORDER = ("hook", "conflict", "evidence", "reversal", "solution", "cta")


def _text(value) -> str:
    return str(value or "").strip()


def _normalize_variant(raw: dict, *, allowed_source_ids: set[str]) -> dict:
    beats_raw = raw.get("beats") or []
    if len(beats_raw) != len(BEAT_ORDER):
        raise ValueError("script variant requires exactly 6 beats")
    beats: list[dict] = []
    for index, expected in enumerate(BEAT_ORDER):
        beat = beats_raw[index] or {}
        beat_type = _text(beat.get("beat"))
        if beat_type != expected:
            raise ValueError(
                f"script beat #{index + 1} must be {expected}, got {beat_type!r}"
            )
        try:
            duration = float(beat.get("duration_sec"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"script beat {expected}.duration_sec is required") from exc
        if duration <= 0 or duration > 12:
            raise ValueError(
                f"script beat {expected}.duration_sec must be between 0 and 12"
            )
        source_ids = list(dict.fromkeys(
            _text(item) for item in (beat.get("source_ids") or []) if _text(item)
        ))
        unknown = sorted(set(source_ids) - allowed_source_ids)
        if unknown:
            raise ValueError(f"script beat {expected} references unknown source_id: {unknown}")
        normalized = {
            "beat": beat_type,
            # The standard product is one native dialogue master. Model-proposed
            # role labels describe the acting perspective, not extra speakers.
            "speaker": "presenter",
            "dialogue": _text(beat.get("dialogue")),
            "duration_sec": duration,
            "visual_intent": _text(beat.get("visual_intent")),
            "source_ids": source_ids,
            "broll_hint": _text(beat.get("broll_hint")),
        }
        for field in ("speaker", "dialogue", "visual_intent"):
            if not normalized[field]:
                raise ValueError(f"script beat {expected}.{field} is required")
        beats.append(normalized)
    title = _text(raw.get("title"))
    thesis = _text(raw.get("creative_thesis"))
    if not title:
        raise ValueError("script variant.title is required")
    if not thesis:
        raise ValueError("script variant.creative_thesis is required")
    total = round(sum(beat["duration_sec"] for beat in beats), 2)
    declared = float(raw.get("target_duration_sec") or total)
    if abs(declared - total) > 0.5:
        raise ValueError("script target_duration_sec must match the beat duration sum")
    return {
        "title": title,
        "creative_thesis": thesis,
        "target_duration_sec": declared,
        "total_duration_sec": total,
        "beats": beats,
    }


def normalize_response(raw: dict, *, allowed_source_ids: set[str]) -> dict:
    variants_raw = raw.get("variants") or []
    if len(variants_raw) != 3:
        raise ValueError("script generation requires exactly 3 variants")
    variants = [
        _normalize_variant(item or {}, allowed_source_ids=allowed_source_ids)
        for item in variants_raw
    ]
    try:
        recommended = int(raw.get("recommended_index"))
    except (TypeError, ValueError) as exc:
        raise ValueError("recommended_index must be 0, 1 or 2") from exc
    if recommended not in range(3):
        raise ValueError("recommended_index must be 0, 1 or 2")
    return {
        "variants": variants,
        "recommended_index": recommended,
        "recommendation_reason": _text(raw.get("recommendation_reason")),
    }


def _load_context(db, brief_id: str) -> tuple[dict, list[dict]]:
    row = db.execute(
        "SELECT status, brief_json, approved_angle_index, approved_job_id "
        "FROM topic_briefs WHERE brief_id=?", (brief_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"topic brief not found: {brief_id}")
    if row["status"] != "approved":
        raise ValueError("topic brief must be approved before script generation")
    brief = json.loads(row["brief_json"])
    angle_index = row["approved_angle_index"]
    if angle_index is None:
        angle_index = brief["recommended_index"]
    angle_index = int(angle_index)
    if angle_index not in range(len(brief.get("angles") or [])):
        raise ValueError("approved brief has an invalid angle index")
    sources = []
    for source in db.execute(
        "SELECT s.source_id,s.fingerprint,s.title,s.source_uri,s.content "
        "FROM script_sources s JOIN topic_brief_sources b "
        "ON b.source_id=s.source_id WHERE b.brief_id=? "
        "ORDER BY s.source_id", (brief_id,),
    ).fetchall():
        sources.append(dict(source))
    if not sources:
        raise ValueError("approved brief has no traceable sources")
    return {
        "brief_id": brief_id,
        "approved_job_id": row["approved_job_id"],
        "audience": brief.get("audience"),
        "angle": brief["angles"][angle_index],
    }, sources


def _fingerprint(context: dict, sources: list[dict]) -> str:
    payload = {
        "version": PROMPT_VERSION,
        "brief_id": context["brief_id"],
        "angle": context["angle"],
        "sources": [
            {"source_id": item["source_id"], "fingerprint": item["fingerprint"]}
            for item in sources
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prompt(context: dict, sources: list[dict]) -> str:
    contract = {
        "variants": [{
            "title": "剧本标题",
            "creative_thesis": "核心命题",
            "target_duration_sec": 24,
            "beats": [{
                "beat": "hook|conflict|evidence|reversal|solution|cta",
                "speaker": "presenter",
                "dialogue": "口语化台词",
                "duration_sec": 4,
                "visual_intent": "画面意图",
                "source_ids": ["SRC-..."],
                "broll_hint": "辅助画面建议",
            }],
        }],
        "recommended_index": 0,
        "recommendation_reason": "推荐理由",
    }
    return (
        "你是设备租赁行业短视频编剧。根据已批准的创意角度生成恰好三版完整剧本。"
        "每版必须依次包含 hook、conflict、evidence、reversal、solution、cta 六个 beat，"
        "每版只允许一位固定主讲人，所有 beat 的 speaker 必须为 presenter；人物引语由主讲人转述。"
        "每个 beat 必须有可直接朗读的中文台词、预计秒数、画面意图、来源 ID 和 B-roll 建议。"
        "只允许引用提供的 source_id；不得编造事实、金额、客户、政策或产品能力；"
        "不得照抄竞品台词，只能借鉴抽象结构。目标为 Seedance 2.5 原生母片加本地导演剪辑。"
        "只输出 JSON，结构参考：\n"
        + json.dumps(contract, ensure_ascii=False)
        + "\n\n已批准 Brief：\n"
        + json.dumps(context, ensure_ascii=False)
        + "\n\n可用来源：\n"
        + json.dumps(sources, ensure_ascii=False)
    )


def _rows_payload(rows) -> dict:
    drafts = [{
        "draft_id": row["draft_id"],
        "variant_index": row["variant_index"],
        "status": row["status"],
        "script": json.loads(row["script_json"]),
    } for row in rows]
    return {
        "brief_id": rows[0]["brief_id"],
        "recommended_index": rows[0]["recommended_index"],
        "recommendation_reason": rows[0]["recommendation_reason"] or "",
        "drafts": drafts,
    }


def generate_drafts(db, brief_id: str, *, force: bool = False,
                    generate_json: Callable = doubao_llm.complete_json) -> dict:
    context, sources = _load_context(db, _text(brief_id))
    request_fingerprint = _fingerprint(context, sources)
    if not force:
        cached = db.execute(
            "SELECT * FROM script_drafts WHERE brief_id=? AND request_fingerprint=? "
            "ORDER BY variant_index", (brief_id, request_fingerprint),
        ).fetchall()
        if len(cached) == 3:
            return {**_rows_payload(cached), "cached": True}
    raw, usage = generate_json(_prompt(context, sources), max_tokens=6000)
    normalized = normalize_response(
        raw, allowed_source_ids={item["source_id"] for item in sources}
    )
    rows = []
    for index, script in enumerate(normalized["variants"]):
        identity = json.dumps(
            {"request": request_fingerprint, "index": index, "script": script},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        draft_id = "SCRIPT-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        db.execute(
            "INSERT OR IGNORE INTO script_drafts("
            "draft_id,brief_id,variant_index,status,request_fingerprint,script_json,"
            "recommended_index,recommendation_reason,input_tokens,output_tokens) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                draft_id, brief_id, index, "candidate", request_fingerprint,
                json.dumps(script, ensure_ascii=False),
                normalized["recommended_index"],
                normalized["recommendation_reason"],
                int((usage or {}).get("input_tokens") or 0),
                int((usage or {}).get("output_tokens") or 0),
            ),
        )
    db.commit()
    rows = db.execute(
        "SELECT * FROM script_drafts WHERE brief_id=? AND request_fingerprint=? "
        "ORDER BY variant_index", (brief_id, request_fingerprint),
    ).fetchall()
    return {**_rows_payload(rows), "cached": False}


def _subtitle(dialogue: str) -> str:
    """Remove declarative sentence-end periods; preserve expressive marks."""
    return _text(dialogue).rstrip("。．.")


def _storyboard(script: dict) -> dict:
    beats = script["beats"]
    speakers = {_text(beat["speaker"]) for beat in beats}
    if len(speakers) != 1:
        raise ValueError("standard single-master script requires exactly one speaker")
    speaker_id = speakers.pop()
    total_duration = round(sum(float(beat["duration_sec"]) for beat in beats), 2)
    voiceover = "".join(_text(beat["dialogue"]) for beat in beats)
    master = {
        "idx": 1,
        "type": "dialogue",
        "speaker": speaker_id,
        "render_mode": "presenter",
        "duration": total_duration,
        "shot_type": "中近景连续讲解",
        "camera": "稳定胸像构图，按叙事节奏做轻微推近",
        "scene": "明亮现代的设备租赁办公室，背景整洁，无可读文字、数字、商标或水印",
        "action": "主讲人面对镜头自然讲解，随钩子、反转和行动号召做克制手势与表情变化",
        "voiceover": voiceover,
        "subtitle": _subtitle(voiceover),
    }
    shots = []
    cursor = 0.0
    for index, beat in enumerate(beats, 1):
        duration = float(beat["duration_sec"])
        shots.append({
            "idx": index,
            "beat": beat["beat"],
            "duration": duration,
            "video_master_idx": 1,
            "audio_master_idx": 1,
            "video_start": cursor,
            "audio_start": cursor,
            "subtitle": _subtitle(beat["dialogue"]),
            "visual_intent": beat["visual_intent"],
            "broll_hint": beat["broll_hint"],
            "source_ids": beat["source_ids"],
        })
        cursor = round(cursor + duration, 2)
    return {
        "production_profile": "standard",
        "production_mode": "native_master_local_director",
        "characters": [{
            "id": speaker_id,
            "name": "主讲人",
            "gender": "female",
            "look": (
                "中国女性设备租赁顾问，偏长椭圆脸，黑棕色中长发，"
                "自然职业淡妆，炭灰色西装与暖白色上衣，真实皮肤纹理"
            ),
        }],
        "hook": beats[0]["dialogue"],
        "masters": [master],
        "shots": shots,
        "bgm": {
            "mood": "现代、克制、逐步明亮、无歌词",
            "prompt": "轻节奏商务音乐，不遮盖原生中文人声",
        },
        "script_metadata": {
            "title": script["title"],
            "creative_thesis": script["creative_thesis"],
            "source_ids": sorted({
                source_id for beat in beats for source_id in beat["source_ids"]
            }),
        },
    }


def _approved_payload(row) -> dict:
    return {
        "draft_id": row["draft_id"],
        "brief_id": row["brief_id"],
        "job_id": row["approved_job_id"],
        "status": row["status"],
        "storyboard": json.loads(row["storyboard_json"]),
    }


def approve_draft(db, draft_id: str) -> dict:
    row = db.execute(
        "SELECT d.*,b.approved_job_id AS brief_job_id "
        "FROM script_drafts d JOIN topic_briefs b ON b.brief_id=d.brief_id "
        "WHERE d.draft_id=?", (_text(draft_id),),
    ).fetchone()
    if not row:
        raise ValueError(f"script draft not found: {draft_id}")
    if row["status"] == "approved" and row["storyboard_json"]:
        return _approved_payload(row)
    sibling = db.execute(
        "SELECT draft_id FROM script_drafts WHERE brief_id=? AND status='approved'",
        (row["brief_id"],),
    ).fetchone()
    if sibling:
        raise ValueError(
            f"brief already has approved draft: {sibling['draft_id']}"
        )
    if row["status"] != "candidate":
        raise ValueError(f"script draft is not approvable: {row['status']}")
    job_id = _text(row["brief_job_id"])
    if not job_id:
        raise ValueError("approved brief has no production job")
    if not db.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone():
        raise ValueError(f"approved brief job not found: {job_id}")
    job_state = db.execute(
        "SELECT state FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()["state"]
    if job_state not in ("PENDING", "STORYBOARDED"):
        raise ValueError(
            f"production job must be PENDING before script approval, got {job_state}"
        )
    storyboard = _storyboard(json.loads(row["script_json"]))
    storyboard_json = json.dumps(storyboard, ensure_ascii=False)
    db.execute(
        "UPDATE script_drafts SET status='rejected',updated_at=CURRENT_TIMESTAMP "
        "WHERE brief_id=? AND draft_id<>? AND status='candidate'",
        (row["brief_id"], row["draft_id"]),
    )
    db.execute(
        "UPDATE script_drafts SET status='approved',approved_job_id=?,"
        "storyboard_json=?,updated_at=CURRENT_TIMESTAMP WHERE draft_id=?",
        (job_id, storyboard_json, row["draft_id"]),
    )
    db.execute(
        "UPDATE jobs SET storyboard=?,state='STORYBOARDED',"
        "updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
        (storyboard_json, job_id),
    )
    db.commit()
    approved = db.execute(
        "SELECT * FROM script_drafts WHERE draft_id=?", (row["draft_id"],)
    ).fetchone()
    return _approved_payload(approved)


def list_drafts(db, brief_id: str) -> list[dict]:
    rows = db.execute(
        "SELECT draft_id,variant_index,status,script_json,recommended_index "
        "FROM script_drafts WHERE brief_id=? ORDER BY variant_index",
        (_text(brief_id),),
    ).fetchall()
    return [{
        "draft_id": row["draft_id"],
        "variant_index": row["variant_index"],
        "status": row["status"],
        "recommended": row["variant_index"] == row["recommended_index"],
        "script": json.loads(row["script_json"]),
    } for row in rows]


def get_storyboard(db, draft_id: str) -> dict:
    row = db.execute(
        "SELECT status,storyboard_json FROM script_drafts WHERE draft_id=?",
        (_text(draft_id),),
    ).fetchone()
    if not row:
        raise ValueError(f"script draft not found: {draft_id}")
    if row["status"] != "approved" or not row["storyboard_json"]:
        raise ValueError("script draft must be approved before export")
    return json.loads(row["storyboard_json"])
