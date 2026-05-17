"""s6: FFmpeg 拼接分镜 + 混音口播 + BGM → final.mp4。"""
from pathlib import Path

from .. import ASSETS_DIR
from ..db import conn
from ..clients import ffmpeg


async def run(job_id: str) -> None:
    base = ASSETS_DIR / job_id
    shot_paths = sorted((base / "shots").glob("*.mp4"))
    if not shot_paths:
        raise RuntimeError(f"no shots found in {base / 'shots'}")

    voice = base / "voice.mp3"
    bgm = base / "bgm.mp3"
    silent = base / "concat_silent.mp4"
    final = base / "final.mp4"

    # 1. 拼画面(去原始音轨)
    await ffmpeg.concat_shots(shot_paths, silent)
    # 2. 混音成最终成片
    await ffmpeg.compose_final(silent, voice, bgm, final)

    db = conn()
    db.execute("UPDATE jobs SET final_path=? WHERE job_id=?",
               (str(final), job_id))
    db.commit()
