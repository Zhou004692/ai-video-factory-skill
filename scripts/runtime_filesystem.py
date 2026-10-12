"""Physical I/O paths for Windows runtime trees; never persist these paths."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator
from urllib.parse import quote


def filesystem_path(path: Path) -> Path:
    """Opt into extended Windows paths only at the filesystem boundary."""
    if os.name != "nt":
        return Path(path)
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def iter_tree(root: Path) -> Iterator[Path]:
    """Traverse physical long paths while yielding ordinary logical paths."""
    physical_root = filesystem_path(root)
    for item in physical_root.rglob("*"):
        yield root / item.relative_to(physical_root)


def sqlite_readonly_uri(path: Path) -> str:
    # '?' in the Windows extended prefix is data, never a URI query delimiter.
    return "file:" + quote(os.fspath(filesystem_path(path)), safe="/:\\") + "?mode=ro"
