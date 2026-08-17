"""隔离测试 broll i2v 新路径:
  1. Seedream 5.0 出一张「价目表特写」关键帧(看文字能不能渲染)
  2. Seedance i2v 用该关键帧作首帧出一段视频(看 i2v 比 t2v 稳多少)
成本约 ¥5(1 张图 + 1 段 6s 720p 视频)。"""
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

SCRIPTS = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS.parent
load_dotenv(SKILL_ROOT / ".env")
sys.path.insert(0, str(SCRIPTS))

from factory.clients import seedance  # noqa: E402

OUT_DIR = SKILL_ROOT / "data" / "broll_test"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# --- 关键帧 prompt(用 Seedream 5.0 出)---
KEYFRAME_PROMPT = """工地集装箱办公室,上午,木桌上摊开一张泛黄的设备价目表纸张。
纸张上清晰可见两行黑色印刷大字:'高空车 日租 ¥800' 和 '月租 ¥200'。
桌上还有一支圆珠笔、一个搪瓷茶杯、一卷卷边的工程图纸。
自然光从右侧窗斜射进来,空气中浮尘在光柱中飘。
35mm 胶片颗粒纪实风,聚焦价目表纸张特写,背景脚手架虚化。
竖屏 9:16 构图。"""

# --- i2v 动效 prompt(Seedance 在关键帧基础上动起来)---
I2V_MOTION_PROMPT = """镜头从右上方缓缓推进至价目表纸张特写,纸张边缘被风轻微吹动。
光柱中浮尘缓慢飘动,光斑在桌面缓慢移动。
工地黄金时刻晨雾,深蓝冷调,35mm 胶片颗粒,纪实电影感。"""


async def main():
    print("[1/2] Seedream 5.0 出关键帧 ...", flush=True)
    img = await seedance.text_to_image(
        KEYFRAME_PROMPT, resolution="1440x2560"
    )
    kf = OUT_DIR / "keyframe.png"
    kf.write_bytes(img)
    print(f"      关键帧 {len(img) // 1024} KB -> {kf}", flush=True)

    print("[2/2] Seedance i2v 用关键帧作首帧出视频 ...", flush=True)
    video = await seedance.text_to_video(
        prompt=I2V_MOTION_PROMPT,
        reference_image=str(kf),
        duration=6.0,
        resolution="720p",
        ratio="9:16",
    )
    mp4 = OUT_DIR / "broll_i2v.mp4"
    mp4.write_bytes(video)
    print(f"      i2v 视频 {len(video) // 1024} KB -> {mp4}", flush=True)

    print(f"\n[done] 两个文件分别在 {OUT_DIR}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
