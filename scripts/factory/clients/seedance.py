"""火山方舟 Seedance(即梦视频底模)client。

接口形态参考火山方舟视频生成 API。具体 endpoint / 字段以官方文档为准:
  https://www.volcengine.com/docs/82379

环境变量:
  ARK_API_KEY     火山方舟 API Key
  ARK_REGION      地域,默认 cn-beijing
  ARK_VIDEO_MODEL 模型 ID,默认 doubao-seedance-1-0-pro
"""
import asyncio
import base64
import os
from pathlib import Path

import httpx

ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = os.getenv("ARK_VIDEO_MODEL", "doubao-seedance-1-0-pro-250528")


def _headers():
    key = os.environ.get("ARK_API_KEY")
    if not key:
        raise RuntimeError("ARK_API_KEY not set in .env")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _img_to_data_uri(path: str) -> str:
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    ext = Path(path).suffix.lstrip(".") or "png"
    return f"data:image/{ext};base64,{b64}"


async def text_to_video(prompt: str,
                        reference_image: str | None = None,
                        duration: float = 10.0,
                        resolution: str = "1080x1920",
                        ratio: str = "9:16",
                        seed: int | None = None,
                        timeout: int = 600) -> bytes:
    """文+图生视频。返回 mp4 字节。

    内部走"提交 → 轮询 → 下载"三步。
    """
    content = [{"type": "text", "text": prompt}]
    if reference_image:
        content.append({
            "type": "image_url",
            "image_url": {"url": _img_to_data_uri(reference_image)},
            "role": "reference_image",
        })

    payload = {
        "model": DEFAULT_MODEL,
        "content": content,
        "duration": int(round(duration)),
        "resolution": resolution,
        "ratio": ratio,
    }
    if seed is not None:
        payload["seed"] = seed

    async with httpx.AsyncClient(timeout=timeout, headers=_headers()) as cli:
        # 1. 提交任务
        r = await cli.post(f"{ARK_BASE}/contents/generations/tasks", json=payload)
        if r.status_code >= 400:
            raise RuntimeError(
                f"seedance submit {r.status_code}: {r.text[:800]} | "
                f"endpoint={ARK_BASE}/contents/generations/tasks, "
                f"model={DEFAULT_MODEL}"
            )
        task_id = r.json()["id"]

        # 2. 轮询
        for _ in range(timeout // 5):
            await asyncio.sleep(5)
            s = await cli.get(f"{ARK_BASE}/contents/generations/tasks/{task_id}")
            s.raise_for_status()
            data = s.json()
            status = data.get("status")
            if status == "succeeded":
                video_url = data["content"]["video_url"]
                vid = await cli.get(video_url, timeout=120)
                vid.raise_for_status()
                return vid.content
            if status == "failed":
                raise RuntimeError(f"seedance task {task_id} failed: {data}")
        raise TimeoutError(f"seedance task {task_id} timed out")


async def text_to_image(prompt: str, resolution: str = "1024x1536",
                        seed: int | None = None) -> bytes:
    """主角定妆照。如果不想用即梦图片,可直接换 Midjourney/SD。"""
    model = os.getenv("ARK_IMAGE_MODEL", "doubao-seedream-3-0-t2i")
    payload = {
        "model": model,
        "prompt": prompt,
        "size": resolution,
    }
    if seed is not None:
        payload["seed"] = seed
    async with httpx.AsyncClient(timeout=120, headers=_headers()) as cli:
        r = await cli.post(f"{ARK_BASE}/images/generations", json=payload)
        if r.status_code >= 400:
            raise RuntimeError(
                f"seedream {r.status_code}: {r.text[:600]} | model={model}"
            )
        url = r.json()["data"][0]["url"]
        img = await cli.get(url)
        return img.content


def estimate_cost_cents(duration: float, resolution: str = "1080x1920") -> int:
    """粗算:Seedance Pro 大约 8 积分/秒,1 积分 ≈ 0.1 元 → 0.8 元/秒 → 80 分/秒。"""
    return int(80 * duration)
