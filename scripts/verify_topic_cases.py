"""验证 v2.4 题材库 case story 库 + 角色花名册的 LLM 行为。

只跑 s1 storyboard 生成,不跑后续 s2-s7,~¥0.1 测试。

判定 v2.4 是否真生效:
  1. reversal_picked 写 case 引用编号(如 "A#3 + B#5, case A1-3")
  2. reversal_research 含 case 里的真实数字(押金 9000 / 扣 4600 / 退 4400)
  3. voiceover 含金句库改写(如"放大镜验车""定义权全在他手上")
  4. characters 用 roster 角色 id(boss_laozhou / engineer_wanggong 等)
  5. 切镜节奏:单镜 ≤6s,至少 6 个镜头(v2.4 强约束)

跑法: py scripts/verify_topic_cases.py
输出: data/verify_topic_cases.json + 控制台 5 项验证报告
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

# 直击 case A1-3(押金五连扣)的主题,看 LLM 能否抽 case 真实数字
TITLE = "押金 9000 退我 4400，验车扣的比租金还狠"


def _check(sb: dict) -> list[tuple[str, bool, str]]:
    """5 项验证,每项返回 (项名, 通过?, 证据)。"""
    res = []

    # 1. reversal_picked 含 case 引用
    rp = sb.get("reversal_picked", "")
    has_case_ref = ("case " in rp.lower()) or ("A1" in rp) or ("a1-" in rp.lower())
    res.append(("1. reversal_picked 含 case 引用编号",
                has_case_ref, rp[:120]))

    # 2. reversal_research 含 case 真实数字(9000/4400/4600/800/1200/1500 任一)
    research = " ".join(sb.get("reversal_research", []))
    real_numbers = ["9000", "4400", "4600", "1200", "1500"]
    hits = [n for n in real_numbers if n in research]
    res.append((f"2. reversal_research 含 case 真实数字",
                len(hits) >= 2, f"命中 {hits}"))

    # 3. voiceover 含金句库改写
    dialogue_shots = [s for s in sb.get("shots", [])
                      if s.get("type") == "dialogue"]
    voices = " ".join(s.get("voiceover", "") for s in dialogue_shots)
    catchphrases = ["放大镜", "退租", "正常磨损", "白纸黑字", "扣得只剩",
                    "比租金还狠", "定义权"]
    cp_hits = [c for c in catchphrases if c in voices]
    res.append((f"3. voiceover 含金句库改写",
                len(cp_hits) >= 1, f"命中 {cp_hits}"))

    # 4. characters 用 roster id(boss_laozhou / engineer_wanggong / 等)
    roster_ids = {"boss_laozhou", "engineer_wanggong", "expert_laofashi",
                  "sales_xiaomei", "rookie_xiaobai"}
    char_ids = {c.get("id") for c in sb.get("characters", [])}
    roster_used = char_ids & roster_ids
    res.append((f"4. characters 用 roster 角色 id",
                bool(roster_used), f"用了 {roster_used or '全自编'}"))

    # 5. 切镜节奏:至少 5 个 shot,单 dialogue 镜 ≤6s(v2.4 强约束)
    shots = sb.get("shots", [])
    long_shots = [s for s in shots if s.get("type") == "dialogue"
                  and s.get("duration", 0) > 6]
    enough_cuts = len(shots) >= 5
    pace_ok = len(long_shots) == 0
    res.append((f"5. 切镜节奏(≥5 镜 + dialogue ≤6s)",
                enough_cuts and pace_ok,
                f"{len(shots)} 镜,超长 dialogue {len(long_shots)}"))

    return res


def main() -> None:
    prompt = storyboard_prompt(
        title=TITLE, track="工程设备",
        duration=30, target_audience="中小工程公司老板",
    )
    print(f"[v2.4-verify] 主题: {TITLE}", flush=True)
    print(f"[v2.4-verify] 跑 doubao 2.0 pro s1 storyboard 生成...", flush=True)
    sb, usage = doubao_llm.complete_json(prompt, max_tokens=6000)
    (DATA_DIR / "verify_topic_cases.json").write_text(
        json.dumps(sb, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[v2.4-verify] tokens in={usage['input_tokens']} "
          f"out={usage['output_tokens']}", flush=True)

    print("\n" + "=" * 60)
    print("v2.4 5 项验证报告")
    print("=" * 60)
    results = _check(sb)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, evidence in results:
        mark = "✅" if ok else "❌"
        print(f"{mark} {name}")
        print(f"   证据: {evidence}")
    print("")
    print(f"通过 {passed}/{len(results)}")


if __name__ == "__main__":
    main()
