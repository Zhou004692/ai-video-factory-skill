"""A1 大壹音色调优 — 同音色不同 speech_rate + directive 强度对比。

固定: voice=zh_male_dayi_uranus_bigtts (你认定最像老周)
变量: speech_rate(语速) + voice_directive(情绪指令强度)

3 个新候选 + 1 个基线对照(原 A1):
  A1_v0_baseline:  原 A1 (-10 默认慢速 + 基础 directive)
  A1_v1_norm:      speech_rate=0   + 基础 directive
  A1_v2_strong:    speech_rate=+3  + 强化 directive(夸张写法)
  A1_v3_extreme:   speech_rate=+5  + 极端 directive(气急败坏)

预算 ~¥0.05 (4 段 × ¥0.013)
"""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

SKILL_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(SKILL_ROOT / ".env")
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from factory import DATA_DIR  # noqa: E402
from factory.clients import seedtts_v2  # noqa: E402

TEXT = "哎我跟你讲，我上个月租挖机交了九千押金，退租的时候只给我打了四千四！"
VOICE = "zh_male_dayi_uranus_bigtts"

DIRECTIVE_BASIC = (
    "用又气又心疼的语气说话,像跟老哥们吐槽,语速偏快带方言味,"
    "'四千四'三字咬重拖一下,尾句一拍桌泄气"
)

DIRECTIVE_STRONG = (
    "用又气又心疼、声带紧绷的语气,像跟老哥们抱怨被坑,语速偏快带北方口音,"
    "'九千''四千四'两个数字都咬重拖长半拍,中段有短暂倒吸气,"
    "尾句声调上扬到破音边缘,带一下'哎呀'的本能反应"
)

DIRECTIVE_EXTREME = (
    "用气急败坏、又心疼又憋屈的语气,像在工地办公室拍着桌子跟老哥泄愤,"
    "语速快、带浓重北方方言味儿,'九千押金''只给我打了四千四'两处都重读咬牙,"
    "中段有明显倒吸气声,尾句声调骤然拔高带破音,像是要拍桌前的最后一句话,"
    "全程带粗糙嘶哑感不是平静叙述"
)

VARIANTS = [
    {"tag": "A1_v0_baseline", "rate": -10, "directive": DIRECTIVE_BASIC,
     "note": "原 A1 (当前生产值,-10 慢 + 基础 directive)"},
    {"tag": "A1_v1_norm",     "rate": 0,   "directive": DIRECTIVE_BASIC,
     "note": "speech_rate=0 (去掉慢)"},
    {"tag": "A1_v2_strong",   "rate": 3,   "directive": DIRECTIVE_STRONG,
     "note": "rate=+3 + 强化 directive (更夸张)"},
    {"tag": "A1_v3_extreme",  "rate": 5,   "directive": DIRECTIVE_EXTREME,
     "note": "rate=+5 + 极端 directive (气急败坏)"},
]


async def gen_one(v: dict, out_dir: Path) -> None:
    try:
        audio = await seedtts_v2.synth(
            TEXT, voice=VOICE, speech_rate=v["rate"],
            context_texts=[v["directive"]],
        )
        path = out_dir / f"{v['tag']}.mp3"
        path.write_bytes(audio)
        print(f"[a1] OK {v['tag']} (rate={v['rate']}) -> {path.name}",
              flush=True)
    except Exception as e:
        print(f"[a1] FAIL {v['tag']}: {e}", flush=True)


async def main() -> None:
    out_dir = DATA_DIR / "voice_ab"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[a1] 跑 {len(VARIANTS)} 个 A1 调优变体...")
    await asyncio.gather(*[gen_one(v, out_dir) for v in VARIANTS])
    print("")
    print("OK A1 调优对比完成")
    print(f"听: {out_dir}\\A1_v*.mp3")
    print("")
    print("评分: 哪个版本情绪表达最像气急败坏的工地老板?")
    for v in VARIANTS:
        print(f"  {v['tag']:25s}  {v['note']}")


if __name__ == "__main__":
    asyncio.run(main())
