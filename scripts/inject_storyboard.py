"""把一份手工 storyboard.json 注入 SQLite,把 job state 直接拨到 STORYBOARDED,
跳过 s1 LLM 生成步骤。

用法:
    python scripts/inject_storyboard.py <storyboard.json> <job_id> <title> [track] [style]

例:
    python scripts/inject_storyboard.py templates/crunch_inject.json J-CRUNCH01 \\
        "48小时项目攻关与清晨交付" 职场剧 B
"""
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

SKILL_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(SKILL_ROOT / ".env")
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from factory import ASSETS_DIR  # noqa: E402
from factory.db import conn  # noqa: E402


def main() -> None:
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    storyboard_path = Path(sys.argv[1])
    job_id = sys.argv[2]
    title = sys.argv[3]
    track = sys.argv[4] if len(sys.argv) > 4 else "通用"
    style = sys.argv[5] if len(sys.argv) > 5 else "D"

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
    for f in ("characters", "narrator", "style_prefix", "shots"):
        if f not in sb:
            raise SystemExit(f"storyboard 缺字段: {f}")
    if not (5 <= len(sb["shots"]) <= 12):
        raise SystemExit(f"shots 数 {len(sb['shots'])} 不在 5-12 范围")
    n_broll = sum(1 for s in sb["shots"] if s.get("type") == "broll")
    if n_broll > 1:
        raise SystemExit(f"broll 镜头 {n_broll} 个,上限 1 个")

    chars = []
    char_ids = set()
    for c in sb["characters"]:
        chars.append({
            "id": c["id"],
            "name": c["name"],
            "gender": c["gender"],
            "voice_id": c["voice_id"],
            "look": c.get("look") or c.get("image_prompt", ""),
        })
        char_ids.add(c["id"])

    topic_id = job_id.removeprefix("J-") if job_id.startswith("J-") else job_id

    db = conn()
    # 幂等:已存在则重写
    db.execute(
        "INSERT OR REPLACE INTO jobs (job_id, topic_id, title, track, style, "
        "target_audience, priority, state, storyboard, characters_json, cost_cents) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,0)",
        (job_id, topic_id, title, track, style, "通用", 5,
         "STORYBOARDED",
         json.dumps(sb, ensure_ascii=False),
         json.dumps(chars, ensure_ascii=False))
    )
    db.execute("DELETE FROM shots WHERE job_id=?", (job_id,))

    for shot in sb["shots"]:
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
    print(f"    shots           = {len(sb['shots'])} "
          f"(dialogue {len(sb['shots']) - n_broll} + broll {n_broll})")
    print(f"    storyboard.json = {out_dir / 'storyboard.json'}")
    print(f"")
    print(f"next:")
    print(f"  python scripts/run_step.py {job_id} s2_character   "
          f"# 生成定妆照+in-scene 图  (~¥1.2)")
    print(f"  python scripts/run_step.py {job_id} s4_voice       "
          f"# 生成 8 段 TTS mp3  (~¥0.05)")


if __name__ == "__main__":
    main()
