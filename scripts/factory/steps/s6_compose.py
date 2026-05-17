"""s6: 每镜处理 → 拼接 → 混入 BGM → final.mp4。

每个 shot 自带音轨:
- dialogue: OmniHuman 烘焙了对白 → 视频已经有音轨,直接归一化
- broll 有 narrator audio_path: 把外部 narrator mp3 mux 到静音视频,然后归一化
- broll 无 narrator: 静音视频,normalize 时补静音

最后 concat → 加 BGM(-12dB,有存在感不喧宾夺主)。
"""
import asyncio
from pathlib import Path

from .. import ASSETS_DIR
from ..db import conn
from ..clients import ffmpeg


async def _prep_shot(shot_row, prep_dir: Path) -> Path:
    """准备单个 shot,返回归一化后的路径。"""
    idx = shot_row["idx"]
    src = Path(shot_row["video_path"])
    audio_path = shot_row["audio_path"]
    is_broll = shot_row["narrative_type"] == "broll"

    # 如果 broll 有 narrator 旁白,先 mux 进视频
    if is_broll and audio_path:
        muxed = prep_dir / f"{idx:02d}_muxed.mp4"
        await ffmpeg.mux_video_audio(src, Path(audio_path), muxed)
        src = muxed

    norm = prep_dir / f"{idx:02d}.mp4"
    await ffmpeg.normalize_shot(src, norm)
    return norm


async def run(job_id: str) -> None:
    base = ASSETS_DIR / job_id
    db = conn()
    rows = db.execute(
        "SELECT idx, video_path, narrative_type, audio_path "
        "FROM shots WHERE job_id=? AND state='OK' ORDER BY idx",
        (job_id,)
    ).fetchall()
    if not rows:
        raise RuntimeError("no OK shots to compose")

    prep_dir = base / "shots_prep"
    prep_dir.mkdir(parents=True, exist_ok=True)

    # 1. 并发准备所有 shot(mux 旁白 + 归一化)
    norm_paths = await asyncio.gather(*[
        _prep_shot(r, prep_dir) for r in rows
    ])

    # 2. concat
    concat_out = base / "concat.mp4"
    await ffmpeg.concat_shots(norm_paths, concat_out)

    # 3. 混入 BGM(-12dB,提升存在感缓解片段间割裂)
    bgm = base / "bgm.mp3"
    final = base / "final.mp4"
    await ffmpeg.mix_bgm(concat_out, bgm, final, bgm_db=-12.0)

    db.execute("UPDATE jobs SET final_path=? WHERE job_id=?",
               (str(final), job_id))
    db.commit()
