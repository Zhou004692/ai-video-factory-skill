"""s5: 生成或挑选 BGM。"""
import json

from .. import ASSETS_DIR
from ..db import conn
from ..clients import music


async def run(job_id: str) -> None:
    db = conn()
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    sb = json.loads(job["storyboard"])
    bgm_hint = sb.get("bgm", {})

    audio = await music.generate(
        style=bgm_hint.get("style", "lo-fi"),
        duration_sec=60,
        bpm_range=tuple(bgm_hint.get("bpm_range", [80, 100])),
        mood=bgm_hint.get("mood", "中性"),
    )
    out = ASSETS_DIR / job_id / "bgm.mp3"
    out.write_bytes(audio)

    cost = music.estimate_cost_cents(60)
    db.execute(
        "UPDATE jobs SET bgm_path=?, cost_cents=cost_cents+? WHERE job_id=?",
        (str(out), cost, job_id)
    )
    db.commit()
