"""TESTJ 二次修订:换音色 + 强化情绪指令,复用 s2 已生成的角色图。

变更:
- saleA: voice_id → zh_female_qinqienv_uranus_bigtts(亲切女)
         voice_directive 全部改成"悲观疲惫,带苦笑,声音低沉拖长"
- saleB: voice_id → zh_female_gaolengyujie_moon_bigtts(高冷御姐 moon)
         voice_directive 全部改成"自信有气场,女王腔调,带挑衅味"
"""
import json
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from dotenv import load_dotenv
load_dotenv(SKILL / ".env")

from factory.db import conn
from factory import ASSETS_DIR


JOB_ID = "J-TESTJ"

SALEA_VOICE = "zh_female_qinqienv_uranus_bigtts"
SALEB_VOICE = "zh_female_gaolengyujie_moon_bigtts"

SALEA_DIRECTIVE = "悲观疲惫,带自嘲苦笑,声音低沉拖长,像加班到崩溃的打工人"
SALEB_DIRECTIVE = "自信有气场,女王腔调,带挑衅和不可一世的玩味,语调上扬"


def update():
    db = conn()

    # 1. 读现有 storyboard + characters_json
    row = db.execute("SELECT * FROM jobs WHERE job_id=?", (JOB_ID,)).fetchone()
    if not row:
        raise RuntimeError(f"{JOB_ID} not found")
    sb = json.loads(row["storyboard"])
    chars = json.loads(row["characters_json"])

    # 2. 更新角色音色
    voice_map = {"saleA": SALEA_VOICE, "saleB": SALEB_VOICE}
    for c in chars:
        if c["id"] in voice_map:
            c["voice_id"] = voice_map[c["id"]]
    for c in sb["characters"]:
        if c["id"] in voice_map:
            c["voice_id"] = voice_map[c["id"]]

    # 3. 更新每镜 voice_directive(根据 speaker)
    for shot in sb["shots"]:
        if shot.get("speaker") == "saleA":
            shot["voice_directive"] = SALEA_DIRECTIVE
        elif shot.get("speaker") == "saleB":
            shot["voice_directive"] = SALEB_DIRECTIVE
        # 老板们不改

    # 4. 写回 jobs
    db.execute(
        "UPDATE jobs SET storyboard=?, characters_json=?, state='CHARACTER_OK', "
        "error=NULL, retries=0 WHERE job_id=?",
        (json.dumps(sb, ensure_ascii=False),
         json.dumps(chars, ensure_ascii=False),
         JOB_ID)
    )

    # 5. 重置 shots 状态(让 s4 重新跑 TTS,s3 重新跑 OmniHuman)
    db.execute(
        "UPDATE shots SET state='PENDING', audio_path=NULL, audio_url=NULL, "
        "video_path=NULL, error=NULL WHERE job_id=?",
        (JOB_ID,)
    )

    # 6. 写回 storyboard.json(覆盖)
    (ASSETS_DIR / JOB_ID / "storyboard.json").write_text(
        json.dumps(sb, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    db.commit()
    print(f"Updated {JOB_ID}:")
    print(f"  saleA voice → {SALEA_VOICE}")
    print(f"  saleB voice → {SALEB_VOICE}")
    print(f"  state reset to CHARACTER_OK,shots reset to PENDING")
    print(f"  下一步 cli.py run 将从 s4 重新跑")


if __name__ == "__main__":
    update()
