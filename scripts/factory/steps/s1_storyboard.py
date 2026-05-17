"""s1: LLM 生成分镜表(新 schema:多角色 + dialogue/broll 标注)。

DEFAULT_LLM_PROVIDER:
  claude  -> Anthropic API(质量天花板,要美元余额)
  doubao  -> 火山方舟豆包大模型(默认,国内可商用)
"""
import json
import os

from .. import ASSETS_DIR
from ..db import conn
from ..prompts import storyboard_prompt


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
    for key in ("characters", "style_prefix", "hook", "shots", "bgm"):
        if key not in sb:
            raise ValueError(f"storyboard missing key: {key}; got {list(sb)}")

    if not isinstance(sb["characters"], list) or not sb["characters"]:
        raise ValueError("characters must be a non-empty list")
    char_ids = set()
    for ch in sb["characters"]:
        for f in ("id", "name", "gender", "image_prompt"):
            if f not in ch:
                raise ValueError(f"character missing {f}: {ch}")
        if ch["gender"] not in ("male", "female"):
            raise ValueError(f"character gender must be male|female: {ch}")
        char_ids.add(ch["id"])

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

    # shots 入库
    for shot in sb["shots"]:
        db.execute(
            "INSERT OR REPLACE INTO shots(job_id, idx, duration_sec, shot_type, "
            "camera, prompt, voiceover, subtitle, narrative_type, speaker_id, "
            "anchor_character) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id, shot["idx"],
                float(shot.get("duration", 6)),
                shot.get("shot_type", ""),
                shot.get("camera", ""),
                shot.get("prompt", ""),
                shot.get("voiceover", ""),
                shot.get("subtitle", shot.get("voiceover", "")),
                shot["type"],
                shot.get("speaker"),
                shot.get("anchor_character"),
            )
        )

    # 角色信息存到 jobs.characters_json,后续 s2 填充 image_path/image_url
    chars = [
        {"id": c["id"], "name": c["name"], "gender": c["gender"],
         "image_prompt": c["image_prompt"]}
        for c in sb["characters"]
    ]
    cost = llm.usage_to_cents(usage)
    db.execute(
        "UPDATE jobs SET storyboard=?, characters_json=?, "
        "cost_cents=cost_cents+? WHERE job_id=?",
        (json.dumps(sb, ensure_ascii=False),
         json.dumps(chars, ensure_ascii=False),
         cost, job_id)
    )
    db.commit()
