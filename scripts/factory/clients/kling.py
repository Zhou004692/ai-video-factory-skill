"""可灵(KlingAI)client。备选 video provider。

官方文档:https://app.klingai.com/global/dev/document-api
环境变量:KLING_ACCESS_KEY / KLING_SECRET_KEY (JWT 鉴权)
"""
import asyncio
import base64
import os
import time
from pathlib import Path

import httpx

KLING_BASE = "https://api.klingai.com"


def _jwt_token() -> str:
    """生成 JWT。简化实现:实际需用 PyJWT。"""
    try:
        import jwt  # noqa
    except ImportError:
        raise RuntimeError("kling client requires: pip install pyjwt")

    ak = os.environ.get("KLING_ACCESS_KEY")
    sk = os.environ.get("KLING_SECRET_KEY")
    if not (ak and sk):
        raise RuntimeError("KLING_ACCESS_KEY/SECRET_KEY not set")
    headers = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": ak,
        "exp": int(time.time()) + 1800,
        "nbf": int(time.time()) - 5,
    }
    return jwt.encode(payload, sk, algorithm="HS256", headers=headers)


def _headers():
    return {
        "Authorization": f"Bearer {_jwt_token()}",
        "Content-Type": "application/json",
    }


async def text_to_video(prompt: str,
                        reference_image: str | None = None,
                        duration: float = 10.0,
                        aspect_ratio: str = "9:16",
                        model_name: str = "kling-v2-1",
                        timeout: int = 600) -> bytes:
    payload = {
        "model_name": model_name,
        "prompt": prompt,
        "duration": str(int(round(duration))),
        "aspect_ratio": aspect_ratio,
        "mode": "pro",
    }
    if reference_image:
        with open(reference_image, "rb") as f:
            payload["image"] = base64.b64encode(f.read()).decode()

    async with httpx.AsyncClient(timeout=timeout) as cli:
        r = await cli.post(f"{KLING_BASE}/v1/videos/text2video",
                           json=payload, headers=_headers())
        r.raise_for_status()
        task_id = r.json()["data"]["task_id"]

        for _ in range(timeout // 5):
            await asyncio.sleep(5)
            s = await cli.get(f"{KLING_BASE}/v1/videos/text2video/{task_id}",
                              headers=_headers())
            s.raise_for_status()
            data = s.json()["data"]
            status = data.get("task_status")
            if status == "succeed":
                url = data["task_result"]["videos"][0]["url"]
                vid = await cli.get(url, timeout=120)
                return vid.content
            if status == "failed":
                raise RuntimeError(f"kling task {task_id} failed: {data}")
        raise TimeoutError(f"kling task {task_id} timed out")


def estimate_cost_cents(duration: float) -> int:
    """可灵 v2.1 Pro 大约 5 元 / 10s。"""
    return int(50 * duration)
