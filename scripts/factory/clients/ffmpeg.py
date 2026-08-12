"""FFmpeg 封装:分镜归一化 + 字幕/hook 烧制 + 转场 + 片尾 CTA + 拼接 + 混入 BGM。"""
import asyncio
import os
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


# 中文字幕字体(雅黑粗体,短视频清晰度好)。可用 SUBTITLE_FONT env 覆盖。
SUBTITLE_FONT = os.getenv("SUBTITLE_FONT", "C:/Windows/Fonts/msyhbd.ttc")
# 品牌信息(片尾 CTA 用),可 env 覆盖
BRAND_NAME = os.getenv("BRAND_NAME", "")
BRAND_CTA = os.getenv("BRAND_CTA", "设备租赁不踩坑 · 关注了解更多")


def _esc(p: str) -> str:
    """ffmpeg filter 里的路径转义(Windows 盘符冒号 + 反斜杠)。"""
    return str(p).replace("\\", "/").replace(":", r"\:")


def _drawtext(textfile: Path, *, fontsize: int, y: str,
              color: str = "white", border: int = 4,
              enable: str | None = None) -> str:
    """构造一个 drawtext filter 片段。中文走 textfile(避免命令行编码坑)。"""
    parts = [
        f"fontfile='{_esc(SUBTITLE_FONT)}'",
        f"textfile='{_esc(textfile)}'",
        f"fontcolor={color}",
        f"fontsize={fontsize}",
        f"borderw={border}",
        "bordercolor=black@0.9",
        "x=(w-text_w)/2",
        f"y={y}",
        "line_spacing=10",
    ]
    if enable:
        parts.append(f"enable='{enable}'")
    return "drawtext=" + ":".join(parts)


def _subtitle_display_text(text: str) -> str:
    """Keep expressive punctuation but omit a terminal full stop on captions."""
    return text.strip().removesuffix("。").removesuffix(".")


def _timed_subtitle_filters(
        output_dir: Path, stem: str, cues: list[dict], *, width: int) -> list[str]:
    """Build one drawtext filter per subtitle cue with an explicit time window."""
    filters: list[str] = []
    for number, cue in enumerate(cues, start=1):
        text = _subtitle_display_text(str(cue.get("text") or ""))
        start = max(float(cue.get("start") or 0), 0.0)
        end = max(float(cue.get("end") or 0), start)
        if not text or end <= start:
            continue
        text_path = output_dir / f"{stem}_sub_{number:02d}.txt"
        text_path.write_text(text, encoding="utf-8")
        filters.append(_drawtext(
            text_path,
            fontsize=int(width * 0.048),
            y="h-text_h-h*0.12",
            color="white",
            border=4,
            enable=f"between(t,{start:.3f},{end:.3f})",
        ))
    return filters


def _hook_font_size(text: str, width: int) -> int:
    """Fit a single-line hook inside 90% of the output width."""
    visible_chars = max(len("".join(text.split())), 1)
    large_size = int(width * 0.072)
    fitted_size = int(width * 0.90 / visible_chars)
    return max(16, min(large_size, fitted_size))


def _thumbnail_hook_font_size(text: str, width: int) -> int:
    """Fit a thumbnail hook inside the branded frame's safe width."""
    visible_chars = max(len("".join(text.split())), 1)
    safe_width = max(width - 80, 1)
    large_size = int(width * 0.13)
    fitted_size = int(safe_width * 0.90 / visible_chars)
    return max(16, min(large_size, fitted_size))


