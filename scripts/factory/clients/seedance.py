"""火山方舟 Seedance 2.5 视频与原生音频 client。

历经 1.0 Pro → 2.0,API 结构基本兼容(content[] 数组 + role),但 2.0 新加多种
reference 模式 + native audio + motion control + first/last frame 锁定等能力。

本 client 暴露 2.x role 模式:
  first_frame         单图 i2v(1.0 已有,兼容老调用)
  last_frame          2.0 新:尾帧锁定
  reference_image     参考图(2.5 最多 30 张,角色/物件保持)
  reference_video     视频参考(2.5 最多 10 段,控运镜/连续动作)
  reference_audio     音频参考(2.5 最多 10 段,控音色/节奏/情绪)

文档:
  https://docs.volcengine.com/docs/82379/2607688?lang=zh (2.5 教程)
  https://docs.volcengine.com/docs/82379/2607689?lang=zh (2.5 提示词)

环境变量:
  ARK_API_KEY             火山方舟 API Key
  ARK_VIDEO_MODEL         默认 doubao-seedance-2-5-260628
  ARK_IMAGE_MODEL         t2i 走的图像模型,默认 doubao-seedream-5-0-260128
  ARK_VIDEO_RESOLUTION    覆盖默认分辨率(可在 .env 设)
"""
import asyncio
import base64
import json
import math
import os
from pathlib import Path
import re

import httpx

from .. import provider_runtime, reference_attention, transport_diagnostics
from . import provider_downloads

ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
SEEDANCE_25_MIN_GENERATION_DURATION_SEC = 4
SEEDANCE_25_MAX_GENERATION_DURATION_SEC = 30
SEEDANCE_25_CAPABILITIES = {
    "resolutions": frozenset({"480p", "720p", "1080p"}),
    "reference_limits": {
        "reference_image": 30,
        "reference_video": 10,
        "reference_audio": 10,
    },
    "task_types": frozenset({"auto", "reference", "edit", "extend"}),
    "reference_duration_limits": {"video": 30, "audio": 30},
}


def capability_profile(model: str) -> dict:
    """Return the single provider capability contract used by every layer."""
    if "seedance-2-5" in str(model or "").lower():
        return SEEDANCE_25_CAPABILITIES
    return {
        "resolutions": frozenset({"480p", "720p", "1080p"}),
        "reference_limits": {
            "reference_image": 9,
            "reference_video": 3,
            "reference_audio": 3,
        },
        "task_types": frozenset({"auto"}),
        # Preserve the existing legacy contract; no unverified duration caps.
        "reference_duration_limits": {},
    }


class SeedanceParameterError(ValueError):
    """A provider contract violation that must never be retried as-is."""

    non_retryable = True
    retryable = False
    code = "request_invalid"
    error_code = code
    stable_code = code


