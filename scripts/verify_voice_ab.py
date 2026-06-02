"""音色 A/B 实测 — 听感投票决定 boss_laozhou 用哪个音色。

同一台词跑 7 个候选,生成 mp3 让 user 听感评判:
  A 组: 2.0 _uranus + context_texts 自然语言情绪 (当前 strict 路径)
  B 组: 1.0 _emo_*_mars + emotion enum (之前废弃的多情感路径)

预算: ~¥0.1 (7 段 × 30 字 × ¥0.0005)

跑法: py scripts/verify_voice_ab.py
输出: data/voice_ab/*.mp3 + 评分清单

诊断目标:
  1. _uranus + context_texts 真能传达情绪吗?(我之前 reference memory 怀疑过)
  2. 1.0 多情感 + emotion=angry@4 听起来比 2.0 强多少?
  3. 哪个音色最像"被坑的工地老板"(不是评书/播音/客服)?
"""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

SKILL_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(SKILL_ROOT / ".env")
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

# 临时关闭 strict 2.0,让 _emo_ 音色走 1.0 资源 fallback
os.environ["TTS_STRICT_V2"] = "false"

from factory import DATA_DIR  # noqa: E402
from factory.clients import seedtts_v2  # noqa: E402


# J-VERIFY07 shot 1 的台词(老周愤怒举退款单)
TEXT = "哎我跟你讲，我上个月租挖机交了九千押金，退租的时候只给我打了四千四！"

# 2.0 _uranus 路径的自然语言情绪指令
DIRECTIVE = (
    "用又气又心疼的语气说话,像跟老哥们吐槽,语速偏快带方言味,"
    "'四千四'三字咬重拖一下,尾句一拍桌泄气"
)


CANDIDATES = [
    # === A 组: 2.0 _uranus 音色 + context_texts 自然语言情绪 (当前架构) ===
    {
        "tag": "A1_dayi_directive",
        "voice": "zh_male_dayi_uranus_bigtts",
        "note": "大壹(当前 boss_laozhou) - 朴实接地气,推荐对话戏",
        "mode": "v2_directive",
    },
    {
        "tag": "A2_jingqiang_uranus",
        "voice": "zh_male_baqiqingshu_uranus_bigtts",
        "note": "霸气青叔(2.0) - 试 _uranus 里最有威严感的",
        "mode": "v2_directive",
    },
    {
        "tag": "A3_xuanyi_directive",
        "voice": "zh_male_xuanyijieshuo_uranus_bigtts",
        "note": "悬疑解说 - 试反转铺垫感",
        "mode": "v2_directive",
    },
    {
        "tag": "A4_linjia_directive",
        "voice": "zh_male_linjiananhai_uranus_bigtts",
        "note": "邻家男孩 - 试年轻日常感",
        "mode": "v2_directive",
    },

    # === B 组: 1.0 多情感音色 + emotion enum (废弃路径,但情绪强) ===
    {
        "tag": "B1_jingqiang_emo_angry4",
        "voice": "zh_male_jingqiangkanye_emo_mars_bigtts",
        "note": "京腔侃爷(1.0 多情感) - 北京大爷/工地老板/生意人",
        "mode": "v1_emotion",
        "emotion": "angry",
        "emotion_scale": 4,
    },
    {
        "tag": "B2_guangzhou_emo_angry4",
        "voice": "zh_male_guangzhoudege_emo_mars_bigtts",
        "note": "广州德哥(1.0 多情感) - 广东老板/生意人/中小企业主",
        "mode": "v1_emotion",
        "emotion": "angry",
        "emotion_scale": 4,
    },
    {
        "tag": "B3_aojiao_emo_angry5",
        "voice": "zh_male_aojiaobazong_emo_v2_mars_bigtts",
        "note": "傲娇霸总(1.0 多情感) - 强势男版,emotion_scale=5 拉满",
        "mode": "v1_emotion",
        "emotion": "angry",
        "emotion_scale": 5,
    },
]


async def gen_one(cand: dict, out_dir: Path) -> None:
    kwargs = {"voice": cand["voice"]}
    if cand["mode"] == "v2_directive":
        kwargs["context_texts"] = [DIRECTIVE]
    else:  # v1_emotion
        kwargs["emotion"] = cand["emotion"]
        kwargs["emotion_scale"] = cand["emotion_scale"]
    try:
        audio = await seedtts_v2.synth(TEXT, **kwargs)
        path = out_dir / f"{cand['tag']}.mp3"
        path.write_bytes(audio)
        print(f"[ab] OK   {cand['tag']} -> {path.name} ({len(audio)} bytes)",
              flush=True)
    except Exception as e:
        print(f"[ab] FAIL {cand['tag']}: {type(e).__name__}: {str(e)[:200]}",
              flush=True)


async def main() -> None:
    out_dir = DATA_DIR / "voice_ab"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[ab] 台词: {TEXT}")
    print(f"[ab] 跑 {len(CANDIDATES)} 个候选音色...")
    print("")
    # 并发跑,加速
    await asyncio.gather(*[gen_one(c, out_dir) for c in CANDIDATES])
    print("")
    print("OK A/B 完成")
    print(f"听: {out_dir}\\*.mp3")
    print("")
    print("评分维度 (1-5 分):")
    print("  - 像不像被坑的工地老板?  (不是评书/播音/客服感)")
    print("  - 情绪传达强度?           (气、心疼、憋屈)")
    print("  - 自然度?                 (不像 AI)")
    print("")
    print("候选对照:")
    for c in CANDIDATES:
        print(f"  {c['tag']:30s}  {c['note']}")


if __name__ == "__main__":
    asyncio.run(main())
