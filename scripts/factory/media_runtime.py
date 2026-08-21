"""Cross-platform FFmpeg, FFprobe and Chinese subtitle-font resolution."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
from typing import Callable, Iterable, Mapping


class MediaRuntimeError(RuntimeError):
    """A required local media dependency is unavailable."""


def resolve_binary(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Path:
    values = dict(os.environ if environ is None else environ)
    key = f"{name.upper()}_PATH"
    explicit = str(values.get(key) or "").strip()
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise MediaRuntimeError(f"{key} 指向的文件不存在：{path}")
        return path

    discovered = which(name)
    if discovered:
        return Path(discovered).expanduser().resolve()

    local_app_data = str(values.get("LOCALAPPDATA") or "").strip()
    if local_app_data:
        candidate = (
            Path(local_app_data)
            / "Microsoft"
            / "WinGet"
            / "Links"
            / f"{name}.exe"
        )
        if candidate.is_file():
            return candidate.resolve()

    label = "FFmpeg" if name.casefold() == "ffmpeg" else "FFprobe"
    raise MediaRuntimeError(
        f"未找到 {label}。请重新双击安装程序，或在配置中指定 {key}"
    )


def _default_font_candidates(platform: str) -> tuple[Path, ...]:
    if platform == "win32":
        windows = Path(os.environ.get("WINDIR") or "C:/Windows")
        return (
            windows / "Fonts" / "msyhbd.ttc",
            windows / "Fonts" / "msyh.ttc",
            windows / "Fonts" / "simhei.ttf",
        )
    if platform == "darwin":
        return (
            Path("/System/Library/Fonts/PingFang.ttc"),
            Path("/System/Library/Fonts/STHeiti Medium.ttc"),
        )
    return (
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    )


def resolve_subtitle_font(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    candidates: Iterable[Path] | None = None,
) -> Path:
    values = dict(os.environ if environ is None else environ)
    explicit = str(values.get("SUBTITLE_FONT") or "").strip()
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise MediaRuntimeError(f"字幕字体不存在：{path}")
        return path

    platform = platform or sys.platform
    search = tuple(candidates) if candidates is not None else _default_font_candidates(platform)
    for candidate in search:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path.resolve()
    raise MediaRuntimeError(
        "未找到可显示中文的字幕字体。Windows 请修复微软雅黑，Linux 请安装 fonts-noto-cjk，"
        "或配置 SUBTITLE_FONT"
    )
