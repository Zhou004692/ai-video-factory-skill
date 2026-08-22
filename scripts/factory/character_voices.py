"""Deterministic role-to-Volcengine-voice matching and persistent binding."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sqlite3
from typing import Iterable


class VoiceMatchError(ValueError):
    """No catalog voice satisfies an explicit role constraint."""


class VoiceBindingRequiredError(RuntimeError):
    """A named speaking role has no safe fixed voice binding."""


@dataclass(frozen=True)
class VoiceMatch:
    voice_type: str
    voice_name: str
    resource_id: str
    preview_url: str
    score: int
    reasons: tuple[str, ...]
    speaker: dict


def ensure_schema(database: sqlite3.Connection) -> None:
    database.executescript(
        """
        CREATE TABLE IF NOT EXISTS character_voice_bindings (
            character_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'volcengine_tts',
            resource_id TEXT NOT NULL DEFAULT 'seed-tts-2.0',
            voice_type TEXT NOT NULL,
            voice_name TEXT NOT NULL DEFAULT '',
            preview_url TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL CHECK(source IN ('auto','manual')),
            match_score INTEGER NOT NULL DEFAULT 0,
            match_reasons_json TEXT NOT NULL DEFAULT '[]',
            profile_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'Active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_character_voice_type
            ON character_voice_bindings(voice_type);
        """
    )
    database.commit()


def _normal_gender(value: object) -> str:
    text = str(value or "").strip().casefold()
    if text in {"female", "女", "女性", "woman", "girl"}:
        return "female"
    if text in {"male", "男", "男性", "man", "boy"}:
        return "male"
    return ""


def _flatten(value: object) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten(item) for item in value)
    return str(value or "")


def _profile(character: dict) -> dict:
    age_value = character.get("age") or character.get("visual_age") or ""
    try:
        age = int(age_value)
    except (TypeError, ValueError):
        age = 0
    text = " ".join(str(character.get(field) or "") for field in (
        "name", "script_character_name", "look", "occupation",
        "personality", "background_description", "voice_directive",
    )).casefold()
    return {
        "gender": _normal_gender(character.get("gender")),
        "age": age,
        "text": text,
        "language": str(character.get("language") or "中文").casefold(),
    }


def _contains_any(text: str, words: Iterable[str]) -> list[str]:
    return [word for word in words if word in text]


def match_voice(character: dict, speakers: list[dict]) -> VoiceMatch:
    profile = _profile(character)
    candidates = [
        speaker for speaker in speakers
        if str(speaker.get("VoiceType") or "").strip()
    ]
    if profile["gender"]:
        candidates = [
            speaker for speaker in candidates
            if _normal_gender(speaker.get("Gender")) == profile["gender"]
        ]
        if not candidates:
            raise VoiceMatchError("火山声线库中没有符合角色性别的可用声线")
    if not candidates:
        raise VoiceMatchError("火山声线库没有可用声线")

    professional = ("专业", "沉稳", "清晰", "知性", "干练", "纪实", "工程", "职场")
    energetic = ("活泼", "开朗", "明快", "有力量", "果断", "爽朗")
    sweet = ("甜美", "甜腻", "撒娇", "可爱", "软萌")
    young = ("青年", "年轻", "少年", "少女")

    scored: list[tuple[int, str, tuple[str, ...], dict]] = []
    for speaker in candidates:
        catalog_text = " ".join(str(speaker.get(field) or "") for field in (
            "Name", "Gender", "Age", "Description"
        )) + " " + " ".join(_flatten(speaker.get(field)) for field in (
            "Languages", "Categories", "NormalLabels", "SpecialLabels",
        ))
        catalog_text = catalog_text.casefold()
        score = 0
        reasons: list[str] = []
        if profile["gender"]:
            score += 80
            reasons.append("性别匹配")
        if "中文" in catalog_text or "zh" in str(
            speaker.get("VoiceType") or ""
        ).casefold():
            score += 20
            reasons.append("中文普通话")
        if profile["age"] and profile["age"] <= 35 and _contains_any(
            catalog_text, young
        ):
            score += 30
            reasons.append("年龄气质匹配")
        role_text = profile["text"]
        professional_hits = _contains_any(role_text, professional)
        if professional_hits:
            overlap = set(professional_hits) & set(
                _contains_any(catalog_text, professional)
            )
            score += 18 * len(overlap)
            if overlap:
                reasons.append("专业气质：" + "、".join(sorted(overlap)))
        energetic_hits = set(_contains_any(role_text, energetic)) & set(
            _contains_any(catalog_text, energetic)
        )
        score += 12 * len(energetic_hits)
        if energetic_hits:
            reasons.append("表达气质：" + "、".join(sorted(energetic_hits)))
        if any(term in role_text for term in ("不甜", "不甜腻", "克制")):
            penalty = 25 * len(_contains_any(catalog_text, sweet))
            score -= penalty
        # Exact tokens from the role description are weaker than the curated
        # dimensions above, but help distinguish industry/scenario voices.
        tokens = {
            token for token in re.split(r"[^\w\u4e00-\u9fff]+", role_text)
            if len(token) >= 2
        }
        token_hits = sorted(token for token in tokens if token in catalog_text)
        score += min(24, 4 * len(token_hits))
        if token_hits:
            reasons.append("角色标签：" + "、".join(token_hits[:4]))
        voice_type = str(speaker.get("VoiceType") or "").strip()
        scored.append((score, voice_type, tuple(reasons), speaker))
    score, voice_type, reasons, speaker = sorted(
        scored, key=lambda item: (-item[0], item[1])
    )[0]
    return VoiceMatch(
        voice_type=voice_type,
        voice_name=str(speaker.get("Name") or voice_type),
        resource_id=str(speaker.get("ResourceID") or "seed-tts-2.0"),
        preview_url=str(
            speaker.get("TrialURL") or speaker.get("ShortTrialURL") or ""
        ),
        score=score,
        reasons=reasons,
        speaker=speaker,
    )


def get_binding(database: sqlite3.Connection, character_id: str):
    ensure_schema(database)
    row = database.execute(
        "SELECT * FROM character_voice_bindings WHERE character_id=? "
        "AND status='Active'", (str(character_id or "").strip(),),
    ).fetchone()
    return dict(row) if row else None


def _save(
    database: sqlite3.Connection, *, character_id: str, display_name: str,
    voice_type: str, voice_name: str, resource_id: str, preview_url: str,
    source: str, score: int = 0, reasons: Iterable[str] = (),
    profile: dict | None = None,
) -> dict:
    ensure_schema(database)
    database.execute(
        "INSERT INTO character_voice_bindings("
        "character_id,display_name,resource_id,voice_type,voice_name,preview_url,"
        "source,match_score,match_reasons_json,profile_json,status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(character_id) DO UPDATE SET "
        "display_name=excluded.display_name,resource_id=excluded.resource_id,"
        "voice_type=excluded.voice_type,voice_name=excluded.voice_name,"
        "preview_url=excluded.preview_url,source=excluded.source,"
        "match_score=excluded.match_score,"
        "match_reasons_json=excluded.match_reasons_json,"
        "profile_json=excluded.profile_json,status='Active',"
        "updated_at=CURRENT_TIMESTAMP",
        (
            character_id, display_name, resource_id, voice_type, voice_name,
            preview_url, source, int(score),
            json.dumps(list(reasons), ensure_ascii=False),
            json.dumps(profile or {}, ensure_ascii=False), "Active",
        ),
    )
    database.commit()
    return get_binding(database, character_id)


def save_manual_binding(
    database: sqlite3.Connection, *, character_id: str, display_name: str,
    voice_type: str, voice_name: str = "", resource_id: str = "seed-tts-2.0",
    preview_url: str = "",
) -> dict:
    if not str(voice_type or "").strip():
        raise ValueError("voice_type 不能为空")
    return _save(
        database,
        character_id=str(character_id).strip(),
        display_name=str(display_name or character_id).strip(),
        voice_type=str(voice_type).strip(),
        voice_name=str(voice_name or voice_type).strip(),
        resource_id=str(resource_id or "seed-tts-2.0").strip(),
        preview_url=str(preview_url or "").strip(),
        source="manual",
    )


def save_auto_binding(
    database: sqlite3.Connection, *, character: dict, match: VoiceMatch,
) -> dict:
    character_id = str(
        character.get("id") or character.get("character_id") or ""
    ).strip()
    display_name = str(
        character.get("script_character_name")
        or character.get("name")
        or character_id
    ).strip()
    if not character_id:
        raise VoiceBindingRequiredError("剧本人物缺少角色 ID，无法绑定火山声线")
    return _save(
        database,
        character_id=character_id,
        display_name=display_name,
        voice_type=match.voice_type,
        voice_name=match.voice_name,
        resource_id=match.resource_id,
        preview_url=match.preview_url,
        source="auto",
        score=match.score,
        reasons=match.reasons,
        profile=_profile(character),
    )


def resolve_voice_binding(
    database: sqlite3.Connection, character: dict, *, client,
) -> dict:
    character_id = str(character.get("id") or character.get("character_id") or "").strip()
    display_name = str(
        character.get("script_character_name")
        or character.get("name")
        or character_id
    ).strip()
    if not character_id:
        raise VoiceBindingRequiredError("剧本人物缺少角色 ID，无法绑定火山声线")
    cached = get_binding(database, character_id)
    if cached:
        return cached
    if client is None:
        raise VoiceBindingRequiredError(
            f"角色“{display_name}”尚未绑定火山声线，且声线库 API 未配置"
        )
    match = match_voice(character, client.list_speakers())
    return save_auto_binding(database, character=character, match=match)
