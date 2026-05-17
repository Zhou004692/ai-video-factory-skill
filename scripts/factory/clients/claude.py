"""Anthropic Claude client。用于分镜、合规、选题打分。"""
import json
import os
import re

import anthropic

_client = None
DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-7")


def get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set in .env")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _extract_json(text: str):
    """从 LLM 输出里抽 JSON。容忍 ```json ... ``` 围栏。"""
    # 先试整段
    try:
        return json.loads(text)
    except Exception:
        pass
    # 找代码块
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    # 找首个 { 到末个 }
    s = text.find("{")
    e = text.rfind("}")
    if s >= 0 and e > s:
        return json.loads(text[s:e+1])
    raise ValueError(f"could not parse JSON from: {text[:500]}")


def complete_json(prompt: str, system: str = None, max_tokens: int = 4000,
                  model: str = None) -> tuple[dict, dict]:
    """同步调用,返回 (JSON, usage_dict)。"""
    client = get_client()
    msg = client.messages.create(
        model=model or DEFAULT_MODEL,
        max_tokens=max_tokens,
        system=system or "你必须输出严格的 JSON,不要任何额外解释文字、不要 markdown 代码块。",
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text
    data = _extract_json(text)
    usage = {
        "input_tokens": msg.usage.input_tokens,
        "output_tokens": msg.usage.output_tokens,
    }
    return data, usage


def usage_to_cents(usage: dict, model: str = DEFAULT_MODEL) -> int:
    """大致换算成本(分)。Opus 4.7 估算价。"""
    if "opus" in model:
        # $15/M in, $75/M out → 7 元/M 国内汇率近似
        cents = (usage["input_tokens"] * 0.15 + usage["output_tokens"] * 0.75) / 100
    elif "sonnet" in model:
        cents = (usage["input_tokens"] * 0.03 + usage["output_tokens"] * 0.15) / 100
    else:
        cents = (usage["input_tokens"] * 0.01 + usage["output_tokens"] * 0.05) / 100
    return max(1, int(cents))
