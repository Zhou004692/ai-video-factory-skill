"""豆包大模型 client(via 火山方舟)。Drop-in 替代 Claude 用于分镜/合规/打分。

环境变量:
  ARK_API_KEY        方舟 API Key(复用 Seedance 的)
  ARK_LLM_MODEL      豆包模型 ID,默认 doubao-1-5-pro-32k-250115
                     如方舟要求带日期版本,以控制台为准
"""
import os

import httpx

from .claude import _extract_json  # JSON 抽取容错,公用

ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = os.getenv("ARK_LLM_MODEL", "doubao-1-5-pro-32k-250115")


def complete_json(prompt: str, system: str | None = None,
                  max_tokens: int = 4000,
                  model: str | None = None) -> tuple[dict, dict]:
    """同步调用,返回 (JSON, usage_dict)。签名与 claude.complete_json 完全一致。"""
    key = os.environ.get("ARK_API_KEY")
    if not key:
        raise RuntimeError("ARK_API_KEY not set in .env")

    payload = {
        "model": model or DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "messages": [
            {"role": "system", "content": system or
             "你必须输出严格的 JSON,不要任何额外解释文字、不要 markdown 代码块。"},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }

    with httpx.Client(timeout=120) as cli:
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
        body = r.json()

    text = body["choices"][0]["message"]["content"]
    data = _extract_json(text)
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
