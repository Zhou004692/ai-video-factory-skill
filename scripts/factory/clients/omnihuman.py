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
import time

import httpx
from volcengine.visual.VisualService import VisualService


REQ_KEY = "jimeng_realman_avatar_picture_omni_v15"

# 火山限流类错误码:遇到这些 code 时退避重试,而不是直接 fail
_RATE_LIMIT_CODES = {50429, 50430, 50431, 50432}


class OmniHumanAccountOverdue(RuntimeError):
    """火山账户欠费 / 配额耗尽。retry 没用,需要 user 充值后再 retry-shot。
    单独子类让上层能区别于"真错误"做不同处理(如 jobs.error 写明 "充值后继续")。
    """
    pass


def _is_account_overdue(message: str, code: int | None) -> bool:
    """识别欠费类错误。火山不同接口码不一,用 message 关键词更稳健。"""
    if not message:
        return False
    msg = message.lower()
    return any(k in msg for k in (
        "overdue", "balance", "insufficient", "account", "arrears", "欠费", "余额"
    ))


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
            seed: int = -1,
            turbo_mode: bool | None = None,
            max_retries: int = 4) -> str:
    """提交任务,返回 task_id。

    50430 类限流错误自动退避重试(10s/20s/40s),不是真失败。
    J-VERIFY05 实测多镜短时间内连续提交会 100% 触发 50430,必须 retry。

    turbo_mode:
        None  → 走 OMNIHUMAN_TURBO env 决定(默认 false)
        True  → 加速生成,牺牲少量画质;测试期推荐开
        False → 标准质量,生产推荐
    """
    if turbo_mode is None:
        turbo_mode = os.getenv("OMNIHUMAN_TURBO", "false").lower() == "true"
    s = _service()
    body = {
        "req_key": REQ_KEY,
        "image_url": image_url,
        "audio_url": audio_url,
        "output_resolution": resolution,
        "pe_fast_mode": resolution == 720,   # 720→true,1080→false
        "turbo_mode": turbo_mode,
        "seed": seed,
    }
    if prompt:
        body["prompt"] = prompt[:300]
    if mask_url:
        body["mask_url"] = mask_url

    for attempt in range(max_retries):
        resp = s.cv_submit_task(body)
        if isinstance(resp, str):
            resp = json.loads(resp)
        code = resp.get("code")
        message = resp.get("message", "")
        if code == 10000:
            return resp["data"]["task_id"]
        # 欠费类:retry 没用,raise 明确子类让上层区分
        if _is_account_overdue(message, code):
            raise OmniHumanAccountOverdue(
                f"火山账户欠费/配额不足,请充值后 retry-shot。code={code} "
                f"msg={message[:200]}"
            )
        if code in _RATE_LIMIT_CODES and attempt < max_retries - 1:
            wait = 10 * (2 ** attempt)  # 10, 20, 40, 80
            print(
                f"[omnihuman] {code} rate-limited, retry "
                f"{attempt + 1}/{max_retries} after {wait}s "
                f"(msg={message[:80]})",
                flush=True,
            )
            time.sleep(wait)
            continue
        raise RuntimeError(f"omnihuman submit failed: {resp}")
    raise RuntimeError(
        f"omnihuman submit: exhausted {max_retries} retries on rate-limit"
    )


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
                   turbo_mode: bool | None = None,
                   timeout: int = 600) -> bytes:
    """主入口:提交 + 轮询 + 下载,返回 mp4 字节。"""
    loop = asyncio.get_running_loop()
    task_id = await loop.run_in_executor(
        None, _submit, image_url, audio_url, prompt, resolution,
        mask_url, seed, turbo_mode
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
