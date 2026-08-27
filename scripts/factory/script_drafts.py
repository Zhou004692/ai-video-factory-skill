"""Traceable complete-script candidates generated from one approved brief."""
from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from typing import Callable

from . import creative_ir, production_config, script_sources
from .clients import doubao_llm


PROMPT_VERSION = "complete-script-v12-voice-presence-performance-fidelity"
BEAT_ORDER = ("hook", "conflict", "evidence", "reversal", "solution", "cta")
MAX_PROVIDER_MASTER_DURATION_SEC = 30.0
RELATIVE_SCENE_PREFIXES = (
    "同上", "同前", "同一上述", "延续上", "承接上",
)
PERFORMANCE_TEXT_FIELDS = (
    "emotion", "tone", "pace", "volume", "pause",
    "facial_expression", "gaze", "gesture", "body_action",
    "speaker_track_id", "voice_cluster_id", "speaker_resolution_status",
)

GENERATION_PREFERENCE_RULES = {
    "suggested_duration_sec": "建议总时长必须是正整数",
    "suggested_shot_count": "建议分镜数必须是正整数",
    "suggested_character_count": "建议角色数必须是正整数",
}


def _text(value) -> str:
    return str(value or "").strip()


def normalize_speech_mode(value, *, speaker: str = "") -> str:
    """Return whether a line is spoken on camera, off camera, or as narration."""
    return creative_ir.normalize_speech_mode(value, speaker=speaker)


def _beat_speech_mode(beat: dict) -> str:
    return normalize_speech_mode(
        beat.get("speech_mode"), speaker=_text(beat.get("speaker"))
    )


