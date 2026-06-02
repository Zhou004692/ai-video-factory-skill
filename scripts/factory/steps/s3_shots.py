"""s3: 按 narrative_type 分流生成视频。

- dialogue → OmniHuman(speaker 角色图 URL + 该镜音频 URL)→ 嘴型同步说话视频
- broll    → Seedance 2.0(prompt + first/last_frame + multi-ref + motion)→ 空镜
"""
import asyncio
import json
import os

from .. import ASSETS_DIR
from ..db import conn
from ..clients import seedance, omnihuman
from ..clients.omnihuman import OmniHumanAccountOverdue
from ..orchestrator import MAX_CONCURRENT_SHOTS


async def _gen_dialogue(job_id: str, shot: dict, char: dict, sb_shot: dict,
                        sem: asyncio.Semaphore) -> tuple[int, Exception | None]:
    async with sem:
        try:
            res = int(os.getenv("OMNIHUMAN_RESOLUTION", "720"))
            image_url = shot.get("character_in_scene_url") or char.get("image_url")
            if not image_url:
                raise RuntimeError(
                    f"shot #{shot['idx']} missing both "
                    f"character_in_scene_url and character image_url"
                )

            # OmniHuman 1.5 接受文本 prompt 控制运镜/情绪/动作/背景(官方上限 300 字符)。
            # 结构化标签写法:PoC 验证(b1c17mrw1)同 portrait/audio,
            #   rich-tagged prompt 比 baseline 文件大 +44%,画面变化显著更丰富
            #   → OmniHuman 1.5 真识别 "camera: / emotion: / body:" 三段标签。
            # 外观/场景已烙进静帧,这里只描述"动态"。
            # 注:voice_directive 已交给 TTS(走 SeedTTS context_texts),不喂 OmniHuman,
            #    避免 prompt 字符冗余;情绪走 dramatic_keywords+expression_arc 一条曲线
            parts = []
            if sb_shot.get("camera"):
                parts.append(f"camera: {sb_shot['camera']}")
            emo_terms = [
                x for x in (sb_shot.get("dramatic_keywords"),
                            sb_shot.get("expression_arc")) if x
            ]
            if emo_terms:
                # 标签内用句号分隔关键词和微表情曲线
                parts.append("emotion: " + "。".join(emo_terms))
            if sb_shot.get("action"):
                parts.append(f"body: {sb_shot['action']} (角色一边说话一边做这些)")
            elif shot.get("prompt"):
                parts.append(f"body: {shot['prompt']}")
            if sb_shot.get("scene_dynamics"):
                parts.append(f"background: {sb_shot['scene_dynamics']}")
            # 反幻觉约束(必加):OmniHuman 在对话戏上下文里有时会"补"一个对手角色
            # 进画面,且不知道对手长啥样,会生成随机人物。强制单人成画。
            parts.append("画面里只有这一个角色,严禁出现其他人物或背景人影")
            omni_prompt = "\n".join(parts)[:300]

            video = await omnihuman.generate(
                image_url=image_url,
                audio_url=shot["audio_url"],
                resolution=res,
                prompt=omni_prompt,
            )
            out_path = ASSETS_DIR / job_id / "shots" / f"{shot['idx']:02d}.mp4"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(video)

            cost = omnihuman.estimate_cost_cents(shot["duration_sec"], res)
            db = conn()
            db.execute(
                "UPDATE shots SET video_path=?, state='OK', provider='omnihuman', "
                "cost_cents=? WHERE job_id=? AND idx=?",
                (str(out_path), cost, job_id, shot["idx"])
            )
            db.execute(
                "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id=?",
                (cost, job_id)
            )
            db.commit()
            return shot["idx"], None
        except OmniHumanAccountOverdue as e:
            # 欠费类:写明确 prefix 到 shot.error,user 充值后 retry-shot 即可
            db = conn()
            db.execute(
                "UPDATE shots SET state='FAILED', "
                "error=? WHERE job_id=? AND idx=?",
                (f"[ACCOUNT_OVERDUE] {str(e)[:900]}", job_id, shot["idx"])
            )
            db.commit()
            print(f"[s3] shot #{shot['idx']} 火山账户欠费 — 充值后跑 "
                  f"`cli.py retry-shot {job_id} {shot['idx']}`",
                  flush=True)
            return shot["idx"], e
        except BaseException as e:
            # 兜底任何异常(包括 CancelledError / SIGTERM 之外的 KeyboardInterrupt 等),
            # 保证 shot state 不会停在 PENDING(避免数据状态歧义)。
            # 注意:SIGKILL 救不了,只能尽力。
            try:
                db = conn()
                db.execute(
                    "UPDATE shots SET state='FAILED', error=? "
                    "WHERE job_id=? AND idx=?",
                    (f"{type(e).__name__}: {str(e)[:900]}",
                     job_id, shot["idx"])
                )
                db.commit()
            except Exception:
                pass  # DB 也挂了,放弃
            # 普通 Exception 走 return tuple(让 gather 汇总);
            # CancelledError 等 BaseException 子类向上抛
            if isinstance(e, Exception):
                return shot["idx"], e
            raise


