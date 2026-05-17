"""FFmpeg 封装。负责拼接分镜 + 混音口播 + BGM。"""
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


async def probe_duration(path: Path) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise FFmpegError(err.decode())
    return float(out.decode().strip())


async def concat_shots(shot_paths: list[Path], out_path: Path) -> None:
    """concat demuxer 拼接。要求所有片段编码一致(同来源通常一致)。"""
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


async def compose_final(video: Path, voice: Path, bgm: Path, out: Path,
                        voice_db: float = 0.0, bgm_db: float = -14.0) -> None:
    """拼好的画面 + 口播 + BGM → 最终成片。

    voice_db / bgm_db 用 dB 控制相对音量。BGM 默认压低 14dB。
    """
    # 音轨混合:口播主、BGM 衬底,以视频时长为准
    filt = (
        f"[1:a]volume={voice_db}dB[v];"
        f"[2:a]volume={bgm_db}dB,aloop=loop=-1:size=2e9[b];"
        f"[v][b]amix=inputs=2:duration=first:dropout_transition=0[a]"
    )
    await _run([
        "-i", str(video),
        "-i", str(voice),
        "-i", str(bgm),
        "-filter_complex", filt,
        "-map", "0:v",
        "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(out),
    ])
