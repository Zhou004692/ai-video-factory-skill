"""Prompt 模板加载器。模板本身放在 references/ 下,这里只负责拼装。"""
from pathlib import Path
import yaml
from . import REFERENCES_DIR


def load_prompt(name: str) -> str:
    """读 references/prompts/<name>.md 全文。"""
    p = REFERENCES_DIR / "prompts" / f"{name}.md"
    return p.read_text(encoding="utf-8")


def load_styles() -> dict:
    """读 references/styles/presets.yaml。"""
    p = REFERENCES_DIR / "styles" / "presets.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def style_prefix(code: str) -> str:
    """A-E 风格代码 → 提示词前缀。"""
    styles = load_styles()
    if code not in styles:
        raise ValueError(f"unknown style: {code} (available: {list(styles)})")
    return styles[code]["prompt"]


def load_voice_catalog() -> dict:
    """读 references/voices/seedtts_v2_catalog.yaml。"""
    p = REFERENCES_DIR / "voices" / "seedtts_v2_catalog.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def voice_catalog_table() -> str:
    """把音色库格式化成 markdown 表格,塞进 prompt。"""
    catalog = load_voice_catalog()
    rows = ["| voice_id | 显示名 | 性别 | 年龄段 | 调性 | 适配角色 | 支持情感(emotion) |",
            "|---|---|---|---|---|---|---|"]
    for vid, meta in catalog.items():
        roles = ", ".join(meta.get("fit_roles", []))
        emos = ", ".join(meta.get("emotions", [])) or "—"
        rows.append(
            f"| `{vid}` | {meta['display']} | {meta['gender']} | "
            f"{meta['age']} | {meta['tone']} | {roles} | {emos} |"
        )
    return "\n".join(rows)


def load_topic_cases() -> str:
    """读 references/prompts/topic_cases.md 全文(v2.4 题材库 case story 库)。"""
    p = REFERENCES_DIR / "prompts" / "topic_cases.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return "(topic_cases.md 不存在,跳过)"


def character_roster_summary() -> str:
    """读 references/characters/roster.yaml 转 markdown 摘要给 LLM。

    保留:角色 id / name / voice_id / look / typical_scenarios / catchphrases
    省略:voice_note / brand_role / image_url 等元信息(LLM 不需要)。
    """
    p = REFERENCES_DIR / "characters" / "roster.yaml"
    if not p.exists():
        return "(roster.yaml 不存在,跳过)"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    lines = [
        "# 角色花名册(优先复用,建立 IP)",
        "",
        "**优先匹配 typical_scenarios 选 id**;选了 id 后 look/voice_id/name 必须照搬,不要现编。",
        "",
    ]
    for char_id, meta in data.items():
        if not isinstance(meta, dict):
            continue
        lines.append(f"## `{char_id}` — {meta.get('name', '')}")
        lines.append("")
        lines.append(f"- gender: {meta.get('gender', '')}")
        lines.append(f"- voice_id: `{meta.get('voice_id', '')}`")
        scenarios = meta.get("typical_scenarios", [])
        if scenarios:
            lines.append(f"- 适用场景: {', '.join(scenarios)}")
        phrases = meta.get("catchphrases", [])
        if phrases:
            lines.append(f"- 口头禅(voiceover 可嵌入): "
                         f"{' / '.join(repr(p) for p in phrases)}")
        if meta.get("look"):
            lines.append(f"- look: {meta['look']}")
        lines.append("")
    return "\n".join(lines)


def storyboard_prompt(title: str, track: str, style_code: str, duration: int,
                      target_audience: str = "通用") -> str:
    tmpl = load_prompt("storyboard")
    style_desc = style_prefix(style_code)
    base = (tmpl
            .replace("{{title}}", title)
            .replace("{{track}}", track)
            .replace("{{style_desc}}", style_desc)
            .replace("{{duration}}", str(duration))
            .replace("{{target_audience}}", target_audience)
            .replace("{{voice_catalog}}", voice_catalog_table()))
    # v2.4:把 case story 库 + 角色花名册作为附录注入 prompt
    # storyboard.md 里写的"参考 topic_cases.md/roster.yaml"才真的可用
    return (
        base
        + "\n\n---\n\n# 附录 1:Case Story 库(v2.4)\n\n"
        + load_topic_cases()
        + "\n\n---\n\n# 附录 2:角色花名册(v2.4)\n\n"
        + character_roster_summary()
    )
