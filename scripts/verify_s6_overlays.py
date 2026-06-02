"""零成本验证 s6 字幕/hook/CTA 烧制(用 J-VERIFY06 现有 shots 重跑 s6)。

不调任何付费 API,纯本地 ffmpeg。覆盖 J-VERIFY06/final.mp4。
"""
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

SKILL_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(SKILL_ROOT / ".env")
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from factory.steps import s6_compose  # noqa: E402

JOB_ID = sys.argv[1] if len(sys.argv) > 1 else "J-VERIFY06"


async def main() -> None:
    print(f"[s6-verify] re-composing {JOB_ID} with subtitle/hook/CTA...",
          flush=True)
    await s6_compose.run(JOB_ID)
    print(f"[s6-verify] done -> data/assets/{JOB_ID}/final.mp4", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
