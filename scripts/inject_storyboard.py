"""把一份手工 storyboard.json 注入 SQLite,把 job state 直接拨到 STORYBOARDED,
跳过 s1 LLM 生成步骤。

用法:
    python scripts/inject_storyboard.py <storyboard.json> <job_id> <title> [track]

例:
    python scripts/inject_storyboard.py templates/crunch_inject.json J-CRUNCH01 \\
        "48小时项目攻关与清晨交付" 职场剧
"""
import json
import sys
from pathlib import Path

# Windows console GBK 编不出 ¥ 等字符,强制 UTF-8 stdout
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

SKILL_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(SKILL_ROOT / ".env")
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from factory import ASSETS_DIR  # noqa: E402
from factory.db import conn  # noqa: E402


def _generation_units(sb: dict) -> list[dict]:
    if sb.get("production_mode") == "native_master_local_director":
        return sb.get("masters") or []
    return sb.get("shots") or []


def _runtime_character(character: dict) -> dict:
    """Keep identity-routing metadata when materializing characters_json."""
    runtime = {
        "id": character["id"],
        "name": character["name"],
        "gender": character.get("gender", "unspecified"),
        "look": character.get("look") or character.get("image_prompt", ""),
    }
    for field in (
        "script_character_name",
        "generation_mode",
        "identity_type",
        "asset_character_id",
        "asset_id",
        "asset_uri",
        "seedance_asset_uri",
        "background_description",
        "seedance_reference_mode",
    ):
        if field in character:
            runtime[field] = character[field]
    return runtime


def _validate_storyboard(sb: dict) -> int:
    """Validate fields needed by manual injection; global style is optional."""
    for field in ("characters", "shots"):
        if field not in sb:
            raise SystemExit(f"storyboard 缺字段: {field}")
    units = _generation_units(sb)
    minimum = (
        1 if sb.get("production_mode") == "native_master_local_director"
        else 2
    )
    if not (minimum <= len(units) <= 12):
        raise SystemExit(
            f"生成单元数 {len(units)} 不在 {minimum}-12 范围"
        )
    n_broll = sum(1 for shot in units if shot.get("type") == "broll")
    if n_broll > 6:
        raise SystemExit(
            f"broll 镜头 {n_broll} 个,上限 6 个(30s 内 5s/broll × 6 = 全 broll 极限);"
            f"如确实需要 broll 主导主题(如老司机验车特写蒙太奇),"
            f"也别超过 6 个,否则没角色感观众记不住品牌"
        )
    return n_broll


def main() -> None:
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    storyboard_path = Path(sys.argv[1])
    job_id = sys.argv[2]
    title = sys.argv[3]
    track = sys.argv[4] if len(sys.argv) > 4 else "通用"

    # 读 JSON 时容错:LLM/手写经常在末尾留 stray 字符(```/XML tag/解释文字)。
    # 实战烧过 ¥15.72 因为末尾多了 `</invoke>`,cli.py 兜底走 s1 LLM 跑无关剧本。
    raw = storyboard_path.read_text(encoding="utf-8")
    last_brace = raw.rfind("}")
    if last_brace > 0 and last_brace < len(raw.rstrip()) - 1:
        print(
            f"[inject] WARN stray content after last '}}' at offset {last_brace}, "
            f"truncating. tail removed: {raw[last_brace+1:][:80]!r}",
            flush=True,
        )
        raw = raw[: last_brace + 1]
    sb = json.loads(raw)

    # 轻量校验(不走完整的 s1._validate,但要求关键字段在)
    n_broll = _validate_storyboard(sb)

    chars = []
    char_ids = set()
    for c in sb["characters"]:
        chars.append(_runtime_character(c))
        char_ids.add(c["id"])

    topic_id = job_id.removeprefix("J-") if job_id.startswith("J-") else job_id

    db = conn()
    # 幂等:已存在则重写
    db.execute(
        "INSERT OR REPLACE INTO jobs (job_id, topic_id, title, track, style, "
        "target_audience, priority, state, storyboard, characters_json, cost_cents) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,0)",
        (job_id, topic_id, title, track, None, "通用", 5,
         "STORYBOARDED",
         json.dumps(sb, ensure_ascii=False),
         json.dumps(chars, ensure_ascii=False))
    )
    db.execute("DELETE FROM shots WHERE job_id=?", (job_id,))

    generation_units = _generation_units(sb)
    for shot in generation_units:
        if shot["type"] == "dialogue":
            shot_prompt = f"{shot.get('scene','')}\n{shot.get('action','')}"
            if shot.get("speaker") not in char_ids:
                raise SystemExit(
                    f"shot {shot['idx']} speaker {shot.get('speaker')!r} "
                    f"not in characters {char_ids}"
                )
        else:
            shot_prompt = shot.get("prompt", "")

        db.execute(
            "INSERT INTO shots (job_id, idx, duration_sec, shot_type, camera, "
            "prompt, voiceover, subtitle, state, narrative_type, speaker_id, "
            "anchor_character) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id, shot["idx"],
                float(shot.get("duration", 6)),
                shot.get("shot_type", ""),
                shot.get("camera", ""),
                shot_prompt,
                shot.get("voiceover", ""),
                shot.get("subtitle", shot.get("voiceover", "")),
                "PENDING",
                shot["type"],
                shot.get("speaker"),
                shot.get("anchor_character"),
            )
        )

    # 落盘 storyboard.json 到 data/assets/<job_id>/
    out_dir = ASSETS_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "storyboard.json").write_text(
        json.dumps(sb, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    db.commit()
    print(f"OK  injected {job_id}")
    print(f"    state           = STORYBOARDED (跳过 s1)")
    print(f"    characters      = {len(chars)}  {[c['name'] for c in chars]}")
    print(f"    generation units= {len(generation_units)} "
          f"(dialogue {len(generation_units) - n_broll} + broll {n_broll})")
    print(f"    storyboard.json = {out_dir / 'storyboard.json'}")
    print(f"")
    print(f"next:")
    print(f"  python scripts/run_step.py {job_id} s3_shots       "
          f"# Seedance 2.5 原生生成画面、语音与口型")


if __name__ == "__main__":
    main()
