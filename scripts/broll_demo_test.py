"""Demo 场景:多元素工地全景(塔吊 + 工人 + 钢管 + 浮尘 + 体积光)。
这种复杂场景是 t2v 的经典死穴:多人比例、机械结构、多层景深它都搞不定。
关键帧的价值就要在这种场景里才能拉开差距,价目表那种简单场景比不出来。

跑 3 步:
1. Seedream 5.0 出关键帧
2. Seedance i2v 用关键帧作首帧
3. Seedance t2v 同 prompt 但无 keyframe(对照)

约 ¥10。"""
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

# --- 关键帧 prompt(用 Seedream 5.0 出,锁定复杂构图)---
KEYFRAME_PROMPT = """工地黄金时刻清晨,晨雾弥漫。
远景:橙色塔吊缓缓伸向天空,工人剪影在脚手架顶部作业。
中景:三名戴黄色安全帽的工人扛着金属管走过工地,身影叠在金色体积光中。
前景:生锈的钢管堆放成扇形,黄沙和散落零件,空气中飞舞的浮尘清晰可见。
35mm 胶片纪实电影感,深蓝冷调与黄金光强烈对比,多层景深,大量俯仰角金属反光特写。
工业纪实电影摄影质感。竖屏 9:16 构图。"""

# --- i2v 动效 prompt(只写动效,场景由 keyframe 锁定)---
I2V_MOTION_PROMPT = """镜头从右侧缓慢推进,塔吊缓缓旋转,工人在脚手架上调整动作,
扛管工人沿工地走过。空气中浮尘缓慢飘动,黄金光斑在金属管表面缓慢移动。
工地黄金时刻晨雾,深蓝冷调,35mm 胶片颗粒,纪实电影感。"""

# --- t2v prompt(没有 keyframe,只能在 prompt 里把场景描述全写进去)---
T2V_PROMPT = """工地黄金时刻清晨,晨雾弥漫。远景橙色塔吊缓缓旋转伸向天空,工人剪影在脚手架顶部作业。
中景三名戴黄色安全帽的工人扛着金属管走过工地,身影叠在金色体积光中。
前景生锈钢管堆放成扇形,黄沙散落零件,浮尘飘动。
镜头从右侧缓慢推进,塔吊旋转,工人调整动作,光斑在金属表面缓慢移动。
35mm 胶片纪实电影感,深蓝冷调与黄金光对比,多层景深,大量俯仰角金属反光,竖屏 9:16。"""


async def main():
    print("[1/3] Seedream 5.0 出关键帧(锁定复杂构图) ...", flush=True)
    img = await seedance.text_to_image(KEYFRAME_PROMPT, resolution="1440x2560")
    kf = OUT_DIR / "demo_keyframe.png"
    kf.write_bytes(img)
    print(f"      关键帧 {len(img) // 1024} KB -> {kf}", flush=True)

    print("[2/3] Seedance i2v 用关键帧作首帧 ...", flush=True)
    video_i2v = await seedance.text_to_video(
        prompt=I2V_MOTION_PROMPT,
        reference_image=str(kf),
        duration=6.0,
        resolution="720p",
        ratio="9:16",
    )
    out_i2v = OUT_DIR / "demo_i2v.mp4"
    out_i2v.write_bytes(video_i2v)
    print(f"      i2v {len(video_i2v) // 1024} KB -> {out_i2v}", flush=True)

    print("[3/3] Seedance t2v(无 keyframe,纯文本)对照 ...", flush=True)
    video_t2v = await seedance.text_to_video(
        prompt=T2V_PROMPT,
        reference_image=None,
        duration=6.0,
        resolution="720p",
        ratio="9:16",
    )
    out_t2v = OUT_DIR / "demo_t2v.mp4"
    out_t2v.write_bytes(video_t2v)
    print(f"      t2v {len(video_t2v) // 1024} KB -> {out_t2v}", flush=True)

    print(f"\n[done] 三个文件都在 {OUT_DIR}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
