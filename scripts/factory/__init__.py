"""AI Video Factory package."""
import os
from pathlib import Path

from runtime_paths import resolve_data_dir

SKILL_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = resolve_data_dir(SKILL_ROOT, os.environ)
ASSETS_DIR = DATA_DIR / "assets"
REFERENCES_DIR = SKILL_ROOT / "references"
