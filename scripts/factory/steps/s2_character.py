"""s2:
- presenter dialogue → 可选生成一张“角色在场景中”静帧供人工预览。
- scene dialogue → 一张无可识别人脸的场景静帧,送 Seedance 当首帧。
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
from pathlib import Path
import shutil

from .. import ASSETS_DIR, DATA_DIR
from ..db import conn
from ..clients import seedance, tos
from ..scene_voiceover import faceless_visual_text

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


def _build_prompt(character: dict, shot: dict) -> str:
    """dialogue 镜头的 character-in-scene 静帧预览 prompt。"""
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
        # 静帧用于预览表情弧线的“起”状态。
        parts.append(f"表情(渲染镜头开始/「起」的瞬间状态): {expr}")
    if drama:
        parts.append(f"情绪基调(强烈,优先): {drama}")
    parts.append(
        "竖屏 9:16 构图, 角色主体清晰可见, 嘴部保持自然闭合或微张(避免特殊嘴型)"
    )
    # === 三条反幻觉约束(P0,修复 J-VERIFY05 实测发现的问题)===
    # 1) 单人成画:LLM voiceover 经常写"老板,..."等对话台词,Seedream 会推断
    #    对方在场加个对手角色背影。s3 原生视频提示也有同款约束,但静帧
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


def _build_broll_prompt(shot: dict,
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
    parts.append("竖屏 9:16 构图, 空镜或物件特写, 不强调人物对话")
    return "\n".join(parts)


def _dialogue_keyframe_kind(shot: dict) -> str:
    """Return the s2 keyframe route for a dialogue storyboard shot."""
    render_mode = (shot.get("render_mode") or "digital_human").lower()
    return "scene_voiceover" if render_mode == "scene" else "digital_human"


def _build_scene_voiceover_prompt(shot: dict) -> str:
    """Build a faceless keyframe prompt for dialogue rendered as scene + V.O.

    Spoken copy and character appearance are deliberately excluded.  Besides
    preventing accidental text rendering, this keeps a portrait-like first
    frame from triggering Seedance's real-face moderation.
    """
    speaker = shot.get("speaker") or shot.get("speaker_id")
    scene = faceless_visual_text(
        shot.get("scene") or shot.get("prompt"), speaker
    )
    lens = shot.get("lens") or ""
    lighting = shot.get("lighting") or ""
    dynamics = faceless_visual_text(shot.get("scene_dynamics"), speaker)
    camera = faceless_visual_text(shot.get("camera"), speaker)
    shot_type = faceless_visual_text(shot.get("shot_type"), speaker)
    cam = f"{camera} {shot_type}".strip()

    parts = [
        f"镜头: {cam}" + (f", {lens}" if lens else ""),
        f"场景: {scene}",
    ]
    if dynamics:
        parts.append(f"环境与物件动态: {dynamics}")
    if lighting:
        parts.append(f"光线: {lighting}")
    parts.extend([
        "竖屏 9:16 构图，以环境、设备、文件、仪表或物件特写承载信息",
        "这是画外音(V.O.)场景，台词只进入音轨，不在画面中呈现说话者",
        "不得出现任何可识别人脸、正面人物、真人肖像或嘴部特写，不做口型同步",
        "如必须出现人物，只能是脸部完全不可见的手部、背影或远景剪影",
        "画面内不要生成字幕、标牌文字或其他可读文字",
    ])
    return "\n".join(parts)


async def _gen_one_shot_img(job_id: str, shot_idx: int, character: dict,
                            shot: dict,
                            sem: asyncio.Semaphore) -> None:
    """dialogue 镜头:每镜出一张 character-in-scene 静帧。"""
    async with sem:
        prompt = _build_prompt(character, shot)
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
        start_prompt = _build_broll_prompt(shot, anchor_char)
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
            end_prompt = _build_broll_prompt(end_shot, anchor_char)
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


async def _gen_scene_voiceover_keyframe(
        job_id: str, shot_idx: int, shot: dict,
        sem: asyncio.Semaphore) -> None:
    """Generate faceless Seedance keyframes for a scene-dialogue shot."""
    async with sem:
        start_prompt = _build_scene_voiceover_prompt(shot)
        img = await _gen_image(start_prompt, references=None)
        out_path = (
            ASSETS_DIR / job_id / "scenes" / f"{shot_idx:02d}_scene_vo.png"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(img)
        url = tos.upload(
            out_path,
            key_hint=f"{job_id}-scene-vo-{shot_idx:02d}",
        )

        end_scene = shot.get("end_scene")
        if end_scene:
            end_shot = {**shot, "scene": end_scene}
            end_prompt = _build_scene_voiceover_prompt(end_shot)
            end_img = await _gen_image(end_prompt, references=None)
            end_path = (
                ASSETS_DIR / job_id / "scenes"
                / f"{shot_idx:02d}_scene_vo_end.png"
            )
            end_path.write_bytes(end_img)
            tos.upload(
                end_path,
                key_hint=f"{job_id}-scene-vo-{shot_idx:02d}-end",
            )

        db = conn()
        db.execute(
            "UPDATE shots SET character_in_scene_path=?, "
            "character_in_scene_url=? WHERE job_id=? AND idx=?",
            (str(out_path), url, job_id, shot_idx),
        )
        db.commit()


async def _gen_reference_img(job_id: str, character: dict,
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

    # 自定义角色使用用户上传的首张图片，不重新生成定妆照。其余图片在
    # 后续 i2i 镜头中作为一致性参考；这里上传首图供 Seedance 母片使用。
    supplied = character.get("image_path")
    if character.get("identity_type") == "custom" and supplied:
        supplied_path = Path(supplied)
        if not supplied_path.exists():
            raise FileNotFoundError(f"custom character reference missing: {supplied_path}")
        job_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(supplied_path, job_path)
        url = tos.upload(job_path, key_hint=f"{job_id}-char-{char_id}")
        references = [
            str(path) for path in character.get("reference_images", [])
            if Path(path).exists()
        ]
        return {
            **character, "image_path": str(job_path), "image_url": url,
            "reference_images": references or [str(supplied_path)],
        }

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
        _gen_reference_img(job_id, c, sem)
        for c in chars.values()
    ])
    enriched_by_id = {c["id"]: c for c in enriched_chars}

    # 2. dialogue 按 render_mode 分流:
    #    digital_human → 有脸 character-in-scene;scene → 无脸场景 + V.O.
    tasks_d = []
    for r in dialogue_shots:
        sb_shot = sb_shots[r["idx"]]
        if _dialogue_keyframe_kind(sb_shot) == "scene_voiceover":
            tasks_d.append(_gen_scene_voiceover_keyframe(
                job_id, r["idx"], sb_shot, sem
            ))
        else:
            tasks_d.append(_gen_one_shot_img(
                job_id, r["idx"], enriched_by_id[r["speaker_id"]],
                sb_shot, sem
            ))
    # 3. broll:每镜专属关键帧(Seedance i2v 首帧)
    tasks_b = []
    for r in broll_shots:
        sb_shot = sb_shots[r["idx"]]
        anchor_id = sb_shot.get("anchor_character")
        anchor_char = enriched_by_id.get(anchor_id) if anchor_id else None
        tasks_b.append(_gen_broll_keyframe(
            job_id, r["idx"], sb_shot, anchor_char, sem
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
