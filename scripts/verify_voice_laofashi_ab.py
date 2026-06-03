"""老法师音色 A/B — J-VERIFY08 baseline 听感"AI 播音腔不符合 52 岁年龄"。

测 4 个 _uranus 男声中年向音色 + 同 V3 极端 directive + speech_rate=3,
让 user 听感投票决定 expert_laofashi 切到哪个。

预算 ~¥0.05 (4 段 × ¥0.013)
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

from factory import DATA_DIR  # noqa: E402
from factory.clients import seedtts_v2  # noqa: E402


# J-VERIFY08 shot 1 老法师台词
TEXT = "实话告诉你，新手租车被坑过才明白，老司机一眼就看这 5 处！"

# 跟 J-VERIFY08 shot 1 一致的 V3 directive
DIRECTIVE = (
    "用阅尽套路的磁性低沉嗓、慢条斯理像揭行业老底,带一丝不易察觉的得意,"
    "'5 处'两字咬清楚加重、留白,尾句神情转严肃像要正式开讲"
)

CANDIDATES = [
    {
        "tag": "L1_baseline_cixing",
        "voice": "zh_male_cixingjieshuonan_uranus_bigtts",
        "note": "磁性解说男声(当前 baseline) - 纪录片旁白调,user 嫌播音腔",
    },
    {
        "tag": "L2_shenyeboke",
        "voice": "zh_male_shenyeboke_uranus_bigtts",
        "note": "深夜播客 - 沉稳/深夜感/哲思,可能更沧桑有'52 岁'味",
    },
    {
        "tag": "L3_xuanyi",
        "voice": "zh_male_xuanyijieshuo_uranus_bigtts",
        "note": "悬疑解说 - 故事讲述/揭秘/反转铺垫,适合'老法师揭秘内幕'调",
    },
    {
        "tag": "L4_gaoleng",
        "voice": "zh_male_gaolengchenwen_uranus_bigtts",
        "note": "高冷沉稳 - 律师/总监/教授感,可能太'冷'但试一下",
    },
    {
        "tag": "L5_baqi",
        "voice": "zh_male_baqiqingshu_uranus_bigtts",
        "note": "霸气青叔 - 威严宣讲,可能也偏播报但跟磁性解说不同调性",
    },
]


async def gen_one(cand: dict, out_dir: Path) -> None:
    try:
        audio = await seedtts_v2.synth(
            TEXT, voice=cand["voice"], speech_rate=3,
            context_texts=[DIRECTIVE],
        )
        path = out_dir / f"{cand['tag']}.mp3"
        path.write_bytes(audio)
        print(f"[lao] OK   {cand['tag']} -> {path.name} ({len(audio)} bytes)",
              flush=True)
    except Exception as e:
        print(f"[lao] FAIL {cand['tag']}: {type(e).__name__}: {str(e)[:200]}",
              flush=True)


async def main() -> None:
    out_dir = DATA_DIR / "voice_ab"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[lao] 台词: {TEXT}")
    print(f"[lao] speech_rate=3 + V3 directive")
    print(f"[lao] 跑 {len(CANDIDATES)} 个候选...")
    print("")
    await asyncio.gather(*[gen_one(c, out_dir) for c in CANDIDATES])
    print("")
    print(f"OK 听: {out_dir}\\L*.mp3")
    print("")
    print("评分维度 (1-5):")
    print("  - 像不像 52 岁市井老炮儿/行业老法师?")
    print("  - 自然度(不像 AI 播报)")
    print("  - 沧桑/世故感")
    print("  - 跟老周(大壹音色)的区分度(避免两个男声听起来一样)")
    print("")
    print("候选对照:")
    for c in CANDIDATES:
        print(f"  {c['tag']:25s}  {c['note']}")


if __name__ == "__main__":
    asyncio.run(main())
