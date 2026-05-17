"""手工注入 TESTI:'B2B 销售追跑挽留 + 反转跳舞' 剧本,跳过 LLM,直接从 s2 开始跑。
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


JOB_ID = "J-TESTI"
TOPIC_ID = "TESTI"
TITLE = "B2B 销售追跑挽留,客户一句年后再说"
TRACK = "工程设备"
STYLE = "F"
TARGET = "中小工程公司老板"


STORYBOARD = {
    "reversal_research": [
        "事实 1:销售为大客户跑断腿垫资催款回款经典惨案",
        "事实 2:客户用'年后再说'冷淡敷衍 5 字斩断所有付出"
    ],
    "reversal_picked": "事实 2 - 5 字冷漠回应让所有铺垫一刀斩断",
    "characters": [
        {
            "id": "merchant",
            "name": "商户销售",
            "gender": "male",
            "voice_id": "zh_male_dayi_uranus_bigtts",
            "look": "30 岁亚洲男性,棕色短发,圆脸,深色工装夹克 + 蓝色牛仔裤 + 白色 T 恤,眼神焦急但真诚,微红眼圈,带工地灰尘感"
        },
        {
            "id": "client",
            "name": "王总(大客户)",
            "gender": "male",
            "voice_id": "zh_male_gaolengchenwen_uranus_bigtts",
            "look": "50 岁亚洲男性,稀疏黑发往后梳,方脸,深灰色商务西装 + 白衬衫无领带,眼神冷淡,微皱眉,典型大老板气场"
        }
    ],
    "narrator": {
        "voice_id": "zh_male_cixingjieshuonan_uranus_bigtts"
    },
    "style_prefix": "工地停车场黄金时刻晨雾弥漫,深蓝冷调,机械金属质感,35mm 胶片颗粒,自然光为主,纪实纪录片风格,镜头硬朗有重量感",
    "hook": "B2B 销售的精神状态",
    "bgm": {
        "style": "前 7 镜悲情 ballad,镜头 8 切换洗脑神曲",
        "mood": "焦灼到自嘲",
        "bpm_range": [70, 130]
    },
    "shots": [
        {
            "idx": 1, "type": "dialogue", "speaker": "merchant",
            "duration": 4, "shot_type": "中景", "camera": "跟随",
            "scene": "工地停车场,远处是黑色奔驰商务车驾驶位车门关上,商户挥舞着一份设备租赁合同奔跑",
            "action": "向前奔跑,挥舞合同,神情急切,呼吸急促,脚下扬起灰尘",
            "voiceover": "王总!王总!您等会儿!您先别走啊!",
            "subtitle": "王总!王总!等等!",
            "anchor_character": None,
        },
        {
            "idx": 2, "type": "dialogue", "speaker": "merchant",
            "duration": 5, "shot_type": "中景", "camera": "固定",
            "scene": "停车场,商户挡在黑色商务车车头前,双手撑住车头,身后是模糊的工地脚手架远景",
            "action": "挡在车前,双手撑车头,微微弯腰,气喘吁吁,眼神恳切",
            "voiceover": "咱不是说好的吗?新疆那项目,回款只对我一个人对接啊!",
            "subtitle": "咱不是说好的吗?",
            "anchor_character": None,
        },
        {
            "idx": 3, "type": "dialogue", "speaker": "merchant",
            "duration": 9, "shot_type": "中景", "camera": "缓推",
            "scene": "停车场,商户站在驾驶位车窗外,手里举着一沓单据和合同,身后停车场有几台高空车",
            "action": "情绪激动,挥动手中单据,一个一个数手指,声音越说越大",
            "voiceover": "为这个单子!乌鲁木齐、库尔勒、阿克苏,三个城市我亲自飞!垫了整整八万运输费!催款电话打了八个您一次都没接全啊!",
            "subtitle": "三城市跑断腿,垫 8 万,8 个电话",
            "anchor_character": None,
        },
        {
            "idx": 4, "type": "dialogue", "speaker": "merchant",
            "duration": 6, "shot_type": "近景", "camera": "固定",
            "scene": "黑色商务车驾驶位车窗外,商户把头凑近车窗玻璃,玻璃上反射着停车场的天光",
            "action": "凑近车窗,声音突然压低,眼眶微红,神情卑微而真诚",
            "voiceover": "王总,是我服务不到位吗?还是我哪句话说错了?还是我...不够真诚啊?",
            "subtitle": "是我...不够真诚吗?",
            "anchor_character": None,
        },
        {
            "idx": 5, "type": "dialogue", "speaker": "merchant",
            "duration": 6, "shot_type": "中景", "camera": "固定",
            "scene": "车窗外,商户双手递出一份新合同,工地远景中工人正在作业",
            "action": "双手递出合同,身体前倾,脸上挤出充满希望的笑容,眼神发亮",
            "voiceover": "您今天回款,以后您所有项目,我最低价、最优先!没设备我从别家给您借来都行!",
            "subtitle": "最低价!最优先!",
            "anchor_character": None,
        },
        {
            "idx": 6, "type": "dialogue", "speaker": "client",
            "duration": 4, "shot_type": "近景", "camera": "固定",
            "scene": "黑色商务车内驾驶位,王总坐在真皮座椅上,手放方向盘,只用余光扫过车窗外的合同",
            "action": "眼皮没抬,语气平淡,缓缓启动车辆,关闭车窗的动作开始",
            "voiceover": "嗯......年后再说吧。",
            "subtitle": "年后再说吧。",
            "anchor_character": None,
        },
        {
            "idx": 7, "type": "broll",
            "duration": 4, "shot_type": "远景", "camera": "固定",
            "scene": "工地停车场全景,黑色商务车驶离扬起黄沙,商户独自站在原地的背影,合同还半举着,远处工地塔吊缓缓转动",
            "action": "",
            "prompt": "Wide shot of construction site parking lot at dawn, a black business sedan driving away kicking up dust, a lone figure standing in the foreground holding a contract, distant cranes silhouettes, 35mm film grain, deep blue cold tone, documentary feel, 9:16 portrait",
            "voiceover": "车窗关上那一刻,八万运输费的回响,只剩尾气。",
            "subtitle": "",
            "anchor_character": None,
        },
        {
            "idx": 8, "type": "dialogue", "speaker": "merchant",
            "duration": 6, "shot_type": "中景", "camera": "固定",
            "scene": "工地停车场,商户站在原地,合同已经垂在身侧,他突然露出一个诡异的释然表情,身体微微开始扭动,准备跳一段奇怪的舞,背景音乐切换",
            "action": "脸上挤出怪诞的笑容,身体开始有节奏地晃动,手指轻弹,准备跳舞自嘲",
            "voiceover": "嗯......挺好挺好,过年了过年了,继续上班吧,继续上班吧。",
            "subtitle": "B2B 销售的精神状态",
            "anchor_character": None,
        },
    ],
    "ending_hook": "你今年的回款都收回来了吗?评论区聊聊。",
    "warnings": [
        "镜头 8 OmniHuman 做不出真实跳舞,只能做基础站立+表情释然+字幕承担反差感"
    ],
}


def inject():
    db = conn()

    # 1. 写 storyboard.json 到 assets
    assets = ASSETS_DIR / JOB_ID
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "storyboard.json").write_text(
        json.dumps(STORYBOARD, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # 2. characters_json(s2 会回写 image_path/image_url)
    chars_json = [
        {"id": c["id"], "name": c["name"], "gender": c["gender"],
         "voice_id": c["voice_id"], "look": c["look"]}
        for c in STORYBOARD["characters"]
    ]

    # 3. 插入 job
    db.execute(
        "INSERT OR REPLACE INTO jobs(job_id, topic_id, title, track, style, "
        "target_audience, priority, state, storyboard, characters_json, "
        "cost_cents) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (JOB_ID, TOPIC_ID, TITLE, TRACK, STYLE, TARGET, 5,
         "STORYBOARDED",
         json.dumps(STORYBOARD, ensure_ascii=False),
         json.dumps(chars_json, ensure_ascii=False),
         0)
    )

    # 4. 清旧 shots 然后重插
    db.execute("DELETE FROM shots WHERE job_id=?", (JOB_ID,))
    for shot in STORYBOARD["shots"]:
        if shot["type"] == "dialogue":
            shot_prompt = f"{shot.get('scene','')}\n{shot.get('action','')}"
        else:
            shot_prompt = shot.get("prompt", "")
        db.execute(
            "INSERT INTO shots(job_id, idx, duration_sec, shot_type, "
            "camera, prompt, voiceover, subtitle, narrative_type, speaker_id, "
            "anchor_character) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                JOB_ID, shot["idx"],
                float(shot.get("duration", 6)),
                shot.get("shot_type", ""),
                shot.get("camera", ""),
                shot_prompt,
                shot.get("voiceover", ""),
                shot.get("subtitle", shot.get("voiceover", "")),
                shot["type"],
                shot.get("speaker"),
                shot.get("anchor_character"),
            )
        )
    db.commit()
    print(f"Injected {JOB_ID}: state=STORYBOARDED, {len(STORYBOARD['shots'])} shots")


if __name__ == "__main__":
    inject()
