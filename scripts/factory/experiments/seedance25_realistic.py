"""Helpers for the Seedance 2.5 photorealistic-person validation."""

from __future__ import annotations

import base64
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx


MODEL = "doubao-seedance-2-5-260628"
ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"


def _file_data_uri(path: Path, media_type: str) -> str:
    extension = path.suffix.lower().lstrip(".") or "png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type}/{extension};base64,{encoded}"


def build_payload(case: dict[str, Any], *, base_dir: Path) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": case["prompt"]}]
    if first_frame := case.get("first_frame"):
        path = (base_dir / first_frame).resolve()
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": _file_data_uri(path, "image")},
                "role": "first_frame",
            }
        )
    if reference_video := case.get("reference_video"):
        video_url = reference_video
        if not reference_video.startswith(("http://", "https://")):
            path = (base_dir / reference_video).resolve()
            video_url = _file_data_uri(path, "video")
        content.append(
            {
                "type": "video_url",
                "video_url": {"url": video_url},
                "role": "reference_video",
            }
        )
    if reference_audio := case.get("reference_audio"):
        path = (base_dir / reference_audio).resolve()
        content.append(
            {
                "type": "audio_url",
                "audio_url": {"url": _file_data_uri(path, "audio")},
                "role": "reference_audio",
            }
        )
    payload: dict[str, Any] = {
        "model": case.get("model", MODEL),
        "content": content,
        "duration": int(case["duration"]),
        "resolution": case["resolution"],
        "generate_audio": True,
    }
    if not first_frame:
        payload["ratio"] = case.get("ratio", "9:16")
    if "motion_strength" in case:
        payload["motion_strength"] = case["motion_strength"]
    return payload


def completed_result(case_id: str, result_path: Path, output_dir: Path) -> dict[str, Any] | None:
    if not result_path.exists():
        return None
    results = json.loads(result_path.read_text(encoding="utf-8"))
    prior = results.get(case_id)
    if not prior or prior.get("terminal") != "succeeded":
        return None
    output = output_dir / prior.get("output_file", "")
    return prior if output.is_file() else None


async def run_case(
    case_id: str,
    case: dict[str, Any],
    *,
    base_dir: Path,
    output_dir: Path,
    result_path: Path,
    api_key: str,
    transport: httpx.AsyncBaseTransport | None = None,
    poll_seconds: float = 5,
) -> dict[str, Any]:
    prior = completed_result(case_id, result_path, output_dir)
    if prior:
        return prior

    output_dir.mkdir(parents=True, exist_ok=True)
    payload = build_payload(case, base_dir=base_dir)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    started = time.perf_counter()
    async with httpx.AsyncClient(headers=headers, timeout=600, transport=transport) as client:
        response = await client.post(f"{ARK_BASE}/contents/generations/tasks", json=payload)
        if response.status_code >= 400:
            result = {
                "terminal": "failed",
                "http_status": response.status_code,
                "elapsed_seconds": round(time.perf_counter() - started, 2),
                "provider_error": response.text,
            }
            results = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
            results[case_id] = result
            result_path.write_text(
                json.dumps(results, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return result
        response.raise_for_status()
        task_id = response.json()["id"]
        while True:
            if poll_seconds:
                await asyncio.sleep(poll_seconds)
            status_response = await client.get(f"{ARK_BASE}/contents/generations/tasks/{task_id}")
            status_response.raise_for_status()
            provider_result = status_response.json()
            status = provider_result.get("status")
            if status == "failed":
                raise RuntimeError(f"Seedance task failed: {provider_result}")
            if status != "succeeded":
                continue
            video_response = await client.get(provider_result["content"]["video_url"], timeout=120)
            video_response.raise_for_status()
            output_file = f"{case_id}.mp4"
            video_bytes = video_response.content
            (output_dir / output_file).write_bytes(video_bytes)
            result = {
                "terminal": "succeeded",
                "task_id": task_id,
                "elapsed_seconds": round(time.perf_counter() - started, 2),
                "output_file": output_file,
                "size_bytes": len(video_bytes),
                "usage": provider_result.get("usage", {}),
                "provider_created_at": provider_result.get("created_at"),
                "provider_updated_at": provider_result.get("updated_at"),
            }
            results = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
            results[case_id] = result
            result_path.write_text(
                json.dumps(results, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return result
