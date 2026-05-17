"""s4: 把所有分镜的 voiceover 拼成一条完整口播音轨。"""
from .. import ASSETS_DIR
from ..db import conn
from ..clients import doubao_tts


async def run(job_id: str) -> None:
    db = conn()
    rows = db.execute(
        "SELECT idx, voiceover FROM shots WHERE job_id=? ORDER BY idx",
        (job_id,)
    ).fetchall()
    full_text = " ".join((r["voiceover"] or "").strip() for r in rows if r["voiceover"])
    if not full_text.strip():
        raise RuntimeError("voiceover empty across all shots")

    audio = await doubao_tts.synth(full_text)
    out = ASSETS_DIR / job_id / "voice.mp3"
    out.write_bytes(audio)

    cost = doubao_tts.estimate_cost_cents(len(full_text))
    db.execute(
        "UPDATE jobs SET voice_path=?, cost_cents=cost_cents+? WHERE job_id=?",
        (str(out), cost, job_id)
    )
    db.commit()
