"""Immutable per-job generation configuration.

The management console freezes this snapshot before paid work. Runtime code
must prefer it over mutable process defaults so preflight, budget and provider
requests describe the same job.
"""
from __future__ import annotations

from collections.abc import Mapping
import os

from .clients import seedance
from .video_routing import normalize_audio_mode


GENERATION_SNAPSHOT_KEY = "generation_snapshot"
GENERATION_SNAPSHOT_VERSION = 1
DEFAULT_VIDEO_MODEL = "doubao-seedance-2-5-260628"
DEFAULT_EXECUTION_ENGINE = "legacy"
EXECUTION_ENGINES = ("legacy", "graph_v2")


def resolve_default_execution_engine(
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve the engine for jobs crossing the script-to-production boundary.

    The fallback deliberately remains ``legacy`` so rollback is possible by
    removing the opt-in configuration.  Formal deployments switch new jobs to
    V2 with ``PRODUCTION_EXECUTION_ENGINE=graph_v2``; persisted jobs are never
    migrated by this resolver.
    """
    env = os.environ if env is None else env
    value = str(
        env.get("PRODUCTION_EXECUTION_ENGINE") or DEFAULT_EXECUTION_ENGINE
    ).strip()
    if value not in EXECUTION_ENGINES:
        raise ValueError(
            "PRODUCTION_EXECUTION_ENGINE must be legacy or graph_v2"
        )
    return value


def _positive_int(value: object, *, default: int, label: str) -> int:
    raw = default if value in (None, "") else value
    try:
        normalized = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if normalized <= 0:
        raise ValueError(f"{label} must be positive")
    return normalized


def _nonnegative_int(value: object, *, default: int, label: str) -> int:
    raw = default if value in (None, "") else value
    try:
        normalized = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if normalized < 0:
        raise ValueError(f"{label} must be non-negative")
    return normalized


def _truthy(value: object, *, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _configured_value(
    settings: Mapping[str, object], field: str, fallback: object
) -> object:
    """Preserve explicit invalid values so validation cannot silently skip them."""
    return settings[field] if field in settings else fallback


def freeze_generation_snapshot(
    settings: Mapping[str, object] | None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Resolve mutable defaults once and return a validated JSON snapshot."""
    settings = settings or {}
    env = env or os.environ
    model = str(
        settings.get("video_model")
        or settings.get("model")
        or env.get("ARK_VIDEO_PRIMARY_MODEL")
        or env.get("ARK_VIDEO_MODEL")
        or DEFAULT_VIDEO_MODEL
    ).strip()
    profile = seedance.capability_profile(model)
    resolution = str(
        settings.get("resolution") or env.get("ARK_VIDEO_RESOLUTION") or "720p"
    ).strip().casefold()
    if resolution not in profile["resolutions"]:
        raise ValueError(
            f"resolution {resolution!r} is not supported by {model}"
        )
    output_format = str(
        settings.get("output_format")
        or env.get("ARK_VIDEO_OUTPUT_FORMAT")
        or "mp4"
    ).strip().casefold()
    if output_format != "mp4":
        raise ValueError(
            "managed pipeline only supports mp4 until MOV conversion is available"
        )
    ratio = str(
        settings.get("orientation") or settings.get("ratio") or "9:16"
    ).strip()
    if ratio not in {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}:
        raise ValueError("ratio is not supported by the production pipeline")
    reference_limit = int(profile["reference_limits"]["reference_video"])
    reference_budget = _positive_int(
        _configured_value(
            settings,
            "reference_video_budget",
            env.get("SEEDANCE_PRODUCTION_REFERENCE_VIDEO_BUDGET"),
        ),
        default=3,
        label="reference_video_budget",
    )
    if reference_budget > reference_limit:
        raise ValueError(
            f"reference_video_budget must not exceed provider limit {reference_limit}"
        )
    max_concurrent = _positive_int(
        _configured_value(
            settings,
            "max_concurrent_masters",
            env.get("SEEDANCE_MAX_CONCURRENT_MASTERS")
            or env.get("MAX_CONCURRENT_SHOTS"),
        ),
        default=3,
        label="max_concurrent_masters",
    )
    return {
        "version": GENERATION_SNAPSHOT_VERSION,
        "model": model,
        "resolution": resolution,
        "output_format": output_format,
        "ratio": ratio,
        "audio_mode": normalize_audio_mode(
            settings.get("audio_mode") or env.get("DIALOGUE_AUDIO_MODE"),
            strict=True,
        ),
        "reference_video_budget": reference_budget,
        "max_concurrent_masters": max_concurrent,
        "qa_enabled": _truthy(
            settings.get("qa_enabled")
            if "qa_enabled" in settings else env.get("VIDEO_QA_ENABLED"),
            default=True,
        ),
        "max_auto_retries": _nonnegative_int(
            settings.get("max_auto_retries")
            if "max_auto_retries" in settings
            else env.get("SEEDANCE_MAX_AUTO_RETRIES"),
            default=1,
            label="max_auto_retries",
        ),
        "auto_retry_budget_cents": _nonnegative_int(
            settings.get("auto_retry_budget_cents")
            if "auto_retry_budget_cents" in settings
            else env.get("SEEDANCE_AUTO_RETRY_BUDGET_CENTS"),
            default=800,
            label="auto_retry_budget_cents",
        ),
        "job_budget_cents": _positive_int(
            _configured_value(
                settings,
                "budget_cents",
                _configured_value(
                    settings,
                    "job_budget_cents",
                    env.get("SEEDANCE_JOB_BUDGET_CENTS"),
                ),
            ),
            default=2500,
            label="job_budget_cents",
        ),
    }


def generation_snapshot(
    storyboard_or_settings: Mapping[str, object] | None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Read a frozen snapshot, falling back only for pre-snapshot legacy jobs."""
    values = storyboard_or_settings or {}
    settings = values.get("production_settings") if isinstance(values, dict) else None
    if not isinstance(settings, Mapping):
        settings = values
    frozen = settings.get(GENERATION_SNAPSHOT_KEY)
    if isinstance(frozen, Mapping):
        required = {
            "model", "resolution", "output_format", "ratio", "audio_mode",
            "reference_video_budget", "max_concurrent_masters", "qa_enabled",
            "max_auto_retries", "auto_retry_budget_cents", "job_budget_cents",
        }
        missing = sorted(required - set(frozen))
        if missing:
            raise ValueError(
                "generation snapshot is incomplete: " + ", ".join(missing)
            )
        return dict(frozen)
    return freeze_generation_snapshot(settings, env=env)


def attach_runtime_snapshot(
    unit: Mapping[str, object], snapshot: Mapping[str, object]
) -> dict:
    return {**unit, "_generation_snapshot": dict(snapshot)}


def snapshot_from_unit(unit: Mapping[str, object]) -> dict | None:
    value = unit.get("_generation_snapshot")
    return dict(value) if isinstance(value, Mapping) else None