def normalize_generation_duration(duration: float) -> int:
    """Map editorial seconds to one valid Seedance 2.5 paid request."""
    try:
        value = float(duration)
    except (TypeError, ValueError) as exc:
        raise SeedanceParameterError(
            "Seedance 2.5 generation duration must be numeric"
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise SeedanceParameterError(
            "Seedance 2.5 generation duration must be greater than 0"
        )
    if value > SEEDANCE_25_MAX_GENERATION_DURATION_SEC:
        raise SeedanceParameterError(
            "Seedance 2.5 generation duration maximum is 30 seconds; "
            "split the provider-facing master before submission"
        )
    return max(SEEDANCE_25_MIN_GENERATION_DURATION_SEC, math.ceil(value))
DEFAULT_MODEL = os.getenv("ARK_VIDEO_MODEL", "doubao-seedance-2-5-260628")


class SeedanceInputPrivacyError(RuntimeError):
    """An ordinary image reference was classified as real-person material."""

    non_retryable = True
    retryable = False
    code = "input_privacy_rejected"
    error_code = code
    stable_code = code


class SeedanceSubmissionUnknownError(RuntimeError):
    """A transient provider transport or service failure."""

    submit_result_unknown = True
    non_retryable = False
    retryable = True
    code = "transient_network"
    error_code = code
    stable_code = code


class SeedanceOutputPolicyError(RuntimeError):
    """The provider rejected generated output for a policy reason."""

    non_retryable = True
    retryable = False
    code = "output_policy_rejected"
    error_code = code
    stable_code = code


class SeedanceAccountUnavailableError(RuntimeError):
    """The Ark account cannot accept paid work until the user intervenes."""

    non_retryable = True
    retryable = False
    pause_state = "PROVIDER_ACTION_REQUIRED"
    code = "account_unavailable"
    error_code = code
    stable_code = code


class SeedanceRateLimitError(RuntimeError):
    """A definite pre-task 429 that is safe to use for capacity learning."""

    non_retryable = True
    retryable = False
    code = "rate_limited"
    error_code = code
    stable_code = code


def _classified(error: Exception, code: str, *, retryable: bool) -> Exception:
    error.code = code
    error.error_code = code
    error.stable_code = code
    error.retryable = retryable
    error.non_retryable = not retryable
    return error


def _safe_provider_code(value: str) -> str:
    code = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", code):
        return code
    return "unknown"


def _provider_error(
    *,
    provider_code: str = "",
    provider_message: str = "",
    status_code: int | None = None,
    task_id: str | None = None,
) -> Exception:
    """Map submit and terminal poll failures into one stable taxonomy."""
    detail = f"{provider_code} {provider_message}".casefold()
    task = f"任务 {task_id} " if task_id else ""
    if status_code == 429 or "ratelimit" in detail or "too many requests" in detail:
        return SeedanceRateLimitError(
            "Seedance 并发额度已满，系统已自动降低有效并发；"
            "请等待冷却后继续，已完成母片不会重做。"
        )
    if (
        "inputimagesensitivecontentdetected" in detail
        or "inputimagessensitivecontentdetected" in detail
    ):
        return SeedanceInputPrivacyError(
            f"Seedance {task}拒绝了可能包含隐私信息的输入图片；"
            "请使用 authorized asset:// 已授权素材或移除该引用。"
        )
    if "copyright" in detail:
        return _classified(
            SeedanceOutputPolicyError(
                f"Seedance {task}输出因版权或敏感内容策略被拒绝；"
                "请调整受保护形象或作品引用。"
            ),
            "output_copyright_rejected",
            retryable=False,
        )
    if (
        "outputvideosensitivecontentdetected" in detail
        or "policyviolation" in detail
    ):
        return SeedanceOutputPolicyError(
            f"Seedance {task}输出被内容安全策略拒绝；请调整该母片内容。"
        )
    if "invalidparameter" in detail:
        return SeedanceParameterError(
            f"Seedance {task}请求参数未通过服务端校验；请修正参数后再提交。"
        )
    account_markers = (
        "accountoverdue",
        "overdue balance",
        "accountdisabled",
        "invalidaccesskey",
        "authenticationfailed",
        "unauthorized",
        "access key is invalid",
    )
    if (
        status_code in {401, 403}
        or str(provider_code).casefold().startswith("account")
        or any(
            marker in detail for marker in account_markers
        )
    ):
        return SeedanceAccountUnavailableError(
            "火山方舟账号认证失败、被停用或欠费，系统已停止新的 Seedance 提交；"
            "如为欠费请充值，并在处理账号问题后从管理台人工解除本地阻断。"
        )
    transient_markers = (
        "remoteprotocolerror",
        "timeout",
        "timed out",
        "internalservererror",
        "serviceunavailable",
        "temporarily unavailable",
    )
    if (status_code is not None and status_code >= 500) or any(
        marker in detail for marker in transient_markers
    ):
        return SeedanceSubmissionUnknownError(
            "Seedance 服务暂时不可用或网络连接中断；本次错误可按重试策略处理。"
        )
    if status_code is not None and 400 <= status_code < 500:
        return SeedanceParameterError(
            "Seedance 请求被服务端拒绝；请检查配置与请求参数后再提交。"
        )
    safe_code = _safe_provider_code(provider_code)
    safe_detail = "未知 Provider 终止错误，原始详情已省略"
    error = SeedanceParameterError(
        f"Seedance {task}返回未知终止错误；系统不会自动发起新的付费提交；"
        f"provider_code={safe_code or 'unknown'}; "
        f"provider_detail={safe_detail or 'not provided'}"
    )
    error.provider_code = safe_code
    error.provider_detail = safe_detail
    return error


def _submission_transport_error(exc: BaseException) -> RuntimeError:
    endpoint = f"{ARK_BASE}/contents/generations/tasks"
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        error = SeedanceSubmissionUnknownError(
            "Seedance 提交前连接失败；本次错误归类为 transient_network，"
            "可按重试策略处理。"
        )
        error.submit_result_unknown = False
        error.transport_diagnostic = transport_diagnostics.exception_diagnostic(
            exc,
            stage="submit_connect",
            endpoint=endpoint,
            submit_result_unknown=False,
        )
        return error
    error = _ambiguous_provider_error(stage="submit")
    error.transport_diagnostic = transport_diagnostics.exception_diagnostic(
        exc,
        stage="submit_response",
        endpoint=endpoint,
        submit_result_unknown=True,
    )
    return error


def _ambiguous_provider_error(
    *, stage: str, task_id: str | None = None
) -> SeedanceSubmissionUnknownError:
    if stage == "submit":
        message = (
            "Seedance 提交结果未知，请核对远端任务后再处理；"
            "系统不会自动重新提交。"
        )
    else:
        message = (
            f"Seedance 任务 {task_id or 'unknown'} 的{stage}结果未知；"
            "请核对远端任务后再处理，系统不会自动重新提交。"
        )
    error = _classified(
        SeedanceSubmissionUnknownError(message),
        "transient_network",
        retryable=False,
    )
    error.pause_state = "PROVIDER_ACTION_REQUIRED"
    if task_id:
        error.task_id = task_id
    return error


def _response_contract_error(detail: str) -> SeedanceParameterError:
    safe_detail = str(detail or "")[:200]
    error = SeedanceParameterError(
        "Seedance provider response did not match the expected contract; "
        f"provider_detail={safe_detail}"
    )
    error.provider_code = "InvalidProviderResponse"
    error.provider_detail = safe_detail
    return error


def _known_task_error(error: Exception, task_id: str) -> Exception:
    """Make any failure after task creation safe against a second paid POST."""
    error.task_id = task_id
    error.pause_state = "PROVIDER_ACTION_REQUIRED"
    error.retryable = False
    error.non_retryable = True
    return error


def _response_json_object(response, *, stage: str) -> dict:
    try:
        data = response.json()
    except (json.JSONDecodeError, TypeError, ValueError):
        raise _response_contract_error(
            f"{stage} response was not valid JSON"
        ) from None
    if not isinstance(data, dict):
        raise _response_contract_error(
            f"{stage} response must be an object"
        )
    return data


def _task_failure_error(task_id: str, data: dict) -> RuntimeError:
    detail = data.get("error") or {}
    if not isinstance(detail, dict):
        return _known_task_error(_provider_error(
            provider_message=str(detail)[:500],
            task_id=task_id,
        ), task_id)
    provider_code = str(detail.get("code") or "")
    error = _provider_error(
        provider_code=provider_code,
        provider_message=str(detail.get("message") or "")[:500],
        task_id=task_id,
    )
    if getattr(error, "stable_code", "") == "transient_network":
        error.args = (
            f"Seedance 任务 {task_id} 已在远端终止失败；请核对远端任务后再处理，"
            "系统不会自动重新提交。",
        )
    error.provider_code = _safe_provider_code(provider_code)
    if not hasattr(error, "provider_detail"):
        error.provider_detail = "Provider terminal task failure"
    return _known_task_error(error, task_id)


def _submission_error(response, *, endpoint: str, model: str,
                      content_roles: list[str]) -> Exception:
    del endpoint, model, content_roles
    if int(response.status_code) >= 500:
        return _ambiguous_provider_error(stage="submit")
    try:
        payload = json.loads(response.text or "{}")
    except (json.JSONDecodeError, TypeError):
        payload = {}
    detail = payload.get("error") if isinstance(payload, dict) else {}
    if not isinstance(detail, dict):
        detail = {}
    return _provider_error(
        provider_code=str(detail.get("code") or ""),
        provider_message=str(detail.get("message") or ""),
        status_code=int(response.status_code),
    )


def _headers():
    key = os.environ.get("ARK_API_KEY")
    if not key:
        raise SeedanceAccountUnavailableError(
            "缺少 ARK_API_KEY，Seedance 账号配置需要人工处理。"
        )
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _img_to_data_uri(path: str) -> str:
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except OSError as exc:
        raise SeedanceParameterError(
            f"Seedance reference image is not readable: {Path(path).name}"
        ) from exc
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
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except OSError as exc:
        raise SeedanceParameterError(
            f"Seedance reference media is not readable: {Path(path).name}"
        ) from exc
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
        raise SeedanceParameterError(
            f"unknown role {role!r}, valid: {_VIDEO_ROLES}"
        )
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
        raise SeedanceParameterError("output_format must be 'mp4' or 'mov'")
    if task_intent not in {"generate", "edit", "extend"}:
        raise SeedanceParameterError(
            "task_intent must be 'generate', 'edit', or 'extend'"
        )
    roles = {item.get("role") for item in content if item.get("role")}
    is_25 = "seedance-2-5" in model.lower()
    if is_25 and {"first_frame", "last_frame"} & roles and {
        "reference_image", "reference_video", "reference_audio"
    } & roles:
        raise SeedanceParameterError(
            "Seedance 2.5 strict first/last frames and multimodal references are mutually exclusive"
        )
    if task_intent == "edit":
        if ratio != "adaptive":
            raise SeedanceParameterError(
                "Seedance edit tasks require ratio='adaptive'"
            )
        if duration != -1:
            raise SeedanceParameterError("Seedance edit tasks require duration=-1")
        if not (({"reference_video"} if is_25 else {"reference_video", "reference_audio"}) & roles):
            raise SeedanceParameterError(
                "Seedance edit tasks require reference_video"
            )
    profile = capability_profile(model)
    if "seedance-2-5" in model.lower():
        if resolution not in profile["resolutions"]:
            raise SeedanceParameterError(
                "Seedance 2.5 resolution must be 480p, 720p, or 1080p"
            )
        if task_intent == "generate":
            duration = normalize_generation_duration(duration)
        if task_intent == "extend":
            if ratio != "adaptive":
                raise SeedanceParameterError(
                    "Seedance extend tasks require ratio='adaptive'"
                )
            if not ({"reference_video"} & roles):
                raise SeedanceParameterError(
                    "Seedance extend tasks require reference_video"
                )
            if duration != -1:
                duration = normalize_generation_duration(duration)

    payload = {
        "model": model,
        "content": content,
        "duration": int(round(duration)),
        "resolution": resolution,
        "output_format": output_format,
    }
    # omni_reference_task_type is a reference-task discriminator.  Sending it
    # on pure text-to-video requests makes the provider validate a reference
    # task that has no reference input and is rejected synchronously.  Keep it
    # for actual multimodal/edit/extend requests only.
    if "seedance-2-5" in model.lower() and roles & {"reference_image","reference_video","reference_audio"}:
        payload["omni_reference_task_type"] = (
            "reference" if task_intent == "generate" else task_intent
        )
    if not ({"first_frame", "last_frame"} & roles):
        payload["ratio"] = ratio
    # Seedance 2.x rejects camera_fixed in both T2V and I2V (including when
    # false); fixed-camera intent must be expressed in the prompt. Preserve
    # the legacy field only for older model families that still accept it.
    if "seedance-2-" not in model.lower() and not last_frame:
        payload["camera_fixed"] = camera_fixed
    if seed is not None:
        payload["seed"] = seed
    # motion_strength is not part of the published Seedance 2.5 API contract.
    # Keep it only for older/experimental model families that explicitly
    # accepted the field, so production 2.5 requests remain fail-closed.
    if motion_strength is not None and "seedance-2-5" not in model.lower():
        payload["motion_strength"] = motion_strength
    if generate_audio:
        payload["generate_audio"] = True
    return payload


def _deduplicate_reference_inputs(materials: list[str] | None,
                                  durations: list[float | None] | None,
                                  kind: str) -> tuple[list[str], list[float | None] | None]:
    materials = materials or []
    if durations is not None and len(durations) != len(materials):
        raise SeedanceParameterError(f"reference {kind} duration metadata must match material count")
    unique = list(dict.fromkeys(materials))
    if durations is None:
        return unique, None
    evidence = {}
    for uri, value in zip(materials, durations):
        seconds = reference_attention.known_duration(value)
        evidence[uri] = max((x for x in (evidence.get(uri), seconds) if x is not None), default=None)
    return unique, [evidence[uri] for uri in unique]


def _validate_reference_limits(
    model: str,
    *,
    image_count: int,
    video_count: int,
    audio_count: int,
) -> None:
    """Validate model-specific multimodal reference limits before submission."""
    limits = capability_profile(model)["reference_limits"]

    counts = {
        "reference_image": image_count,
        "reference_video": video_count,
        "reference_audio": audio_count,
    }
    for reference_type, count in counts.items():
        maximum = limits[reference_type]
        if count > maximum:
            raise SeedanceParameterError(
                f"{reference_type} count {count} exceeds max {maximum}"
            )


async def text_to_video(
    prompt: str,
    *,
    # 旧名(1.0 风格):reference_image 等于 first_frame,向后兼容
    reference_image: str | None = None,
    # 2.0 全 role:
    first_frame: str | None = None,
    last_frame: str | None = None,
    references: list[str] | None = None,           # 2.5 最多 30 张
    reference_videos: list[str] | None = None,     # 2.5 最多 10 段
    reference_audios: list[str] | None = None,     # 2.5 最多 10 段
    # Local preflight evidence only; these fields are never sent to the provider.
    reference_video_durations: list[float | None] | None = None,
    reference_audio_durations: list[float | None] | None = None,
    # 基本控制
    duration: float = 10.0,
    resolution: str = "720p",
    ratio: str = "9:16",
    seed: int | None = None,
    model: str | None = None,
    # 2.0 运动控制
    camera_fixed: bool = False,
    motion_strength: float | None = None,          # 仅旧/实验模型兼容
    # native audio(2.0 新):让视频自带音轨(对话/音效/环境音/BGM)
    generate_audio: bool = False,
    audio_output: bool | None = None,
    output_format: str = "mp4",
    task_intent: str = "generate",
    metadata_out: dict | None = None,
    on_task_created=None,
    on_task_updated=None,
    before_submit=None,
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
        raise SeedanceParameterError(
            f"unsupported resolution {resolution!r}; "
            f"use 480p/720p/1080p or a WxH key from {list(_RES_MAP)}"
        )

    resolved_model = model or DEFAULT_MODEL
    references = list(dict.fromkeys(references or []))
    reference_videos, reference_video_durations = _deduplicate_reference_inputs(
        reference_videos, reference_video_durations, "video",
    )
    reference_audios, reference_audio_durations = _deduplicate_reference_inputs(
        reference_audios, reference_audio_durations, "audio",
    )
    _validate_reference_limits(
        resolved_model,
        image_count=len(references or []),
        video_count=len(reference_videos or []),
        audio_count=len(reference_audios or []),
    )
    duration_manifest = []
    for kind, materials, durations in (
        ("video", reference_videos or [], reference_video_durations),
        ("audio", reference_audios or [], reference_audio_durations),
    ):
        if durations is None:
            continue  # Legacy callers may not carry duration evidence.
        if len(durations) != len(materials):
            raise SeedanceParameterError(f"reference {kind} duration metadata must match material count")
        duration_manifest.extend({"kind": kind, "duration_sec": seconds,
                                  "material_index": index}
                                 for index, seconds in enumerate(durations, 1))
    limits = capability_profile(resolved_model)["reference_duration_limits"]
    try:
        reference_attention.validate_durations(
            duration_manifest, max_video_duration_sec=limits.get("video"),
            max_audio_duration_sec=limits.get("audio"), require_known=True,
        )
    except ValueError as exc:
        raise SeedanceParameterError(str(exc)) from None
    if metadata_out is not None and duration_manifest:
        metadata_out["reference_manifest"] = reference_attention.sanitized_manifest(duration_manifest)

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

    runtime = provider_runtime.from_environment()
    if runtime.mode == "replay":
        replay = runtime.lookup("seedance.video.generate", payload)
        task_id = str(replay.get("task_id") or "").strip()
        if not task_id:
            raise provider_runtime.ReplayFixtureMissing(
                "Seedance video replay response is missing task_id"
            )
        media_path = Path(str(replay.get("media_path") or ""))
        if not media_path.is_file():
            raise provider_runtime.ReplayFixtureMissing(
                "Seedance video replay response is missing media"
            )
        if metadata_out is not None:
            metadata_out.update({
                "task_id": task_id,
                "usage": replay.get("usage") or {},
                "fixture_id": replay.get("fixture_id"),
                "request_hash": replay.get("request_hash"),
                "mode": "replay",
                "paid": False,
            })
        if on_task_created is not None:
            on_task_created(task_id)
        if on_task_updated is not None:
            on_task_updated(task_id, "completed", replay)
        return media_path.read_bytes()

    async with httpx.AsyncClient(timeout=timeout, headers=_headers()) as cli:
        # 1. 提交任务
        if before_submit is not None:
            before_submit()
        try:
            r = await cli.post(
                f"{ARK_BASE}/contents/generations/tasks", json=payload
            )
        except httpx.TransportError as exc:
            raise _submission_transport_error(exc) from exc
        if r.status_code >= 400:
            raise _submission_error(
                r,
                endpoint=f"{ARK_BASE}/contents/generations/tasks",
                model=payload["model"],
                content_roles=[
                    c.get("role") for c in content if c.get("role")
                ],
            )
        try:
            submit_data = _response_json_object(r, stage="submit")
        except SeedanceParameterError:
            # The remote side may have created a paid task before returning a
            # malformed 2xx response. Never turn that ambiguity into a repost.
            raise _ambiguous_provider_error(stage="submit") from None
        task_id = submit_data.get("id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise _ambiguous_provider_error(stage="submit")
        task_id = task_id.strip()
        if metadata_out is not None:
            metadata_out["task_id"] = task_id
        if on_task_created is not None:
            try:
                on_task_created(task_id)
            except Exception:
                raise _ambiguous_provider_error(
                    stage="本地持久化", task_id=task_id
                ) from None

        def notify_task_updated(status: str, data: dict) -> None:
            if on_task_updated is None:
                return
            try:
                on_task_updated(task_id, status, data)
            except Exception:
                raise _ambiguous_provider_error(
                    stage="本地持久化", task_id=task_id
                ) from None

        # 2. 轮询
        last_poll_diagnostic = None
        for _ in range(max(1, timeout // 5)):
            await asyncio.sleep(5)
            try:
                s = await cli.get(
                    f"{ARK_BASE}/contents/generations/tasks/{task_id}"
                )
            except httpx.TransportError as exc:
                # The paid task id is known. Continue polling that same task;
                # never create a second task because a status request failed.
                last_poll_diagnostic = transport_diagnostics.exception_diagnostic(
                    exc,
                    stage="poll",
                    endpoint=f"{ARK_BASE}/contents/generations/tasks/{task_id}",
                    submit_result_unknown=False,
                    submit_state="submitted_known",
                )
                continue
            if s.status_code >= 500:
                continue
            if s.status_code >= 400:
                notify_task_updated(
                    "action_required", {"status": "action_required"}
                )
                raise _known_task_error(_submission_error(
                    s,
                    endpoint=f"{ARK_BASE}/contents/generations/tasks/{task_id}",
                    model=payload["model"],
                    content_roles=[],
                ), task_id)
            try:
                data = _response_json_object(s, stage="poll")
            except SeedanceParameterError as exc:
                notify_task_updated(
                    "action_required", {"status": "action_required"}
                )
                raise _known_task_error(exc, task_id) from None
            status = data.get("status")
            if status in {"succeeded", "completed"}:
                content_result = data.get("content")
                video_url = (
                    content_result.get("video_url")
                    if isinstance(content_result, dict)
                    else None
                )
                if not isinstance(video_url, str) or not video_url.strip():
                    notify_task_updated("action_required", {
                        "status": "action_required",
                        "usage": data.get("usage") or {},
                    })
                    raise _known_task_error(
                        _response_contract_error(
                            f"{status} result missing video_url"
                        ),
                        task_id,
                    )
                video_url = video_url.strip()
                if metadata_out is not None:
                    metadata_out.update({
                        "task_id": task_id,
                        "usage": data.get("usage") or {},
                        "created_at": data.get("created_at"),
                        "updated_at": data.get("updated_at"),
                    })
                try:
                    video, _ = await provider_downloads.download_bytes(
                        video_url, timeout=120
                    )
                except httpx.TransportError as exc:
                    notify_task_updated("action_required", {
                        "status": "action_required",
                        "usage": data.get("usage") or {},
                    })
                    error = _ambiguous_provider_error(
                        stage="下载", task_id=task_id
                    )
                    error.transport_diagnostic = (
                        transport_diagnostics.exception_diagnostic(
                            exc,
                            stage="download",
                            endpoint=video_url,
                            submit_result_unknown=False,
                            submit_state="submitted_known",
                        )
                    )
                    raise error from None
                except httpx.HTTPStatusError as exc:
                    notify_task_updated("action_required", {
                        "status": "action_required",
                        "usage": data.get("usage") or {},
                    })
                    if exc.response.status_code >= 500:
                        raise _ambiguous_provider_error(
                            stage="下载", task_id=task_id
                        ) from None
                    error = _provider_error(
                        status_code=exc.response.status_code,
                        task_id=task_id,
                    )
                    raise _known_task_error(error, task_id) from None
                notify_task_updated("completed", data)
                return video
            if status == "failed":
                notify_task_updated("failed", data)
                raise _task_failure_error(task_id, data)
        notify_task_updated("action_required", {"status": "action_required"})
        error = _ambiguous_provider_error(stage="轮询", task_id=task_id)
        if last_poll_diagnostic is not None:
            error.transport_diagnostic = last_poll_diagnostic
        raise error


async def inspect_task(task_id: str) -> dict:
    """Inspect and, when complete, download one known remote task without POST."""
    normalized_id = str(task_id or "").strip()
    if not normalized_id:
        raise ValueError("Seedance task_id is required")
    runtime = provider_runtime.from_environment()
    if runtime.mode == "replay":
        replay = runtime.lookup(
            "seedance.video.inspect", {"task_id": normalized_id}
        )
        raw_status = str(replay.get("status") or "").strip().casefold()
        status = (
            "completed" if raw_status in {"completed", "succeeded"}
            else "failed" if raw_status == "failed"
            else "pending" if raw_status in {"pending", "queued", "running"}
            else "unknown"
        )
        result = {
            **replay,
            "status": status,
            "task_id": normalized_id,
            "usage": replay.get("usage") or {},
            "cost_cents": 0,
            "paid": False,
        }
        if status != "completed":
            return result
        media_path = Path(str(replay.get("media_path") or ""))
        if not media_path.is_file():
            return {**result, "status": "unknown"}
        return {
            **result,
            "video_url": media_path.as_uri(),
            "video": media_path.read_bytes(),
        }
    async with httpx.AsyncClient(timeout=120, headers=_headers()) as cli:
        try:
            response = await cli.get(
                f"{ARK_BASE}/contents/generations/tasks/{normalized_id}"
            )
        except httpx.TransportError as exc:
            return {
                "status": "unknown",
                "task_id": normalized_id,
                "transport_diagnostic": transport_diagnostics.exception_diagnostic(
                    exc,
                    stage="reconcile",
                    endpoint=f"{ARK_BASE}/contents/generations/tasks/{normalized_id}",
                    submit_result_unknown=False,
                    submit_state="submitted_known",
                ),
            }
        if response.status_code >= 500:
            return {"status": "unknown", "task_id": normalized_id}
        if response.status_code >= 400:
            response.raise_for_status()
        data = _response_json_object(response, stage="reconcile")
        raw_status = str(data.get("status") or "").strip().casefold()
        status = (
            "completed" if raw_status in {"completed", "succeeded"}
            else "failed" if raw_status == "failed"
            else "pending" if raw_status in {"pending", "queued", "running"}
            else "unknown"
        )
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        result = {
            "status": status,
            "task_id": normalized_id,
            "usage": usage,
            "cost_cents": usage_cost_cents(usage),
        }
        if status != "completed":
            return result
        content = data.get("content")
        video_url = (
            content.get("video_url") if isinstance(content, dict) else None
        )
        if not isinstance(video_url, str) or not video_url.strip():
            return {**result, "status": "unknown"}
        try:
            video, final_url = await provider_downloads.download_bytes(
                video_url.strip(), timeout=120
            )
        except httpx.HTTPError as exc:
            return {
                **result,
                "status": "unknown",
                "video_url": video_url,
                "transport_diagnostic": transport_diagnostics.exception_diagnostic(
                    exc,
                    stage="download",
                    endpoint=video_url,
                    submit_result_unknown=False,
                    submit_state="submitted_known",
                ),
            }
        return {**result, "video_url": final_url, "video": video}


async def text_to_image(prompt: str, resolution: str = "1024x1536",
                        seed: int | None = None,
                        model: str | None = None) -> bytes:
    """t2i 入口（可显式锁定模型，默认读取 ARK_IMAGE_MODEL）。
    主路径是 seedream_45.t2i / i2i;这里只作 IMAGE_PROVIDER=seedream 时的最终 fallback。
    """
    model = model or os.getenv(
        "ARK_IMAGE_MODEL", "doubao-seedream-5-0-260128"
    )
    payload = {
        "model": model,
        "prompt": prompt,
        "size": resolution,
        "watermark": False,
    }
    if seed is not None:
        payload["seed"] = seed
    runtime = provider_runtime.from_environment()
    if runtime.mode == "replay":
        replay = runtime.lookup("seedance.image.generate", payload)
        media_path = Path(str(replay.get("media_path") or ""))
        if not media_path.is_file():
            raise provider_runtime.ReplayFixtureMissing(
                "Seedance image replay response is missing media"
            )
        return media_path.read_bytes()
    async with httpx.AsyncClient(timeout=120, headers=_headers()) as cli:
        try:
            r = await cli.post(f"{ARK_BASE}/images/generations", json=payload)
        except httpx.TransportError as exc:
            raise _submission_transport_error(exc) from None
        if r.status_code >= 400:
            raise _submission_error(
                r,
                endpoint=f"{ARK_BASE}/images/generations",
                model=model,
                content_roles=[],
            )
        try:
            data = _response_json_object(r, stage="image submit")
        except SeedanceParameterError:
            raise _ambiguous_provider_error(stage="submit") from None
        items = data.get("data")
        url = (
            items[0].get("url")
            if isinstance(items, list) and items and isinstance(items[0], dict)
            else None
        )
        if not isinstance(url, str) or not url.strip():
            raise _ambiguous_provider_error(stage="submit")
        try:
            img, _ = await provider_downloads.download_bytes(
                url.strip(), timeout=120
            )
        except (ValueError, httpx.HTTPError):
            raise _ambiguous_provider_error(stage="图像下载") from None
        return img


ESTIMATION_PRICING_VERSION = "seedance25-area-v1"


def estimated_cost_rate_cents_per_second(resolution: str = "720p") -> int:
    """Conservative Seedance 2.5 rate calibrated by output pixel area."""
    return {
        "480p": 68,
        "720p": 153,
        "1080p": 344,
    }.get(str(resolution or "720p").lower(), 153)


def estimate_cost_cents(duration: float, resolution: str = "720p") -> int:
    """Conservative Seedance 2.5 estimate derived from measured token bills."""
    return int(
        estimated_cost_rate_cents_per_second(resolution) * duration
    )


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
