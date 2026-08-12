"""火山方舟 Seedance 2.5 视频与原生音频 client。

历经 1.0 Pro → 2.0,API 结构基本兼容(content[] 数组 + role),但 2.0 新加多种
reference 模式 + native audio + motion control + first/last frame 锁定等能力。

本 client 暴露 2.x role 模式:
  first_frame         单图 i2v(1.0 已有,兼容老调用)
  last_frame          2.0 新:尾帧锁定
  reference_image     2.0 新:多张参考图(最多 9 张,角色/物件保持)
  reference_video     2.0 新:视频参考(最多 3 段,控运镜风格)
  reference_audio     2.0 新:音频参考(最多 3 段,控节奏/情绪)

文档:
  https://www.volcengine.com/docs/82379       (火山方舟主入口)
  https://www.volcengine.com/docs/82379/1520757 (1.0 Pro 接口)
  https://www.volcengine.com/docs/82379/2298881 (2.0 SDK 示例)
  https://blog.laozhang.ai/en/posts/seedance-2-api (字段说明实测)

环境变量:
  ARK_API_KEY             火山方舟 API Key
  ARK_VIDEO_MODEL         默认 doubao-seedance-2-5-260628
  ARK_IMAGE_MODEL         t2i 走的图像模型,默认 doubao-seedream-5-0-260128
  ARK_VIDEO_RESOLUTION    覆盖默认分辨率(可在 .env 设)
"""
import asyncio
import base64
import os
from pathlib import Path

import httpx

ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = os.getenv("ARK_VIDEO_MODEL", "doubao-seedance-2-5-260628")


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


def _to_image_arg(item: str | os.PathLike[str]) -> str:
    """本地路径 / URL / data URI 都规整成 URL/data URI。"""
    item = os.fspath(item)
    if item.startswith(("http://", "https://", "data:", "asset://")):
        return item
    return _img_to_data_uri(item)


def _file_to_data_uri(path: str, mime_hint: str) -> str:
    """通用文件(视频/音频)转 data URI。"""
    if path.startswith(("http://", "https://", "data:")):
        return path
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:{mime_hint};base64,{b64}"


_RES_MAP = {  # 字面分辨率 → 方舟预设
    "480x854": "480p",   "854x480": "480p",
    "720x1280": "720p",  "1280x720": "720p",
    "1080x1920": "1080p", "1920x1080": "1080p",
}

# 2.0 支持的所有 role(client 暴露但服务端可能尚未全部 GA)
_VIDEO_ROLES = {"first_frame", "last_frame",
                "reference_image", "reference_video", "reference_audio"}


def _push_image(content: list, role: str, item: str) -> None:
    """统一往 content 数组里 push 一张 image/data URI。"""
    if role not in _VIDEO_ROLES:
        raise ValueError(f"unknown role {role!r}, valid: {_VIDEO_ROLES}")
    content.append({
        "type": "image_url",
        "image_url": {"url": _to_image_arg(item)},
        "role": role,
    })


def _push_video(content: list, role: str, path: str) -> None:
    content.append({
        "type": "video_url",
        "video_url": {"url": _file_to_data_uri(path, "video/mp4")},
        "role": role,
    })


def _push_audio(content: list, role: str, path: str) -> None:
    suffix = Path(path).suffix.lower() if not path.startswith(
        ("http://", "https://", "data:")
    ) else ""
    mime = {
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
    }.get(suffix, "audio/mpeg")
    content.append({
        "type": "audio_url",
        "audio_url": {"url": _file_to_data_uri(path, mime)},
        "role": role,
    })


