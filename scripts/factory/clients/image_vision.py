"""Ark multimodal JSON analysis for local source frames."""
from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx

from .. import provider_runtime
from .claude import _extract_json
from .doubao_llm import ARK_BASE


def _image_data_uri(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".") or "jpeg"
    mime = "jpeg" if suffix in {"jpg", "jpeg"} else suffix
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/{mime};base64,{encoded}"


def request_payload(prompt: str, *, image_path: str | Path | None = None,
                    max_tokens: int = 1200,
                    model: str | None = None) -> dict:
    resolved_model = model or os.getenv(
        "ARK_SOURCE_ANALYSIS_MODEL",
        os.getenv("ARK_VIDEO_QA_MODEL", "doubao-seed-2-0-pro-260215"),
    )
    content: list[dict] = []
    if image_path is not None:
        content.append({
            "type": "image_url",
            "image_url": {"url": _image_data_uri(Path(image_path))},
        })
    content.append({"type": "text", "text": prompt})
    return {
        "model": resolved_model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": content}],
    }


def analyze_json(prompt: str, *, image_path: str | Path | None = None,
                 max_tokens: int = 1200,
                 model: str | None = None) -> tuple[dict, dict]:
    payload = request_payload(
        prompt, image_path=image_path, max_tokens=max_tokens, model=model
    )
    runtime = provider_runtime.from_environment()
    if runtime.mode == "replay":
        replay = runtime.lookup("doubao.vision.analyze_json", payload)
        result = replay.get("result")
        usage = replay.get("usage")
        if not isinstance(result, dict) or not isinstance(usage, dict):
            raise provider_runtime.ReplayFixtureMissing(
                "Vision replay response requires result and usage objects"
            )
        return result, {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
        }

    key = os.environ.get("ARK_API_KEY")
    if not key:
        raise RuntimeError("ARK_API_KEY not set in .env")
    with httpx.Client(timeout=120) as client:
        response = client.post(
            f"{ARK_BASE}/chat/completions", json=payload,
            headers={"Authorization": f"Bearer {key}"},
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"source vision {response.status_code}: {response.text[:800]} | "
            f"model={payload['model']}"
        )
    body = response.json()
    result = _extract_json(body["choices"][0]["message"]["content"])
    usage = body.get("usage") or {}
    return result, {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
    }
