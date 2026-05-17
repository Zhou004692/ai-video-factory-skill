"""s6: 归一化每镜 → 拼接 → 混入 BGM → final.mp4。

每个 shot 自带音轨(OmniHuman 烘焙了对白,Seedance 是静音/环境音),
所以这里不再单独叠加 voice.mp3,只在拼好的画面上加 BGM。
"""
import asyncio
from pathlib import Path

from .. import ASSETS_DIR
from ..db import conn
from ..clients import ffmpeg


async def run(job_id: str) -> None:
    base = ASSETS_DIR / job_id
    db = conn()
    rows = db.execute(
        "SELECT idx, video_path FROM shots WHERE job_id=? AND state='OK' "
        "ORDER BY idx", (job_id,)
    ).fetchall()
    if not rows:
        raise RuntimeError("no OK shots to compose")

    norm_dir = base / "shots_norm"
    norm_dir.mkdir(parents=True, exist_ok=True)

    # 1. 归一化所有 shot 到统一规格(并发跑)
    tasks = []
    norm_paths: list[Path] = []
    for r in rows:
        src = Path(r["video_path"])
        dst = norm_dir / f"{r['idx']:02d}.mp4"
        norm_paths.append(dst)
        tasks.append(ffmpeg.normalize_shot(src, dst))
    await asyncio.gather(*tasks)

    # 2. concat
    concat_out = base / "concat.mp4"
    await ffmpeg.concat_shots(norm_paths, concat_out)

    # 3. 混入 BGM
    bgm = base / "bgm.mp3"
    final = base / "final.mp4"
    await ffmpeg.mix_bgm(concat_out, bgm, final)

    db.execute("UPDATE jobs SET final_path=? WHERE job_id=?",
               (str(final), job_id))
    db.commit()