async def _run(args: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        msg = (err or out).decode("utf-8", errors="replace")[-1500:]
        raise FFmpegError(f"ffmpeg failed: {msg}")


async def _ffprobe(args: list[str]) -> str:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise FFmpegError((err or out).decode())
    return out.decode("utf-8", errors="replace")


async def probe_duration(path: Path) -> float:
    out = await _ffprobe([
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    return float(out.strip())


async def has_audio_stream(path: Path) -> bool:
    out = await _ffprobe([
        "-select_streams", "a",
        "-show_entries", "stream=codec_type",
        "-of", "default=noprint_wrappers=1",
        str(path),
    ])
    return "codec_type=audio" in out


async def extract_audio_wav(video_path: Path, output_path: Path) -> None:
    """Extract a 16 kHz mono WAV suitable for ASR and speaker checks."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    await _run([
        "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000",
        str(output_path),
    ])


async def extract_frames(video_path: Path, output_dir: Path,
                         timestamps: list[float]) -> list[Path]:
    """Extract deterministic JPEG samples for later OCR/visual analysis."""
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, timestamp in enumerate(timestamps, start=1):
        path = output_dir / f"frame-{index:03d}.jpg"
        await _run([
            "-ss", f"{max(float(timestamp), 0.0):.3f}",
            "-i", str(video_path),
            "-frames:v", "1", "-q:v", "2", str(path),
        ])
        paths.append(path)
    return paths


async def normalize_shot(in_path: Path, out_path: Path,
                         width: int = 1080, height: int = 1920,
                         subtitle: str | None = None,
                         hook: str | None = None,
                         fade: bool = True,
                         duration: float | None = None) -> None:
    """归一化:统一 9:16 H.264 AAC 44.1k 立体声;无音频则补静音。

    subtitle: 该镜字幕(底部居中,白字黑边)。短视频 90% 静音看,必烧。
    hook:     0-3 秒大字钩子(仅第一镜传,顶部偏上,黄字)。
    fade:     首尾 0.15s 淡入淡出,缓解硬切割裂感(轻量转场)。
    duration: 该镜时长(算 fade-out 起点用);不传则不加 fade-out。
    """
    vf_chain = [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
    ]
    # 字幕(底部,距底 12%)
    if subtitle and subtitle.strip():
        sub_txt = out_path.parent / f"{out_path.stem}_sub.txt"
        sub_txt.write_text(subtitle.strip(), encoding="utf-8")
        vf_chain.append(_drawtext(
            sub_txt, fontsize=int(width * 0.048), y="h-text_h-h*0.12",
            color="white", border=4))
    # hook 大字(仅第一镜,0-3 秒,顶部 18%,黄字大号)
    if hook and hook.strip():
        hook_txt = out_path.parent / f"{out_path.stem}_hook.txt"
        hook_txt.write_text(hook.strip(), encoding="utf-8")
        vf_chain.append(_drawtext(
            hook_txt, fontsize=_hook_font_size(hook, width), y="h*0.16",
            color="yellow", border=6, enable="between(t,0,3)"))
    # fade 转场(轻量,缓解硬切)
    if fade:
        vf_chain.append("fade=t=in:st=0:d=0.15")
        if duration and duration > 0.4:
            vf_chain.append(f"fade=t=out:st={duration - 0.15:.2f}:d=0.15")
    vf = ",".join(vf_chain)

    has_audio = await has_audio_stream(in_path)
    if has_audio:
        args = [
            "-i", str(in_path),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-pix_fmt", "yuv420p",
            str(out_path),
        ]
    else:
        args = [
            "-i", str(in_path),
            "-f", "lavfi", "-i", "anullsrc=cl=stereo:r=44100",
            "-vf", vf,
            "-shortest",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-pix_fmt", "yuv420p",
            str(out_path),
        ]
    await _run(args)


async def cut_director_segment(
        video_path: Path, audio_path: Path, out_path: Path, *,
        video_start: float, audio_start: float, duration: float,
        subtitle: str | None = None, subtitle_cues: list[dict] | None = None,
        hook: str | None = None,
        width: int = 1080, height: int = 1920) -> None:
    """Cut local picture and native audio from independently chosen masters.

    This is the standard pipeline's core edit primitive: a B-roll master can
    provide picture while an unmodified Seedance presenter master provides the
    continuous native voice. Both streams are re-encoded to concat-safe output.
    """
    if duration <= 0:
        raise ValueError("director segment duration must be > 0")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    vf_chain = [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
    ]
    if subtitle_cues:
        vf_chain.extend(_timed_subtitle_filters(
            out_path.parent, out_path.stem, subtitle_cues, width=width,
        ))
    elif subtitle and subtitle.strip():
        sub_txt = out_path.parent / f"{out_path.stem}_sub.txt"
        sub_txt.write_text(subtitle.strip(), encoding="utf-8")
        vf_chain.append(_drawtext(
            sub_txt, fontsize=int(width * 0.048), y="h-text_h-h*0.12",
            color="white", border=4,
        ))
    if hook and hook.strip():
        hook_txt = out_path.parent / f"{out_path.stem}_hook.txt"
        hook_txt.write_text(hook.strip(), encoding="utf-8")
        vf_chain.append(_drawtext(
            hook_txt, fontsize=_hook_font_size(hook, width), y="h*0.16",
            color="yellow", border=6,
            enable=f"between(t,0,{min(duration, 3.0):.3f})",
        ))

    args = [
        "-ss", f"{max(video_start, 0):.3f}", "-i", str(video_path),
        "-ss", f"{max(audio_start, 0):.3f}", "-i", str(audio_path),
        "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", ",".join(vf_chain),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-pix_fmt", "yuv420p", "-shortest", "-movflags", "+faststart",
        str(out_path),
    ]
    await _run(args)


async def mux_video_audio(video: Path, audio: Path, out_path: Path) -> None:
    """把外部音频 mux 到视频上,替换/补足原音轨。

    使用 apad 让 audio 补静音以匹配视频长度;-shortest 兜底防 audio 反超。
    """
    await _run([
        "-i", str(video),
        "-i", str(audio),
        "-af", "apad",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        str(out_path),
    ])


def _native_narration_filter(*, ambient_lufs: float = -32.0,
                             narration_db: float = 0.0) -> str:
    """Build a stable ambience-under-narration mix across model outputs."""
    return (
        f"[0:a]loudnorm=I={ambient_lufs}:TP=-9.0:LRA=7.0[ambient];"
        f"[1:a]volume={narration_db}dB,apad[voice];"
        "[ambient][voice]amix=inputs=2:duration=first:"
        "dropout_transition=0:normalize=0,alimiter=limit=0.95[a]"
    )


async def mix_video_narration(video: Path, narration: Path, out_path: Path,
                              *, ambient_lufs: float = -32.0,
                              narration_db: float = 0.0) -> None:
    """Normalize Seedance ambience below narration without lowering the voice."""
    filt = _native_narration_filter(
        ambient_lufs=ambient_lufs, narration_db=narration_db
    )
    await _run([
        "-i", str(video),
        "-i", str(narration),
        "-filter_complex", filt,
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path),
    ])


async def make_cta_card(out_path: Path, *, width: int = 1080, height: int = 1920,
                        duration: float = 2.5,
                        brand: str | None = None,
                        cta: str | None = None) -> None:
    """生成片尾 CTA 卡片(深蓝底 + 品牌名大字 + CTA 文案 + 淡入)。
    输出格式与 normalize_shot 一致(H.264 + AAC 静音),可直接进 concat 列表。"""
    brand = brand or BRAND_NAME
    cta = cta or BRAND_CTA
    brand_txt = out_path.parent / f"{out_path.stem}_brand.txt"
    cta_txt = out_path.parent / f"{out_path.stem}_cta.txt"
    brand_txt.write_text(brand.strip(), encoding="utf-8")
    cta_txt.write_text(cta.strip(), encoding="utf-8")

    vf = ",".join([
        # 品牌名(居中偏上,大号白字)
        _drawtext(brand_txt, fontsize=int(width * 0.10),
                  y="h*0.40", color="white", border=5),
        # CTA 文案(居中偏下,中号品牌色)
        _drawtext(cta_txt, fontsize=int(width * 0.05),
                  y="h*0.55", color="0xFF9D5C", border=4),
        "fade=t=in:st=0:d=0.3",
    ])
    await _run([
        "-f", "lavfi", "-i", f"color=c=0x1A2740:s={width}x{height}:d={duration}",
        "-f", "lavfi", "-i", "anullsrc=cl=stereo:r=44100",
        "-vf", vf,
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ])


async def generate_thumbnail(video_path: Path, hook_text: str, out_path: Path,
                             *, ts: float = 4.5,
                             width: int = 720, height: int = 1280) -> None:
    """从成片抽帧 + 叠超大 hook + 品牌色边框 + 底部品牌色块,生成视频号封面 jpg。

    视频号封面决定 50% 点击率,当前管线默认抽首帧凑合,这步专门做"高点击率封面"。
    ts 默认 4.5s(避开成片首 3 秒已烧的 hook 时段,防止"双层 hook"叠加);
    封面大字在顶部偏下,不挡主角脸。
    """
    hook = (hook_text or BRAND_NAME).strip()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    hook_txt = out_path.parent / f"{out_path.stem}_hook.txt"
    brand_txt = out_path.parent / f"{out_path.stem}_brand.txt"
    hook_txt.write_text(hook, encoding="utf-8")
    brand_txt.write_text(BRAND_NAME, encoding="utf-8")

    # 动态字号:按 hook 字数算,保证一行内可完整显示不溢出
    # 公式:safe_w = width - 80(去边框+内边距),每字宽约 fontsize,留 8% 安全边
    # 上限 width*0.13(短 hook 大字冲击力),下限 width*0.06(超长 hook 仍清晰)
    hook_fs = _thumbnail_hook_font_size(hook, width)
    # 半透明黑底高度自适应字号(给字留充分上下 padding)
    band_h = int(hook_fs * 1.6)
    band_y = 80
    text_y = band_y + (band_h - hook_fs) // 2 - 5  # 视觉居中(略上偏)

    brand_fs = int(width * 0.06)
    brand_band_y = int(height * 0.85)
    brand_band_h = height - brand_band_y

    vf = ",".join([
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
        # 橙色品牌色厚边框
        f"drawbox=x=15:y=15:w={width-30}:h={height-30}:color=0xFF6500@0.85:t=10",
        # 半透明黑底(自适应字号高度)
        f"drawbox=x=0:y={band_y}:w={width}:h={band_h}:color=0x000000@0.55:t=fill",
        # hook 大字(动态字号 + 居中)
        _drawtext(hook_txt, fontsize=hook_fs, y=str(text_y),
                  color="yellow", border=8),
        # 底部深蓝色块作品牌锚
        f"drawbox=x=0:y={brand_band_y}:w={width}:h={brand_band_h}:color=0x1A2740:t=fill",
        # 品牌名叠在底部色块上
        _drawtext(brand_txt, fontsize=brand_fs, y="h*0.90",
                  color="white", border=3),
    ])
    await _run([
        "-ss", str(ts), "-i", str(video_path),
        "-frames:v", "1",
        "-vf", vf,
        "-update", "1",
        "-q:v", "2",
        str(out_path),
    ])


async def concat_shots(shot_paths: list[Path], out_path: Path) -> None:
    """concat demuxer 拼接。要求所有片段格式一致(请先 normalize_shot)。"""
    list_file = out_path.parent / "concat.txt"
    list_file.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in shot_paths),
        encoding="utf-8"
    )
    await _run([
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(out_path),
    ])


async def mix_bgm(video_with_audio: Path, bgm: Path, out: Path,
                  bgm_db: float = -16.0) -> None:
    """在已有(对白)音轨上叠加 BGM(低音量,循环填满,以视频长度为准)。"""
    filt = (
        f"[1:a]volume={bgm_db}dB,aloop=loop=-1:size=2e9[b];"
        f"[0:a][b]amix=inputs=2:duration=first:dropout_transition=0[a]"
    )
    await _run([
        "-i", str(video_with_audio),
        "-i", str(bgm),
        "-filter_complex", filt,
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out),
    ])


# 兼容旧 s6 调用(已被新版替代,保留以防 import 报错)
async def compose_final(video: Path, voice: Path, bgm: Path, out: Path,
                        voice_db: float = 0.0, bgm_db: float = -14.0) -> None:
    """旧管线:画面 + 单一旁白 + BGM → 成片。新管线不再用。"""
    filt = (
        f"[1:a]volume={voice_db}dB,apad[v];"
        f"[2:a]volume={bgm_db}dB,aloop=loop=-1:size=2e9[b];"
        f"[v][b]amix=inputs=2:duration=longest:dropout_transition=0[a]"
    )
    await _run([
        "-i", str(video),
        "-i", str(voice),
        "-i", str(bgm),
        "-filter_complex", filt,
        "-map", "0:v", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(out),
    ])
