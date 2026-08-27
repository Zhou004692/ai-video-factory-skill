"""Select live providers or deterministic zero-cost fixture replay.

Replay mode is deliberately fail-closed. A missing fixture must stop the test
before any production client has a chance to construct an outbound request.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
from typing import Literal, Mapping


ProviderMode = Literal["live", "replay"]


class ReplayFixtureMissing(RuntimeError):
    """A replay request has no local response and may not fall back to live."""

    stable_code = "replay_fixture_missing"
    error_code = stable_code
    non_retryable = True
    retryable = False


@lru_cache(maxsize=16)
def _shared_replay_store(fixture_root: Path):
    """Keep stateful fixture sequences stable across runtime reconstruction."""
    from .provider_replay import ReplayStore

    return ReplayStore(fixture_root)


@dataclass(frozen=True)
class ProviderRuntime:
    mode: ProviderMode
    fixture_root: Path | None = None

    def lookup(self, operation: str, request: Mapping[str, object]) -> dict:
        if self.mode != "replay":
            raise RuntimeError("provider fixture lookup requires replay mode")
        if self.fixture_root is None or not self.fixture_root.is_dir():
            raise ReplayFixtureMissing(
                "replay fixture root does not exist: "
                f"{self.fixture_root or '<not configured>'}"
            )
        return _shared_replay_store(self.fixture_root).lookup(operation, request)


def from_environment(
    environ: Mapping[str, str] | None = None,
) -> ProviderRuntime:
    values = os.environ if environ is None else environ
    raw_mode = str(values.get("FACTORY_PROVIDER_MODE", "live") or "live")
    mode = raw_mode.strip().casefold()
    if mode not in {"live", "replay"}:
        raise ValueError(
            "FACTORY_PROVIDER_MODE must be either 'live' or 'replay'"
        )
    configured_root = str(
        values.get("FACTORY_REPLAY_FIXTURE_ROOT", "") or ""
    ).strip()
    fixture_root = (
        Path(configured_root).expanduser().resolve()
        if configured_root else None
    )
    return ProviderRuntime(mode=mode, fixture_root=fixture_root)
