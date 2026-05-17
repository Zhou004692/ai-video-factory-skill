"""FFmpeg 封装:分镜归一化 + 拼接 + 混入 BGM。"""
import asyncio
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


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
                         width: int = 1080, height: int = 1920) -> None:
    """归一化:统一 9:16 H.264 AAC 44.1k 立体声;无音频则补静音。"""
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    )
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
