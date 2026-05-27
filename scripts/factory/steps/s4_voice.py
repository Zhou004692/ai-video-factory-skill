"""s4: 每个镜头 TTS。
- dialogue: 用 character.voice_id(speaker 的音色)
- broll:    用 narrator.voice_id

每个 shot 可在 storyboard 里加 emotion / emotion_scale / voice_directive 字段,
精细控制 TTS 情绪和语气。
"""
import json
import os

from .. import ASSETS_DIR
from ..banned_words import sanitize as _sanitize_voiceover
from ..db import conn
from ..clients import seedtts_v2, doubao_tts, tos


DEFAULT_CONTEXT = "你说话像是和朋友日常聊天,自然、有起伏,不要播音员腔调"


def _tts_client():
    name = os.getenv("TTS_VERSION", "v2").lower()
    return seedtts_v2 if name == "v2" else doubao_tts


async def run(job_id: str) -> None:
    db = conn()
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    sb = json.loads(job["storyboard"])
    chars = {c["id"]: c for c in json.loads(job["characters_json"])}
    narrator_voice = sb.get("narrator", {}).get("voice_id")
    sb_shots = {s["idx"]: s for s in sb["shots"]}

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
        raw_text = (r["voiceover"] or "").strip()
        if not raw_text:
            continue

        # 火山审核词预过滤(实测 OmniHuman 50514 Pre Audio Risk 都源于这类词)
        text, hits = _sanitize_voiceover(raw_text, idx)
        if hits:
            # 同步回写 DB + storyboard,防止下次 retry 又触发同一条
            db.execute(
                "UPDATE shots SET voiceover=? WHERE job_id=? AND idx=?",
                (text, job_id, idx),
            )
            for s in sb["shots"]:
                if s["idx"] == idx:
                    s["voiceover"] = text
                    if s.get("subtitle") == raw_text:
                        s["subtitle"] = text
            db.execute(
                "UPDATE jobs SET storyboard=? WHERE job_id=?",
                (json.dumps(sb, ensure_ascii=False), job_id),
            )

        sb_shot = sb_shots.get(idx, {})

        if r["narrative_type"] == "dialogue":
            speaker = chars.get(r["speaker_id"])
            if not speaker:
                raise RuntimeError(f"shot #{idx} unknown speaker {r['speaker_id']}")
            voice = speaker.get("voice_id") if is_v2 else \
                    doubao_tts.voice_for_gender(speaker.get("gender"))
        else:
            if not narrator_voice:
                continue
            voice = narrator_voice if is_v2 else \
                    doubao_tts.voice_for_gender("female")

        if not voice:
            raise RuntimeError(f"shot #{idx} no voice resolved")

        kwargs = {"voice": voice}
        if is_v2:
            # context_texts:语气指令(每镜可覆盖)
            directive = sb_shot.get("voice_directive") or DEFAULT_CONTEXT
            kwargs["context_texts"] = [directive]
            # TTS_STRICT_V2=true(走 2.0 资源):情绪靠 context_texts 自然语言指令,
            # emotion/emotion_scale 是 1.0 多情感音色专属字段,2.0 不消费 —— 强制丢弃,
            # 避免跟 voice_directive 冲突产出怪音频。
            strict_v2 = os.getenv("TTS_STRICT_V2", "false").lower() == "true"
            if not strict_v2:
                if sb_shot.get("emotion"):
                    kwargs["emotion"] = sb_shot["emotion"]
                if sb_shot.get("emotion_scale"):
                    kwargs["emotion_scale"] = int(sb_shot["emotion_scale"])
            # speech_rate per shot 可选
            if sb_shot.get("speech_rate") is not None:
                kwargs["speech_rate"] = int(sb_shot["speech_rate"])
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
