"""s1: LLM 生成分镜表 + 主角描述 + 风格底色 + BGM 提示。

DEFAULT_LLM_PROVIDER:
  claude  -> 走 Anthropic API(质量天花板,但要美元余额)
  doubao  -> 走火山方舟豆包大模型(国内合规,价格低,默认)
"""
import json
import os
from pathlib import Path

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


async def run(job_id: str) -> None:
    db = conn()
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not job:
        raise RuntimeError(f"job {job_id} not found")

    prompt = storyboard_prompt(
        title=job["title"],
        track=job["track"] or "通用",
        style_code=job["style"] or "D",
        duration=60,
        target_audience=job["target_audience"] or "通用",
    )
    llm = _llm()
    sb, usage = llm.complete_json(prompt, max_tokens=6000)

    # 校验关键字段
    for key in ("character", "style_prefix", "hook", "shots", "bgm"):
        if key not in sb:
            raise ValueError(f"storyboard missing key: {key}; got {list(sb)}")
    if not isinstance(sb["shots"], list) or not (6 <= len(sb["shots"]) <= 12):
        raise ValueError(f"shots count out of range: {len(sb.get('shots', []))}")

    # 落盘
    out_dir = ASSETS_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "storyboard.json").write_text(
        json.dumps(sb, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # shots 入库
    for i, shot in enumerate(sb["shots"], 1):
        db.execute(
            "INSERT OR REPLACE INTO shots(job_id, idx, duration_sec, shot_type, "
            "camera, prompt, voiceover, subtitle) VALUES(?,?,?,?,?,?,?,?)",
            (
                job_id, i,
                float(shot.get("duration", 6)),
                shot.get("shot_type", ""),
                shot.get("camera", ""),
                shot.get("prompt", ""),
                shot.get("voiceover", ""),
                shot.get("subtitle", shot.get("voiceover", "")),
            )
        )
    cost = llm.usage_to_cents(usage)
    db.execute(
        "UPDATE jobs SET storyboard=?, cost_cents=cost_cents+? WHERE job_id=?",
        (json.dumps(sb, ensure_ascii=False), cost, job_id)
    )
    db.commit()