async def _gen_broll(job_id: str, shot: dict, style_prefix: str,
                     anchor_image_path: str | None,
                     sb_shot: dict,
                     sem: asyncio.Semaphore) -> tuple[int, Exception | None]:
    async with sem:
        try:
            prompt_full = f"{style_prefix}\n\n{shot['prompt']}"
            # === Seedance 2.0 深度利用 ===
            # 首帧:s2 生成的 per-shot 关键帧(画面更稳),老 job 退到 anchor 定妆照
            keyframe = shot.get("character_in_scene_path") or anchor_image_path
            # 尾帧:s2 出了 broll_end 时启用(按命名约定查找,不进 DB schema)
            end_keyframe_path = (
                ASSETS_DIR / job_id / "scenes"
                / f"{shot['idx']:02d}_broll_end.png"
            )
            last_frame = str(end_keyframe_path) if end_keyframe_path.exists() else None
            # ⚠️ Seedance 2.0 硬约束:first/last_frame 跟 reference_image **互斥**
            # ("first/last frame content cannot be mixed with reference media content")
            # J-VERIFY07 实测:scene-dialogue 模式传了 first_frame + anchor reference 直接 400
            # 选择:i2v 优先(first_frame 已是该镜专属静帧,信息够),不混 reference_image
            extra_refs: list[str] = []  # 保留变量给未来"无 first_frame + 多 ref"模式
            # camera_fixed / motion_strength 优先用 storyboard 显式字段,
            # 老 job(没这俩字段)走 fallback:从 camera 关键词推断 + 默认 0.4
            camera_fixed = sb_shot.get("camera_fixed")
            if camera_fixed is None:
                cam = (shot.get("camera") or "").lower()
                camera_fixed = any(k in cam for k in ("固定", "static", "fixed"))
            motion_strength = sb_shot.get("motion_strength")
            if motion_strength is None:
                motion_strength = 0.4
            video = await seedance.text_to_video(
                prompt=prompt_full,
                first_frame=keyframe,
                last_frame=last_frame,
                references=extra_refs or None,
                duration=shot["duration_sec"],
                resolution=os.getenv("ARK_VIDEO_RESOLUTION", "720p"),
                ratio="9:16",
                camera_fixed=bool(camera_fixed),
                motion_strength=float(motion_strength),
            )
            out_path = ASSETS_DIR / job_id / "shots" / f"{shot['idx']:02d}.mp4"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(video)

            cost = seedance.estimate_cost_cents(shot["duration_sec"])
            db = conn()
            db.execute(
                "UPDATE shots SET video_path=?, state='OK', provider='seedance', "
                "cost_cents=? WHERE job_id=? AND idx=?",
                (str(out_path), cost, job_id, shot["idx"])
            )
            db.execute(
                "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id=?",
                (cost, job_id)
            )
            db.commit()
            return shot["idx"], None
        except Exception as e:
            db = conn()
            db.execute(
                "UPDATE shots SET state='FAILED', error=? WHERE job_id=? AND idx=?",
                (str(e)[:1000], job_id, shot["idx"])
            )
            db.commit()
            return shot["idx"], e


async def run(job_id: str) -> None:
    db = conn()
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    sb = json.loads(job["storyboard"])
    chars = {c["id"]: c for c in json.loads(job["characters_json"])}
    style_prefix = sb.get("style_prefix", "")
    sb_shots = {s["idx"]: s for s in sb["shots"]}

    rows = db.execute(
        "SELECT * FROM shots WHERE job_id=? AND state!='OK' ORDER BY idx",
        (job_id,)
    ).fetchall()
    if not rows:
        return

    sem = asyncio.Semaphore(MAX_CONCURRENT_SHOTS)
    tasks = []
    for r in rows:
        shot = dict(r)
        sb_shot = sb_shots.get(shot["idx"], {})
        # 混合路线 B:dialogue 镜头按 render_mode 分流
        #   digital_human(默认) → OmniHuman 真人脸开口(情绪爆点镜,贵但有表演)
        #   scene              → Seedance i2v 场景视频 + 该镜 voice 作旁白(铺垫镜,省成本)
        #                        s6 会把 voice mux 进无音轨的 Seedance 视频 + 烧字幕
        render_mode = (sb_shot.get("render_mode") or "digital_human").lower()
        if shot["narrative_type"] == "dialogue" and render_mode == "digital_human":
            char = chars.get(shot["speaker_id"])
            if not char:
                raise RuntimeError(
                    f"shot #{shot['idx']} speaker {shot['speaker_id']} not found"
                )
            if not shot.get("audio_url"):
                raise RuntimeError(
                    f"shot #{shot['idx']} missing audio_url (s4_voice 未生成?)"
                )
            tasks.append(_gen_dialogue(job_id, shot, char, sb_shot, sem))
        elif shot["narrative_type"] == "dialogue" and render_mode == "scene":
            # 场景旁白模式:用 speaker 的 in-scene 静帧作 Seedance 首帧,
            # voice 在 s6 mux(画外音/V.O.,不要求嘴型同步)
            speaker = chars.get(shot["speaker_id"])
            anchor_path = speaker.get("image_path") if speaker else None
            tasks.append(_gen_broll(job_id, shot, style_prefix,
                                    anchor_path, sb_shot, sem))
        else:
            anchor_id = shot.get("anchor_character")
            anchor_path = None
            if anchor_id:
                anchor_char = chars.get(anchor_id)
                if not anchor_char:
                    raise RuntimeError(
                        f"shot #{shot['idx']} anchor_character "
                        f"{anchor_id} not in characters"
                    )
                anchor_path = anchor_char.get("image_path")
            tasks.append(_gen_broll(job_id, shot, style_prefix, anchor_path, sb_shot, sem))

    results = await asyncio.gather(*tasks)
    failed = [(i, e) for i, e in results if e is not None]
    if failed:
        msgs = "; ".join(f"#{i}: {e}" for i, e in failed)
        raise RuntimeError(f"{len(failed)}/{len(results)} shots failed: {msgs}")
