"""Generate small deterministic local media without model or network calls."""
from __future__ import annotations

from pathlib import Path
import subprocess


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True)


def build_media_set(root: Path, *, duration_sec: int = 2) -> dict[str, Path]:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    video = root / "master.mp4"
    audio = root / "speech.wav"
    image = root / "anchor.png"
    _run([
        "ffmpeg", "-y", "-f", "lavfi", "-i",
        f"color=c=0x426B5B:s=320x568:r=25:d={duration_sec}",
        "-f", "lavfi", "-i", f"sine=frequency=880:duration={duration_sec}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(video),
    ])
    _run([
        "ffmpeg", "-y", "-f", "lavfi", "-i",
        f"sine=frequency=880:duration={duration_sec}",
        "-c:a", "pcm_s16le", str(audio),
    ])
    _run([
        "ffmpeg", "-y", "-f", "lavfi", "-i",
        "color=c=0xD9E7E0:s=320x568", "-frames:v", "1", str(image),
    ])
    return {"video": video, "audio": audio, "image": image}
