"""s1: LLM 生成分镜表(新 schema:多角色 + dialogue/broll 标注)。

DEFAULT_LLM_PROVIDER:
  claude  -> Anthropic API(质量天花板,要美元余额)
  doubao  -> 火山方舟豆包大模型(默认,国内可商用)
"""
import json
import os

from .. import ASSETS_DIR
from ..db import conn
from ..prompts import storyboard_prompt, load_voice_catalog


def _llm():
    name = os.getenv("DEFAULT_LLM_PROVIDER", "doubao").lower()
    if name == "claude":
        from ..clients import claude as mod
        return mod
    if name == "doubao":
        from ..clients import doubao_llm as mod
        return mod
    raise ValueError(f"unknown LLM provider: {name}")


def _validate(sb: dict) -> None:
    for key in ("characters", "narrator", "style_prefix", "hook", "shots", "bgm"):
        if key not in sb:
            raise ValueError(f"storyboard missing key: {key}; got {list(sb)}")

    if not isinstance(sb["characters"], list) or not sb["characters"]:
        raise ValueError("characters must be a non-empty list")
    catalog = load_voice_catalog()
    char_ids = set()
    for ch in sb["characters"]:
        # look 是新字段(简短特征,各镜头复用);image_prompt 旧版兼容
        if "look" not in ch and "image_prompt" not in ch:
            raise ValueError(f"character missing both 'look' and 'image_prompt': {ch}")
        for f in ("id", "name", "gender", "voice_id"):
            if f not in ch:
                raise ValueError(f"character missing {f}: {ch}")
        if ch["gender"] not in ("male", "female"):
            raise ValueError(f"character gender must be male|female: {ch}")
        if ch["voice_id"] not in catalog:
            raise ValueError(
                f"character {ch['id']} voice_id {ch['voice_id']!r} "
                f"not in catalog. Available: {list(catalog)[:5]} ..."
            )
        # 性别一致性检查
        if catalog[ch["voice_id"]]["gender"] != ch["gender"]:
            raise ValueError(
                f"character {ch['id']} gender={ch['gender']} mismatches "
                f"voice gender={catalog[ch['voice_id']]['gender']}"
            )
        char_ids.add(ch["id"])

    # narrator 校验
    narrator = sb.get("narrator") or {}
    if not narrator.get("voice_id"):
        raise ValueError("narrator.voice_id is required")
    if narrator["voice_id"] not in catalog:
        raise ValueError(
            f"narrator.voice_id {narrator['voice_id']!r} not in catalog"
        )

    if not isinstance(sb["shots"], list) or not (5 <= len(sb["shots"]) <= 12):
        raise ValueError(f"shots count out of range: {len(sb.get('shots', []))}")

    for shot in sb["shots"]:
        if shot.get("type") not in ("dialogue", "broll"):
            raise ValueError(f"shot.type must be dialogue|broll: {shot}")
        if shot["type"] == "dialogue":
            if shot.get("speaker") not in char_ids:
                raise ValueError(
                    f"dialogue shot speaker must be one of {char_ids}: {shot}"
                )
            if shot.get("duration", 0) > 12:
                raise ValueError(f"dialogue shot {shot['idx']} > 12s (OmniHuman limit)")
            if not (shot.get("voiceover") or "").strip():
                raise ValueError(f"dialogue shot {shot['idx']} has empty voiceover")
            # 新约束:每个 dialogue 镜头必须有 scene + action(用于生成 in-scene 主角图)
            for fld in ("scene", "action"):
                if not (shot.get(fld) or "").strip():
                    raise ValueError(
                        f"dialogue shot {shot['idx']} missing field {fld!r} "
                        f"(needed for per-shot character-in-scene image)"
                    )
        else:  # broll
            anchor = shot.get("anchor_character")
            if anchor is not None and anchor not in char_ids:
                raise ValueError(
                    f"broll shot {shot['idx']} anchor_character must be one of "
                    f"{char_ids} or null: {anchor}"
                )

    # 软约束提醒:broll 镜头最多 1 个
    n_broll = sum(1 for s in sb["shots"] if s["type"] == "broll")
    if n_broll > 1:
        raise ValueError(
            f"too many broll shots ({n_broll}); max 1 broll per video. "
            f"Make them dialogue with scene+action instead."
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
        style_code=job["style"] or "D",
        duration=duration,
        target_audience=job["target_audience"] or "通用",
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

    # shots 入库 — prompt 字段用 "scene + action"(dialogue) 或 prompt(broll)
    for shot in sb["shots"]:
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
            "voice_id": c["voice_id"],
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
