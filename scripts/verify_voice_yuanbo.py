"""老法师音色第 3 轮 A/B — 测之前 catalog 漏掉的 _moon_bigtts 系列。

vectcut 整理:zh_male_yuanboxiaoshu_moon_bigtts (渊博小叔) "成熟、知识渊博"
完美匹配"52 岁阅尽行业的老法师"调性 —— 这是 _moon 老版 1.0 资源音色,
我们 catalog 之前完全没收录。

预算 ~¥0.02 (1 段)
"""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(SKILL_ROOT / ".env")
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

# _moon 音色走 1.0 资源,临时关 strict
os.environ["TTS_STRICT_V2"] = "false"

from factory import DATA_DIR  # noqa: E402
from factory.clients import seedtts_v2  # noqa: E402

TEXT = "实话告诉你，新手租车被坑过才明白，老司机一眼就看这 5 处！"


async def main() -> None:
    out_dir = DATA_DIR / "voice_ab"
    out_dir.mkdir(parents=True, exist_ok=True)
    voice = "zh_male_yuanboxiaoshu_moon_bigtts"
    print(f"[lao3] 测渊博小叔 (1.0 _moon 资源,catalog 之前漏收) ...")
    try:
        # _moon 音色走 seed-tts-1.0 资源(_uranus 是 2.0,_emo_*_mars 走 1.0,
        # _moon 也是 1.0 老版),显式传 resource_id 绕过自动路由
        audio = await seedtts_v2.synth(
            TEXT, voice=voice, speech_rate=3,
            resource_id="seed-tts-1.0",
        )
        path = out_dir / "L9_yuanboxiaoshu_moon.mp3"
        path.write_bytes(audio)
        print(f"[lao3] OK -> {path.name} ({len(audio)} bytes)")
    except Exception as e:
        print(f"[lao3] FAIL: {type(e).__name__}: {str(e)[:300]}")


if __name__ == "__main__":
    asyncio.run(main())
