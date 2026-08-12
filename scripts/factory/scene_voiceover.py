"""Pure helpers shared by the faceless scene + voiceover pipeline."""
import re


_UNSAFE_HUMAN_TERMS = (
    "老周",
    "王工",
    "小沈",
    "小美",
    "小白",
    "说话者",
    "角色",
    "人物",
    "真人",
    "肖像",
    "人像",
    "面部",
    "人脸",
    "正脸",
    "侧脸",
    "脸",
    "嘴部",
    "嘴型",
    "口型",
    "开口",
    "说话",
    "对镜头",
    "看镜头",
)


def faceless_visual_text(value: object, *extra_terms: object) -> str:
    """Keep only environment/object clauses from a legacy visual field.

    Older storyboards sometimes put the speaker and face direction inside
    ``scene`` or ``camera``.  Negative prompts alone are not reliable enough:
    remove those clauses before the request reaches an image/video provider.
    """
    if not value:
        return ""
    blocked = _UNSAFE_HUMAN_TERMS + tuple(
        str(term) for term in extra_terms if term
    )
    clauses = re.split(r"[，,。；;\n]+", str(value))
    safe = [
        clause.strip()
        for clause in clauses
        if clause.strip() and not any(term in clause for term in blocked)
    ]
    return "，".join(safe)


def non_text_visual_text(value: object) -> str:
    """Reframe identifier-bearing props so the model need not draw glyphs."""
    text = str(value or "")
    replacements = (
        (r"车架号(?:钢印)?", "车架结构与打磨痕迹"),
        (r"(?:设备)?编号(?:贴纸|标签)?", "无字标签背面"),
        (r"(?:金属)?铭牌", "金属安装位"),
        (r"贴纸", "无字材料边角"),
        (r"钢印", "金属压痕"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    return text
