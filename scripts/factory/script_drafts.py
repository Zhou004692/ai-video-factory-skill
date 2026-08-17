"""Traceable complete-script candidates generated from one approved brief."""
from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from typing import Callable

from .clients import doubao_llm


PROMPT_VERSION = "complete-script-v7-video-reconstruction"
BEAT_ORDER = ("hook", "conflict", "evidence", "reversal", "solution", "cta")
MAX_PROVIDER_MASTER_DURATION_SEC = 30.0

GENERATION_PREFERENCE_RULES = {
    "suggested_duration_sec": (15, 72, "建议总时长需在 15–72 秒之间"),
    "suggested_shot_count": (4, 8, "建议分镜数需在 4–8 个之间"),
    "suggested_character_count": (1, 6, "建议角色数需在 1–6 人之间"),
}


def _text(value) -> str:
    return str(value or "").strip()


def normalize_generation_preferences(raw: dict | None) -> dict:
    """Return validated optional creative guidance, never hard output gates."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("剧本创作建议格式无效")
    normalized: dict[str, int] = {}
    for field, (minimum, maximum, message) in GENERATION_PREFERENCE_RULES.items():
        value = raw.get(field)
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            raise ValueError(message)
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(message) from exc
        if not numeric.is_integer() or not minimum <= numeric <= maximum:
            raise ValueError(message)
        normalized[field] = int(numeric)
    return normalized


def estimate_dialogue_duration(dialogue: str) -> float:
    """Estimate natural spoken duration and round upward to 0.5 seconds."""
    text = _text(dialogue)
    if not text:
        return 1.5
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
    latin_words = len(re.findall(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)*", text))
    light_pauses = len(re.findall(r"[，、,:：]", text))
    strong_pauses = len(re.findall(r"[。！？!?；;]", text))
    seconds = (
        cjk_count / 3.6
        + latin_words / 2.6
        + light_pauses * 0.12
        + strong_pauses * 0.28
        + 0.55
    )
    rounded = math.ceil(seconds * 2) / 2
    return max(1.5, rounded)


def adjust_script_durations(script: dict) -> dict:
    """Recompute beat and aggregate durations from the edited dialogue."""
    adjusted = deepcopy(script if isinstance(script, dict) else {})
    beats = adjusted.get("beats") or []
    for beat in beats:
        beat["duration_sec"] = estimate_dialogue_duration(beat.get("dialogue"))
    total = round(sum(float(beat["duration_sec"]) for beat in beats), 2)
    adjusted["target_duration_sec"] = total
    adjusted["total_duration_sec"] = total
    adjusted["duration_mode"] = "auto"
    return adjusted


def _is_valid_duration(value) -> bool:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(duration) and duration > 0


def _normalize_variant(raw: dict, *, allowed_source_ids: set[str]) -> dict:
    beats_raw = raw.get("beats") or []
    if not isinstance(beats_raw, list) or not beats_raw:
        raise ValueError("script variant requires at least 1 narrative beat")
    beats: list[dict] = []
    for index, beat in enumerate(beats_raw):
        beat = beats_raw[index] or {}
        beat_type = _text(beat.get("beat")) or (
            BEAT_ORDER[index] if index < len(BEAT_ORDER)
            else f"segment_{index + 1}"
        )
        beat_label = beat_type or f"#{index + 1}"
        dialogue = _text(beat.get("dialogue"))
        duration = (
            float(beat.get("duration_sec"))
            if _is_valid_duration(beat.get("duration_sec"))
            else estimate_dialogue_duration(dialogue)
        )
        source_ids = list(dict.fromkeys(
            _text(item) for item in (beat.get("source_ids") or []) if _text(item)
        ))
        unknown = sorted(set(source_ids) - allowed_source_ids)
        if unknown:
            raise ValueError(
                f"script beat {beat_label} references unknown source_id: {unknown}"
            )
        normalized = {
            "beat": beat_type,
            "speaker": _text(beat.get("speaker")),
            "dialogue": dialogue,
            "duration_sec": duration,
            "visual_intent": _text(beat.get("visual_intent")),
            "source_ids": source_ids,
            "broll_hint": _text(beat.get("broll_hint")),
        }
        for field in ("speaker", "dialogue", "visual_intent"):
            if not normalized[field]:
                raise ValueError(f"script beat {beat_label}.{field} is required")
        beats.append(normalized)
    title = _text(raw.get("title"))
    thesis = _text(raw.get("creative_thesis"))
    if not title:
        raise ValueError("script variant.title is required")
    if not thesis:
        raise ValueError("script variant.creative_thesis is required")
    total = round(sum(beat["duration_sec"] for beat in beats), 2)
    # The total is derived from normalized beats. Model-provided and manually
    # edited aggregate fields are hints, never an independent source of truth.
    declared = total
    return {
        "title": title,
        "creative_thesis": thesis,
        "target_duration_sec": declared,
        "total_duration_sec": total,
        "beats": beats,
    }


def normalize_response(raw: dict, *, allowed_source_ids: set[str]) -> dict:
    variants_raw = raw.get("variants") or []
    if not isinstance(variants_raw, list) or not variants_raw:
        raise ValueError("script generation requires at least 1 usable variant")
    variants = [
        _normalize_variant(item or {}, allowed_source_ids=allowed_source_ids)
        for item in variants_raw[:3]
    ]
    try:
        recommended = int(raw.get("recommended_index"))
    except (TypeError, ValueError):
        recommended = 0
    if recommended not in range(len(variants)):
        recommended = 0
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
        "SELECT s.source_id,s.fingerprint,s.source_type,s.title,s.source_uri,"
        "s.content,s.metadata_json "
        "FROM script_sources s JOIN topic_brief_sources b "
        "ON b.source_id=s.source_id WHERE b.brief_id=? "
        "ORDER BY s.source_id", (brief_id,),
    ).fetchall():
        item = dict(source)
        try:
            metadata = json.loads(item.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            metadata = {}
        item["duration_sec"] = metadata.get("duration_sec")
        item["structure_analysis"] = (
            metadata.get("structure_analysis") or {}
        )
        sources.append(item)
    if not sources:
        raise ValueError("approved brief has no traceable sources")
    return {
        "brief_id": brief_id,
        "approved_job_id": row["approved_job_id"],
        "audience": brief.get("audience"),
        "angle": brief["angles"][angle_index],
    }, sources


def _reconstruction_source(sources: list[dict]) -> dict | None:
    """Return the single video source that has a usable observed timeline."""
    candidates = [
        source for source in sources
        if source.get("source_type") == "video"
        and (source.get("structure_analysis") or {}).get("segments")
    ]
    return candidates[0] if len(candidates) == 1 else None


def _segment_visual_intent(segment: dict) -> str:
    parts = []
    for label, value in (
        ("场景", _text(segment.get("scene"))),
        ("出镜人物", "、".join(
            _text(item) for item in segment.get("characters") or []
            if _text(item)
        )),
        ("人物动作", _text(segment.get("action"))),
        ("景别与运镜", _text(segment.get("camera"))),
        ("画面文字", _text(segment.get("on_screen_text"))),
    ):
        if value:
            parts.append(f"{label}：{value}")
    return "；".join(parts) or "严格沿用原片该时间段的可见画面"


def _reconstruction_variant(sources: list[dict]) -> dict | None:
    """Build a lossless draft directly from observed video segments.

    The model may still produce two creative adaptations, but the first draft
    is deterministic so the source timeline cannot be silently rewritten.
    """
    source = _reconstruction_source(sources)
    if not source:
        return None
    analysis = source["structure_analysis"]
    beats: list[dict] = []
    for segment in analysis.get("segments") or []:
        start = max(0.0, float(segment.get("start_sec") or 0))
        end = max(start, float(segment.get("end_sec") or start))
        if end <= start:
            continue
        characters = [
            _text(item) for item in segment.get("characters") or []
            if _text(item)
        ]
        dialogue = _text(segment.get("dialogue"))
        speaker = (
            _text(segment.get("speaker"))
            or (characters[0] if characters else "现场旁白")
        )
        supporting = "；".join(
            item for item in (
                _text(segment.get("on_screen_text")),
                _text(segment.get("audio")),
            ) if item
        )
        beats.append({
            "beat": _text(segment.get("role")) or "other",
            "speaker": speaker,
            "dialogue": dialogue,
            "duration_sec": round(end - start, 3),
            "visual_intent": _segment_visual_intent(segment),
            "source_ids": [source["source_id"]],
            "broll_hint": supporting,
            "source_start_sec": round(start, 3),
            "source_end_sec": round(end, 3),
            "scene": _text(segment.get("scene")),
            "characters": characters,
            "action": _text(segment.get("action")),
            "camera": _text(segment.get("camera")),
            "on_screen_text": _text(segment.get("on_screen_text")),
            "audio": _text(segment.get("audio")),
        })
    if not beats:
        return None
    total = round(sum(float(beat["duration_sec"]) for beat in beats), 3)
    return {
        "title": f"原片高还原稿｜{_text(source.get('title')) or '本地视频'}",
        "creative_thesis": (
            _text(analysis.get("summary")) or "按原片时间线还原"
        ),
        "target_duration_sec": total,
        "total_duration_sec": total,
        "reconstruction_mode": "source_timeline",
        "beats": beats,
    }


def _fingerprint(
    context: dict, sources: list[dict], preferences: dict | None = None
) -> str:
    payload = {
        "version": PROMPT_VERSION,
        "brief_id": context["brief_id"],
        "angle": context["angle"],
        "sources": [
            {
                "source_id": item["source_id"],
                "fingerprint": item["fingerprint"],
                "source_type": item.get("source_type"),
                "structure_analysis": item.get("structure_analysis") or {},
            }
            for item in sources
        ],
        "generation_preferences": normalize_generation_preferences(preferences),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prompt(
    context: dict, sources: list[dict], preferences: dict | None = None
) -> str:
    preferences = normalize_generation_preferences(preferences)
    reconstruction = _reconstruction_source(sources)
    reconstruction_guidance = ""
    if reconstruction:
        reconstruction_guidance = (
            "该任务包含本地视频的镜头级结构分析。系统会把原时间线确定性保存为"
            "第一版‘原片高还原稿’，你生成的候选也必须以原片为事实边界：逐段继承"
            "场景、出镜人物、说话人、动作、景别与运镜、原声台词、画面文字和环境声。"
            "不得把人物原话改成主持人或旁白转述，不得新增原片没有出现的仓库、会议室、"
            "车队、二维码、客户评价或产品能力。原片总时长优先于建议时长；若要优化节奏，"
            "只能压缩停顿或调整剪辑，不得改写事实和人物关系。"
        )
    contract = {
        "variants": [{
            "title": "剧本标题",
            "creative_thesis": "核心命题",
            "target_duration_sec": 24,
            "beats": [{
                "beat": "hook|conflict|evidence|reversal|solution|cta",
                "speaker": "character_id",
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
        "你是设备租赁行业短视频编剧。根据已批准的创意角度，目标生成三版完整剧本。"
        + reconstruction_guidance
        +
        "每版应覆盖 hook、conflict、evidence、reversal、solution、cta 等必要叙事功能；"
        "beat 数量和顺序由内容决定，可合并、拆分或省略不适用的模板段落，不得为凑六段重复内容。"
        "speaker 使用稳定的简短人物 ID；同一人物在所有 beat 中必须使用同一个 ID。"
        "人物数量由选题、来源和叙事需要决定，不得因为口播形式默认只有一名 presenter。"
        "口播可以采用单人主讲、双人问答、多人接力或同场对谈；来源或 Brief 出现多个明确人物、"
        "观点或多轮对话时，必须使用不同且稳定的 speaker ID，不得合并为 presenter；"
        "确实只有单一视角时无需硬凑多人。"
        "角色分配规则：presenter 不是默认人物；只有 Brief 明确要求主持人或旁白，或者内容确实是"
        "没有故事人物的单一视角知识讲解时才可使用 presenter。只要来源或创意中存在明确的事件人物、"
        "角色关系或场景冲突，冲突、质疑、反驳、决策和行动必须由对应的事件当事人物说出，"
        "不得由 presenter 转述或代说；来源中的人物台词必须归属该人物。必要的事实串联可以使用旁白，"
        "但旁白不能抢走角色推动剧情的台词。"
        "每个 beat 必须有可直接朗读的中文台词、预计秒数、画面意图、来源 ID 和 B-roll 建议。"
        "visual_intent 必须是可直接导演执行的画面，至少写清场景空间、人物走位、动作或技能展示、"
        "关键物件变化以及景别与运镜；不得只写‘人物讲解’或泛化氛围。"
        "多人剧情不得默认写成办公室站桩口播，必须让人物在剧情场景中行动并由镜头参与叙事；"
        "单人知识讲解才允许使用稳定口播构图。"
        "只允许引用提供的 source_id；不得编造事实、金额、客户、政策或产品能力；"
        "不得照抄竞品台词，只能借鉴抽象结构。目标为 Seedance 2.5 原生母片加本地导演剪辑。"
        "下方创作建议均为软约束：建议总时长用于调节各 beat 的台词密度和节奏，各 beat 秒数之和应尽量接近；"
        "建议分镜数是后续本地导演拆解的画面密度目标，不是剧本段数硬门槛，请在 visual_intent 中提供足够且互不重复的动作、"
        "场景与运镜变化；建议角色数用于分配台词和行动，但不得遗漏来源中的关键人物，也不得为凑人数虚构无意义角色。"
        "建议为空时由你根据来源与创意自主决定。"
        "只输出 JSON，结构参考：\n"
        + json.dumps(contract, ensure_ascii=False)
        + "\n\n创作建议（软约束）：\n"
        + json.dumps(preferences, ensure_ascii=False)
        + "\n\n已批准 Brief：\n"
        + json.dumps(context, ensure_ascii=False)
        + "\n\n可用来源：\n"
        + json.dumps(sources, ensure_ascii=False)
    )


def _repair_prompt(
    context: dict, sources: list[dict], raw: dict, error: ValueError,
    preferences: dict | None = None,
) -> str:
    return (
        _prompt(context, sources, preferences)
        + "\n\n契约修复：上一次 JSON 未通过校验。"
        + f"校验原因：{error}。请重新输出完整 JSON，不要解释。"
        + "请尽量提供 3 个完整 variants；每个 variant 至少包含一个有完整台词、"
        + "画面意图、人物和来源证据的 narrative beat。"
        + "\n上一次响应：\n"
        + json.dumps(raw, ensure_ascii=False)[:12000]
    )


def _syntax_repair_prompt(
    context: dict, sources: list[dict], error: json.JSONDecodeError,
    preferences: dict | None = None,
) -> str:
    return (
        _prompt(context, sources, preferences)
        + "\n\nJSON 语法错误：上一次响应无法解析，原因是："
        + str(error)
        + "。请重新生成完整 JSON，不要续写、不要解释、不要代码块，"
        + "并检查所有逗号、双引号、花括号和方括号是否配对。"
    )


def _combined_usage(*items: dict) -> dict:
    return {
        key: sum(int((item or {}).get(key) or 0) for item in items)
        for key in ("input_tokens", "output_tokens")
    }


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
                    preferences: dict | None = None,
                    generate_json: Callable = doubao_llm.complete_json) -> dict:
    preferences = normalize_generation_preferences(preferences)
    context, sources = _load_context(db, _text(brief_id))
    request_fingerprint = _fingerprint(context, sources, preferences)
    if not force:
        cached = db.execute(
            "SELECT * FROM script_drafts WHERE brief_id=? AND request_fingerprint=? "
            "ORDER BY variant_index", (brief_id, request_fingerprint),
        ).fetchall()
        if cached:
            return {**_rows_payload(cached), "cached": True}
    allowed_source_ids = {item["source_id"] for item in sources}
    prompt = _prompt(context, sources, preferences)
    usage: dict = {}
    normalized: dict | None = None
    for attempt in range(2):
        try:
            raw, call_usage = generate_json(
                prompt, max_tokens=6000 if attempt == 0 else 12000
            )
        except json.JSONDecodeError as syntax_error:
            if attempt == 1:
                raise ValueError(
                    "AI 连续两次返回的剧本 JSON 格式无效，请重新生成"
                ) from syntax_error
            prompt = _syntax_repair_prompt(
                context, sources, syntax_error, preferences
            )
            continue
        usage = _combined_usage(usage, call_usage)
        try:
            normalized = normalize_response(
                raw, allowed_source_ids=allowed_source_ids
            )
            break
        except ValueError as contract_error:
            if attempt == 1:
                raise
            prompt = _repair_prompt(
                context, sources, raw, contract_error, preferences
            )
    if normalized is None:
        raise ValueError("AI 剧本生成未返回可用结果，请重新生成")
    reconstruction = _reconstruction_variant(sources)
    if reconstruction:
        normalized["variants"][0] = reconstruction
        normalized["recommended_index"] = 0
        normalized["recommendation_reason"] = (
            "第一版逐段继承原视频时间线、人物、台词与画面证据，适合作为复刻基线；"
            "其余版本用于比较节奏与创意改编。"
        )
    rows = []
    for index, script in enumerate(normalized["variants"]):
        script["generation_preferences"] = preferences
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


def _broll_plan(beats: list[dict], *, max_duration: float = 12) -> list[tuple[int, dict]]:
    priority = {
        "evidence": 0, "reversal": 1, "solution": 2,
        "conflict": 3, "hook": 4, "cta": 5,
    }
    candidates = sorted(
        enumerate(beats),
        key=lambda item: (priority.get(_text(item[1].get("beat")), 99), item[0]),
    )
    selected: list[tuple[int, dict]] = []
    duration = 0.0
    for index, beat in candidates:
        beat_duration = float(beat.get("duration_sec") or 0)
        if beat_duration <= 0 or duration + beat_duration > max_duration:
            continue
        selected.append((index, beat))
        duration += beat_duration
        if len(selected) == 2:
            break
    return selected


def _split_fragment_durations(duration: float) -> list[float]:
    """Split one editorial duration into provider-safe generation units."""
    part_count = max(
        1, math.ceil(duration / MAX_PROVIDER_MASTER_DURATION_SEC)
    )
    if part_count == 1:
        return [duration]
    base = round(duration / part_count, 2)
    parts = [base] * (part_count - 1)
    parts.append(round(duration - sum(parts), 2))
    return parts


def _split_dialogue(text: str, part_count: int) -> list[str]:
    """Split dialogue without losing or rewriting any source characters."""
    if part_count <= 1:
        return [text]
    if len(text) < part_count:
        raise ValueError(
            "script beat duration is too long for its dialogue; add the full "
            "dialogue or shorten the editorial duration"
        )
    punctuation = set("，。！？；、,:：!?;")
    chunks: list[str] = []
    cursor = 0
    for part_index in range(part_count - 1):
        remaining_parts = part_count - part_index - 1
        latest = len(text) - remaining_parts
        ideal = round(len(text) * (part_index + 1) / part_count)
        candidates = [
            index + 1 for index in range(cursor, latest)
            if text[index] in punctuation
        ]
        cut = (
            min(candidates, key=lambda value: abs(value - ideal))
            if candidates else ideal
        )
        cut = max(cursor + 1, min(cut, latest))
        chunks.append(text[cursor:cut])
        cursor = cut
    chunks.append(text[cursor:])
    return chunks


def _provider_timeline_units(beats: list[dict]) -> list[dict]:
    """Expand long beats only for provider execution, preserving the script."""
    units: list[dict] = []
    for beat_index, beat in enumerate(beats):
        durations = _split_fragment_durations(float(beat["duration_sec"]))
        dialogues = _split_dialogue(_text(beat["dialogue"]), len(durations))
        for fragment_index, (duration, dialogue) in enumerate(
            zip(durations, dialogues), 1
        ):
            fragment = deepcopy(beat)
            fragment["duration_sec"] = duration
            fragment["dialogue"] = dialogue
            fragment["_source_beat_index"] = beat_index
            fragment["_fragment_index"] = fragment_index
            fragment["_fragment_count"] = len(durations)
            units.append(fragment)
    return units


def _storyboard(script: dict) -> dict:
    beats = script["beats"]
    timeline_units = _provider_timeline_units(beats)
    original_speakers = list(dict.fromkeys(
        _text(beat["speaker"]) for beat in beats
    ))
    speaker_aliases: dict[str, str] = {}
    named_speakers = [
        speaker for speaker in original_speakers if speaker != "presenter"
    ]
    if len(original_speakers) > 3 and "presenter" in original_speakers and named_speakers:
        # A generic presenter is often emitted for the opening narration of a
        # multi-character story. It is not a fourth on-screen identity: keep
        # the narration in the first named character's continuous master.
        speaker_aliases["presenter"] = named_speakers[0]

    def effective_speaker(beat: dict) -> str:
        speaker = _text(beat["speaker"])
        return speaker_aliases.get(speaker, speaker)

    speakers = list(dict.fromkeys(effective_speaker(beat) for beat in beats))
    cinematic_story = len(speakers) > 1
    # The finished story may be longer than 30 seconds, but the live Seedance
    # 2.5 task endpoint accepts at most 30 seconds per paid generation unit.
    # Split only the provider-facing master, at existing editorial beat
    # boundaries, so the script and final director timeline remain intact.
    max_master_duration = MAX_PROVIDER_MASTER_DURATION_SEC
    master_groups: list[tuple[str, list[tuple[int, dict]]]] = []
    for speaker in speakers:
        current: list[tuple[int, dict]] = []
        current_duration = 0.0
        for unit_index, beat in enumerate(timeline_units):
            if effective_speaker(beat) != speaker:
                continue
            beat_duration = float(beat["duration_sec"])
            if current and current_duration + beat_duration > max_master_duration:
                master_groups.append((speaker, current))
                current = []
                current_duration = 0.0
            current.append((unit_index, beat))
            current_duration += beat_duration
        if current:
            master_groups.append((speaker, current))
    masters = []
    master_index_by_unit: dict[int, int] = {}
    for master_idx, (speaker_id, indexed_group) in enumerate(master_groups, 1):
        group = [beat for _, beat in indexed_group]
        for unit_index, _ in indexed_group:
            master_index_by_unit[unit_index] = master_idx
        voiceover = "".join(_text(beat["dialogue"]) for beat in group)
        performance_beats = []
        master_cursor = 0.0
        for beat in group:
            duration = float(beat["duration_sec"])
            performance_beats.append({
                "beat": beat["beat"],
                "source_beat_index": beat["_source_beat_index"],
                "fragment_index": beat["_fragment_index"],
                "fragment_count": beat["_fragment_count"],
                "start": round(master_cursor, 2),
                "duration": duration,
                "dialogue": _text(beat["dialogue"]),
                "visual_intent": _text(beat["visual_intent"]),
                "broll_hint": _text(beat["broll_hint"]),
            })
            master_cursor = round(master_cursor + duration, 2)
        if cinematic_story:
            render_mode = "cinematic_character"
            shot_type = "电影化连续情境表演"
            camera = (
                "根据每个剧情节拍切换景别与机位；使用建立镜头、跟拍、环绕、"
                "动作特写和反应镜头，保持空间方向连续，禁止全程固定胸像"
            )
            scene = "；随后转入：".join(
                item["visual_intent"] for item in performance_beats
            )
            action = "；".join(
                item["visual_intent"] for item in performance_beats
            )
        else:
            render_mode = "presenter"
            shot_type = "中近景连续讲解"
            camera = "稳定胸像构图，按叙事节奏做轻微推近"
            scene = "明亮现代的设备租赁办公室，背景整洁，无可读文字、数字、商标或水印"
            action = "主讲人面对镜头自然讲解，随钩子、反转和行动号召做克制手势与表情变化"
        masters.append({
            "idx": master_idx,
            "type": "dialogue",
            "speaker": speaker_id,
            "render_mode": render_mode,
            "duration": round(sum(
                float(beat["duration_sec"]) for beat in group
            ), 2),
            "shot_type": shot_type,
            "camera": camera,
            "scene": scene,
            "action": action,
            "voiceover": voiceover,
            "subtitle": _subtitle(voiceover),
            "performance_beats": performance_beats,
        })
    broll_ranges: dict[int, float] = {}
    broll_master_idx: int | None = None
    selected_broll = _broll_plan(beats) if len(masters) < 3 else []
    if selected_broll:
        broll_master_idx = len(masters) + 1
        broll_cursor = 0.0
        visual_sequences = []
        for beat_index, beat in selected_broll:
            broll_ranges[beat_index] = round(broll_cursor, 2)
            broll_cursor += float(beat["duration_sec"])
            scene_direction = _text(
                beat.get("broll_hint") or beat.get("visual_intent")
            )
            visual_sequences.append(
                f"{beat['beat']}：具体场景要求：{scene_direction}"
            )
        prompt = (
            "生成一段无对白、无旁白的电影化 B-roll 连续蒙太奇。"
            "每个叙事段落先给出完整环境建立镜头，再进入人物动作、关键物件变化、"
            "技能效果和环境反应；具体行业场景必须清晰可辨。"
            "严格复现画面意图和辅助画面中的主体、环境与动作，"
            "禁止仅用无语义的手部特写、通用办公室或西装人物替代具体场景。"
            "使用建立镜头、跟拍、环绕和动作特写，禁止固定胸像口播；"
            "同一种景别或同一动作不得连续重复。"
            "避免可识别真人正脸，不出现可读文字、数字、商标或水印。"
            + "；随后转入：".join(visual_sequences)
        )
        masters.append({
            "idx": broll_master_idx,
            "type": "broll",
            "duration": round(broll_cursor, 2),
            "shot_type": "电影化场景与动作蒙太奇",
            "camera": "建立镜头、跟拍、环绕、动作特写与环境反应镜头",
            "scene": prompt,
            "prompt": prompt,
            "anchor_character": None,
            "asset_tags": [
                _text(beat.get("beat")) for _, beat in selected_broll
            ],
        })
    shots = []
    cursors = {
        master["idx"]: 0.0 for master in masters
        if master["type"] == "dialogue"
    }
    for index, beat in enumerate(timeline_units, 1):
        duration = float(beat["duration_sec"])
        speaker_id = effective_speaker(beat)
        unit_index = index - 1
        source_beat_index = beat["_source_beat_index"]
        master_idx = master_index_by_unit[unit_index]
        cursor = cursors[master_idx]
        broll_start = broll_ranges.get(source_beat_index)
        video_master_idx = (
            broll_master_idx if broll_start is not None else master_idx
        )
        shots.append({
            "idx": index,
            "beat": beat["beat"],
            "source_beat_index": source_beat_index,
            "fragment_index": beat["_fragment_index"],
            "fragment_count": beat["_fragment_count"],
            "duration": duration,
            "video_master_idx": video_master_idx,
            "audio_master_idx": master_idx,
            "video_start": broll_start if broll_start is not None else cursor,
            "audio_start": cursor,
            "subtitle": _subtitle(beat["dialogue"]),
            "visual_intent": beat["visual_intent"],
            "broll_hint": beat["broll_hint"],
            "source_ids": beat["source_ids"],
        })
        cursors[master_idx] = round(cursor + duration, 2)
    return {
        "production_profile": "standard",
        "production_mode": "native_master_local_director",
        "characters": [{
            "id": speaker,
            "name": "主讲人" if speaker == "presenter" else speaker,
            "gender": "unspecified",
            "look": "待在视频制作设置中绑定角色资产",
        } for speaker in speakers],
        "hook": beats[0]["dialogue"],
        "masters": masters,
        "shots": shots,
        "director_plan": {
            "strategy": (
                "dialogue_plus_broll" if broll_master_idx is not None
                else "cinematic_dialogue_only"
            ),
            "dialogue_master_count": len(master_groups),
            "broll_master_count": 1 if broll_master_idx is not None else 0,
            "broll_beats": [
                beat["beat"] for _, beat in selected_broll
            ],
            "suggested_edit_shot_count": (
                script.get("generation_preferences", {}).get(
                    "suggested_shot_count"
                )
            ),
        },
        "bgm": {
            "mood": "现代、克制、逐步明亮、无歌词",
            "prompt": "轻节奏商务音乐，不遮盖原生中文人声",
        },
        "script_metadata": {
            "title": script["title"],
            "creative_thesis": script["creative_thesis"],
            "reconstruction_mode": script.get("reconstruction_mode"),
            "generation_preferences": script.get(
                "generation_preferences", {}
            ),
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
        "SELECT d.*,b.approved_job_id AS brief_job_id,"
        "b.approved_topic_id,b.recommended_title,b.brief_json "
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
    topic_id = _text(row["approved_topic_id"])
    job_id = _text(row["brief_job_id"])
    if not topic_id and job_id:
        existing_job = db.execute(
            "SELECT topic_id FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        topic_id = _text(existing_job["topic_id"] if existing_job else "")
    if not topic_id:
        raise ValueError("approved brief has no topic id")
    job_id = job_id or f"J-{topic_id}"
    if not db.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone():
        brief = json.loads(row["brief_json"] or "{}")
        db.execute(
            "INSERT INTO jobs(job_id,topic_id,title,target_audience,priority) "
            "VALUES(?,?,?,?,?)",
            (
                job_id, row["approved_topic_id"],
                row["recommended_title"], str(brief.get("audience") or ""), 3,
            ),
        )
    job_state = db.execute(
        "SELECT state FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()["state"]
    if job_state not in ("PENDING", "STORYBOARDED"):
        raise ValueError(
            f"production job must be PENDING before script approval, got {job_state}"
        )
    storyboard = _storyboard(json.loads(row["script_json"]))
    # Approval is the production hand-off boundary. Never persist a
    # Storyboard that the worker will reject after the job has been queued.
    from factory.steps import s1_storyboard
    s1_storyboard._validate(storyboard)
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
        "UPDATE topic_briefs SET approved_job_id=?,updated_at=CURRENT_TIMESTAMP "
        "WHERE brief_id=?",
        (job_id, row["brief_id"]),
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
