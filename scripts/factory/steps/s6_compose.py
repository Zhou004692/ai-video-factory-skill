"""s6: 每镜处理(字幕/hook/fade) → 片尾 CTA → 拼接 → 混入 BGM → final.mp4。

每个 shot 自带音轨:
- dialogue: OmniHuman 烘焙了对白 → 视频已经有音轨,直接归一化
- broll 有 narrator audio_path: 把外部 narrator mp3 mux 到静音视频,然后归一化
- broll 无 narrator: 静音视频,normalize 时补静音

成片增强(短视频标配):
- 每镜烧字幕(subtitle 字段,底部居中) —— 90% 用户静音刷,必须
- 第一镜 0-3 秒烧 hook 大字(钩子)
- 每镜首尾 0.15s 淡入淡出(轻量转场)
- 片尾追加品牌 CTA 卡(可 APPEND_CTA=false 关闭)

最后 concat → 加 BGM(-12dB,有存在感不喧宾夺主)。
"""
import asyncio
import json
import os
from pathlib import Path

from .. import ASSETS_DIR
from ..db import conn
from ..clients import ffmpeg


async def _prep_shot(shot_row, prep_dir: Path,
                     subtitle: str | None, hook: str | None) -> Path:
    """准备单个 shot,返回归一化后(含字幕/hook/fade)的路径。"""
    idx = shot_row["idx"]
    src = Path(shot_row["video_path"])
    audio_path = shot_row["audio_path"]

    # 通用 mux:视频无音轨 + 有外部 audio → mux(画外音/旁白)。
    # 覆盖两种情况:broll narrator 旁白 + 混合路线 scene-dialogue 的 speaker 配音。
    # OmniHuman 视频自带嘴型同步音轨(has_audio=True),不会被 mux。
    if audio_path and not await ffmpeg.has_audio_stream(src):
        muxed = prep_dir / f"{idx:02d}_muxed.mp4"
        await ffmpeg.mux_video_audio(src, Path(audio_path), muxed)
        src = muxed

    duration = await ffmpeg.probe_duration(src)
    norm = prep_dir / f"{idx:02d}.mp4"
    await ffmpeg.normalize_shot(
        src, norm, subtitle=subtitle, hook=hook,
        fade=True, duration=duration,
    )
    return norm


async def run(job_id: str) -> None:
    base = ASSETS_DIR / job_id
    db = conn()
    job = db.execute("SELECT storyboard FROM jobs WHERE job_id=?",
                     (job_id,)).fetchone()
    sb = json.loads(job["storyboard"]) if job and job["storyboard"] else {}
    sb_shots = {s["idx"]: s for s in sb.get("shots", [])}
    hook_text = sb.get("hook", "")

    rows = db.execute(
        "SELECT idx, video_path, narrative_type, audio_path "
        "FROM shots WHERE job_id=? AND state='OK' ORDER BY idx",
        (job_id,)
    ).fetchall()
    if not rows:
        raise RuntimeError("no OK shots to compose")

    prep_dir = base / "shots_prep"
    prep_dir.mkdir(parents=True, exist_ok=True)

    # 1. 并发准备所有 shot(mux 旁白 + 字幕 + hook + fade + 归一化)
    #    hook 大字只烧在第一镜(idx 最小的那个)
    first_idx = min(r["idx"] for r in rows)
    norm_paths = await asyncio.gather(*[
        _prep_shot(
            r, prep_dir,
            subtitle=sb_shots.get(r["idx"], {}).get("subtitle"),
            hook=hook_text if r["idx"] == first_idx else None,
        )
        for r in rows
    ])
    norm_paths = list(norm_paths)

    # 2. 片尾 CTA 卡(APPEND_CTA=false 可关)
    if os.getenv("APPEND_CTA", "true").lower() == "true":
        cta_path = prep_dir / "zz_cta.mp4"
        await ffmpeg.make_cta_card(cta_path)
        norm_paths.append(cta_path)

    # 3. concat
    concat_out = base / "concat.mp4"
    await ffmpeg.concat_shots(norm_paths, concat_out)

    # 4. 混入 BGM(-12dB,提升存在感缓解片段间割裂)
    bgm = base / "bgm.mp3"
    final = base / "final.mp4"
    await ffmpeg.mix_bgm(concat_out, bgm, final, bgm_db=-12.0)

    db.execute("UPDATE jobs SET final_path=? WHERE job_id=?",
               (str(final), job_id))
    db.commit()
