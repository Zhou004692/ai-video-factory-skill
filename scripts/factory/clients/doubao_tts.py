"""豆包 TTS。

默认走**火山方舟**(只需 ARK_API_KEY),OpenAI-compatible audio.speech 接口:
  POST https://ark.cn-beijing.volces.com/api/v3/audio/speech

前提:在方舟控制台开通"豆包·语音合成"系列模型(model ID 类似
doubao-seed-tts 或 doubao-tts-pro,以控制台为准)。

如果你想走旧版「语音技术」控制台(APPID+Access Token),设置:
  VOLC_TTS_PROVIDER=legacy
  VOLC_TTS_APPID=...
  VOLC_TTS_TOKEN=...
环境变量:
  ARK_API_KEY        方舟 API Key
  ARK_TTS_MODEL      方舟里的 TTS 模型 ID,默认 doubao-seed-tts
  VOLC_TTS_VOICE     音色 ID,以方舟 TTS 模型详情为准
"""
import base64
import os
import uuid

import httpx

ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_VOICE = os.getenv("VOLC_TTS_VOICE", "zh_female_shuangkuaisisi_moon_bigtts")
DEFAULT_MODEL = os.getenv("ARK_TTS_MODEL", "doubao-seed-tts")

# 性别 → 音色 ID 默认映射,可通过 .env 覆盖
GENDER_VOICE = {
    "male":   os.getenv("VOLC_TTS_VOICE_MALE",   "BV002_streaming"),
    "female": os.getenv("VOLC_TTS_VOICE_FEMALE", "BV001_streaming"),
}


def voice_for_gender(gender: str | None) -> str:
    """根据性别选音色;None / 未知 → 用 DEFAULT_VOICE。"""
    if not gender:
        return DEFAULT_VOICE
    return GENDER_VOICE.get(gender.lower(), DEFAULT_VOICE)


async def synth(text: str, voice: str | None = None,
                speed: float = 1.0, emotion: str = "neutral") -> bytes:
    provider = os.getenv("VOLC_TTS_PROVIDER", "ark").lower()
    if provider == "legacy":
        return await _synth_legacy(text, voice, speed, emotion)
    return await _synth_ark(text, voice, speed)


async def _synth_ark(text: str, voice: str | None, speed: float) -> bytes:
    """方舟 audio.speech 路径(默认)。"""
    key = os.environ.get("ARK_API_KEY")
    if not key:
        raise RuntimeError("ARK_API_KEY not set in .env")
    payload = {
        "model": DEFAULT_MODEL,
        "input": text,
        "voice": voice or DEFAULT_VOICE,
        "speed": speed,
        "response_format": "mp3",
    }
    async with httpx.AsyncClient(timeout=60) as cli:
        r = await cli.post(
            f"{ARK_BASE}/audio/speech",
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"ark tts {r.status_code}: {r.text[:500]} | "
                f"NOTE: ark.cn-beijing.volces.com does NOT host TTS. "
                f"Use legacy path: set VOLC_TTS_PROVIDER=legacy and provide "
                f"VOLC_TTS_APPID + VOLC_TTS_TOKEN from console.volcengine.com/speech/app"
            )
        return r.content


async def _synth_legacy(text: str, voice: str | None,
                        speed: float, emotion: str) -> bytes:
    """旧版「语音技术」路径(openspeech.bytedance.com)。"""
    appid = os.environ.get("VOLC_TTS_APPID")
    token = os.environ.get("VOLC_TTS_TOKEN")
    if not (appid and token):
        raise RuntimeError("VOLC_TTS_APPID/TOKEN required for legacy provider")
    payload = {
        "app": {"appid": appid, "token": "access_token", "cluster": "volcano_tts"},
        "user": {"uid": "ai-video-factory"},
        "audio": {
            "voice_type": voice or DEFAULT_VOICE,
            "encoding": "mp3",
            "speed_ratio": speed,
            "emotion": emotion,
        },
        "request": {
            "reqid": str(uuid.uuid4()),
            "text": text,
            "operation": "query",
        },
    }
    headers = {"Authorization": f"Bearer; {token}"}
    cluster = os.getenv("VOLC_TTS_CLUSTER", "volcano_tts")
    payload["app"]["cluster"] = cluster
    async with httpx.AsyncClient(timeout=60) as cli:
        r = await cli.post(
            "https://openspeech.bytedance.com/api/v1/tts",
            json=payload, headers=headers
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"legacy tts {r.status_code}: {r.text[:800]} | "
                f"payload.cluster={cluster}, voice={payload['audio']['voice_type']}, "
                f"appid={appid[:4]}... | "
                f"Common fixes: VOLC_TTS_CLUSTER (volcano_tts for standard, "
                f"volcano_icl_v2 for big-model) / VOLC_TTS_VOICE (zh_female_qingxin "
                f"for standard, *_bigtts suffix for big-model)"
            )
        data = r.json()
        if data.get("code") != 3000:
            raise RuntimeError(f"legacy tts: {data}")
        return base64.b64decode(data["data"])


def estimate_cost_cents(char_count: int) -> int:
    """火山 TTS 约 0.0001 元/字符,1000 字 ≈ 1 分。"""
    return max(1, char_count // 1000)
