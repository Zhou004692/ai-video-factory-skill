"""BGM 生成 client。优先海绵音乐(字节同门)、备选 Suno。

海绵音乐目前没有官方对外 API,这里给一个本地降级方案:
- 如果配了 SUNO_API_KEY → 走 Suno
- 否则 → 从 references/bgm_library/ 里挑预设音乐(用户自备)

后续海绵音乐对外开 API 后,把 _via_haimian 实装即可。
"""
import asyncio
import os
import random
from pathlib import Path

import httpx

from .. import REFERENCES_DIR
from . import ffmpeg, provider_downloads


class MusicProviderConfigError(ValueError):
    """A configured music provider is unknown or cannot execute."""


class MusicTaskUnknownError(RuntimeError):
    non_retryable = True
    pause_state = "PROVIDER_ACTION_REQUIRED"

    def __init__(self, message: str, *, task_id: str | None = None):
        super().__init__(message)
        self.task_id = task_id


def provider_registry() -> dict[str, dict]:
    """Single capability registry used by resolution, estimates and execution."""
    return {
        "silent": {"supported": True, "paid": False,
                   "estimate": lambda _duration: 0, "executor": _via_silent},
        "library": {"supported": True, "paid": False,
                    "estimate": lambda _duration: 0, "executor": _via_library},
        "suno": {"supported": True, "paid": True,
                 "estimate": lambda _duration: 20, "executor": _via_suno},
        "haimian": {"supported": False, "paid": False,
                    "estimate": None, "executor": None},
    }


def resolve_provider(
    configured: dict[str, str] | None = None, *, explicit: str | None = None
) -> str:
    """Resolve once with explicit > process environment > file config."""
    value = (
        explicit
        if explicit is not None
        else os.environ.get("DEFAULT_MUSIC_PROVIDER")
        or (configured or {}).get("DEFAULT_MUSIC_PROVIDER")
        or "silent"
    )
    normalized = str(value or "").strip().casefold() or "silent"
    registry = provider_registry()
    if normalized not in registry:
        raise MusicProviderConfigError(
            f"unknown music provider: {normalized}"
        )
    if not registry[normalized]["supported"]:
        raise MusicProviderConfigError(
            f"music provider {normalized} / 海绵音乐尚未实现，不能执行"
        )
    return normalized


def may_submit_paid_work(provider: str) -> bool:
    resolved = resolve_provider(explicit=provider)
    return bool(provider_registry()[resolved]["paid"])


async def generate(style: str, duration_sec: int, bpm_range: tuple[int, int] = (80, 100),
                   mood: str = "积极", *, provider: str | None = None,
                   metadata_out: dict | None = None,
                   on_task_created=None, on_task_updated=None) -> bytes:
    """生成或挑选一段 BGM。返回 mp3 字节。"""
    provider = resolve_provider(explicit=provider)

    executor = provider_registry()[provider]["executor"]
    if executor is None:
        raise MusicProviderConfigError(
            f"music provider {provider} is not implemented"
        )
    if provider == "suno":
        return await executor(
            style, duration_sec, bpm_range, mood,
            metadata_out=metadata_out,
            on_task_created=on_task_created,
            on_task_updated=on_task_updated,
        )
    elif provider == "library":
        try:
            return executor(style, duration_sec)
        except (OSError, RuntimeError) as exc:
            raise MusicProviderConfigError(
                "已显式选择 library 音乐库，但未找到可用 mp3；"
                "请在 references/bgm_library/ 下放入素材，或将 "
                "DEFAULT_MUSIC_PROVIDER 改为 silent/suno"
            ) from exc
    else:  # silent — 跑通管线用的兜底,无声 BGM
        return await executor(duration_sec)


