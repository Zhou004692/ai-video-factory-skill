"""Doubao video-understanding quality gate."""
from dataclasses import dataclass
from difflib import SequenceMatcher
import json
import os
import re

import httpx

from .. import transport_diagnostics
from .claude import _extract_json
from . import doubao_llm


ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"


class VideoQATransportError(RuntimeError):
    """Sanitized QA transport failure with submit certainty evidence."""

    stable_code = "transient_network"
    code = stable_code
    error_code = stable_code


def _transport_error(exc: BaseException, *, stage: str) -> VideoQATransportError:
    submit_unknown = not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
    error = VideoQATransportError(
        "视频自动质检网络不可用；候选母片已保留并转人工复核。"
    )
    error.submit_result_unknown = submit_unknown
    error.retryable = False
    error.non_retryable = True
    error.transport_diagnostic = transport_diagnostics.exception_diagnostic(
        exc,
        stage=stage,
        endpoint=f"{ARK_BASE}/chat/completions",
        submit_result_unknown=submit_unknown,
    )
    return error


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


def normalize_lip_sync_evidence(result: dict) -> dict:
    """Normalize model observations into reviewable lip-sync intervals."""
    source = result.get("lip_sync_evidence") or {}
    if not isinstance(source, dict):
        source = {}

    def score(value: object, default: float = 0.0) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return default

    segments = []
    for item in source.get("segments") or []:
        if not isinstance(item, dict):
            continue
        try:
            start = max(0.0, float(item.get("start_sec") or 0))
            end = max(0.0, float(item.get("end_sec") or 0))
            offset = int(round(float(item.get("offset_ms") or 0)))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        alignment = score(item.get("alignment"), 1.0)
        confidence = score(item.get("confidence"), 0.0)
        absolute_offset = abs(offset)
        if alignment < 0.60 or absolute_offset >= 250:
            severity = "high"
        elif alignment < 0.75 or absolute_offset >= 160:
            severity = "medium"
        else:
            severity = "low"
        segments.append({
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "alignment": round(alignment, 4),
            "confidence": round(confidence, 4),
            "offset_ms": offset,
            "dialogue": str(item.get("dialogue") or "").strip(),
            "reason": str(item.get("reason") or "").strip(),
            "severity": severity,
        })
    normalized = {
        "overall_alignment": round(
            score(source.get("overall_alignment"), 1.0), 4
        ),
        "confidence": round(score(source.get("confidence"), 0.0), 4),
        "segments": sorted(segments, key=lambda item: (
            item["start_sec"], item["end_sec"]
        )),
    }
    result["lip_sync_evidence"] = normalized
    return normalized


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
             require_lip_sync: bool = True,
             expected_text: str | None = None,
             expected_visual: str | None = None,
             forbid_people: bool = False,
             forbid_readable_text: bool = False,
             require_broll_semantics: bool = False,
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
    lip_sync_evidence = normalize_lip_sync_evidence(result)
    located_mismatch = bool(
        lip_sync_evidence["confidence"] >= 0.65
        and lip_sync_evidence["overall_alignment"] < 0.78
    ) or any(
        segment["confidence"] >= 0.65
        and (
            segment["alignment"] < 0.75
            or abs(segment["offset_ms"]) >= 160
        )
        for segment in lip_sync_evidence["segments"]
    )
    if expect_speech and require_lip_sync and (
        result.get("lip_sync") is False or located_mismatch
    ):
        reasons.append("lip_sync_mismatch")
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
    if require_broll_semantics:
        semantics = result.get("broll_semantics") or {}
        if not semantics:
            reasons.append("broll_semantic_evidence_missing")
        else:
            if not bool(semantics.get("industry_subject_visible")):
                reasons.append("missing_industry_subject")
            if not bool(semantics.get("environment_context_visible")):
                reasons.append("missing_environment_context")
            if not bool(semantics.get("meaningful_action_visible")):
                reasons.append("missing_meaningful_action")
            dominant = str(semantics.get("dominant_subject") or "").lower()
            hand_ratio = float(semantics.get("hand_closeup_ratio") or 0)
            if dominant in {"hand", "hands", "finger", "fingers"} \
                    and hand_ratio >= 0.55:
                reasons.append("meaningless_hand_closeup")
    return QADecision(not reasons, tuple(reasons), similarity)


