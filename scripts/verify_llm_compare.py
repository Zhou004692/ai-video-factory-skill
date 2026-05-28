"""s1 LLM 脚本质量对比:doubao-1-5-pro-32k vs doubao-seed-2-0-pro-260215。

同一主题分别用两个 LLM 生成 storyboard,对比创意/反转/render_mode 分配质量。
预算: 2 次 LLM 调用 ≈ ¥0.2

跑法: py scripts/verify_llm_compare.py
输出: data/llm_compare_1.5.json + data/llm_compare_2.0.json + 控制台摘要
"""
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

SKILL_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(SKILL_ROOT / ".env")
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from factory import DATA_DIR  # noqa: E402
from factory.prompts import storyboard_prompt  # noqa: E402
from factory.clients import doubao_llm  # noqa: E402

MODELS = {
    "1.5": "doubao-1-5-pro-32k-250115",
    "2.0": "doubao-seed-2-0-pro-260215",
}

TITLE = "合同里这3个字让你赔10万，90%的人没看到"


def _summary(tag: str, sb: dict, usage: dict) -> str:
    shots = sb.get("shots", [])
    dia = [s for s in shots if s.get("type") == "dialogue"]
    dh = [s for s in dia if (s.get("render_mode") or "digital_human") == "digital_human"]
    scene = [s for s in dia if s.get("render_mode") == "scene"]
    broll = [s for s in shots if s.get("type") == "broll"]
    has_vd = sum(1 for s in dia if (s.get("voice_directive") or "").strip())
    lines = [
        f"=== {tag} ({MODELS[tag]}) ===",
        f"  tokens: in={usage.get('input_tokens')} out={usage.get('output_tokens')}",
        f"  shots: {len(shots)} (dialogue {len(dia)} / broll {len(broll)})",
        f"  render_mode: digital_human {len(dh)} / scene {len(scene)}  "
        f"(混合路线 B 是否被采纳)",
        f"  voice_directive 覆盖: {has_vd}/{len(dia)}",
        f"  reversal_picked: {sb.get('reversal_picked', '')[:80]}",
        f"  hook: {sb.get('hook', '')}",
        f"  shot1 voiceover: {(dia[0].get('voiceover') if dia else '')[:60]}",
    ]
    return "\n".join(lines)


def main() -> None:
    prompt = storyboard_prompt(
        title=TITLE, track="工程设备", style_code="F",
        duration=30, target_audience="中小工程公司老板",
    )
    out_dir = DATA_DIR
    results = {}
    for tag, model in MODELS.items():
        print(f"[llm-cmp] generating with {tag} ({model})...", flush=True)
        try:
            sb, usage = doubao_llm.complete_json(prompt, max_tokens=6000, model=model)
            (out_dir / f"llm_compare_{tag}.json").write_text(
                json.dumps(sb, ensure_ascii=False, indent=2), encoding="utf-8")
            results[tag] = _summary(tag, sb, usage)
        except Exception as e:
            results[tag] = f"=== {tag} ({model}) ===\n  FAILED: {type(e).__name__}: {str(e)[:300]}"

    print("\n" + "=" * 60)
    for tag in MODELS:
        print(results[tag])
        print()


if __name__ == "__main__":
    main()
