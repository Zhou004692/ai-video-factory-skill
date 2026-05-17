"""s3: 并发生成每个分镜的视频。"""
import asyncio
import json
import os

from .. import ASSETS_DIR
from ..db import conn
from ..clients import seedance, kling
from ..orchestrator import MAX_CONCURRENT_SHOTS


PROVIDERS = {
    "seedance": seedance,
    "kling": kling,
}


def _client():
    name = os.getenv("DEFAULT_VIDEO_PROVIDER", "seedance").lower()
    if name not in PROVIDERS:
        raise ValueError(f"unknown video provider: {name}")
    return name, PROVIDERS[name]


async def _gen_one(job_id: str, shot: dict, character_img: str, style_prefix: str,
                   provider_name: str, provider, sem: asyncio.Semaphore):
    async with sem:
        try:
            prompt_full = f"{style_prefix}\n\n{shot['prompt']}"
            video = await provider.text_to_video(
                prompt=prompt_full,
                reference_image=character_img,
                duration=shot["duration_sec"],
                resolution="1080x1920",
            )
            out_path = ASSETS_DIR / job_id / "shots" / f"{shot['idx']:02d}.mp4"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(video)

            cost = provider.estimate_cost_cents(shot["duration_sec"]) \
                   if hasattr(provider, "estimate_cost_cents") else 0

            db = conn()
            db.execute(
                "UPDATE shots SET video_path=?, state='OK', provider=?, "
                "cost_cents=? WHERE job_id=? AND idx=?",
                (str(out_path), provider_name, cost, job_id, shot["idx"])
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
    style_prefix = sb.get("style_prefix", "")

    shots = db.execute(
        "SELECT idx, duration_sec, prompt FROM shots "
        "WHERE job_id=? AND state!='OK' ORDER BY idx",
        (job_id,)
    ).fetchall()
    if not shots:
        return

    provider_name, provider = _client()
    sem = asyncio.Semaphore(MAX_CONCURRENT_SHOTS)

    tasks = [
        _gen_one(job_id, dict(s), job["character_img"], style_prefix,
                 provider_name, provider, sem)
        for s in shots
    ]
    results = await asyncio.gather(*tasks)

    failed = [(idx, err) for idx, err in results if err is not None]
    if failed:
        msgs = "; ".join(f"#{i}: {e}" for i, e in failed)
        raise RuntimeError(f"{len(failed)}/{len(results)} shots failed: {msgs}")
