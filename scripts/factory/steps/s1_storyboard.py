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
        if "look" not in character and "image_prompt" not in character:
            raise ValueError(
                f"character missing both 'look' and 'image_prompt': {character}"
            )
        for field in ("id", "name", "gender"):
            if field not in character:
                raise ValueError(f"character missing {field}: {character}")
        if character["gender"] not in ("male", "female"):
            raise ValueError(f"character gender must be male|female: {character}")
        char_ids.add(character["id"])

    masters = sb["masters"]
    if not isinstance(masters, list) or not masters:
        raise ValueError("standard director pipeline requires at least 1 paid master")
    if len(masters) > 3:
        raise ValueError("standard director pipeline allows at most 3 paid masters")
    if sum(float(master.get("duration") or 0) for master in masters) > 36:
        raise ValueError("standard director pipeline paid master duration exceeds 36s")
    master_by_idx = {master.get("idx"): master for master in masters}
    if len(master_by_idx) != len(masters) or None in master_by_idx:
        raise ValueError("master idx values must be present and unique")

    max_dialogue_duration = 30 if _long_scene_enabled() else 12
    for master in masters:
        if master.get("type") not in ("dialogue", "broll"):
            raise ValueError(f"master.type must be dialogue|broll: {master}")
        if float(master.get("duration") or 0) <= 0:
            raise ValueError(f"master {master.get('idx')} duration must be > 0")
        if master["type"] == "dialogue":
            if master.get("speaker") not in char_ids:
                raise ValueError(
                    f"dialogue master speaker must be one of {char_ids}: {master}"
                )
            render_mode = (master.get("render_mode") or "presenter").lower()
            if render_mode not in ("presenter", "digital_human", "scene"):
                raise ValueError(
                    f"dialogue master {master['idx']} render_mode must be "
                    f"presenter|scene, got {master.get('render_mode')!r}"
                )
            if master["duration"] > max_dialogue_duration:
                raise ValueError(
                    f"dialogue master {master['idx']} > {max_dialogue_duration}s"
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
    if not isinstance(timeline, list) or not (4 <= len(timeline) <= 8):
        raise ValueError("standard director timeline must contain 4 to 8 edit shots")
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
        if video_start + duration > float(master_by_idx[video_idx]["duration"]):
            raise ValueError(f"edit shot {edit.get('idx')} exceeds video master")
        if audio_start + duration > float(master_by_idx[audio_idx]["duration"]):
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
        # look 是新字段(简短特征,各镜头复用);image_prompt 旧版兼容
        if "look" not in ch and "image_prompt" not in ch:
            raise ValueError(f"character missing both 'look' and 'image_prompt': {ch}")
        for f in ("id", "name", "gender"):
            if f not in ch:
                raise ValueError(f"character missing {f}: {ch}")
        if ch["gender"] not in ("male", "female"):
            raise ValueError(f"character gender must be male|female: {ch}")
        char_ids.add(ch["id"])

    if not isinstance(sb["shots"], list) or not (2 <= len(sb["shots"]) <= 12):
        raise ValueError(f"shots count out of range: {len(sb.get('shots', []))}")

    for shot in _generation_units(sb):
        if shot.get("type") not in ("dialogue", "broll"):
            raise ValueError(f"shot.type must be dialogue|broll: {shot}")
        if shot["type"] == "dialogue":
            if shot.get("speaker") not in char_ids:
                raise ValueError(
                    f"dialogue shot speaker must be one of {char_ids}: {shot}"
                )
            render_mode = (shot.get("render_mode") or "presenter").lower()
            if render_mode not in ("presenter", "digital_human", "scene"):
                raise ValueError(
                    f"dialogue shot {shot['idx']} render_mode must be "
                    f"presenter|scene, got {shot.get('render_mode')!r}"
                )
            max_dialogue_duration = 30 if _long_scene_enabled() else 12
            if shot.get("duration", 0) > max_dialogue_duration:
                raise ValueError(
                    f"dialogue shot {shot['idx']} > {max_dialogue_duration}s "
                    "(configured Seedance native speech limit)"
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

    # 软约束:broll 镜头最多 6 个(放宽,允许 broll 主导特写蒙太奇主题)
    # v2.4 之前 ≤1 是因为 broll 没角色感影响品牌记忆;
    # 现在允许 broll 主导(如老司机验车 5 处),但仍需至少 2-3 dialogue 镜头保品牌人格
    n_broll = sum(1 for s in sb["shots"] if s["type"] == "broll")
    if n_broll > 6:
        raise ValueError(
            f"too many broll shots ({n_broll}); max 6 per video "
            f"(30s 内 5s/broll × 6 = 全 broll 极限,再多没角色感观众记不住)"
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
            "gender": c["gender"],
            "look": c.get("look") or c.get("image_prompt", ""),
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