def normalize_generation_preferences(raw: dict | None) -> dict:
    """Return validated optional creative guidance, never hard output gates."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("剧本创作建议格式无效")
    normalized: dict[str, int] = {}
    for field, message in GENERATION_PREFERENCE_RULES.items():
        value = raw.get(field)
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            raise ValueError(message)
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(message) from exc
        if not numeric.is_integer() or numeric <= 0:
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
            "speech_mode": normalize_speech_mode(
                beat.get("speech_mode"), speaker=_text(beat.get("speaker"))
            ),
        }
        for field in PERFORMANCE_TEXT_FIELDS:
            value = _text(beat.get(field))
            if value:
                normalized[field] = value
        confidence = beat.get("speaker_confidence")
        if confidence not in (None, ""):
            try:
                normalized["speaker_confidence"] = round(
                    min(1.0, max(0.0, float(confidence))), 4
                )
            except (TypeError, ValueError):
                pass
        if beat.get("speaker_review_required") is True:
            normalized["speaker_review_required"] = True
        # Continuity metadata is optional.  When present it is a hard
        # production constraint, not prose for the model to reinterpret.
        for field in (
            "continuity_id", "scene_id", "scene", "location", "time_of_day",
            "site_id", "site_description",
        ):
            value = _text(beat.get(field))
            if value:
                normalized[field] = value
        cast = [
            _text(item.get("id") if isinstance(item, dict) else item)
            for item in (beat.get("characters") or beat.get("cast") or [])
        ]
        cast = [item for item in cast if item]
        if cast:
            normalized["characters"] = list(dict.fromkeys(cast))
        if beat.get("force_master_break") is True:
            normalized["force_master_break"] = True
        if beat.get("broll_required") is True:
            normalized["broll_required"] = True
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
    characters: list[dict] = []
    for item in raw.get("characters") or []:
        if not isinstance(item, dict):
            continue
        character_id = _text(item.get("id") or item.get("name"))
        if not character_id:
            continue
        characters.append({
            "id": character_id,
            "name": _text(item.get("name")) or character_id,
            "gender": _text(item.get("gender")) or "unspecified",
            "look": _text(item.get("look") or item.get("image_prompt")),
            "background_description": _text(
                item.get("background_description") or item.get("role")
            ),
        })
    total = round(sum(beat["duration_sec"] for beat in beats), 2)
    # The total is derived from normalized beats. Model-provided and manually
    # edited aggregate fields are hints, never an independent source of truth.
    declared = total
    result = {
        "title": title,
        "creative_thesis": thesis,
        "target_duration_sec": declared,
        "total_duration_sec": total,
        "beats": beats,
    }
    if characters:
        result["characters"] = characters
    return result


def normalize_response(raw: dict, *, allowed_source_ids: set[str]) -> dict:
    variants_raw = raw.get("variants") or []
    if not isinstance(variants_raw, list) or not variants_raw:
        raise ValueError("script generation requires at least 1 usable variant")
    variants: list[dict] = []
    variant_errors: list[ValueError] = []
    for item in variants_raw[:3]:
        try:
            variants.append(_normalize_variant(
                item or {}, allowed_source_ids=allowed_source_ids
            ))
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ValueError):
                variant_errors.append(exc)
            continue
    if not variants:
        if variant_errors:
            raise variant_errors[0]
        raise ValueError("script generation requires at least 1 usable variant")
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
    previous_scene = ""
    for segment_index, segment in enumerate(analysis.get("segments") or [], 1):
        start = max(0.0, float(segment.get("start_sec") or 0))
        end = max(start, float(segment.get("end_sec") or start))
        if end <= start:
            continue
        characters = [
            _text(item) for item in segment.get("characters") or []
            if _text(item)
        ]
        dialogue = _text(segment.get("dialogue"))
        speaker = _text(segment.get("speaker"))
        if not speaker:
            speaker = _text(segment.get("speaker_track_id"))
        if not speaker:
            voice_cluster_id = _text(segment.get("voice_cluster_id"))
            stable_suffix = re.sub(
                r"[^A-Za-z0-9_-]+", "-", voice_cluster_id
            ).strip("-")
            speaker = (
                f"unresolved_voice_{stable_suffix}"
                if stable_suffix else f"unresolved_speaker_{segment_index:03d}"
            )
        raw_scene = _text(segment.get("scene"))
        compact_scene = re.sub(r"\s+", "", raw_scene)
        relative_scene = compact_scene.startswith(RELATIVE_SCENE_PREFIXES)
        scene = (
            previous_scene
            if previous_scene and (not raw_scene or relative_scene)
            else raw_scene
        )
        if scene:
            previous_scene = scene
        resolved_segment = {**segment, "scene": scene}
        supporting = "；".join(
            item for item in (
                _text(segment.get("on_screen_text")),
                _text(segment.get("audio")),
            ) if item
        )
        reconstructed = {
            "beat": _text(segment.get("role")) or "other",
            "speaker": speaker,
            "dialogue": dialogue,
            "duration_sec": round(end - start, 3),
            "visual_intent": _segment_visual_intent(resolved_segment),
            "source_ids": [source["source_id"]],
            "broll_hint": supporting,
            "source_start_sec": round(start, 3),
            "source_end_sec": round(end, 3),
            "scene": scene,
            "characters": characters,
            "action": _text(segment.get("action")),
            "camera": _text(segment.get("camera")),
            "on_screen_text": _text(segment.get("on_screen_text")),
            "audio": _text(segment.get("audio")),
            "speech_mode": normalize_speech_mode(
                segment.get("speech_mode"), speaker=speaker
            ),
            "speaker_review_required": bool(
                segment.get("speaker_review_required")
                or not _text(segment.get("speaker"))
            ),
        }
        for field in PERFORMANCE_TEXT_FIELDS:
            value = _text(segment.get(field))
            if value:
                reconstructed[field] = value
        confidence = segment.get("speaker_confidence")
        if confidence not in (None, ""):
            try:
                reconstructed["speaker_confidence"] = round(
                    min(1.0, max(0.0, float(confidence))), 4
                )
            except (TypeError, ValueError):
                pass
        beats.append(reconstructed)
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
    prompt_sources = deepcopy(sources)
    for source in prompt_sources:
        source["content"] = script_sources.compact_text_for_prompt(
            source.get("content") or "", max_chars=60000
        )
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
            "characters": [{
                "id": "character_id",
                "name": "character name",
                "gender": "male|female|unspecified",
                "look": "age, ethnicity, hair, clothing and carried equipment",
                "background_description": "role, responsibility, personality and relationships",
            }],
            "beats": [{
                "beat": "hook|conflict|evidence|reversal|solution|cta",
                "speaker": "character_id",
                "dialogue": "口语化台词",
                "duration_sec": 4,
                "visual_intent": "画面意图",
                "source_ids": ["SRC-..."],
                "continuity_id": "same-scene-stable-id",
                "site_id": "same-project-site-id-or-empty",
                "site_description": "同一具体项目现场的稳定描述；没有则留空",
                "scene": "具体地点、时段与空间状态",
                "characters": ["character_id"],
                "emotion": "角色当下情绪",
                "tone": "语气与表达方式",
                "pace": "语速",
                "volume": "音量",
                "pause": "停顿位置与长度",
                "facial_expression": "表情变化",
                "gaze": "视线对象与变化",
                "gesture": "手势",
                "body_action": "身体动作与走位",
                "speaker_confidence": 1.0,
                "speaker_review_required": False,
                "broll_required": False,
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
        "每个有人物表演的 beat 必须保留或明确 emotion、tone、pace、volume、pause、"
        "facial_expression、gaze、gesture 与 body_action；本地视频来源已有这些字段时不得删减或泛化。"
        "speaker_review_required=true 表示说话人仍未核实，不得擅自把该台词改分给第一个出镜人物。"
        "visual_intent 必须是可直接导演执行的画面，至少写清场景空间、人物走位、动作或技能展示、"
        "关键物件变化以及景别与运镜；不得只写‘人物讲解’或泛化氛围。"
        "多人剧情不得默认写成办公室站桩口播，必须让人物在剧情场景中行动并由镜头参与叙事；"
        "单人知识讲解才允许使用稳定口播构图。"
        "每个 beat 必须给出 continuity_id、scene 和 characters：同一地点、时段、人物关系和剧情目标连续时复用同一 continuity_id；"
        "只有地点或时段跳变、核心出场组合变化、剧情目标切换，或高风险复杂动作需要独立生成时才更换 continuity_id。"
        "若多个 beat 明确发生在同一个具体项目现场、园区或建筑群，但属于装卸区、产线、调度室等不同子空间，"
        "必须复用同一个 site_id，并用 site_description 描述这个项目现场；这些 beat 的 scene 和 continuity_id 仍保持各自独立。"
        "只有泛称相似但并非同一具体现场的工厂、办公室或仓库，不得复用 site_id；不存在上层现场关系时 site_id 留空。"
        "不要按说话人或固定秒数更换 continuity_id。broll_hint 只是本地导演剪辑建议，不会自动创建付费母片。"
        "broll_required=true 等价于授权新增一条独立付费 B-roll 母片；仅当该画面提供台词之外不可缺少的证据、实物或流程信息，"
        "并且无法在对应对白母片的 visual_intent 中完成时才设为 true；只为补充景别、特写、环境空镜或重复对白母片已有动作时必须为 false。"
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
        + json.dumps(prompt_sources, ensure_ascii=False)
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
        (
            (index, beat) for index, beat in enumerate(beats)
            if beat.get("broll_required") is True
        ),
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


def _explicit_continuity_key(beat: dict) -> str | None:
    """Return a stable hard-boundary key when the script provides one."""
    continuity_id = _text(beat.get("continuity_id"))
    scene_value = _text(
        beat.get("scene_id") or beat.get("location") or beat.get("scene")
    ).casefold()
    if continuity_id:
        # A model-provided continuity id is useful but cannot override a
        # declared physical-location change.  Merging two locations into one
        # paid master makes both character blocking and scene references
        # impossible to satisfy.
        key = f"continuity:{continuity_id.casefold()}"
        return f"{key};scene:{scene_value}" if scene_value else key
    components = [
        ("scene", scene_value),
        ("time", _text(beat.get("time_of_day")).casefold()),
    ]
    cast = sorted({
        _text(item.get("id") if isinstance(item, dict) else item).casefold()
        for item in (beat.get("characters") or beat.get("cast") or [])
        if _text(item.get("id") if isinstance(item, dict) else item)
    })
    if cast:
        components.append(("cast", "|".join(cast)))
    populated = [f"{name}:{value}" for name, value in components if value]
    return ";".join(populated) or None


def _scene_anchor_key(continuity_key: str) -> str:
    digest = hashlib.sha256(continuity_key.encode("utf-8")).hexdigest()[:12]
    return f"scene-{digest}"


def _site_anchor_metadata(beats: list[dict]) -> dict:
    """Carry only an explicit shared project-site identity across subspaces."""
    site_ids = list(dict.fromkeys(
        _text(beat.get("site_id")).casefold()
        for beat in beats if _text(beat.get("site_id"))
    ))
    if len(site_ids) != 1:
        return {}
    site_id = site_ids[0]
    description = next((
        _text(beat.get("site_description"))
        for beat in beats if _text(beat.get("site_description"))
    ), site_id)
    digest = hashlib.sha256(site_id.encode("utf-8")).hexdigest()[:12]
    return {
        "site_anchor_key": f"site-{digest}",
        "site_anchor_description": description,
    }


def _scene_description(beats: list[dict]) -> str:
    values = [
        _text(beat.get("scene") or beat.get("location"))
        for beat in beats
        if _text(beat.get("scene") or beat.get("location"))
    ]
    return "；".join(dict.fromkeys(values)) or _text(beats[0].get("visual_intent"))


def _continuity_segments(
    timeline_units: list[dict],
) -> list[tuple[str, list[tuple[int, dict]]]]:
    """Group adjacent beats by story continuity, never by speaker identity."""
    segments: list[tuple[str, list[tuple[int, dict]]]] = []
    current_key = "continuous:default"
    current_explicit = False
    current: list[tuple[int, dict]] = []
    for unit_index, beat in enumerate(timeline_units):
        explicit_key = _explicit_continuity_key(beat)
        force_break = beat.get("force_master_break") is True
        boundary = bool(current) and (
            force_break
            or (
                explicit_key is not None
                and current_explicit
                and explicit_key != current_key
            )
            or (explicit_key is not None and not current_explicit)
        )
        if boundary:
            segments.append((current_key, current))
            current = []
        if not current:
            current_key = explicit_key or "continuous:default"
            current_explicit = explicit_key is not None
        current.append((unit_index, beat))
    if current:
        segments.append((current_key, current))
    return segments


def _balanced_master_groups(
    indexed_units: list[tuple[int, dict]],
    *,
    max_duration: float = MAX_PROVIDER_MASTER_DURATION_SEC,
) -> list[list[tuple[int, dict]]]:
    """Partition a continuous segment without producing a greedy short tail."""
    if not indexed_units:
        return []
    durations = [float(beat["duration_sec"]) for _, beat in indexed_units]
    total = sum(durations)
    group_count = max(1, math.ceil(total / max_duration))
    if group_count == 1:
        return [indexed_units]

    prefix = [0.0]
    for duration in durations:
        prefix.append(prefix[-1] + duration)

    # Dynamic programming keeps editorial beat boundaries and minimizes
    # deviation from an even split.  This turns 36 seconds into 18+18 rather
    # than a fragile 30+6 paid tail.  If indivisible beats make the theoretical
    # ceil(total/30) count impossible (20+20+20), increase only to the minimum
    # feasible count instead of failing the production plan.
    solution: tuple[float, list[int]] | None = None
    while group_count <= len(indexed_units) and solution is None:
        target = total / group_count
        dp: dict[tuple[int, int], tuple[float, list[int]]] = {
            (0, 0): (0.0, [])
        }
        for groups_used in range(1, group_count + 1):
            for end in range(groups_used, len(indexed_units) + 1):
                best: tuple[float, list[int]] | None = None
                for start in range(groups_used - 1, end):
                    prior = dp.get((groups_used - 1, start))
                    if prior is None:
                        continue
                    duration = prefix[end] - prefix[start]
                    if duration > max_duration + 1e-6:
                        continue
                    cost = prior[0] + (duration - target) ** 2
                    candidate = (cost, [*prior[1], end])
                    if best is None or candidate[0] < best[0]:
                        best = candidate
                if best is not None:
                    dp[(groups_used, end)] = best
        solution = dp.get((group_count, len(indexed_units)))
        if solution is None:
            group_count += 1
    if solution is None:
        raise ValueError(
            "continuous segment cannot be partitioned into provider-safe masters"
        )
    groups: list[list[tuple[int, dict]]] = []
    start = 0
    for end in solution[1]:
        groups.append(indexed_units[start:end])
        start = end
    return groups


def _storyboard(script: dict) -> dict:
    requested_beats = script["beats"]
    beats = deepcopy(requested_beats)
    # Older cached reconstruction drafts can retain relative descriptions
    # such as "同上述场景".  Resolve them before continuity grouping so a
    # silent lead-in and its following line do not become separate paid
    # masters, leaving the first one with an empty voiceover.
    previous_scene = ""
    for beat in beats:
        beat["speech_mode"] = normalize_speech_mode(
            beat.get("speech_mode"), speaker=_text(beat.get("speaker"))
        )
        raw_scene = _text(beat.get("scene") or beat.get("location"))
        compact_scene = re.sub(r"\s+", "", raw_scene)
        if previous_scene and (
            not raw_scene
            or compact_scene.startswith(RELATIVE_SCENE_PREFIXES)
        ):
            beat["scene"] = previous_scene
        elif raw_scene:
            beat["scene"] = raw_scene
            previous_scene = raw_scene
    duration_adjustments = []
    for index, beat in enumerate(beats):
        requested_duration = float(beat["duration_sec"])
        minimum_duration = estimate_dialogue_duration(beat.get("dialogue"))
        source_timing_locked = bool(
            script.get("reconstruction_mode") == "source_timeline"
            or (
                beat.get("source_start_sec") is not None
                and beat.get("source_end_sec") is not None
            )
        )
        production_duration = (
            requested_duration
            if source_timing_locked
            else max(requested_duration, minimum_duration)
        )
        beat["duration_sec"] = production_duration
        if production_duration > requested_duration + 1e-6:
            duration_adjustments.append({
                "source_beat_index": index,
                "requested_duration_sec": requested_duration,
                "minimum_speech_duration_sec": minimum_duration,
                "production_duration_sec": production_duration,
            })
    timeline_units = _provider_timeline_units(beats)
    original_audio_speakers = list(dict.fromkeys(
        _text(beat["speaker"]) for beat in beats
    ))
    speaker_aliases: dict[str, str] = {}
    named_speakers = [
        speaker for speaker in original_audio_speakers if speaker != "presenter"
    ]
    if len(original_audio_speakers) > 3 and "presenter" in original_audio_speakers and named_speakers:
        # A generic presenter is often emitted for the opening narration of a
        # multi-character story. It is not a fourth on-screen identity: keep
        # the narration in the first named character's continuous master.
        speaker_aliases["presenter"] = named_speakers[0]

    def effective_speaker(beat: dict) -> str:
        speaker = _text(beat["speaker"])
        return speaker_aliases.get(speaker, speaker)

    on_screen_speakers = list(dict.fromkeys(
        effective_speaker(beat) for beat in beats
        if _beat_speech_mode(beat) == "on_screen"
    ))
    declared_characters = list(dict.fromkeys(
        _text(character.get("id") if isinstance(character, dict) else character)
        for beat in beats
        for character in (beat.get("characters") or beat.get("cast") or [])
        if _text(character.get("id") if isinstance(character, dict) else character)
    ))
    visible_character_ids = list(dict.fromkeys(
        [*declared_characters, *on_screen_speakers]
    ))
    cinematic_story = bool(
        script.get("reconstruction_mode") == "source_timeline"
        or len(visible_character_ids) > 1
    )
    # A paid master is a continuous dramatic scene, not a speaker bucket and
    # not an arbitrary ten-second slice.  Seedance 2.5's 30 seconds is a hard
    # maximum.  Only explicit continuity changes or the provider limit split
    # a scene, and long scenes are balanced to avoid wasteful short tails.
    master_groups: list[tuple[str, list[tuple[int, dict]]]] = []
    for continuity_key, segment in _continuity_segments(timeline_units):
        for group in _balanced_master_groups(segment):
            master_groups.append((continuity_key, group))
    masters = []
    master_index_by_unit: dict[int, int] = {}
    for master_idx, (continuity_key, indexed_group) in enumerate(master_groups, 1):
        group = [beat for _, beat in indexed_group]
        scene_anchor_key = _scene_anchor_key(continuity_key)
        scene_anchor_description = _scene_description(group)
        site_anchor = _site_anchor_metadata(group)
        group_audio_speakers = list(dict.fromkeys(
            effective_speaker(beat) for beat in group
        ))
        group_speakers = list(dict.fromkeys(
            effective_speaker(beat) for beat in group
            if _beat_speech_mode(beat) == "on_screen"
        ))
        group_cast: list[str] = []
        for beat in group:
            cast_candidates = list(
                beat.get("characters") or beat.get("cast") or []
            )
            if _beat_speech_mode(beat) == "on_screen":
                cast_candidates.append(effective_speaker(beat))
            for character_id in cast_candidates:
                normalized = _text(
                    character_id.get("id")
                    if isinstance(character_id, dict) else character_id
                )
                if normalized and normalized not in group_cast:
                    group_cast.append(normalized)
        if not group_speakers and group_cast:
            group_speakers = [group_cast[0]]
        speaker_id = (
            group_speakers[0] if group_speakers
            else group_audio_speakers[0]
        )
        for unit_index, _ in indexed_group:
            master_index_by_unit[unit_index] = master_idx
        voiceover = "".join(_text(beat["dialogue"]) for beat in group)
        has_dialogue = bool(voiceover.strip())
        performance_beats = []
        master_cursor = 0.0
        for beat in group:
            duration = float(beat["duration_sec"])
            performance_beats.append({
                "beat": beat["beat"],
                "speaker": effective_speaker(beat),
                "speech_mode": _beat_speech_mode(beat),
                "source_beat_index": beat["_source_beat_index"],
                "fragment_index": beat["_fragment_index"],
                "fragment_count": beat["_fragment_count"],
                "start": round(master_cursor, 2),
                "duration": duration,
                "dialogue": _text(beat["dialogue"]),
                "visual_intent": _text(beat["visual_intent"]),
                "broll_hint": _text(beat["broll_hint"]),
                **({
                    "source_start_sec": beat["source_start_sec"],
                    "source_end_sec": beat["source_end_sec"],
                } if beat.get("source_start_sec") is not None
                and beat.get("source_end_sec") is not None else {}),
                **{
                    field: beat[field]
                    for field in PERFORMANCE_TEXT_FIELDS
                    if beat.get(field) not in (None, "")
                },
                **({
                    "speaker_confidence": beat["speaker_confidence"]
                } if beat.get("speaker_confidence") is not None else {}),
                "speaker_review_required": bool(
                    beat.get("speaker_review_required")
                ),
            })
            master_cursor = round(master_cursor + duration, 2)
        if cinematic_story:
            render_mode = "cinematic_character"
            shot_type = "电影化连续情境表演"
            camera = (
                "采用单一连续运动路径：先用建立镜头交代空间，再随剧情缓慢推近、"
                "跟移或环绕；每个动作只做一次有动机的景别变化，切换机位必须借助"
                "人物动作、前景遮挡或视线变化，结束前预留稳定画面；保持180度轴线"
                "和空间方向，禁止无动机跳切、反复推拉及全程固定胸像"
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
            camera = (
                "采用单一连续运动路径：稳定中近景起步，按叙事节奏缓慢推近，"
                "在观点落点前停止运动并稳定收束，禁止反复推拉或无动机跳切"
            )
            scene = "明亮现代的设备租赁办公室，背景整洁，无可读文字、数字、商标或水印"
            action = "主讲人面对镜头自然讲解，随钩子、反转和行动号召做克制手势与表情变化"
        if not has_dialogue:
            render_mode = "broll"
            shot_type = "电影化无对白场景镜头"
            camera = "建立镜头、自然跟拍或克制推进，保留环境与人物动作连续性"
            scene = "；随后转入：".join(
                item["visual_intent"] for item in performance_beats
            )
            action = "；".join(
                item["visual_intent"] for item in performance_beats
            )
        masters.append({
            "idx": master_idx,
            "type": "dialogue" if has_dialogue else "broll",
            "speaker": speaker_id if has_dialogue else "",
            "speakers": group_speakers if has_dialogue else [],
            "audio_speakers": group_audio_speakers if has_dialogue else [],
            "cast": group_cast,
            "continuity_key": continuity_key,
            "scene_anchor_key": scene_anchor_key,
            "scene_anchor_description": scene_anchor_description,
            **site_anchor,
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
            **({
                "prompt": scene,
                "anchor_character": (
                    group_cast[0] if len(group_cast) == 1 else None
                ),
            } if not has_dialogue else {}),
        })
    broll_ranges: dict[int, tuple[int, float]] = {}
    broll_master_indexes: list[int] = []
    selected_broll = _broll_plan(beats)
    if selected_broll:
        broll_groups: list[tuple[str, list[tuple[int, dict]]]] = []
        for beat_index, beat in selected_broll:
            key = _explicit_continuity_key(beat) or "continuous:default"
            if broll_groups and broll_groups[-1][0] == key:
                broll_groups[-1][1].append((beat_index, beat))
            else:
                broll_groups.append((key, [(beat_index, beat)]))
        for continuity_key, selected_group in broll_groups:
            broll_master_idx = len(masters) + 1
            broll_master_indexes.append(broll_master_idx)
            broll_cursor = 0.0
            visual_sequences = []
            group_beats = [beat for _, beat in selected_group]
            site_anchor = _site_anchor_metadata(group_beats)
            cast: list[str] = []
            for beat_index, beat in selected_group:
                broll_ranges[beat_index] = (
                    broll_master_idx, round(broll_cursor, 2)
                )
                broll_cursor += float(beat["duration_sec"])
                scene_direction = _text(
                    beat.get("broll_hint") or beat.get("visual_intent")
                )
                visual_sequences.append(
                    f"{beat['beat']}：具体场景要求：{scene_direction}"
                )
                for character_id in [
                    *(beat.get("characters") or beat.get("cast") or []),
                    beat.get("speaker"),
                ]:
                    normalized = _text(
                        character_id.get("id")
                        if isinstance(character_id, dict) else character_id
                    )
                    if normalized and normalized not in cast:
                        cast.append(normalized)
            cast_rule = (
                f"允许出镜角色仅限：{'、'.join(cast)}；所有角色必须沿用各自定妆身份参考，"
                "不得生成未列角色、替身或随机路人。"
                if cast else
                "画面不得出现可识别人物正脸或随机路人。"
            )
            prompt = (
                "生成一段无对白、无旁白的电影化 B-roll 连续蒙太奇。"
                "每个叙事段落先给出完整环境建立镜头，再进入人物动作、关键物件变化、"
                "技能效果和环境反应；具体行业场景必须清晰可辨。"
                "严格复现画面意图和辅助画面中的主体、环境与动作，"
                "禁止仅用无语义的手部特写、通用办公室或西装人物替代具体场景。"
                "使用建立镜头、跟拍、环绕和动作特写，禁止固定胸像口播；"
                "同一种景别或同一动作不得连续重复。"
                f"{cast_rule}"
                "不出现悬浮文案、可读文字、数字、商标、界面、标识或水印。"
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
                "continuity_key": continuity_key,
                "scene_anchor_key": _scene_anchor_key(continuity_key),
                "scene_anchor_description": _scene_description(group_beats),
                **site_anchor,
                "cast": cast,
                "anchor_character": cast[0] if len(cast) == 1 else None,
                "asset_tags": [
                    _text(beat.get("beat")) for _, beat in selected_group
                ],
            })
    shots = []
    cursors = {master["idx"]: 0.0 for master in masters}
    for index, beat in enumerate(timeline_units, 1):
        duration = float(beat["duration_sec"])
        speaker_id = effective_speaker(beat)
        unit_index = index - 1
        source_beat_index = beat["_source_beat_index"]
        master_idx = master_index_by_unit[unit_index]
        cursor = cursors[master_idx]
        broll_target = broll_ranges.get(source_beat_index)
        video_master_idx = broll_target[0] if broll_target else master_idx
        broll_start = broll_target[1] if broll_target else None
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
            **{
                field: beat[field]
                for field in PERFORMANCE_TEXT_FIELDS
                if beat.get(field) not in (None, "")
            },
            **({
                "speaker_confidence": beat["speaker_confidence"]
            } if beat.get("speaker_confidence") is not None else {}),
            "speaker_review_required": bool(
                beat.get("speaker_review_required")
            ),
        })
        cursors[master_idx] = round(cursor + duration, 2)

    master_by_idx = {int(master["idx"]): master for master in masters}
    for shot_index, shot in enumerate(shots):
        shot["transition_duration_sec"] = 0.0
        shot["preserve_audio_continuity"] = False
        if shot_index == 0:
            shot["continuity_relation"] = "opening"
            shot["transition_type"] = "none"
            shot["transition_reason"] = "首镜直接进入，不添加无意义转场"
            continue

        previous = shots[shot_index - 1]
        current_video_idx = int(shot["video_master_idx"])
        current_audio_idx = int(shot["audio_master_idx"])
        previous_video_idx = int(previous["video_master_idx"])
        previous_audio_idx = int(previous["audio_master_idx"])
        current_video = master_by_idx.get(current_video_idx, {})
        previous_video = master_by_idx.get(previous_video_idx, {})
        current_key = str(current_video.get("continuity_key") or "")
        previous_key = str(previous_video.get("continuity_key") or "")
        audio_contiguous = (
            current_audio_idx == previous_audio_idx
            and abs(
                float(shot.get("audio_start") or 0)
                - (
                    float(previous.get("audio_start") or 0)
                    + float(previous["duration"])
                )
            ) <= 0.05
        )
        picture_is_broll = current_video_idx != current_audio_idx
        leaving_broll = (
            previous_video_idx != previous_audio_idx and audio_contiguous
        )
        if picture_is_broll or leaving_broll:
            shot["continuity_relation"] = "audio_bridge"
            shot["transition_type"] = "cut"
            shot["transition_reason"] = (
                "对白音轨连续，用有语义的辅助画面完成视觉切换"
            )
            shot["preserve_audio_continuity"] = True
            continue

        video_contiguous = (
            current_video_idx == previous_video_idx
            and abs(
                float(shot.get("video_start") or 0)
                - (
                    float(previous.get("video_start") or 0)
                    + float(previous["duration"])
                )
            ) <= 0.05
        )
        if video_contiguous and audio_contiguous:
            shot["continuity_relation"] = "same_master_continuation"
            shot["transition_type"] = "cut"
            shot["transition_reason"] = (
                "同一母片按时间连续推进，保留原生运镜和表演"
            )
            shot["preserve_audio_continuity"] = True
        elif current_key and current_key == previous_key:
            shot["continuity_relation"] = "same_scene_angle_change"
            shot["transition_type"] = "match_cut"
            shot["transition_reason"] = (
                "同场景换机位，沿用人物站位、视线和180度轴线"
            )
        else:
            shot["continuity_relation"] = "scene_change"
            shot["transition_type"] = "dip_to_black"
            shot["transition_duration_sec"] = 0.12
            shot["transition_reason"] = (
                "场景发生变化，用极短暗场建立新空间"
            )
    character_profiles = {
        _text(item.get("id")): item
        for item in (script.get("characters") or [])
        if isinstance(item, dict) and _text(item.get("id"))
    }
    storyboard_characters = []
    for speaker in visible_character_ids:
        profile = deepcopy(character_profiles.get(speaker) or {})
        profile["id"] = speaker
        profile["name"] = _text(profile.get("name")) or speaker
        profile["gender"] = _text(profile.get("gender")) or "unspecified"
        profile["look"] = _text(profile.get("look")) or "按剧本人物设定自动定妆"
        if _text(profile.get("background_description")):
            profile["background_description"] = _text(
                profile["background_description"]
            )
        storyboard_characters.append(profile)
    return {
        "production_profile": "standard",
        "production_mode": "native_master_local_director",
        "characters": storyboard_characters,
        "hook": beats[0]["dialogue"],
        "masters": masters,
        "shots": shots,
        "director_plan": {
            "master_grouping": "content_continuity",
            "provider_max_master_duration_sec": MAX_PROVIDER_MASTER_DURATION_SEC,
            "strategy": (
                "dialogue_plus_broll"
                if any(master["type"] == "broll" for master in masters)
                else "cinematic_dialogue_only"
            ),
            "dialogue_master_count": sum(
                master["type"] == "dialogue" for master in masters
            ),
            "broll_master_count": sum(
                master["type"] == "broll" for master in masters
            ),
            "paid_master_count": len(masters),
            "local_edit_shot_count": len(timeline_units),
            "speech_duration_adjusted": bool(duration_adjustments),
            "speech_duration_adjustments": duration_adjustments,
            "requested_duration_sec": round(sum(
                float(beat["duration_sec"]) for beat in requested_beats
            ), 2),
            "production_duration_sec": round(sum(
                float(beat["duration_sec"]) for beat in beats
            ), 2),
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
    execution_engine = production_config.resolve_default_execution_engine()
    if not db.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone():
        brief = json.loads(row["brief_json"] or "{}")
        db.execute(
            "INSERT INTO jobs(job_id,topic_id,title,target_audience,priority,"
            "execution_engine) VALUES(?,?,?,?,?,?)",
            (
                job_id, row["approved_topic_id"],
                row["recommended_title"], str(brief.get("audience") or ""), 3,
                execution_engine,
            ),
        )
    job_state = db.execute(
        "SELECT state FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()["state"]
    if job_state not in ("PENDING", "STORYBOARDED"):
        raise ValueError(
            f"production job must be PENDING before script approval, got {job_state}"
        )
    script = json.loads(row["script_json"])
    storyboard = _storyboard(script)
    # Approval is the production hand-off boundary. Never persist a
    # Storyboard that the worker will reject after the job has been queued.
    from factory.steps import s1_storyboard
    s1_storyboard._validate(storyboard)
    # The production-ready storyboard is the first artifact with a complete,
    # canonical character table. Reconstruction scripts may intentionally
    # keep identities only on beats, so deriving Creative IR from the raw
    # script can leave every V2 beat pointing at an empty character table.
    creative = creative_ir.from_storyboard(storyboard)
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
        "UPDATE jobs SET storyboard=?,creative_ir_json=?,creative_ir_version=?,"
        "state='STORYBOARDED',execution_engine=?,"
        "updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
        (
            storyboard_json,
            json.dumps(creative.as_dict(), ensure_ascii=False),
            creative.version,
            execution_engine,
            job_id,
        ),
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
