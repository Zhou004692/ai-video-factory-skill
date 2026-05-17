"""s4: 每个镜头 TTS。
- dialogue: 用 character.voice_id(speaker 的音色)
- broll:    用 narrator.voice_id(全片旁白音色,桥接片段)

broll 镜头有了 voiceover(narrator 短句)就 TTS,空字符串就跳过(纯静音 broll)。
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
    sb = json.loads(job["storyboard"])
    chars = {c["id"]: c for c in json.loads(job["characters_json"])}
    narrator_voice = sb.get("narrator", {}).get("voice_id")

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
        text = (r["voiceover"] or "").strip()
        if not text:
            continue

        if r["narrative_type"] == "dialogue":
            speaker = chars.get(r["speaker_id"])
            if not speaker:
                raise RuntimeError(f"shot #{idx} unknown speaker {r['speaker_id']}")
            voice = speaker.get("voice_id") if is_v2 else \
                    doubao_tts.voice_for_gender(speaker.get("gender"))
        else:  # broll with narrator
            if not narrator_voice:
                # 没配 narrator,broll 旁白只能跳过
                continue
            voice = narrator_voice if is_v2 else \
                    doubao_tts.voice_for_gender("female")  # V1 fallback

        if not voice:
            raise RuntimeError(f"shot #{idx} no voice resolved")

        # 给 V2 加日常对话语气指令,减弱播报感(V1 自动忽略 context_texts)
        kwargs = {"voice": voice}
        if is_v2:
            kwargs["context_texts"] = [
                "你说话像是和朋友日常聊天,自然、有起伏,不要播音员腔调"
            ]
        audio = await tts.synth(text, **kwargs)
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
