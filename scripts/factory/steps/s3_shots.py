"""s3: 按 narrative_type 分流生成视频。

- dialogue → OmniHuman(speaker 角色图 URL + 该镜音频 URL)→ 嘴型同步说话视频
- broll    → Seedance 1.0 Pro(prompt + style_prefix)→ 空镜
"""
import asyncio
import json
import os

from .. import ASSETS_DIR
from ..db import conn
from ..clients import seedance, omnihuman
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

            # 把 dramatic_keywords + voice_directive 拼进 OmniHuman 的 prompt
            # (用于影响画面、动作、运镜)
            parts = [shot.get("prompt", "")]
            if sb_shot.get("dramatic_keywords"):
                parts.append(f"EMOTION: {sb_shot['dramatic_keywords']}")
            if sb_shot.get("voice_directive"):
                parts.append(f"语气: {sb_shot['voice_directive']}")
            omni_prompt = ". ".join(p for p in parts if p)[:300]

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
        except Exception as e:
            db = conn()
            db.execute(
                "UPDATE shots SET state='FAILED', error=? WHERE job_id=? AND idx=?",
                (str(e)[:1000], job_id, shot["idx"])
            )
            db.commit()
            return shot["idx"], e


async def _gen_broll(job_id: str, shot: dict, style_prefix: str,
                     anchor_image_path: str | None,
                     sem: asyncio.Semaphore) -> tuple[int, Exception | None]:
    async with sem:
        try:
            prompt_full = f"{style_prefix}\n\n{shot['prompt']}"
            video = await seedance.text_to_video(
                prompt=prompt_full,
                reference_image=anchor_image_path,   # 有锚定角色时用其定妆照做首帧
                duration=shot["duration_sec"],
                resolution=os.getenv("ARK_VIDEO_RESOLUTION", "720p"),
                ratio="9:16",
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
        if shot["narrative_type"] == "dialogue":
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
            tasks.append(_gen_broll(job_id, shot, style_prefix, anchor_path, sem))

    results = await asyncio.gather(*tasks)
    failed = [(i, e) for i, e in results if e is not None]
    if failed:
        msgs = "; ".join(f"#{i}: {e}" for i, e in failed)
        raise RuntimeError(f"{len(failed)}/{len(results)} shots failed: {msgs}")
