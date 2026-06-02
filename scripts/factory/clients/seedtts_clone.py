"""SeedTTS 声音复刻 2.0 client(火山引擎 mega_tts 接口)。

火山控制台预先创建 speaker_id(S_xxx 格式),然后用 30s+ 音频训练复刻。
2025-10 起的 ICL 2.0(model_type=4)秒级克隆,相似度 97.5%。

接口:
  POST /api/v1/mega_tts/audio/upload  上传训练
  POST /api/v1/mega_tts/status         查询状态

环境变量(跟 seedtts_v2 共用):
  VOLC_TTS_APPID    应用 ID
  VOLC_TTS_TOKEN    Access Token

合成时怎么用复刻音色:
  把 S_xxx 填到 storyboard characters[].voice_id 或 roster.yaml
  seedtts_v2.synth 检测 S_ 前缀自动切 seed-icl-2.0 资源,管线透明支持

文档: https://www.volcengine.com/docs/6561/1305191
"""
import asyncio
import base64
import os
from pathlib import Path

import httpx

BASE = "https://openspeech.bytedance.com"
UPLOAD_PATH = "/api/v1/mega_tts/audio/upload"
STATUS_PATH = "/api/v1/mega_tts/status"


def _headers() -> dict:
    appid = os.environ.get("VOLC_TTS_APPID")
    token = os.environ.get("VOLC_TTS_TOKEN")
    if not (appid and token):
        raise RuntimeError("VOLC_TTS_APPID/TOKEN not set in .env")
    return {
        "Authorization": f"Bearer; {token}",
        "Resource-Id": "volc.megatts.voiceclone",
        "Content-Type": "application/json",
    }


async def upload_audio(
    speaker_id: str,
    audio_paths: list[str | Path],
    model_type: int = 4,
) -> dict:
    """上传训练音频。

    Args:
        speaker_id: 火山控制台预先创建的 S_xxx 格式 ID(必须先到控制台分配,不能自己定)
        audio_paths: 1-10 段音频文件(单段 30s+,总时长建议 30s-5min,清晰单人无杂音)
        model_type: 4=ICL 2.0(默认,推荐) / 1=复刻 1.0(旧版)
    """
    if not speaker_id.startswith("S_"):
        raise ValueError(
            f"speaker_id 必须是火山控制台分配的 S_xxx 格式,got {speaker_id!r}。"
            f"去 https://console.volcengine.com/speech/service/9 创建。"
        )
    if len(audio_paths) > 10:
        raise ValueError(f"单个 speaker 最多 10 段音频,got {len(audio_paths)}")

    appid = os.environ.get("VOLC_TTS_APPID")
    audios = []
    for p in audio_paths:
        data = Path(p).read_bytes()
        fmt = Path(p).suffix.lstrip(".").lower() or "mp3"
        if fmt not in {"mp3", "wav", "m4a", "aac"}:
            raise ValueError(f"不支持的音频格式: {fmt}(p={p})")
        audios.append({
            "audio_bytes": base64.b64encode(data).decode(),
            "audio_format": fmt,
        })
    payload = {
        "appid": appid,
        "speaker_id": speaker_id,
        "audios": audios,
        "model_type": model_type,
        "source": 2,
    }
    async with httpx.AsyncClient(timeout=300, headers=_headers()) as cli:
        r = await cli.post(BASE + UPLOAD_PATH, json=payload)
        if r.status_code >= 400:
            raise RuntimeError(
                f"seedtts upload {r.status_code}: {r.text[:600]}"
            )
        return r.json()


async def query_status(speaker_id: str) -> dict:
    """查询训练状态。

    返回 dict 含 status:
      0=未找到 / 1=训练中 / 2=成功 / 3=失败 / 4=活跃(可用)
    """
    appid = os.environ.get("VOLC_TTS_APPID")
    payload = {"appid": appid, "speaker_id": speaker_id}
    async with httpx.AsyncClient(timeout=60, headers=_headers()) as cli:
        r = await cli.post(BASE + STATUS_PATH, json=payload)
        if r.status_code >= 400:
            raise RuntimeError(
                f"seedtts status {r.status_code}: {r.text[:300]}"
            )
        return r.json()


def _extract_status(info: dict) -> int | None:
    """火山响应嵌套不一定,容错抽 status。"""
    if "status" in info:
        return info["status"]
    if "data" in info and isinstance(info["data"], dict):
        return info["data"].get("status")
    return None


async def wait_until_ready(speaker_id: str,
                           timeout: int = 600,
                           poll_interval: int = 5) -> None:
    """轮询直到训练完成(ICL 2.0 秒级,但服务端可能排队几分钟)。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        info = await query_status(speaker_id)
        status = _extract_status(info)
        if status in (2, 4):
            print(f"[clone] {speaker_id} ready (status={status})", flush=True)
            return
        if status == 3:
            raise RuntimeError(f"复刻训练失败 {speaker_id}: {info}")
        print(f"[clone] {speaker_id} 训练中 (status={status})...", flush=True)
        await asyncio.sleep(poll_interval)
    raise TimeoutError(f"复刻训练超时 {speaker_id} ({timeout}s)")
