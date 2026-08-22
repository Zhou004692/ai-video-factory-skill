"""Volcengine TTS 2.0 speaker catalog and fixed-voice synthesis clients."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from typing import Any
from urllib.parse import quote, urlencode
import uuid

import httpx


CATALOG_VERSION = "2025-05-20"
CATALOG_SERVICE = "speech_saas_prod"
CATALOG_ENDPOINT = "open.volcengineapi.com"
TTS_ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
DEFAULT_RESOURCE_ID = "seed-tts-2.0"


class VolcTTSConfigError(RuntimeError):
    """The speech credentials required before a paid request are absent."""


class VolcTTSResponseError(RuntimeError):
    """The speech service returned an invalid or failed response."""


class VolcTTSSubmissionUnknownError(VolcTTSResponseError):
    """The TTS POST may have been accepted; automatic repost is unsafe."""

    submit_result_unknown = True
    pause_state = "PROVIDER_ACTION_REQUIRED"
    non_retryable = True


class VolcTTSRejectedError(VolcTTSResponseError):
    """The service explicitly rejected the request before useful output."""

    submit_result_unknown = False
    non_retryable = True


@dataclass(frozen=True)
class TTSResult:
    audio: bytes
    duration_ms: int
    timestamps: tuple[dict, ...]
    request_id: str
    usage: dict


def estimate_cost_cents(text: str) -> int:
    """Use the account-configured TTS price; never invent a public price."""
    try:
        rate = max(
            0,
            float(os.getenv("VOLC_TTS_PRICE_CENTS_PER_10K_CHARS", "0")),
        )
    except ValueError:
        rate = 0
    return int(math.ceil(len(str(text or "")) * rate / 10000))


class VolcSpeechClient:
    """Small HMAC client for the account-visible ListSpeakers API."""

    def __init__(
        self, *, ak: str | None = None, sk: str | None = None,
        region: str = "cn-beijing", endpoint: str = CATALOG_ENDPOINT,
    ) -> None:
        self.ak = str(ak or os.getenv("VOLC_AK") or "").strip()
        self.sk = str(sk or os.getenv("VOLC_SK") or "").strip()
        self.region = region
        self.endpoint = endpoint
        if not self.ak or not self.sk:
            raise VolcTTSConfigError(
                "VOLC_AK / VOLC_SK 未配置，无法读取火山声线库"
            )
        self._http = httpx.Client(timeout=20, trust_env=False)

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def _hmac(key: bytes, value: str) -> bytes:
        return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()

    def _signed_headers(self, query: dict, body: bytes) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        x_date = now.strftime("%Y%m%dT%H%M%SZ")
        date = x_date[:8]
        body_hash = hashlib.sha256(body).hexdigest()
        canonical_query = urlencode(sorted(query.items()), quote_via=quote)
        canonical_headers = (
            "content-type:application/json\n"
            f"host:{self.endpoint}\n"
            f"x-content-sha256:{body_hash}\n"
            f"x-date:{x_date}\n"
        )
        signed_headers = "content-type;host;x-content-sha256;x-date"
        canonical_request = "\n".join((
            "POST", "/", canonical_query, canonical_headers,
            signed_headers, body_hash,
        ))
        scope = (
            f"{date}/{self.region}/{CATALOG_SERVICE}/request"
        )
        string_to_sign = "\n".join((
            "HMAC-SHA256", x_date, scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ))
        key_date = self._hmac(self.sk.encode("utf-8"), date)
        key_region = self._hmac(key_date, self.region)
        key_service = self._hmac(key_region, CATALOG_SERVICE)
        signing_key = self._hmac(key_service, "request")
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            "Host": self.endpoint,
            "X-Content-Sha256": body_hash,
            "X-Date": x_date,
            "Authorization": (
                f"HMAC-SHA256 Credential={self.ak}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
        }

    def _call(self, action: str, payload: dict) -> dict:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        query = {"Action": action, "Version": CATALOG_VERSION}
        try:
            response = self._http.post(
                f"https://{self.endpoint}/", params=query, content=body,
                headers=self._signed_headers(query, body),
            )
            response.raise_for_status()
            decoded = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise VolcTTSResponseError(
                "读取火山声线库失败，请检查语音服务权限和网络"
            ) from exc
        error = (decoded.get("ResponseMetadata") or {}).get("Error") or {}
        if error:
            code = str(error.get("Code") or "SpeechCatalogError")
            raise VolcTTSResponseError(
                f"读取火山声线库失败：{code}"
            )
        result = decoded.get("Result") or {}
        if not isinstance(result, dict):
            raise VolcTTSResponseError("火山声线库返回格式无效")
        return result

    def list_speakers(
        self, *, resource_id: str = DEFAULT_RESOURCE_ID,
        page: int = 1, limit: int = 100,
    ) -> list[dict]:
        current_page = max(1, int(page))
        page_size = min(100, max(1, int(limit)))
        collected: list[dict] = []
        while True:
            result = self._call("ListSpeakers", {
                "ResourceIDs": [resource_id],
                "Page": current_page,
                "Limit": page_size,
            })
            speakers = result.get("Speakers") or []
            if not isinstance(speakers, list):
                raise VolcTTSResponseError("火山声线库 Speakers 字段无效")
            collected.extend(
                item for item in speakers if isinstance(item, dict)
            )
            try:
                total = max(0, int(result.get("Total") or 0))
            except (TypeError, ValueError):
                total = 0
            if not speakers or not total or len(collected) >= total:
                break
            current_page += 1
        return collected


def _decode_json_stream(raw: str) -> list[dict]:
    decoder = json.JSONDecoder()
    index = 0
    events: list[dict] = []
    while index < len(raw):
        while index < len(raw) and raw[index].isspace():
            index += 1
        if index >= len(raw):
            break
        try:
            value, index = decoder.raw_decode(raw, index)
        except json.JSONDecodeError as exc:
            raise VolcTTSResponseError("火山 TTS 返回了无法解析的数据流") from exc
        if isinstance(value, dict):
            events.append(value)
    return events


async def synthesize(
    *, text: str, voice_type: str,
    app_id: str | None = None, api_key: str | None = None,
    resource_id: str = DEFAULT_RESOURCE_ID,
    voice_instruction: str = "", speed_ratio: float = 1.0,
    transport: httpx.AsyncBaseTransport | None = None,
    endpoint: str = TTS_ENDPOINT,
    request_id: str | None = None,
) -> TTSResult:
    """Synthesize exact TTS 2.0 audio for one role using one fixed voice."""
    app_id = str(app_id or os.getenv("VOLC_TTS_APPID") or "").strip()
    api_key = str(
        api_key
        or os.getenv("VOLC_TTS_API_KEY")
        or os.getenv("VOLC_TTS_ACCESS_TOKEN")
        or ""
    ).strip()
    if not app_id or not api_key:
        raise VolcTTSConfigError(
            "VOLC_TTS_APPID / VOLC_TTS_API_KEY 未配置，无法生成固定声线对白"
        )
    text = str(text or "").strip()
    voice_type = str(voice_type or "").strip()
    if not text or not voice_type:
        raise VolcTTSConfigError("TTS 文本和 voice_type 不能为空")
    request_id = str(request_id or uuid.uuid4())
    req_params: dict[str, Any] = {
        "text": text,
        "speaker": voice_type,
        "audio_params": {"format": "mp3", "sample_rate": 24000},
        "speed_ratio": max(0.5, min(2.0, float(speed_ratio))),
        "with_timestamp": True,
    }
    if str(voice_instruction or "").strip():
        req_params["voice_instruction"] = str(voice_instruction).strip()
    payload = {
        "user": {"uid": f"ai-video-factory-{request_id[:8]}"},
        "req_params": req_params,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Api-App-Id": app_id,
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": request_id,
        "X-Control-Require-Usage-Tokens-Return": "text_words",
    }
    async with httpx.AsyncClient(
        timeout=120, transport=transport, trust_env=False
    ) as client:
        try:
            response = await client.post(endpoint, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise VolcTTSSubmissionUnknownError(
                "火山 TTS 提交结果不可确认，系统已阻止自动重发以避免重复计费"
            ) from exc
        if 400 <= response.status_code < 500:
            raise VolcTTSRejectedError(
                f"火山 TTS 拒绝请求（HTTP {response.status_code}），"
                "请检查声线授权与请求参数"
            )
        if response.status_code >= 500:
            raise VolcTTSSubmissionUnknownError(
                "火山 TTS 服务端异常，提交结果不可确认；请先人工核对"
            )
    try:
        events = _decode_json_stream(response.text)
    except VolcTTSResponseError as exc:
        raise VolcTTSSubmissionUnknownError(str(exc)) from exc
    chunks: list[bytes] = []
    timestamps: list[dict] = []
    duration_ms = 0
    usage: dict = {}
    for event in events:
        code = event.get("code")
        if code not in (None, 0, 20000000):
            safe_code = str(code)[:40]
            raise VolcTTSSubmissionUnknownError(
                f"火山 TTS 返回错误：{safe_code}；请先核对本次请求"
            )
        encoded = event.get("data")
        if encoded:
            try:
                chunks.append(base64.b64decode(encoded, validate=True))
            except (ValueError, TypeError) as exc:
                raise VolcTTSSubmissionUnknownError(
                    "火山 TTS 音频分片无效；请先核对本次请求"
                ) from exc
        addition = event.get("addition") or {}
        if isinstance(addition, dict):
            duration_ms = max(
                duration_ms, int(addition.get("duration") or 0)
            )
            candidate_timestamps = (
                addition.get("timestamps")
                or addition.get("subtitles")
                or []
            )
            if isinstance(candidate_timestamps, list):
                timestamps.extend(
                    item for item in candidate_timestamps
                    if isinstance(item, dict)
                )
            if isinstance(addition.get("usage"), dict):
                usage.update(addition["usage"])
        if isinstance(event.get("usage"), dict):
            usage.update(event["usage"])
    audio = b"".join(chunks)
    if not audio:
        raise VolcTTSSubmissionUnknownError(
            "火山 TTS 未返回音频数据；请先核对本次请求"
        )
    return TTSResult(
        audio=audio,
        duration_ms=duration_ms,
        timestamps=tuple(timestamps),
        request_id=request_id,
        usage=usage,
    )
