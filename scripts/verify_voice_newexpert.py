"""老法师角色重新定位 (25-40 岁内行/新锐) — 音色 A/B 第 4 轮。

放宽候选池到 young/young-adult/middle-aged 偏年轻向,找"成熟但不老"的内行揭秘调:
  M1 解说小明(young-adult)  - 知性/专业讲解,"轻量老炮"
  M2 反卷青年(young)        - 调皮/犀利,段子手感
  M3 儒雅男友多情感(young-adult) - 知性温和有情绪
  M4 优柔公子多情感(young)  - 温润书卷气
  + 之前 L3 悬疑解说 / L9 渊博小叔 做对照

预算 ~¥0.06 (4 段新)
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

os.environ["TTS_STRICT_V2"] = "false"

from factory import DATA_DIR  # noqa: E402
from factory.clients import seedtts_v2  # noqa: E402

TEXT = "实话告诉你，新手租车被坑过才明白，老司机一眼就看这 5 处！"

DIRECTIVE = (
    "用知性内行揭秘的语气,像跟新手讲行业门道,语速适中带点冷静感,"
    "'5 处'两字咬清楚,尾句神情转严肃像要开讲"
)


CANDIDATES = [
    {
        "tag": "M1_xiaoming",
        "voice": "zh_male_jieshuoxiaoming_uranus_bigtts",
        "note": "解说小明(young-adult _uranus) - 知性专业讲解,适合行业讲解/轻量老炮",
    },
    {
        "tag": "M2_fanjuan",
        "voice": "zh_male_fanjuanqingnian_uranus_bigtts",
        "note": "反卷青年(young _uranus) - 调皮犀利,段子手感",
    },
    {
        "tag": "M3_ruyayichen_emo",
        "voice": "zh_male_ruyayichen_emo_v2_mars_bigtts",
        "note": "儒雅男友多情感(young-adult) - 知性温和,有情绪表达",
        "emotion": "coldness",
        "emotion_scale": 3,
    },
    {
        "tag": "M4_yourou_emo",
        "voice": "zh_male_yourougongzi_emo_v2_mars_bigtts",
        "note": "优柔公子多情感(young) - 温润书卷气",
        "emotion": "neutral",
        "emotion_scale": 3,
    },
]


async def gen_one(cand: dict, out_dir: Path) -> None:
    try:
        kwargs = {"voice": cand["voice"], "speech_rate": 3}
        if "emotion" in cand:
            kwargs["emotion"] = cand["emotion"]
            kwargs["emotion_scale"] = cand["emotion_scale"]
        else:
            kwargs["context_texts"] = [DIRECTIVE]
        audio = await seedtts_v2.synth(TEXT, **kwargs)
        path = out_dir / f"{cand['tag']}.mp3"
        path.write_bytes(audio)
        print(f"[m] OK   {cand['tag']} -> {path.name}", flush=True)
    except Exception as e:
        print(f"[m] FAIL {cand['tag']}: {type(e).__name__}: {str(e)[:200]}",
              flush=True)


async def main() -> None:
    out_dir = DATA_DIR / "voice_ab"
    print(f"[m] 跑 {len(CANDIDATES)} 个 25-40 岁内行候选...")
    await asyncio.gather(*[gen_one(c, out_dir) for c in CANDIDATES])
    print("")
    print(f"OK 听: {out_dir}\\M1-M4.mp3")
    print("  + 对照: L3 悬疑解说, L9 渊博小叔(都听过)")
    print("")
    print("评分: 像不像 30-35 岁内行揭秘者?(成熟+知性+不老气)")


if __name__ == "__main__":
    asyncio.run(main())