def evaluate_continuity(
    result: dict,
    *,
    min_identity_alignment: float = 0.82,
    min_scene_alignment: float = 0.78,
    min_blocking_alignment: float = 0.80,
    blocking_mode: str = "continuity",
) -> QADecision:
    """Fail closed only for continuity dimensions backed by references."""
    reasons: list[str] = []
    if bool(result.get("identity_checked")) and float(
        result.get("identity_alignment") or 0
    ) < min_identity_alignment:
        reasons.append("identity_drift")
    if bool(result.get("scene_checked")) and float(
        result.get("scene_alignment") or 0
    ) < min_scene_alignment:
        reasons.append("scene_drift")
    if str(blocking_mode or "continuity").casefold() != "free" \
            and bool(result.get("blocking_checked")) and float(
        result.get("blocking_alignment") or 0
    ) < min_blocking_alignment:
        reasons.append("blocking_drift")
    if (
        bool(result.get("wardrobe_checked", "wardrobe_match" in result))
        and result.get("wardrobe_match") is False
    ):
        reasons.append("wardrobe_drift")
    if (
        bool(result.get("prop_state_checked", "prop_state_match" in result))
        and result.get("prop_state_match") is False
    ):
        reasons.append("prop_state_mismatch")
    return QADecision(not reasons, tuple(reasons))


def analyze_continuity(
    current_video_url: str,
    *,
    contract: dict,
    identity_image_urls: list[str] | tuple[str, ...] = (),
    scene_image_url: str | None = None,
    previous_video_url: str | None = None,
    model: str | None = None,
    timeout: int = 180,
) -> tuple[dict, QADecision]:
    """Compare a candidate master against canonical and incremental state."""
    key = os.environ.get("ARK_API_KEY")
    if not key:
        raise RuntimeError("ARK_API_KEY not set in .env")
    model = model or os.getenv(
        "ARK_VIDEO_QA_MODEL",
        os.getenv("ARK_LLM_MODEL", "doubao-seed-2-0-pro-260215"),
    )
    spatial = contract.get("spatial") if isinstance(contract, dict) else {}
    spatial = spatial if isinstance(spatial, dict) else {}
    legacy_mode = (
        "locked"
        if isinstance(contract, dict) and int(contract.get("version") or 1) < 2
        else "continuity"
    )
    blocking_mode = str(
        spatial.get("mode")
        or (contract.get("blocking_mode") if isinstance(contract, dict) else "")
        or legacy_mode
    ).strip().casefold()
    if blocking_mode not in {"free", "continuity", "locked"}:
        blocking_mode = "continuity"
    blocking_instruction = {
        "free": (
            "本镜为空间自由调度：不得用左右槽位或绝对站位判失败，"
            "blocking_checked 必须为 false；仍需检查人物身份和场景。"
        ),
        "continuity": (
            "本镜为空间连续调度：检查上一镜结束状态到本镜开始状态、视线、"
            "出入方向和180度轴线是否连贯；允许剧情驱动的走位，不得按绝对"
            "左右位置或静态构图判失败。"
        ),
        "locked": (
            "本镜为精确站位锁定：按契约检查人物左右顺序、相对距离、朝向和"
            "指定位置。"
        ),
    }[blocking_mode]
    prompt = (
        "你是跨镜头连续性质检器。第一个视频是当前候选母片；随后图片依次是"
        "人物标准定妆照和场景标准锚点；最后一个视频（如有）是上一条已验收"
        "的同场景母片。标准锚点是真值，上一母片只用于动作、站位和空间状态。"
        "逐项判断人物身份、视觉年龄、发型、服装、场景结构、设备位置、人物"
        "空间关系、道具状态和180度轴线。" + blocking_instruction + "没有对应参考证据的"
        "维度必须把 checked 设为 false，不得猜测。只输出 JSON："
        '{"identity_checked":false,"identity_alignment":0.0,'
        '"scene_checked":false,"scene_alignment":0.0,'
        '"blocking_checked":false,"blocking_alignment":0.0,'
        '"wardrobe_checked":false,"wardrobe_match":true,'
        '"prop_state_checked":false,"prop_state_match":true,'
        '"mismatches":[],"reason":""}。\n'
        "continuity_contract="
        + json.dumps(contract or {}, ensure_ascii=False)
    )
    content: list[dict] = [
        {"type": "text", "text": prompt},
        {"type": "video_url", "video_url": {"url": current_video_url}},
    ]
    for url in identity_image_urls:
        if str(url or "").startswith("https://"):
            content.append({"type": "image_url", "image_url": {"url": str(url)}})
    if str(scene_image_url or "").startswith("https://"):
        content.append({
            "type": "image_url", "image_url": {"url": str(scene_image_url)}
        })
    if str(previous_video_url or "").startswith("https://"):
        content.append({
            "type": "video_url", "video_url": {"url": str(previous_video_url)}
        })
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": content}],
    }
    with httpx.Client(timeout=timeout) as cli:
        try:
            response = cli.post(
                f"{ARK_BASE}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
            )
        except httpx.TransportError as exc:
            raise _transport_error(exc, stage="continuity_qa_submit") from exc
        if response.status_code >= 400:
            raise RuntimeError(
                f"continuity_qa {response.status_code}: {response.text[:800]} | "
                f"model={model}"
            )
        body = response.json()
    raw = _extract_json(body["choices"][0]["message"]["content"])
    return raw, evaluate_continuity(raw, blocking_mode=blocking_mode)


