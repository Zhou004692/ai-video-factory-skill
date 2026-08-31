"""s1: LLM 生成分镜表(新 schema:多角色 + dialogue/broll 标注)。

DEFAULT_LLM_PROVIDER:
  claude  -> Anthropic API(质量天花板,要美元余额)
  doubao  -> 火山方舟豆包大模型(默认,国内可商用)
"""
import json
import os

from .. import ASSETS_DIR, script_sources
from ..db import conn
from ..prompts import storyboard_prompt


# Storyboard timings are serialized as decimal seconds, but Python evaluates
# them as binary floats.  An edit that ends exactly at its master boundary
# (for example 14.30 + 0.64 == 14.94) can therefore overshoot by a few machine
# units.  This epsilon absorbs representation noise only; material overruns
# remain invalid.
_TIMELINE_EPSILON_SEC = 1e-6


def _exceeds_timeline_boundary(start: float, duration: float, limit: float) -> bool:
    return start + duration - limit > _TIMELINE_EPSILON_SEC


def _long_scene_enabled() -> bool:
    configured = os.getenv("SEEDANCE25_LONG_SCENE_ENABLED")
    if configured is None:
        return True
    return configured.strip().lower() in {
        "1", "true", "yes", "on",
    }


def _llm():
    name = os.getenv("DEFAULT_LLM_PROVIDER", "doubao").lower()
    if name == "claude":
        from ..clients import claude as mod
        return mod
    if name == "doubao":
        from ..clients import doubao_llm as mod
        return mod
    raise ValueError(f"unknown LLM provider: {name}")


def _is_director_storyboard(sb: dict) -> bool:
    return sb.get("production_mode") == "native_master_local_director"


def _generation_units(sb: dict) -> list[dict]:
    """Return paid Seedance units while preserving legacy storyboards."""
    return (sb.get("masters") or []) if _is_director_storyboard(sb) else (
        sb.get("shots") or []
    )


