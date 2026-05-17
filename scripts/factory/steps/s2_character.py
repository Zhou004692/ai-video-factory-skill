"""s2: 为每个角色生成定妆照(Seedream)+ 上传 TOS 拿 URL。

输出:
- 本地 data/assets/<job>/characters/<char_id>.png
- TOS URL 回写到 jobs.characters_json[i].image_url
"""
import asyncio
import json

from .. import ASSETS_DIR
from ..db import conn
from ..clients import seedance, tos


async def _one(job_id: str, char: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        out_path = ASSETS_DIR / job_id / "characters" / f"{char['id']}.png"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img = await seedance.text_to_image(char["image_prompt"], resolution="1440x2560")
        out_path.write_bytes(img)

        # 上传 TOS
        url = tos.upload(out_path, key_hint=f"{job_id}-char-{char['id']}")
        return {**char, "image_path": str(out_path), "image_url": url}


async def run(job_id: str) -> None:
    db = conn()
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    chars = json.loads(job["characters_json"])

    sem = asyncio.Semaphore(2)
    enriched = await asyncio.gather(*[_one(job_id, c, sem) for c in chars])

    # 回写 characters_json 含 image_path/image_url
    # 同时把"主角(第一个)"图存到 jobs.character_img,兼容旧代码
    db.execute(
        "UPDATE jobs SET characters_json=?, character_img=?, "
        "cost_cents=cost_cents+? WHERE job_id=?",
        (json.dumps(enriched, ensure_ascii=False),
         enriched[0]["image_path"],
         10 * len(enriched),  # ~¥0.1 / 张
         job_id)
    )
    db.commit()
