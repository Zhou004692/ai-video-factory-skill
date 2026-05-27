"""OmniHuman 1.5 prompt 自然语言控制能力 PoC。

验证 OmniHuman 1.5 的 prompt 字段是否真识别"自然语言 emotion / body / camera"指令
——还是只能通过静帧本身控制(prompt 沦为装饰)。

对照:同一张 portrait + 同一段音频,跑两次:
  A. baseline:极简 prompt(只描述场景)
  B. rich:    自然语言 emotion + body + camera 详细指令

预算: 2 × OmniHuman 720p turbo, ~7s audio ≈ ¥7

跑法:
    py scripts/omnihuman_prompt_capabilities_test.py

输出: data/assets/J-VERIFY05/omnihuman_prompt_poc/baseline.mp4 + rich.mp4
对比标准:
  rich 视频的表情幅度 / 身体姿态 / 镜头运动 显著强于 baseline → prompt 字段真识别自然语言
  两者差不多 → OmniHuman 主要靠静帧 + 音频驱动,prompt 字段是装饰,emotion/body 要靠改静帧
"""
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

SKILL_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(SKILL_ROOT / ".env")
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from factory import ASSETS_DIR  # noqa: E402
from factory.clients import omnihuman  # noqa: E402
from factory.db import conn  # noqa: E402


JOB_ID = "J-VERIFY05"
SHOT_IDX = 1

# ===== Baseline:极简 prompt =====
BASELINE_PROMPT = "工地集装箱办公室,午后,人物对镜头说话"

# ===== Rich:自然语言 emotion + body + camera 结构化指令 =====
# 三个维度的明确指令,看 OmniHuman 是否真识别
RICH_PROMPT = """工地集装箱办公室,午后,木桌上摊开价目表。

emotion: 用半信半疑的怀疑语气,瞳孔从淡定到微缩到嘴角下压,情绪从平静到精明算计到不易察觉的得意,微表情有眉心收紧、鼻腔轻哼。

body: 双手拿起价目表凑近眯眼细看,身体微微前倾,食指点在某行价格上,中段抬眼看向镜头,末段缓缓靠回椅背双手交叉。

camera: 镜头从中景缓慢推近至人物面部特写,焦点从前景图纸虚化中过渡,推进感强,末段在面部特写处停住带轻微呼吸感。

画面里只有这一个角色,严禁出现其他人物或背景人影"""


async def main() -> None:
    db = conn()
    row = db.execute(
        "SELECT character_in_scene_url, audio_url FROM shots "
        "WHERE job_id=? AND idx=?",
        (JOB_ID, SHOT_IDX),
    ).fetchone()
    if not row or not row["character_in_scene_url"] or not row["audio_url"]:
        raise SystemExit(
            f"shot {JOB_ID}#{SHOT_IDX} missing img_url or audio_url, "
            f"先跑过 s2+s4 再试"
        )
    img_url = row["character_in_scene_url"]
    aud_url = row["audio_url"]
    print(f"[poc] using shot {JOB_ID}#{SHOT_IDX}", flush=True)
    print(f"      img: {img_url[:80]}...", flush=True)
    print(f"      aud: {aud_url[:80]}...", flush=True)

    out_dir = ASSETS_DIR / JOB_ID / "omnihuman_prompt_poc"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Baseline
    print(f"\n[poc] A. baseline (极简 prompt) generating...", flush=True)
    vid_a = await omnihuman.generate(
        image_url=img_url, audio_url=aud_url,
        prompt=BASELINE_PROMPT, resolution=720, turbo_mode=True,
    )
    (out_dir / "baseline.mp4").write_bytes(vid_a)
    print(f"      -> baseline.mp4 ({len(vid_a)} bytes)", flush=True)

    # Rich
    print(f"\n[poc] B. rich (emotion+body+camera 详细) generating...", flush=True)
    vid_b = await omnihuman.generate(
        image_url=img_url, audio_url=aud_url,
        prompt=RICH_PROMPT, resolution=720, turbo_mode=True,
    )
    (out_dir / "rich.mp4").write_bytes(vid_b)
    print(f"      -> rich.mp4 ({len(vid_b)} bytes)", flush=True)

    print("")
    print(f"OK  PoC done.")
    print(f"compare:")
    print(f"  baseline: {out_dir / 'baseline.mp4'}")
    print(f"  rich:     {out_dir / 'rich.mp4'}")
    print(f"判定:")
    print(f"  rich 表情幅度 / 身体姿态 / 镜头运动 显著强 → prompt 真识别自然语言")
    print(f"  两者差不多 → prompt 字段是装饰,要靠静帧/音频驱动")


if __name__ == "__main__":
    asyncio.run(main())
