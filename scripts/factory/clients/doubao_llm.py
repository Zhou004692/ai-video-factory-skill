"""豆包大模型 client(via 火山方舟)。Drop-in 替代 Claude 用于分镜/合规/打分。

环境变量:
  ARK_API_KEY        方舟 API Key(复用 Seedance 的)
  ARK_LLM_MODEL      豆包模型 ID,默认 doubao-1-5-pro-32k-250115
                     如方舟要求带日期版本,以控制台为准
"""
import hashlib
import json
import logging
import os

import httpx

from .. import provider_runtime

from .claude import _extract_json  # JSON 抽取容错,公用

ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = os.getenv("ARK_LLM_MODEL", "doubao-1-5-pro-32k-250115")
DEFAULT_TIMEOUT_SEC = 300.0
DEFAULT_SYSTEM_PROMPT = (
    "你必须输出严格的 JSON,不要任何额外解释文字、不要 markdown 代码块。"
)
logger = logging.getLogger(__name__)


def _record_json_failure(error: json.JSONDecodeError, *, text: str,
                         payload: dict, kind: str, choice: dict | None = None,
                         usage: dict | None = None) -> json.JSONDecodeError:
    """Keep failure evidence without logging prompts, credentials or content."""
    text = str(text or "")
    diagnostic = {
        "kind": kind,
        "model": payload["model"],
        "max_tokens": payload["max_tokens"],
        "finish_reason": (choice or {}).get("finish_reason"),
        "input_tokens": (usage or {}).get("prompt_tokens"),
        "output_tokens": (usage or {}).get("completion_tokens"),
        "output_chars": len(text),
        "output_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "line": error.lineno,
        "column": error.colno,
    }
    error.response_diagnostic = diagnostic
    logger.warning("LLM JSON response rejected: %s", json.dumps(diagnostic, sort_keys=True))
    return error


def _timeout_seconds() -> float:
    raw = os.environ.get("ARK_LLM_TIMEOUT_SEC") or DEFAULT_TIMEOUT_SEC
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("ARK_LLM_TIMEOUT_SEC must be a positive number") from exc
    if value <= 0:
        raise ValueError("ARK_LLM_TIMEOUT_SEC must be a positive number")
    return value


def request_payload(prompt: str, system: str | None = None,
                    max_tokens: int = 4000,
                    model: str | None = None) -> dict:
    return {
        "model": model or DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "messages": [
            {"role": "system", "content": system or DEFAULT_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }


def complete_json(prompt: str, system: str | None = None,
                  max_tokens: int = 4000,
                  model: str | None = None, *, strict_json: bool = False) -> tuple[dict, dict]:
    """同步调用,返回 (JSON, usage_dict)。签名与 claude.complete_json 完全一致。"""
    payload = request_payload(
        prompt, system=system, max_tokens=max_tokens, model=model
    )

    runtime = provider_runtime.from_environment()
    if runtime.mode == "replay":
        replay = runtime.lookup("doubao.llm.complete_json", payload)
        result = replay.get("result")
        usage = replay.get("usage")
        if not isinstance(result, dict) or not isinstance(usage, dict):
            raise provider_runtime.ReplayFixtureMissing(
                "Doubao LLM replay response requires result and usage objects"
            )
        return result, {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
        }

    key = os.environ.get("ARK_API_KEY")
    if not key:
        raise RuntimeError("ARK_API_KEY not set in .env")

    with httpx.Client(timeout=_timeout_seconds()) as cli:
        r = cli.post(
            f"{ARK_BASE}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"doubao_llm {r.status_code}: {r.text[:800]} | "
                f"model={payload['model']} | "
                f"FIX: open this model in 方舟 console, or set ARK_LLM_MODEL "
                f"in .env to a model your account has activated"
            )
        try:
            body = r.json()
        except json.JSONDecodeError as error:
            _record_json_failure(
                error, text=r.text, payload=payload, kind="response_json"
            )
            raise

    choice = body["choices"][0]
    text = choice["message"]["content"]
    if choice.get("finish_reason") in {"length", "max_tokens"}:
        error = json.JSONDecodeError(
            "LLM JSON output truncated by token limit", text, len(text)
        )
        raise _record_json_failure(
            error, text=text, payload=payload, kind="truncated",
            choice=choice, usage=body.get("usage"),
        )
    try:
        data = json.loads(text) if strict_json else _extract_json(text)
    except json.JSONDecodeError as error:
        _record_json_failure(
            error, text=text, payload=payload, kind="syntax",
            choice=choice, usage=body.get("usage"),
        )
        raise
    usage = {
        "input_tokens": body["usage"]["prompt_tokens"],
        "output_tokens": body["usage"]["completion_tokens"],
    }
    return data, usage


def usage_to_cents(usage: dict, model: str = DEFAULT_MODEL) -> int:
    """豆包 1.5 Pro 32k:¥0.0008/k input,¥0.002/k output(2025 价)。
    一次分镜大约 ¥0.05-0.10。"""
    cents = (usage["input_tokens"] * 0.008 + usage["output_tokens"] * 0.02) / 100
    return max(1, int(cents))
