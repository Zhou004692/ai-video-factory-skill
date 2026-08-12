"""Cross-platform installer for the AI Video Factory skill runtime."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import venv


ROOT = Path(__file__).resolve().parents[1]


def _venv_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def install(root: Path = ROOT, *, create_venv: bool = True,
            install_dependencies: bool = True) -> dict:
    root = root.resolve()
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env_file = root / ".env"
    env_created = False
    if not env_file.exists():
        shutil.copy2(root / "templates" / "env.example", env_file)
        env_created = True

    venv_created = False
    if create_venv and not _venv_python(root).exists():
        venv.EnvBuilder(with_pip=True).create(root / ".venv")
        venv_created = True

    if install_dependencies:
        python = _venv_python(root)
        if not python.exists():
            raise RuntimeError("virtual environment is missing; remove --no-venv")
        subprocess.run(
            [str(python), "-m", "pip", "install", "-r", str(root / "scripts" / "requirements.txt")],
            check=True,
        )

    return {
        "root": str(root),
        "env_created": env_created,
        "venv_created": venv_created,
        "data_dir": str(data_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--no-venv", action="store_true")
    parser.add_argument("--no-deps", action="store_true")
    args = parser.parse_args()
    result = install(
        args.root,
        create_venv=not args.no_venv,
        install_dependencies=not args.no_deps,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
