"""s2: 生成主角定妆照(用 Seedance/Seedream 文生图)。"""
import json

from .. import ASSETS_DIR
from ..db import conn
from ..clients import seedance


async def run(job_id: str) -> None:
    db = conn()
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    sb = json.loads(job["storyboard"])

    char_prompt = sb["character"]["image_prompt"] if isinstance(sb["character"], dict) \
                  else sb["character"]

    out_dir = ASSETS_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "character.png"

    # Seedream 5.0 要求 >= 3686400 像素;1440x2560 = 3.69M,9:16 适配竖屏
    img_bytes = await seedance.text_to_image(char_prompt, resolution="1440x2560")
    out_path.write_bytes(img_bytes)

    db.execute(
        "UPDATE jobs SET character_img=?, cost_cents=cost_cents+? WHERE job_id=?",
        (str(out_path), 10, job_id)   # 一张图大约 ¥0.1
    )
    db.commit()
