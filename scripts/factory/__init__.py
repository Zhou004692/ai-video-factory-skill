"""AI Video Factory package."""
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = SKILL_ROOT / "data"
ASSETS_DIR = DATA_DIR / "assets"
REFERENCES_DIR = SKILL_ROOT / "references"