def analyze(video_url: str, *, expect_speech: bool,
            require_lip_sync: bool = True,
            expected_text: str | None = None,
            expected_visual: str | None = None,
            forbid_people: bool = False,
            forbid_readable_text: bool = False,
            require_broll_semantics: bool = False,
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
        '"lip_sync_evidence":{"overall_alignment":1.0,"confidence":0.0,'
        '"segments":[{"start_sec":0.0,"end_sec":0.0,'
        '"alignment":1.0,"confidence":0.0,"offset_ms":0,'
        '"dialogue":"","reason":""}]},'
        '"observed_visual":"客观、具体描述实际画面",'
        '"has_visible_person":false,"has_readable_text":false,'
        '"notes":"其他观察"}。'
    )
    if require_broll_semantics:
        prompt += (
            "\n这是行业场景 B-roll 语义质检。还必须输出 broll_semantics："
            "industry_subject_visible 表示是否清楚出现提示词要求的行业主体或设备；"
            "environment_context_visible 表示是否存在能说明地点与主体关系的环境建立画面；"
            "meaningful_action_visible 表示是否出现与业务相关的真实操作、移动或交互；"
            "dominant_subject 只能填 equipment/environment/person/hands/fingers/object/other；"
            "hand_closeup_ratio 为手或手指特写占整个视频时长的比例 0 到 1。"
            "不要因为单帧短暂出现设备就判定主体已出现。"
        )
    if expect_speech and require_lip_sync:
        prompt += (
            "\n口型证据必须覆盖整段视频后再判断。overall_alignment、alignment 和 "
            "confidence 范围为 0 到 1；offset_ms 为嘴部动作相对语音的有符号毫秒偏差。"
            "segments 只列出可明确定位的异常时间段；无法确认时 confidence 必须低于 "
            "0.65，不得猜测异常区间。"
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
        try:
            response = cli.post(
                f"{ARK_BASE}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
            )
        except httpx.TransportError as exc:
            raise _transport_error(exc, stage="qa_submit") from exc
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
        require_lip_sync=require_lip_sync,
        expected_text=expected_text,
        expected_visual=expected_visual,
        forbid_people=forbid_people,
        forbid_readable_text=forbid_readable_text,
        require_broll_semantics=require_broll_semantics,
    )
