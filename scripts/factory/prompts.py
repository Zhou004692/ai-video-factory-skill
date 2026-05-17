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


def storyboard_prompt(title: str, track: str, style_code: str, duration: int,
                      target_audience: str = "通用") -> str:
    tmpl = load_prompt("storyboard")
    style_desc = style_prefix(style_code)
    return (tmpl
            .replace("{{title}}", title)
            .replace("{{track}}", track)
            .replace("{{style_desc}}", style_desc)
            .replace("{{duration}}", str(duration))
            .replace("{{target_audience}}", target_audience))
