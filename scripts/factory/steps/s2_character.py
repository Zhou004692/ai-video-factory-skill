"""s2: 为**每个 dialogue 镜头**生成"角色在场景中"的图像,送 OmniHuman 当输入。

策略:
- 同一角色用**同一个 seed**(基于 character_id 哈希),提升脸/服装一致性
- prompt = look + scene + action + style_prefix
- 上传 TOS,记 URL 到 shots.character_in_scene_url
- 顺手保留每角色一张"reference"图(可用于未来 ref-image 接入或 fallback)
"""
import asyncio
import hashlib
import json

from .. import ASSETS_DIR
from ..db import conn
from ..clients import seedance, tos


def _seed_for(char_id: str) -> int:
    """char_id → 稳定正整数 seed,跨镜头一致。"""
    h = hashlib.sha1(char_id.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") & 0x7FFFFFFF


def _build_prompt(character: dict, shot: dict, style_prefix: str) -> str:
    look = character["look"]
    scene = shot.get("scene") or ""
    action = shot.get("action") or ""
    drama = shot.get("dramatic_keywords") or ""
    parts = [
        look,
        f"场景: {shot.get('camera','')} {shot.get('shot_type','')} 镜头. {scene}",
        f"动作/表情: {action}",
    ]
    if drama:
        # 强调情绪 — 让 Seedream 出更有戏剧张力的图
        parts.append(f"EMOTIONAL EXPRESSION (强烈,优先): {drama}")
    parts.append(f"风格: {style_prefix}")
    parts.append(
        "竖屏 9:16 构图, 角色主体清晰可见, 嘴部保持自然闭合或微张(避免特殊嘴型)"
    )
    return "\n".join(parts)


async def _gen_one_shot_img(job_id: str, shot_idx: int, character: dict,
                            shot: dict, style_prefix: str,
                            sem: asyncio.Semaphore) -> None:
    async with sem:
        prompt = _build_prompt(character, shot, style_prefix)
        seed = _seed_for(character["id"])
        img = await seedance.text_to_image(
            prompt, resolution="1440x2560", seed=seed
        )
        out_path = (ASSETS_DIR / job_id / "scenes"
                    / f"{shot_idx:02d}_{character['id']}.png")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(img)
        url = tos.upload(
            out_path,
            key_hint=f"{job_id}-scene-{shot_idx:02d}-{character['id']}"
        )
        db = conn()
        db.execute(
            "UPDATE shots SET character_in_scene_path=?, character_in_scene_url=? "
            "WHERE job_id=? AND idx=?",
            (str(out_path), url, job_id, shot_idx)
        )
        db.commit()


async def _gen_reference_img(job_id: str, character: dict,
                             style_prefix: str,
                             sem: asyncio.Semaphore) -> dict:
    """每角色一张通用定妆照(简单背景),用作 fallback / 兼容旧代码字段。"""
    async with sem:
        prompt = (
            f"{character['look']}\n"
            f"竖屏 9:16 半身像,简洁纯色背景,主体清晰居中,自然光"
        )
        seed = _seed_for(character["id"])
        img = await seedance.text_to_image(
            prompt, resolution="1440x2560", seed=seed
        )
        path = ASSETS_DIR / job_id / "characters" / f"{character['id']}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(img)
        url = tos.upload(path, key_hint=f"{job_id}-char-{character['id']}")
        return {
            **character,
            "image_path": str(path),
            "image_url": url,
        }


async def run(job_id: str) -> None:
    db = conn()
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    sb = json.loads(job["storyboard"])
    chars = {c["id"]: c for c in json.loads(job["characters_json"])}
    style_prefix = sb["style_prefix"]

    shots = db.execute(
        "SELECT * FROM shots WHERE job_id=? AND narrative_type='dialogue' "
        "ORDER BY idx", (job_id,)
    ).fetchall()

    sem = asyncio.Semaphore(2)

    # 1. 每角色一张 reference 定妆照(成本 ~¥0.1 × N)
    enriched_chars = await asyncio.gather(*[
        _gen_reference_img(job_id, c, style_prefix, sem)
        for c in chars.values()
    ])

    # 2. 每个 dialogue 镜头一张 character-in-scene(成本 ~¥0.1 × M)
    sb_shots = {s["idx"]: s for s in sb["shots"]}
    await asyncio.gather(*[
        _gen_one_shot_img(
            job_id, r["idx"], chars[r["speaker_id"]],
            sb_shots[r["idx"]], style_prefix, sem
        )
        for r in shots
    ])

    # 3. 回写 characters_json + 主角图(兼容)
    cost = 10 * (len(enriched_chars) + len(shots))   # ~¥0.1 per image
    db.execute(
        "UPDATE jobs SET characters_json=?, character_img=?, "
        "cost_cents=cost_cents+? WHERE job_id=?",
        (json.dumps(enriched_chars, ensure_ascii=False),
         enriched_chars[0]["image_path"],
         cost, job_id)
    )
    db.commit()
