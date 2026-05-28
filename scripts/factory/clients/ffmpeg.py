"""FFmpeg 封装:分镜归一化 + 字幕/hook 烧制 + 转场 + 片尾 CTA + 拼接 + 混入 BGM。"""
import asyncio
import os
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


# 中文字幕字体(雅黑粗体,短视频清晰度好)。可用 SUBTITLE_FONT env 覆盖。
SUBTITLE_FONT = os.getenv("SUBTITLE_FONT", "C:/Windows/Fonts/msyhbd.ttc")
# 品牌信息(片尾 CTA 用),可 env 覆盖
BRAND_NAME = os.getenv("BRAND_NAME", "众能联合")
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
            hook_txt, fontsize=int(width * 0.072), y="h*0.16",
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
