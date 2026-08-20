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

from .. import ASSETS_DIR, subtitles
from ..db import conn
from ..clients import bigasr, ffmpeg
from ..review_candidates import finalize_accepted_candidates


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
        duration = float(edit["duration"])
        subtitle = edit.get("subtitle")
        subtitle_cues = edit.get("subtitle_cues") or []
        if subtitle and not subtitle_cues:
            lead = min(0.15, duration / 4)
            subtitle_cues = subtitles.timed_cues(
                subtitle, lead, max(lead, duration - 0.15)
            )
            subtitle = None
        segments.append({
            "idx": int(edit["idx"]),
            "video_path": paths[video_idx],
            "native_audio": paths[audio_idx],
            "video_start": float(edit.get("video_start") or 0),
            "audio_start": float(edit.get("audio_start") or 0),
            "duration": duration,
            "subtitle": subtitle,
            "subtitle_cues": subtitle_cues,
            "continuity_relation": str(
                edit.get("continuity_relation") or ""
            ),
            "transition_type": str(edit.get("transition_type") or "none"),
            "transition_duration_sec": float(
                edit.get("transition_duration_sec") or 0
            ),
            "transition_reason": str(edit.get("transition_reason") or ""),
            "preserve_audio_continuity": bool(
                edit.get("preserve_audio_continuity")
            ),
            "video_fade_in_sec": 0.0,
            "video_fade_out_sec": 0.0,
        })
    for index, segment in enumerate(segments):
        if index == 0 or segment["transition_type"] != "dip_to_black":
            continue
        previous = segments[index - 1]
        requested = max(float(segment["transition_duration_sec"]), 0.0)
        duration = min(
            requested,
            0.25,
            float(previous["duration"]) / 2,
            float(segment["duration"]) / 2,
        )
        previous["video_fade_out_sec"] = duration
        segment["video_fade_in_sec"] = duration
    return segments


def _extend_terminal_edit_for_speech(
    storyboard: dict,
    master_idx: int,
    transcription: bigasr.Transcription,
    *,
    available_duration: float,
    tail_guard_sec: float = 0.15,
) -> bool:
    """Keep the final edit open until native speech and its closing breath end."""
    speech_ends = [
        utterance.end_ms for utterance in transcription.utterances
        if utterance.end_ms is not None
    ]
    if transcription.speech_end_ms is not None:
        speech_ends.append(transcription.speech_end_ms)
    if not speech_ends:
        return False
    edits = [
        edit for edit in (storyboard.get("shots") or [])
        if int(edit.get(
            "audio_master_idx", edit.get("video_master_idx", -1)
        )) == int(master_idx)
    ]
    if not edits:
        return False
    terminal = max(
        edits,
        key=lambda edit: (
            float(edit.get("audio_start") or 0)
            + float(edit.get("duration") or 0)
        ),
    )
    edit_start = float(terminal.get("audio_start") or 0)
    current_end = edit_start + float(terminal.get("duration") or 0)
    target_end = min(
        float(available_duration),
        max(speech_ends) / 1000.0 + max(0.0, tail_guard_sec),
    )
    if target_end <= current_end + 0.001:
        return False
    terminal["duration"] = round(target_end - edit_start, 3)
    return True


def _align_internal_edits_to_speech_pauses(
    storyboard: dict,
    master_idx: int,
    transcription: bigasr.Transcription,
    *,
    max_shift_sec: float = 1.0,
) -> bool:
    """Move internal picture cuts off spoken phrases onto ASR utterance ends."""
    edits = sorted(
        (
            edit for edit in (storyboard.get("shots") or [])
            if int(edit.get(
                "audio_master_idx", edit.get("video_master_idx", -1)
            )) == int(master_idx)
        ),
        key=lambda edit: float(edit.get("audio_start") or 0),
    )
    if len(edits) < 2:
        return False
    pauses = sorted({
        round(utterance.end_ms / 1000.0, 3)
        for utterance in transcription.utterances
        if utterance.end_ms is not None
    })
    if not pauses:
        return False

    changed = False
    for left, right in zip(edits, edits[1:]):
        left_start = float(left.get("audio_start") or 0)
        right_start = float(right.get("audio_start") or 0)
        right_end = right_start + float(right.get("duration") or 0)
        candidates = [
            pause for pause in pauses
            if left_start + 0.5 < pause < right_end - 0.5
            and abs(pause - right_start) <= max_shift_sec
        ]
        if not candidates:
            continue
        boundary = min(candidates, key=lambda pause: abs(pause - right_start))
        if abs(boundary - right_start) <= 0.001:
            continue
        left["duration"] = round(boundary - left_start, 3)
        right["audio_start"] = boundary
        right["duration"] = round(right_end - boundary, 3)
        changed = True
    return changed


def _synchronize_master_contract_durations(storyboard: dict) -> bool:
    """Keep validated master bounds consistent with accepted edit timelines."""
    required: dict[int, float] = {}
    for edit in storyboard.get("shots") or []:
        duration = float(edit.get("duration") or 0)
        video_idx = int(edit.get("video_master_idx", -1))
        audio_idx = int(edit.get("audio_master_idx", video_idx))
        required[video_idx] = max(
            required.get(video_idx, 0.0),
            float(edit.get("video_start") or 0) + duration,
        )
        required[audio_idx] = max(
            required.get(audio_idx, 0.0),
            float(edit.get("audio_start") or 0) + duration,
        )
    changed = False
    for master in storyboard.get("masters") or []:
        idx = int(master.get("idx") or -1)
        duration = round(max(
            float(master.get("duration") or 0),
            required.get(idx, 0.0),
        ), 3)
        if duration and abs(duration - float(master.get("duration") or 0)) > 0.001:
            master["duration"] = duration
            changed = True
    return changed


