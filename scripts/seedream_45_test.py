"""Seedream 4.5 i2i 一致性 PoC(独立脚本,不动主管线)。

用 J-VERIFY04 的现有 engineer portrait 作 reference,
重出 shot 04 / 05 的 in-scene 静帧,对比"原 Seedream 3.0 t2i"和"新 4.5 i2i"的人脸一致性。

预算: 2 张图 × ¥0.29 = ¥0.58

跑法:
    1. 火山控制台开通 doubao-seedream-4-5-251128 模型/endpoint
    2. .env 加: SEEDREAM_45_MODEL_ID=<your_endpoint_id_or_model_id>
    3. py scripts/seedream_45_test.py
    4. 看输出文件: data/assets/J-VERIFY04/seedream45_poc/04.png + 05.png
    5. 跟原图 data/assets/J-VERIFY04/scenes/04_engineer.png 对比

判定标准:
    portrait 和 i2i 出来的 scene 04/05 看上去是同一个人 → A 方案升级
    不像 → 调 prompt / 调 sequential / 再调一次
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


JOB_ID = "J-VERIFY04"

# 直接复用 J-VERIFY04 storyboard 里 shot 04 / 05 的 scene+action 文本作 prompt。
# 这两镜原图 engineer(王工)portrait vs scene 不像同一人 — 这次用 i2i 应该修掉。
SHOTS = {
    4: {
        "look": "32 岁北方亚洲女性。**棕色直发盘成低马尾**,**瓜子脸 + 凤眼细长单眼皮**"
                ",鼻梁挺直,**薄唇微抿嘴角下压**。**冷调白皙皮肤**偏冷感,素颜无浓妆。"
                "**黑色技术夹克 + 白衬衫**,手里拿着深蓝色检测板。"
                "**面相清冷干练 / 设备工程师 / 北方理工女,绝非邻家女孩 / 娃娃脸 / 双眼皮甜美型**"
                "。不戴眼镜。",
        "scene": "高空车一侧,王工站在设备旁,手里拿着深蓝色检测板,身后是部分设备和工地远景虚化",
        "action": "左手举起检测板,右手食指指向设备某一处,身体微微转向老周,目光直视带专业感",
        "lighting": "侧光偏冷,从左侧斜射,王工面部冷调,带专业感",
    },
    5: {
        "look": "32 岁北方亚洲女性。**棕色直发盘成低马尾**,**瓜子脸 + 凤眼细长单眼皮**"
                ",鼻梁挺直,**薄唇微抿嘴角下压**。**冷调白皙皮肤**偏冷感,素颜无浓妆。"
                "**黑色技术夹克 + 白衬衫**,手里拿着深蓝色检测板。"
                "**面相清冷干练 / 设备工程师 / 北方理工女,绝非邻家女孩 / 娃娃脸 / 双眼皮甜美型**"
                "。不戴眼镜。",
        "scene": "王工特写,镜头前景虚化的设备铭牌,王工指着铭牌和轮胎方向,表情冷漠",
        "action": "右手在铭牌上指点,然后转向轮胎方向,目光不离老周,身体不动表情冷漠",
        "lighting": "侧逆光偏冷,王工面部一档柔光补暗部",
    },
}

STYLE_PREFIX = (
    "工地清晨晨雾, 深蓝冷调, 机械金属质感, 35mm 胶片颗粒, "
    "自然光为主, 偶有体积光穿过尘埃, 镜头硬朗有重量感, 纪实电影感"
)


def _build_prompt(shot: dict) -> str:
    parts = [
        shot["look"],
        f"场景: {shot['scene']}",
        f"光线: {shot['lighting']}",
        f"动作/姿态: {shot['action']}",
        f"风格: {STYLE_PREFIX}",
        "竖屏 9:16 构图, 角色主体清晰可见, 嘴部保持自然闭合或微张",
        "**保持参考图人物的脸型/五官/发型/服装严格一致**",
    ]
    return "\n".join(parts)


async def main() -> None:
    portrait = ASSETS_DIR / JOB_ID / "characters" / "engineer.png"
    if not portrait.exists():
        raise SystemExit(f"portrait not found: {portrait}")

    out_dir = ASSETS_DIR / JOB_ID / "seedream45_poc"
    out_dir.mkdir(parents=True, exist_ok=True)

    for idx, shot in SHOTS.items():
        prompt = _build_prompt(shot)
        print(f"[poc] generating shot #{idx} with reference {portrait.name}...",
              flush=True)
        img = await seedream_45.i2i(
            prompt=prompt,
            references=[str(portrait)],
            size="1440x2560",  # Seedream 4.5 要求 ≥ 3,686,400 像素
        )
        out_path = out_dir / f"{idx:02d}.png"
        out_path.write_bytes(img)
        print(f"[poc] -> {out_path} ({len(img)} bytes)", flush=True)

    print("")
    print(f"OK  PoC done. ¥{2 * 29 / 100:.2f} consumed.")
    print(f"compare:")
    print(f"  reference: {portrait}")
    print(f"  new 04:    {out_dir / '04.png'}")
    print(f"  new 05:    {out_dir / '05.png'}")
    print(f"  old 04:    {ASSETS_DIR / JOB_ID / 'scenes' / '04_engineer.png'}")
    print(f"  old 05:    {ASSETS_DIR / JOB_ID / 'scenes' / '05_engineer.png'}")
    print("")
    print("判定:portrait 跟新 04/05 像同一人 → 切 IMAGE_PROVIDER=seedream_45 主管线全量启用")


if __name__ == "__main__":
    asyncio.run(main())