async def _via_silent(duration_sec: int) -> bytes:
    """用 ffmpeg 生成一段静音 mp3。测试和 BGM 不可用时的兜底。"""
    import tempfile
    out = Path(tempfile.mktemp(suffix=".mp3"))
    proc = await asyncio.create_subprocess_exec(
        ffmpeg.executable("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
        "-t", str(duration_sec), "-q:a", "9", "-acodec", "libmp3lame",
        str(out),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"silent bgm gen failed: {err.decode()[-500:]}")
    data = out.read_bytes()
    out.unlink(missing_ok=True)
    return data


async def _via_suno_legacy(style: str, duration: int, bpm_range, mood: str, *,
                           metadata_out: dict | None = None,
                           on_task_created=None, on_task_updated=None) -> bytes:
    """Suno API(经第三方 sunoapi.com 转接,官方未公开)。"""
    key = os.environ.get("SUNO_API_KEY")
    if not key:
        raise RuntimeError("SUNO_API_KEY not set")
    payload = {
        "prompt": f"{style}, {mood}, BPM {bpm_range[0]}-{bpm_range[1]}, instrumental, {duration}s",
        "make_instrumental": True,
        "wait_audio": True,
    }
    async with httpx.AsyncClient(timeout=300, headers={"Authorization": f"Bearer {key}"}) as cli:
        try:
            r = await cli.post("https://api.sunoapi.com/api/v1/suno/create", json=payload)
        except httpx.TransportError as exc:
            raise MusicTaskUnknownError(
                f"Suno 提交结果未知：{exc}"
            ) from exc
        r.raise_for_status()
        try:
            body = r.json()
            first = (body.get("data") or [{}])[0] if isinstance(body, dict) else {}
            task_id = str(
                (body.get("task_id") or body.get("id") if isinstance(body, dict) else "")
                or first.get("task_id") or first.get("id") or ""
            ).strip()
        except (ValueError, TypeError, AttributeError, IndexError) as exc:
            raise MusicTaskUnknownError("Suno 提交响应无法核对") from exc
        if not task_id:
            raise MusicTaskUnknownError("Suno 提交响应缺少 task_id")
        if metadata_out is not None:
            metadata_out["task_id"] = task_id
        if on_task_created:
            on_task_created(task_id)
        url = str(first.get("audio_url") or "").strip()
        if not url:
            if on_task_updated:
                on_task_updated(task_id, "action_required", {})
            raise MusicTaskUnknownError(
                "Suno task 已创建但音频结果尚不可核对", task_id=task_id
            )
        usage = body.get("usage") or first.get("usage") or {}
        if metadata_out is not None:
            metadata_out["usage"] = usage
        if on_task_updated:
            on_task_updated(task_id, "completed", {"usage": usage})
        try:
            audio, _ = await provider_downloads.download_bytes(url, timeout=60)
        except httpx.HTTPError as exc:
            raise MusicTaskUnknownError(
                "Suno task 已完成但音频下载结果未知", task_id=task_id
            ) from exc
        return audio


def _first_result(body: object) -> dict:
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else {}
    return data if isinstance(data, dict) else {}


def _task_status(body: object) -> str:
    if not isinstance(body, dict):
        return ""
    raw = str(body.get("status") or _first_result(body).get("status") or "")
    normalized = raw.strip().casefold()
    if normalized in {"completed", "succeeded", "success"}:
        return "completed"
    if normalized in {"failed", "error", "cancelled", "canceled"}:
        return "failed"
    if normalized in {
        "pending", "queued", "running", "processing", "submitted",
    }:
        return "pending"
    return ""


async def _via_suno(style: str, duration: int, bpm_range, mood: str, *,
                    metadata_out: dict | None = None,
                    on_task_created=None, on_task_updated=None) -> bytes:
    """Submit once, persist the task id, then poll/download that same task."""
    key = os.environ.get("SUNO_API_KEY")
    if not key:
        raise RuntimeError("SUNO_API_KEY not set")
    payload = {
        "prompt": (
            f"{style}, {mood}, BPM {bpm_range[0]}-{bpm_range[1]}, "
            f"instrumental, {duration}s"
        ),
        "make_instrumental": True,
        "wait_audio": False,
    }
    async with httpx.AsyncClient(
        timeout=300, headers={"Authorization": f"Bearer {key}"}
    ) as cli:
        if metadata_out is not None:
            metadata_out["post_started"] = True
        try:
            response = await cli.post(
                "https://api.sunoapi.com/api/v1/suno/create", json=payload
            )
        except httpx.TransportError as exc:
            raise MusicTaskUnknownError(
                f"Suno 提交结果未知：{exc}"
            ) from exc
        if response.status_code >= 500:
            raise MusicTaskUnknownError(
                f"Suno 提交返回 HTTP {response.status_code}，任务是否创建未知"
            )
        if response.status_code >= 400:
            if metadata_out is not None:
                metadata_out["definitive_rejection"] = True
            response.raise_for_status()
        try:
            body = response.json()
            first = _first_result(body)
            task_id = str(
                (body.get("task_id") or body.get("id")
                 if isinstance(body, dict) else "")
                or first.get("task_id") or first.get("id") or ""
            ).strip()
        except (ValueError, TypeError, AttributeError, IndexError) as exc:
            raise MusicTaskUnknownError("Suno 提交响应无法核对") from exc
        if not task_id:
            raise MusicTaskUnknownError("Suno 提交响应缺少 task_id")
        if metadata_out is not None:
            metadata_out["task_id"] = task_id
        if on_task_created:
            on_task_created(task_id)

        status = _task_status(body)
        url = str(
            first.get("audio_url") or body.get("audio_url") or ""
        ).strip()
        usage = body.get("usage") or first.get("usage") or {}
        attempts = max(1, int(os.environ.get("SUNO_POLL_ATTEMPTS", "120")))
        interval = max(
            0.0, float(os.environ.get("SUNO_POLL_INTERVAL_SEC", "2"))
        )
        for attempt in range(attempts):
            if url and status in {"", "completed"}:
                status = "completed"
                break
            if status == "failed":
                if on_task_updated:
                    on_task_updated(task_id, "action_required", {
                        "usage": usage,
                        "remote_status": "failed",
                        "cost_cents": estimate_cost_cents(
                            duration, provider="suno"
                        ),
                    })
                raise MusicTaskUnknownError(
                    "Suno task 已失败，请先核对任务与计费后再决定是否重试",
                    task_id=task_id,
                )
            if attempt:
                await asyncio.sleep(interval)
            try:
                polled = await cli.get(
                    f"https://api.sunoapi.com/api/v1/suno/task/{task_id}"
                )
            except httpx.TransportError as exc:
                raise MusicTaskUnknownError(
                    "Suno task 轮询结果未知", task_id=task_id
                ) from exc
            if polled.status_code >= 500:
                raise MusicTaskUnknownError(
                    f"Suno task 轮询返回 HTTP {polled.status_code}",
                    task_id=task_id,
                )
            polled.raise_for_status()
            try:
                body = polled.json()
                first = _first_result(body)
            except (ValueError, TypeError, AttributeError, IndexError) as exc:
                raise MusicTaskUnknownError(
                    "Suno task 轮询响应无法核对", task_id=task_id
                ) from exc
            status = _task_status(body)
            url = str(
                first.get("audio_url") or body.get("audio_url") or ""
            ).strip()
            usage = body.get("usage") or first.get("usage") or usage or {}
            if not status and not url:
                raise MusicTaskUnknownError(
                    "Suno task 轮询响应缺少可识别状态",
                    task_id=task_id,
                )
            if on_task_updated and status == "pending":
                on_task_updated(task_id, "pending", {"usage": usage})
        else:
            raise MusicTaskUnknownError(
                "Suno task 轮询超时，请使用 task_id 核对，禁止重新提交",
                task_id=task_id,
            )
        if status != "completed" or not url:
            raise MusicTaskUnknownError(
                "Suno task 已创建但音频结果尚不可核对", task_id=task_id
            )
        if metadata_out is not None:
            metadata_out["usage"] = usage
        try:
            audio, _ = await provider_downloads.download_bytes(url, timeout=60)
        except httpx.HTTPError as exc:
            raise MusicTaskUnknownError(
                "Suno task 已完成但音频下载结果未知", task_id=task_id
            ) from exc
        if on_task_updated:
            on_task_updated(task_id, "completed", {
                "usage": usage,
                "cost_cents": estimate_cost_cents(duration, provider="suno"),
            })
        return audio


async def _via_haimian(style, duration, bpm_range, mood) -> bytes:
    raise NotImplementedError("海绵音乐 API 尚未对外开放,请改用 SUNO 或 library 模式")


async def inspect_task(task_id: str, *, provider: str) -> dict:
    """Inspect a known paid music task without creating another task."""
    resolved = resolve_provider(explicit=provider)
    if resolved != "suno":
        raise MusicProviderConfigError(
            f"music provider {resolved} does not support task reconciliation"
        )
    key = os.environ.get("SUNO_API_KEY")
    if not key:
        raise RuntimeError("SUNO_API_KEY not set")
    normalized_id = str(task_id or "").strip()
    async with httpx.AsyncClient(
        timeout=120, headers={"Authorization": f"Bearer {key}"}
    ) as cli:
        try:
            response = await cli.get(
                f"https://api.sunoapi.com/api/v1/suno/task/{normalized_id}"
            )
        except httpx.TransportError:
            return {"status": "unknown", "task_id": normalized_id}
        if response.status_code >= 500:
            return {"status": "unknown", "task_id": normalized_id}
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError:
            return {"status": "unknown", "task_id": normalized_id}
        status = _task_status(body) or "unknown"
        result = {
            "status": status, "task_id": normalized_id,
            "usage": body.get("usage") or {},
            "cost_cents": (
                estimate_cost_cents(0, provider=resolved)
                if status in {"completed", "failed"} else 0
            ),
        }
        if status != "completed":
            return result
        first = _first_result(body)
        url = str(first.get("audio_url") or body.get("audio_url") or "").strip()
        if not url:
            return {**result, "status": "unknown"}
        try:
            audio, final_url = await provider_downloads.download_bytes(
                url, timeout=60
            )
        except httpx.HTTPError:
            return {**result, "status": "unknown"}
        return {**result, "audio": audio, "audio_url": final_url}


def _via_library(style: str, duration_sec: int) -> bytes:
    """从本地音乐库随机挑一首。要先把 mp3 放到 references/bgm_library/<style>/"""
    lib = REFERENCES_DIR / "bgm_library" / style
    if not lib.exists():
        # 兜底:任意一首
        lib = REFERENCES_DIR / "bgm_library"
    candidates = list(lib.rglob("*.mp3"))
    if not candidates:
        raise RuntimeError(
            f"no bgm in {lib}. 请放一些 mp3 到这个目录,或改用 SUNO provider"
        )
    return random.choice(candidates).read_bytes()


def estimate_cost_cents(duration_sec: int, provider: str | None = None) -> int:
    p = resolve_provider(explicit=provider)
    estimate = provider_registry()[p]["estimate"]
    if estimate is None:
        raise MusicProviderConfigError(f"music provider {p} cannot estimate")
    return max(0, int(estimate(duration_sec)))
