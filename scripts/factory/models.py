"""状态机定义。Job 流转的唯一可信来源。"""
from enum import Enum


class State(str, Enum):
    PENDING       = "PENDING"
    STORYBOARDED  = "STORYBOARDED"
    PRODUCTION_QUEUED = "PRODUCTION_QUEUED"
    PRODUCTION_RUNNING = "PRODUCTION_RUNNING"
    ANCHOR_REVIEW_REQUIRED = "ANCHOR_REVIEW_REQUIRED"
    CHARACTER_OK  = "CHARACTER_OK"
    SHOTS_OK      = "SHOTS_OK"
    VOICE_OK      = "VOICE_OK"
    BGM_OK        = "BGM_OK"
    COMPOSED      = "COMPOSED"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    PUBLISHED     = "PUBLISHED"
    FAILED        = "FAILED"
    BLOCKED       = "BLOCKED"   # 合规拦截


# (当前状态, step 名, 下一状态)
# Seedance 2.5 directly generates the picture, dialogue and lip sync from the
# storyboard. CHARACTER_OK and VOICE_OK remain only as resume bridges for old
# persisted jobs.
TRANSITIONS = [
    (State.PENDING,      "s1_storyboard", State.STORYBOARDED),
    (State.STORYBOARDED, "s3_shots",      State.SHOTS_OK),
    (State.PRODUCTION_RUNNING, "s3_shots", State.SHOTS_OK),
    (State.CHARACTER_OK, "s3_shots",      State.SHOTS_OK),  # legacy resume
    (State.VOICE_OK,     "s3_shots",      State.SHOTS_OK),  # legacy resume
    (State.SHOTS_OK,     "s5_bgm",        State.BGM_OK),
    (State.BGM_OK,       "s6_compose",    State.COMPOSED),
]


TERMINAL_STATES = {State.PUBLISHED.value, State.FAILED.value, State.BLOCKED.value}
