"""老法师音色第 2 轮 A/B — 补 1.0 多情感系列(之前漏测)。

L1-L5(第 1 轮)全 _uranus user 都嫌不像 52 岁。问题:catalog 标 middle-aged
最老到 40-50,**没有 elder/senior 男声**。

第 2 轮测 1.0 多情感音色里被漏掉的可能候选:
  - shenyeboke 多情感版(L2 测了 _uranus 版,漏了 _emo_v2 版,有 depressed/sad 情绪)
  - jingqiang 给老法师试(之前测老周时角色错位)
  - 加 emotion=coldness/depressed 试沧桑感

预算 ~¥0.05 (3 段 × ¥0.015)
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

# 临时关 strict 走 1.0 资源
os.environ["TTS_STRICT_V2"] = "false"

from factory import DATA_DIR  # noqa: E402
from factory.clients import seedtts_v2  # noqa: E402

TEXT = "实话告诉你，新手租车被坑过才明白，老司机一眼就看这 5 处！"

CANDIDATES = [
    {
        "tag": "L6_shenyeboke_emo_coldness4",
        "voice": "zh_male_shenyeboke_emo_v2_mars_bigtts",
        "note": "深夜播客多情感版 + coldness@4 - 沉稳深夜感+冷感,可能最'沧桑'",
        "emotion": "coldness",
        "emotion_scale": 4,
    },
    {
        "tag": "L7_shenyeboke_emo_depressed4",
        "voice": "zh_male_shenyeboke_emo_v2_mars_bigtts",
        "note": "深夜播客多情感版 + depressed@4 - 加抑郁/疲惫感",
        "emotion": "depressed",
        "emotion_scale": 4,
    },
    {
        "tag": "L8_jingqiang_emo_hate3",
        "voice": "zh_male_jingqiangkanye_emo_mars_bigtts",
        "note": "京腔侃爷 + hate@3 - 北京老炮的'不屑'感(给老法师试,不是给老周)",
        "emotion": "hate",
        "emotion_scale": 3,
    },
]


async def gen_one(cand: dict, out_dir: Path) -> None:
    try:
        audio = await seedtts_v2.synth(
            TEXT, voice=cand["voice"], speech_rate=3,
            emotion=cand["emotion"], emotion_scale=cand["emotion_scale"],
        )
        path = out_dir / f"{cand['tag']}.mp3"
        path.write_bytes(audio)
        print(f"[lao2] OK   {cand['tag']} -> {path.name}",
              flush=True)
    except Exception as e:
        print(f"[lao2] FAIL {cand['tag']}: {type(e).__name__}: {str(e)[:200]}",
              flush=True)


async def main() -> None:
    out_dir = DATA_DIR / "voice_ab"
    print(f"[lao2] 跑第 2 轮 {len(CANDIDATES)} 个候选 (1.0 多情感系列)...")
    await asyncio.gather(*[gen_one(c, out_dir) for c in CANDIDATES])
    print("")
    print(f"OK 听: {out_dir}\\L6/L7/L8.mp3")
    print("评分:跟第 1 轮 L1-L5 比,有没有更接近 52 岁老炮儿沧桑感?")


if __name__ == "__main__":
    asyncio.run(main())
