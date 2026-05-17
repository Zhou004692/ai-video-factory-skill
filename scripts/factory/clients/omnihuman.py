"""OmniHuman 1.5 client(火山引擎视觉智能服务,即梦同源数字人模型)。

输入:单张人像图(URL) + 音频(URL,≤60s,建议 ≤15s)
输出:对应人物讲话的视频(嘴型同步,带表情和动作)

接口:https://visual.volcengineapi.com
鉴权:Volcengine AK/SK 签名(Region=cn-north-1, Service=cv)
模式:异步任务(提交 → 轮询 → 拿视频 URL)

环境变量:
  VOLC_AK              Access Key ID
  VOLC_SK              Secret Access Key
"""
import asyncio
import json
import os

import httpx
from volcengine.visual.VisualService import VisualService


REQ_KEY = "jimeng_realman_avatar_picture_omni_v15"


def _service() -> VisualService:
    ak = os.environ.get("VOLC_AK")
    sk = os.environ.get("VOLC_SK")
    if not (ak and sk):
        raise RuntimeError("VOLC_AK / VOLC_SK not set in .env")
    s = VisualService()
    s.set_ak(ak)
    s.set_sk(sk)
    return s


def _submit(image_url: str, audio_url: str,
            prompt: str | None = None,
            resolution: int = 1080,
            mask_url: list[str] | None = None,
            seed: int = -1) -> str:
    """提交任务,返回 task_id。"""
    s = _service()
    body = {
        "req_key": REQ_KEY,
        "image_url": image_url,
        "audio_url": audio_url,
        "output_resolution": resolution,
        "pe_fast_mode": resolution == 720,   # 720→true,1080→false
        "seed": seed,
    }
    if prompt:
        body["prompt"] = prompt[:300]
    if mask_url:
        body["mask_url"] = mask_url

    resp = s.cv_submit_task(body)
    if isinstance(resp, str):
        resp = json.loads(resp)
    if resp.get("code") != 10000:
        raise RuntimeError(f"omnihuman submit failed: {resp}")
    return resp["data"]["task_id"]


def _query(task_id: str) -> dict:
    s = _service()
    body = {"req_key": REQ_KEY, "task_id": task_id}
    resp = s.cv_get_result(body)
    if isinstance(resp, str):
        resp = json.loads(resp)
    return resp


async def generate(image_url: str, audio_url: str,
                   prompt: str | None = None,
                   resolution: int = 1080,
                   mask_url: list[str] | None = None,
                   seed: int = -1,
                   timeout: int = 600) -> bytes:
    """主入口:提交 + 轮询 + 下载,返回 mp4 字节。"""
    loop = asyncio.get_running_loop()
    task_id = await loop.run_in_executor(
        None, _submit, image_url, audio_url, prompt, resolution, mask_url, seed
    )

    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await asyncio.sleep(5)
        resp = await loop.run_in_executor(None, _query, task_id)
        if resp.get("code") != 10000:
            raise RuntimeError(f"omnihuman query {task_id}: {resp}")
        data = resp["data"] or {}
        status = data.get("status")
        if status == "done":
            url = data.get("video_url")
            if not url:
                raise RuntimeError(f"task {task_id} done but no video_url: {data}")
            async with httpx.AsyncClient(timeout=120) as cli:
                r = await cli.get(url)
                r.raise_for_status()
                return r.content
        if status in {"not_found", "expired"}:
            raise RuntimeError(f"task {task_id} {status}")
        # 其他状态(processing / in_queue / generating)继续轮询

    raise TimeoutError(f"omnihuman task {task_id} timed out after {timeout}s")


def estimate_cost_cents(audio_duration_sec: float, resolution: int = 1080) -> int:
    """OmniHuman 大致按音频时长计费,1080p 约 ¥1/秒,720p 约 ¥0.5/秒。
    (实际以火山引擎账单为准)"""
    rate = 100 if resolution == 1080 else 50
    return max(50, int(rate * audio_duration_sec))
