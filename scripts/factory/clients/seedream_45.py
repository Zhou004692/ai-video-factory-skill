"""火山方舟 doubao-seedream-4.5 client。

为什么单独建一个文件:
- 现有 seedance.text_to_image 用的是 doubao-seedream-3.0-t2i,**没有 reference image**
- doubao-seedream-4.5 (251128 发布) 原生支持 1-14 张参考图,角色/服装/场景元素保持一致
- 用于做 portrait→scene 一致性升级,替代之前规划的 gpt-image-2 路径

环境变量:
  ARK_API_KEY            火山方舟 API Key (与 seedance 共用)
  SEEDREAM_45_MODEL_ID   火山控制台开通的 endpoint id (例 ep-2026... 或 doubao-seedream-4-5-251128)

API 形态(火山方舟 OpenAI 兼容 images API):
  POST https://ark.cn-beijing.volces.com/api/v3/images/generations
  {
    "model": "<model_id>",
    "prompt": "...",
    "image": ["url1", "url2"],          # 1-14 张参考图(URL 或 data URI)
    "sequential_image_generation": "disabled",
    "size": "1024x1024",
    "response_format": "url"
  }
  约束: image 数 + n(生成数) ≤ 15
"""
import base64
import os
from pathlib import Path

import httpx

ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = os.getenv("SEEDREAM_45_MODEL_ID", "doubao-seedream-4-5-251128")


def _headers() -> dict:
    key = os.environ.get("ARK_API_KEY")
    if not key:
        raise RuntimeError("ARK_API_KEY not set in .env")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _img_to_data_uri(path: str) -> str:
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    ext = Path(path).suffix.lstrip(".") or "png"
    return f"data:image/{ext};base64,{b64}"


def _to_image_arg(item: str) -> str:
    """把本地路径或 URL 都规整成 API 可吃的形态。"""
    if item.startswith(("http://", "https://", "data:")):
        return item
    return _img_to_data_uri(item)


async def i2i(prompt: str,
              references: list[str],
              size: str = "1440x2560",
              seed: int | None = None,
              model: str | None = None,
              timeout: int = 180) -> bytes:
    """图生图(带 reference image)。返回 PNG bytes。

    references: 1-14 张参考图,可混合本地路径 / HTTPS URL / data URI。
    seed: 给同一 prompt 出可复现结果。
    model: 覆盖 DEFAULT_MODEL,用于跨版本对比(如 5.0/5.0-lite)。
    """
    if not references:
        raise ValueError("seedream_45.i2i requires at least 1 reference image")
    if len(references) > 14:
        raise ValueError(f"too many references ({len(references)}), max 14")
    model_id = model or DEFAULT_MODEL
    payload = {
        "model": model_id,
        "prompt": prompt,
        "image": [_to_image_arg(r) for r in references],
        "sequential_image_generation": "disabled",
        "size": size,
        "response_format": "url",
    }
    if seed is not None:
        payload["seed"] = seed
    async with httpx.AsyncClient(timeout=timeout, headers=_headers()) as cli:
        r = await cli.post(f"{ARK_BASE}/images/generations", json=payload)
        if r.status_code >= 400:
            raise RuntimeError(
                f"seedream i2i {r.status_code}: {r.text[:800]} | "
                f"model={model_id}, refs={len(references)}"
            )
        body = r.json()
        url = body["data"][0]["url"]
        img = await cli.get(url, timeout=60)
        img.raise_for_status()
        return img.content


async def t2i(prompt: str,
              size: str = "1440x2560",
              seed: int | None = None,
              timeout: int = 120) -> bytes:
    """纯文生图(同模型,无 reference)。返回 PNG bytes。"""
    payload = {
        "model": DEFAULT_MODEL,
        "prompt": prompt,
        "size": size,
        "response_format": "url",
    }
    if seed is not None:
        payload["seed"] = seed
    async with httpx.AsyncClient(timeout=timeout, headers=_headers()) as cli:
        r = await cli.post(f"{ARK_BASE}/images/generations", json=payload)
        if r.status_code >= 400:
            raise RuntimeError(
                f"seedream-4.5 t2i {r.status_code}: {r.text[:600]} | "
                f"model={DEFAULT_MODEL}"
            )
        url = r.json()["data"][0]["url"]
        img = await cli.get(url, timeout=60)
        img.raise_for_status()
        return img.content


def estimate_cost_cents() -> int:
    """官方价 ~$0.04/张 ≈ ¥0.29 ≈ 29 分。给主管线 budget 用。"""
    return 29
