"""s4: 每个 dialogue 镜头单独 TTS(SeedTTS 2.0 大模型),用 character.voice_id 指定音色。

broll 镜头跳过(画面+字幕承担信息)。
"""
import json
import os

from .. import ASSETS_DIR
from ..db import conn
from ..clients import seedtts_v2, doubao_tts, tos


def _tts_client():
    """根据 TTS_VERSION 切换 V2(SeedTTS 2.0 大模型,默认) / V1(普通版兜底)。"""
    name = os.getenv("TTS_VERSION", "v2").lower()
    return seedtts_v2 if name == "v2" else doubao_tts


async def run(job_id: str) -> None:
    db = conn()
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    chars = {c["id"]: c for c in json.loads(job["characters_json"])}

    rows = db.execute(
        "SELECT idx, narrative_type, speaker_id, voiceover "
        "FROM shots WHERE job_id=? ORDER BY idx",
        (job_id,)
    ).fetchall()

    voice_dir = ASSETS_DIR / job_id / "voices"
    voice_dir.mkdir(parents=True, exist_ok=True)

    tts = _tts_client()
    is_v2 = (tts is seedtts_v2)

    total_chars = 0
    for r in rows:
        idx = r["idx"]
        if r["narrative_type"] != "dialogue":
            continue
        text = (r["voiceover"] or "").strip()
        if not text:
            continue
        speaker = chars.get(r["speaker_id"])
        if not speaker:
            raise RuntimeError(f"shot #{idx} unknown speaker {r['speaker_id']}")

        if is_v2:
            voice = speaker.get("voice_id")
            if not voice:
                raise RuntimeError(
                    f"speaker {speaker['id']} missing voice_id (V2 需要)"
                )
            audio = await tts.synth(text, voice=voice)
        else:
            voice = doubao_tts.voice_for_gender(speaker.get("gender"))
            audio = await tts.synth(text, voice=voice)

        audio_path = voice_dir / f"{idx:02d}.mp3"
        audio_path.write_bytes(audio)

        url = tos.upload(audio_path, key_hint=f"{job_id}-voice-{idx:02d}")
        db.execute(
            "UPDATE shots SET audio_path=?, audio_url=? WHERE job_id=? AND idx=?",
            (str(audio_path), url, job_id, idx)
        )
        total_chars += len(text)

    cost = tts.estimate_cost_cents(total_chars)
    db.execute(
        "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id=?",
        (cost, job_id)
    )
    db.commit()
