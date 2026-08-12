"""OpenAI gpt-image-2 client。

官方文档:
- 生成:  https://developers.openai.com/api/reference/resources/images/methods/generate
- 编辑:  /v1/images/edits  (传 reference image 做角色一致性)
- 模型:  gpt-image-2 (2026-04-21 发布)

价格(RMB cents,1 USD ≈ 7.3 RMB):
    quality | 1024x1024 | 1024x1536 | 1536x1024
    low     |     4     |     4     |     4
    medium  |    39     |    30     |    30
    high    |    154    |    120    |    120
非 popular size 按像素比例从 1024x1536 base 推算。

环境变量:
  OPENAI_API_KEY     必填
  OPENAI_BASE_URL    可选,走代理时填(如 https://api.openai-proxy.com/v1)
  GPT_IMAGE_MODEL    默认 gpt-image-2
  GPT_IMAGE_QUALITY  默认 medium  (low/medium/high/auto)
  GPT_IMAGE_SIZE     默认 1088x1920(自定义 9:16,16 倍数,满足 OpenAI 像素约束)
"""
import asyncio
import base64
import os
from pathlib import Path

import httpx


class ContentPolicyError(RuntimeError):
    """gpt-image 内容审核拒绝。上层应回退到其他 provider,不应让整个 job 失败。"""


def _base() -> str:
    return os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")


def _model() -> str:
    return os.getenv("GPT_IMAGE_MODEL", "gpt-image-2")


def _default_size() -> str:
    return os.getenv("GPT_IMAGE_SIZE", "1088x1920")


def _default_quality() -> str:
    return os.getenv("GPT_IMAGE_QUALITY", "medium")


def _headers() -> dict:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set in .env")
    return {"Authorization": f"Bearer {key}"}


def _classify_error(status: int, body: str) -> RuntimeError:
    low = body.lower()
    if "content_policy" in low or "moderation" in low or "safety" in low:
        return ContentPolicyError(f"gpt-image moderation: {body[:300]}")
    return RuntimeError(f"gpt-image {status}: {body[:500]}")


async def generate(prompt: str,
                   size: str | None = None,
                   quality: str | None = None,
                   timeout: float = 240.0) -> bytes:
    """文生图。返回 PNG bytes。"""
    payload = {
        "model": _model(),
        "prompt": prompt[:4000],
        "size": size or _default_size(),
        "quality": quality or _default_quality(),
        "n": 1,
    }
    async with httpx.AsyncClient(timeout=timeout) as cli:
        r = await cli.post(
            f"{_base()}/images/generations",
            headers={**_headers(), "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code >= 400:
            raise _classify_error(r.status_code, r.text)
        b64 = r.json()["data"][0]["b64_json"]
        return base64.b64decode(b64)


async def edit(prompt: str,
               reference_images: list,
               size: str | None = None,
               quality: str | None = None,
               timeout: float = 240.0) -> bytes:
    """图编辑 with 参考图(角色 in-scene 一致性)。返回 PNG bytes。"""
    refs = list(reference_images)[:16]
    files = []
    if len(refs) == 1:
        p = Path(refs[0])
        files = [("image", (p.name, p.read_bytes(), "image/png"))]
    else:
        for p in refs:
            p = Path(p)
            files.append(("image[]", (p.name, p.read_bytes(), "image/png")))
    data = {
        "model": _model(),
        "prompt": prompt[:4000],
        "size": size or _default_size(),
        "quality": quality or _default_quality(),
        "n": "1",
    }
    async with httpx.AsyncClient(timeout=timeout) as cli:
        r = await cli.post(
            f"{_base()}/images/edits",
            headers=_headers(),
            data=data,
            files=files,
        )
        if r.status_code >= 400:
            raise _classify_error(r.status_code, r.text)
        b64 = r.json()["data"][0]["b64_json"]
        return base64.b64decode(b64)


# popular size 价格表(RMB cents)
_PRICE = {
    ("low", "1024x1024"): 4,
    ("low", "1024x1536"): 4,
    ("low", "1536x1024"): 4,
    ("medium", "1024x1024"): 39,
    ("medium", "1024x1536"): 30,
    ("medium", "1536x1024"): 30,
    ("high", "1024x1024"): 154,
    ("high", "1024x1536"): 120,
    ("high", "1536x1024"): 120,
}


def estimate_cost_cents(quality: str | None = None,
                        size: str | None = None) -> int:
    """每张图成本(分,人民币)。非 popular size 按像素比例推算。"""
    q = quality or _default_quality()
    s = size or _default_size()
    if (q, s) in _PRICE:
        return _PRICE[(q, s)]
    base = _PRICE.get((q, "1024x1536"), 30)
    try:
        w, h = map(int, s.split("x"))
        ratio = (w * h) / (1024 * 1536)
        return max(1, int(round(base * ratio)))
    except Exception:
        return base
