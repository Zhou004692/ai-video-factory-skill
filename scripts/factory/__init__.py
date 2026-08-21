"""AI Video Factory package."""
import os
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent.parent
_configured_data_dir = os.environ.get("FACTORY_DATA_DIR", "").strip()
DATA_DIR = (
    Path(_configured_data_dir).expanduser()
    if _configured_data_dir
    else SKILL_ROOT / "data"
)
if not DATA_DIR.is_absolute():
    DATA_DIR = SKILL_ROOT / DATA_DIR
DATA_DIR = DATA_DIR.resolve()
ASSETS_DIR = DATA_DIR / "assets"
REFERENCES_DIR = SKILL_ROOT / "references"
