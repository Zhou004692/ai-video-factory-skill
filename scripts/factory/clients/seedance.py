"""火山方舟 Seedance 2.0 (即梦视频底模) client。

历经 1.0 Pro → 2.0,API 结构基本兼容(content[] 数组 + role),但 2.0 新加多种
reference 模式 + native audio + motion control + first/last frame 锁定等能力。

本 client 暴露 2.0 全部 role 模式:
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
  ARK_VIDEO_MODEL         默认 doubao-seedance-2-0-260128
  ARK_IMAGE_MODEL         t2i 走的图像模型,默认 doubao-seedream-5-0-260128
  ARK_VIDEO_RESOLUTION    覆盖默认分辨率(可在 .env 设)
"""
import asyncio
import base64
import os
from pathlib import Path

import httpx

ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = os.getenv("ARK_VIDEO_MODEL", "doubao-seedance-2-0-260128")


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


def _to_image_arg(item: str) -> str:
    """本地路径 / URL / data URI 都规整成 URL/data URI。"""
    if item.startswith(("http://", "https://", "data:")):
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
    content.append({
        "type": "audio_url",
        "audio_url": {"url": _file_to_data_uri(path, "audio/mp3")},
        "role": role,
    })


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
    resolution: str = "1080p",
    ratio: str = "9:16",
    seed: int | None = None,
    # 2.0 运动控制
    camera_fixed: bool = False,
    motion_strength: float | None = None,          # 0.0-1.0 实测可调
    # native audio(2.0 新):让视频自带音轨(对话/音效/环境音/BGM)
    audio_output: bool = False,
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
      - audio_output=True                          视频自带音轨
    """
    # 分辨率归一化
    if resolution in _RES_MAP:
        resolution = _RES_MAP[resolution]
    elif resolution not in {"480p", "720p", "1080p"}:
        raise ValueError(
            f"unsupported resolution {resolution!r}; "
            f"use 480p/720p/1080p or a WxH key from {list(_RES_MAP)}"
        )

    # references 数量校验(2.0 文档限制)
    if references and len(references) > 9:
        raise ValueError(f"reference_image[] max 9, got {len(references)}")
    if reference_videos and len(reference_videos) > 3:
        raise ValueError(f"reference_video[] max 3, got {len(reference_videos)}")
    if reference_audios and len(reference_audios) > 3:
        raise ValueError(f"reference_audio[] max 3, got {len(reference_audios)}")

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

    payload = {
        "model": DEFAULT_MODEL,
        "content": content,
        "duration": int(round(duration)),
        "resolution": resolution,
        "ratio": ratio,
    }
    # Seedance 2.0 硬约束 #2(J-VERIFY08 实测):flf2v 模式(有 last_frame)
    # 不支持 camera_fixed 参数 ("must be empty"),只有 i2v/t2v 模式才传。
    if not last_frame:
        payload["camera_fixed"] = camera_fixed
    if seed is not None:
        payload["seed"] = seed
    if motion_strength is not None:
        payload["motion_strength"] = motion_strength
    if audio_output:
        # 2.0 native audio:视频带音轨返回
        payload["audio_output"] = True

    async with httpx.AsyncClient(timeout=timeout, headers=_headers()) as cli:
        # 1. 提交任务
        r = await cli.post(f"{ARK_BASE}/contents/generations/tasks", json=payload)
        if r.status_code >= 400:
            raise RuntimeError(
                f"seedance submit {r.status_code}: {r.text[:800]} | "
                f"endpoint={ARK_BASE}/contents/generations/tasks, "
                f"model={DEFAULT_MODEL}, "
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
    """Seedance 2.0 价格按分辨率分档(以方舟账单为准):
      480p ≈ 12 分/秒 / 720p ≈ 40 分/秒 / 1080p ≈ 80 分/秒
    """
    rate = {"480p": 12, "720p": 40, "1080p": 80}.get(resolution, 40)
    return int(rate * duration)