def _validate_director_storyboard(sb: dict) -> None:
    for key in (
        "production_profile", "production_mode", "characters", "hook",
        "masters", "shots", "bgm",
    ):
        if key not in sb:
            raise ValueError(f"storyboard missing key: {key}; got {list(sb)}")
    if sb["production_profile"] != "standard":
        raise ValueError("director pipeline production_profile must be standard")

    characters = sb["characters"]
    if not isinstance(characters, list) or not characters:
        raise ValueError("characters must be a non-empty list")
    char_ids: set[str] = set()
    for character in characters:
        asset_backed = (
            character.get("identity_type") in {"real_person", "virtual_person"}
            and str(character.get("seedance_asset_uri") or "").startswith("asset://")
        )
        if not asset_backed and "look" not in character and "image_prompt" not in character:
            raise ValueError(
                f"character missing both 'look' and 'image_prompt': {character}"
            )
        for field in (("id", "name") if asset_backed else ("id", "name", "gender")):
            if field not in character:
                raise ValueError(f"character missing {field}: {character}")
        if not asset_backed and character["gender"] not in (
            "male", "female", "unspecified"
        ):
            raise ValueError(
                f"character gender must be male|female|unspecified: {character}"
            )
        char_ids.add(character["id"])

    masters = sb["masters"]
    if not isinstance(masters, list) or not masters:
        raise ValueError("standard director pipeline requires at least 1 paid master")
    master_by_idx = {master.get("idx"): master for master in masters}
    if len(master_by_idx) != len(masters) or None in master_by_idx:
        raise ValueError("master idx values must be present and unique")

    for master in masters:
        if master.get("type") not in ("dialogue", "broll"):
            raise ValueError(f"master.type must be dialogue|broll: {master}")
        if float(master.get("duration") or 0) <= 0:
            raise ValueError(f"master {master.get('idx')} duration must be > 0")
        if master["type"] == "dialogue":
            performance_beats = [
                beat for beat in (master.get("performance_beats") or [])
                if isinstance(beat, dict) and str(beat.get("dialogue") or "").strip()
            ]
            audio_only_dialogue = bool(performance_beats) and all(
                str(beat.get("speech_mode") or "on_screen")
                in {"off_screen", "narration"}
                for beat in performance_beats
            )
            if not audio_only_dialogue and master.get("speaker") not in char_ids:
                raise ValueError(
                    f"dialogue master speaker must be one of {char_ids}: {master}"
                )
            master_speakers = master.get("speakers") or [master.get("speaker")]
            if (
                not audio_only_dialogue
                and (not isinstance(master_speakers, list) or not master_speakers)
            ):
                raise ValueError(
                    f"dialogue master {master['idx']} speakers must be a non-empty list"
                )
            unknown_speakers = (
                set() if audio_only_dialogue
                else set(master_speakers) - char_ids
            )
            if unknown_speakers:
                raise ValueError(
                    f"dialogue master {master['idx']} has unknown speakers: "
                    f"{sorted(unknown_speakers)}"
                )
            render_mode = (master.get("render_mode") or "presenter").lower()
            if render_mode not in (
                "presenter", "digital_human", "scene", "cinematic_character"
            ):
                raise ValueError(
                    f"dialogue master {master['idx']} render_mode must be "
                    "presenter|scene|cinematic_character, got "
                    f"{master.get('render_mode')!r}"
                )
            if not (master.get("voiceover") or "").strip():
                raise ValueError(
                    f"dialogue master {master['idx']} has empty voiceover"
                )
            for field in ("scene", "action"):
                if not (master.get(field) or "").strip():
                    raise ValueError(
                        f"dialogue master {master['idx']} missing field {field!r}"
                    )
        else:
            anchor = master.get("anchor_character")
            if anchor is not None and anchor not in char_ids:
                raise ValueError(
                    f"broll master {master['idx']} anchor_character must be one "
                    f"of {char_ids} or null: {anchor}"
                )

    timeline = sb["shots"]
    if not isinstance(timeline, list) or not timeline:
        raise ValueError("director timeline must contain at least 1 edit shot")
    audio_ranges: dict[int, list[tuple[float, float]]] = {}
    for edit in timeline:
        duration = float(edit.get("duration") or 0)
        if duration <= 0:
            raise ValueError(f"edit shot {edit.get('idx')} duration must be > 0")
        video_idx = edit.get("video_master_idx")
        audio_idx = edit.get("audio_master_idx", video_idx)
        if video_idx not in master_by_idx or audio_idx not in master_by_idx:
            raise ValueError(
                f"edit shot {edit.get('idx')} references an unknown master"
            )
        video_start = float(edit.get("video_start") or 0)
        audio_start = float(edit.get("audio_start") or 0)
        if min(video_start, audio_start) < 0:
            raise ValueError(f"edit shot {edit.get('idx')} start must be >= 0")
        if _exceeds_timeline_boundary(
            video_start,
            duration,
            float(master_by_idx[video_idx]["duration"]),
        ):
            raise ValueError(f"edit shot {edit.get('idx')} exceeds video master")
        if _exceeds_timeline_boundary(
            audio_start,
            duration,
            float(master_by_idx[audio_idx]["duration"]),
        ):
            raise ValueError(f"edit shot {edit.get('idx')} exceeds audio master")
        audio_ranges.setdefault(audio_idx, []).append((audio_start, duration))

    for master_idx, master in master_by_idx.items():
        if master["type"] != "dialogue":
            continue
        cursor = 0.0
        for start, duration in audio_ranges.get(master_idx, []):
            if abs(start - cursor) > 0.05:
                raise ValueError(
                    f"audio timeline for master {master_idx} must be continuous"
                )
            cursor += duration
        if abs(cursor - float(master["duration"])) > 0.05:
            raise ValueError(
                f"audio timeline for master {master_idx} must be continuous"
            )


