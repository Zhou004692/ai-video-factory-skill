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


def _json_candidate(text: str) -> str:
    """Return the first JSON-looking top-level value, including truncation."""
    starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    if not starts:
        return ""
    return text[min(starts):].strip().removesuffix("```").strip()


def _repair_json_text(text: str) -> str:
    """Conservatively repair common LLM JSON syntax damage.

    This layer only repairs syntax. Domain validators still decide whether a
    recovered object is complete enough to persist or requires a model retry.
    """
    candidate = _json_candidate(text)
    if not candidate:
        return ""
    expected_closers = {"{": "}", "[": "]"}
    stack: list[str] = []
    output: list[str] = []
    in_string = False
    escaped = False
    for char in candidate:
        if in_string:
            if escaped:
                output.append(char)
                escaped = False
            elif char == "\\":
                output.append(char)
                escaped = True
            elif char == '"':
                output.append(char)
                in_string = False
            elif char == "\n":
                output.append("\\n")
            elif char == "\r":
                output.append("\\r")
            elif char == "\t":
                output.append("\\t")
            elif ord(char) < 0x20:
                output.append(f"\\u{ord(char):04x}")
            else:
                output.append(char)
            continue
        if char == '"':
            output.append(char)
            in_string = True
            continue
        if char in expected_closers:
            stack.append(expected_closers[char])
            output.append(char)
            continue
        if char in ("}", "]"):
            output.append(char)
            if stack and stack[-1] == char:
                stack.pop()
                if not stack:
                    break
            continue
        output.append(char)
    if escaped:
        output.append("\\")
    if in_string:
        output.append('"')
    output.extend(reversed(stack))
    repaired = "".join(output)
    string_token = r'"(?:\\.|[^"\\])*"'
    value_token = rf'(?:{string_token}|[}}\]]|-?\d+(?:\.\d+)?|true|false|null)'
    key_token = rf'(?={string_token}\s*:)'
    repaired = re.sub(
        rf'({value_token})\s+{key_token}', lambda match: match.group(1) + ",",
        repaired,
    )
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    return repaired


def _extract_json(text: str):
    """Extract strict JSON, then apply deterministic local syntax recovery."""
    candidates = [str(text or "").strip()]
    candidates.extend(
        match.group(1).strip() for match in re.finditer(
            r"```(?:json)?\s*([\s\S]*?)\s*```", str(text or ""), re.IGNORECASE
        )
    )
    candidate = _json_candidate(str(text or ""))
    if candidate:
        candidates.append(candidate)
    last_error: json.JSONDecodeError | None = None
    for candidate in dict.fromkeys(item for item in candidates if item):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as error:
            last_error = error
    for candidate in dict.fromkeys(item for item in candidates if item):
        repaired = _repair_json_text(candidate)
        if not repaired:
            continue
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as error:
            last_error = error
    if last_error:
        raise last_error
    raise json.JSONDecodeError("could not find JSON object", str(text or ""), 0)


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