def _video_task_payload(*, model: str, content: list, duration: float,
                        resolution: str, ratio: str,
                        camera_fixed: bool, last_frame: str | None,
                        seed: int | None, motion_strength: float | None,
                        generate_audio: bool,
                        output_format: str = "mp4",
                        task_intent: str = "generate") -> dict:
    """Build one Seedance task payload using official API field names."""
    if output_format not in {"mp4", "mov"}:
        raise ValueError("output_format must be 'mp4' or 'mov'")
    if task_intent not in {"generate", "edit"}:
        raise ValueError("task_intent must be 'generate' or 'edit'")
    roles = {item.get("role") for item in content if item.get("role")}
    if task_intent == "edit":
        if ratio != "adaptive":
            raise ValueError("Seedance edit tasks require ratio='adaptive'")
        if duration != -1:
            raise ValueError("Seedance edit tasks require duration=-1")
        if not ({"reference_video", "reference_audio"} & roles):
            raise ValueError(
                "Seedance edit tasks require reference_video or reference_audio"
            )
    if "seedance-2-5" in model.lower():
        if task_intent == "generate" and duration > 30:
            raise ValueError("Seedance 2.5 generate duration cannot exceed 30 seconds")
        if resolution not in {"480p", "720p"}:
            raise ValueError("Seedance 2.5 resolution must be 480p or 720p")

    payload = {
        "model": model,
        "content": content,
        "duration": int(round(duration)),
        "resolution": resolution,
        "output_format": output_format,
    }
    if not ({"first_frame", "last_frame"} & roles):
        payload["ratio"] = ratio
    # Seedance 2.x rejects camera_fixed in both T2V and I2V (including when
    # false); fixed-camera intent must be expressed in the prompt. Preserve
    # the legacy field only for older model families that still accept it.
    if "seedance-2-" not in model.lower() and not last_frame:
        payload["camera_fixed"] = camera_fixed
    if seed is not None:
        payload["seed"] = seed
    if motion_strength is not None:
        payload["motion_strength"] = motion_strength
    if generate_audio:
        payload["generate_audio"] = True
    return payload


def _validate_reference_limits(
    model: str,
    *,
    image_count: int,
    video_count: int,
    audio_count: int,
) -> None:
    """Validate model-specific multimodal reference limits before submission."""
    if "seedance-2-5" in model.lower():
        limits = {"reference_image": 30, "reference_video": 10, "reference_audio": 10}
    else:
        limits = {"reference_image": 9, "reference_video": 3, "reference_audio": 3}

    counts = {
        "reference_image": image_count,
        "reference_video": video_count,
        "reference_audio": audio_count,
    }
    for reference_type, count in counts.items():
        maximum = limits[reference_type]
        if count > maximum:
            raise ValueError(f"{reference_type} count {count} exceeds max {maximum}")


