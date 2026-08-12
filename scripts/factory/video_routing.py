"""Shot-level Seedance 2.5 routing for native video and speech."""
from dataclasses import dataclass
import os
from typing import Mapping


SEEDANCE25_MODEL = "doubao-seedance-2-5-260628"


def precision_motion_reason(shot: Mapping[str, object]) -> str | None:
    """Return why a shot is unsafe for generative motion, if applicable.

    Seedance can preserve a subject or reach an end frame, but it is not a
    deterministic animation engine. Exact readings, directions and UI state
    transitions must be redesigned before an expensive video request.
    """
    if str(shot.get("motion_control") or "").lower() in {
        "precise", "deterministic", "exact"
    }:
        return "motion_control requests deterministic/precise motion"

    text = " ".join(
        str(shot.get(field) or "")
        for field in ("scene", "scene_dynamics", "action", "prompt", "end_scene")
    ).lower()
    absolute_directions = (
        "向画面左侧", "向画面右侧", "从左向右", "从右向左",
        "left to right", "right to left",
    )
    directed_object_motion = (
        "行走", "走向", "驶向", "移动到", "飞向", "滑向",
        "walks", "walking", "drives", "moving toward", "flies",
    )
    if any(term in text for term in absolute_directions) and any(
        term in text for term in directed_object_motion
    ):
        return "人物、车辆或物体的绝对运动方向"

    rules = (
        (
            ("指针", "needle", "表针"),
            ("顺时针", "逆时针", "回落", "旋转", "转到", "指向", "刻度",
             "clockwise", "counterclockwise", "rotate", "reading"),
            "仪表指针的方向或刻度变化",
        ),
        (
            ("数字", "读数", "计数器", "金额", "number", "counter", "reading"),
            ("跳动", "递增", "递减", "变为", "变化到",
             "increase", "decrease", "change from", "change to"),
            "数字或读数的精确变化",
        ),
        (
            ("ui", "界面", "按钮", "进度条", "dashboard", "button", "progress"),
            ("点击", "切换", "增长", "减少", "变化", "click", "switch", "change"),
            "UI 状态或交互变化",
        ),
    )
    for subjects, actions, label in rules:
        if any(term in text for term in subjects) and any(
            term in text for term in actions
        ):
            return label
    return None


@dataclass(frozen=True)
class VideoRoute:
    model: str
    tier: str
    generate_audio: bool
    qa_required: bool
    fallback_model: str | None = None


def _truthy(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def select_route(shot: dict, storyboard_shot: dict,
                 *, env: Mapping[str, str] | None = None) -> VideoRoute:
    """Route every new shot to Seedance 2.5 with its native audio enabled."""
    env = env or os.environ
    return VideoRoute(
        model=env.get("ARK_VIDEO_PRIMARY_MODEL", SEEDANCE25_MODEL),
        tier="seedance25",
        generate_audio=True,
        qa_required=_truthy(env.get("VIDEO_QA_ENABLED"), default=True),
        fallback_model=None,
    )
