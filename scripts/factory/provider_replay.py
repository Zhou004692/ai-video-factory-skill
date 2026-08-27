"""Versioned, deterministic provider response replay.

Fixtures are selected by a canonical request fingerprint. The store is local,
stateful only for response sequencing, and never attempts a live fallback.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import threading
from typing import Mapping

from .provider_runtime import ReplayFixtureMissing


FIXTURE_SCHEMA_VERSION = 1
DEFAULT_ADAPTER_VERSION = "1"
DEFAULT_CAPABILITY_VERSION = "1"
_REDACTED_KEYS = frozenset({
    "access_key",
    "api_key",
    "authorization",
    "secret",
    "secret_access_key",
    "token",
})


class ReplayFixtureInvalid(ValueError):
    """The fixture cannot safely or deterministically serve requests."""


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key).casefold() not in _REDACTED_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        if "\\" in value or re.match(r"^[A-Za-z]:/", normalized):
            normalized = re.sub(r"/{2,}", "/", normalized)
        return normalized
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def fingerprint(
    operation: str,
    request: Mapping[str, object],
    *,
    adapter_version: str = DEFAULT_ADAPTER_VERSION,
    capability_version: str = DEFAULT_CAPABILITY_VERSION,
    fixture_schema_version: int = FIXTURE_SCHEMA_VERSION,
) -> str:
    payload = {
        "adapter_version": str(adapter_version),
        "capability_version": str(capability_version),
        "fixture_schema_version": int(fixture_schema_version),
        "operation": str(operation).strip(),
        "request": _canonical(request),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class ReplayStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        manifest_path = self.root / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ReplayFixtureInvalid(
                f"replay manifest does not exist: {manifest_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ReplayFixtureInvalid(
                f"replay manifest is invalid JSON: {manifest_path}"
            ) from exc
        if not isinstance(manifest, dict):
            raise ReplayFixtureInvalid("replay manifest must be a JSON object")
        if manifest.get("fixture_schema_version") != FIXTURE_SCHEMA_VERSION:
            raise ReplayFixtureInvalid(
                "fixture_schema_version must be "
                f"{FIXTURE_SCHEMA_VERSION}"
            )
        self.fixture_id = str(manifest.get("fixture_id") or "").strip()
        if not self.fixture_id:
            raise ReplayFixtureInvalid("fixture_id is required")
        raw_operations = manifest.get("operations")
        if not isinstance(raw_operations, list):
            raise ReplayFixtureInvalid("operations must be a list")
        self._entries: dict[tuple[str, str], tuple[dict, ...]] = {}
        for position, item in enumerate(raw_operations, 1):
            if not isinstance(item, dict):
                raise ReplayFixtureInvalid(
                    f"operation entry #{position} must be an object"
                )
            operation = str(item.get("operation") or "").strip()
            request_hash = str(item.get("request_hash") or "").strip()
            responses = item.get("responses")
            if (
                not operation
                or not request_hash.startswith("sha256:")
                or not isinstance(responses, list)
                or not responses
                or not all(isinstance(response, dict) for response in responses)
            ):
                raise ReplayFixtureInvalid(
                    f"operation entry #{position} is incomplete"
                )
            key = operation, request_hash
            if key in self._entries:
                raise ReplayFixtureInvalid(
                    f"duplicate replay operation: {operation} {request_hash}"
                )
            self._entries[key] = tuple(deepcopy(responses))
        self._positions: dict[tuple[str, str], int] = {}
        self._lock = threading.RLock()

    def lookup(self, operation: str, request: Mapping[str, object]) -> dict:
        request_hash = fingerprint(operation, request)
        key = str(operation).strip(), request_hash
        with self._lock:
            responses = self._entries.get(key)
            if responses is None:
                raise ReplayFixtureMissing(
                    f"replay fixture missing: operation={key[0]} "
                    f"request_hash={request_hash}"
                )
            position = self._positions.get(key, 0)
            response = deepcopy(responses[position])
            self._positions[key] = min(position + 1, len(responses) - 1)
        media = str(response.pop("media", "") or "").strip()
        if media:
            media_path = (self.root / media).resolve()
            if not media_path.is_relative_to(self.root) or not media_path.is_file():
                raise ReplayFixtureInvalid(
                    f"replay media is missing or outside fixture root: {media}"
                )
            response["media_path"] = str(media_path)
        response.update({
            "fixture_id": self.fixture_id,
            "mode": "replay",
            "paid": False,
            "request_hash": request_hash,
        })
        return response