async def text_to_video(
    prompt: str,
    *,
    # 旧名(1.0 风格):reference_image 等于 first_frame,向后兼容
    reference_image: str | None = None,
    # 2.0 全 role:
    first_frame: str | None = None,
    last_frame: str | None = None,
    references: list[str] | None = None,           # reference_image[] 最多 9 张
    reference_videos: list[str] | None = None,     # 最多 3 段
    reference_audios: list[str] | None = None,     # 最多 3 段
    # 基本控制
    duration: float = 10.0,
    resolution: str = "720p",
    ratio: str = "9:16",
    seed: int | None = None,
    model: str | None = None,
    # 2.0 运动控制
    camera_fixed: bool = False,
    motion_strength: float | None = None,          # 0.0-1.0 实测可调
    # native audio(2.0 新):让视频自带音轨(对话/音效/环境音/BGM)
    generate_audio: bool = False,
    audio_output: bool | None = None,
    output_format: str = "mp4",
    task_intent: str = "generate",
    metadata_out: dict | None = None,
    timeout: int = 600,
) -> bytes:
    """文+多 reference → 视频。返回 mp4 字节。

    向后兼容:reference_image= 等于 first_frame= (1.0 风格)。

    深度利用 2.0:
      - references=[portrait, scene_keyframe]    多图保持角色一致
      - first_frame + last_frame                  锁定起止构图
      - reference_videos=[运镜参考片段]            学运镜节奏
      - reference_audios=[环境音/音效参考]         匹配节奏
      - motion_strength=0.3                       缓慢扫过镜头
      - camera_fixed=True                          固定特写
      - generate_audio=True                        视频自带音轨
    """
    # 分辨率归一化
    if resolution in _RES_MAP:
        resolution = _RES_MAP[resolution]
    elif resolution not in {"480p", "720p", "1080p"}:
        raise ValueError(
            f"unsupported resolution {resolution!r}; "
            f"use 480p/720p/1080p or a WxH key from {list(_RES_MAP)}"
        )

    resolved_model = model or DEFAULT_MODEL
    _validate_reference_limits(
        resolved_model,
        image_count=len(references or []),
        video_count=len(reference_videos or []),
        audio_count=len(reference_audios or []),
    )

    # 兼容旧名
    if reference_image and not first_frame:
        first_frame = reference_image

    # 构建 content
    content: list = [{"type": "text", "text": prompt}]
    if first_frame:
        _push_image(content, "first_frame", first_frame)
    if last_frame:
        _push_image(content, "last_frame", last_frame)
    for r in references or []:
        _push_image(content, "reference_image", r)
    for v in reference_videos or []:
        _push_video(content, "reference_video", v)
    for a in reference_audios or []:
        _push_audio(content, "reference_audio", a)

    # Backward-compatible alias for older local scripts. The provider field is
    # always the official `generate_audio`, never `audio_output`.
    if audio_output is not None:
        generate_audio = audio_output
    payload = _video_task_payload(
        model=resolved_model,
        content=content,
        duration=duration,
        resolution=resolution,
        ratio=ratio,
        camera_fixed=camera_fixed,
        last_frame=last_frame,
        seed=seed,
        motion_strength=motion_strength,
        generate_audio=generate_audio,
        output_format=output_format,
        task_intent=task_intent,
    )

    async with httpx.AsyncClient(timeout=timeout, headers=_headers()) as cli:
        # 1. 提交任务
        r = await cli.post(f"{ARK_BASE}/contents/generations/tasks", json=payload)
        if r.status_code >= 400:
            raise RuntimeError(
                f"seedance submit {r.status_code}: {r.text[:800]} | "
                f"endpoint={ARK_BASE}/contents/generations/tasks, "
                f"model={payload['model']}, "
                f"content_roles={[c.get('role') for c in content if c.get('role')]}"
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
                if metadata_out is not None:
                    metadata_out.update({
                        "task_id": task_id,
                        "usage": data.get("usage") or {},
                        "created_at": data.get("created_at"),
                        "updated_at": data.get("updated_at"),
                    })
                video_url = data["content"]["video_url"]
                vid = await cli.get(video_url, timeout=120)
                vid.raise_for_status()
                return vid.content
            if status == "failed":
                raise RuntimeError(f"seedance task {task_id} failed: {data}")
        raise TimeoutError(f"seedance task {task_id} timed out")


async def text_to_image(prompt: str, resolution: str = "1024x1536",
                        seed: int | None = None) -> bytes:
    """t2i 兜底入口(用 ARK_IMAGE_MODEL 指向的图像模型,默认 Seedream 5.0)。
    主路径是 seedream_45.t2i / i2i;这里只作 IMAGE_PROVIDER=seedream 时的最终 fallback。
    """
    model = os.getenv("ARK_IMAGE_MODEL", "doubao-seedream-5-0-260128")
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
                f"seedream(fallback) {r.status_code}: {r.text[:600]} | model={model}"
            )
        url = r.json()["data"][0]["url"]
        img = await cli.get(url)
        return img.content


def estimate_cost_cents(duration: float, resolution: str = "720p") -> int:
    """Seedance 2.5 价格按分辨率分档(以方舟账单为准):
      480p ≈ 12 分/秒 / 720p ≈ 40 分/秒 / 1080p ≈ 80 分/秒
    """
    rate = {"480p": 12, "720p": 40, "1080p": 80}.get(resolution, 40)
    return int(rate * duration)


def usage_cost_cents(
    usage: dict,
    *,
    price_rmb_per_1k_tokens: float = 0.07,
) -> int:
    """Calculate billed cents from provider-reported output token usage."""
    tokens = int(
        usage.get("completion_tokens")
        or usage.get("total_tokens")
        or 0
    )
    return round(tokens * price_rmb_per_1k_tokens * 100 / 1000)
