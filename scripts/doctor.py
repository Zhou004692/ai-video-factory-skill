"""Run local, non-billing readiness checks for AI Video Factory."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PACKAGES = ("click", "dotenv", "httpx", "yaml", "tos")
VIDEO_KEYS = (
    "ARK_API_KEY", "VOLC_ASR_APPID", "VOLC_ASR_TOKEN",
    "VOLC_AK", "VOLC_SK", "TOS_BUCKET",
)


def _configured(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text and not text.startswith("<") and not text.endswith("..."))


def diagnose(root: Path = ROOT, *, profile: str = "script") -> dict:
    env_path = root / ".env"
    config = {**dotenv_values(env_path), **os.environ}
    provider = str(config.get("DEFAULT_LLM_PROVIDER") or "doubao").strip().lower()
    if profile == "script":
        required = ("ANTHROPIC_API_KEY",) if provider == "claude" else ("ARK_API_KEY",)
    else:
        required = VIDEO_KEYS
    checks = {
        "python": {"ok": sys.version_info >= (3, 11), "detail": sys.version.split()[0]},
        "packages": {
            "ok": all(importlib.util.find_spec(name) for name in REQUIRED_PACKAGES),
            "detail": list(REQUIRED_PACKAGES),
        },
        "environment_file": {"ok": env_path.is_file(), "detail": str(env_path)},
        "credentials": {
            "ok": all(_configured(config.get(key)) for key in required),
            "detail": {
                "provider": provider,
                **{key: _configured(config.get(key)) for key in required},
            },
        },
        "data_directory": {
            "ok": (root / "data").is_dir(), "detail": str(root / "data")
        },
    }
    if profile == "video":
        for binary in ("ffmpeg", "ffprobe"):
            checks[binary] = {
                "ok": shutil.which(binary) is not None,
                "detail": shutil.which(binary) or "not found",
            }
    return {
        "profile": profile,
        "ready": all(item["ok"] for item in checks.values()),
        "checks": checks,
        "paid_provider_calls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("script", "video"), default="script")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = diagnose(profile=args.profile)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"AI Video Factory doctor · {args.profile}")
        for name, item in report["checks"].items():
            print(f"[{'OK' if item['ok'] else 'FAIL'}] {name}: {item['detail']}")
        print(f"ready={str(report['ready']).lower()} paid_provider_calls=0")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
