"""豆包 TTS / 火山 TTS client。

火山引擎语音合成大模型 API:
  https://www.volcengine.com/docs/6561
环境变量:
  ARK_API_KEY  (复用)
  VOLC_TTS_APPID
  VOLC_TTS_TOKEN
  VOLC_TTS_VOICE  音色,默认 zh_female_qingxin
"""
import os
import uuid

import httpx

VOLC_TTS_URL = "https://openspeech.bytedance.com/api/v1/tts"
DEFAULT_VOICE = os.getenv("VOLC_TTS_VOICE", "zh_female_qingxin")


async def synth(text: str, voice: str | None = None,
                speed: float = 1.0,
                emotion: str = "neutral") -> bytes:
    """合成,返回 mp3 字节。"""
    appid = os.environ.get("VOLC_TTS_APPID")
    token = os.environ.get("VOLC_TTS_TOKEN")
    if not (appid and token):
        raise RuntimeError("VOLC_TTS_APPID/TOKEN not set")

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
    async with httpx.AsyncClient(timeout=60) as cli:
        r = await cli.post(VOLC_TTS_URL, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 3000:
            raise RuntimeError(f"tts error: {data}")
        import base64
        return base64.b64decode(data["data"])


def estimate_cost_cents(char_count: int) -> int:
    """火山 TTS 大约 0.0001 元/字符,1000 字 ≈ 1 分。"""
    return max(1, char_count // 1000)
