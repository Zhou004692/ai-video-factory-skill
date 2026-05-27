"""Seedream 5.0 i2i 一致性 PoC(独立脚本)。

复用 4.5 PoC 的 prompt 和 reference,只换 model id,
为了能直接和 4.5 输出做 A/B 对比。

预算: 2 张图 × ~¥0.29-0.4 = ¥0.6-0.8(5.0 价格可能略高于 4.5)

跑法:
    py scripts/seedream_50_test.py

输出: data/assets/J-VERIFY04/seedream50_poc/04.png + 05.png
"""
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

SKILL_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(SKILL_ROOT / ".env")
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from factory import ASSETS_DIR  # noqa: E402
from factory.clients import seedream_45  # noqa: E402

# 复用 4.5 PoC 的 SHOTS / STYLE / _build_prompt
from seedream_45_test import SHOTS, _build_prompt  # noqa: E402


JOB_ID = "J-VERIFY04"
MODEL_5_0 = "doubao-seedream-5-0-260128"  # 5.0 (2026-01-28 发布)


async def main() -> None:
    portrait = ASSETS_DIR / JOB_ID / "characters" / "engineer.png"
    if not portrait.exists():
        raise SystemExit(f"portrait not found: {portrait}")

    out_dir = ASSETS_DIR / JOB_ID / "seedream50_poc"
    out_dir.mkdir(parents=True, exist_ok=True)

    for idx, shot in SHOTS.items():
        prompt = _build_prompt(shot)
        print(f"[poc-5.0] generating shot #{idx} with model {MODEL_5_0}...",
              flush=True)
        img = await seedream_45.i2i(
            prompt=prompt,
            references=[str(portrait)],
            size="1440x2560",
            model=MODEL_5_0,
        )
        out_path = out_dir / f"{idx:02d}.png"
        out_path.write_bytes(img)
        print(f"[poc-5.0] -> {out_path} ({len(img)} bytes)", flush=True)

    print("")
    print(f"OK  5.0 PoC done.")
    print(f"compare 4.5 vs 5.0 vs reference:")
    print(f"  reference: {portrait}")
    print(f"  4.5 #04:   {ASSETS_DIR / JOB_ID / 'seedream45_poc' / '04.png'}")
    print(f"  5.0 #04:   {out_dir / '04.png'}")
    print(f"  4.5 #05:   {ASSETS_DIR / JOB_ID / 'seedream45_poc' / '05.png'}")
    print(f"  5.0 #05:   {out_dir / '05.png'}")


if __name__ == "__main__":
    asyncio.run(main())
