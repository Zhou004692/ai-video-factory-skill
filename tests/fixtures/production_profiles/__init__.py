"""Immutable creative-production reliability profiles."""
from __future__ import annotations

import json
from pathlib import Path


PROFILE_DIR = Path(__file__).resolve().parent


def load_profile_fixture(profile_id: str) -> dict:
    path = PROFILE_DIR / f"{profile_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_profile_fixtures() -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(PROFILE_DIR.glob("*.json"))
    ]
