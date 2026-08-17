"""跟 i2v 那条做对照:同样的场景描述 + 同样的运镜描述,但 Seedance 走纯 t2v(无 keyframe)。
看 t2v 在没有关键帧锚定时,视频画面相比 i2v 损失多少。"""
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

# 把 i2v 那条的「关键帧场景信息 + 运镜信息」全合并到一个 prompt 里,公平对比 t2v
T2V_PROMPT = """工地集装箱办公室,上午,木桌上摊开一张泛黄的设备价目表纸张。
纸张上清晰可见两行黑色印刷大字:'高空车 日租 ¥800' 和 '月租 ¥200'。
桌上还有一支圆珠笔、一个搪瓷茶杯、一卷卷边的工程图纸。
自然光从右侧窗斜射进来,空气中浮尘在光柱中飘。
镜头从右上方缓缓推进至价目表纸张特写,纸张边缘被风轻微吹动,光斑在桌面缓慢移动。
35mm 胶片颗粒,工地黄金时刻晨雾,深蓝冷调,纪实电影感,竖屏 9:16 构图。"""


async def main():
    print("Seedance t2v(无 keyframe)出视频 ...", flush=True)
    video = await seedance.text_to_video(
        prompt=T2V_PROMPT,
        reference_image=None,  # 关键:不传 keyframe → 纯 t2v
        duration=6.0,
        resolution="720p",
        ratio="9:16",
    )
    mp4 = OUT_DIR / "broll_t2v.mp4"
    mp4.write_bytes(video)
    print(f"[done] t2v 视频 {len(video) // 1024} KB -> {mp4}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
