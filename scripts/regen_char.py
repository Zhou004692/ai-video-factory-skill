"""重跑指定角色的定妆照 + 该角色的所有 in-scene 图,不触碰其他角色。

用法:
    python scripts/regen_char.py <job_id> <char_id>

例:
    python scripts/regen_char.py J-CRUNCH01 xiaotao
"""
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

SKILL_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(SKILL_ROOT / ".env")
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from factory import ASSETS_DIR          # noqa: E402
from factory.db import conn             # noqa: E402
from factory.clients import seedance, tos  # noqa: E402
from factory.steps.s2_character import _seed_for, _build_prompt  # noqa: E402


async def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)

    job_id = sys.argv[1]
    char_id = sys.argv[2]

    db = conn()
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not job:
        raise SystemExit(f"job not found: {job_id}")

    sb = json.loads(job["storyboard"])
    chars = {c["id"]: c for c in json.loads(job["characters_json"])}
    if char_id not in chars:
        raise SystemExit(f"char_id '{char_id}' not in job characters: {list(chars)}")

    character = chars[char_id]
    seed = _seed_for(char_id)
    sem = asyncio.Semaphore(2)

    # 1. 定妆照
    ref_prompt = (
        f"{character['look']}\n"
        f"竖屏 9:16 半身像,简洁纯色背景,主体清晰居中,自然光"
    )
    print(f"[ref] 生成 {char_id} 定妆照 ...")
    async with sem:
        img = await seedance.text_to_image(ref_prompt, resolution="1440x2560", seed=seed)
    ref_path = ASSETS_DIR / job_id / "characters" / f"{char_id}.png"
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_bytes(img)
    ref_url = tos.upload(ref_path, key_hint=f"{job_id}-char-{char_id}")
    print(f"[ref] 完成 → {ref_path.name}  url={ref_url[:60]}...")

    # 更新 characters_json 中该角色的 image_path/image_url
    enriched = []
    for c in json.loads(job["characters_json"]):
        if c["id"] == char_id:
            c = {**c, "image_path": str(ref_path), "image_url": ref_url}
        enriched.append(c)
    db.execute(
        "UPDATE jobs SET characters_json=?, cost_cents=cost_cents+10 WHERE job_id=?",
        (json.dumps(enriched, ensure_ascii=False), job_id)
    )
    db.commit()

    # 2. 该角色的所有 in-scene 图
    shots = db.execute(
        "SELECT * FROM shots WHERE job_id=? AND speaker_id=? AND narrative_type='dialogue' "
        "ORDER BY idx",
        (job_id, char_id)
    ).fetchall()

    sb_shots = {s["idx"]: s for s in sb["shots"]}

    async def regen_scene(row):
        shot = sb_shots[row["idx"]]
        prompt = _build_prompt(character, shot)
        async with sem:
            img = await seedance.text_to_image(prompt, resolution="1440x2560", seed=seed)
        out_path = ASSETS_DIR / job_id / "scenes" / f"{row['idx']:02d}_{char_id}.png"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(img)
        url = tos.upload(out_path, key_hint=f"{job_id}-scene-{row['idx']:02d}-{char_id}")
        db2 = conn()
        db2.execute(
            "UPDATE shots SET character_in_scene_path=?, character_in_scene_url=? "
            "WHERE job_id=? AND idx=?",
            (str(out_path), url, job_id, row["idx"])
        )
        db2.commit()
        print(f"[scene {row['idx']:02d}] 完成 → {out_path.name}")

    print(f"[scene] 重生成 {char_id} 的 {len(shots)} 张 in-scene 图 ...")
    await asyncio.gather(*[regen_scene(r) for r in shots])

    # 更新总 cost 估算
    db.execute(
        "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id=?",
        (10 * len(shots), job_id)
    )
    db.commit()

    row = db.execute("SELECT cost_cents FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    total = len(shots) + 1
    print(f"\n完成: 重生成 {total} 张图 (~CNY{total*0.10:.2f})")
    print(f"累计花费: CNY{row['cost_cents']/100:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
