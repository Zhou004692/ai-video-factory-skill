"""注入 TESTJ:双销售反差 + 双老板辩论 + 众能平台产品广告(抖音 CTA)。
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
TOPIC_ID = "TESTJ"
TITLE = "众能平台:狗都不干?我干!双女销售反差广告"


STORYBOARD = {
    "reversal_research": [
        "传统销售年后揽活靠陌拜 + 老客户跟进,效率低",
        "众能平台聚合需求 + 客户列表 + 拜访记录,把销售从外勤变内勤"
    ],
    "reversal_picked": "工具升级让'狗都不干的活'变成'专业自信高效'",
    "characters": [
        {
            "id": "saleA",
            "name": "销售A(传统)",
            "gender": "female",
            "voice_id": "zh_female_qingxinnvsheng_uranus_bigtts",
            "look": "35 岁亚洲女性,中长黑发紧紧扎成低马尾,深灰色端庄西装套装,白衬衫扣到顶,无饰品,眉头紧蹙,眼神疲惫无神,妆容极淡发暗,典型加班白领憔悴感"
        },
        {
            "id": "saleB",
            "name": "销售B(前卫)",
            "gender": "female",
            "voice_id": "zh_female_gaolengyujie_uranus_bigtts",
            "look": "28 岁亚洲女性,栗色波浪长发披肩,红色西装外套不扣 + 黑色高领修身上衣 + 黑色皮裤 + 黑色尖头高跟鞋,涂深红口红,墨镜推到头顶,大金圆耳环,自信挑眉,女王气场,专业且时髦"
        },
        {
            "id": "bossA",
            "name": "老板A(怀疑)",
            "gender": "male",
            "voice_id": "zh_male_gaolengchenwen_uranus_bigtts",
            "look": "50 岁亚洲男性,黑发整齐往后梳,方脸,深蓝色商务西装 + 白衬衫领带,微皱眉,眼神冷静审视,手交叉胸前,典型怀疑型老板"
        },
        {
            "id": "bossB",
            "name": "老板B(过来人)",
            "gender": "male",
            "voice_id": "zh_male_dayi_uranus_bigtts",
            "look": "45 岁亚洲男性,棕色短发偏分,圆脸略胖,浅蓝色商务休闲外套 + 米色衬衫无领带,自信微笑,眼神有光,典型用过好产品的过来人"
        }
    ],
    "narrator": {"voice_id": "zh_male_cixingjieshuonan_uranus_bigtts"},
    "style_prefix": "现代化办公室,落地窗自然光,莫兰迪冷色调,Pinterest 杂志感构图,产品摄影质感,景深虚化,8K 锐利,真实质感",
    "hook": "狗都不干的活,我干",
    "bgm": {
        "style": "电子节奏,前奏沉闷,B 入场后切换爆点",
        "mood": "对比反差强",
        "bpm_range": [80, 130]
    },
    "shots": [
        {
            "idx": 1, "type": "dialogue", "speaker": "saleA",
            "duration": 3, "shot_type": "近景", "camera": "固定",
            "scene": "现代办公室隔间内,桌上堆满纸质合同和泛黄报表,电脑屏幕弹着'催款无回应',窗外阴天",
            "action": "趴在桌上,头压在合同堆上,脸颊紧贴桌面,眼神涣散,嘴角下垂",
            "voiceover": "设备租赁?狗都不干!",
            "subtitle": "狗都不干!",
            "dramatic_keywords": "completely defeated, face pressed to desk, eyes hollow staring blankly, mouth slack, despair posture",
            "voice_directive": "极度疲惫无奈,带自嘲苦笑,声音低沉拖长",
            "emotion": "sad", "emotion_scale": 4,
        },
        {
            "idx": 2, "type": "dialogue", "speaker": "saleB",
            "duration": 4, "shot_type": "中景", "camera": "缓推",
            "scene": "现代办公室入口推门处,落地窗光线从背后倾泻,B 一只手撑门一只手叉腰登场",
            "action": "踩着高跟鞋大步流星走进来,推门动作有力,头微仰挑眉,墨镜推上头顶",
            "voiceover": "狗不干?我干!",
            "subtitle": "我干!",
            "dramatic_keywords": "powerful entrance, hand on hip with attitude, chin raised confidently, sunglasses pushed up, hair flowing, backlit cinematic lighting",
            "voice_directive": "霸气十足自信回击,声音上扬,带挑衅又带玩味,像甩王炸",
            "emotion": "excited", "emotion_scale": 5,
        },
        {
            "idx": 3, "type": "dialogue", "speaker": "saleB",
            "duration": 3, "shot_type": "近景", "camera": "固定",
            "scene": "办公室中央,B 双手插裤袋站立,身后是设备租赁公司 logo 墙",
            "action": "挑眉对镜头,嘴角微扬,自信不可一世",
            "voiceover": "干的就是设备租赁!",
            "subtitle": "干的就是设备租赁",
            "dramatic_keywords": "fierce smug grin, eyebrow arched provocatively, head slight tilt, confident magnetic gaze straight to camera",
            "voice_directive": "自信中带一点'我就这么牛'的玩味,语调上扬",
            "emotion": "happy", "emotion_scale": 4,
        },
        {
            "idx": 4, "type": "dialogue", "speaker": "bossA",
            "duration": 3, "shot_type": "近景", "camera": "固定",
            "scene": "办公室一角真皮沙发上,老板 A 翘腿坐着,桌上有咖啡杯,墙上挂行业奖牌",
            "action": "翘起二郎腿,双手交叉在胸前,嘴角上撇露出怀疑笑容,目光斜睨",
            "voiceover": "没客户?我看你怎么干?",
            "subtitle": "没客户你怎么干?",
            "dramatic_keywords": "skeptical raised eyebrow, smug dismissive smile, arms crossed, sideways glance, leaning back",
            "voice_directive": "怀疑加挑衅,带轻蔑的笑意,语气平稳但暗含嘲讽",
            "emotion": "neutral", "emotion_scale": 3,
        },
        {
            "idx": 5, "type": "dialogue", "speaker": "bossB",
            "duration": 3, "shot_type": "近景", "camera": "固定",
            "scene": "同沙发旁,老板 B 端着咖啡杯站起,身后是城市天际线落地窗",
            "action": "微笑摇头,手指轻轻一弹,胸有成竹的神情",
            "voiceover": "怎么可能?入驻众能平台啊!",
            "subtitle": "入驻众能平台!",
            "dramatic_keywords": "knowing confident smile, finger snap gesture of certainty, chest out, eyes gleaming",
            "voice_directive": "胸有成竹的过来人语气,带点'这有什么好怀疑'的笑意",
            "emotion": "happy", "emotion_scale": 3,
        },
        {
            "idx": 6, "type": "dialogue", "speaker": "saleB",
            "duration": 8, "shot_type": "中景", "camera": "缓推",
            "scene": "B 站在大屏幕前,屏幕显示众能平台界面:客户需求列表自动滚动,匹配状态闪烁",
            "action": "手指快速划过屏幕,转身对镜头列举要点,动作流畅自信",
            "voiceover": "需求秒匹配,客户成列表,实时查!客户信息一键录入,拜访记录在线填,从此你只管收钱!",
            "subtitle": "秒匹配·列表查·一键录入",
            "dramatic_keywords": "swiping screen with energy, turning toward camera with smooth gesture, confident commanding presence, slight smirk of victory",
            "voice_directive": "节奏明快,自信流畅,像背完美产品介绍但又像聊天分享,有感染力",
            "emotion": "excited", "emotion_scale": 4,
        },
        {
            "idx": 7, "type": "dialogue", "speaker": "bossA",
            "duration": 3, "shot_type": "近景", "camera": "固定",
            "scene": "老板 A 凑到 B 旁边的屏幕前,惊讶地看着滚动的客户列表",
            "action": "原本翘起的腿放下,身体前倾,眼睛瞪大,嘴巴微张",
            "voiceover": "这么方便?哪儿能联系?",
            "subtitle": "哪儿能联系?",
            "dramatic_keywords": "eyes wide with surprise, leaning forward eagerly, mouth open in genuine interest, formerly skeptical now curious",
            "voice_directive": "从怀疑变好奇,语气突然热切,带一丝迫切",
            "emotion": "surprised", "emotion_scale": 4,
        },
        {
            "idx": 8, "type": "dialogue", "speaker": "bossB",
            "duration": 3, "shot_type": "近景", "camera": "固定",
            "scene": "老板 B 站在镜头中央,身后是清晰的众能 logo 大屏,手势明显指向画面左下角",
            "action": "右手举起食指,指向画面左下方,眼神看向镜头,微笑",
            "voiceover": "点屏幕左下方,直接对接!",
            "subtitle": "👇 点左下方",
            "dramatic_keywords": "pointing decisively toward bottom-left corner of frame, eye contact with camera, helpful warm smile",
            "voice_directive": "热情指路语气,像朋友给小贴士",
            "emotion": "happy", "emotion_scale": 3,
        },
        {
            "idx": 9, "type": "dialogue", "speaker": "saleB",
            "duration": 3, "shot_type": "近景", "camera": "固定",
            "scene": "B 站在镜头前,身后是设备租赁工地远景叠化,众能 logo 浮现",
            "action": "手指镜头,挑眉,嘴角扬起自信的笑容,墨镜戴回",
            "voiceover": "入驻众能,共享海量资源!",
            "subtitle": "入驻众能·共享海量资源",
            "dramatic_keywords": "pointing finger toward camera, confident wink, smile with attitude, sunglasses being lowered onto face, cinematic backlight",
            "voice_directive": "号召力强,自信感染,像广告金句结尾",
            "emotion": "excited", "emotion_scale": 5,
        },
    ],
    "ending_hook": "做销售的朋友 点左下,体验下众能。",
    "warnings": [],
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
        (JOB_ID, TOPIC_ID, TITLE, "B2B SaaS广告", "D", "设备租赁销售", 5,
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
    print(f"Injected {JOB_ID}: {len(STORYBOARD['shots'])} shots, "
          f"{len(STORYBOARD['characters'])} characters")


if __name__ == "__main__":
    inject()
