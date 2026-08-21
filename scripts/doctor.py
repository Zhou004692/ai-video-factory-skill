"""Run local, non-billing readiness checks for AI Video Factory."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Mapping

from dotenv import dotenv_values

from factory.media_runtime import (
    MediaRuntimeError,
    resolve_binary,
    resolve_subtitle_font,
)
from factory.runtime_profile import RuntimeProfileError, resolve_runtime_profile


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PACKAGES = ("click", "dotenv", "httpx", "yaml", "tos")
VIDEO_KEYS = (
    "ARK_API_KEY", "VOLC_AK", "VOLC_SK", "TOS_BUCKET",
)
FIXED_TTS_KEYS = ("VOLC_TTS_APPID", "VOLC_TTS_API_KEY")


def _configured(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text and not text.startswith("<") and not text.endswith("..."))


def diagnose(
    root: Path = ROOT,
    *,
    profile: str = "script",
    environ: Mapping[str, str] | None = None,
) -> dict:
    env_path = root / ".env"
    config = {
        key: str(value or "") for key, value in dotenv_values(env_path).items()
    }
    if environ is not None:
        for key, value in environ.items():
            config.setdefault(key, value)
    provider = str(config.get("DEFAULT_LLM_PROVIDER") or "doubao").strip().lower()
    if profile == "learning":
        required = ()
    elif profile == "script":
        required = ("ANTHROPIC_API_KEY",) if provider == "claude" else ("ARK_API_KEY",)
    else:
        audio_mode = str(
            config.get("DIALOGUE_AUDIO_MODE") or "seedance_native"
        ).strip().casefold()
        required = VIDEO_KEYS + (
            FIXED_TTS_KEYS if audio_mode != "seedance_native" else ()
        )
    try:
        runtime = resolve_runtime_profile(root=root, environ=config)
        runtime_public = runtime.as_public_dict()
        disk = shutil.disk_usage(runtime.data_dir)
        runtime_check = {"ok": True, "detail": runtime_public}
        data_check = {
            "ok": True,
            "detail": {
                "path": str(runtime.data_dir),
                "writable": True,
                "free_bytes": disk.free,
            },
        }
    except (RuntimeProfileError, OSError) as exc:
        runtime = None
        runtime_public = {
            "mode": str(config.get("FACTORY_RUNTIME") or "desktop"),
            "data_dir": str(config.get("FACTORY_DATA_DIR") or root / "data"),
        }
        runtime_check = {"ok": False, "detail": str(exc)}
        data_check = {"ok": False, "detail": str(exc)}

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
        "runtime": runtime_check,
        "data_directory": data_check,
    }
    if profile == "video":
        for binary in ("ffmpeg", "ffprobe"):
            try:
                resolved = resolve_binary(
                    binary, environ=config, which=shutil.which
                )
                checks[binary] = {"ok": True, "detail": str(resolved)}
            except MediaRuntimeError as exc:
                checks[binary] = {"ok": False, "detail": str(exc)}
        try:
            font = resolve_subtitle_font(environ=config)
            checks["subtitle_font"] = {"ok": True, "detail": str(font)}
        except MediaRuntimeError as exc:
            checks["subtitle_font"] = {"ok": False, "detail": str(exc)}
    return {
        "profile": profile,
        "runtime": runtime_public,
        "ready": all(item["ok"] for item in checks.values()),
        "checks": checks,
        "paid_provider_calls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", choices=("script", "video", "learning"), default="script"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = diagnose(profile=args.profile, environ=os.environ)
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
