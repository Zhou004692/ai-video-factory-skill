"""BGM 生成 client。优先海绵音乐(字节同门)、备选 Suno。

海绵音乐目前没有官方对外 API,这里给一个本地降级方案:
- 如果配了 SUNO_API_KEY → 走 Suno
- 否则 → 从 references/bgm_library/ 里挑预设音乐(用户自备)

后续海绵音乐对外开 API 后,把 _via_haimian 实装即可。
"""
import asyncio
import os
import random
from pathlib import Path

import httpx

from .. import REFERENCES_DIR
from . import ffmpeg


async def generate(style: str, duration_sec: int, bpm_range: tuple[int, int] = (80, 100),
                   mood: str = "积极") -> bytes:
    """生成或挑选一段 BGM。返回 mp3 字节。"""
    provider = os.getenv("DEFAULT_MUSIC_PROVIDER", "silent").lower()

    if provider == "suno":
        return await _via_suno(style, duration_sec, bpm_range, mood)
    elif provider == "haimian":
        return await _via_haimian(style, duration_sec, bpm_range, mood)
    elif provider == "library":
        try:
            return _via_library(style, duration_sec)
        except (OSError, RuntimeError):
            # BGM is optional packaging. A missing local library must not
            # discard an otherwise valid, already-paid Seedance master.
            return await _via_silent(duration_sec)
    else:  # silent — 跑通管线用的兜底,无声 BGM
        return await _via_silent(duration_sec)


async def _via_silent(duration_sec: int) -> bytes:
    """用 ffmpeg 生成一段静音 mp3。测试和 BGM 不可用时的兜底。"""
    import tempfile
    out = Path(tempfile.mktemp(suffix=".mp3"))
    proc = await asyncio.create_subprocess_exec(
        ffmpeg.executable("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
        "-t", str(duration_sec), "-q:a", "9", "-acodec", "libmp3lame",
        str(out),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"silent bgm gen failed: {err.decode()[-500:]}")
    data = out.read_bytes()
    out.unlink(missing_ok=True)
    return data


async def _via_suno(style: str, duration: int, bpm_range, mood: str) -> bytes:
    """Suno API(经第三方 sunoapi.com 转接,官方未公开)。"""
    key = os.environ.get("SUNO_API_KEY")
    if not key:
        raise RuntimeError("SUNO_API_KEY not set")
    payload = {
        "prompt": f"{style}, {mood}, BPM {bpm_range[0]}-{bpm_range[1]}, instrumental, {duration}s",
        "make_instrumental": True,
        "wait_audio": True,
    }
    async with httpx.AsyncClient(timeout=300, headers={"Authorization": f"Bearer {key}"}) as cli:
        r = await cli.post("https://api.sunoapi.com/api/v1/suno/create", json=payload)
        r.raise_for_status()
        url = r.json()["data"][0]["audio_url"]
        audio = await cli.get(url, timeout=60)
        return audio.content


async def _via_haimian(style, duration, bpm_range, mood) -> bytes:
    raise NotImplementedError("海绵音乐 API 尚未对外开放,请改用 SUNO 或 library 模式")


def _via_library(style: str, duration_sec: int) -> bytes:
    """从本地音乐库随机挑一首。要先把 mp3 放到 references/bgm_library/<style>/"""
    lib = REFERENCES_DIR / "bgm_library" / style
    if not lib.exists():
        # 兜底:任意一首
        lib = REFERENCES_DIR / "bgm_library"
    candidates = list(lib.rglob("*.mp3"))
    if not candidates:
        raise RuntimeError(
            f"no bgm in {lib}. 请放一些 mp3 到这个目录,或改用 SUNO provider"
        )
    return random.choice(candidates).read_bytes()


def estimate_cost_cents(duration_sec: int, provider: str | None = None) -> int:
    p = (provider or os.getenv("DEFAULT_MUSIC_PROVIDER", "silent")).lower()
    if p == "suno":
        return 20  # 大约 0.2 元/首
    return 0      # silent / library / haimian 暂记 0
