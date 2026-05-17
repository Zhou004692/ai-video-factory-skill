"""注入 TESTI v2:B2B 销售追跑挽留剧本 + 每镜 emotion / voice_directive / dramatic_keywords 强化。
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


JOB_ID = "J-TESTI2"
TOPIC_ID = "TESTI2"
TITLE = "B2B 销售追跑挽留客户冷漠(情绪强化版)"


STORYBOARD = {
    "reversal_research": [
        "销售为大客户跑断腿垫资催款回款经典惨案",
        "客户用'年后再说'5 字斩断所有付出"
    ],
    "reversal_picked": "客户冷漠回应 + 销售自嘲跳舞收尾",
    "characters": [
        {
            "id": "merchant",
            "name": "商户销售",
            "gender": "male",
            "voice_id": "zh_male_dayi_uranus_bigtts",
            "look": "30 岁亚洲男性,棕色短发凌乱,圆脸,深色工装夹克 + 蓝色牛仔裤 + 白色 T 恤,眼神焦急绝望,微红眼圈,带工地灰尘感"
        },
        {
            "id": "client",
            "name": "王总(大客户)",
            "gender": "male",
            "voice_id": "zh_male_gaolengchenwen_uranus_bigtts",
            "look": "50 岁亚洲男性,稀疏黑发往后梳,方脸,深灰色商务西装 + 白衬衫无领带,眼神冷漠淡定,微皱眉,典型大老板气场"
        }
    ],
    "narrator": {"voice_id": "zh_male_cixingjieshuonan_uranus_bigtts"},
    "style_prefix": "工地停车场黄金时刻晨雾弥漫,深蓝冷调,35mm 胶片颗粒,自然光为主,纪实纪录片风格,镜头硬朗有重量感",
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
            "action": "向前狂奔,挥舞合同,脸涨红呼吸急促,眼神急迫,脚下扬起灰尘",
            "voiceover": "王总!王总!您等会儿!您先别走啊!",
            "subtitle": "王总!王总!等等!",
            "anchor_character": None,
            "dramatic_keywords": "face flushed with desperation, sweat on forehead, eyes wide and pleading, mouth open in shout, arm extended forward grasping",
            "voice_directive": "极度焦急,带着喘息,嗓音略哑,像是要哭出来的呼喊",
            "emotion": "surprised", "emotion_scale": 5,
        },
        {
            "idx": 2, "type": "dialogue", "speaker": "merchant",
            "duration": 5, "shot_type": "中景", "camera": "固定",
            "scene": "停车场,商户挡在黑色商务车车头前,双手撑住车头,身后是模糊的工地脚手架远景",
            "action": "挡在车前,双手撑车头,微微弯腰,气喘吁吁,眼神恳切几乎要落泪",
            "voiceover": "咱不是说好的吗?新疆那项目,回款只对我一个人对接啊!",
            "subtitle": "咱不是说好的吗?",
            "anchor_character": None,
            "dramatic_keywords": "leaning forward into the car hood, hands pressed flat, knuckles white, eyes desperate and watering, chin trembling slightly",
            "voice_directive": "委屈又焦急,嗓音颤抖,带怨气和不甘,像在质问又像在哀求",
            "emotion": "sad", "emotion_scale": 4,
        },
        {
            "idx": 3, "type": "dialogue", "speaker": "merchant",
            "duration": 9, "shot_type": "中景", "camera": "缓推",
            "scene": "停车场,商户站在驾驶位车窗外,手里举着一沓单据和合同,身后停车场有几台高空车",
            "action": "情绪几乎崩溃,挥动手中单据,一个个数手指,声音越说越大",
            "voiceover": "为这个单子!乌鲁木齐、库尔勒、阿克苏,三个城市我亲自飞!垫了整整八万运输费!催款电话打了八个您一次都没接全啊!",
            "subtitle": "三城市跑断腿,垫 8 万,8 个电话",
            "anchor_character": None,
            "dramatic_keywords": "waving papers in the air aggressively, face contorted with frustration, mouth wide open shouting, eyes red and wet, body leaning forward in agitation",
            "voice_directive": "情绪激动几近哽咽,语速逐渐加快,数到八万的时候提高音量,带哭腔",
            "emotion": "angry", "emotion_scale": 5,
        },
        {
            "idx": 4, "type": "dialogue", "speaker": "merchant",
            "duration": 6, "shot_type": "近景", "camera": "固定",
            "scene": "黑色商务车驾驶位车窗外,商户把头凑近车窗玻璃,玻璃上反射着停车场的天光",
            "action": "凑近车窗,声音骤然压低,眼眶通红几乎落泪,神情卑微到极致",
            "voiceover": "王总,是我服务不到位吗?还是我哪句话说错了?还是我...不够真诚啊?",
            "subtitle": "是我...不够真诚吗?",
            "anchor_character": None,
            "dramatic_keywords": "face pressed near the car window, tears about to fall, eyes pleading and broken, lips trembling, head slightly bowed in submission",
            "voice_directive": "声音突然变得极度卑微低沉,带哽咽,几乎要哭出来,像是在自我怀疑的低语",
            "emotion": "sad", "emotion_scale": 5,
        },
        {
            "idx": 5, "type": "dialogue", "speaker": "merchant",
            "duration": 6, "shot_type": "中景", "camera": "固定",
            "scene": "车窗外,商户双手递出一份新合同,工地远景中工人正在作业",
            "action": "双手颤抖递出合同,身体前倾,脸上挤出充满希望的苦笑,眼神发亮",
            "voiceover": "您今天回款,以后您所有项目,我最低价、最优先!没设备我从别家给您借来都行!",
            "subtitle": "最低价!最优先!",
            "anchor_character": None,
            "dramatic_keywords": "hands extending a contract with trembling, eyes wide and hopeful, forced bright smile despite tears, body bent forward eagerly",
            "voice_directive": "突然提起精神最后一搏,声音急切带着祈求,语速加快,像孤注一掷",
            "emotion": "excited", "emotion_scale": 4,
        },
        {
            "idx": 6, "type": "dialogue", "speaker": "client",
            "duration": 4, "shot_type": "近景", "camera": "固定",
            "scene": "黑色商务车内驾驶位,王总坐在真皮座椅上,手放方向盘,只用余光扫过车窗外的合同",
            "action": "眼皮没抬,语气平淡冷漠,缓缓启动车辆,关闭车窗的动作开始",
            "voiceover": "嗯......年后再说吧。",
            "subtitle": "年后再说吧。",
            "anchor_character": None,
            "dramatic_keywords": "completely indifferent expression, eyes not even glancing at the contract, slight bored exhale, finger tapping steering wheel",
            "voice_directive": "极度冷淡,语气敷衍,声音平铺直叙没有任何情感,像在打发苍蝇",
            "emotion": "neutral", "emotion_scale": 2,
            "speech_rate": -15,
        },
        {
            "idx": 7, "type": "broll",
            "duration": 4, "shot_type": "远景", "camera": "固定",
            "scene": "工地停车场全景,黑色商务车驶离扬起黄沙,商户独自站在原地的背影,合同还半举着,远处工地塔吊缓缓转动",
            "action": "",
            "prompt": "Wide shot of construction site parking lot at dawn, a black business sedan driving away kicking up dust, a lone figure standing in the foreground holding a contract, distant cranes silhouettes, 35mm film grain, deep blue cold tone, documentary feel, 9:16 portrait, melancholic mood",
            "voiceover": "车窗关上的那一刻,八万运输费的回响,只剩尾气。",
            "subtitle": "",
            "anchor_character": None,
            "voice_directive": "纪录片旁白,低沉沉思,带一丝怅然",
            "emotion": "sad", "emotion_scale": 3,
        },
        {
            "idx": 8, "type": "dialogue", "speaker": "merchant",
            "duration": 6, "shot_type": "中景", "camera": "固定",
            "scene": "工地停车场,商户站在原地,合同已经垂在身侧,他突然露出一个诡异的释然表情,身体微微开始扭动,背景音乐切换",
            "action": "脸上挤出怪诞而扭曲的笑容,眼神空洞,身体开始有节奏地晃动",
            "voiceover": "嗯......挺好挺好,过年了过年了,继续上班吧,继续上班吧。",
            "subtitle": "B2B 销售的精神状态",
            "anchor_character": None,
            "dramatic_keywords": "weirdly hollow smile, eyes glazed and empty, head tilted with a forced cheerful expression that's clearly broken, body starting to sway awkwardly in self-mockery",
            "voice_directive": "情绪从绝望突然切换到诡异的自嘲,声音平淡却带着崩溃边缘的笑意,像是在自我安慰",
            "emotion": "happy", "emotion_scale": 2,
        },
    ],
    "ending_hook": "你今年的回款都收回来了吗?评论区聊聊。",
    "warnings": ["OmniHuman 无法生成真实大幅戏剧动作 — 已用 dramatic_keywords + emotion 推到极限"],
}


def inject():
    db = conn()
    assets = ASSETS_DIR / JOB_ID
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "storyboard.json").write_text(
        json.dumps(STORYBOARD, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    chars_json = [
        {"id": c["id"], "name": c["name"], "gender": c["gender"],
         "voice_id": c["voice_id"], "look": c["look"]}
        for c in STORYBOARD["characters"]
    ]
    db.execute(
        "INSERT OR REPLACE INTO jobs(job_id, topic_id, title, track, style, "
        "target_audience, priority, state, storyboard, characters_json, cost_cents) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (JOB_ID, TOPIC_ID, TITLE, "工程设备", "F", "中小工程公司老板", 5,
         "STORYBOARDED",
         json.dumps(STORYBOARD, ensure_ascii=False),
         json.dumps(chars_json, ensure_ascii=False),
         0)
    )
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
    print(f"Injected {JOB_ID}: {len(STORYBOARD['shots'])} shots with emotion fields")


if __name__ == "__main__":
    inject()
