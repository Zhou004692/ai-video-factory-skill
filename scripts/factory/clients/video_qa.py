"""Doubao video-understanding quality gate."""
from dataclasses import dataclass
from difflib import SequenceMatcher
import os
import re

import httpx

from .claude import _extract_json
from . import doubao_llm


ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"


@dataclass(frozen=True)
class QADecision:
    passed: bool
    reasons: tuple[str, ...]
    transcript_similarity: float | None = None


_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}


def _chinese_integer(value: str) -> str:
    if not value:
        return ""
    if not any(ch in _CN_SMALL_UNITS or ch == "万" for ch in value):
        return "".join(str(_CN_DIGITS[ch]) for ch in value)

    total = 0
    section = 0
    number = 0
    for char in value:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
        elif char in _CN_SMALL_UNITS:
            unit = _CN_SMALL_UNITS[char]
            section += (number or 1) * unit
            number = 0
        elif char == "万":
            total += (section + number) * 10000
            section = 0
            number = 0
    return str(total + section + number)


def _replace_chinese_number(match: re.Match[str]) -> str:
    value = match.group(0)
    if "点" not in value:
        return _chinese_integer(value)
    integer, fraction = value.split("点", 1)
    return f"{_chinese_integer(integer)}.{_chinese_integer(fraction)}"


def normalize_transcript_text(value: object) -> str:
    """Canonicalize spoken Chinese numerals and BigASR ITN output."""
    text = str(value or "").lower()
    text = re.sub(
        r"[零〇一二两三四五六七八九十百千万]+(?:点[零〇一二两三四五六七八九]+)?",
        _replace_chinese_number,
        text,
    )
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text)


def _normalized_text(value: object) -> str:
    return normalize_transcript_text(value)


def _visual_comparison_prompt(expected: str, observed: str) -> str:
    """Build a text-only, second-stage semantic comparison prompt."""
    return (
        "你是视频语义验收器。只比较目标画面与盲审观察，不评价清晰度或美感。"
        "只把盲审明确描述且与目标相反的内容记为 mismatch；"
        "未提及不等于不匹配，不得因盲审省略次要细节、画幅、镜头稳定性、"
        "水印或音频描述而扣分。主体、场景和关键动作都匹配且无明确冲突时，"
        "alignment 应不低于 0.75；只有次要明确差异时为 0.75-0.89；"
        "如果主体对象、场景或关键动作存在明确冲突，alignment 必须低于 0.5。"
        "只输出 JSON："
        '{"alignment":0.0,"matches":[],"mismatches":[],"reason":""}。\n'
        f"目标画面：{expected}\n"
        f"盲审观察：{observed}"
    )


def evaluate(result: dict, *, expect_speech: bool,
             expected_text: str | None = None,
             expected_visual: str | None = None,
             forbid_people: bool = False,
             forbid_readable_text: bool = False,
             min_confidence: float = 0.8,
             min_transcript_similarity: float = 0.95,
             min_visual_alignment: float = 0.75) -> QADecision:
    """Convert model output into a deterministic pipeline decision."""
    reasons: list[str] = []
    transcript = _normalized_text(result.get("transcript"))
    confidence = float(result.get("confidence") or 0)
    similarity: float | None = None

    # `confidence` is the transcript confidence. For a B-roll that correctly
    # contains no speech, an empty transcript has no confidence to score.
    if confidence < min_confidence and (expect_speech or bool(transcript)):
        reasons.append("low_confidence")
    if not expect_speech and transcript:
        reasons.append("unexpected_speech")
    if expect_speech and expected_text:
        expected = _normalized_text(expected_text)
        similarity = SequenceMatcher(None, expected, transcript).ratio()
        if similarity < min_transcript_similarity:
            reasons.append("transcript_mismatch")
    if expected_visual:
        comparison = result.get("visual_comparison") or {}
        visual_alignment = float(
            comparison.get("alignment", result.get("visual_alignment") or 0)
        )
        if visual_alignment < min_visual_alignment:
            reasons.append("visual_mismatch")
    if forbid_people and bool(result.get("has_visible_person")):
        reasons.append("unexpected_person")
    if forbid_readable_text and bool(result.get("has_readable_text")):
        reasons.append("unexpected_readable_text")
    return QADecision(not reasons, tuple(reasons), similarity)


def analyze(video_url: str, *, expect_speech: bool,
            expected_text: str | None = None,
            expected_visual: str | None = None,
            forbid_people: bool = False,
            forbid_readable_text: bool = False,
            model: str | None = None,
            timeout: int = 180) -> tuple[dict, QADecision]:
    """Analyze a remotely accessible MP4 and return raw output + decision."""
    key = os.environ.get("ARK_API_KEY")
    if not key:
        raise RuntimeError("ARK_API_KEY not set in .env")
    model = model or os.getenv(
        "ARK_VIDEO_QA_MODEL",
        os.getenv("ARK_LLM_MODEL", "doubao-seed-2-0-pro-260215"),
    )
    prompt = (
        "你是视频盲审观察员。你不知道目标提示词，只能客观描述视频中实际"
        "出现的主体、场景、动作、人物、文字和声音。不要猜测创作意图。"
        "只输出 JSON："
        '{"transcript":"逐字转写，无人声则为空字符串",'
        '"extra_audio":"实际听到的环境音和音效",'
        '"lip_sync":true,"confidence":0.0,'
        '"observed_visual":"客观、具体描述实际画面",'
        '"has_visible_person":false,"has_readable_text":false,'
        '"notes":"其他观察"}。'
    )
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{
            "role": "user",
            "content": [
                {"type": "video_url", "video_url": {"url": video_url}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    with httpx.Client(timeout=timeout) as cli:
        response = cli.post(
            f"{ARK_BASE}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"video_qa {response.status_code}: {response.text[:800]} | "
                f"model={model}"
            )
        body = response.json()
    content = body["choices"][0]["message"]["content"]
    raw = _extract_json(content)
    if expected_visual:
        observed = str(raw.get("observed_visual") or raw.get("notes") or "")
        try:
            comparison, _ = doubao_llm.complete_json(
                _visual_comparison_prompt(expected_visual, observed),
                system=(
                    "你只做目标与观察的语义比较，严格输出 JSON。"
                    "主体对象不同必须判为不匹配。"
                ),
                max_tokens=600,
                model=model,
            )
        except Exception as exc:
            comparison = {
                "alignment": 0.0,
                "matches": [],
                "mismatches": ["comparison_error"],
                "reason": str(exc),
            }
        raw["visual_comparison"] = comparison
        raw["visual_alignment"] = float(comparison.get("alignment") or 0)
    return raw, evaluate(
        raw,
        expect_speech=expect_speech,
        expected_text=expected_text,
        expected_visual=expected_visual,
        forbid_people=forbid_people,
        forbid_readable_text=forbid_readable_text,
    )
