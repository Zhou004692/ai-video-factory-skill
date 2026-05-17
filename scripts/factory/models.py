"""状态机定义。Job 流转的唯一可信来源。"""
from enum import Enum


class State(str, Enum):
    PENDING       = "PENDING"
    STORYBOARDED  = "STORYBOARDED"
    CHARACTER_OK  = "CHARACTER_OK"
    SHOTS_OK      = "SHOTS_OK"
    VOICE_OK      = "VOICE_OK"
    BGM_OK        = "BGM_OK"
    COMPOSED      = "COMPOSED"
    PUBLISHED     = "PUBLISHED"
    FAILED        = "FAILED"
    BLOCKED       = "BLOCKED"   # 合规拦截


# (当前状态, step 名, 下一状态)
# 新顺序:voice 先于 shots(OmniHuman 需要音频 URL 才能驱动对口型)
TRANSITIONS = [
    (State.PENDING,      "s1_storyboard", State.STORYBOARDED),
    (State.STORYBOARDED, "s2_character",  State.CHARACTER_OK),
    (State.CHARACTER_OK, "s4_voice",      State.VOICE_OK),
    (State.VOICE_OK,     "s3_shots",      State.SHOTS_OK),
    (State.SHOTS_OK,     "s5_bgm",        State.BGM_OK),
    (State.BGM_OK,       "s6_compose",    State.COMPOSED),
    (State.COMPOSED,     "s7_publish",    State.PUBLISHED),
]


TERMINAL_STATES = {State.PUBLISHED.value, State.FAILED.value, State.BLOCKED.value}
