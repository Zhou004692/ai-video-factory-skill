"""s2:
- 每个 dialogue 镜头 → 一张 "角色在场景中" 静帧,送 OmniHuman 当首帧。
- 每个 broll 镜头 → 一张专属关键帧,送 Seedance i2v 当首帧。
- 顺手为每角色保留一张通用定妆照(用作 fallback / seedream_45 i2i 的 reference)。

策略:
- 同一角色用同一个 seed(char_id 哈希),提升脸/服装一致性
- IMAGE_PROVIDER=seedream      → doubao-seedream-3.0-t2i(默认,无 reference)
- IMAGE_PROVIDER=seedream_45   → doubao-seedream-4.5 (portrait 仍 t2i,后续镜头 i2i + portrait 作 reference,人脸一致性更稳)
- IMAGE_PROVIDER=gpt_image     → gpt-image-2 (中国大陆网络不通,失败自动回退 Seedream)
"""
import asyncio
import hashlib
import json
import os
import shutil

from .. import ASSETS_DIR, DATA_DIR
from ..db import conn
from ..clients import seedance, tos

# 角色花名册 portrait 缓存目录(跨 job 复用同 char_id 的 portrait)
CHAR_CACHE_DIR = DATA_DIR / "character_cache"

try:
    from ..clients import gpt_image
except ImportError:
    gpt_image = None

try:
    from ..clients import seedream_45
except ImportError:
    seedream_45 = None

PROVIDER = os.getenv("IMAGE_PROVIDER", "seedream").lower()


async def _gen_image(prompt: str, seed: int | None = None,
                     references: list | None = None) -> bytes:
    """根据 IMAGE_PROVIDER 出图。
    - gpt_image:    references 非空走 edits;任何错误回退 Seedream
    - seedream_45:  references 非空走 i2i (4.5 模型);否则 t2i (4.5 模型)
    - seedream:     默认,走 seedance.text_to_image(3.0 t2i,无 reference)
    """
    if PROVIDER == "gpt_image" and gpt_image is not None:
        try:
            if references:
                return await gpt_image.edit(prompt, references)
            return await gpt_image.generate(prompt)
        except gpt_image.ContentPolicyError as e:
            print(f"[s2] gpt-image moderation, fallback Seedream: "
                  f"{str(e)[:120]}", flush=True)
        except Exception as e:
            print(f"[s2] gpt-image error, fallback Seedream: "
                  f"{type(e).__name__}: {str(e)[:200]}", flush=True)

    if PROVIDER == "seedream_45" and seedream_45 is not None:
        try:
            if references:
                return await seedream_45.i2i(prompt, references, seed=seed)
            return await seedream_45.t2i(prompt, seed=seed)
        except Exception as e:
            print(f"[s2] seedream-4.5 error, fallback seedream-3.0: "
                  f"{type(e).__name__}: {str(e)[:200]}", flush=True)

    return await seedance.text_to_image(prompt, resolution="1440x2560", seed=seed)


