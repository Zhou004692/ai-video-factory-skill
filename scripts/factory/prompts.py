"""Prompt 模板加载器。模板本身放在 references/ 下,这里只负责拼装。"""
from pathlib import Path
import yaml
from . import REFERENCES_DIR


def load_prompt(name: str) -> str:
    """读 references/prompts/<name>.md 全文。"""
    p = REFERENCES_DIR / "prompts" / f"{name}.md"
    return p.read_text(encoding="utf-8")


def load_topic_cases() -> str:
    """读 references/prompts/topic_cases.md 全文(v2.4 题材库 case story 库)。"""
    p = REFERENCES_DIR / "prompts" / "topic_cases.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return "(topic_cases.md 不存在,跳过)"


def character_roster() -> dict:
    """Return reusable character metadata from the canonical roster."""
    p = REFERENCES_DIR / "characters" / "roster.yaml"
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def character_roster_summary() -> str:
    """读 references/characters/roster.yaml 转 markdown 摘要给 LLM。

    保留:角色 id / name / look / typical_scenarios / catchphrases。
    声线由 Seedance 2.5 根据人物设定和逐镜 voice_directive 原生生成。
    """
    data = character_roster()
    if not data:
        return "(roster.yaml 不存在,跳过)"
    lines = [
        "# 角色花名册(优先复用,建立 IP)",
        "",
        "**优先匹配 typical_scenarios 选 id**;选了 id 后 look/name 必须照搬,不要现编。",
        "",
    ]
    for char_id, meta in data.items():
        if not isinstance(meta, dict):
            continue
        lines.append(f"## `{char_id}` — {meta.get('name', '')}")
        lines.append("")
        lines.append(f"- gender: {meta.get('gender', '')}")
        scenarios = meta.get("typical_scenarios", [])
        if scenarios:
            lines.append(f"- 适用场景: {', '.join(scenarios)}")
        phrases = meta.get("catchphrases", [])
        if phrases:
            lines.append(f"- 口头禅(voiceover 可嵌入): "
                         f"{' / '.join(repr(p) for p in phrases)}")
        if meta.get("look"):
            lines.append(f"- look: {meta['look']}")
        if meta.get("reference_image"):
            lines.append(f"- reference_image: {meta['reference_image']}")
        lines.append("")
    return "\n".join(lines)


def storyboard_prompt(title: str, track: str, duration: int = 30,
                      target_audience: str = "通用",
                      source_context: str = "") -> str:
    tmpl = load_prompt("storyboard")
    base = (tmpl
            .replace("{{title}}", title)
            .replace("{{track}}", track)
            .replace("{{duration}}", str(duration))
            .replace("{{target_audience}}", target_audience))
    # v2.4:把 case story 库 + 角色花名册作为附录注入 prompt
    # storyboard.md 里写的"参考 topic_cases.md/roster.yaml"才真的可用
    prompt = (
        base
        + "\n\n---\n\n# 附录 1:Case Story 库(v2.4)\n\n"
        + load_topic_cases()
        + "\n\n---\n\n# 附录 2:角色花名册(v2.4)\n\n"
        + character_roster_summary()
    )
    if source_context.strip():
        prompt += "\n\n---\n\n# 附录 3:本选题来源素材\n\n" + source_context
    return prompt