async def _refresh_director_speech_timeline(
    job_id: str,
    storyboard: dict,
    master_rows,
    database,
) -> bool:
    """Backfill ASR after manual review and align cuts to actual native speech."""
    if not (storyboard.get("production_settings") or {}).get(
            "asr_enabled", True):
        return False
    paths = {int(row["idx"]): Path(row["video_path"]) for row in master_rows}
    master_types = {
        int(master["idx"]): str(master.get("type") or "")
        for master in (storyboard.get("masters") or [])
        if master.get("idx") is not None
    }
    pending: set[int] = set()
    for edit in storyboard.get("shots") or []:
        audio_idx = int(edit.get(
            "audio_master_idx", edit.get("video_master_idx", -1)
        ))
        if (
            int(edit.get("speech_timeline_version") or 0) < 2
            and (
                edit.get("subtitle")
                or edit.get("subtitle_cues")
                or master_types.get(audio_idx) == "dialogue"
            )
        ):
            pending.add(audio_idx)

    changed = False
    for master_idx in sorted(pending):
        master_path = paths.get(master_idx)
        if master_path is None:
            continue
        asr_source = master_path.with_suffix(".wav")
        if not asr_source.exists():
            asr_source = master_path
        try:
            transcription = await asyncio.to_thread(
                bigasr.transcribe_detailed, asr_source
            )
        except Exception as exc:
            raise RuntimeError(
                f"母片 #{master_idx} 的 ASR 时间轴生成失败，已停止合成以避免字幕错位：{exc}"
            ) from exc

        related = [
            edit for edit in (storyboard.get("shots") or [])
            if int(edit.get(
                "audio_master_idx", edit.get("video_master_idx", -1)
            )) == master_idx
        ]
        terminal = max(
            related,
            key=lambda edit: (
                float(edit.get("audio_start") or 0)
                + float(edit.get("duration") or 0)
            ),
        )
        video_idx = int(terminal.get("video_master_idx", master_idx))
        video_path = paths.get(video_idx)
        audio_duration = await ffmpeg.probe_duration(master_path)
        video_duration = (
            await ffmpeg.probe_duration(video_path)
            if video_path is not None else audio_duration
        )
        available_end = min(
            audio_duration,
            float(terminal.get("audio_start") or 0)
            + max(
                0.0,
                video_duration - float(terminal.get("video_start") or 0),
            ),
        )
        changed = _align_internal_edits_to_speech_pauses(
            storyboard, master_idx, transcription
        ) or changed
        changed = _extend_terminal_edit_for_speech(
            storyboard,
            master_idx,
            transcription,
            available_duration=available_end,
        ) or changed
        changed = subtitles.apply_master_transcription(
            storyboard, master_idx, transcription
        ) or changed
        for edit in related:
            if int(edit.get("speech_timeline_version") or 0) != 2:
                edit["speech_timeline_version"] = 2
                changed = True

    changed = _synchronize_master_contract_durations(storyboard) or changed

    if changed:
        encoded = json.dumps(storyboard, ensure_ascii=False)
        database.execute(
            "UPDATE jobs SET storyboard=? WHERE job_id=?",
            (encoded, job_id),
        )
        database.commit()
        storyboard_path = ASSETS_DIR / job_id / "storyboard.json"
        storyboard_path.parent.mkdir(parents=True, exist_ok=True)
        storyboard_path.write_text(
            json.dumps(storyboard, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return changed


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
        video_fade_in_sec=segment.get("video_fade_in_sec", 0),
        video_fade_out_sec=segment.get("video_fade_out_sec", 0),
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
        await _refresh_director_speech_timeline(job_id, sb, rows, db)
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

    # 5. 默认封面与最终时间轴首帧同源，避免点击播放时画面跳变。
    #    强营销封面作为独立可选资产保存，不再覆盖默认播放封面。
    if os.getenv("GEN_THUMBNAIL", "true").lower() == "true":
        thumb = base / "thumbnail.jpg"
        try:
            await ffmpeg.generate_continuity_cover(final, thumb)
            print(f"[s6] continuity cover -> {thumb}", flush=True)
        except Exception as e:
            print(f"[s6] continuity cover failed (非致命): {e}", flush=True)
    if os.getenv("GEN_MARKETING_THUMBNAIL", "true").lower() == "true":
        marketing_thumb = base / "marketing_thumbnail.jpg"
        try:
            await ffmpeg.generate_thumbnail(final, hook_text, marketing_thumb)
            print(f"[s6] marketing thumbnail -> {marketing_thumb}", flush=True)
        except Exception as e:
            print(f"[s6] marketing thumbnail failed (非致命): {e}", flush=True)

    db.execute("UPDATE jobs SET final_path=? WHERE job_id=?",
               (str(final), job_id))
    db.commit()
    finalize_accepted_candidates(db, ASSETS_DIR, job_id)