def _seed_for(char_id: str) -> int:
    """char_id → 稳定正整数 seed,跨镜头一致。"""
    h = hashlib.sha1(char_id.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") & 0x7FFFFFFF


def _build_prompt(character: dict, shot: dict, style_prefix: str) -> str:
    """dialogue 镜头的 character-in-scene 静帧 prompt(OmniHuman 首帧)。"""
    look = character["look"]
    scene = shot.get("scene") or ""
    action = shot.get("action") or ""
    drama = shot.get("dramatic_keywords") or ""
    expr = shot.get("expression_arc") or ""
    dynamics = shot.get("scene_dynamics") or ""
    lens = shot.get("lens") or ""
    lighting = shot.get("lighting") or ""

    cam = f"{shot.get('camera', '')} {shot.get('shot_type', '')}".strip()
    parts = [
        look,
        f"镜头: {cam}" + (f", {lens}" if lens else ""),
        f"场景: {scene}",
    ]
    if dynamics:
        parts.append(f"场景微动态(画面里正在发生): {dynamics}")
    if lighting:
        parts.append(f"光线: {lighting}")
    parts.append(f"动作/姿态: {action}")
    if expr:
        # 静帧是 OmniHuman 的首帧 — 渲染表情弧线的"起"状态
        parts.append(f"表情(渲染镜头开始/「起」的瞬间状态): {expr}")
    if drama:
        parts.append(f"情绪基调(强烈,优先): {drama}")
    parts.append(f"风格: {style_prefix}")
    parts.append(
        "竖屏 9:16 构图, 角色主体清晰可见, 嘴部保持自然闭合或微张(避免特殊嘴型)"
    )
    # === 三条反幻觉约束(P0,修复 J-VERIFY05 实测发现的问题)===
    # 1) 单人成画:LLM voiceover 经常写"老板,..."等对话台词,Seedream 会推断
    #    对方在场加个对手角色背影。s3 OmniHuman 那层已加同款约束,但首帧
    #    在 s2 就已经画了对手,必须在 s2 卡住。
    parts.append(
        "**画面里只有这一个角色,严禁出现其他人物、背景人影、对手角色的身影或背影**"
    )
    # 2) 严格 look 约束:防 Seedream 在新场景(如强风工地)凭空加飘起的发丝、
    #    胡须、配饰。J-VERIFY05 shot 4 平头老板头顶冒出一缕长发就是这个 bug。
    parts.append(
        "**角色外观必须严格按上述 look 描述渲染,不要添加描述里没有的头发(如飘起的长发丝)、胡须、配饰、纹身**"
    )
    # 3) 物件反重力约束:i2i 融合 portrait 参考图的手持物时,纸张/工具
    #    经常在桌面上悬空。J-VERIFY05 shot 3 桌上纸张漂浮就是这个 bug。
    parts.append(
        "**所有物件(纸张、文件、工具、设备)必须自然贴合桌面或被手稳固抓握,严禁悬空漂浮**"
    )
    return "\n".join(parts)


def _build_broll_prompt(shot: dict, style_prefix: str,
                        character: dict | None = None) -> str:
    """broll 镜头的关键帧 prompt(Seedance i2v 首帧)。
    没有 character 时纯场景;有 anchor_character 时把 look 也放进去。"""
    scene = shot.get("scene") or shot.get("prompt") or ""
    lens = shot.get("lens") or ""
    lighting = shot.get("lighting") or ""
    dynamics = shot.get("scene_dynamics") or ""

    cam = f"{shot.get('camera', '')} {shot.get('shot_type', '')}".strip()
    parts = []
    if character:
        parts.append(character["look"])
    parts.append(f"镜头: {cam}" + (f", {lens}" if lens else ""))
    parts.append(f"场景: {scene}")
    if dynamics:
        parts.append(f"场景微动态: {dynamics}")
    if lighting:
        parts.append(f"光线: {lighting}")
    parts.append(f"风格: {style_prefix}")
    parts.append("竖屏 9:16 构图, 空镜或物件特写, 不强调人物对话")
    return "\n".join(parts)


async def _gen_one_shot_img(job_id: str, shot_idx: int, character: dict,
                            shot: dict, style_prefix: str,
                            sem: asyncio.Semaphore) -> None:
    """dialogue 镜头:每镜出一张 character-in-scene 静帧。"""
    async with sem:
        prompt = _build_prompt(character, shot, style_prefix)
        seed = _seed_for(character["id"])
        refs = [character["image_path"]] if (
            PROVIDER in ("gpt_image", "seedream_45")
            and character.get("image_path")
        ) else None
        img = await _gen_image(prompt, seed=seed, references=refs)
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


async def _gen_broll_keyframe(job_id: str, shot_idx: int, shot: dict,
                              anchor_char: dict | None,
                              style_prefix: str,
                              sem: asyncio.Semaphore) -> None:
    """broll 镜头:每镜出 1-2 张关键帧,送 Seedance i2v 当首帧/尾帧。
    深度利用 Seedance 2.0 first_frame + last_frame 锁定起止构图。
    storyboard 给了 `end_scene` 字段时出双帧,否则只出首帧。"""
    async with sem:
        refs = [anchor_char["image_path"]] if (
            PROVIDER in ("gpt_image", "seedream_45")
            and anchor_char and anchor_char.get("image_path")
        ) else None

        # 首帧关键帧(开始构图)
        start_prompt = _build_broll_prompt(shot, style_prefix, anchor_char)
        img = await _gen_image(start_prompt, references=refs)
        out_path = (ASSETS_DIR / job_id / "scenes"
                    / f"{shot_idx:02d}_broll.png")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(img)
        url = tos.upload(
            out_path,
            key_hint=f"{job_id}-broll-{shot_idx:02d}"
        )

        # 尾帧关键帧(结束构图,storyboard 有 end_scene 字段才出)
        # 命名约定:{idx}_broll_end.png,s3_shots 按惯例查找,不进 DB schema
        end_scene = shot.get("end_scene")
        if end_scene:
            end_shot = {**shot, "scene": end_scene}  # 用 end_scene 覆盖 scene
            end_prompt = _build_broll_prompt(end_shot, style_prefix, anchor_char)
            end_img = await _gen_image(end_prompt, references=refs)
            end_path = (ASSETS_DIR / job_id / "scenes"
                        / f"{shot_idx:02d}_broll_end.png")
            end_path.write_bytes(end_img)
            tos.upload(end_path, key_hint=f"{job_id}-broll-{shot_idx:02d}-end")

        db = conn()
        db.execute(
            "UPDATE shots SET character_in_scene_path=?, "
            "character_in_scene_url=? WHERE job_id=? AND idx=?",
            (str(out_path), url, job_id, shot_idx)
        )
        db.commit()


async def _gen_reference_img(job_id: str, character: dict,
                             style_prefix: str,
                             sem: asyncio.Semaphore) -> dict:
    """每角色一张通用定妆照(简单背景)。

    Portrait 缓存机制(v2.4 角色 IP 复用):
    - 同 char_id(如 boss_laozhou / engineer_wanggong)首次出场 → 生成 + 写入
      `data/character_cache/{char_id}.png` + .json(含 TOS URL)
    - 后续 job 同 char_id → 直接复用缓存,跳过 ¥0.3/张 Seedream 调用
    - 跨片人格连续性的关键(老周永远是同一张脸)
    - 需要更新角色形象时,手动删除 cache 文件即可触发重生成
    """
    char_id = character["id"]
    cache_png = CHAR_CACHE_DIR / f"{char_id}.png"
    cache_meta = CHAR_CACHE_DIR / f"{char_id}.json"
    job_path = ASSETS_DIR / job_id / "characters" / f"{char_id}.png"

    # 缓存命中:复用 portrait + TOS URL
    if cache_png.exists() and cache_meta.exists():
        meta = json.loads(cache_meta.read_text(encoding="utf-8"))
        job_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cache_png, job_path)
        print(
            f"[s2] cache hit: {char_id} 复用 portrait (省 ¥0.3 + IP 一致性)",
            flush=True,
        )
        return {
            **character,
            "image_path": str(job_path),
            "image_url": meta["image_url"],
        }

    # 未命中:生成 + 写缓存
    async with sem:
        prompt = (
            f"{character['look']}\n"
            f"竖屏 9:16 半身像,简洁纯色背景,主体清晰居中,自然光"
        )
        seed = _seed_for(char_id)
        img = await _gen_image(prompt, seed=seed)
        job_path.parent.mkdir(parents=True, exist_ok=True)
        job_path.write_bytes(img)
        url = tos.upload(job_path, key_hint=f"{job_id}-char-{char_id}")

        # 写入缓存(下次复用)
        CHAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(job_path, cache_png)
        cache_meta.write_text(
            json.dumps({
                "char_id": char_id,
                "image_url": url,
                "first_job": job_id,
                "look_preview": character["look"][:80],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[s2] cache MISS: {char_id} 新生成 portrait,已写入 cache",
              flush=True)
        return {
            **character,
            "image_path": str(job_path),
            "image_url": url,
        }


async def run(job_id: str) -> None:
    db = conn()
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    sb = json.loads(job["storyboard"])
    chars = {c["id"]: c for c in json.loads(job["characters_json"])}
    style_prefix = sb["style_prefix"]

    sb_shots = {s["idx"]: s for s in sb["shots"]}
    dialogue_shots = db.execute(
        "SELECT * FROM shots WHERE job_id=? AND narrative_type='dialogue' "
        "ORDER BY idx", (job_id,)
    ).fetchall()
    broll_shots = db.execute(
        "SELECT * FROM shots WHERE job_id=? AND narrative_type='broll' "
        "ORDER BY idx", (job_id,)
    ).fetchall()

    sem = asyncio.Semaphore(2)

    # 1. 每角色一张 reference 定妆照
    enriched_chars = await asyncio.gather(*[
        _gen_reference_img(job_id, c, style_prefix, sem)
        for c in chars.values()
    ])
    enriched_by_id = {c["id"]: c for c in enriched_chars}

    # 2. dialogue:每镜 character-in-scene 静帧(OmniHuman 首帧)
    tasks_d = [
        _gen_one_shot_img(
            job_id, r["idx"], enriched_by_id[r["speaker_id"]],
            sb_shots[r["idx"]], style_prefix, sem
        )
        for r in dialogue_shots
    ]
    # 3. broll:每镜专属关键帧(Seedance i2v 首帧)
    tasks_b = []
    for r in broll_shots:
        sb_shot = sb_shots[r["idx"]]
        anchor_id = sb_shot.get("anchor_character")
        anchor_char = enriched_by_id.get(anchor_id) if anchor_id else None
        tasks_b.append(_gen_broll_keyframe(
            job_id, r["idx"], sb_shot, anchor_char, style_prefix, sem
        ))
    await asyncio.gather(*(tasks_d + tasks_b))

    # 4. 回写 characters_json + 主角图(兼容)+ cost
    total_shots = len(dialogue_shots) + len(broll_shots)
    per_img = gpt_image.estimate_cost_cents() if (
        PROVIDER == "gpt_image" and gpt_image is not None
    ) else 10
    cost = per_img * (len(enriched_chars) + total_shots)
    db.execute(
        "UPDATE jobs SET characters_json=?, character_img=?, "
        "cost_cents=cost_cents+? WHERE job_id=?",
        (json.dumps(enriched_chars, ensure_ascii=False),
         enriched_chars[0]["image_path"],
         cost, job_id)
    )
    db.commit()
