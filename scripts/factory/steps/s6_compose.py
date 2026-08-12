"""s6: 每镜处理(字幕/hook/fade) → 片尾 CTA → 拼接 → 混入 BGM → final.mp4。

每个 Seedance 2.5 shot 已自带对白/画外音/环境音轨，合成阶段直接保留。

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
    duration = await ffmpeg.probe_duration(src)
    norm = prep_dir / f"{idx:02d}.mp4"
    await ffmpeg.normalize_shot(
        src, norm, subtitle=subtitle, hook=hook,
        fade=True, duration=duration,
    )
    return norm


def _director_segments(sb: dict, master_rows) -> list[dict]:
    """Resolve final edit shots to local picture/audio master paths."""
    paths = {int(row["idx"]): row["video_path"] for row in master_rows}
    segments: list[dict] = []
    for edit in sb.get("shots") or []:
        video_idx = int(edit["video_master_idx"])
        audio_idx = int(edit.get("audio_master_idx", video_idx))
        if video_idx not in paths or audio_idx not in paths:
            raise RuntimeError(
                f"director shot #{edit.get('idx')} references missing master"
            )
        segments.append({
            "idx": int(edit["idx"]),
            "video_path": paths[video_idx],
            "native_audio": paths[audio_idx],
            "video_start": float(edit.get("video_start") or 0),
            "audio_start": float(edit.get("audio_start") or 0),
            "duration": float(edit["duration"]),
            "subtitle": edit.get("subtitle"),
            "subtitle_cues": edit.get("subtitle_cues") or [],
        })
    return segments


async def _prep_director_segment(segment: dict, prep_dir: Path,
                                 hook: str | None) -> Path:
    out = prep_dir / f"{segment['idx']:02d}.mp4"
    await ffmpeg.cut_director_segment(
        Path(segment["video_path"]),
        Path(segment["native_audio"]),
        out,
        video_start=segment["video_start"],
        audio_start=segment["audio_start"],
        duration=segment["duration"],
        subtitle=segment.get("subtitle"),
        subtitle_cues=segment.get("subtitle_cues"),
        hook=hook,
    )
    return out


async def run(job_id: str) -> None:
    base = ASSETS_DIR / job_id
    db = conn()
    job = db.execute("SELECT storyboard FROM jobs WHERE job_id=?",
                     (job_id,)).fetchone()
    sb = json.loads(job["storyboard"]) if job and job["storyboard"] else {}
    sb_shots = {s["idx"]: s for s in sb.get("shots", [])}
    hook_text = sb.get("hook", "")

    rows = db.execute(
        "SELECT idx, video_path "
        "FROM shots WHERE job_id=? AND state='OK' ORDER BY idx",
        (job_id,)
    ).fetchall()
    if not rows:
        raise RuntimeError("no OK shots to compose")

    prep_dir = base / "shots_prep"
    prep_dir.mkdir(parents=True, exist_ok=True)

    # 1. 并发准备所有 shot(保留原生音轨 + 字幕 + hook + fade + 归一化)
    #    hook 大字只烧在第一镜(idx 最小的那个)
    director_mode = (
        sb.get("production_mode") == "native_master_local_director"
    )
    if director_mode:
        segments = _director_segments(sb, rows)
        first_idx = min(segment["idx"] for segment in segments)
        norm_paths = await asyncio.gather(*[
            _prep_director_segment(
                segment,
                prep_dir,
                hook=hook_text if segment["idx"] == first_idx else None,
            )
            for segment in segments
        ])
    else:
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

    # 5. 生成视频号封面 thumbnail(决定 50% 点击率)
    #    抽 1.5s 帧 + 叠超大 hook 大字 + 品牌色边框 + 品牌锚
    if os.getenv("GEN_THUMBNAIL", "true").lower() == "true":
        thumb = base / "thumbnail.jpg"
        try:
            await ffmpeg.generate_thumbnail(final, hook_text, thumb)
            print(f"[s6] thumbnail -> {thumb}", flush=True)
        except Exception as e:
            print(f"[s6] thumbnail gen failed (非致命): {e}", flush=True)

    db.execute("UPDATE jobs SET final_path=? WHERE job_id=?",
               (str(final), job_id))
    db.commit()