def _validate(sb: dict) -> None:
    if _is_director_storyboard(sb):
        _validate_director_storyboard(sb)
        return
    for key in ("characters", "hook", "shots", "bgm"):
        if key not in sb:
            raise ValueError(f"storyboard missing key: {key}; got {list(sb)}")

    if not isinstance(sb["characters"], list) or not sb["characters"]:
        raise ValueError("characters must be a non-empty list")
    char_ids = set()
    for ch in sb["characters"]:
        # Asset-backed identities use the Volc portrait asset as their source
        # of truth; prompt-only fictional characters still need a stable look.
        asset_backed = (
            ch.get("identity_type") in {"real_person", "virtual_person"}
            and str(ch.get("seedance_asset_uri") or "").startswith("asset://")
        )
        if not asset_backed and "look" not in ch and "image_prompt" not in ch:
            raise ValueError(f"character missing both 'look' and 'image_prompt': {ch}")
        for f in (("id", "name") if asset_backed else ("id", "name", "gender")):
            if f not in ch:
                raise ValueError(f"character missing {f}: {ch}")
        if not asset_backed and ch["gender"] not in (
            "male", "female", "unspecified"
        ):
            raise ValueError(
                f"character gender must be male|female|unspecified: {ch}"
            )
        char_ids.add(ch["id"])

    if not isinstance(sb["shots"], list) or not sb["shots"]:
        raise ValueError("storyboard must contain at least 1 generation unit")

    for shot in _generation_units(sb):
        if shot.get("type") not in ("dialogue", "broll"):
            raise ValueError(f"shot.type must be dialogue|broll: {shot}")
        if shot["type"] == "dialogue":
            if shot.get("speaker") not in char_ids:
                raise ValueError(
                    f"dialogue shot speaker must be one of {char_ids}: {shot}"
                )
            render_mode = (shot.get("render_mode") or "presenter").lower()
            if render_mode not in (
                "presenter", "digital_human", "scene", "cinematic_character"
            ):
                raise ValueError(
                    f"dialogue shot {shot['idx']} render_mode must be "
                    "presenter|scene|cinematic_character, got "
                    f"{shot.get('render_mode')!r}"
                )
            if not (shot.get("voiceover") or "").strip():
                raise ValueError(f"dialogue shot {shot['idx']} has empty voiceover")
            # 两种模式都需要 scene + action：真人模式控制原生表演，
            # scene 模式用于无人脸环境/物件画面。
            for fld in ("scene", "action"):
                if not (shot.get(fld) or "").strip():
                    raise ValueError(
                        f"dialogue shot {shot['idx']} missing field {fld!r} "
                        f"(needed for per-shot visual generation)"
                    )
        else:  # broll
            anchor = shot.get("anchor_character")
            if anchor is not None and anchor not in char_ids:
                raise ValueError(
                    f"broll shot {shot['idx']} anchor_character must be one of "
                    f"{char_ids} or null: {anchor}"
                )



async def run(job_id: str) -> None:
    db = conn()
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not job:
        raise RuntimeError(f"job {job_id} not found")

    duration = int(os.getenv("VIDEO_DURATION", "30"))
    prompt = storyboard_prompt(
        title=job["title"],
        track=job["track"] or "通用",
        duration=duration,
        target_audience=job["target_audience"] or "通用",
        source_context=script_sources.topic_source_context(
            db, job["topic_id"]
        ),
    )
    if _long_scene_enabled():
        prompt += (
            "\n\n# Seedance 2.5 长镜头\n"
            "如内容适合一镜完整表达，优先使用 13—24 秒 dialogue；"
            "25—30 秒只用于低密度台词并必须预留自然收尾时间。"
            "所有长镜头都须保证台词能在镜头内自然说完，并使用连续三段式时间轴。"
        )
    llm = _llm()
    sb, usage = llm.complete_json(prompt, max_tokens=6000)
    _validate(sb)

    # 落盘
    out_dir = ASSETS_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "storyboard.json").write_text(
        json.dumps(sb, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Paid generation units enter the shots table. In director mode these are
    # masters; final edit shots remain in storyboard JSON only.
    for shot in _generation_units(sb):
        if shot["type"] == "dialogue":
            shot_prompt = f"{shot.get('scene','')}\n{shot.get('action','')}"
        else:
            shot_prompt = shot.get("prompt", "")
        db.execute(
            "INSERT OR REPLACE INTO shots(job_id, idx, duration_sec, shot_type, "
            "camera, prompt, voiceover, subtitle, narrative_type, speaker_id, "
            "anchor_character) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id, shot["idx"],
                float(shot.get("duration", 6)),
                shot.get("shot_type", ""),
                shot.get("camera", ""),
                shot_prompt,
                shot.get("voiceover", ""),
                shot.get("subtitle", shot.get("voiceover", "")),
                shot["type"],
                shot.get("speaker"),
                shot.get("anchor_character"),
            )
        )

    # 角色信息存到 jobs.characters_json,新字段 look(每镜头复用)
    chars = []
    for c in sb["characters"]:
        chars.append({
            "id": c["id"],
            "name": c["name"],
            "gender": c.get("gender"),
            "look": c.get("look") or c.get("image_prompt", ""),
            "identity_type": c.get("identity_type"),
            "seedance_asset_uri": c.get("seedance_asset_uri"),
        })
    cost = llm.usage_to_cents(usage)
    db.execute(
        "UPDATE jobs SET storyboard=?, characters_json=?, "
        "cost_cents=cost_cents+? WHERE job_id=?",
        (json.dumps(sb, ensure_ascii=False),
         json.dumps(chars, ensure_ascii=False),
         cost, job_id)
    )
    db.commit()
