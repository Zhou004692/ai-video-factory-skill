"""Application service for the local AI Video Factory management console."""
from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from . import (
    anchor_generation, character_assets, character_voices, error_registry, master_dependencies, media_sources, performance_feedback, production_metrics, production_planner, quality_canary,
    network_recovery, node_state_machine, production_config, production_executor, recovery_plan,
    production_invariants, production_projection, provider_capacity,
    provider_health, provider_tasks, recovery_controller, scene_assets,
    script_drafts, script_sources, source_analysis, source_scheduler,
    state_machine, task_doctor, topic_briefs, transport_diagnostics,
    master_download_recovery, content_review,
)
from .clients import douyin, music, seedance, video_qa, volc_assets, volc_tts
from .video_routing import AUDIO_MODE_FIXED_TTS, normalize_audio_mode
from .review_candidates import (
    controlled_candidate_path, controlled_job_asset_path,
    finalize_accepted_candidates,
)
from .steps import s1_storyboard, s3_shots, s5_bgm, s6_compose


CUSTOM_CHARACTER_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
CUSTOM_CHARACTER_IMAGE_MAX_BYTES = 20 * 1024 * 1024
CUSTOM_CHARACTER_IMAGE_MAX_COUNT = 24
WEIXIN_CHANNELS_ASSISTANT_URL = "https://channels.weixin.qq.com/login"

FINAL_COST_STATES = {"COMPOSED", "READY_TO_PUBLISH", "PUBLISHED"}

_TERMINAL_VIDEO_STATES = {"PUBLISHED", "REJECTED"}
_DEFAULT_VIDEO_ACTIONS = {
    "PENDING": {"id": "view_progress", "label": "查看任务进度"},
    "STORYBOARDED": {"id": "confirm_budget", "label": "完成制作设置并预检"},
    "PRODUCTION_QUEUED": {"id": "view_progress", "label": "查看排队进度"},
    "PRODUCTION_RUNNING": {"id": "view_progress", "label": "查看生产进度"},
    "BUDGET_CONFIRMATION_REQUIRED": {
        "id": "increase_job_budget", "label": "确认预算并继续",
    },
    "REVIEW_DECIDING": {"id": "view_progress", "label": "查看复核进度"},
    "ANCHOR_REVIEW_REQUIRED": {
        "id": "review_continuity_anchors", "label": "确认人物与场景锚点",
    },
    "CHARACTER_OK": {"id": "view_progress", "label": "查看生产进度"},
    "VOICE_OK": {"id": "view_progress", "label": "查看生产进度"},
    "SHOTS_OK": {
        "id": "resume_local_postproduction", "label": "继续本地后期",
    },
    "BGM_OK": {
        "id": "resume_local_postproduction", "label": "继续本地合成",
    },
    "COMPOSED": {"id": "review_final_video", "label": "验收成片"},
    "READY_TO_PUBLISH": {"id": "publish_video", "label": "选择渠道并发布"},
    "FAILED": {"id": "preserve_and_diagnose", "label": "保留现场并重新诊断"},
    "BLOCKED": {"id": "preserve_and_diagnose", "label": "保留现场并重新诊断"},
    "RECOVERY_REQUIRED": {
        "id": "preserve_and_diagnose", "label": "检查恢复现场",
    },
}

_VIDEO_STATE_REASONS = {
    "PENDING": "任务已创建，正在等待进入制作流程",
    "STORYBOARDED": "导演计划已就绪，等待确认制作设置与预算",
    "PRODUCTION_QUEUED": "任务已进入生产队列",
    "PRODUCTION_RUNNING": "人物、场景或母片正在生成",
    "BUDGET_CONFIRMATION_REQUIRED": "下一次付费调用超过已确认预算，已在扣费前暂停",
    "REVIEW_DECIDING": "人工复核决定正在安全落盘",
    "ANCHOR_REVIEW_REQUIRED": "人物与场景锚点已生成，等待人工确认",
    "CHARACTER_OK": "人物资产已就绪，正在继续生产",
    "VOICE_OK": "声音资产已就绪，正在继续生产",
    "SHOTS_OK": "全部母片已就绪，等待本地后期",
    "BGM_OK": "配乐已就绪，等待本地合成",
    "COMPOSED": "成片已生成，等待人工验收",
    "READY_TO_PUBLISH": "成片已通过验收，等待选择渠道发布",
    "FAILED": "生产失败，现场与技术证据已保留",
    "BLOCKED": "任务已阻断，等待人工诊断",
    "RECOVERY_REQUIRED": "自动恢复次数已达上限，等待人工检查",
    "PRODUCTION_REVIEW_REQUIRED": "候选母片等待人工复核",
    "PROVIDER_ACTION_REQUIRED": "远端模型任务需要人工处理或核对",
    "REJECTED_PENDING_RECONCILIATION": "业务已终止，仍需核对远端成本",
    "REJECTED": "任务已由人工终止",
    "PUBLISHED": "视频已发布",
}

_PRODUCTION_STAGE_DEFINITIONS = (
    ("character", "人物定妆", frozenset({"character_anchor"})),
    ("scene", "场景准备", frozenset({"scene_anchor"})),
    ("master", "母片生成", frozenset({"master"})),
    ("compose", "本地剪辑", frozenset({"bgm", "local_compose"})),
    ("review", "成片验收", frozenset({"final_review"})),
)
_PRODUCTION_STAGE_STATE_PRIORITY = (
    "ACTION_REQUIRED", "BUDGET_REQUIRED", "REVIEW_REQUIRED", "FAILED", "RUNNING", "READY",
    "PENDING", "SUCCEEDED", "SKIPPED",
)


def _production_stage_projection(nodes: list[dict]) -> list[dict]:
    stages: list[dict] = []
    for stage_id, label, node_types in _PRODUCTION_STAGE_DEFINITIONS:
        members = [
            node for node in nodes if str(node.get("node_type")) in node_types
        ]
        states = {str(node.get("state") or "PENDING") for node in members}
        stage_state = next((
            state for state in _PRODUCTION_STAGE_STATE_PRIORITY
            if state in states
        ), "PENDING")
        stages.append({
            "id": stage_id,
            "label": label,
            "state": stage_state,
            "completed": sum(
                str(node.get("state")) in {"SUCCEEDED", "SKIPPED"}
                for node in members
            ),
            "total": len(members),
        })
    return stages


SECRET_KEYS = {
    "ANTHROPIC_API_KEY",
    "ARK_API_KEY",
    "VOLC_ASR_API_KEY",
    "VOLC_TTS_API_KEY",
    "VOLC_ASR_TOKEN",
    "VOLC_AK",
    "VOLC_SK",
    "SUNO_API_KEY",
    "DOUYIN_CLIENT_SECRET",
    "DOUYIN_ACCESS_TOKEN",
}


def _calibration_summary(samples: list[tuple[int, int]]) -> dict:
    estimated_total = sum(item[0] for item in samples)
    actual_total = sum(item[1] for item in samples)
    variance = actual_total - estimated_total
    variance_percent = (
        round(variance * 100 / estimated_total, 1)
        if estimated_total else None
    )
    mape = (
        round(sum(
            abs(actual - estimated) * 100 / estimated
            for estimated, actual in samples
        ) / len(samples), 1)
        if samples else None
    )
    return {
        "sample_count": len(samples),
        "estimated_cost_cents": estimated_total,
        "actual_cost_cents": actual_total,
        "variance_cents": variance,
        "variance_percent": variance_percent,
        "mean_absolute_percentage_error": mape,
        "status": (
            "no_data" if not samples
            else "calibrating" if len(samples) < 3
            else "stable" if mape is not None and mape <= 10
            else "drift"
        ),
    }


def _cost_estimation_calibration(database, jobs) -> dict:
    """Summarize only immutable confirmation snapshots from final jobs."""
    samples: list[tuple[int, int, str]] = []
    segment_samples: dict[str, dict[str, list[tuple[int, int]]]] = {
        "resolution": {},
        "duration_bucket": {},
        "reference_type": {},
    }
    calls_by_master: dict[tuple[str, int], int] = {}
    for call in database.execute(
        "SELECT job_id,shot_idx,SUM(cost_cents) AS actual_cost_cents "
        "FROM provider_calls WHERE provider LIKE 'seedance%' "
        "AND shot_idx>0 GROUP BY job_id,shot_idx"
    ).fetchall():
        calls_by_master[(str(call["job_id"]), int(call["shot_idx"]))] = int(
            call["actual_cost_cents"] or 0
        )
    for row in jobs:
        if str(row["state"] or "") not in FINAL_COST_STATES:
            continue
        try:
            storyboard = json.loads(row["storyboard"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        confirmed = (
            storyboard.get("production_settings") or {}
        ).get("confirmed_cost_estimate")
        if not isinstance(confirmed, dict):
            continue
        try:
            estimated = int(confirmed.get("projected_total_cost_cents") or 0)
        except (TypeError, ValueError):
            continue
        if estimated <= 0:
            continue
        samples.append((
            estimated,
            int(row["cost_cents"] or 0),
            str(confirmed.get("pricing_version") or "unknown"),
        ))
        paid_masters = confirmed.get("paid_masters") or []
        if not isinstance(paid_masters, list):
            continue
        for master in paid_masters:
            if not isinstance(master, dict):
                continue
            try:
                master_idx = int(master.get("master_idx") or 0)
                master_estimate = int(master.get("estimated_cost_cents") or 0)
            except (TypeError, ValueError):
                continue
            actual = calls_by_master.get((str(row["job_id"]), master_idx))
            if master_idx <= 0 or master_estimate <= 0 or actual is None:
                continue
            for dimension in segment_samples:
                key = str(master.get(dimension) or "").strip()
                if key:
                    segment_samples[dimension].setdefault(key, []).append(
                        (master_estimate, actual)
                    )
    overall = _calibration_summary([
        (estimated, actual) for estimated, actual, _version in samples
    ])
    order = {
        "resolution": ["480p", "720p", "1080p"],
        "duration_bucket": ["4-12s", "13-24s", "25-30s"],
        "reference_type": ["none", "audio", "image", "video", "mixed"],
    }
    segments = {}
    for dimension, grouped in segment_samples.items():
        keys = sorted(
            grouped,
            key=lambda key: (
                order[dimension].index(key)
                if key in order[dimension] else len(order[dimension]),
                key,
            ),
        )
        segments[dimension] = [
            {"key": key, **_calibration_summary(grouped[key])}
            for key in keys
        ]
    return {
        **overall,
        "pricing_versions": sorted({item[2] for item in samples}),
        "segments": segments,
    }


def _files_have_same_content(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    digests = []
    for path in (left, right):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digests.append(digest.digest())
    return digests[0] == digests[1]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lip_sync_repair_suggestion(
    storyboard: dict, master: dict, master_idx: int, duration_sec: float,
    qa: dict | None = None,
) -> dict:
    """Locate the smallest speech interval backed by the edited subtitle cues."""
    duration = max(0.1, float(duration_sec or master.get("duration") or 0.1))
    evidence = video_qa.normalize_lip_sync_evidence(
        dict((qa or {}).get("result") or {})
    )
    located = [
        segment for segment in evidence.get("segments") or []
        if segment.get("confidence", 0) >= 0.65
        and segment.get("severity") in {"high", "medium"}
        and float(segment.get("start_sec") or 0) < duration
        and float(segment.get("end_sec") or 0) > 0
    ]
    if located:
        target = min(located, key=lambda item: (
            item.get("alignment", 1),
            -item.get("confidence", 0),
            item.get("start_sec", 0),
        ))
        start = max(0.0, float(target["start_sec"]) - 0.25)
        end = min(duration, float(target["end_sec"]) + 0.25)
        offset = int(target.get("offset_ms") or 0)
        confidence = round(float(target.get("confidence") or 0) * 100)
        return {
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "dialogue": str(target.get("dialogue") or "").strip(),
            "reason": (
                f"自动质检定位 {target['start_sec']}–{target['end_sec']} 秒，"
                f"口型偏移 {offset}ms，置信度 {confidence}%"
            ),
            "source": "lip_sync_evidence",
        }
    intervals: list[tuple[float, float]] = []
    dialogue: list[str] = []
    for edit in storyboard.get("shots") or []:
        if int(edit.get("audio_master_idx") or 0) != int(master_idx):
            continue
        audio_start = max(0.0, float(edit.get("audio_start") or 0))
        for cue in edit.get("subtitle_cues") or []:
            start = max(0.0, min(duration, audio_start + float(
                cue.get("start") or 0
            )))
            end = max(start, min(duration, audio_start + float(
                cue.get("end") or cue.get("start") or 0
            )))
            if end <= start:
                continue
            intervals.append((start, end))
            text = str(cue.get("text") or "").strip()
            if text and text not in dialogue:
                dialogue.append(text)
    if intervals:
        start = min(item[0] for item in intervals)
        end = max(item[1] for item in intervals)
    else:
        start, end = 0.0, duration
        fallback = str(master.get("voiceover") or "").strip()
        if fallback:
            dialogue.append(fallback)
    return {
        "start_sec": round(start, 3),
        "end_sec": round(end, 3),
        "dialogue": "，".join(dialogue),
        "reason": "自动根据字幕时间轴定位口型偏差区间",
    }

EDITABLE_CONFIG = {
    "DEFAULT_LLM_PROVIDER",
    "CLAUDE_MODEL",
    "ARK_LLM_TIMEOUT_SEC",
    "ARK_API_KEY",
    "ARK_REGION",
    "ARK_VIDEO_PRIMARY_MODEL",
    "ARK_VIDEO_MODEL",
    "ARK_VIDEO_RESOLUTION",
    "VIDEO_QA_ENABLED",
    "SEEDANCE25_LONG_SCENE_ENABLED",
    "SEEDANCE_MAX_AUTO_RETRIES",
    "SEEDANCE_AUTO_RETRY_BUDGET_CENTS",
    "SEEDANCE_JOB_BUDGET_CENTS",
    "SEEDANCE_PRODUCTION_REFERENCE_VIDEO_BUDGET",
    "PRODUCTION_EXECUTION_ENGINE",
    "CHARACTER_ANCHOR_GENERATOR",
    "SCENE_ANCHOR_GENERATOR",
    "VOLC_ASR_API_KEY",
    "VOLC_TTS_APPID",
    "VOLC_TTS_API_KEY",
    "VOLC_TTS_PRICE_CENTS_PER_10K_CHARS",
    "DIALOGUE_AUDIO_MODE",
    "VOLC_AK",
    "VOLC_SK",
    "VOLC_ASSETS_ENDPOINT",
    "VOLC_ASSETS_REGION",
    "VOLC_ASSETS_PROJECT",
    "TOS_BUCKET",
    "TOS_REGION",
    "TOS_ENDPOINT",
    "TOS_OBJECT_PREFIX",
    "SUNO_API_KEY",
    "DEFAULT_MUSIC_PROVIDER",
    "MAX_CONCURRENT_JOBS",
    "MAX_CONCURRENT_SHOTS",
    "MAX_RETRIES",
    "PUBLISH_MODE",
    "BRAND_NAME",
    "DOUYIN_CLIENT_KEY",
    "DOUYIN_CLIENT_SECRET",
    "DOUYIN_REDIRECT_URI",
    "WEIXIN_CHANNELS_ASSISTANT_URL",
}

REQUIRED_CONFIG_GROUPS = (
    {
        "id": "ark",
        "priority": "P0",
        "blocking": True,
        "impact": "未配置时，剧本生成与 Seedance 视频生成均不可用",
        "title": "方舟生成能力",
        "capability": "解锁剧本生成与 Seedance 2.5 视频生成",
        "keys": ("ARK_API_KEY",),
        "guide": {
            "url": "https://www.volcengine.com/docs/84990/1829055?lang=zh",
            "label": "打开火山方舟 API Key 指引",
            "steps": (
                "登录火山方舟控制台，进入“系统管理 → API Key 管理”",
                "复制具备模型调用权限的 API Key",
                "粘贴到本页并保存；剧本流水线即可使用",
            ),
        },
    },
    {
        "id": "tts",
        "priority": "P0",
        "blocking": True,
        "impact": "未配置时，对白角色无法使用固定声线，系统会在 Seedance 付费调用前停止",
        "title": "角色固定声线",
        "capability": "按角色设定匹配火山声线，并生成可复用的精确对白音轨",
        "keys": ("VOLC_TTS_APPID", "VOLC_TTS_API_KEY"),
        "guide": {
            "url": "https://www.volcengine.com/docs/6561/2228192?lang=zh",
            "label": "打开火山语音合成 2.0 接入指引",
            "steps": (
                "在豆包语音控制台开通语音合成 2.0（seed-tts-2.0）",
                "复制 App ID 与 API Key，保存到本页",
                "如需自动挑选声线，再配置同账号 VOLC_AK / VOLC_SK；手工绑定声线则不需要",
            ),
        },
    },
    {
        "id": "asr",
        "priority": "P1",
        "blocking": False,
        "impact": "不影响原生母片生成；未配置时无法自动校准字幕与语音文本",
        "title": "字幕与语音识别",
        "capability": "从原生母片提取准确字幕并完成本地导演剪辑",
        "keys": ("VOLC_ASR_API_KEY",),
        "guide": {
            "url": "https://console.volcengine.com/speech/new/setting/apikeys?projectName=default",
            "label": "打开豆包语音控制台",
            "steps": (
                "在新版豆包语音控制台开通“录音文件识别 2.0”",
                "进入 API Key 页面创建独立 App Key",
                "复制 App Key 粘贴到本页并保存",
            ),
        },
    },
    {
        "id": "storage",
        "priority": "P1",
        "blocking": False,
        "impact": "未配置时不能上传参考素材、自动查询真人资产或使用云端素材存储；纯文本生成仍可用",
        "title": "火山账号与素材存储",
        "capability": "上传参考素材、查询真人资产并保存生成文件",
        "keys": ("VOLC_AK", "VOLC_SK", "TOS_BUCKET"),
        "guide": {
            "url": "https://www.volcengine.com/docs/6349/74830?lang=zh",
            "label": "打开 TOS 配置指引",
            "steps": (
                "在 TOS 创建或选择一个 Bucket，建议与方舟使用同一区域",
                "为 IAM 子用户创建具备该 Bucket 读写权限的 AK / SK",
                "粘贴 AK、SK 和完整 Bucket 名称；不要使用主账号长期密钥",
            ),
        },
    },
)

REQUIRED_CONFIG_KEYS = {
    key for group in REQUIRED_CONFIG_GROUPS for key in group["keys"]
}


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _configured(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text and not text.startswith("<") and text != "...")


def _weixin_channels_assistant_url(value: object) -> str:
    """Return an official Channels Assistant URL, never an arbitrary host."""
    candidate = str(value or "").strip()
    if candidate:
        parsed = urlparse(candidate)
        if parsed.scheme == "https" and parsed.hostname == "channels.weixin.qq.com":
            return candidate
    return WEIXIN_CHANNELS_ASSISTANT_URL


def _rows(cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


def _completed_identity_keys(
    database: sqlite3.Connection, job_id: str
) -> set[str]:
    return {
        str(row["identity_key"])
        for row in database.execute(
            "SELECT identity_key FROM character_identity_anchors "
            "WHERE job_id=? AND state='READY' AND source_type!='volc_asset'",
            (job_id,),
        ).fetchall()
    }


def _completed_scene_keys(
    database: sqlite3.Connection, job_id: str
) -> set[str]:
    return {
        str(row["anchor_key"])
        for row in database.execute(
            "SELECT anchor_key FROM scene_visual_anchors "
            "WHERE job_id=? AND state='READY'",
            (job_id,),
        ).fetchall()
    }


def _request_invalid_identity_anchor_retry_ready(
    database: sqlite3.Connection,
    *,
    job_id: str,
    state: object,
    failure_code: object,
    cost_cents: object,
    shots: list,
) -> bool:
    """Allow an explicit retry only for an unpaid identity-anchor rejection."""
    if (
        str(state or "") != "FAILED"
        or str(failure_code or "") != "request_invalid"
        or int(cost_cents or 0) != 0
        or not shots
        or any(str(row["state"]) != "PENDING" for row in shots)
    ):
        return False
    if database.execute(
        "SELECT 1 FROM provider_calls WHERE job_id=? LIMIT 1", (job_id,)
    ).fetchone():
        return False
    failed = database.execute(
        "SELECT cost_cents,video_path,video_url,image_path,image_url "
        "FROM character_identity_anchors WHERE job_id=? AND state='FAILED' "
        "AND source_type IN ('seedance_identity_anchor','seedream_identity_image')",
        (job_id,),
    ).fetchall()
    return bool(failed) and all(
        int(row["cost_cents"] or 0) == 0
        and not any(
            str(row[field] or "").strip()
            for field in ("video_path", "video_url", "image_path", "image_url")
        )
        for row in failed
    )


def _publication_metadata(value: object) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _publication_is_pending(value: object) -> bool:
    return str(_publication_metadata(value).get("review_status") or "").lower() in {
        "pending", "reviewing", "submitted",
    }


REVIEW_CHECKS = (
    "identity", "lip_sync", "subtitle_sync", "visual_integrity",
    "clean_frame", "editing_rhythm",
)


def _source_quality(content: object) -> dict:
    text = " ".join(str(content or "").split())
    length = len(text)
    if length < 20:
        return {
            "passed": False, "level": "blocked", "score": min(30, length * 2),
            "reason": "来源内容不足，无法形成有证据支撑的选题",
            "suggestion": "请补充事件背景、目标受众、核心事实和期望观点",
        }
    if length < 60:
        return {
            "passed": True, "level": "warning", "score": min(69, 40 + length // 3),
            "reason": "来源信息较少，生成结果需要加强人工核验",
            "suggestion": "建议补充事实依据、案例或数据后再生成",
        }
    return {
        "passed": True, "level": "ready", "score": min(100, 70 + length // 20),
        "reason": "来源信息量满足选题分析要求", "suggestion": "可以生成选题简报",
    }


def _director_timeline(storyboard: dict) -> list[dict]:
    """Expose the local edit decision without leaking implementation details."""
    masters = {
        int(item.get("idx") or index): item
        for index, item in enumerate(storyboard.get("masters") or [], 1)
    }
    timeline = []
    for index, edit in enumerate(storyboard.get("shots") or [], 1):
        video_idx = int(edit.get("video_master_idx") or edit.get("idx") or index)
        audio_idx = int(edit.get("audio_master_idx") or video_idx)
        video_master = masters.get(video_idx, {})
        audio_master = masters.get(audio_idx, {})
        video_type = str(video_master.get("type") or "dialogue")
        timeline.append({
            "idx": int(edit.get("idx") or index),
            "beat": str(edit.get("beat") or "scene"),
            "duration_sec": float(edit.get("duration") or 0),
            "video_master_idx": video_idx,
            "audio_master_idx": audio_idx,
            "video_start_sec": float(edit.get("video_start") or 0),
            "audio_start_sec": float(edit.get("audio_start") or 0),
            "video_source_label": (
                f"场景 B-roll #{video_idx}" if video_type == "broll"
                else f"对白母片 #{video_idx}"
            ),
            "audio_source_label": f"对白音轨 #{audio_idx}",
            "uses_broll": video_type == "broll" and video_idx != audio_idx,
            "speaker": str(audio_master.get("speaker") or ""),
            "visual_intent": str(edit.get("visual_intent") or ""),
            "broll_hint": str(edit.get("broll_hint") or ""),
            "continuity_relation": str(
                edit.get("continuity_relation") or ""
            ),
            "transition_type": str(edit.get("transition_type") or "none"),
            "transition_duration_sec": float(
                edit.get("transition_duration_sec") or 0
            ),
            "transition_reason": str(edit.get("transition_reason") or ""),
            "preserve_audio_continuity": bool(
                edit.get("preserve_audio_continuity")
            ),
        })
    return timeline


def _process_is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, int(pid)
        )
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


class ManagementService:
    """Read and mutate local factory state through a UI-safe boundary."""

    def __init__(
        self,
        database: sqlite3.Connection,
        root: Path,
        *,
        worker_mode: str = "child_process",
        data_dir: Path | None = None,
        env_path: Path | None = None,
        production_service=None,
        production_read_model=None,
        transport_preflight_enabled: bool = False,
    ):
        self.database = database
        self.root = Path(root).resolve()
        self.data_dir = (
            Path(data_dir).resolve() if data_dir is not None else self.root / "data"
        )
        self.env_path = (
            Path(env_path).resolve() if env_path is not None else self.root / ".env"
        )
        if worker_mode not in {"child_process", "external"}:
            raise ValueError("worker_mode must be child_process or external")
        self.worker_mode = worker_mode
        self.transport_preflight_enabled = bool(transport_preflight_enabled)
        self.transport_diagnostic_path = (
            self.data_dir / "runtime" / "transport-preflight.json"
        )
        if production_service is None or production_read_model is None:
            from .services import ProductionReadModel, ProductionService
        self.production_service = (
            production_service
            if production_service is not None
            else ProductionService(
                self.database,
                self.root,
                env_path=self.env_path,
                start_handler=self._start_video_job_impl,
                resume_handler=self._resume_failed_video_impl,
                review_handler=self._review_video_impl,
                reconcile_handler=self._reconcile_provider_task_impl,
            )
        )
        self.production_read_model = (
            production_read_model
            if production_read_model is not None
            else ProductionReadModel(self.database)
        )
        self._production_workers: dict[str, subprocess.Popen] = {}
        self._final_review_lock = threading.RLock()
        self._last_provider_sweep = {
            "last_sweep_at": None,
            "checked": 0,
            "completed": 0,
            "pending": 0,
            "failed": 0,
            "skipped": 0,
            "items": [],
        }
        character_assets.ensure_schema(self.database)
        character_voices.ensure_schema(self.database)
        source_scheduler.ensure_schema(self.database)
        job_columns = {
            row[1] for row in self.database.execute("PRAGMA table_info(jobs)")
        }
        for column, ddl in (
            ("characters_json", "TEXT"),
            ("failure_code", "TEXT"),
            ("production_worker_pid", "INTEGER"),
            ("production_started_at", "TIMESTAMP"),
            ("production_heartbeat_at", "TEXT"),
            ("worker_lease_id", "TEXT"),
            ("recovery_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("production_spec_json", "TEXT"),
            ("production_spec_hash", "TEXT"),
            ("production_spec_version", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in job_columns:
                self.database.execute(f"ALTER TABLE jobs ADD COLUMN {column} {ddl}")
                self.database.commit()
        shot_columns = {
            row[1] for row in self.database.execute("PRAGMA table_info(shots)")
        }
        for column, ddl in (
            ("narrative_type", "TEXT"),
            ("speaker_id", "TEXT"),
            ("anchor_character", "TEXT"),
            ("video_url", "TEXT"),
        ):
            if column not in shot_columns:
                self.database.execute(f"ALTER TABLE shots ADD COLUMN {column} {ddl}")
        for table in ("topic_briefs", "script_drafts"):
            columns = {
                row[1] for row in self.database.execute(f"PRAGMA table_info({table})")
            }
            if "revision" not in columns:
                self.database.execute(
                    f"ALTER TABLE {table} ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
                )
        self.database.execute(
            "CREATE TABLE IF NOT EXISTS content_revisions ("
            "revision_id TEXT PRIMARY KEY, entity_type TEXT NOT NULL,"
            "entity_id TEXT NOT NULL, revision INTEGER NOT NULL,"
            "payload_json TEXT NOT NULL, change_note TEXT NOT NULL DEFAULT '',"
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "UNIQUE(entity_type,entity_id,revision))"
        )
        self.database.commit()
        self._recover_review_operations()
        self._repair_legacy_budget_pauses()
        self._repair_dangling_job_links()
        self._reconcile_orphaned_production_workers()
        production_invariants.repair_all_safe(self.database)
        self._clear_stale_success_errors()

    def diagnose_video_task(self, job_id: str) -> dict:
        """Return a read-only, business-facing diagnosis for one video task."""
        return task_doctor.TaskDoctor(
            self.database, self.root
        ).diagnose_video(job_id)

    def repair_video_task(
        self, job_id: str, action_ids: list[str] | None = None,
    ) -> dict:
        """Apply only Task Doctor's current zero-cost repair allowlist."""
        return task_doctor.TaskDoctor(
            self.database, self.root
        ).repair_video(job_id, action_ids=action_ids)

    @staticmethod
    def _budget_pause_details(message: str) -> dict[str, int] | None:
        match = re.search(
            r"current=(\d+),\s*pending=(\d+),\s*"
            r"next_estimate=(\d+),\s*limit=(\d+)\s*cents",
            str(message or ""),
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        return {
            "current_cost_cents": int(match.group(1)),
            "pending_cost_cents": int(match.group(2)),
            "next_estimate_cents": int(match.group(3)),
            "budget_limit_cents": int(match.group(4)),
        }

    def _repair_legacy_budget_pauses(self, job_id: str | None = None) -> int:
        """Turn unpaid legacy budget failures into a recoverable pause."""
        where = "n.state='FAILED' AND n.actual_cost_cents=0 "
        params: list[object] = []
        if job_id:
            where += "AND n.job_id=? "
            params.append(job_id)
        rows = self.database.execute(
            "SELECT n.job_id,n.node_id,n.provider_task_id,n.output_json,"
            "e.payload_json FROM production_nodes n "
            "LEFT JOIN production_node_events e ON e.rowid=("
            "SELECT e2.rowid FROM production_node_events e2 "
            "WHERE e2.job_id=n.job_id AND e2.node_id=n.node_id "
            "ORDER BY e2.created_at DESC,e2.rowid DESC LIMIT 1) "
            f"WHERE {where}",
            params,
        ).fetchall()
        repaired = 0
        for row in rows:
            if str(row["provider_task_id"] or "").strip():
                continue
            try:
                output = json.loads(row["output_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                output = {}
            try:
                event_payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                event_payload = {}
            message = str(
                output.get("error") or event_payload.get("error") or ""
            )
            details = self._budget_pause_details(message)
            if not details and str(output.get("stable_code") or "") != (
                "budget_confirmation_required"
            ):
                continue
            output.update(details or {})
            output.update({
                "stable_code": "budget_confirmation_required",
                "error": message,
            })
            current_job_id = str(row["job_id"])
            node_id = str(row["node_id"])
            updated = self.database.execute(
                "UPDATE production_nodes SET state='BUDGET_REQUIRED',"
                "error_code='budget_confirmation_required',"
                "next_action='increase_job_budget',output_json=?,lease_id=NULL,"
                "heartbeat_at=NULL,updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=? AND node_id=? AND state='FAILED' "
                "AND actual_cost_cents=0 AND provider_task_id IS NULL",
                (
                    json.dumps(output, ensure_ascii=False, sort_keys=True),
                    current_job_id, node_id,
                ),
            )
            if int(updated.rowcount or 0) != 1:
                continue
            self.database.execute(
                "INSERT OR IGNORE INTO production_node_events("
                "job_id,event_id,node_id,event,from_state,to_state,payload_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    current_job_id,
                    f"{current_job_id}:{node_id}:budget-pause-repair-v1",
                    node_id, "repair_budget_pause", "FAILED",
                    "BUDGET_REQUIRED",
                    json.dumps(output, ensure_ascii=False, sort_keys=True),
                ),
            )
            if node_id.startswith("master:"):
                try:
                    master_idx = int(node_id.split(":", 1)[1])
                except ValueError:
                    master_idx = 0
                if master_idx:
                    self.database.execute(
                        "UPDATE shots SET state='PENDING',error=NULL "
                        "WHERE job_id=? AND idx=? AND state='FAILED'",
                        (current_job_id, master_idx),
                    )
            self.database.execute(
                "UPDATE jobs SET state='BUDGET_CONFIRMATION_REQUIRED',"
                "failure_code='budget_confirmation_required',"
                "error='下一次付费调用超过已确认预算，已在扣费前暂停',"
                "production_worker_pid=NULL,updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=? AND state IN ("
                "'FAILED','PRODUCTION_RUNNING','PRODUCTION_QUEUED','STORYBOARDED'"
                ")",
                (current_job_id,),
            )
            repaired += 1
        if repaired:
            self.database.commit()
        return repaired

    def _recover_review_operations(self) -> None:
        """Recover durable review file operations left by a process exit."""
        rows = self.database.execute(
            "SELECT job_id,idx,state,video_qa FROM shots "
            "WHERE state='REVIEW_DECIDING' OR video_qa LIKE '%review_operation%'"
        ).fetchall()
        changed = False
        repaired_graph_jobs: set[str] = set()
        for row in rows:
            try:
                qa = json.loads(row["video_qa"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            operation = qa.get("review_operation")
            if not isinstance(operation, dict):
                continue
            job_id = str(row["job_id"])
            decision = str(operation.get("decision") or "")
            phase = str(operation.get("phase") or "claimed")

            def controlled(key: str, *, require_file: bool = False) -> Path | None:
                return controlled_job_asset_path(
                    self.data_dir / "assets", job_id,
                    operation.get(key), require_file=require_file,
                )

            rejected_interrupted = bool(
                row["state"] == "FAILED"
                and qa.get("human_decision") == "reject_job"
                and phase != "committed"
            )
            if row["state"] == "REVIEW_DECIDING" or rejected_interrupted:
                recovered_state = (
                    "FAILED" if rejected_interrupted else "REVIEW_REQUIRED"
                )
                expected_state = str(row["state"])
                cleanup_after_commit: list[Path] = []
                try:
                    if decision == "accept_candidate":
                        active = controlled("active_path")
                        active_audio = active.with_suffix(".wav") if active else None
                        backup = controlled("prior_backup_path", require_file=True)
                        backup_audio = controlled(
                            "prior_audio_backup_path", require_file=True
                        )
                        if active and backup:
                            shutil.copy2(backup, active)
                        elif active and phase != "claimed":
                            active.unlink(missing_ok=True)
                        if active_audio and backup_audio:
                            shutil.copy2(backup_audio, active_audio)
                        elif active_audio and phase in {
                            "audio_published", "committed",
                        }:
                            active_audio.unlink(missing_ok=True)
                    elif decision == "regenerate_master":
                        candidate_path = controlled("candidate_path")
                        archive = controlled("archive_path", require_file=True)
                        if candidate_path and archive:
                            candidate_path.parent.mkdir(parents=True, exist_ok=True)
                            archive.replace(candidate_path)
                        candidate_audio = (
                            candidate_path.with_suffix(".wav")
                            if candidate_path else None
                        )
                        archive_audio = controlled(
                            "archive_audio_path", require_file=True
                        )
                        if candidate_audio and archive_audio:
                            archive_audio.replace(candidate_audio)
                    for key in (
                        "prepared_video_path", "prepared_audio_path",
                        "prior_backup_path", "prior_audio_backup_path",
                    ):
                        path = controlled(key)
                        if path:
                            cleanup_after_commit.append(path)
                except OSError:
                    continue
                qa.pop("review_operation", None)
                try:
                    restored = self.database.execute(
                        "UPDATE shots SET state=?,video_qa=? "
                        "WHERE job_id=? AND idx=? AND state=?",
                        (
                            recovered_state,
                            json.dumps(qa, ensure_ascii=False),
                            job_id, int(row["idx"]), expected_state,
                        ),
                    )
                    if int(restored.rowcount or 0) != 1:
                        self.database.rollback()
                        continue
                    # The restored DB state is the recovery linearization point.
                    # Keep every backup until it is durable so a second restart
                    # can safely repeat the file restoration.
                    self.database.commit()
                except Exception:
                    self.database.rollback()
                    raise
                for path in cleanup_after_commit:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        # Recovery is already durable; stale backups are safe for
                        # later GC and must not put the shot back into limbo.
                        pass
                continue
            if phase != "committed":
                continue
            if decision in {"accept_candidate", "keep_prior", "regenerate_master"}:
                if self._sync_graph_master_review_decision(
                    job_id,
                    int(row["idx"]),
                    decision=decision,
                    operation_id=str(operation.get("operation_id") or "recovery"),
                ):
                    repaired_graph_jobs.add(job_id)
                    changed = True
            if decision == "regenerate_master":
                cleanup_ok = True
                for key in ("archive_path", "archive_audio_path"):
                    path = controlled(key)
                    if path:
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            cleanup_ok = False
                if cleanup_ok:
                    self.database.execute(
                        "UPDATE shots SET video_qa=NULL WHERE job_id=? AND idx=?",
                        (job_id, int(row["idx"])),
                    )
                    changed = True
            elif decision == "keep_prior":
                cleanup_ok = True
                candidate_path = controlled("candidate_path")
                for path in (
                    candidate_path,
                    candidate_path.with_suffix(".wav") if candidate_path else None,
                ):
                    if path:
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            cleanup_ok = False
                if cleanup_ok:
                    qa.pop("review_operation", None)
                    self.database.execute(
                        "UPDATE shots SET video_qa=? WHERE job_id=? AND idx=?",
                        (
                            json.dumps(qa, ensure_ascii=False),
                            job_id, int(row["idx"]),
                        ),
                    )
                    changed = True
        for job_id in repaired_graph_jobs:
            report = production_projection.reconcile(self.database, job_id)
            if (
                report.current_job_state == "PRODUCTION_REVIEW_REQUIRED"
                and report.projected_job_state != "PRODUCTION_REVIEW_REQUIRED"
            ):
                self.database.execute(
                    "UPDATE jobs SET state=?,error=NULL,production_worker_pid=NULL,"
                    "updated_at=CURRENT_TIMESTAMP WHERE job_id=? "
                    "AND state='PRODUCTION_REVIEW_REQUIRED'",
                    (report.projected_job_state, job_id),
                )
                changed = True
        if changed:
            self.database.commit()

    def _assert_no_unresolved_provider_tasks(self, job_id: str) -> None:
        """Prevent a paid repost while a remote task still needs reconciliation."""
        try:
            provider_tasks.assert_no_unresolved(self.database, job_id)
        except provider_tasks.ProviderTaskGateError as exc:
            raise ValueError(str(exc)) from exc

    def _sync_graph_reconciled_master_candidate(
        self,
        job_id: str,
        master_idx: int,
        *,
        task_id: str,
        candidate_path: Path,
        candidate_url: str | None,
    ) -> bool:
        """Converge a late paid provider result into the Graph v2 review gate."""
        engine = self.database.execute(
            "SELECT execution_engine FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not engine or str(engine["execution_engine"] or "legacy") != "graph_v2":
            return False
        node_id = f"master:{int(master_idx)}"
        node = self.database.execute(
            "SELECT state,output_json FROM production_nodes "
            "WHERE job_id=? AND node_id=?",
            (job_id, node_id),
        ).fetchone()
        if node is None:
            return False
        current = str(node["state"] or "")
        if current not in {
            "SUCCEEDED", "REVIEW_REQUIRED", "BUDGET_REQUIRED",
            "ACTION_REQUIRED", "FAILED", "RUNNING", "READY",
        }:
            raise ValueError(
                f"reconciled master graph state is inconsistent: {node_id}={current}"
            )
        if current not in {"SUCCEEDED", "REVIEW_REQUIRED"}:
            node_state_machine.transition_node_in_transaction(
                self.database,
                job_id,
                node_id,
                "reconcile_review",
                event_id=f"{job_id}:{node_id}:provider-reconciled:{task_id}",
                payload={
                    "provider_task_id": str(task_id),
                    "master_idx": int(master_idx),
                    "candidate_path": str(candidate_path),
                },
            )
        try:
            output = json.loads(node["output_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            output = {}
        for key in (
            "stable_code", "error", "error_type", "current_cost_cents",
            "pending_cost_cents", "next_estimate_cents", "budget_limit_cents",
        ):
            output.pop(key, None)
        output.update({
            "master_idx": int(master_idx),
            "status": "manual_review_required",
            "reasons": ["provider_task_reconciled"],
            "candidate_path": str(candidate_path),
            "candidate_url": str(candidate_url or ""),
        })
        provider_cost = int(self.database.execute(
            "SELECT COALESCE(SUM(cost_cents),0) FROM provider_calls "
            "WHERE job_id=? AND shot_idx=?",
            (job_id, int(master_idx)),
        ).fetchone()[0] or 0)
        self.database.execute(
            "UPDATE production_nodes SET actual_cost_cents=?,provider_task_id=?,"
            "error_code=NULL,next_action='review_candidate',output_json=?,"
            "lease_id=NULL,heartbeat_at=NULL,updated_at=CURRENT_TIMESTAMP "
            "WHERE job_id=? AND node_id=?",
            (
                provider_cost,
                str(task_id),
                json.dumps(output, ensure_ascii=False, sort_keys=True),
                job_id,
                node_id,
            ),
        )
        return True

    def _music_provider(self) -> str:
        return music.resolve_provider(_read_env(self.env_path))

    def _resume_reconcile_publish(
        self, job_id: str, task_id: str, usage: dict, *, initial_delta: int = 0
    ) -> dict:
        """Publish a reconciled asset journal without another provider request."""
        staging = self._controlled_job_asset(
            job_id, usage.get("publish_staging_path"),
            label="远端核对暂存文件", require_file=True,
        )
        target = self._controlled_job_asset(
            job_id, usage.get("publish_target_path"),
            label="远端核对目标文件",
        )
        if not staging or not target:
            raise ValueError("远端核对发布日志不完整，无法安全恢复")
        target.parent.mkdir(parents=True, exist_ok=True)
        publish_copy = self._controlled_job_asset(
            job_id,
            target.parent / f".{target.name}.{secrets.token_hex(8)}.publish",
            label="远端核对发布副本",
        )
        try:
            shutil.copy2(staging, publish_copy)
            with publish_copy.open("rb+") as stream:
                os.fsync(stream.fileno())
            publish_copy.replace(target)
        except Exception:
            publish_copy.unlink(missing_ok=True)
            raise

        completed_usage = dict(usage)
        for key in (
            "publish_staging_path", "publish_target_path",
            "publish_resume_state", "publish_kind",
        ):
            completed_usage.pop(key, None)
            completed_usage["status"] = "completed"
        try:
            self.database.execute("BEGIN IMMEDIATE")
            call_updated = self.database.execute(
                "UPDATE provider_calls SET usage_json=? "
                "WHERE task_id=? AND job_id=?",
                (
                    json.dumps(completed_usage, ensure_ascii=False),
                    str(task_id), job_id,
                ),
            )
            if int(call_updated.rowcount or 0) != 1:
                raise ValueError("远端核对任务已被并发处理")
            production_executor.release_resolved_provider_gates(self.database, job_id)
            unresolved = provider_tasks.unresolved_task_ids(
                self.database, job_id
            )
            review_count = int(self.database.execute(
                "SELECT COUNT(*) FROM shots WHERE job_id=? "
                "AND state='REVIEW_REQUIRED'", (job_id,)
            ).fetchone()[0])
            non_ok_count = int(self.database.execute(
                "SELECT COUNT(*) FROM shots WHERE job_id=? AND state!='OK'",
                (job_id,),
            ).fetchone()[0])
            current_job = self.database.execute(
                "SELECT bgm_path FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if unresolved:
                resume_state = "PROVIDER_ACTION_REQUIRED"
            elif review_count:
                resume_state = "PRODUCTION_REVIEW_REQUIRED"
            elif non_ok_count:
                resume_state = "PRODUCTION_QUEUED"
            elif current_job and current_job["bgm_path"] and (
                self._controlled_job_asset(
                    job_id, current_job["bgm_path"], label="核对后配乐",
                    require_file=True,
                )
            ):
                resume_state = "BGM_OK"
            else:
                resume_state = "SHOTS_OK"
            job_updated = self.database.execute(
                "UPDATE jobs SET state=?,error=NULL,updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=? AND state='PROVIDER_ACTION_REQUIRED'",
                (resume_state, job_id),
            )
            if int(job_updated.rowcount or 0) != 1:
                raise ValueError("远端核对任务已被并发处理")
            self.database.execute(
                "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
                (
                    job_id, "provider_task_reconcile", "info",
                    f"远端任务 {task_id} 的暂存产物已恢复发布到 {resume_state}",
                ),
            )
            self.database.commit()
        except Exception:
            self.database.rollback()
            raise
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        return {
            "job_id": job_id, "task_id": str(task_id),
            "remote_status": "completed", "state": resume_state,
            "cost_delta_cents": max(0, int(initial_delta or 0)),
        }

    def reconcile_provider_task(self, job_id: str, task_id: str) -> dict:
        return self.production_service.reconcile(job_id, task_id)

    def _reconcile_provider_task_impl(self, job_id: str, task_id: str) -> dict:
        """Inspect one known task, persist final evidence, then CAS-resume safely."""
        job = self.database.execute(
            "SELECT state,bgm_path FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        call = self.database.execute(
            "SELECT task_id,shot_idx,provider,intent,cost_cents,usage_json "
            "FROM provider_calls WHERE job_id=? AND task_id=?",
            (job_id, str(task_id)),
        ).fetchone()
        if call is None:
            for candidate in self.database.execute(
                "SELECT task_id,shot_idx,provider,intent,cost_cents,usage_json "
                "FROM provider_calls WHERE job_id=? "
                "AND task_id LIKE 'local-submit:%'",
                (job_id,),
            ).fetchall():
                if provider_tasks.effective_task_id(
                    candidate["task_id"], candidate["usage_json"]
                ) == str(task_id):
                    call = candidate
                    break
        if not job or not call:
            raise ValueError("待核对的远端任务不存在")
        if job["state"] == "FAILED":
            recovery_controller.admit_stopped_provider_reconciliation(
                self.database, job_id, str(task_id), worker_alive=_process_is_alive,
            )
            job = self.database.execute(
                "SELECT state,bgm_path FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        terminal_reconciliation = (
            job["state"] == "REJECTED_PENDING_RECONCILIATION"
        )
        if job["state"] not in {
            "PROVIDER_ACTION_REQUIRED", "REJECTED_PENDING_RECONCILIATION",
        }:
            raise ValueError("当前任务不在模型任务待核对状态")
        provider = str(call["provider"] or "").strip().casefold()
        try:
            existing_usage = json.loads(call["usage_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            existing_usage = {}
        storage_task_id = str(call["task_id"])
        remote_value = existing_usage.get("remote_task_id")
        remote_task_id = (
            remote_value.strip() if isinstance(remote_value, str) else ""
        )
        if storage_task_id.startswith("local-submit:"):
            if not remote_task_id or remote_task_id == storage_task_id:
                raise ValueError(
                    "本地 Provider 预留记录缺少可核对的 remote_task_id；"
                    "已保留证据，请人工核查 Provider 后处置，禁止重新提交"
                )
            provider_tasks.promote_reservation(
                self.database,
                storage_task_id,
                task_id=remote_task_id,
                job_id=job_id,
                shot_idx=int(call["shot_idx"] or 0),
                provider=str(call["provider"] or ""),
                intent=str(call["intent"] or ""),
            )
            task_id = remote_task_id
            call = self.database.execute(
                "SELECT task_id,shot_idx,provider,intent,cost_cents,usage_json "
                "FROM provider_calls WHERE job_id=? AND task_id=?",
                (job_id, remote_task_id),
            ).fetchone()
            existing_usage = json.loads(call["usage_json"] or "{}")
        # A partially promoted task can have both a real row and a local alias.
        # Retire matching aliases even when lookup found the real row first.
        for alias in self.database.execute("SELECT * FROM provider_calls WHERE job_id=? AND task_id LIKE 'local-submit:%'",(job_id,)).fetchall():
            if provider_tasks.effective_task_id(alias['task_id'],alias['usage_json']) == str(task_id):
                provider_tasks.promote_reservation(self.database,alias['task_id'],task_id=str(task_id),
                    job_id=job_id,shot_idx=int(call['shot_idx'] or 0),provider=call['provider'],intent=call['intent'])
        if not terminal_reconciliation:
            from . import accepted_task_recovery
            accepted = accepted_task_recovery.recover(self.database,self.data_dir / 'assets',job_id,str(task_id))
            if accepted is not None:
                return accepted
        if existing_usage.get("publish_staging_path"):
            return self._resume_reconcile_publish(
                job_id, str(task_id), existing_usage
            )
        if not terminal_reconciliation and existing_usage.get('status') in {'completed','failed'}:
            return {'job_id':job_id,'task_id':str(task_id),'remote_status':existing_usage['status'],
                    'state':job['state'],'cost_delta_cents':0}
        if provider in {"seedance", "seedance_scene", "seedance_identity"}:
            inspected = asyncio.run(seedance.inspect_task(str(task_id)))
        elif provider == "suno":
            inspected = asyncio.run(
                music.inspect_task(str(task_id), provider=provider)
            )
        else:
            raise ValueError(f"provider {provider} 不支持远端任务核对")
        if provider in {"seedance_scene", "seedance_identity"} and not terminal_reconciliation:
            from . import anchor_recovery
            return anchor_recovery.reconcile(
                self.database, self.data_dir, job_id, str(task_id), inspected
            )
        status = str(inspected.get("status") or "unknown").strip().casefold()
        if status not in {"completed", "failed"}:
            provider_tasks.mark_action_required(
                self.database, str(task_id),
                message=f"remote status: {status}",
            )
            return {
                "job_id": job_id, "task_id": str(task_id),
                "remote_status": status,
                "state": str(job["state"]),
            }

        if terminal_reconciliation:
            try:
                self.database.execute("BEGIN IMMEDIATE")
                current = self.database.execute(
                    "SELECT state FROM jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                if (
                    not current
                    or current["state"] != "REJECTED_PENDING_RECONCILIATION"
                ):
                    raise ValueError("终止任务的成本核对已被并发处理")
                delta = provider_tasks.upsert_final_call(
                    self.database,
                    task_id=str(task_id),
                    job_id=job_id,
                    shot_idx=int(call["shot_idx"] or 0),
                    provider=provider,
                    intent=str(call["intent"] or ""),
                    status=status,
                    usage=(inspected.get("usage")
                           if isinstance(inspected.get("usage"), dict) else {}),
                    cost_cents=max(0, int(inspected.get("cost_cents") or 0)),
                )
                unresolved = provider_tasks.unresolved_task_ids(
                    self.database, job_id
                )
                next_state = (
                    "REJECTED_PENDING_RECONCILIATION"
                    if unresolved else "REJECTED"
                )
                updated = self.database.execute(
                    "UPDATE jobs SET state=?,cost_cents=cost_cents+?,error=?,"
                    "production_worker_pid=NULL,updated_at=CURRENT_TIMESTAMP "
                    "WHERE job_id=? AND state='REJECTED_PENDING_RECONCILIATION'",
                    (
                        next_state, delta,
                        (
                            "已人工终止，仍有远端成本待核对"
                            if unresolved else "人工终止视频任务；成本已核对"
                        ),
                        job_id,
                    ),
                )
                if int(updated.rowcount or 0) != 1:
                    raise ValueError("终止任务的成本核对已被并发处理")
                self.database.execute(
                    "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
                    (
                        job_id, "provider_task_reconcile", "info",
                        f"已终止任务的远端成本 {task_id} 核对为 {status}",
                    ),
                )
                self.database.commit()
            except Exception:
                self.database.rollback()
                raise
            return {
                "job_id": job_id, "task_id": str(task_id),
                "remote_status": status, "state": next_state,
                "cost_delta_cents": delta,
            }

        shot_idx = int(call["shot_idx"] or 0)
        candidate_path = None
        bgm_path = None
        staged_asset = None
        if provider == "seedance" and status == "completed":
            video = inspected.get("video")
            if not isinstance(video, (bytes, bytearray)) or not video:
                provider_tasks.mark_action_required(
                    self.database, str(task_id),
                    message="completed task missing downloadable video",
                )
                return {
                    "job_id": job_id, "task_id": str(task_id),
                    "remote_status": "unknown",
                    "state": "PROVIDER_ACTION_REQUIRED",
                }
            candidate_path = (
                self.data_dir / "assets" / job_id / "shots" / "review"
                / f"{shot_idx:02d}-reconciled-{str(task_id)[-12:]}.mp4"
            ).resolve()
            staging_dir = candidate_path.parent / "staging"
            staging_dir.mkdir(parents=True, exist_ok=True)
            staged_asset = staging_dir / (
                f".{candidate_path.name}.{secrets.token_hex(8)}.stage"
            )
            with staged_asset.open("xb") as stream:
                stream.write(bytes(video))
                stream.flush()
                os.fsync(stream.fileno())
        if provider == "suno" and status == "completed":
            audio = inspected.get("audio")
            if not isinstance(audio, (bytes, bytearray)) or not audio:
                provider_tasks.mark_action_required(
                    self.database, str(task_id),
                    message="completed task missing downloadable audio",
                )
                return {
                    "job_id": job_id, "task_id": str(task_id),
                    "remote_status": "unknown",
                    "state": "PROVIDER_ACTION_REQUIRED",
                }
            bgm_path = (
                self.data_dir / "assets" / job_id / "bgm.mp3"
            ).resolve()
            staging_dir = bgm_path.parent / "review" / "staging"
            staging_dir.mkdir(parents=True, exist_ok=True)
            staged_asset = staging_dir / (
                f".{bgm_path.name}.{secrets.token_hex(8)}.stage"
            )
            with staged_asset.open("xb") as stream:
                stream.write(bytes(audio))
                stream.flush()
                os.fsync(stream.fileno())

        try:
            self.database.execute("BEGIN IMMEDIATE")
            current = self.database.execute(
                "SELECT state FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if not current or current["state"] != "PROVIDER_ACTION_REQUIRED":
                raise ValueError("模型任务已被其他请求处理")
            delta = provider_tasks.upsert_final_call(
                self.database,
                task_id=str(task_id),
                job_id=job_id,
                shot_idx=shot_idx,
                provider=provider,
                intent=str(call["intent"] or ""),
                status=status,
                usage=(inspected.get("usage")
                       if isinstance(inspected.get("usage"), dict) else {}),
                cost_cents=max(0, int(inspected.get("cost_cents") or 0)),
            )
            if provider == "seedance" and shot_idx:
                shot = self.database.execute(
                    "SELECT state FROM shots WHERE job_id=? AND idx=?",
                    (job_id, shot_idx),
                ).fetchone()
                if shot and shot["state"] != "OK":
                    if status == "completed":
                        qa = json.dumps({
                            "status": "manual_review_required",
                            "reason": "远端任务已核对完成，候选需人工复核",
                            "reasons": ["provider_task_reconciled"],
                            "candidate_path": str(candidate_path),
                            "candidate_url": inspected.get("video_url"),
                            "provider_calls": [{
                                "task_id": str(task_id),
                                "usage": inspected.get("usage") or {},
                                "cost_cents": max(0, int(
                                    inspected.get("cost_cents") or 0
                                )),
                            }],
                        }, ensure_ascii=False)
                        self.database.execute(
                            "UPDATE shots SET state='REVIEW_REQUIRED',video_qa=?,"
                            "error=?,provider='seedance',cost_cents=cost_cents+? "
                            "WHERE job_id=? AND idx=?",
                            (
                                qa, "远端任务已完成，等待人工复核", delta,
                                job_id, shot_idx,
                            ),
                        )
                        self._sync_graph_reconciled_master_candidate(
                            job_id,
                            shot_idx,
                            task_id=str(task_id),
                            candidate_path=candidate_path,
                            candidate_url=str(inspected.get("video_url") or ""),
                        )
                    else:
                        self.database.execute(
                            "UPDATE shots SET state='PENDING',error=NULL "
                            "WHERE job_id=? AND idx=?",
                            (job_id, shot_idx),
                        )
            if bgm_path:
                self.database.execute(
                    "UPDATE jobs SET bgm_path=? WHERE job_id=?",
                    (str(bgm_path), job_id),
                )

            review_count = int(self.database.execute(
                "SELECT COUNT(*) FROM shots WHERE job_id=? "
                "AND state='REVIEW_REQUIRED'", (job_id,)
            ).fetchone()[0])
            non_ok_count = int(self.database.execute(
                "SELECT COUNT(*) FROM shots WHERE job_id=? AND state!='OK'",
                (job_id,),
            ).fetchone()[0])
            unresolved = provider_tasks.unresolved_task_ids(self.database, job_id)
            if unresolved:
                next_state = "PROVIDER_ACTION_REQUIRED"
            elif review_count:
                next_state = "PRODUCTION_REVIEW_REQUIRED"
            elif non_ok_count:
                next_state = "PRODUCTION_QUEUED"
            elif bgm_path or (
                job["bgm_path"]
                and self._controlled_job_asset(
                    job_id, job["bgm_path"], label="历史配乐",
                    require_file=True,
                )
            ):
                next_state = "BGM_OK"
            else:
                next_state = "SHOTS_OK"
            persisted_state = next_state
            job_error = None
            if staged_asset:
                target = candidate_path or bgm_path
                if target is None:
                    raise RuntimeError("远端任务核对缺少受控落盘目标")
                journal_usage = (
                    dict(inspected.get("usage"))
                    if isinstance(inspected.get("usage"), dict) else {}
                )
                journal_usage.update({
                    "status": "action_required",
                    "publish_staging_path": str(staged_asset),
                    "publish_target_path": str(target),
                    "publish_resume_state": next_state,
                    "publish_kind": provider,
                })
                call_updated = self.database.execute(
                    "UPDATE provider_calls SET usage_json=? "
                    "WHERE task_id=? AND job_id=?",
                    (
                        json.dumps(journal_usage, ensure_ascii=False),
                        str(task_id), job_id,
                    ),
                )
                if int(call_updated.rowcount or 0) != 1:
                    raise ValueError("模型任务已被其他请求处理")
                persisted_state = "PROVIDER_ACTION_REQUIRED"
                job_error = "远端任务已核对，等待原子发布暂存产物"
            updated = self.database.execute(
                "UPDATE jobs SET state=?,cost_cents=cost_cents+?,error=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE job_id=? "
                "AND state='PROVIDER_ACTION_REQUIRED'",
                (persisted_state, delta, job_error, job_id),
            )
            if int(updated.rowcount or 0) != 1:
                raise ValueError("模型任务已被其他请求处理")
            self.database.execute(
                "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
                (
                    job_id, "provider_task_reconcile", "info",
                    f"远端任务 {task_id} 已核对为 {status}，恢复到 {next_state}",
                ),
            )
            self.database.commit()
        except Exception:
            self.database.rollback()
            if staged_asset:
                staged_asset.unlink(missing_ok=True)
            raise
        if staged_asset:
            journal = json.loads(self.database.execute(
                "SELECT usage_json FROM provider_calls "
                "WHERE task_id=? AND job_id=?",
                (str(task_id), job_id),
            ).fetchone()[0])
            return self._resume_reconcile_publish(
                job_id, str(task_id), journal, initial_delta=delta
            )
        return {
            "job_id": job_id, "task_id": str(task_id),
            "remote_status": status, "state": next_state,
            "cost_delta_cents": delta,
        }

    def reconcile_provider_tasks(self, *, limit: int = 10) -> dict:
        """Inspect a bounded set of known remote tasks without any paid submit."""
        bounded_limit = max(1, min(50, int(limit or 10)))
        rows = self.database.execute(
            "SELECT p.task_id,p.job_id,p.usage_json "
            "FROM provider_calls p JOIN jobs j ON j.job_id=p.job_id "
            "WHERE j.state IN "
            "('PROVIDER_ACTION_REQUIRED','REJECTED_PENDING_RECONCILIATION') "
            "OR (j.state='FAILED' AND j.execution_engine='graph_v2' "
            "AND COALESCE(j.failure_code,'') NOT IN "
            "('input_privacy_rejected','output_policy_rejected',"
            "'output_copyright_rejected')) "
            "ORDER BY p.created_at,p.task_id"
        ).fetchall()
        report = {
            "last_sweep_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "checked": 0,
            "completed": 0,
            "pending": 0,
            "failed": 0,
            "skipped": 0,
            "items": [],
        }
        for row in rows:
            try:
                usage = json.loads(row["usage_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                usage = {}
            status = str(
                usage.get("status") if isinstance(usage, dict) else ""
            ).strip().casefold()
            if status not in provider_tasks.UNRESOLVED_STATUSES:
                continue
            storage_task_id = str(row["task_id"])
            remote_task_id = provider_tasks.effective_task_id(
                storage_task_id, row["usage_json"]
            )
            if (
                storage_task_id.startswith("local-submit:")
                and remote_task_id == storage_task_id
            ):
                report["skipped"] += 1
                continue
            if report["checked"] >= bounded_limit:
                break
            report["checked"] += 1
            item = {
                "job_id": str(row["job_id"]),
                "task_id": remote_task_id,
            }
            try:
                result = self.reconcile_provider_task(
                    str(row["job_id"]), remote_task_id
                )
                remote_status = str(
                    result.get("remote_status") or "unknown"
                ).strip().casefold()
                item.update({
                    "outcome": (
                        "completed"
                        if remote_status in {"completed", "failed"}
                        else "pending"
                    ),
                    "remote_status": remote_status,
                    "state": result.get("state"),
                })
                report[item["outcome"]] += 1
            except Exception as exc:
                report["failed"] += 1
                item.update({
                    "outcome": "failed",
                    "message": str(exc)[:300],
                })
            report["items"].append(item)
        self._last_provider_sweep = report
        return dict(report)

    def _repair_dangling_job_links(self) -> list[str]:
        """Return approved drafts with a deleted production job to review."""
        rows = self.database.execute(
            "SELECT d.draft_id,d.approved_job_id,b.approved_topic_id,"
            "b.recommended_title,b.brief_json FROM script_drafts d "
            "JOIN topic_briefs b ON b.brief_id=d.brief_id "
            "LEFT JOIN jobs j ON j.job_id=d.approved_job_id "
            "WHERE d.status='approved' AND d.approved_job_id IS NOT NULL "
            "AND j.job_id IS NULL"
        ).fetchall()
        draft_ids = [str(row["draft_id"]) for row in rows]
        if draft_ids:
            placeholders = ",".join("?" for _ in draft_ids)
            self.database.execute(
                f"UPDATE script_drafts SET status='candidate',"
                f"approved_job_id=NULL,storyboard_json=NULL,"
                f"updated_at=CURRENT_TIMESTAMP WHERE draft_id IN ({placeholders})",
                draft_ids,
            )
            self.database.commit()
        return draft_ids

    def _reconcile_orphaned_production_workers(self) -> list[str]:
        """Return persisted running jobs without a live process to the queue."""
        recovered: list[str] = []
        rows = self.database.execute(
            "SELECT job_id,production_worker_pid FROM jobs "
            "WHERE state='PRODUCTION_RUNNING'"
        ).fetchall()
        for row in rows:
            local_worker = self._production_workers.get(str(row["job_id"]))
            if local_worker is not None and local_worker.poll() is None:
                continue
            if _process_is_alive(row["production_worker_pid"]):
                continue
            outcome = recovery_controller.reconcile_job(
                self.database,
                str(row["job_id"]),
                worker_alive=_process_is_alive,
            )
            if outcome.action != "requeue":
                continue
            recovered.append(str(row["job_id"]))
            self.database.execute(
                "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
                (
                    row["job_id"], "production_queue", "warning",
                    "检测到生产进程已中断，任务已自动恢复排队",
                ),
            )
        if recovered:
            self.database.commit()
        return recovered

    def record_queue_supervisor_error(self, message: str) -> None:
        self.database.execute(
            "INSERT INTO events(job_id,step,level,message) VALUES(NULL,?,?,?)",
            ("production_queue", "error", str(message or "队列调度失败")),
        )
        self.database.commit()

    def production_health(self, *, supervisor_alive: bool) -> dict:
        running = _rows(self.database.execute(
            "SELECT job_id,title,state,production_started_at,updated_at FROM jobs "
            "WHERE state='PRODUCTION_RUNNING' "
            "ORDER BY production_started_at ASC,job_id ASC"
        ))
        queued = _rows(self.database.execute(
            "SELECT job_id,title,state,created_at,updated_at FROM jobs "
            "WHERE state='PRODUCTION_QUEUED' "
            "ORDER BY priority DESC,created_at ASC,job_id ASC"
        ))
        recovered = self.database.execute(
            "SELECT COUNT(*) FROM events WHERE step='production_queue' "
            "AND level='warning' AND message LIKE '%自动恢复排队%'"
        ).fetchone()[0]
        error = self.database.execute(
            "SELECT message FROM events WHERE step='production_queue' "
            "AND level='error' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        environment = _read_env(self.env_path)
        try:
            configured_master_limit = max(1, int(
                environment.get("SEEDANCE_MAX_CONCURRENT_MASTERS")
                or environment.get("MAX_CONCURRENT_SHOTS")
                or 3
            ))
        except (TypeError, ValueError):
            configured_master_limit = 3
        return {
            "supervisor_alive": bool(supervisor_alive),
            "concurrency_limit": self._production_limit(),
            "running": running,
            "queued": queued,
            "orphaned_recovered": int(recovered or 0),
            "last_error": str(error["message"]) if error else None,
            "master_capacity": provider_capacity.status(
                self.database, "seedance",
                configured_limit=configured_master_limit,
            ),
            "provider_recovery": dict(self._last_provider_sweep),
            "transport_preflight": transport_diagnostics.load_report(
                self.transport_diagnostic_path
            ),
        }

    def run_transport_preflight(self) -> dict:
        """Run read-only network checks and persist only sanitized evidence."""
        environment = dict(os.environ)
        environment.update(_read_env(self.env_path))
        try:
            timeout = float(
                environment.get("PROVIDER_PREFLIGHT_TIMEOUT_SEC") or 4.0
            )
        except (TypeError, ValueError):
            timeout = 4.0
        timeout = max(1.0, min(timeout, 10.0))
        return transport_diagnostics.run_preflight(
            timeout=timeout,
            environment=environment,
            report_path=self.transport_diagnostic_path,
        )

    def _require_transport_preflight(self) -> dict | None:
        if not self.transport_preflight_enabled:
            return None
        report = self.run_transport_preflight()
        if not report.get("ready"):
            raise transport_diagnostics.TransportPreflightError(report)
        return report

    def production_consistency(self, *, limit: int = 100) -> dict:
        from . import production_projection

        reports = production_projection.consistency_reports(
            self.database, limit=limit,
        )
        affected = [report.as_dict() for report in reports if report.differences]
        return {
            "checked_count": len(reports),
            "affected_count": len(affected),
            "affected_jobs": affected,
        }

    def assign_execution_engine(self, job_id: str, engine: str) -> dict:
        return self.production_service.assign_execution_engine(job_id, engine)

    def compile_video_job(self, job_id: str) -> dict:
        """Compile and explain v2 production without any provider submission."""
        return self.production_service.compile(job_id)

        # Kept temporarily below as a rollback reference during the gray period.
        from . import production_graph

        job = self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not job:
            raise ValueError("视频任务不存在")
        try:
            runtime_storyboard = json.loads(job["storyboard"] or "{}")
        except json.JSONDecodeError:
            runtime_storyboard = {}
        runtime_settings = runtime_storyboard.setdefault(
            "production_settings", {}
        )
        runtime_settings.setdefault(
            "music_provider",
            music.resolve_provider(configured=_read_env(self.env_path)),
        )

        compilation = production_planner.compile_job_spec(
            self.database,
            job_id,
            self.root,
            storyboard_override=runtime_storyboard,
            persist=False,
            target_version=2,
        )
        issue_payload = [{
            "code": issue.code,
            "severity": issue.severity,
            "stage": issue.stage,
            "subject": issue.subject,
            "message": issue.message,
        } for issue in compilation.issues]
        if compilation.spec is None:
            return {
                "job_id": job_id,
                "compilable": False,
                "blockers": [
                    issue for issue in issue_payload
                    if issue["severity"] == "blocking"
                ],
                "decisions": [
                    issue for issue in issue_payload
                    if issue["severity"] == "decision"
                ],
                "required_content": list(compilation.required_content),
            }
        spec = compilation.spec
        production_graph.persist_graph(self.database, spec)
        adaptation_actions = list(dict.fromkeys(
            action for master in spec.masters
            for action in master.adaptation_actions
        ))
        preserved = list(dict.fromkeys(
            item for master in spec.masters
            for item in master.preserved_requirements
        ))
        node_counts = Counter(node.node_type for node in spec.nodes)
        return {
            "job_id": job_id,
            "compilable": True,
            "spec_hash": spec.spec_hash,
            "version": spec.version,
            "mode": spec.mode,
            "reliability_commitment": spec.reliability_commitment,
            "complexity": dict(spec.complexity),
            "required_content": list(compilation.required_content),
            "preserved_content": preserved,
            "adaptation_actions": adaptation_actions,
            "blockers": [],
            "decisions": [
                issue for issue in issue_payload
                if issue["severity"] == "decision"
            ],
            "estimated_cost_cents": spec.estimated_cost_cents,
            "budget_cents": spec.budget_cents,
            "master_count": len(spec.masters),
            "node_counts": dict(node_counts),
            "nodes": [
                {
                    "node_id": node.node_id,
                    "node_type": node.node_type,
                    "paid": node.paid,
                    "estimated_cost_cents": node.estimated_cost_cents,
                    "quality_gates": list(node.quality_gates),
                }
                for node in spec.nodes
            ],
            "edges": [
                {
                    "from": edge.from_node_id,
                    "to": edge.to_node_id,
                    "type": edge.edge_type,
                }
                for edge in spec.edges
            ],
        }

    def production_graph_detail(self, job_id: str) -> dict:
        return self.production_read_model.graph_detail(job_id)

        # Kept temporarily below as a rollback reference during the gray period.
        nodes = _rows(self.database.execute(
            "SELECT node_id,node_type,state,paid,estimated_cost_cents,"
            "actual_cost_cents,provider_task_id,error_code,next_action "
            "FROM production_nodes WHERE job_id=? ORDER BY node_type,node_id",
            (job_id,),
        ))
        if not nodes:
            raise ValueError("该任务尚未完成生产预检")
        edges = _rows(self.database.execute(
            "SELECT from_node_id,to_node_id,edge_type FROM production_edges "
            "WHERE job_id=? ORDER BY from_node_id,to_node_id",
            (job_id,),
        ))
        return {"job_id": job_id, "nodes": nodes, "edges": edges}

    def _clear_stale_success_errors(self) -> list[str]:
        rows = self.database.execute(
            "SELECT job_id,error FROM jobs WHERE state IN "
            "('COMPOSED','READY_TO_PUBLISH','PUBLISHED') "
            "AND error IS NOT NULL AND TRIM(error)<>''"
        ).fetchall()
        repaired = [str(row["job_id"]) for row in rows]
        for row in rows:
            self.database.execute(
                "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
                (
                    row["job_id"], "state_repair", "warning",
                    f"已归档完成任务的历史错误：{str(row['error'])[:500]}",
                ),
            )
        if repaired:
            placeholders = ",".join("?" for _ in repaired)
            self.database.execute(
                f"UPDATE jobs SET error=NULL WHERE job_id IN ({placeholders})",
                repaired,
            )
            self.database.commit()
        return repaired

    def overview(self) -> dict:
        source_count = self.database.execute(
            "SELECT COUNT(*) FROM script_sources"
        ).fetchone()[0]
        brief_count = self.database.execute(
            "SELECT COUNT(*) FROM topic_briefs"
        ).fetchone()[0]
        draft_count = self.database.execute(
            "SELECT COUNT(*) FROM script_drafts"
        ).fetchone()[0]
        jobs = self.database.execute(
            "SELECT j.job_id,j.state,j.cost_cents,j.storyboard FROM jobs j WHERE NOT ("
            "j.state='PENDING' AND NOT EXISTS (SELECT 1 FROM script_drafts d "
            "WHERE d.approved_job_id=j.job_id AND d.status='approved') "
            "AND NOT EXISTS (SELECT 1 FROM shots s WHERE s.job_id=j.job_id))"
        ).fetchall()
        job_ids = {str(row["job_id"]) for row in jobs}
        job_state_by_id = {
            str(row["job_id"]): str(row["state"]) for row in jobs
        }
        manual_review_job_ids: set[str] = set()
        manual_review_failure_job_ids: set[str] = set()
        for row in self.database.execute(
            "SELECT job_id,video_qa FROM shots WHERE video_qa IS NOT NULL"
        ).fetchall():
            job_id = str(row["job_id"])
            if job_state_by_id.get(job_id) not in {
                "FAILED", "BLOCKED", "PRODUCTION_REVIEW_REQUIRED",
            }:
                continue
            try:
                qa = json.loads(row["video_qa"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if qa.get("status") == "manual_review_required":
                manual_review_job_ids.add(job_id)
                if job_state_by_id.get(job_id) in {"FAILED", "BLOCKED"}:
                    manual_review_failure_job_ids.add(job_id)
        tracked_by_job = {
            str(row["job_id"]): int(row["tracked_cost_cents"] or 0)
            for row in self.database.execute(
                "SELECT job_id,SUM(cost_cents) AS tracked_cost_cents "
                "FROM provider_calls GROUP BY job_id"
            ).fetchall()
            if str(row["job_id"]) in job_ids
        }
        total_cost_cents = sum(int(row["cost_cents"] or 0) for row in jobs)
        tracked_call_cost_cents = sum(tracked_by_job.values())
        cost_delta_cents = total_cost_cents - tracked_call_cost_cents
        states = Counter(str(row["state"]) for row in jobs)
        publication_rows = self.database.execute(
            "SELECT job_id,metadata_json FROM platform_publications"
        ).fetchall()
        publication_count = len(publication_rows)
        verified_job_ids = {
            str(row["job_id"]) for row in publication_rows
            if not _publication_is_pending(row["metadata_json"])
        }
        published_count = len({
            str(row["job_id"]) for row in self.database.execute(
                "SELECT job_id FROM jobs WHERE state='PUBLISHED'"
            ).fetchall()
            if str(row["job_id"]) in verified_job_ids
        })
        legacy_unverified = self.database.execute(
            "SELECT COUNT(*) FROM jobs j WHERE j.state='PUBLISHED' "
            "AND NOT EXISTS (SELECT 1 FROM platform_publications p "
            "WHERE p.job_id=j.job_id)"
        ).fetchone()[0]
        snapshots = self.database.execute(
            "SELECT COUNT(*) FROM performance_snapshots"
        ).fetchone()[0]
        active_characters = self.database.execute(
            "SELECT COUNT(*) FROM character_asset_bindings WHERE status='Active'"
        ).fetchone()[0]
        ready_to_publish = states.get("READY_TO_PUBLISH", 0)
        failures_total = states.get("FAILED", 0) + states.get("BLOCKED", 0)
        metric_now = datetime.now(timezone.utc)
        reliability_daily = production_metrics.reliability_trends(
            self.database, days=30, now=metric_now,
        )
        reliability_windows = {
            "7d": production_metrics.reliability_summary(
                self.database,
                since=(metric_now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
                now=metric_now,
            ),
            "30d": production_metrics.reliability_summary(
                self.database,
                since=(metric_now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
                now=metric_now,
            ),
        }
        canary_scorecard = quality_canary.summarize_acceptance_records(
            quality_canary.load_acceptance_records(
                self.root, data_dir=self.data_dir
            )
        )
        production_compiler_v2 = production_metrics.production_v2_summary(
            self.database
        )
        return {
            "scripts": {
                "sources": source_count,
                "briefs": brief_count,
                "drafts": draft_count,
            },
            "videos": {
                "jobs": len(jobs),
                "active": sum(states.get(state, 0) for state in (
                    "PENDING", "STORYBOARDED", "PRODUCTION_QUEUED",
                    "PRODUCTION_RUNNING", "ANCHOR_REVIEW_REQUIRED",
                    "SHOTS_OK", "BGM_OK"
                )),
                "completed": (
                    states.get("COMPOSED", 0) + states.get("READY_TO_PUBLISH", 0)
                    + states.get("PUBLISHED", 0)
                ),
                "review_required": len(manual_review_job_ids),
                "failed": max(
                    0, failures_total - len(manual_review_failure_job_ids)
                ),
                "failures_total": failures_total,
                "cost_cents": total_cost_cents,
                "tracked_call_cost_cents": tracked_call_cost_cents,
                "unallocated_cost_cents": max(0, cost_delta_cents),
                "cost_delta_cents": cost_delta_cents,
                "reconciliation_status": (
                    "reconciled" if cost_delta_cents == 0 else "partial"
                ),
                "cost_estimation": _cost_estimation_calibration(
                    self.database, jobs
                ),
                "states": dict(states),
            },
            "publishing": {
                "ready": ready_to_publish,
                "mapped": publication_count,
                "published": published_count,
                "legacy_unverified": legacy_unverified,
                "snapshots": snapshots,
            },
            "configuration": {
                "active_characters": active_characters,
                "readiness": self._readiness(),
            },
            "reliability": {
                "windows": reliability_windows,
                "daily": reliability_daily,
                "release_gate": quality_canary.evaluate_release_gate(
                    reliability_daily,
                ),
                "canary_scorecard": canary_scorecard,
                "production_compiler_v2": production_compiler_v2,
                "canary_profiles": [{
                    "id": str(profile["id"]),
                    "duration_sec": int(profile["duration_sec"]),
                    "budget_cents": int(profile["budget_cents"]),
                    "character_count": len(profile.get("characters") or []),
                } for profile in quality_canary.load_frozen_profiles()],
            },
        }

    def create_quality_canary(self, payload: dict) -> dict:
        """Create an isolated, user-confirmed canary job without paid calls."""
        profile_id = str(payload.get("profile_id") or "").strip()
        contract = quality_canary.prepare_explicit_run(
            profile_id,
            confirmed=payload.get("confirmed") is True,
            budget_cents=int(payload.get("budget_cents") or 0),
        )
        storyboard = quality_canary.build_isolated_storyboard(profile_id, contract)
        s1_storyboard._validate(storyboard)
        token = secrets.token_hex(6)
        job_id = f"J-CANARY-{token}"
        topic_id = f"CANARY-{token}"
        title = f"质量金丝雀 · {profile_id}"
        characters = storyboard["characters"]
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,batch_id,state,storyboard,"
            "characters_json,cost_cents) VALUES(?,?,?,?,?,?,?,0)",
            (
                job_id, topic_id, title,
                f"CANARY-{datetime.now(timezone.utc).date().isoformat()}",
                "STORYBOARDED",
                json.dumps(storyboard, ensure_ascii=False),
                json.dumps(characters, ensure_ascii=False),
            ),
        )
        for master in storyboard.get("masters") or []:
            self.database.execute(
                "INSERT INTO shots(job_id,idx,duration_sec,shot_type,camera,prompt,"
                "voiceover,subtitle,state,narrative_type,speaker_id,anchor_character) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    int(master.get("idx") or 0),
                    float(master.get("duration") or 0),
                    str(master.get("shot_type") or ""),
                    str(master.get("camera") or ""),
                    str(master.get("scene") or master.get("prompt") or ""),
                    str(master.get("voiceover") or ""),
                    str(master.get("subtitle") or master.get("voiceover") or ""),
                    "PENDING",
                    str(master.get("type") or "dialogue"),
                    master.get("speaker"),
                    master.get("anchor_character"),
                ),
            )
        self.database.execute(
            "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
            (
                job_id, "quality_canary", "info",
                "已创建隔离质量金丝雀；尚未调用 Provider，开始生成前仍需确认制作预算",
            ),
        )
        self.database.commit()
        return {
            "job_id": job_id,
            "state": "STORYBOARDED",
            "title": title,
            "contract": contract,
            "provider_calls": 0,
        }

    def todo_center(self) -> dict:
        content_items: list[dict] = []
        for row in self.database.execute(
            "SELECT candidate_id,title FROM source_candidates "
            "WHERE status='candidate' ORDER BY created_at LIMIT 30"
        ).fetchall():
            content_items.append({
                "id": str(row["candidate_id"]), "title": str(row["title"]),
                "kind": "candidate", "page": "scripts", "stage": "candidates",
            })
        for row in self.database.execute(
            "SELECT s.source_id,s.title FROM script_sources s WHERE NOT EXISTS ("
            "SELECT 1 FROM topic_brief_sources r WHERE r.source_id=s.source_id) "
            "ORDER BY s.created_at LIMIT 30"
        ).fetchall():
            content_items.append({
                "id": str(row["source_id"]),
                "title": str(row["title"] or "未命名来源"),
                "kind": "source", "page": "scripts", "stage": "sources",
            })
        for row in self.database.execute(
            "SELECT b.brief_id,b.recommended_title FROM topic_briefs b "
            "WHERE NOT EXISTS (SELECT 1 FROM script_drafts d WHERE d.brief_id=b.brief_id) "
            "ORDER BY b.created_at LIMIT 30"
        ).fetchall():
            content_items.append({
                "id": str(row["brief_id"]),
                "title": str(row["recommended_title"] or "未命名简报"),
                "kind": "brief", "page": "scripts", "stage": "briefs",
            })
        for row in self.database.execute(
            "SELECT draft_id,script_json FROM script_drafts "
            "WHERE status='candidate' ORDER BY created_at LIMIT 60"
        ).fetchall():
            try:
                script = json.loads(row["script_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                script = {}
            content_items.append({
                "id": str(row["draft_id"]),
                "title": str(script.get("title") or row["draft_id"]),
                "kind": "draft", "page": "scripts", "stage": "drafts",
            })

        def job_items(state: str, *, kind: str, stage: str) -> list[dict]:
            return [{
                "id": str(row["job_id"]),
                "title": str(row["title"] or row["job_id"]),
                "kind": kind, "page": "videos", "stage": stage,
            } for row in self.database.execute(
                "SELECT job_id,title FROM jobs WHERE state=? "
                "ORDER BY updated_at LIMIT 50", (state,),
            ).fetchall()]

        manual_review_ids: list[str] = []
        for row in self.database.execute(
            "SELECT job_id,video_qa FROM shots WHERE video_qa IS NOT NULL "
            "ORDER BY job_id,idx"
        ).fetchall():
            try:
                qa = json.loads(row["video_qa"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if qa.get("status") == "manual_review_required":
                manual_review_ids.append(str(row["job_id"]))
        manual_review_ids = list(dict.fromkeys(manual_review_ids))
        manual_review_items: list[dict] = []
        if manual_review_ids:
            placeholders = ",".join("?" for _ in manual_review_ids)
            titles = {
                str(row["job_id"]): str(row["title"] or row["job_id"])
                for row in self.database.execute(
                    f"SELECT job_id,title FROM jobs WHERE job_id IN ({placeholders}) "
                    "AND state IN ('FAILED','PRODUCTION_REVIEW_REQUIRED')",
                    manual_review_ids,
                ).fetchall()
            }
            manual_review_items = [{
                "id": job_id, "title": titles.get(job_id, job_id),
                "kind": "master_review", "page": "videos", "stage": "REVIEW_REQUIRED",
            } for job_id in manual_review_ids if job_id in titles]
        represented_review_ids = {item["id"] for item in manual_review_items}
        manual_review_items.extend(
            item for item in job_items(
                "ANCHOR_REVIEW_REQUIRED",
                kind="anchor_review",
                stage="ANCHOR_REVIEW_REQUIRED",
            )
            if item["id"] not in represented_review_ids
        )

        acceptance_items = []
        for item in job_items("COMPOSED", kind="final_acceptance", stage="COMPOSED"):
            latest = self.database.execute(
                "SELECT decision FROM review_events WHERE job_id=? "
                "ORDER BY id DESC LIMIT 1", (item["id"],),
            ).fetchone()
            if not latest or str(latest["decision"]) != "approved":
                acceptance_items.append(item)

        performance_items = [{
            "id": str(item["publication_id"]),
            "title": str(item.get("title") or item["publication_id"]),
            "kind": "performance", "page": "publishing", "stage": "performance",
            "window_hours": int(item["window_hours"]),
            "due_at": str(item["due_at"]),
        } for item in performance_feedback.performance_due_tasks(self.database)]
        provider_action_items = job_items(
            "PROVIDER_ACTION_REQUIRED",
            kind="provider_action",
            stage="PROVIDER_ACTION_REQUIRED",
        )
        provider_action_items.extend(job_items(
            "REJECTED_PENDING_RECONCILIATION",
            kind="provider_action",
            stage="REJECTED_PENDING_RECONCILIATION",
        ))
        groups = [
            ("content_decision", "需内容决策", content_items),
            ("production_setup", "需制作设置", job_items(
                "STORYBOARDED", kind="production_setup", stage="STORYBOARDED"
            )),
            ("manual_review", "需人工复核", manual_review_items),
            ("provider_action", "需模型核对", provider_action_items),
            ("final_acceptance", "需成片验收", acceptance_items),
            ("publish", "需发布", job_items(
                "READY_TO_PUBLISH", kind="publish", stage="READY_TO_PUBLISH"
            )),
            ("performance", "需绩效录入", performance_items),
        ]
        return {
            "total": sum(len(items) for _, _, items in groups),
            "groups": [
                {"key": key, "title": title, "count": len(items), "items": items}
                for key, title, items in groups
            ],
        }

    def scripts(self) -> dict:
        sources = _rows(self.database.execute(
            "SELECT source_id,source_type,title,source_uri,content,metadata_json,created_at "
            "FROM script_sources ORDER BY created_at DESC LIMIT 100"
        ))
        for source in sources:
            uri = str(source.get("source_uri") or "")
            source_type = str(source.get("source_type") or "")
            if source_type in {"video", "audio"} or urlparse(uri).scheme in {"", "file"}:
                source["source_display"] = "人工上传"
            elif uri.startswith(("manual://", "competitor://")):
                source["source_display"] = "人工创建"
            else:
                source["source_display"] = uri
            try:
                source["metadata"] = json.loads(source.pop("metadata_json") or "{}")
            except json.JSONDecodeError:
                source["metadata"] = {}
        briefs = _rows(self.database.execute(
            "SELECT brief_id,status,recommended_title,brief_json,approved_job_id,"
            "approved_angle_index,revision,created_at,updated_at "
            "FROM topic_briefs ORDER BY created_at DESC LIMIT 40"
        ))
        for brief in briefs:
            try:
                brief["brief"] = json.loads(brief.pop("brief_json") or "{}")
            except json.JSONDecodeError:
                brief["brief"] = {}
        drafts = _rows(self.database.execute(
            "SELECT draft_id,brief_id,variant_index,status,recommended_index,"
            "approved_job_id,script_json,recommendation_reason,revision,created_at,"
            "updated_at FROM script_drafts "
            "ORDER BY created_at DESC,variant_index LIMIT 60"
        ))
        for draft in drafts:
            try:
                draft["script"] = json.loads(draft.pop("script_json") or "{}")
            except json.JSONDecodeError:
                draft["script"] = {}
        return {"sources": sources, "briefs": briefs, "drafts": drafts}

    def source_feeds(self) -> dict:
        return {"feeds": source_scheduler.list_feeds(self.database)}

    def create_source_feed(self, payload: dict) -> dict:
        return source_scheduler.create_feed(
            self.database,
            name=str(payload.get("name") or ""),
            feed_type=str(payload.get("feed_type") or "rss"),
            endpoint=str(payload.get("endpoint") or ""),
            source_kind=str(payload.get("source_kind") or "hotspot"),
            enabled=payload.get("enabled") is not False,
            authorization_confirmed=payload.get("authorization_confirmed") is not False,
            interval_minutes=payload.get("interval_minutes") or 60,
        )

    def update_source_feed(self, feed_id: str, payload: dict) -> dict:
        return source_scheduler.update_feed(
            self.database,
            feed_id,
            enabled=payload.get("enabled") is not False,
            interval_minutes=payload.get("interval_minutes") or 60,
        )

    def collect_source_feed(self, feed_id: str) -> dict:
        result = source_scheduler.collect_feed(self.database, feed_id)
        if result.get("error"):
            raise ValueError(f"采集失败：{result['error']}")
        return result

    def collect_enabled_source_feeds(self) -> list[dict]:
        return source_scheduler.collect_enabled_feeds(self.database)

    def source_candidates(self, status: str | None = "candidate") -> dict:
        return {
            "candidates": source_scheduler.list_candidates(
                self.database, status=status
            )
        }

    def promote_source_candidate(self, candidate_id: str) -> dict:
        return source_scheduler.promote_candidate(self.database, candidate_id)

    def reject_source_candidate(self, candidate_id: str, payload: dict) -> dict:
        return source_scheduler.reject_candidate(
            self.database,
            candidate_id,
            reason=str(payload.get("reason") or ""),
        )

    def delete_source_candidate(self, candidate_id: str) -> dict:
        deleted = source_scheduler.delete_candidate(self.database, candidate_id)
        if not deleted:
            raise ValueError("仅待处理候选可删除，或该候选已不存在")
        return {"candidate_id": candidate_id, "deleted": True}

    def batch_delete_source_candidates(self, candidate_ids: list[str]) -> dict:
        normalized = list(dict.fromkeys(
            str(candidate_id or "").strip() for candidate_id in candidate_ids
            if str(candidate_id or "").strip()
        ))
        if not normalized:
            raise ValueError("请选择需要删除的自动选题候选")
        if len(normalized) > 100:
            raise ValueError("单次最多删除 100 条自动选题候选")
        placeholders = ",".join("?" for _ in normalized)
        rows = self.database.execute(
            f"SELECT candidate_id,status FROM source_candidates "
            f"WHERE candidate_id IN ({placeholders})",
            normalized,
        ).fetchall()
        candidates = {str(row["candidate_id"]): str(row["status"]) for row in rows}
        invalid = [
            candidate_id for candidate_id in normalized
            if candidates.get(candidate_id) != "candidate"
        ]
        if invalid:
            raise ValueError("以下候选不存在或已处理：" + "、".join(invalid))
        self.database.execute(
            f"DELETE FROM source_candidates WHERE candidate_id IN ({placeholders})",
            normalized,
        )
        self.database.commit()
        return {"deleted": len(normalized), "candidate_ids": normalized}

    def delete_source_feed(self, feed_id: str) -> dict:
        deleted = source_scheduler.delete_feed(self.database, feed_id)
        if not deleted:
            raise ValueError("订阅源不存在")
        return {"feed_id": feed_id, "deleted": True}

    def content_projects(self) -> dict:
        """Return one user-facing content object across every internal stage."""
        projects: list[dict] = []
        source_rows = self.database.execute(
            "SELECT source_id,source_type,title,source_uri,content,created_at "
            "FROM script_sources ORDER BY created_at DESC"
        ).fetchall()
        for row in source_rows:
            source = dict(row)
            link = self.database.execute(
                "SELECT b.brief_id,b.status,b.recommended_title,b.brief_json,b.approved_job_id "
                "FROM topic_briefs b JOIN topic_brief_sources r ON r.brief_id=b.brief_id "
                "WHERE r.source_id=? ORDER BY b.created_at DESC LIMIT 1",
                (source["source_id"],),
            ).fetchone()
            brief = dict(link) if link else None
            if brief:
                try:
                    brief["brief"] = json.loads(brief.pop("brief_json") or "{}")
                except json.JSONDecodeError:
                    brief["brief"] = {}
            job_id = ""
            selected = None
            has_drafts = False
            video = None
            publications: list[str] = []
            if brief:
                has_drafts = self.database.execute(
                    "SELECT 1 FROM script_drafts WHERE brief_id=? LIMIT 1",
                    (brief["brief_id"],),
                ).fetchone() is not None
                selected_row = self.database.execute(
                    "SELECT draft_id,approved_job_id FROM script_drafts "
                    "WHERE brief_id=? AND status='approved' "
                    "ORDER BY updated_at DESC LIMIT 1", (brief["brief_id"],),
                ).fetchone()
                if selected_row:
                    selected = selected_row["draft_id"]
                    job_id = str(selected_row["approved_job_id"] or "")
            if job_id:
                job = self.database.execute(
                    "SELECT job_id,title,state,cost_cents,final_path,updated_at FROM jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                video = dict(job) if job else None
                publications = sorted(
                    item["platform"] for item in self.database.execute(
                        "SELECT platform FROM platform_publications WHERE job_id=?", (job_id,),
                    ).fetchall()
                )
            if not brief:
                stage, action = "source", "generate_brief"
            elif not has_drafts:
                stage, action = "brief", "generate_scripts"
            elif not selected:
                stage, action = "script", "review_script"
            elif video and video["state"] == "STORYBOARDED":
                stage, action = "production_setup", "configure_video"
            elif video and video["state"] == "COMPOSED":
                review = self.database.execute(
                    "SELECT decision FROM review_events WHERE job_id=? ORDER BY id DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
                stage, action = ("review", "submit_publish") if review and review["decision"] == "approved" else ("review", "review_video")
            elif video and video["state"] == "READY_TO_PUBLISH":
                stage, action = "publish", "continue_publish"
            elif video and publications:
                stage, action = "performance", "record_performance"
            else:
                stage, action = "production", "view_progress"
            projects.append({
                # The source title is the user-owned content-project name.  A
                # brief's recommended title is an AI output and must not rename
                # the project as it advances through the pipeline.
                "project_id": source["source_id"],
                "title": source.get("title") or (brief or {}).get("recommended_title") or "未命名内容",
                "source": source, "quality_gate": _source_quality(source.get("content")),
                "brief": brief, "selected_script_id": selected, "video": video,
                "published_platforms": publications, "stage": stage, "next_action": action,
            })
        return {"projects": projects}

    def add_source(self, *, source_type: str, title: str, content: str) -> dict:
        kind = str(source_type or "manual").strip().lower()
        if kind == "audio":
            raise ValueError("已取消音频来源，仅支持人工文本、公开网页和本地视频")
        value = str(content or "").strip()
        if not value:
            raise ValueError("来源内容不能为空")
        if kind == "url":
            result = script_sources.ingest_url(
                self.database, value, title=str(title or "").strip() or None
            )
        else:
            result = script_sources.ingest_text(
                self.database,
                value,
                title=str(title or "").strip() or None,
                source_uri=f"{kind or 'manual'}://management-console",
                source_type=kind or "manual",
            )
        reused_source = not result.created
        if reused_source:
            result = script_sources.instantiate_content_source(
                self.database, result.source_id, title=str(title or "").strip()
            )
        response = {
            "source_id": result.source_id, "created": True,
            "source_type": kind,
        }
        if reused_source:
            response["reused_source"] = True
        return response

    def add_media_source(self, path: str | Path, *, title: str = "") -> dict:
        media_path = Path(path).resolve()
        suffix = media_path.suffix.lower()
        if suffix not in media_sources.MEDIA_EXTENSIONS:
            raise ValueError("仅支持常见视频格式")
        if suffix not in media_sources.VIDEO_EXTENSIONS:
            raise ValueError("已取消音频来源，仅支持本地视频来源")
        env = _read_env(self.env_path)
        api_key = str(env.get("VOLC_ASR_API_KEY") or "").strip()
        app_id = str(env.get("VOLC_ASR_APPID") or "").strip()
        access_token = str(env.get("VOLC_ASR_TOKEN") or "").strip()
        if not api_key and not (app_id and access_token):
            raise ValueError(
                "本地视频转写需要先配置火山语音识别："
                "请优先填写新版 VOLC_ASR_API_KEY，或填写旧版 AppID 和 Token"
            )
        credentials = (
            {"asr_api_key": api_key, "asr_app_id": "", "asr_access_token": ""}
            if api_key else
            {"asr_app_id": app_id, "asr_access_token": access_token}
        )
        result = asyncio.run(media_sources.ingest_media(
            self.database, media_path, title=str(title or "").strip() or None,
            **credentials,
        ))
        source_id = result.source_id
        self._load_runtime_env()
        source_analysis.analyze_source(self.database, source_id)
        response = {
            "source_id": source_id,
            "created": True,
            "source_type": "video",
        }
        return response

    def analyze_media_source(self, source_id: str, *, force: bool = True) -> dict:
        source_id = str(source_id or "").strip()
        row = self.database.execute(
            "SELECT source_type FROM script_sources WHERE source_id=?",
            (source_id,),
        ).fetchone()
        if not row:
            raise ValueError("内容来源不存在")
        if row["source_type"] not in {"video", "audio"}:
            raise ValueError("仅音视频来源支持结构解析")
        if force and row["source_type"] == "video":
            asyncio.run(media_sources.refresh_analysis_frames(
                self.database, source_id
            ))
        self._load_runtime_env()
        report = source_analysis.analyze_source(
            self.database, source_id, force=force
        )
        return {"source_id": source_id, "analysis": report}

    def _source_media_metadata(self, source_id: str) -> tuple[str, dict]:
        source_id = str(source_id or "").strip()
        row = self.database.execute(
            "SELECT source_type,metadata_json FROM script_sources WHERE source_id=?",
            (source_id,),
        ).fetchone()
        if not row:
            raise ValueError("内容来源不存在")
        source_type = str(row["source_type"] or "")
        if source_type not in {"video", "audio"}:
            raise ValueError("该来源没有可预览的原始音视频")
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("来源解析元数据无效") from exc
        return source_type, metadata

    def source_media_file(self, source_id: str) -> Path:
        """Return the archived original media used by performance review."""
        _, metadata = self._source_media_metadata(source_id)
        path = Path(str(metadata.get("archived_path") or "")).resolve()
        if not path.is_file():
            raise ValueError("原始音视频文件不存在，请重新上传解析")
        return path

    def source_frame_file(self, source_id: str, frame_index: int) -> Path:
        """Return one declared analysis frame without exposing arbitrary paths."""
        _, metadata = self._source_media_metadata(source_id)
        expected = int(frame_index)
        frame = next(
            (
                item for item in metadata.get("frames") or []
                if int(item.get("index") or -1) == expected
            ),
            None,
        )
        if not frame:
            raise ValueError("画面证据不存在，请重新解析视频")
        path = Path(str(frame.get("path") or "")).resolve()
        if not path.is_file():
            raise ValueError("画面证据不存在，请重新解析视频")
        return path

    def review_source_performance(self, source_id: str, payload: dict) -> dict:
        """Persist human speaker resolution without rerunning paid analysis."""
        source_id = str(source_id or "").strip()
        row = self.database.execute(
            "SELECT source_type,metadata_json FROM script_sources WHERE source_id=?",
            (source_id,),
        ).fetchone()
        if not row:
            raise ValueError("内容来源不存在")
        if row["source_type"] not in {"video", "audio"}:
            raise ValueError("仅音视频来源支持人物表演校正")
        metadata = json.loads(row["metadata_json"] or "{}")
        analysis = metadata.get("structure_analysis") or {}
        segments = analysis.get("segments") or []
        assignments = payload.get("assignments") or []
        if not isinstance(assignments, list) or not assignments:
            raise ValueError("请至少校正一个说话人")
        for assignment in assignments:
            if not isinstance(assignment, dict):
                raise ValueError("说话人校正格式无效")
            try:
                segment_index = int(assignment.get("segment_index"))
            except (TypeError, ValueError) as exc:
                raise ValueError("说话人校正缺少有效分段序号") from exc
            if segment_index < 0 or segment_index >= len(segments):
                raise ValueError("说话人校正分段不存在")
            speaker = str(assignment.get("speaker") or "").strip()
            if not speaker:
                raise ValueError("说话人不能为空")
            segment = segments[segment_index]
            segment["speaker"] = speaker
            segment["speech_mode"] = script_drafts.normalize_speech_mode(
                assignment.get("speech_mode"), speaker=speaker
            )
            speaker_track_id = str(
                assignment.get("speaker_track_id") or ""
            ).strip()
            if speaker_track_id:
                segment["speaker_track_id"] = speaker_track_id
            for field in script_drafts.PERFORMANCE_TEXT_FIELDS:
                if field not in assignment:
                    continue
                value = str(assignment.get(field) or "").strip()
                if value:
                    segment[field] = value
                else:
                    segment.pop(field, None)
            segment["speaker_confidence"] = 1.0
            segment["speaker_review_required"] = False
            segment["speaker_resolution_status"] = "manual"
        analysis["performance_ir_version"] = 2
        metadata["structure_analysis"] = analysis
        self.database.execute(
            "UPDATE script_sources SET metadata_json=? WHERE source_id=?",
            (json.dumps(metadata, ensure_ascii=False), source_id),
        )
        self.database.commit()
        review_required_count = sum(
            1 for segment in segments
            if segment.get("speaker_review_required") is True
        )
        return {
            "source_id": source_id,
            "analysis": analysis,
            "review_required_count": review_required_count,
        }

    def _load_runtime_env(self) -> None:
        """Make values saved by the console visible to provider clients immediately."""
        for key, value in _read_env(self.env_path).items():
            os.environ[key] = value

    def generate_brief_from_source(
        self, source_id: str, *, risk_confirmed: bool = False
    ) -> dict:
        source_id = str(source_id or "").strip()
        source = self.database.execute(
            "SELECT content,source_type,metadata_json FROM script_sources "
            "WHERE source_id=?", (source_id,)
        ).fetchone()
        if not source:
            raise ValueError("内容来源不存在")
        quality = _source_quality(source["content"])
        if not quality["passed"] and not risk_confirmed:
            raise ValueError("来源内容不足，请补充信息；如确认风险，可人工确认后继续")
        self._load_runtime_env()
        metadata = json.loads(source["metadata_json"] or "{}")
        if (source["source_type"] in {"video", "audio"}
                and not metadata.get("structure_analysis")):
            source_analysis.analyze_source(self.database, source_id)
        return topic_briefs.generate_brief(self.database, [source_id])

    def confirm_brief_and_generate_scripts(
        self, brief_id: str, *, angle_index: int,
        generation_preferences: dict | None = None,
    ) -> dict:
        brief_id = str(brief_id or "").strip()
        row = self.database.execute(
            "SELECT brief_json,approved_topic_id,approved_job_id FROM topic_briefs "
            "WHERE brief_id=?", (brief_id,),
        ).fetchone()
        if not row:
            raise ValueError("选题简报不存在")
        brief = json.loads(row["brief_json"] or "{}")
        angles = brief.get("angles") or []
        if int(angle_index) not in range(len(angles)):
            raise ValueError("请选择一个有效的创意角度")
        # One brief maps to one stable topic/job so retries remain idempotent.
        topic_id = str(row["approved_topic_id"] or (
            "TOPIC-" + brief_id.removeprefix("BRIEF-")
        ))
        try:
            job_id = topic_briefs.approve_brief(
                self.database,
                brief_id,
                topic_id=topic_id,
                target_audience=str(brief.get("audience") or ""),
                angle_index=int(angle_index),
                commit=False,
            )
            self._load_runtime_env()
            result = script_drafts.generate_drafts(
                self.database,
                brief_id,
                preferences=generation_preferences,
            )
            self.database.commit()
        except Exception:
            self.database.rollback()
            raise
        return {
            "brief_id": brief_id,
            "job_id": job_id,
            "draft_count": len(result.get("drafts") or []),
            "cached": bool(result.get("cached")),
        }

    def _record_content_revision(
        self, *, entity_type: str, entity_id: str, revision: int,
        payload: dict, change_note: str = "",
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        identity = f"{entity_type}:{entity_id}:{revision}:{encoded}"
        revision_id = "REV-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        self.database.execute(
            "INSERT OR IGNORE INTO content_revisions("
            "revision_id,entity_type,entity_id,revision,payload_json,change_note) "
            "VALUES(?,?,?,?,?,?)",
            (
                revision_id, entity_type, entity_id, int(revision), encoded,
                str(change_note or "").strip(),
            ),
        )

    def update_brief(
        self, brief_id: str, payload: dict, *, change_note: str = ""
    ) -> dict:
        brief_id = str(brief_id or "").strip()
        row = self.database.execute(
            "SELECT status,recommended_title,brief_json,approved_topic_id,"
            "approved_job_id,revision FROM topic_briefs WHERE brief_id=?",
            (brief_id,),
        ).fetchone()
        if not row:
            raise ValueError("选题简报不存在")
        source_ids = {
            str(item["source_id"]) for item in self.database.execute(
                "SELECT source_id FROM topic_brief_sources WHERE brief_id=?",
                (brief_id,),
            ).fetchall()
        }
        normalized = topic_briefs.normalize_brief(
            payload if isinstance(payload, dict) else {},
            allowed_source_ids=source_ids,
        )
        drafts = self.database.execute(
            "SELECT draft_id,status,approved_job_id,storyboard_json "
            "FROM script_drafts WHERE brief_id=?", (brief_id,),
        ).fetchall()
        if any(
            item["status"] == "approved" or item["approved_job_id"]
            or item["storyboard_json"] for item in drafts
        ):
            raise ValueError("剧本已进入视频制作，简报不能覆盖；请复制为新版本")
        job_id = str(row["approved_job_id"] or "")
        topic_id = str(row["approved_topic_id"] or "")
        if job_id:
            job = self.database.execute(
                "SELECT state FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if job and job["state"] != "PENDING":
                raise ValueError("剧本已进入视频制作，简报不能覆盖；请复制为新版本")
        current_payload = json.loads(row["brief_json"] or "{}")
        current_revision = int(row["revision"] or 1)
        self._record_content_revision(
            entity_type="brief", entity_id=brief_id, revision=current_revision,
            payload=current_payload, change_note=change_note,
        )
        downstream_invalidated = bool(drafts or job_id)
        if drafts:
            self.database.execute(
                "DELETE FROM script_drafts WHERE brief_id=?", (brief_id,)
            )
        if job_id:
            self.database.execute("DELETE FROM events WHERE job_id=?", (job_id,))
            self.database.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
        if topic_id:
            self.database.execute(
                "DELETE FROM topic_script_sources WHERE topic_id=?", (topic_id,)
            )
        self.database.execute(
            "DELETE FROM creative_angle_tags WHERE brief_id=?", (brief_id,)
        )
        title = normalized["angles"][normalized["recommended_index"]]["title"]
        self.database.execute(
            "UPDATE topic_briefs SET status='candidate',recommended_title=?,"
            "brief_json=?,duplicate_json=NULL,approved_topic_id=NULL,"
            "approved_job_id=NULL,approved_angle_index=NULL,revision=?,"
            "updated_at=CURRENT_TIMESTAMP WHERE brief_id=?",
            (
                title, json.dumps(normalized, ensure_ascii=False),
                current_revision + 1, brief_id,
            ),
        )
        self.database.commit()
        return {
            "brief_id": brief_id, "status": "candidate",
            "revision": current_revision + 1, "brief": normalized,
            "downstream_invalidated": downstream_invalidated,
        }

    def update_script_draft(
        self, draft_id: str, payload: dict, *, change_note: str = "",
        auto_adjust_durations: bool = False,
    ) -> dict:
        draft_id = str(draft_id or "").strip()
        row = self.database.execute(
            "SELECT draft_id,brief_id,status,script_json,revision "
            "FROM script_drafts WHERE draft_id=?", (draft_id,),
        ).fetchone()
        if not row:
            raise ValueError("剧本不存在")
        if row["status"] != "candidate":
            raise ValueError("剧本已提交视频制作，不能覆盖；请复制为新版本")
        source_ids = {
            str(item["source_id"]) for item in self.database.execute(
                "SELECT source_id FROM topic_brief_sources WHERE brief_id=?",
                (row["brief_id"],),
            ).fetchall()
        }
        editable_payload = payload if isinstance(payload, dict) else {}
        repaired_invalid_duration = any(
            not script_drafts._is_valid_duration(beat.get("duration_sec"))
            for beat in (editable_payload.get("beats") or [])
            if isinstance(beat, dict)
        )
        if auto_adjust_durations:
            editable_payload = script_drafts.adjust_script_durations(
                editable_payload
            )
        normalized = script_drafts._normalize_variant(
            editable_payload,
            allowed_source_ids=source_ids,
        )
        normalized["duration_mode"] = (
            "auto" if auto_adjust_durations else "manual"
        )
        if isinstance(editable_payload.get("generation_preferences"), dict):
            normalized["generation_preferences"] = (
                script_drafts.normalize_generation_preferences(
                    editable_payload["generation_preferences"]
                )
            )
        current_payload = json.loads(row["script_json"] or "{}")
        current_revision = int(row["revision"] or 1)
        self._record_content_revision(
            entity_type="script", entity_id=draft_id, revision=current_revision,
            payload=current_payload, change_note=change_note,
        )
        self.database.execute(
            "UPDATE script_drafts SET script_json=?,revision=?,"
            "updated_at=CURRENT_TIMESTAMP WHERE draft_id=?",
            (
                json.dumps(normalized, ensure_ascii=False),
                current_revision + 1, draft_id,
            ),
        )
        self.database.commit()
        return {
            "draft_id": draft_id, "status": "candidate",
            "revision": current_revision + 1, "script": normalized,
            "duration_adjusted": bool(
                auto_adjust_durations or repaired_invalid_duration
            ),
        }

    def _delete_candidate_brief(self, brief_id: str) -> None:
        self.database.execute(
            "DELETE FROM script_drafts WHERE brief_id=?", (brief_id,)
        )
        self.database.execute(
            "DELETE FROM creative_angle_tags WHERE brief_id=?", (brief_id,)
        )
        self.database.execute(
            "DELETE FROM topic_brief_sources WHERE brief_id=?", (brief_id,)
        )
        self.database.execute(
            "DELETE FROM topic_briefs WHERE brief_id=?", (brief_id,)
        )

    def delete_brief(self, brief_id: str) -> dict:
        row = self.database.execute(
            "SELECT status,approved_job_id FROM topic_briefs WHERE brief_id=?",
            (brief_id,),
        ).fetchone()
        if not row:
            raise ValueError("选题简报不存在")
        if row["status"] == "approved" or row["approved_job_id"]:
            raise ValueError("选题简报已进入视频流水线，不能删除")
        self._delete_candidate_brief(brief_id)
        self.database.commit()
        return {"brief_id": brief_id, "deleted": True}

    def delete_source(self, source_id: str) -> dict:
        if not self.database.execute(
            "SELECT 1 FROM script_sources WHERE source_id=?", (source_id,)
        ).fetchone():
            raise ValueError("内容来源不存在")
        linked = self.database.execute(
            "SELECT b.brief_id,b.status,b.approved_job_id FROM topic_briefs b "
            "JOIN topic_brief_sources r ON r.brief_id=b.brief_id "
            "WHERE r.source_id=?", (source_id,),
        ).fetchall()
        if any(row["status"] == "approved" or row["approved_job_id"] for row in linked):
            raise ValueError("内容来源关联的选题已进入视频流水线，不能删除")
        # Candidate briefs are derived artifacts. Removing an input invalidates them,
        # even when a brief had more than one source.
        for row in linked:
            self._delete_candidate_brief(str(row["brief_id"]))
        self.database.execute(
            "DELETE FROM topic_script_sources WHERE source_id=?", (source_id,)
        )
        self.database.execute(
            "DELETE FROM script_sources WHERE source_id=?", (source_id,)
        )
        self.database.commit()
        return {
            "source_id": source_id,
            "deleted": True,
            "deleted_briefs": len(linked),
        }

    def approve_script(self, draft_id: str) -> dict:
        return script_drafts.approve_draft(self.database, draft_id)

    def delete_script(self, draft_id: str) -> dict:
        row = self.database.execute(
            "SELECT status,approved_job_id FROM script_drafts WHERE draft_id=?",
            (draft_id,),
        ).fetchone()
        if not row:
            raise ValueError("剧本不存在")
        if row["status"] == "approved" or row["approved_job_id"]:
            raise ValueError("已提交视频流水线的剧本不能删除")
        self.database.execute("DELETE FROM script_drafts WHERE draft_id=?", (draft_id,))
        self.database.commit()
        return {"draft_id": draft_id, "deleted": True}

    def batch_reject_scripts(self, draft_ids: list[str]) -> dict:
        normalized = list(dict.fromkeys(
            str(draft_id or "").strip() for draft_id in draft_ids
            if str(draft_id or "").strip()
        ))
        if not normalized:
            raise ValueError("请选择需要标记为不采用的候选剧本")
        if len(normalized) > 100:
            raise ValueError("单次最多处理 100 个候选剧本")
        placeholders = ",".join("?" for _ in normalized)
        rows = self.database.execute(
            f"SELECT draft_id,status,approved_job_id FROM script_drafts "
            f"WHERE draft_id IN ({placeholders})",
            normalized,
        ).fetchall()
        drafts = {str(row["draft_id"]): row for row in rows}
        invalid = [
            draft_id for draft_id in normalized
            if draft_id not in drafts
            or str(drafts[draft_id]["status"]) != "candidate"
            or drafts[draft_id]["approved_job_id"]
        ]
        if invalid:
            raise ValueError("以下剧本不存在或不可标记为不采用：" + "、".join(invalid))
        self.database.execute(
            f"UPDATE script_drafts SET status='rejected',"
            f"updated_at=CURRENT_TIMESTAMP WHERE draft_id IN ({placeholders})",
            normalized,
        )
        self.database.commit()
        return {"rejected": len(normalized), "draft_ids": normalized}

    def batch_production_preflight(self, job_ids: list[str]) -> dict:
        normalized = list(dict.fromkeys(
            str(job_id or "").strip() for job_id in job_ids
            if str(job_id or "").strip()
        ))
        if not normalized:
            raise ValueError("请选择需要执行生产预检的视频任务")
        if len(normalized) > 50:
            raise ValueError("单次最多预检 50 个视频任务")
        placeholders = ",".join("?" for _ in normalized)
        rows = self.database.execute(
            f"SELECT job_id,title,state,storyboard,cost_cents FROM jobs "
            f"WHERE job_id IN ({placeholders})",
            normalized,
        ).fetchall()
        jobs = {str(row["job_id"]): row for row in rows}
        invalid = [
            job_id for job_id in normalized
            if job_id not in jobs or str(jobs[job_id]["state"]) != "STORYBOARDED"
        ]
        if invalid:
            raise ValueError("以下任务不存在或不在待生成状态：" + "、".join(invalid))
        config = _read_env(self.env_path)
        reports: list[dict] = []
        for job_id in normalized:
            row = jobs[job_id]
            try:
                storyboard = json.loads(row["storyboard"] or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"任务 {job_id} 的分镜数据损坏") from exc
            report = production_planner.analyze_storyboard(
                self.database,
                storyboard,
                budget_cents=(
                    storyboard.get("production_settings", {}).get("budget_cents")
                    or config.get("SEEDANCE_JOB_BUDGET_CENTS")
                ),
                resolution=config.get("ARK_VIDEO_RESOLUTION"),
                completed_identity_keys=_completed_identity_keys(
                    self.database, job_id
                ),
                completed_scene_keys=_completed_scene_keys(
                    self.database, job_id
                ),
                current_cost_cents=int(row["cost_cents"] or 0),
            )
            reports.append({
                "job_id": job_id,
                "title": str(row["title"] or job_id),
                "preflight": report,
            })
        return {
            "job_count": len(reports),
            "paid_provider_calls": 0,
            "estimated_paid_calls": sum(
                int(item["preflight"].get("estimated_paid_calls") or 0)
                for item in reports
            ),
            "estimated_cost_cents": sum(
                int(item["preflight"].get("estimated_cost_cents") or 0)
                for item in reports
            ),
            "all_budget_ok": all(
                bool(item["preflight"].get("budget_ok")) for item in reports
            ),
            "jobs": reports,
        }

    def videos(self) -> dict:
        jobs = _rows(self.database.execute(
            "SELECT j.job_id,j.title,j.state,j.cost_cents,j.final_path,j.error,"
            "j.failure_code,j.storyboard,j.execution_engine,j.production_spec_json,"
            "j.created_at,j.updated_at "
            "FROM jobs j WHERE NOT ("
            "j.state='PENDING' AND NOT EXISTS (SELECT 1 FROM script_drafts d "
            "WHERE d.approved_job_id=j.job_id AND d.status='approved') "
            "AND NOT EXISTS (SELECT 1 FROM shots s WHERE s.job_id=j.job_id)) "
            "ORDER BY j.created_at DESC LIMIT 80"
        ))
        for job in jobs:
            final_file = Path(job["final_path"]) if job.get("final_path") else None
            job["final_exists"] = bool(final_file and final_file.exists())
            job["final_version"] = (
                final_file.stat().st_mtime_ns if job["final_exists"] else None
            )
            try:
                storyboard = json.loads(job.pop("storyboard") or "{}")
            except json.JSONDecodeError:
                storyboard = {}
            job["production_profile"] = str(
                storyboard.get("production_profile") or "standard"
            )
            job["characters"] = storyboard.get("characters") or []
            job["production_settings"] = storyboard.get("production_settings") or {}
            job["production_graph_nodes"] = []
            job["production_stages"] = []
            raw_production_spec = job.pop("production_spec_json", None)
            production_spec = None
            if raw_production_spec:
                try:
                    production_spec = json.loads(raw_production_spec)
                except (TypeError, json.JSONDecodeError):
                    production_spec = None
            if str(job.get("execution_engine") or "legacy") == "graph_v2":
                graph_nodes = _rows(self.database.execute(
                    "SELECT node_id,node_type,state,paid,estimated_cost_cents,"
                    "actual_cost_cents,provider_task_id,error_code,next_action,"
                    "output_json "
                    "FROM production_nodes WHERE job_id=? "
                    "ORDER BY node_type,node_id",
                    (job["job_id"],),
                ))
                job["production_graph_nodes"] = graph_nodes
                job["production_stages"] = _production_stage_projection(
                    graph_nodes
                )
                budget_nodes = [
                    node for node in graph_nodes
                    if str(node.get("state")) == "BUDGET_REQUIRED"
                ]
                budget_details: list[dict] = []
                for node in budget_nodes:
                    try:
                        detail = json.loads(node.pop("output_json") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        detail = {}
                    budget_details.append({
                        "node_id": str(node.get("node_id") or ""),
                        "estimated_cost_cents": int(
                            node.get("estimated_cost_cents") or 0
                        ),
                        **(
                            detail if isinstance(detail, dict) else {}
                        ),
                    })
                for node in graph_nodes:
                    node.pop("output_json", None)
                if budget_details:
                    job["budget_confirmation"] = {
                        "nodes": budget_details,
                        "current_cost_cents": int(job.get("cost_cents") or 0),
                        "current_budget_cents": int(
                            job["production_settings"].get("budget_cents") or 0
                        ),
                        "minimum_budget_without_retry_cents": (
                            int(job.get("cost_cents") or 0)
                            + sum(
                                max(
                                    int(item.get("estimated_cost_cents") or 0),
                                    int(item.get("next_estimate_cents") or 0),
                                )
                                for item in budget_details
                            )
                        ),
                    }
            confirmed_cost = job["production_settings"].get(
                "confirmed_cost_estimate"
            )
            actual_cost = int(job.get("cost_cents") or 0)
            if isinstance(confirmed_cost, dict) and confirmed_cost.get(
                "projected_total_cost_cents"
            ) is not None:
                estimated_cost = int(
                    confirmed_cost.get("projected_total_cost_cents") or 0
                )
                variance = actual_cost - estimated_cost
                is_final = str(job.get("state") or "") in FINAL_COST_STATES
                job["cost_observation"] = {
                    "estimated_cost_cents": estimated_cost,
                    "actual_cost_cents": actual_cost,
                    "variance_cents": variance,
                    "variance_percent": (
                        round(variance * 100 / estimated_cost, 1)
                        if estimated_cost > 0 else None
                    ),
                    "status": (
                        "over_estimate" if is_final and variance > 0
                        else "within_estimate" if is_final
                        else "pending"
                    ),
                    "is_final": is_final,
                    "pricing_version": str(
                        confirmed_cost.get("pricing_version") or ""
                    ),
                }
            else:
                job["cost_observation"] = {
                    "estimated_cost_cents": None,
                    "actual_cost_cents": actual_cost,
                    "variance_cents": None,
                    "variance_percent": None,
                    "status": "unavailable",
                    "is_final": str(job.get("state") or "") in FINAL_COST_STATES,
                    "pricing_version": None,
                }
            call_summary = self.database.execute(
                "SELECT COUNT(*) AS call_count,COALESCE(SUM(cost_cents),0) AS cost "
                "FROM provider_calls WHERE job_id=?",
                (job["job_id"],),
            ).fetchone()
            tracked_call_cost = int(call_summary["cost"] or 0)
            cost_delta = int(job.get("cost_cents") or 0) - tracked_call_cost
            job["tracked_call_cost_cents"] = tracked_call_cost
            job["unallocated_cost_cents"] = max(0, cost_delta)
            job["cost_delta_cents"] = cost_delta
            job["cost_reconciliation_status"] = (
                "reconciled" if cost_delta == 0 else "partial"
            )
            unresolved_ids = set(provider_tasks.unresolved_task_ids(
                self.database, job["job_id"]
            ))
            job["unresolved_provider_tasks"] = []
            if unresolved_ids:
                for task in self.database.execute(
                    "SELECT task_id,provider,intent,usage_json,cost_cents "
                    "FROM provider_calls WHERE job_id=? ORDER BY created_at,task_id",
                    (job["job_id"],),
                ).fetchall():
                    display_task_id = provider_tasks.effective_task_id(
                        task["task_id"], task["usage_json"]
                    )
                    if display_task_id not in unresolved_ids:
                        continue
                    storage_task_id = str(task["task_id"])
                    is_local = storage_task_id.startswith("local-submit:")
                    try:
                        task_usage = json.loads(task["usage_json"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        task_usage = {}
                    remote_value = (
                        task_usage.get("remote_task_id")
                        if isinstance(task_usage, dict) else None
                    )
                    remote_task_id = (
                        remote_value.strip()
                        if isinstance(remote_value, str) else ""
                    )
                    reconcilable = not is_local or bool(
                        remote_task_id and remote_task_id != storage_task_id
                    )
                    item = {
                        "task_id": display_task_id,
                        "provider": str(task["provider"]),
                        "intent": str(task["intent"] or ""),
                        "cost_cents": int(task["cost_cents"] or 0),
                        "reconcilable": reconcilable,
                    }
                    if not reconcilable:
                        item["action_message"] = (
                            "缺少远端任务 ID，当前不可自动核对；"
                            "请人工核查 Provider 后处置，禁止重新提交"
                        )
                    job["unresolved_provider_tasks"].append(item)
            plan_storyboard = (
                production_spec.get("runtime_storyboard")
                if str(job.get("execution_engine") or "legacy") == "graph_v2"
                and isinstance(production_spec, dict)
                and isinstance(production_spec.get("runtime_storyboard"), dict)
                else storyboard
            )
            planned_masters = list(
                plan_storyboard.get("masters")
                or plan_storyboard.get("shots") or []
            )
            job["director_plan"] = plan_storyboard.get("director_plan") or {
                "strategy": "legacy_dialogue_only",
                "dialogue_master_count": sum(
                    1 for master in planned_masters
                    if (master.get("type") or "dialogue") == "dialogue"
                ),
                "broll_master_count": sum(
                    1 for master in planned_masters if master.get("type") == "broll"
                ),
                "broll_beats": [],
            }
            if str(job.get("execution_engine") or "legacy") == "graph_v2":
                dialogue_master_count = sum(
                    1 for master in planned_masters
                    if (master.get("type") or "dialogue") == "dialogue"
                )
                broll_master_count = sum(
                    1 for master in planned_masters
                    if master.get("type") == "broll"
                )
                job["director_plan"] = {
                    **job["director_plan"],
                    "strategy": (
                        "dialogue_plus_broll"
                        if broll_master_count else "cinematic_dialogue_only"
                    ),
                    "dialogue_master_count": dialogue_master_count,
                    "broll_master_count": broll_master_count,
                    "paid_master_count": len(planned_masters),
                    "broll_beats": (
                        list(job["director_plan"].get("broll_beats") or [])
                        if broll_master_count else []
                    ),
                }
            has_approved_script = bool(self.database.execute(
                "SELECT 1 FROM script_drafts WHERE approved_job_id=? "
                "AND status='approved' LIMIT 1",
                (job["job_id"],),
            ).fetchone())
            job["director_plan_upgradeable"] = bool(
                job["state"] == "STORYBOARDED"
                and job["director_plan"]["strategy"] == "legacy_dialogue_only"
                and int(job.get("cost_cents") or 0) == 0
                and int(call_summary["call_count"] or 0) == 0
                and has_approved_script
            )
            job["planned_masters"] = planned_masters
            job['has_visual_requirements'] = content_review.enabled(plan_storyboard)
            job['visual_requirements_summary'] = plan_storyboard.get('visual_requirements') or []
            job["director_timeline"] = _director_timeline(plan_storyboard)
            shots = _rows(self.database.execute(
                "SELECT idx,duration_sec,state,provider,cost_cents,video_path,"
                "video_url,video_qa,error "
                "FROM shots WHERE job_id=? ORDER BY idx", (job["job_id"],)
            ))
            previous_master_preview_url = None
            qa_by_master_idx: dict[int, dict] = {}
            for shot in shots:
                try:
                    qa = json.loads(shot.pop("video_qa") or "{}")
                except (json.JSONDecodeError, TypeError):
                    qa = {}
                qa_by_master_idx[int(shot["idx"])] = qa
                plan_master = next((m for m in plan_storyboard.get('masters') or [] if int(m['idx']) == int(shot['idx'])), {})
                shot['has_visual_requirements'] = bool(content_review.requirements(plan_master))
                shot['content_review_summary'] = [{'asset_sha256': r.get('asset_sha256'),
                    'requirement_set_hash': r.get('requirement_set_hash'), 'items': r.get('items'), 'reviewed_at': r.get('reviewed_at')}
                    for r in qa.get('content_reviews') or [] if isinstance(r, dict) and r.get('authority') == content_review.AUTHORITY]
                shot["manual_review_required"] = (
                    qa.get("status") == "manual_review_required"
                )
                shot["quality_reasons"] = list(qa.get("reasons") or [])
                shot["quality_status"] = str(qa.get("status") or "")
                shot["lip_sync_evidence"] = (
                    video_qa.normalize_lip_sync_evidence(
                        dict(qa.get("result") or {})
                    )
                    if "lip_sync_mismatch" in shot["quality_reasons"]
                    else None
                )
                shot["failure_code"] = str(qa.get("failure_code") or "")
                shot["human_decision"] = str(
                    qa.get("human_decision") or ""
                )
                shot["reviewed_at"] = str(qa.get("reviewed_at") or "")
                shot["review_resolved"] = bool(
                    shot["quality_status"]
                    in {
                        "candidate_accepted",
                        "candidate_accepted_pending",
                        "candidate_rejected",
                    }
                    and shot["human_decision"]
                    in {"accept_candidate", "keep_prior"}
                )
                shot["candidate_preview_url"] = None
                shot["previous_master_preview_url"] = previous_master_preview_url
                video_path = str(shot.get("video_path") or "").strip()
                shot["has_prior_master"] = bool(
                    video_path and Path(video_path).is_file()
                )
                if shot["manual_review_required"]:
                    try:
                        self.master_candidate_file(job["job_id"], shot["idx"])
                        shot["candidate_preview_url"] = (
                            f"/api/videos/{job['job_id']}/masters/"
                            f"{shot['idx']}/candidate"
                        )
                    except ValueError:
                        pass
                if shot.get("state") == "OK" and str(
                    shot.get("video_url") or ""
                ).strip():
                    previous_master_preview_url = str(shot["video_url"]).strip()
            job["shots"] = shots
            if shots:
                job["director_plan_upgradeable"] = False
            completed_indexes = {
                int(shot["idx"]) for shot in shots if shot.get("state") == "OK"
            }
            master_by_idx = {
                int(master.get("idx") or index): master
                for index, master in enumerate(planned_masters, 1)
            }
            all_master_indexes = set(master_by_idx)
            configured_budget = (
                storyboard.get("production_settings", {}).get("budget_cents")
                or _read_env(self.env_path).get("SEEDANCE_JOB_BUDGET_CENTS")
            )
            configured_resolution = _read_env(self.env_path).get(
                "ARK_VIDEO_RESOLUTION"
            )
            for shot in shots:
                idx = int(shot["idx"])
                master = master_by_idx.get(idx, {})
                shot["master_type"] = str(master.get("type") or "dialogue")
                shot["speaker"] = str(master.get("speaker") or "")
                repair_report = production_planner.analyze_storyboard(
                    self.database,
                    storyboard,
                    budget_cents=configured_budget,
                    resolution=configured_resolution,
                    completed_master_indexes=all_master_indexes - {idx},
                    completed_identity_keys=_completed_identity_keys(
                        self.database, job["job_id"]
                    ),
                    completed_scene_keys=_completed_scene_keys(
                        self.database, job["job_id"]
                    ),
                    current_cost_cents=int(job.get("cost_cents") or 0),
                )
                repair_item = next((
                    item for item in repair_report["paid_masters"]
                    if int(item["master_idx"]) == idx
                ), None)
                shot["regeneration_estimated_cost_cents"] = int(
                    (repair_item or {}).get("estimated_cost_cents") or 0
                )
                shot["regeneration_uses_library"] = any(
                    int(item["master_idx"]) == idx
                    for item in repair_report["library_hits"]
                )
                shot["regeneration_eligible"] = bool(
                    job["state"] in {"COMPOSED", "FAILED", "BLOCKED"}
                    and shot.get("state") in {"OK", "FAILED"}
                    and all(other.get("state") == "OK" for other in shots if int(other["idx"]) != idx)
                )
                shot["lip_sync_repair_eligible"] = bool(
                    job["state"] in {
                        "COMPOSED", "FAILED", "BLOCKED",
                        "PRODUCTION_REVIEW_REQUIRED",
                    }
                    and shot["master_type"] == "dialogue"
                    and shot.get("state") in {
                        "OK", "FAILED", "REVIEW_REQUIRED",
                    }
                    and (
                        shot.get("has_prior_master")
                        or (
                            shot.get("candidate_preview_url")
                            and set(shot["quality_reasons"])
                            == {"lip_sync_mismatch"}
                        )
                    )
                )
                shot["extension_eligible"] = bool(
                    job["state"] in {
                        "COMPOSED", "FAILED", "BLOCKED",
                        "PRODUCTION_REVIEW_REQUIRED",
                    }
                    and shot.get("state") in {
                        "OK", "FAILED", "REVIEW_REQUIRED",
                    }
                    and shot.get("has_prior_master")
                )
                shot["lip_sync_repair_suggestion"] = None
                if (
                    shot["lip_sync_repair_eligible"]
                    and "lip_sync_mismatch" in shot["quality_reasons"]
                ):
                    shot["lip_sync_repair_suggestion"] = (
                        _lip_sync_repair_suggestion(
                            storyboard,
                            master,
                            idx,
                            float(
                                master.get("duration")
                                or shot.get("duration_sec")
                                or 0
                            ),
                            qa_by_master_idx.get(idx),
                        )
                    )
                shot["repair_comparison"] = None
                if (
                    str(master.get("repair_mode") or "")
                    == "seedance_lip_sync_edit"
                    and shot.get("candidate_preview_url")
                ):
                    source = controlled_job_asset_path(
                        self.data_dir / "assets",
                        job["job_id"],
                        master.get("repair_source_path"),
                        require_file=True,
                    )
                    if source is not None:
                        baseline_cost = max(
                            0,
                            int(master.get("repair_baseline_cost_cents") or 0),
                        )
                        shot["repair_comparison"] = {
                            "available": True,
                            "mode": "seedance_lip_sync_edit",
                            "original_preview_url": (
                                f"/api/videos/{job['job_id']}/masters/"
                                f"{idx}/repair-source"
                            ),
                            "candidate_preview_url": (
                                shot["candidate_preview_url"]
                            ),
                            "repair_start_sec": float(
                                master.get("repair_start_sec") or 0
                            ),
                            "repair_end_sec": float(
                                master.get("repair_end_sec") or 0
                            ),
                            "additional_cost_cents": max(
                                0,
                                int(shot.get("cost_cents") or 0)
                                - baseline_cost,
                            ),
                        }
            job["completed_master_count"] = len(completed_indexes)
            job["generating_master_count"] = sum(
                1 for shot in shots if shot.get("state") == "GENERATING"
            )
            job["failed_master_count"] = sum(
                1 for shot in shots if shot.get("state") == "FAILED"
            )
            job["review_required_master_count"] = sum(
                1 for shot in shots if shot.get("manual_review_required")
            )
            job["waiting_master_count"] = sum(
                1 for shot in shots if shot.get("state") == "PENDING"
            )
            job["remaining_master_count"] = max(
                0, (len(shots) or len(planned_masters)) - len(completed_indexes)
            )
            job["partial_production"] = bool(
                job["state"] == "STORYBOARDED"
                and completed_indexes
                and job["remaining_master_count"]
            )
            failed_shots = [
                shot for shot in shots if shot.get("state") == "FAILED"
            ]
            pending_shots = [
                shot for shot in shots if shot.get("state") == "PENDING"
            ]
            account_shot_failure = bool(
                failed_shots
                and all(
                    shot.get("failure_code") == "account_unavailable"
                    for shot in failed_shots
                )
                and all(
                    shot.get("state") in {"OK", "FAILED"} for shot in shots
                )
            )
            account_anchor_failure = bool(
                job.get("failure_code") == "account_unavailable"
                and pending_shots
                and all(
                    shot.get("state") in {"OK", "PENDING"} for shot in shots
                )
            )
            structured_failure_codes = {
                str(job.get("failure_code") or "").strip(),
                *(
                    str(shot.get("failure_code") or "").strip()
                    for shot in failed_shots
                ),
            } - {""}
            identity_anchor_retry_ready = (
                _request_invalid_identity_anchor_retry_ready(
                    self.database,
                    job_id=job["job_id"],
                    state=job["state"],
                    failure_code=job.get("failure_code"),
                    cost_cents=job.get("cost_cents"),
                    shots=shots,
                )
            )
            job["identity_anchor_retry_ready"] = identity_anchor_retry_ready
            job["network_retry_indexes"] = network_recovery.retry_indexes(self.database, job["job_id"])
            job["network_retry_anchor_ids"] = network_recovery.retry_anchor_ids(self.database, job["job_id"])
            provider_recovery_ready = bool(
                job["state"] in {"FAILED", "PROVIDER_ACTION_REQUIRED"}
                and not job["unresolved_provider_tasks"]
                and structured_failure_codes == {"account_unavailable"}
                and (account_shot_failure or account_anchor_failure)
                and provider_health.get_status(
                    self.database, "seedance"
                )["status"] == provider_health.AVAILABLE
            )
            legacy_failed_recovery = bool(
                job["state"] == "FAILED" and not structured_failure_codes
            )
            job["resumable"] = bool(
                (
                    legacy_failed_recovery
                    or provider_recovery_ready
                    or identity_anchor_retry_ready
                )
                and (
                    job["remaining_master_count"]
                    or (shots and all(shot.get("state") == "OK" for shot in shots))
                )
            )
            job["local_postproduction_resumable"] = bool(
                job["state"] in {"FAILED", "BGM_OK"}
                and shots
                and all(shot.get("state") == "OK" for shot in shots)
                and not job["unresolved_provider_tasks"]
                and job.get("failure_code") != "music_configuration_invalid"
            )
            if job["local_postproduction_resumable"]:
                job["resumable"] = True
            job["master_count"] = len(shots) or len(planned_masters)
            duration_source = shots or planned_masters
            job["total_master_duration_sec"] = round(sum(
                float(item.get("duration_sec") or item.get("duration") or 0)
                for item in duration_source
            ), 2)
            makeup_rows = {
                str(item["identity_key"]): dict(item)
                for item in self.database.execute(
                    "SELECT identity_key,state,source_type,asset_uri,video_path,"
                    "video_url,anchor_mode,image_path,image_url,preview_path,"
                    "duration_sec,cost_cents,approval_state,approved_at,"
                    "review_note,generator,model,error "
                    "FROM character_identity_anchors WHERE job_id=?",
                    (job["job_id"],),
                ).fetchall()
            }
            job["character_makeup_plan"] = []
            planned_makeup = anchor_generation.apply_character_generator(
                character_assets.character_makeup_plan(job["characters"]),
                storyboard,
                resolution=configured_resolution or "720p",
            )
            for makeup in planned_makeup:
                persisted = makeup_rows.get(makeup["identity_key"])
                if persisted:
                    makeup["source"] = str(
                        persisted.get("source_type") or makeup["source"]
                    )
                    makeup["status"] = str(
                        persisted.get("state") or makeup["status"]
                    ).lower()
                    makeup["cost_cents"] = int(
                        persisted.get("cost_cents") or 0
                    )
                    makeup["error"] = str(persisted.get("error") or "")
                    makeup["approval_state"] = str(
                        persisted.get("approval_state") or "PENDING_REVIEW"
                    ).lower()
                    makeup["approved_at"] = str(
                        persisted.get("approved_at") or ""
                    )
                    makeup["review_note"] = str(
                        persisted.get("review_note") or ""
                    )
                    makeup["anchor_mode"] = str(
                        persisted.get("anchor_mode")
                        or makeup.get("anchor_mode")
                        or ""
                    )
                    makeup["generator"] = str(
                        persisted.get("generator")
                        or makeup.get("generator") or ""
                    )
                    makeup["model"] = str(
                        persisted.get("model") or makeup.get("model") or ""
                    )
                    preview = str(persisted.get("preview_path") or "")
                    if preview and Path(preview).is_file():
                        preview_revision = Path(preview).stat().st_mtime_ns
                        makeup["preview_url"] = (
                            f"/api/videos/{job['job_id']}/identities/"
                            f"{makeup['identity_token']}/preview"
                            f"?v={preview_revision}"
                        )
                if not makeup.get("preview_url"):
                    representative = next((
                        character for character in job["characters"]
                        if str(character.get("id") or "")
                        in makeup["character_ids"]
                    ), {})
                    asset_character_id = str(
                        representative.get("asset_character_id") or ""
                    )
                    if asset_character_id:
                        makeup["preview_url"] = (
                            self._local_character_preview_url(
                                asset_character_id
                            )
                        )
                job["character_makeup_plan"].append(makeup)
            scene_rows = {
                str(row["anchor_key"]): dict(row)
                for row in self.database.execute(
                    "SELECT anchor_key,description,master_indexes,state,"
                    "image_path,image_url,preview_path,cost_cents,approval_state,"
                    "approved_at,review_note,generator,model,video_path,video_url,error "
                    "FROM scene_visual_anchors WHERE job_id=?",
                    (job["job_id"],),
                ).fetchall()
            }
            graph_scene_keys = {
                str(node.get("node_id") or "").split(":", 1)[1]
                for node in job.get("production_graph_nodes") or []
                if str(node.get("node_type") or "") == "scene_anchor"
                and ":" in str(node.get("node_id") or "")
            }
            runtime_storyboard = (
                production_spec.get("runtime_storyboard")
                if isinstance(production_spec, dict)
                and isinstance(production_spec.get("runtime_storyboard"), dict)
                else storyboard
            )
            planned_scene_anchors = (
                scene_assets.required_scene_anchors(
                    runtime_storyboard, graph_scene_keys,
                )
                if graph_scene_keys
                else scene_assets.recurring_scene_anchors(storyboard)
            )
            scene_specs = {
                item["anchor_key"]: item for item in planned_scene_anchors
            }
            job["scene_anchor_plan"] = []
            for anchor_key in dict.fromkeys([*scene_specs, *scene_rows]):
                planned = scene_specs.get(anchor_key, {})
                planned = anchor_generation.decorate_scene_anchor(
                    planned, storyboard,
                    resolution=configured_resolution or "720p",
                ) if planned else {}
                persisted = scene_rows.get(anchor_key, {})
                try:
                    master_indexes = json.loads(
                        persisted.get("master_indexes") or "[]"
                    )
                except json.JSONDecodeError:
                    master_indexes = []
                entry = {
                    "anchor_key": anchor_key,
                    "anchor_scope": str(
                        planned.get("anchor_scope") or "scene"
                    ),
                    "anchor_token": scene_assets.anchor_token(anchor_key),
                    "description": str(
                        persisted.get("description")
                        or planned.get("description") or ""
                    ),
                    "master_indexes": (
                        master_indexes or planned.get("master_indexes") or []
                    ),
                    "blocking_anchor": planned.get("blocking_anchor") or {},
                    "status": str(persisted.get("state") or "PENDING").lower(),
                    "approval_state": str(
                        persisted.get("approval_state") or "PENDING_REVIEW"
                    ).lower(),
                    "approved_at": str(persisted.get("approved_at") or ""),
                    "review_note": str(persisted.get("review_note") or ""),
                    "cost_cents": int(persisted.get("cost_cents") or 0),
                    "estimated_cost_cents": 29,
                    "generator": str(
                        persisted.get("generator")
                        or planned.get("generator")
                        or anchor_generation.selected_generator(storyboard, "scene")
                    ),
                    "model": str(
                        persisted.get("model") or planned.get("model") or ""
                    ),
                    "error": str(persisted.get("error") or ""),
                }
                entry["estimated_cost_cents"] = int(
                    planned.get("estimated_cost_cents")
                    or entry["cost_cents"] or 0
                )
                preview = Path(str(persisted.get("preview_path") or ""))
                if preview.is_file():
                    preview_revision = preview.stat().st_mtime_ns
                    entry["preview_url"] = (
                        f"/api/videos/{job['job_id']}/scenes/"
                        f"{entry['anchor_token']}/preview"
                        f"?v={preview_revision}"
                    )
                job["scene_anchor_plan"].append(entry)
            job["preflight"] = (
                production_planner.analyze_storyboard(
                    self.database,
                    storyboard,
                    budget_cents=(
                        configured_budget
                    ),
                    resolution=configured_resolution,
                    completed_master_indexes=completed_indexes,
                    completed_identity_keys=_completed_identity_keys(
                        self.database, job["job_id"]
                    ),
                    completed_scene_keys=_completed_scene_keys(
                        self.database, job["job_id"]
                    ),
                    current_cost_cents=int(job.get("cost_cents") or 0),
                )
                if job["state"] == "STORYBOARDED" else None
            )
            if (
                job["preflight"]
                and isinstance(production_spec, dict)
                and int(production_spec.get("version") or 0) == 2
            ):
                completed_node_ids = {
                    str(node.get("node_id") or "")
                    for node in job["production_graph_nodes"]
                    if str(node.get("state") or "") in {"SUCCEEDED", "SKIPPED"}
                }
                job["preflight"] = (
                    production_planner.align_preflight_with_compiled_spec(
                        job["preflight"],
                        production_spec,
                        completed_node_ids=completed_node_ids,
                        current_cost_cents=int(job.get("cost_cents") or 0),
                    )
                )
            if job["preflight"]:
                estimated_makeup = {
                    item["identity_key"]: item
                    for item in job["preflight"]["character_makeup_plan"]
                }
                for makeup in job["character_makeup_plan"]:
                    estimate = estimated_makeup.get(makeup["identity_key"], {})
                    if "estimated_cost_cents" in estimate:
                        makeup["estimated_cost_cents"] = int(
                            estimate["estimated_cost_cents"] or 0
                        )
            review = self.database.execute(
                "SELECT decision,manual_minutes,first_pass,notes,checks_json,created_at "
                "FROM review_events WHERE job_id=? ORDER BY id DESC LIMIT 1",
                (job["job_id"],),
            ).fetchone()
            job["latest_review"] = dict(review) if review else None
            if job["latest_review"]:
                try:
                    job["latest_review"]["checks"] = json.loads(
                        job["latest_review"].pop("checks_json") or "{}"
                    )
                except json.JSONDecodeError:
                    job["latest_review"]["checks"] = {}
            evidence = self.database.execute(
                "SELECT COUNT(*) FROM platform_publications WHERE job_id=?", (job["job_id"],)
            ).fetchone()[0]
            if job["state"] == "PUBLISHED" and not evidence:
                job["display_state"] = "LEGACY_UNVERIFIED"
                job["display_state_label"] = "历史状态待核实"
            elif job["state"] == "PUBLISHED":
                job["display_state"] = "PUBLISHED_VERIFIED"
                job["display_state_label"] = "已核实发布"
            elif job["partial_production"]:
                job["display_state"] = "PRODUCTION_PAUSED"
                job["display_state_label"] = "待继续生成"
            else:
                job["display_state"] = job["state"]
                job["display_state_label"] = None
            pause_presentation = {
                "PRODUCTION_REVIEW_REQUIRED": {
                    "label": "母片待复核",
                    "next_action": {
                        "id": "review_master_candidates",
                        "label": "预览并处理候选母片",
                    },
                },
                "PROVIDER_ACTION_REQUIRED": {
                    "label": "模型账号待处理",
                    "next_action": {
                        "id": (
                            "resume_failed_video"
                            if job["resumable"] else "view_provider_account_guide"
                        ),
                        "label": (
                            "继续生成剩余母片"
                            if job["resumable"] else "核对远端任务"
                            if any(
                                task.get("reconcilable") is not False
                                for task in job["unresolved_provider_tasks"]
                            ) else "查看账号处理指引"
                        ),
                    },
                },
                "BUDGET_CONFIRMATION_REQUIRED": {
                    "label": "预算待确认",
                    "next_action": {
                        "id": "increase_job_budget",
                        "label": "确认预算并继续",
                    },
                },
                "REJECTED_PENDING_RECONCILIATION": {
                    "label": "已终止，待成本核对",
                    "next_action": {
                        "id": "reconcile_rejected_provider_cost",
                        "label": "核对终止任务成本",
                    },
                },
            }.get(job["state"])
            if identity_anchor_retry_ready:
                job["display_state_label"] = "人物定妆待重试"
                job["next_action"] = {
                    "id": "retry_identity_anchor",
                    "label": "修复后重新尝试人物定妆",
                }
            elif job["state"] == "BGM_OK" and job["local_postproduction_resumable"]:
                job["display_state_label"] = "待完成本地合成"
                job["next_action"] = {
                    "id": "resume_local_postproduction",
                    "label": "继续本地合成",
                }
            elif pause_presentation:
                job["display_state_label"] = pause_presentation["label"]
                job["next_action"] = pause_presentation["next_action"]
            elif job["network_retry_indexes"]:
                job["next_action"] = {"id": "retry_network_failures", "label": "重试连接失败的母片"}
            elif job["state"] == "FAILED" and job["resumable"]:
                job["next_action"] = (
                    {
                        "id": "resume_local_postproduction",
                        "label": "继续本地后期",
                    }
                    if job["local_postproduction_resumable"]
                    else {
                        "id": "resume_failed_video",
                        "label": "继续生成剩余母片",
                    }
                )
            elif job["state"] == "FAILED" and str(
                job.get("failure_code") or ""
            ).strip():
                failure = error_registry.describe_code(
                    job["failure_code"], stage="production"
                )
                job["next_action"] = {
                    "id": failure.next_action.id,
                    "label": failure.next_action.label,
                }
            else:
                default_action = _DEFAULT_VIDEO_ACTIONS.get(job["state"])
                if job["state"] == "COMPOSED" and (
                    job.get("latest_review") or {}
                ).get("decision") == "approved":
                    default_action = {
                        "id": "submit_publish", "label": "提交到待发布",
                    }
                job["next_action"] = (
                    dict(default_action) if default_action else None
                )
            if job["state"] in _TERMINAL_VIDEO_STATES:
                job["next_action"] = None
            job["allowed_actions"] = (
                [dict(job["next_action"])] if job["next_action"] else []
            )
            failure_code = str(job.get("failure_code") or "").strip()
            if failure_code:
                status_message = error_registry.describe_code(
                    failure_code, stage="production"
                ).user_message
            else:
                status_message = _VIDEO_STATE_REASONS.get(
                    job["state"], "任务状态已更新，请按下一步操作继续"
                )
            job["status_reason"] = {
                "code": failure_code or str(job["state"]).lower(),
                "message": status_message,
            }
            plan = recovery_plan.resolve(self.database, job["job_id"])
            if plan is not None:
                job["recovery"] = plan
                job["next_action"] = plan["next_action"]
                job["allowed_actions"] = plan["allowed_actions"]
                job["status_reason"] = {"code": "recovery_plan", "message": plan["reason"]}
                job["resumable"] = "resume_failed_video" in plan["action_ids"]
                for shot in shots:
                    shot["candidate_review_eligible"] = bool(
                        shot.get("candidate_preview_url") and "review_master_candidates" in plan["action_ids"]
                    )
            elif job['has_visual_requirements'] and job['state'] in {'FAILED', 'BGM_OK', 'PRODUCTION_REVIEW_REQUIRED'} and shots and all(s['state'] == 'OK' for s in shots) and (
                job.get('failure_code') == 'content_review_required' or (self.data_dir / 'assets' / job['job_id'] / 'content_retention.review.json').is_file()):
                job['next_action'] = {'id': 'review_visual_content', 'label': '复核画面内容并恢复本地合成'}
                job['allowed_actions'] = [job['next_action']]
        return {"jobs": jobs}

    def upgrade_director_plan(self, job_id: str) -> dict:
        job = self.database.execute(
            "SELECT state,cost_cents,storyboard FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not job:
            raise ValueError("视频任务不存在")
        if job["state"] != "STORYBOARDED":
            raise ValueError("只有尚未开始生成的待生成任务可以升级导演方案")
        activity = self.database.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM shots WHERE job_id=?) AS shot_count,"
            "(SELECT COUNT(*) FROM provider_calls WHERE job_id=?) AS call_count",
            (job_id, job_id),
        ).fetchone()
        if (int(job["cost_cents"] or 0) > 0
                or int(activity["shot_count"] or 0) > 0
                or int(activity["call_count"] or 0) > 0):
            raise ValueError("该任务已经产生付费调用或母片记录，不能重写导演方案")
        draft = self.database.execute(
            "SELECT draft_id,script_json FROM script_drafts "
            "WHERE approved_job_id=? AND status='approved' "
            "ORDER BY updated_at DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if not draft:
            raise ValueError("未找到该任务对应的已审核剧本，无法升级导演方案")
        try:
            previous = json.loads(job["storyboard"] or "{}")
            script = json.loads(draft["script_json"] or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("任务剧本或分镜数据损坏，无法升级") from exc
        if (previous.get("director_plan") or {}).get("strategy") in {
            "dialogue_plus_broll", "cinematic_dialogue_only",
        }:
            raise ValueError("该任务已经使用新版导演方案")

        upgraded = script_drafts._storyboard(script)
        upgraded["production_settings"] = previous.get("production_settings") or {}
        old_characters = {
            str(character.get("id")): character
            for character in previous.get("characters") or []
            if character.get("id")
        }
        upgraded["characters"] = [
            {**character, **old_characters.get(str(character.get("id")), {})}
            for character in upgraded.get("characters") or []
        ]
        encoded = json.dumps(upgraded, ensure_ascii=False)
        self.database.execute(
            "UPDATE jobs SET storyboard=?,error=NULL,updated_at=CURRENT_TIMESTAMP "
            "WHERE job_id=? AND state='STORYBOARDED'",
            (encoded, job_id),
        )
        self.database.execute(
            "UPDATE script_drafts SET storyboard_json=?,updated_at=CURRENT_TIMESTAMP "
            "WHERE draft_id=?",
            (encoded, draft["draft_id"]),
        )
        self.database.execute(
            "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
            (job_id, "director_plan_upgrade", "info", "已升级为新版导演计划"),
        )
        self.database.commit()
        return {
            "job_id": job_id,
            "director_plan": upgraded.get("director_plan") or {},
            "master_count": len(upgraded.get("masters") or []),
        }

    def rebuild_failed_director_plan(self, job_id: str) -> dict:
        """Recompile a stopped job from its approved script before another paid call."""
        job = self.database.execute(
            "SELECT state,storyboard,characters_json FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not job:
            raise ValueError("视频任务不存在")
        if job["state"] not in {"FAILED", "BLOCKED"}:
            raise ValueError("只有失败或已暂停的视频任务可以重建导演方案")
        self._assert_no_unresolved_provider_tasks(job_id)
        preserved_master = self.database.execute(
            "SELECT idx,state,cost_cents,video_path FROM shots WHERE job_id=? "
            "AND (state='OK' OR cost_cents>0 OR COALESCE(video_path,'')!='') "
            "ORDER BY idx LIMIT 1",
            (job_id,),
        ).fetchone()
        if preserved_master:
            raise ValueError(
                "任务包含成功或已付费母片；为避免删除付费产物，重建已关闭。"
                "如需追加生成，请显式确认新增成本并使用单条返修。"
            )
        draft = self.database.execute(
            "SELECT draft_id,script_json FROM script_drafts "
            "WHERE approved_job_id=? AND status='approved' "
            "ORDER BY updated_at DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if not draft:
            raise ValueError("未找到该任务对应的已审核剧本，无法安全重建")
        try:
            previous = json.loads(job["storyboard"] or "{}")
            script = json.loads(draft["script_json"] or "{}")
            runtime_characters = json.loads(job["characters_json"] or "[]")
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("任务剧本、分镜或角色数据损坏，无法安全重建") from exc

        rebuilt = script_drafts._storyboard(script)
        rebuilt["production_settings"] = previous.get("production_settings") or {}
        old_characters = {
            str(character.get("id")): character
            for character in (runtime_characters or previous.get("characters") or [])
            if character.get("id")
        }
        rebuilt["characters"] = [
            {**character, **old_characters.get(str(character.get("id")), {})}
            for character in rebuilt.get("characters") or []
        ]
        scene_assets.apply_continuity_metadata(
            rebuilt, rebuilt.get("characters") or []
        )
        generation_units = rebuilt.get("masters") or rebuilt.get("shots") or []
        invalidated = int(self.database.execute(
            "SELECT COUNT(*) FROM shots WHERE job_id=?", (job_id,)
        ).fetchone()[0] or 0)
        self.database.execute("DELETE FROM shots WHERE job_id=?", (job_id,))
        for unit in generation_units:
            self.database.execute(
                "INSERT INTO shots(job_id,idx,duration_sec,shot_type,camera,prompt,"
                "voiceover,subtitle,state,narrative_type,speaker_id,anchor_character) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id, int(unit.get("idx") or 0),
                    float(unit.get("duration") or unit.get("duration_sec") or 0),
                    str(unit.get("shot_type") or ""),
                    str(unit.get("camera") or ""),
                    str(unit.get("scene") or unit.get("prompt") or ""),
                    str(unit.get("voiceover") or ""),
                    str(unit.get("subtitle") or unit.get("voiceover") or ""),
                    "PENDING", str(unit.get("type") or "dialogue"),
                    unit.get("speaker"), unit.get("anchor_character"),
                ),
            )
        encoded = json.dumps(rebuilt, ensure_ascii=False)
        encoded_characters = json.dumps(
            rebuilt.get("characters") or [], ensure_ascii=False
        )
        self.database.execute(
            "UPDATE jobs SET state='STORYBOARDED',storyboard=?,characters_json=?,"
            "error=NULL,retries=0,final_path=NULL,updated_at=CURRENT_TIMESTAMP "
            "WHERE job_id=? AND state IN ('FAILED','BLOCKED')",
            (encoded, encoded_characters, job_id),
        )
        self.database.execute(
            "UPDATE script_drafts SET storyboard_json=?,updated_at=CURRENT_TIMESTAMP "
            "WHERE draft_id=?",
            (encoded, draft["draft_id"]),
        )
        self.database.execute("DELETE FROM review_events WHERE job_id=?", (job_id,))
        self.database.execute(
            "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
            (
                job_id, "director_plan_rebuild", "info",
                "失败任务已按已审核剧本重建可执行导演计划；历史成本保留",
            ),
        )
        self.database.commit()
        return {
            "job_id": job_id,
            "state": "STORYBOARDED",
            "invalidated_masters": invalidated,
            "master_count": len(generation_units),
            "director_plan": rebuilt.get("director_plan") or {},
        }

    def resume_failed_video(self, job_id: str) -> dict:
        return self.production_service.resume(job_id)

    def _resume_completed_master_downloads(
        self, job_id: str, retry_indexes: list[int], *, completed: int,
    ) -> dict | None:
        """Requeue bound completed tasks for GET-only recovery, atomically."""
        job = self.database.execute(
            "SELECT execution_engine FROM jobs WHERE job_id=?", (job_id,),
        ).fetchone()
        if not retry_indexes or job["execution_engine"] != "graph_v2":
            return None
        with self.database:
            self.database.execute("BEGIN IMMEDIATE")
            assessment = master_download_recovery.assess(
                self.database, job_id, retry_indexes
            )
            if assessment is None:
                return None
            if not assessment.eligible:
                raise ValueError(assessment.reason)
            for candidate in assessment.candidates:
                output = json.loads(candidate.output_json or "{}")
                output.pop("_executor_checkpoint", None)
                output["resume_provider_task_id"] = candidate.task_id
                node_state_machine.transition_node_in_transaction(
                    self.database, job_id, candidate.node_id, "retry",
                    event_id=f"{job_id}:download-recovery:{secrets.token_hex(12)}",
                    payload={"provider_task_id": candidate.task_id},
                )
                self.database.execute(
                    "UPDATE production_nodes SET output_json=?,lease_id=NULL,"
                    "heartbeat_at=NULL,error_code=NULL,next_action=NULL "
                    "WHERE job_id=? AND node_id=?",
                    (json.dumps(output, ensure_ascii=False), job_id, candidate.node_id),
                )
                self.database.execute(
                    "UPDATE shots SET state='PENDING',error=NULL,video_qa=NULL "
                    "WHERE job_id=? AND idx=?", (job_id, candidate.index),
                )
            state_machine.transition(
                self.database, job_id, "queued",
                event_id=f"{job_id}:download-recovery-queued:{secrets.token_hex(12)}",
                updates={"error": None, "failure_code": None, "retries": 0,
                         "production_worker_pid": None},
            )
        return {
            "job_id": job_id, "state": "PRODUCTION_QUEUED",
            "recovery_kind": "completed_master_download",
            "preserved_masters": completed,
            "remaining_masters": len(assessment.candidates),
            "manual_review_masters": 0,
        }

    def _resume_failed_video_impl(self, job_id: str) -> dict:
        job = self.database.execute(
            "SELECT state,bgm_path,failure_code,cost_cents FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not job:
            raise ValueError("视频任务不存在")
        resume_from_state = str(job["state"])
        if resume_from_state not in {
            "FAILED", "PROVIDER_ACTION_REQUIRED", "BGM_OK",
        }:
            raise ValueError("只有失败或账号处理完成的视频任务可以继续生成")
        plan = recovery_plan.resolve(self.database, job_id)
        if (
            plan is not None
            and "resume_failed_video" in plan["action_ids"]
            and plan.get("recovery_kind") != "completed_master_download"
        ):
            return recovery_plan.resume_ready_graph(self.database, job_id)
        if plan is not None and "review_master_candidates" not in plan["action_ids"]:
            recovery_plan.require_action(self.database, job_id, "resume_failed_video")
        shots = self.database.execute(
            "SELECT idx,state,video_qa FROM shots WHERE job_id=? ORDER BY idx",
            (job_id,),
        ).fetchall()
        if resume_from_state == "BGM_OK":
            unresolved = provider_tasks.unresolved_task_ids(self.database, job_id)
            if unresolved:
                raise ValueError(
                    "存在尚未核对的远端任务，请先核对 task_id："
                    + ", ".join(unresolved[:5])
                )
            if not shots or any(row["state"] != "OK" for row in shots):
                raise ValueError("只有全部母片已完成的任务可以继续本地合成")
            bgm_value = str(job["bgm_path"] or "").strip()
            bgm = Path(bgm_value).resolve() if bgm_value else None
            music_provider = self._music_provider()
            try:
                if not (bgm and bgm.is_file()):
                    asyncio.run(s5_bgm.run(job_id, provider=music_provider))
                asyncio.run(s6_compose.run(job_id))
                finalize_accepted_candidates(
                    self.database, self.data_dir / "assets", job_id
                )
            except Exception as exc:
                self.database.execute(
                    "UPDATE jobs SET error=?,updated_at=CURRENT_TIMESTAMP "
                    "WHERE job_id=? AND state='BGM_OK'",
                    (f"local postproduction failed: {exc}", job_id),
                )
                self.database.commit()
                raise RuntimeError(f"local postproduction failed: {exc}") from exc
            state_machine.transition(
                self.database,
                job_id,
                "composed",
                event_id=f"{job_id}:resume-compose:{secrets.token_hex(12)}",
                updates={"error": None, "failure_code": None, "retries": 0},
            )
            self.database.execute(
                "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
                (job_id, "local_postproduction_resume", "info", "本地成片合成已恢复完成"),
            )
            self.database.commit()
            return {
                "job_id": job_id,
                "state": "COMPOSED",
                "preserved_masters": len(shots),
                "remaining_masters": 0,
                "manual_review_masters": 0,
                "local_postproduction": True,
            }
        failed_rows = [row for row in shots if row["state"] == "FAILED"]
        pending_rows = [row for row in shots if row["state"] == "PENDING"]
        shot_failure_codes: list[str] = []
        for row in failed_rows:
            try:
                qa = json.loads(row["video_qa"] or "{}")
            except (json.JSONDecodeError, TypeError):
                qa = {}
            code = str(qa.get("failure_code") or "").strip()
            if code:
                shot_failure_codes.append(code)
        structured_failure_codes = {
            str(job["failure_code"] or "").strip(), *shot_failure_codes,
        } - {""}
        music_configuration_retry_ready = bool(
            structured_failure_codes == {"music_configuration_invalid"}
            and shots
            and all(row["state"] == "OK" for row in shots)
        )
        identity_anchor_retry_ready = _request_invalid_identity_anchor_retry_ready(
            self.database,
            job_id=job_id,
            state=resume_from_state,
            failure_code=job["failure_code"],
            cost_cents=job["cost_cents"],
            shots=list(shots),
        )
        if (
            structured_failure_codes
            and structured_failure_codes != {"account_unavailable"}
            and not identity_anchor_retry_ready
            and not music_configuration_retry_ready
        ):
            raise ValueError(
                "该失败任务的稳定 failure code 不可恢复；"
                "隐私、内容政策、版权及其他 Provider 拒绝禁止重新提交"
            )
        if resume_from_state == "PROVIDER_ACTION_REQUIRED" or structured_failure_codes:
            unresolved = provider_tasks.unresolved_task_ids(self.database, job_id)
            if unresolved:
                raise ValueError(
                    "存在尚未核对的远端任务，请先核对 task_id："
                    + ", ".join(unresolved[:5])
                )
        if structured_failure_codes == {"account_unavailable"}:
            provider_health.assert_available(self.database, "seedance")
            account_shot_failure = bool(
                failed_rows
                and all(
                    code == "account_unavailable" for code in shot_failure_codes
                )
                and len(shot_failure_codes) == len(failed_rows)
                and all(row["state"] in {"OK", "FAILED"} for row in shots)
            )
            account_anchor_failure = bool(
                str(job["failure_code"] or "") == "account_unavailable"
                and pending_rows
                and all(row["state"] in {"OK", "PENDING"} for row in shots)
            )
            if not (account_shot_failure or account_anchor_failure):
                raise ValueError(
                    "该 Provider 暂停任务的稳定 failure code 不可恢复；"
                    "仅账号阻断解除后的 account_unavailable 任务可继续生成"
                )
        elif resume_from_state == "PROVIDER_ACTION_REQUIRED":
            raise ValueError(
                "该 Provider 暂停任务缺少可恢复的稳定 failure code"
            )
        completed = sum(row["state"] == "OK" for row in shots)
        review_indexes: list[int] = []
        retry_indexes: list[int] = []
        for row in shots:
            if row["state"] == "OK":
                continue
            try:
                qa = json.loads(row["video_qa"] or "{}")
            except (json.JSONDecodeError, TypeError):
                qa = {}
            has_review_artifact = bool(controlled_candidate_path(
                self.data_dir / "assets", job_id,
                qa.get("candidate_path"),
            ))
            if (
                qa.get("status") == "manual_review_required"
                and has_review_artifact
            ):
                review_indexes.append(int(row["idx"]))
            else:
                retry_indexes.append(int(row["idx"]))
        remaining = len(retry_indexes)
        unresolved = provider_tasks.unresolved_task_ids(self.database, job_id)
        if unresolved:
            if review_indexes:
                placeholders = ",".join("?" for _ in review_indexes)
                self.database.execute(
                    f"UPDATE shots SET state='REVIEW_REQUIRED' "
                    f"WHERE job_id=? AND idx IN ({placeholders})",
                    (job_id, *review_indexes),
                )
            message = (
                "存在尚未核对的远端任务，已保留本地候选决定；"
                "请先核对 task_id：" + ", ".join(unresolved[:5])
            )
            state_machine.transition(
                self.database,
                job_id,
                "provider_paused",
                event_id=f"{job_id}:resume-provider:{secrets.token_hex(12)}",
                updates={
                    "error": message,
                    "production_worker_pid": None,
                },
            )
            self.database.commit()
            return {
                "job_id": job_id,
                "state": "PROVIDER_ACTION_REQUIRED",
                "preserved_masters": completed,
                "remaining_masters": remaining,
                "manual_review_masters": len(review_indexes),
                "unresolved_provider_tasks": unresolved,
            }
        if review_indexes:
            placeholders = ",".join("?" for _ in review_indexes)
            self.database.execute(
                f"UPDATE shots SET state='REVIEW_REQUIRED' "
                f"WHERE job_id=? AND idx IN ({placeholders})",
                (job_id, *review_indexes),
            )
            state_machine.transition(
                self.database,
                job_id,
                "resume_review",
                event_id=f"{job_id}:resume-review:{secrets.token_hex(12)}",
                updates={
                    "error": f"仍有 {len(review_indexes)} 条母片待人工复核",
                    "failure_code": None,
                },
            )
            self.database.commit()
            return {
                "job_id": job_id,
                "state": "PRODUCTION_REVIEW_REQUIRED",
                "preserved_masters": completed,
                "remaining_masters": remaining,
                "manual_review_masters": len(review_indexes),
            }
        if not remaining:
            bgm_value = str(job["bgm_path"] or "").strip()
            bgm = Path(bgm_value).resolve() if bgm_value else None
            music_provider = self._music_provider()
            if (
                not (bgm and bgm.is_file())
                and music.may_submit_paid_work(music_provider)
            ):
                self._assert_no_unresolved_provider_tasks(job_id)
            state_machine.transition(
                self.database,
                job_id,
                "resume_local_post",
                event_id=f"{job_id}:resume-local:{secrets.token_hex(12)}",
                updates={"error": None, "failure_code": None},
            )
            self.database.commit()
            try:
                if not (bgm and bgm.is_file()):
                    asyncio.run(s5_bgm.run(job_id, provider=music_provider))
                state_machine.transition(
                    self.database,
                    job_id,
                    "bgm_completed",
                    event_id=f"{job_id}:resume-bgm:{secrets.token_hex(12)}",
                    updates={"error": None},
                )
                self.database.commit()
                asyncio.run(s6_compose.run(job_id))
                finalize_accepted_candidates(
                    self.database, self.data_dir / "assets", job_id
                )
            except Exception as exc:
                outcome = error_registry.describe_failure(
                    exc, stage="local_postproduction"
                )
                state_machine.transition(
                    self.database,
                    job_id,
                    "failed",
                    event_id=f"{job_id}:resume-local-failed:{secrets.token_hex(12)}",
                    updates={
                        "error": outcome.user_message,
                        "failure_code": outcome.code,
                    },
                )
                self.database.commit()
                raise RuntimeError(f"local postproduction failed: {exc}") from exc
            state_machine.transition(
                self.database,
                job_id,
                "composed",
                event_id=f"{job_id}:resume-composed:{secrets.token_hex(12)}",
                updates={"error": None, "failure_code": None, "retries": 0},
            )
            self.database.commit()
            return {
                "job_id": job_id,
                "state": "COMPOSED",
                "preserved_masters": completed,
                "remaining_masters": 0,
                "manual_review_masters": 0,
                "local_postproduction": True,
            }
        self._assert_no_unresolved_provider_tasks(job_id)
        recovered = self._resume_completed_master_downloads(
            job_id, retry_indexes, completed=completed,
        )
        if recovered is not None:
            return recovered
        if identity_anchor_retry_ready:
            self.database.execute(
                "UPDATE character_identity_anchors SET state='PENDING',"
                "error=NULL,approval_state='PENDING_REVIEW',approved_at=NULL,"
                "review_note='',updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=? AND state='FAILED' AND cost_cents=0",
                (job_id,),
            )
        placeholders = ",".join("?" for _ in retry_indexes)
        self.database.execute(
            f"UPDATE shots SET state='PENDING',error=NULL,video_qa=NULL "
            f"WHERE job_id=? AND idx IN ({placeholders})",
            (job_id, *retry_indexes),
        )
        state_machine.transition(
            self.database,
            job_id,
            "resume_generation",
            event_id=f"{job_id}:resume-generation:{secrets.token_hex(12)}",
            updates={
                "retries": 0,
                "error": None,
                "failure_code": None,
            },
        )
        self.database.commit()
        return {
            "job_id": job_id,
            "state": "STORYBOARDED",
            "preserved_masters": completed,
            "remaining_masters": remaining,
            "manual_review_masters": len(review_indexes),
        }

    def retry_network_failures(self, job_id: str, *, confirmed: bool = False) -> dict:
        indexes = network_recovery.queue_retry(self.database, job_id, confirmed=confirmed)
        dispatch = self.dispatch_production_queue()
        return {"job_id": job_id, "retried_master_indexes": indexes,
                "state": "PRODUCTION_RUNNING" if job_id in dispatch["dispatched_jobs"] else "PRODUCTION_QUEUED"}

    def prepare_master_regeneration(
        self, job_id: str, master_idx: int, *, reason: str = ""
    ) -> dict:
        """Invalidate one master while preserving every other paid result."""
        job = self.database.execute(
            "SELECT state,storyboard FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not job:
            raise ValueError("视频任务不存在")
        if job["state"] not in {"COMPOSED", "FAILED", "BLOCKED"}:
            raise ValueError("只有已完成或已停止的视频任务可以返修单个母片")
        self._assert_no_unresolved_provider_tasks(job_id)
        target = self.database.execute(
            "SELECT idx,state FROM shots WHERE job_id=? AND idx=?",
            (job_id, int(master_idx)),
        ).fetchone()
        if not target:
            raise ValueError("需要返修的母片不存在")
        if target["state"] not in {"OK", "FAILED"}:
            raise ValueError("该母片尚未形成可返修结果")
        other_incomplete = self.database.execute(
            "SELECT COUNT(*) FROM shots WHERE job_id=? AND idx!=? AND state!='OK'",
            (job_id, int(master_idx)),
        ).fetchone()[0]
        if other_incomplete:
            raise ValueError("任务还有其他未完成母片，请先使用继续生成处理")
        reason_text = str(reason or "").strip()
        if not reason_text:
            raise ValueError("请填写该母片需要返修的原因")
        try:
            storyboard = json.loads(job["storyboard"] or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("视频任务的导演计划无法读取") from exc
        generation_units = storyboard.get("masters") or storyboard.get("shots") or []
        master_plan = next((
            item for item in generation_units
            if int(item.get("idx") or 0) == int(master_idx)
        ), None)
        if master_plan is None:
            raise ValueError("导演计划中不存在该母片")
        for edit in storyboard.get("shots") or []:
            audio_idx = int(edit.get(
                "audio_master_idx", edit.get("video_master_idx", -1)
            ))
            if audio_idx != int(master_idx):
                continue
            old_cues = edit.get("subtitle_cues") or []
            if not edit.get("subtitle") and old_cues:
                edit["subtitle"] = "".join(
                    str(cue.get("text") or "") for cue in old_cues
                ).strip()
            edit["subtitle_cues"] = []
            edit.pop("speech_timeline_version", None)
        repair_instruction = (
            f"本次返修必须解决：{reason_text}。"
            "严格按返修意见改变场景、动作或表演，不得复刻上一版的问题。"
        )
        master_plan["repair_direction"] = reason_text
        if str(master_plan.get("type") or "dialogue") == "broll":
            scene_hints = [
                str(edit.get("broll_hint") or "").strip()
                for edit in (storyboard.get("shots") or [])
                if int(edit.get("video_master_idx") or 0) == int(master_idx)
                and str(edit.get("broll_hint") or "").strip()
            ]
            master_prompt = (
                "生成一段无对白、无旁白的电影化 B-roll。"
                f"{repair_instruction}"
                f"具体场景要求：{'；'.join(scene_hints)}。"
                "必须先出现完整环境建立镜头，再展示行业主体、关键物件和真实操作动作；"
                "禁止固定胸像口播，禁止无语义的手部或手指特写，禁止可读文字、数字、"
                "商标和水印。"
            )
            master_plan["prompt"] = master_prompt
            master_plan["scene"] = master_prompt
        else:
            master_prompt = (
                f"{str(master_plan.get('prompt') or master_plan.get('scene') or '').strip()}"
                f"{repair_instruction}"
            )
            master_plan["prompt"] = master_prompt
            if master_plan.get("scene"):
                master_plan["scene"] = (
                    f"{str(master_plan['scene']).strip()}{repair_instruction}"
                )
            if master_plan.get("action"):
                master_plan["action"] = (
                    f"{str(master_plan['action']).strip()}{repair_instruction}"
                )
        encoded_storyboard = json.dumps(storyboard, ensure_ascii=False)
        self.database.execute(
            "UPDATE shots SET state='PENDING',prompt=?,duration_sec=?,provider=NULL,"
            "video_model=NULL,native_audio=0,video_qa=NULL,error=NULL "
            "WHERE job_id=? AND idx=?",
            (
                master_prompt,
                float(master_plan.get("duration") or 0),
                job_id,
                int(master_idx),
            ),
        )
        self.database.execute(
            "UPDATE jobs SET state='STORYBOARDED',storyboard=?,error=NULL,"
            "retries=0,updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
            (encoded_storyboard, job_id),
        )
        # Any approval applies to the old composition and must never unlock the
        # regenerated result for publication.
        self.database.execute(
            "DELETE FROM review_events WHERE job_id=?", (job_id,)
        )
        self.database.execute(
            "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
            (
                job_id, "master_regeneration", "info",
                f"母片 #{int(master_idx)} 已进入返修：{reason_text}",
            ),
        )
        self.database.commit()
        preserved = self.database.execute(
            "SELECT COUNT(*) FROM shots WHERE job_id=? AND state='OK'",
            (job_id,),
        ).fetchone()[0]
        return {
            "job_id": job_id,
            "state": "STORYBOARDED",
            "regeneration_master_idx": int(master_idx),
            "preserved_masters": int(preserved or 0),
            "remaining_masters": 1,
        }

    def prepare_master_lip_sync_repair(
        self, job_id: str, master_idx: int, *, start_sec: float,
        end_sec: float, reason: str = "",
    ) -> dict:
        """Queue a Seedance edit that changes only one master's mouth motion."""
        job = self.database.execute(
            "SELECT state,storyboard FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not job:
            raise ValueError("视频任务不存在")
        if job["state"] not in {
            "COMPOSED", "FAILED", "BLOCKED", "PRODUCTION_REVIEW_REQUIRED",
        }:
            raise ValueError("只有已生成或待人工复核的视频任务可以修复口型")
        self._assert_no_unresolved_provider_tasks(job_id)
        target = self.database.execute(
            "SELECT idx,state,duration_sec,cost_cents,video_path,video_url,"
            "video_qa FROM shots "
            "WHERE job_id=? AND idx=?",
            (job_id, int(master_idx)),
        ).fetchone()
        if not target:
            raise ValueError("需要修复的母片不存在")
        if target["state"] not in {"OK", "FAILED", "REVIEW_REQUIRED"}:
            raise ValueError("该母片尚未形成可修复的视频")
        other_incomplete = self.database.execute(
            "SELECT COUNT(*) FROM shots WHERE job_id=? AND idx!=? AND state!='OK'",
            (job_id, int(master_idx)),
        ).fetchone()[0]
        if other_incomplete:
            raise ValueError("任务还有其他未完成母片，请先完成或复核后再修复口型")

        try:
            storyboard = json.loads(job["storyboard"] or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("视频任务的导演计划无法读取") from exc
        masters = storyboard.get("masters") or storyboard.get("shots") or []
        master = next((
            item for item in masters
            if int(item.get("idx") or 0) == int(master_idx)
        ), None)
        if master is None:
            raise ValueError("导演计划中不存在该母片")
        if str(master.get("type") or "dialogue") != "dialogue":
            raise ValueError("只有包含人物对白的母片可以执行口型局部修复")

        source = controlled_job_asset_path(
            self.data_dir / "assets", job_id, target["video_path"],
            require_file=True,
        )
        source_kind = "active_master"
        source_url = str(target["video_url"] or "").strip()
        if source is None:
            try:
                qa = json.loads(target["video_qa"] or "{}")
            except (json.JSONDecodeError, TypeError):
                qa = {}
            source = controlled_candidate_path(
                self.data_dir / "assets", job_id,
                qa.get("candidate_path"),
            )
            source_kind = "review_candidate"
            source_url = str(qa.get("candidate_url") or "").strip()
        if (
            source is None
            and str(master.get("repair_mode") or "")
            == "seedance_lip_sync_edit"
        ):
            source = controlled_job_asset_path(
                self.data_dir / "assets", job_id,
                master.get("repair_source_path"), require_file=True,
            )
            source_kind = "persisted_repair_source"
            source_url = str(master.get("repair_source_url") or "").strip()
        if source is None:
            raise ValueError("原始母片文件不存在，不能执行保留式口型修复")

        duration = max(0.1, float(
            master.get("duration") or target["duration_sec"] or 0
        ))
        start = max(0.0, float(start_sec))
        end = min(duration, float(end_sec))
        if end <= start:
            raise ValueError("口型修复结束时间必须晚于开始时间且位于母片时长内")
        reason_text = str(reason or "").strip() or "口型与原生语音不同步"
        interval_dialogue: list[str] = []
        for edit in storyboard.get("shots") or []:
            if int(edit.get("audio_master_idx") or 0) != int(master_idx):
                continue
            audio_start = float(edit.get("audio_start") or 0)
            for cue in edit.get("subtitle_cues") or []:
                cue_start = audio_start + float(cue.get("start") or 0)
                cue_end = audio_start + float(cue.get("end") or cue_start)
                if cue_end <= start or cue_start >= end:
                    continue
                text = str(cue.get("text") or "").strip()
                if text and text not in interval_dialogue:
                    interval_dialogue.append(text)
        repair_dialogue = "，".join(interval_dialogue).strip() or str(
            master.get("voiceover") or ""
        ).strip()
        native_audio = source.with_suffix(".wav")
        master.update({
            "repair_mode": "seedance_lip_sync_edit",
            "repair_source_path": str(source),
            "repair_start_sec": start,
            "repair_end_sec": end,
            "repair_dialogue": repair_dialogue,
            "repair_direction": reason_text,
            "repair_baseline_cost_cents": max(
                0,
                int(
                    master.get("repair_baseline_cost_cents")
                    or target["cost_cents"]
                    or 0
                ),
            ),
        })
        if source_url.startswith("https://"):
            master["repair_source_url"] = source_url
        else:
            master.pop("repair_source_url", None)
        if native_audio.is_file():
            master["repair_audio_path"] = str(native_audio)
        repair_prompt = s3_shots._build_lip_sync_edit_prompt({
            **master,
            "duration_sec": duration,
        })
        self.database.execute(
            "UPDATE shots SET state='PENDING',prompt=?,provider=NULL,"
            "video_model=NULL,native_audio=0,error=NULL "
            "WHERE job_id=? AND idx=?",
            (repair_prompt, job_id, int(master_idx)),
        )
        self.database.execute(
            "UPDATE jobs SET state='STORYBOARDED',storyboard=?,error=NULL,"
            "retries=0,updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
            (json.dumps(storyboard, ensure_ascii=False), job_id),
        )
        self.database.execute("DELETE FROM review_events WHERE job_id=?", (job_id,))
        self.database.execute(
            "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
            (
                job_id, "master_lip_sync_repair", "info",
                f"母片 #{int(master_idx)} 已进入 Seedance 2.5 "
                f"{start:g}-{end:g} 秒口型局部修复",
            ),
        )
        self.database.commit()
        return {
            "job_id": job_id,
            "state": "STORYBOARDED",
            "repair_kind": "seedance_lip_sync_edit",
            "repair_source_kind": source_kind,
            "repair_master_idx": int(master_idx),
            "repair_start_sec": start,
            "repair_end_sec": end,
        }

    def prepare_master_extension(
        self, job_id: str, master_idx: int, *, extension_sec: float,
        direction: str,
    ) -> dict:
        """Queue a Seedance extend candidate without replacing the prior master."""
        job = self.database.execute(
            "SELECT state,storyboard FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not job:
            raise ValueError("视频任务不存在")
        if job["state"] not in {
            "COMPOSED", "FAILED", "BLOCKED", "PRODUCTION_REVIEW_REQUIRED",
        }:
            raise ValueError("只有已生成或待人工复核的视频任务可以延长母片")
        self._assert_no_unresolved_provider_tasks(job_id)
        target = self.database.execute(
            "SELECT idx,state,duration_sec,video_path FROM shots "
            "WHERE job_id=? AND idx=?",
            (job_id, int(master_idx)),
        ).fetchone()
        if not target:
            raise ValueError("需要延长的母片不存在")
        if target["state"] not in {"OK", "FAILED", "REVIEW_REQUIRED"}:
            raise ValueError("该母片尚未形成可延长的视频")
        other_incomplete = self.database.execute(
            "SELECT COUNT(*) FROM shots WHERE job_id=? AND idx!=? AND state!='OK'",
            (job_id, int(master_idx)),
        ).fetchone()[0]
        if other_incomplete:
            raise ValueError("任务还有其他未完成母片，请先完成或复核后再延长")
        try:
            extension = float(extension_sec)
        except (TypeError, ValueError) as exc:
            raise ValueError("延长时长必须在 4 到 30 秒之间") from exc
        if extension < 4 or extension > 30:
            raise ValueError("延长时长必须在 4 到 30 秒之间")
        direction_text = str(direction or "").strip()
        if not direction_text:
            raise ValueError("请填写延长后的动作、运镜和结束状态")

        try:
            storyboard = json.loads(job["storyboard"] or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("视频任务的导演计划无法读取") from exc
        masters = storyboard.get("masters") or storyboard.get("shots") or []
        master = next((
            item for item in masters
            if int(item.get("idx") or 0) == int(master_idx)
        ), None)
        if master is None:
            raise ValueError("导演计划中不存在该母片")
        source = controlled_job_asset_path(
            self.data_dir / "assets", job_id, target["video_path"],
            require_file=True,
        )
        if source is None:
            raise ValueError("原始母片文件不存在，不能执行保留式延长")

        for key in (
            "repair_start_sec", "repair_end_sec", "repair_dialogue",
            "repair_audio_path",
        ):
            master.pop(key, None)
        master.update({
            "repair_mode": "seedance_extend",
            "repair_source_path": str(source),
            "extend_duration_sec": extension,
            "extend_direction": direction_text,
        })
        extend_prompt = s3_shots._build_extend_prompt({
            **master,
            "duration_sec": float(
                master.get("duration") or target["duration_sec"] or 0
            ),
        })
        self.database.execute(
            "UPDATE shots SET state='PENDING',prompt=?,provider=NULL,"
            "video_model=NULL,native_audio=0,error=NULL "
            "WHERE job_id=? AND idx=?",
            (extend_prompt, job_id, int(master_idx)),
        )
        self.database.execute(
            "UPDATE jobs SET state='STORYBOARDED',storyboard=?,error=NULL,"
            "retries=0,updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
            (json.dumps(storyboard, ensure_ascii=False), job_id),
        )
        self.database.execute("DELETE FROM review_events WHERE job_id=?", (job_id,))
        self.database.execute(
            "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
            (
                job_id, "master_extension", "info",
                f"母片 #{int(master_idx)} 已进入 Seedance 2.5 "
                f"延长 {extension:g} 秒候选生成",
            ),
        )
        self.database.commit()
        return {
            "job_id": job_id,
            "state": "STORYBOARDED",
            "repair_kind": "seedance_extend",
            "repair_master_idx": int(master_idx),
            "extension_sec": extension,
        }

    def configure_video(self, job_id: str, settings: dict) -> dict:
        row = self.database.execute(
            "SELECT state,storyboard,execution_engine FROM jobs WHERE job_id=?", (job_id,),
        ).fetchone()
        if not row:
            raise ValueError("视频任务不存在")
        if row["state"] != "STORYBOARDED":
            raise ValueError("只有待生成任务可以修改制作设置")
        try:
            storyboard = json.loads(row["storyboard"] or "{}")
        except json.JSONDecodeError:
            storyboard = {}
        performance_beats = [
            beat
            for master in storyboard.get("masters") or []
            for beat in (master.get("performance_beats") or [])
            if isinstance(beat, dict)
        ]
        explicit_on_screen = {
            str(beat.get("speaker") or "").strip()
            for beat in performance_beats
            if str(beat.get("speaker") or "").strip()
            and script_drafts.normalize_speech_mode(
                beat.get("speech_mode"),
                speaker=str(beat.get("speaker") or "").strip(),
            ) == "on_screen"
        }
        voice_only = {
            str(beat.get("speaker") or "").strip()
            for beat in performance_beats
            if str(beat.get("speaker") or "").strip()
            and script_drafts.normalize_speech_mode(
                beat.get("speech_mode"),
                speaker=str(beat.get("speaker") or "").strip(),
            ) != "on_screen"
        } - explicit_on_screen

        def visible_candidate(value: object) -> str:
            candidate = str(value or "").strip()
            if not candidate or candidate in voice_only:
                return ""
            if (
                candidate not in explicit_on_screen
                and script_drafts.normalize_speech_mode(
                    None, speaker=candidate
                ) != "on_screen"
            ):
                return ""
            return candidate

        speaker_candidates: list[str] = [
            visible_candidate(item.get("id"))
            for item in (storyboard.get("characters") or [])
            if visible_candidate(item.get("id"))
        ]
        for master in storyboard.get("masters") or []:
            if master.get("type") != "dialogue":
                continue
            speaker_candidates.append(visible_candidate(master.get("speaker")))
            speaker_candidates.extend(
                visible_candidate(speaker)
                for speaker in (master.get("speakers") or [])
            )
            speaker_candidates.extend(
                visible_candidate(beat.get("speaker"))
                for beat in (master.get("performance_beats") or [])
                if isinstance(beat, dict)
                and script_drafts.normalize_speech_mode(
                    beat.get("speech_mode"),
                    speaker=str(beat.get("speaker") or "").strip(),
                ) == "on_screen"
            )
        speakers = list(dict.fromkeys(
            speaker for speaker in speaker_candidates if speaker
        ))
        if not speakers:
            speakers = [
                visible_candidate(item.get("id"))
                for item in (storyboard.get("characters") or [])
                if visible_candidate(item.get("id"))
            ]
        raw_mappings = settings.get("character_mappings") or {}
        if not isinstance(raw_mappings, dict):
            raise ValueError("人物角色映射格式无效")
        character_id = str(settings.get("character_id") or "").strip()
        if character_id and not speakers:
            speakers = ["presenter"]
        if character_id and not raw_mappings:
            raw_mappings = {speaker: character_id for speaker in speakers or ["presenter"]}
        character_mappings = {
            str(speaker).strip(): str(asset_character_id or "").strip()
            for speaker, asset_character_id in raw_mappings.items()
            if str(speaker).strip()
        }
        character_mappings = {
            speaker: character_mappings.get(speaker, "") for speaker in speakers
        }
        script_characters = {
            str(item.get("id") or ""): item
            for item in (storyboard.get("characters") or [])
            if str(item.get("id") or "")
        }
        runtime_characters = []
        for speaker in speakers:
            mapped_character_id = character_mappings[speaker]
            script_character = script_characters.get(speaker, {})
            if not mapped_character_id:
                fallback_look = str(
                    script_character.get("look")
                    or script_character.get("image_prompt")
                    or f"按剧本中“{speaker}”的台词、动作与场景描述生成一致人物形象"
                ).strip()
                runtime_characters.append({
                    **script_character,
                    "id": speaker,
                    "name": str(script_character.get("name") or speaker),
                    "script_character_name": str(
                        script_character.get("name") or speaker
                    ),
                    "gender": str(
                        script_character.get("gender") or "unspecified"
                    ),
                    "look": fallback_look,
                    "background_description": str(
                        script_character.get("background_description")
                        or fallback_look
                    ),
                    "generation_mode": "ai_generated",
                })
                continue
            character = self.database.execute(
            "SELECT character_id,display_name,identity_type,gender,asset_id,asset_uri,"
                "background_description "
                "FROM character_asset_bindings "
                "WHERE character_id=? AND status='Active'", (mapped_character_id,),
            ).fetchone()
            if not character:
                raise ValueError(f"人物 {speaker} 指定的角色未启用或不存在")
            runtime_character = {
                "id": speaker,
                "name": character["display_name"],
                "script_character_name": str(
                    script_character.get("name") or speaker
                ),
                "generation_mode": "fixed_asset",
                "asset_character_id": character["character_id"],
                "identity_type": character["identity_type"],
                "gender": (
                    character["gender"]
                    or str(script_character.get("gender") or "female")
                ),
                "asset_id": character["asset_id"],
                "asset_uri": character["asset_uri"],
                "background_description": character["background_description"],
                "look": character["background_description"],
            }
            if character["identity_type"] == "custom":
                paths = self._character_image_paths(character["character_id"])
                if not paths:
                    raise ValueError(f"人物 {speaker} 指定的自定义角色尚未上传图片")
                runtime_character["image_path"] = str(paths[0])
                runtime_character["reference_images"] = [str(path) for path in paths]
                runtime_character["seedance_reference_mode"] = "description_only"
            else:
                runtime_character["seedance_asset_uri"] = character["asset_uri"]
            runtime_characters.append(runtime_character)
        orientation = str(settings.get("orientation") or "9:16")
        if orientation not in {"9:16", "16:9", "1:1"}:
            raise ValueError("视频画幅无效")
        budget = int(settings.get("budget_cents") or 0)
        if budget <= 0:
            raise ValueError("单任务预算必须大于 0")
        try:
            anchor_review_mode = s3_shots.normalize_anchor_review_mode(
                settings.get("anchor_review_mode")
            )
        except ValueError as exc:
            raise ValueError("锚点审核方式必须是自动或人工") from exc
        normalized = {
            "character_id": (
                next(iter(character_mappings.values()), "")
                if character_mappings
                and len(set(character_mappings.values())) <= 1 else ""
            ),
            "character_mappings": character_mappings,
            "orientation": orientation,
            "production_profile": str(settings.get("production_profile") or "standard"),
            "subtitle_style": str(settings.get("subtitle_style") or "clean"),
            "asr_enabled": bool(settings.get("asr_enabled", True)),
            "audio_mode": normalize_audio_mode(
                settings.get("audio_mode")
                or _read_env(self.env_path).get("DIALOGUE_AUDIO_MODE"),
                strict=True,
            ),
            "anchor_review_mode": anchor_review_mode,
            "budget_cents": budget,
            "character_anchor_generator": anchor_generation.normalize_generator(
                settings.get("character_anchor_generator")
                or _read_env(self.env_path).get("CHARACTER_ANCHOR_GENERATOR"),
                strict=True,
            ),
            "scene_anchor_generator": anchor_generation.normalize_generator(
                settings.get("scene_anchor_generator")
                or _read_env(self.env_path).get("SCENE_ANCHOR_GENERATOR"),
                strict=True,
            ),
        }
        snapshot_input = dict(normalized)
        for field in (
            "video_model", "model", "resolution", "output_format",
            "reference_video_budget", "max_concurrent_masters", "qa_enabled",
            "max_auto_retries", "auto_retry_budget_cents",
        ):
            if field in settings:
                snapshot_input[field] = settings[field]
        normalized[production_config.GENERATION_SNAPSHOT_KEY] = (
            production_config.freeze_generation_snapshot(
                snapshot_input,
                env=_read_env(self.env_path),
            )
        )
        storyboard["production_profile"] = normalized["production_profile"]
        storyboard["production_settings"] = normalized
        if runtime_characters:
            storyboard["characters"] = runtime_characters
        scene_assets.apply_continuity_metadata(
            storyboard, runtime_characters
        )
        generation_units = storyboard.get("masters") or storyboard.get("shots") or []
        storyboard["master_dependency_graph"] = (
            master_dependencies.build_master_dependency_graph(generation_units)
        )
        # Production settings may be revisited while resuming a partially paid
        # job.  Keep successful masters so changing presentation settings never
        # discards usable provider output or causes it to be purchased again.
        completed_indexes = {
            int(row["idx"])
            for row in self.database.execute(
                "SELECT idx FROM shots WHERE job_id=? AND state='OK'", (job_id,)
            ).fetchall()
        }
        self.database.execute(
            "DELETE FROM shots WHERE job_id=? AND (state IS NULL OR state!='OK')",
            (job_id,),
        )
        for unit in generation_units:
            unit_idx = int(unit.get("idx") or 0)
            if unit_idx in completed_indexes:
                continue
            self.database.execute(
                "INSERT INTO shots(job_id,idx,duration_sec,shot_type,camera,prompt,"
                "voiceover,subtitle,state,narrative_type,speaker_id,anchor_character) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id, unit_idx,
                    float(unit.get("duration") or unit.get("duration_sec") or 0),
                    str(unit.get("shot_type") or ""), str(unit.get("camera") or ""),
                    str(unit.get("scene") or unit.get("prompt") or ""),
                    str(unit.get("voiceover") or ""),
                    str(unit.get("subtitle") or unit.get("voiceover") or ""),
                    "PENDING", str(unit.get("type") or "dialogue"),
                    unit.get("speaker"), unit.get("anchor_character"),
                ),
            )
        self.database.execute(
            "UPDATE jobs SET storyboard=?,characters_json=?,"
            "updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
            (
                json.dumps(storyboard, ensure_ascii=False),
                json.dumps(runtime_characters, ensure_ascii=False),
                job_id,
            ),
        )
        self.database.commit()
        result = {
            "job_id": job_id,
            "production_settings": normalized,
            "master_dependency_graph": storyboard["master_dependency_graph"],
        }
        if str(row["execution_engine"] or "legacy") == "graph_v2":
            result["compilation"] = self.production_service.compile(job_id)
        return result

    def review_video(
        self, job_id: str, *, decision: str, manual_minutes: float = 0,
        notes: str = "", first_pass: bool | None = None,
        checks: dict[str, bool] | None = None,
    ) -> dict:
        return self.production_service.review(
            job_id,
            decision=decision,
            manual_minutes=manual_minutes,
            notes=notes,
            first_pass=first_pass,
            checks=checks,
        )

    def _review_video_impl(
        self, job_id: str, *, decision: str, manual_minutes: float = 0,
        notes: str = "", first_pass: bool | None = None,
        checks: dict[str, bool] | None = None,
    ) -> dict:
        row = self.database.execute(
            "SELECT state,final_path,execution_engine FROM jobs WHERE job_id=?",
            (job_id,)
        ).fetchone()
        if not row:
            raise ValueError("视频任务不存在")
        if row['state'] == 'READY_TO_PUBLISH' and decision == 'approved':
            with self._final_review_lock, node_state_machine.atomic(self.database):
                live = self.database.execute('SELECT state FROM jobs WHERE job_id=?', (job_id,)).fetchone()
                report = content_review.require_final(self.database, self.data_dir / 'assets', job_id)
                if live and live['state'] == 'READY_TO_PUBLISH' and report and all(bool((checks or {}).get(key)) for key in REVIEW_CHECKS):
                    if row['execution_engine'] == 'graph_v2':
                        event_id = f'{job_id}:final-review:approved:' + content_review.approval_revision(self.database, job_id, report)
                        prior = self.database.execute('SELECT 1 FROM production_node_events WHERE job_id=? AND event_id=? AND event=\'approve\'', (job_id, event_id)).fetchone()
                    else:
                        prior = self.database.execute('SELECT 1 FROM review_events WHERE job_id=? AND decision=\'approved\' LIMIT 1', (job_id,)).fetchone()
                    if prior:
                        return {'job_id': job_id, 'decision': decision, 'publish_unlocked': True, 'already_reviewed': True}
        if row["state"] != "COMPOSED":
            raise ValueError("只有已完成的成片可以进行人工验收")
        if not row["final_path"] or not Path(row["final_path"]).is_file():
            raise ValueError("成品文件不存在，不能进行人工验收")
        normalized_checks = {
            key: bool((checks or {}).get(key)) for key in REVIEW_CHECKS
        }
        if decision == "approved" and not all(normalized_checks.values()):
            raise ValueError("验收通过前必须确认全部验收项")
        if decision != "approved" and not str(notes or "").strip():
            raise ValueError("验收不通过时必须填写问题说明")
        if decision == 'approved':
            content_review.require_final(self.database, self.data_dir / 'assets', job_id)
        with self._final_review_lock:
            self.database.execute("BEGIN IMMEDIATE")
            try:
                # Re-read after acquiring the write lock. The earlier read is
                # only a fast validation path and cannot authorize mutation.
                locked_row = self.database.execute(
                    "SELECT state,final_path,execution_engine FROM jobs "
                    "WHERE job_id=?", (job_id,),
                ).fetchone()
                if locked_row is None or str(locked_row["state"]) != "COMPOSED":
                    raise ValueError("final_review is stale: job is not COMPOSED")
                if decision == 'approved':
                    binding = content_review.require_final(self.database, self.data_dir / 'assets', job_id)
                if not locked_row["final_path"] or not Path(
                    locked_row["final_path"]
                ).is_file():
                    raise ValueError("final_review media is missing")

                is_graph = (
                    str(locked_row["execution_engine"] or "legacy")
                    == "graph_v2"
                )
                if is_graph:
                    final_review = self.database.execute(
                        "SELECT state FROM production_nodes WHERE job_id=? "
                        "AND node_id='final_review'", (job_id,),
                    ).fetchone()
                    if (
                        final_review is None
                        or str(final_review["state"]) != "REVIEW_REQUIRED"
                    ):
                        raise ValueError(
                            "final_review node is not awaiting review"
                        )

                if decision == "approved":
                    reviewed_at = time.strftime("%Y-%m-%d %H:%M:%S")
                    flagged = self.database.execute(
                        "SELECT idx,video_qa FROM shots WHERE job_id=?",
                        (job_id,),
                    ).fetchall()
                    for shot in flagged:
                        try:
                            qa = json.loads(shot["video_qa"] or "{}")
                        except (json.JSONDecodeError, TypeError):
                            qa = {}
                        if qa.get("status") != "manual_review_required":
                            continue
                        qa["automated_status"] = "manual_review_required"
                        qa["status"] = "human_review_approved"
                        qa["human_decision"] = "final_approved"
                        qa["reviewed_at"] = reviewed_at
                        self.database.execute(
                            "UPDATE shots SET video_qa=? WHERE job_id=? "
                            "AND idx=?",
                            (
                                json.dumps(qa, ensure_ascii=False),
                                job_id, shot["idx"],
                            ),
                        )

                production_metrics.record_review_in_transaction(
                    self.database, job_id, str(decision or "").strip(),
                    manual_minutes=float(manual_minutes or 0),
                    notes=str(notes or "").strip() or None,
                    first_pass=first_pass,
                    checks=normalized_checks,
                )
                if is_graph:
                    node_state_machine.transition_node_in_transaction(
                        self.database,
                        job_id,
                        "final_review",
                        "approve" if decision == "approved" else "reject",
                        event_id=(
                            f"{job_id}:final-review:{str(decision).strip()}" +
                            (':' + content_review.approval_revision(self.database, job_id, binding) if decision == 'approved' and binding else '')
                        ),
                        payload={
                            "decision": str(decision),
                            "notes": str(notes or "").strip(),
                        },
                    )
                    projected_state = production_projection.reconcile(
                        self.database, job_id
                    ).projected_job_state
                else:
                    projected_state = (
                        "READY_TO_PUBLISH"
                        if decision == "approved" else "COMPOSED"
                    )
                updated = self.database.execute(
                    "UPDATE jobs SET state=?,error=NULL,"
                    "updated_at=CURRENT_TIMESTAMP WHERE job_id=? "
                    "AND state='COMPOSED'",
                    (projected_state, job_id),
                )
                if updated.rowcount != 1:
                    raise ValueError("final_review lost its job-state compare-and-swap")
                self.database.commit()
            except Exception:
                self.database.rollback()
                raise
            persisted = self.database.execute(
                "SELECT state FROM jobs WHERE job_id=?", (job_id,),
            ).fetchone()
        return {
            "job_id": job_id, "decision": decision,
            "publish_unlocked": (
                persisted is not None
                and str(persisted["state"]) == "READY_TO_PUBLISH"
            ),
        }

    def delete_video(self, job_id: str) -> dict:
        row = self.database.execute(
            "SELECT state FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not row:
            raise ValueError("视频任务不存在")
        if row["state"] in {
            "PROVIDER_ACTION_REQUIRED", "REJECTED_PENDING_RECONCILIATION",
        }:
            raise ValueError("任务仍有远端成本待核对，不能删除")
        if row["state"] in {
            "PENDING", "PRODUCTION_QUEUED", "PRODUCTION_RUNNING",
            "SHOTS_OK", "VOICE_OK", "BGM_OK",
        }:
            raise ValueError("生成中的视频任务不能删除")
        if row["state"] in {"READY_TO_PUBLISH", "PUBLISHED"}:
            raise ValueError("已进入发布流水线的视频记录不能删除")
        if self.database.execute(
            "SELECT 1 FROM platform_publications WHERE job_id=?", (job_id,)
        ).fetchone():
            raise ValueError("已有发布记录的视频不能删除")
        linked_draft = self.database.execute(
            "SELECT draft_id FROM script_drafts WHERE approved_job_id=? "
            "ORDER BY updated_at DESC LIMIT 1", (job_id,),
        ).fetchone()
        restored_draft_id = linked_draft["draft_id"] if linked_draft else None
        # Deleting a failed production is a rollback to the script decision,
        # not a deletion of the content project. Clear all dangling job links
        # so the chosen script can be reviewed and submitted again.
        self.database.execute(
            "UPDATE script_drafts SET status='candidate',approved_job_id=NULL,"
            "storyboard_json=NULL,updated_at=CURRENT_TIMESTAMP "
            "WHERE approved_job_id=?", (job_id,),
        )
        self.database.execute(
            "DELETE FROM character_identity_anchors WHERE job_id=?", (job_id,)
        )
        self.database.execute("DELETE FROM shots WHERE job_id=?", (job_id,))
        self.database.execute("DELETE FROM events WHERE job_id=?", (job_id,))
        self.database.execute(
            "DELETE FROM job_transitions WHERE job_id=?", (job_id,)
        )
        self.database.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
        self.database.commit()
        self._repair_dangling_job_links()
        dispatch = self.dispatch_production_queue()
        return {
            "job_id": job_id,
            "deleted": True,
            "dispatched_jobs": dispatch["dispatched_jobs"],
            "restored_draft_id": restored_draft_id,
        }

    def repair_execution_contract(self, job_id: str) -> dict:
        """Repair disposable graph-v2 projections and resume without new spec."""
        row = self.database.execute(
            "SELECT state,execution_engine,failure_code FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not row:
            raise ValueError("视频任务不存在")
        if str(row["execution_engine"] or "legacy") != "graph_v2":
            raise ValueError("该任务未使用生产图执行引擎，不能执行此恢复操作")
        state = str(row["state"] or "")
        if state in {"PRODUCTION_QUEUED", "PRODUCTION_RUNNING"}:
            return {
                "job_id": job_id, "state": state, "recovered": True,
                "repaired_codes": [], "dispatched_jobs": [],
            }
        if state != "FAILED" or str(
            row["failure_code"] or ""
        ) != "execution_contract_invalid":
            raise ValueError("当前任务不处于生产数据恢复状态")

        report = production_invariants.repair_safe_projections(
            self.database, job_id,
        )
        if report.blockers:
            raise ValueError(
                "生产数据包含已付费或已生成媒体，系统已保留现场，"
                "不能自动覆盖；请查看恢复诊断"
            )
        retried_nodes = production_executor.retry_repaired_contract_nodes(
            self.database, job_id,
        )
        state_machine.transition(
            self.database,
            job_id,
            "queued",
            event_id=f"{job_id}:contract-repaired:{secrets.token_hex(8)}",
            updates={
                "error": None,
                "failure_code": None,
                "production_worker_pid": None,
                "recovery_attempts": 0,
            },
            payload={
                "repaired_codes": list(report.repaired_codes),
                "retried_nodes": list(retried_nodes),
            },
        )
        dispatch = self.dispatch_production_queue()
        return {
            "job_id": job_id,
            "state": "PRODUCTION_QUEUED",
            "recovered": True,
            "repaired_codes": list(report.repaired_codes),
            "retried_nodes": list(retried_nodes),
            "dispatched_jobs": dispatch.get("dispatched_jobs", []),
        }

    def _production_limit(self) -> int:
        value = _read_env(self.env_path).get("MAX_CONCURRENT_JOBS") or "1"
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 1

    def _production_worker_command(self, job_id: str) -> list[str]:
        runtime = self.root / ".venv" / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        python = runtime if runtime.exists() else Path(sys.executable)
        row = self.database.execute(
            "SELECT execution_engine FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        execution_engine = (
            str(row["execution_engine"] or "legacy") if row else "legacy"
        )
        if execution_engine == "graph_v2":
            return [
                str(python), str(self.root / "scripts" / "cli.py"),
                "produce-graph", job_id, "--dispatched",
            ]
        return [
            str(python), str(self.root / "scripts" / "cli.py"),
            "produce-approved", job_id, "--yes", "--dispatched",
        ]

    def dispatch_production_queue(self) -> dict:
        """Atomically claim queued jobs up to the local concurrency limit."""
        if self.database.in_transaction:
            # A shared-connection supervisor can see uncommitted queue rows.
            # Never commit its caller's command or launch work before that
            # caller has chosen to commit; the next supervisor tick can retry.
            return {'worker_mode': self.worker_mode, 'queued_jobs': [],
                    'dispatched_jobs': [], 'failed_workers': [], 'dispatch_deferred': True}
        if self.worker_mode == "external":
            queued = self.database.execute(
                "SELECT job_id FROM jobs WHERE state='PRODUCTION_QUEUED' "
                "ORDER BY priority DESC, created_at ASC, job_id ASC"
            ).fetchall()
            return {
                "worker_mode": "external",
                "queued_jobs": [str(row["job_id"]) for row in queued],
                "dispatched_jobs": [],
                "failed_workers": [],
            }
        exited_jobs: list[str] = []
        for job_id, worker in list(self._production_workers.items()):
            returncode = worker.poll()
            if returncode is None or not isinstance(returncode, int):
                continue
            self._production_workers.pop(job_id, None)
            current = self.database.execute(
                "SELECT state FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            worker_owned_states = {
                "PRODUCTION_RUNNING", "SHOTS_OK", "VOICE_OK", "BGM_OK",
            }
            if current and str(current["state"]) not in worker_owned_states:
                self.database.execute(
                    "UPDATE jobs SET production_worker_pid=NULL "
                    "WHERE job_id=?",
                    (job_id,),
                )
                self.database.commit()
                continue
            try:
                state_machine.transition(
                    self.database,
                    job_id,
                    "failed",
                    event_id=(
                        f"{job_id}:worker-exit:{returncode}:"
                        f"{secrets.token_hex(8)}"
                    ),
                    updates={
                        "error": "本地生产进程意外退出，任务现场已保留",
                        "failure_code": "internal_error",
                        "production_worker_pid": None,
                    },
                    payload={"worker_exit_code": int(returncode)},
                )
                exited_jobs.append(job_id)
            except state_machine.InvalidTransitionError:
                self.database.execute(
                    "UPDATE jobs SET production_worker_pid=NULL "
                    "WHERE job_id=?",
                    (job_id,),
                )
                self.database.commit()
        self._reconcile_orphaned_production_workers()
        active_states = (
            "PRODUCTION_RUNNING", "SHOTS_OK", "VOICE_OK", "BGM_OK",
        )
        active = self.database.execute(
            "SELECT COUNT(*) FROM jobs WHERE state IN (?,?,?,?)",
            active_states,
        ).fetchone()[0]
        available = max(0, self._production_limit() - int(active or 0))
        if not available:
            return {"dispatched_jobs": []}
        queued = self.database.execute(
            "SELECT job_id FROM jobs WHERE state='PRODUCTION_QUEUED' "
            "ORDER BY priority DESC, created_at ASC, job_id ASC LIMIT ?",
            (available,),
        ).fetchall()
        dispatched: list[str] = []
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        for row in queued:
            job_id = row["job_id"]
            try:
                state_machine.transition(
                    self.database,
                    job_id,
                    "production_started",
                    event_id=f"{job_id}:worker:{secrets.token_hex(12)}",
                    updates={
                        "error": None,
                        "failure_code": None,
                        "production_heartbeat_at": (
                            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        ),
                        "worker_lease_id": None,
                    },
                )
            except state_machine.InvalidTransitionError:
                continue
            try:
                worker = subprocess.Popen(
                    self._production_worker_command(job_id),
                    cwd=self.root,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creation_flags,
                )
                self._production_workers[job_id] = worker
                worker_pid = getattr(worker, "pid", None)
                self.database.execute(
                    "UPDATE jobs SET production_worker_pid=?,"
                    "production_started_at=CURRENT_TIMESTAMP,"
                    "updated_at=CURRENT_TIMESTAMP WHERE job_id=? "
                    "AND state='PRODUCTION_RUNNING'",
                    (
                        worker_pid if isinstance(worker_pid, int) else None,
                        job_id,
                    ),
                )
                self.database.commit()
            except Exception:
                state_machine.transition(
                    self.database,
                    job_id,
                    "worker_start_failed",
                    event_id=(
                        f"{job_id}:worker-start-failed:{secrets.token_hex(12)}"
                    ),
                    updates={"production_worker_pid": None},
                )
                raise
            dispatched.append(job_id)
        return {"dispatched_jobs": dispatched, "failed_workers": exited_jobs}

    def submit_for_publish(self, job_id: str) -> dict:
        content_review.require_final(self.database, self.data_dir / 'assets', job_id)
        with self._final_review_lock, node_state_machine.atomic(self.database):
            content_review.require_final(self.database, self.data_dir / 'assets', job_id)
            return self._submit_for_publish_impl(job_id)

    def _submit_for_publish_impl(self, job_id: str) -> dict:
        row = self.database.execute(
            "SELECT state,final_path FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not row:
            raise ValueError("视频任务不存在")
        if row["state"] == "READY_TO_PUBLISH":
            return {"job_id": job_id, "state": "READY_TO_PUBLISH"}
        if row["state"] != "COMPOSED":
            raise ValueError("只有已完成的成品才能提交发布")
        if not row["final_path"] or not Path(row["final_path"]).is_file():
            raise ValueError("成品文件不存在，不能提交发布")
        review = self.database.execute(
            "SELECT decision FROM review_events WHERE job_id=? "
            "ORDER BY id DESC LIMIT 1", (job_id,),
        ).fetchone()
        if not review or review["decision"] != "approved":
            raise ValueError("成片必须先完成人工验收并通过，才能提交发布")
        self.database.execute(
            "UPDATE jobs SET state='READY_TO_PUBLISH',error=NULL,updated_at=CURRENT_TIMESTAMP "
            "WHERE job_id=?", (job_id,)
        )
        return {"job_id": job_id, "state": "READY_TO_PUBLISH"}

    def video_file(self, job_id: str) -> Path:
        row = self.database.execute(
            "SELECT final_path FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not row or not row["final_path"]:
            raise ValueError("成品文件不存在")
        path = Path(row["final_path"]).resolve()
        if not path.is_file():
            raise ValueError("成品文件不存在")
        return path

    def video_cover_file(self, job_id: str) -> Path:
        """Return the cover extracted from the final timeline's first frame."""
        final_path = self.video_file(job_id)
        cover = final_path.parent / "thumbnail.jpg"
        allowed_root = (self.data_dir / "assets" / str(job_id)).resolve()
        resolved = cover.resolve()
        if not resolved.is_relative_to(allowed_root) or not resolved.is_file():
            raise ValueError("成片首帧封面不存在")
        return resolved

    def master_candidate_file(self, job_id: str, master_idx: int) -> Path:
        row = self.database.execute(
            "SELECT video_qa FROM shots WHERE job_id=? AND idx=?",
            (job_id, int(master_idx)),
        ).fetchone()
        if not row:
            raise ValueError("待复核候选母片不存在")
        try:
            qa = json.loads(row["video_qa"] or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("待复核候选母片不存在") from exc
        raw_path = str(qa.get("candidate_path") or "").strip()
        if not raw_path:
            raise ValueError("待复核候选母片不存在")
        path = controlled_candidate_path(
            self.data_dir / "assets", job_id, raw_path
        )
        if path is None:
            raise ValueError("待复核候选母片不存在")
        return path

    def visual_repair_plan(self, job_id: str, master_idx: int, source='candidate') -> dict:
        from . import visual_repair
        return visual_repair.plan(self.database, self.data_dir / 'assets', job_id, master_idx, source)

    def apply_visual_recut(self, job_id: str, master_idx: int, payload: dict) -> dict:
        from . import visual_repair
        caller_owns_transaction = self.database.in_transaction
        # Own one command transaction, including invalidation and queueing. A
        # caller's transaction remains its caller's transaction (savepoints).
        with self._final_review_lock, node_state_machine.atomic(self.database):
            job = self.database.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if not job or job['state'] in visual_repair.TERMINATED:
                raise ValueError('content terminated or missing job cannot recut')
            if payload.get('confirmed') is not True or payload.get('source') != 'active':
                raise ValueError('content explicit active picture-window confirmation required')
            self._assert_no_unresolved_provider_tasks(job_id)
            sb = content_review.object_json(job['storyboard'])
            command = content_review.digest({'master_idx': int(master_idx), 'payload': payload})
            overlay = sb.get(visual_repair.OVERLAY_KEY) or {}
            if overlay.get('command_fingerprint') == command:
                rows = self.database.execute('SELECT idx,state,video_path,video_qa FROM shots WHERE job_id=? ORDER BY idx', (job_id,)).fetchall()
                visual_repair.verify_overlay(self.database, job_id, sb, rows)
                return {'job_id': job_id, 'local_only': True, 'already_queued': True, 'state': job['state'], 'dispatch_deferred': caller_owns_transaction}
            current = self.visual_repair_plan(job_id, master_idx, 'active')
            if set(payload) != {*current['guards'], 'source', 'confirmed', 'edits'}:
                raise ValueError('content local recut accepts only explicit picture edits and current guards')
            if any(payload.get(key) != value for key, value in current['guards'].items()):
                raise ValueError('content local recut is stale; refresh selected source and repair plan')
            if current['unresolved_provider_task_ids']:
                raise ValueError('content reconcile existing paid task first')
            local = current['routes'][0]
            if not local['executable_now']:
                raise ValueError('content safe local recut unavailable: ' + local['reason'])
            node = self.database.execute("SELECT * FROM production_nodes WHERE job_id=? AND node_id='local_compose'", (job_id,)).fetchone()
            if job['execution_engine'] != 'graph_v2' or not node or node['state'] not in {'SUCCEEDED', 'REVIEW_REQUIRED'}:
                raise ValueError('content local recut requires paused graph local compose')
            rows = self.database.execute('SELECT idx,state,video_path,video_qa FROM shots WHERE job_id=? ORDER BY idx', (job_id,)).fetchall()
            durations = {int(idx): binding['duration_sec'] for idx, binding in current['guards']['expected_source_bindings'].items()}
            edited = visual_repair.make_overlay(sb, payload.get('edits'), current['guards'], durations)
            if not visual_repair.authored_safe(self.database, job_id, payload['edits'], sb['shots']):
                raise ValueError('content local recut cannot change authored mouth/coverage constraints')
            changed = {e['edit_idx'] for e in payload['edits']}
            if any(int(e.get('video_master_idx', -1)) != int(master_idx) for e in sb['shots'] if e['idx'] in changed):
                raise ValueError('content local recut must stay within selected master')
            if content_review.retention(edited, rows, durations)['passed'] is not True:
                raise ValueError('content local recut must retain all complete observed actions after fades/output order')
            edited[visual_repair.OVERLAY_KEY]['command_fingerprint'] = command
            self.database.execute('UPDATE jobs SET storyboard=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=?',
                (json.dumps(edited, ensure_ascii=False, allow_nan=False), job_id))
            if node['state'] == 'SUCCEEDED':
                self._invalidate_content_final(job_id, command)
            elif node['error_code'] != 'content_review_required':
                raise ValueError('content local compose is paused for a different reason')
            # Existing recovery authority closes the final approval cycle and
            # readies only local compose; it never schedules masters or music.
            result = self.resume_visual_content(job_id)
            return {**result, 'repair_route': 'local_recut', 'edits': payload['edits'], 'dispatch_deferred': caller_owns_transaction}

    def content_review_source_file(self, job_id: str, master_idx: int, source='candidate') -> Path:
        return content_review.selected(self.database, self.data_dir / 'assets', job_id, master_idx, source)[-1]

    def align_cached_visual_timeline(self, job_id: str, master_idx: int, payload: dict) -> dict:
        from . import visual_repair
        caller_owns_transaction = self.database.in_transaction
        with self._final_review_lock, node_state_machine.atomic(self.database):
            job = self.database.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if not job or job['state'] in visual_repair.TERMINATED:
                raise ValueError('content terminated or missing job cannot realign')
            current = self.visual_repair_plan(job_id, master_idx, 'active')
            if (payload.get('confirmed') is not True or payload.get('source') != 'active' or
                set(payload) != {*current['guards'], 'source', 'confirmed'}):
                raise ValueError('content explicit cached alignment/discard confirmation required')
            request_fingerprint = content_review.digest({'master_idx': int(master_idx), 'payload': payload})
            sb = content_review.object_json(job['storyboard'])
            prior = next((item for item in sb.get('local_visual_edit_history') or []
                          if item.get('request_fingerprint') == request_fingerprint), None)
            if prior:
                stable_guards = ('expected_asset_sha256', 'expected_requirement_set_hash', 'expected_spec_hash', 'expected_contract_hash', 'expected_source_bindings', 'expected_frozen_alignment_edit_plan_fingerprint')
                if (any(payload.get(key) != current['guards'][key] for key in stable_guards) or
                    prior['cache_bindings'] != current['guards']['expected_asr_cache_bindings']['frozen']):
                    raise ValueError('content cached alignment repeat is stale')
                self._assert_no_unresolved_provider_tasks(job_id)
                return {'job_id': job_id, 'state': job['state'], 'local_only': True, 'alignment_only': True, 'already_queued': True, 'dispatch_deferred': caller_owns_transaction}
            if any(payload.get(key) != value for key, value in current['guards'].items()):
                raise ValueError('content cached alignment is stale; refresh current source and ASR cache')
            if not current['local_alignment_recovery']['executable_now']:
                raise ValueError('content alignment requires matching cached ASR and accepted, paused local graph')
            self._assert_no_unresolved_provider_tasks(job_id)
            overlay = sb.pop(visual_repair.OVERLAY_KEY, None)
            prior_alignment = sb.get(visual_repair.ALIGNMENT_KEY)
            if overlay:
                sb['shots'] = deepcopy(overlay['base_shots'])
            sb[visual_repair.ALIGNMENT_KEY] = {'authority': visual_repair.AUTHORITY,
                'spec_hash': current['guards']['expected_spec_hash'], 'contract_hash': current['guards']['expected_contract_hash'],
                'source_bindings': current['guards']['expected_source_bindings'],
                'confirmed_live_edit_plan_fingerprint': current['guards']['expected_edit_plan_fingerprint'],
                'queued_live_edit_plan_fingerprint': content_review.edit_plan_fingerprint(sb),
                'live_edit_plan_fingerprint': content_review.edit_plan_fingerprint(sb),
                'frozen_alignment_edit_plan_fingerprint': current['guards']['expected_frozen_alignment_edit_plan_fingerprint'],
                'asr_cache_bindings': current['local_alignment_recovery']['cache_bindings']}
            epoch = content_review.digest({'payload': payload, 'prior_overlay': overlay})
            history = sb.setdefault('local_visual_edit_history', [])
            if not isinstance(history, list):
                raise ValueError('content local edit audit history is invalid')
            history.append({'authority': visual_repair.AUTHORITY, 'action': 'discard_overlay_and_align_cached_speech',
                'prior_overlay': overlay, 'cache_bindings': current['local_alignment_recovery']['cache_bindings'],
                'prior_alignment_authorization': prior_alignment,
                'command_fingerprint': epoch, 'request_fingerprint': request_fingerprint})
            self.database.execute('UPDATE jobs SET storyboard=? WHERE job_id=?', (json.dumps(sb, ensure_ascii=False, allow_nan=False), job_id))
            node = self.database.execute("SELECT state FROM production_nodes WHERE job_id=? AND node_id='local_compose'", (job_id,)).fetchone()
            if node['state'] == 'SUCCEEDED':
                self._invalidate_content_final(job_id, epoch)
            return {**self.resume_visual_content(job_id, _cached_alignment=True), 'alignment_only': True, 'dispatch_deferred': caller_owns_transaction}

    def resume_visual_content(self, job_id: str, *, _cached_alignment=False) -> dict:
        with self._final_review_lock, node_state_machine.atomic(self.database):
            job = self.database.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if not job:
                raise ValueError('content job not found')
            sb = content_review.object_json(job['storyboard'])
            if not content_review.enabled(sb):
                raise ValueError('content contract required for local resume')
            if job['state'] in {'PUBLISHED', 'REJECTED', 'REJECTED_PENDING_RECONCILIATION'}:
                raise ValueError('content terminated job cannot resume')
            self._assert_no_unresolved_provider_tasks(job_id)
            rows = self.database.execute('SELECT idx,state,video_path,video_qa FROM shots WHERE job_id=? ORDER BY idx', (job_id,)).fetchall()
            if not rows or any(r['state'] != 'OK' for r in rows):
                raise ValueError('content local resume requires all masters OK')
            for row in rows:
                self.content_review_source_file(job_id, row['idx'], 'active')
            report = content_review.retention(sb, rows, {int(r['idx']): content_review.probe_duration(Path(r['video_path'])) for r in rows})
            if _cached_alignment:
                from . import visual_repair, production_compiler
                frozen = production_compiler.ensure_director_timeline(content_review.object_json(job['production_spec_json']).get('runtime_storyboard') or {})
                if not visual_repair.cached_alignment(self.database, job_id, frozen, rows)['available']:
                    raise ValueError('content local alignment requires matching cached ASR; new ASR calls are not authorized')
            if report.get('passed') is not True and not _cached_alignment:
                raise ValueError('content local resume blocked: ' + ', '.join(report['reasons']) + '; review current source or locally recut/repair cropped action')
            bgm = self._controlled_job_asset(job_id, job['bgm_path'], label='content BGM', require_file=True)
            if bgm is None:
                raise ValueError('content local resume requires existing BGM; paid regeneration is not authorized')
            if job['state'] in {'COMPOSED', 'READY_TO_PUBLISH'}:
                try:
                    content_review.require_final(self.database, self.data_dir / 'assets', job_id)
                except ValueError:
                    epoch = self.database.execute('SELECT COALESCE(MAX(rowid),0) FROM job_transitions WHERE job_id=?', (job_id,)).fetchone()[0]
                    self._invalidate_content_final(job_id, f'local-resume:{epoch}')
                    job = self.database.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
                else:
                    return {'job_id': job_id, 'state': job['state'], 'already_queued': True, 'local_only': True}
            node = self.database.execute("SELECT * FROM production_nodes WHERE job_id=? AND node_id='local_compose'", (job_id,)).fetchone()
            if job['execution_engine'] == 'graph_v2':
                if not node:
                    raise ValueError('content local compose node missing')
                if node['state'] in {'READY', 'RUNNING', 'SUCCEEDED'}:
                    prior = self.database.execute("SELECT 1 FROM production_node_events WHERE job_id=? AND node_id='local_compose' AND event='resume_visual_content'", (job_id,)).fetchone()
                    if prior:
                        return {'job_id': job_id, 'state': job['state'], 'already_queued': True, 'local_only': True}
                recovery_plan.require_action(self.database, job_id, 'review_visual_content')
                if node['state'] != 'REVIEW_REQUIRED' or node['error_code'] != 'content_review_required':
                    raise ValueError('content local compose is not awaiting content review')
                epoch = self.database.execute("SELECT COALESCE(MAX(rowid),0) FROM production_node_events WHERE job_id=? AND node_id='local_compose'", (job_id,)).fetchone()[0]
                node_state_machine.transition_node_in_transaction(self.database, job_id, 'local_compose', 'resume_visual_content',
                    event_id=f'{job_id}:content-local-resume:{epoch}', payload={'edit_plan_fingerprint': content_review.edit_plan_fingerprint(sb), 'cached_alignment_only': bool(_cached_alignment)})
                self.database.execute("UPDATE production_nodes SET error_code=NULL,next_action=NULL,output_json='{}' WHERE job_id=? AND node_id='local_compose'", (job_id,))
                self.database.execute('UPDATE jobs SET error=NULL,failure_code=NULL WHERE job_id=?', (job_id,))
                projected = production_projection.persist(self.database, job_id)
                return {'job_id': job_id, 'state': projected.projected_job_state, 'already_queued': False, 'local_only': True}
            if job['state'] not in {'FAILED', 'BGM_OK', 'PRODUCTION_REVIEW_REQUIRED'}:
                raise ValueError('content legacy local pause changed')
            state_machine.transition(self.database, job_id, 'resume_content_local',
                event_id=f'{job_id}:content-local-resume:{content_review.digest(report)}', updates={'error': None, 'failure_code': None})
        # Existing legacy BGM_OK path preserves the already-available music.
        return self._resume_failed_video_impl(job_id)

    def content_review_detail(self, job_id: str, master_idx: int, source='candidate') -> dict:
        _, _, sb, master, qa, path = content_review.selected(
            self.database, self.data_dir / 'assets', job_id, master_idx, source)
        content_review.validate_contract(sb)
        reqs = content_review.requirements(master)
        sha = content_review.visual_evidence.asset_fingerprint(path)
        try:
            duration = content_review.probe_duration(path)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise ValueError('content selected source duration unavailable') from exc
        return {'job_id': job_id, 'master_idx': int(master_idx), 'source': source,
            'asset_sha256': sha, 'requirement_set_hash': content_review.visual_contract.requirement_set_hash(reqs),
            'duration_sec': duration, 'requirements': reqs, 'visual_plan': master.get('visual_plan'),
            'automatic_findings': content_review.visual_evidence.evaluate(reqs, content_review.automatic(qa),
                asset_sha256=sha, duration_sec=duration),
            'human_review': content_review.matching_human(qa, reqs, sha),
            'preview_url': f'/api/videos/{job_id}/masters/{int(master_idx)}/content-preview?source={source}'}

    def record_content_review(self, job_id: str, master_idx: int, payload: dict) -> dict:
        source = payload.get('source', 'candidate')
        with self._final_review_lock, node_state_machine.atomic(self.database):
            # File, shot and contract are re-read under the same write lock that
            # owns persistence. Client hashes never establish human authority.
            detail = self.content_review_detail(job_id, master_idx, source)
            current_job = self.database.execute('SELECT state FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if current_job['state'] in {'PUBLISHED', 'REJECTED', 'REJECTED_PENDING_RECONCILIATION'}:
                raise ValueError('content review cannot mutate a terminated job')
            if (payload.get('expected_asset_sha256') != detail['asset_sha256'] or
                payload.get('expected_requirement_set_hash') != detail['requirement_set_hash']):
                raise ValueError('content review is stale; refresh selected source')
            items = content_review.validate_items(detail['requirements'], payload.get('items'), detail['duration_sec'])
            _, shot, _, _, qa, path = content_review.selected(self.database, self.data_dir / 'assets', job_id, master_idx, source)
            if content_review.visual_evidence.asset_fingerprint(path) != detail['asset_sha256']:
                raise ValueError('content asset changed during review')
            record = {'version': 1, 'authority': content_review.AUTHORITY, 'provenance': 'human',
                'asset_sha256': detail['asset_sha256'], 'requirement_set_hash': detail['requirement_set_hash'],
                'items': items}
            existing = detail['human_review']
            if existing and existing.get('items') == items:
                return {'job_id': job_id, 'master_idx': int(master_idx), 'review': existing, 'already_recorded': True}
            record['reviewed_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            records = qa.get('content_reviews') or []
            if not isinstance(records, list):
                records = []
            qa['content_reviews'] = [*records, record]
            self.database.execute('UPDATE shots SET video_qa=? WHERE job_id=? AND idx=?',
                (json.dumps(qa, ensure_ascii=False, allow_nan=False), job_id, int(master_idx)))
            self.database.execute('UPDATE jobs SET updated_at=CURRENT_TIMESTAMP WHERE job_id=?', (job_id,))
            if source in {'active', 'prior'} and current_job['state'] in {'COMPOSED', 'READY_TO_PUBLISH'}:
                self._invalidate_content_final(job_id, content_review.digest(record))
            return {'job_id': job_id, 'master_idx': int(master_idx), 'review': record, 'already_recorded': False}

    def _invalidate_content_final(self, job_id: str, epoch: str) -> None:
        job = self.database.execute('SELECT execution_engine FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if job['execution_engine'] == 'graph_v2':
            node_state_machine.transition_node_in_transaction(self.database, job_id, 'local_compose', 'invalidate_content',
                event_id=f'{job_id}:content-invalidate:{epoch}')
            final = self.database.execute("SELECT state FROM production_nodes WHERE job_id=? AND node_id='final_review'", (job_id,)).fetchone()
            if final and final['state'] in {'REVIEW_REQUIRED', 'SUCCEEDED'}:
                node_state_machine.transition_node_in_transaction(self.database, job_id, 'final_review', 'invalidate_final_review',
                    event_id=f'{job_id}:content-final-invalidate:{epoch}')
            self.database.execute("UPDATE production_nodes SET error_code='content_review_required',next_action='review_visual_content' WHERE job_id=? AND node_id='local_compose'", (job_id,))
            production_projection.persist(self.database, job_id)
        else:
            state_machine.transition(self.database, job_id, 'content_review_changed', event_id=f'{job_id}:content-invalidate:{epoch}')
        self.database.execute("UPDATE jobs SET failure_code='content_review_required',error='Content proof changed; review and locally recompose' WHERE job_id=?", (job_id,))

    def master_repair_source_file(
        self, job_id: str, master_idx: int
    ) -> Path:
        row = self.database.execute(
            "SELECT storyboard FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not row:
            raise ValueError("视频任务不存在")
        try:
            storyboard = json.loads(row["storyboard"] or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("原始修复母片不存在") from exc
        masters = storyboard.get("masters") or storyboard.get("shots") or []
        master = next((
            item for item in masters
            if int(item.get("idx") or 0) == int(master_idx)
        ), None)
        if (
            not master
            or str(master.get("repair_mode") or "")
            != "seedance_lip_sync_edit"
        ):
            raise ValueError("原始修复母片不存在")
        path = controlled_job_asset_path(
            self.data_dir / "assets",
            job_id,
            master.get("repair_source_path"),
            require_file=True,
        )
        if path is None:
            raise ValueError("原始修复母片不存在")
        return path

    def _controlled_job_asset(
        self,
        job_id: str,
        raw_path: object,
        *,
        label: str,
        require_file: bool = False,
    ) -> Path | None:
        value = str(raw_path or "").strip()
        if not value:
            return None
        path = controlled_job_asset_path(
            self.data_dir / "assets",
            job_id,
            value,
            require_file=require_file,
        )
        if path is None:
            raise ValueError(f"{label}不在当前任务受控目录或文件不存在")
        return path

    def _sync_graph_master_review_decision(
        self,
        job_id: str,
        master_idx: int,
        *,
        decision: str,
        operation_id: str,
    ) -> bool:
        """Keep a committed shot review and its frozen graph node consistent."""
        engine = self.database.execute(
            "SELECT execution_engine FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not engine or str(engine["execution_engine"] or "legacy") != "graph_v2":
            return False
        if decision not in {"accept_candidate", "keep_prior", "regenerate_master"}:
            return False
        node_id = f"master:{int(master_idx)}"
        node = self.database.execute(
            "SELECT state,provider_task_id FROM production_nodes WHERE job_id=? AND node_id=?",
            (job_id, node_id),
        ).fetchone()
        if not node:
            return False
        current = str(node["state"])
        regenerate = decision == "regenerate_master"
        event_id = (
            f"{job_id}:{node_id}:candidate-{decision}:"
            f"{str(operation_id or 'committed')}"
        )
        if regenerate and self.database.execute(
            "SELECT 1 FROM production_node_events WHERE job_id=? AND event_id=?",
            (job_id, event_id),
        ).fetchone():
            return False
        if current == "SUCCEEDED" and not regenerate:
            return False
        if current != "REVIEW_REQUIRED":
            raise ValueError(
                f"reviewed master graph state is inconsistent: {node_id}={current}"
            )
        node_state_machine.transition_node_in_transaction(
            self.database,
            job_id,
            node_id,
            "regenerate" if regenerate else "approve",
            event_id=event_id,
            payload={
                "decision": decision,
                "master_idx": int(master_idx),
                "previous_provider_task_id": node["provider_task_id"],
            },
        )
        if regenerate:
            # The operator rejected this paid candidate. Keep its ledger and
            # cumulative cost, but give the next attempt a new identity and no
            # stale output/task pointer. Shot and graph commit atomically.
            self.database.execute(
                "UPDATE production_nodes SET provider_task_id=NULL,output_json='{}',"
                "idempotency_key=?,lease_id=NULL,heartbeat_at=NULL "
                "WHERE job_id=? AND node_id=?",
                (f"{job_id}:{node_id}:regenerate:{operation_id}", job_id, node_id),
            )
        self.database.execute(
            "UPDATE production_nodes SET error_code=NULL,next_action=NULL,"
            "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND node_id=?",
            (job_id, node_id),
        )
        return True

    def review_master_candidate(
        self, job_id: str, master_idx: int, *, decision: str
    ) -> dict:
        normalized = str(decision or "").strip()
        if normalized not in {
            "accept_candidate", "regenerate_master", "reject_job", "keep_prior",
        }:
            raise ValueError("请选择接受候选、单条重做或终止任务")
        job = self.database.execute(
            "SELECT state,final_path,bgm_path,storyboard,execution_engine "
            "FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        shot = self.database.execute(
            "SELECT state,video_path,video_url,video_qa FROM shots "
            "WHERE job_id=? AND idx=?",
            (job_id, int(master_idx)),
        ).fetchone()
        if not job or not shot:
            raise ValueError("待复核候选母片不存在")
        try:
            qa = json.loads(shot["video_qa"] or "{}")
        except (json.JSONDecodeError, TypeError):
            qa = {}
        existing_operation = qa.get("review_operation")
        if not isinstance(existing_operation, dict):
            existing_operation = qa.get('content_candidate_acceptance')
        if (
            isinstance(existing_operation, dict)
            and existing_operation.get("phase") == "committed"
        ):
            committed_decision = str(existing_operation.get("decision") or "")
            if committed_decision != normalized:
                raise ValueError("该母片已提交其他复核决定")
            if normalized in {'accept_candidate', 'keep_prior'} and content_review.enabled(content_review.object_json(job['storyboard'])):
                with node_state_machine.atomic(self.database):
                    content_review.require_selected(self.database, self.data_dir / 'assets', job_id, master_idx, 'active')
                    locked_shot = self.database.execute('SELECT video_path,video_qa FROM shots WHERE job_id=? AND idx=?', (job_id, int(master_idx))).fetchone()
                    if locked_shot['video_qa'] != shot['video_qa'] or existing_operation.get('selected_sha256', existing_operation.get('candidate_sha256')) != _file_sha256(Path(locked_shot['video_path'])):
                        raise ValueError('content committed source is stale')
            return {
                "job_id": job_id,
                "master_idx": int(master_idx),
                "decision": normalized,
                "state": str(job["state"]),
                "remaining_review_count": int(self.database.execute(
                    "SELECT COUNT(*) FROM shots WHERE job_id=? "
                    "AND state IN ('REVIEW_REQUIRED','REVIEW_DECIDING')",
                    (job_id,),
                ).fetchone()[0]),
                "already_committed": True,
            }
        legacy_review = bool(
            job["state"] == "FAILED"
            and shot["state"] == "FAILED"
            and qa.get("status") == "manual_review_required"
        )
        active_review = bool(
            job["state"] in {"PRODUCTION_REVIEW_REQUIRED", "FAILED", "PROVIDER_ACTION_REQUIRED", "BUDGET_CONFIRMATION_REQUIRED"}
            and shot["state"] in {"REVIEW_REQUIRED", "FAILED"}
            and qa.get("status") == "manual_review_required"
        )
        if not (legacy_review or active_review):
            raise ValueError("当前母片不在人工复核状态")
        prior_value = str(shot["video_path"] or "").strip()
        prior = self._controlled_job_asset(
            job_id, prior_value, label="旧母片", require_file=True
        ) if prior_value and normalized in {"accept_candidate", "keep_prior"} else None
        has_prior = prior is not None
        if normalized in {'accept_candidate', 'keep_prior'}:
            content_review.require_selected(self.database, self.data_dir / 'assets', job_id, master_idx,
                'candidate' if normalized == 'accept_candidate' else 'active')
        if normalized == "keep_prior" and not has_prior:
            raise ValueError("当前母片没有可保留的旧版，请接受候选或单条重做")
        candidate = (
            self.master_candidate_file(job_id, master_idx)
            if normalized in {"accept_candidate", "regenerate_master"}
            else controlled_candidate_path(
                self.data_dir / "assets", job_id,
                qa.get("candidate_path"),
            )
        )
        final_value = str(job["final_path"] or "").strip()
        final = self._controlled_job_asset(
            job_id, final_value, label="历史成片"
        ) if normalized in {"accept_candidate", "keep_prior"} and final_value else None
        bgm_value = str(job["bgm_path"] or "").strip()
        bgm = self._controlled_job_asset(
            job_id, bgm_value, label="历史配乐"
        ) if normalized in {"accept_candidate", "keep_prior"} and bgm_value else None
        if normalized == "regenerate_master":
            self._assert_no_unresolved_provider_tasks(job_id)

        regeneration_storyboard = None
        regeneration_duration = None
        if normalized == "regenerate_master":
            try:
                regeneration_storyboard = json.loads(job["storyboard"] or "{}")
            except (json.JSONDecodeError, TypeError) as exc:
                raise ValueError("视频任务的导演计划无法读取") from exc
            regeneration_master = next((
                item for item in (regeneration_storyboard.get("masters") or [])
                if int(item.get("idx") or 0) == int(master_idx)
            ), None)
            if regeneration_master is not None:
                regeneration_duration = float(
                    regeneration_master.get("duration") or 0
                )
                for edit in regeneration_storyboard.get("shots") or []:
                    audio_idx = int(edit.get(
                        "audio_master_idx",
                        edit.get("video_master_idx", -1),
                    ))
                    if audio_idx != int(master_idx):
                        continue
                    old_cues = edit.get("subtitle_cues") or []
                    if not edit.get("subtitle") and old_cues:
                        edit["subtitle"] = "".join(
                            str(cue.get("text") or "") for cue in old_cues
                        ).strip()
                    edit["subtitle_cues"] = []
                    edit.pop("speech_timeline_version", None)

        claimed_state = str(shot["state"])
        claimed_job_state = str(job["state"])
        original_video_qa = str(shot["video_qa"] or "") or None
        operation_id = secrets.token_hex(12)
        operation_staging = (
            self.data_dir / "assets" / job_id / "review" / "staging"
        ).resolve()
        candidate_path_value = str(candidate) if candidate else str(
            qa.get("candidate_path") or ""
        )
        operation: dict = {
            "operation_id": operation_id,
            "decision": normalized,
            "phase": "claimed",
            "candidate_path": candidate_path_value,
            "candidate_sha256": (
                _file_sha256(candidate) if candidate and candidate.is_file() else None
            ),
        }
        if normalized == "accept_candidate" and candidate:
            active_for_operation = (
                prior if has_prior else
                candidate.parent.parent / f"{int(master_idx):02d}.mp4"
            )
            operation.update({
                "active_path": str(active_for_operation),
                "prior_backup_path": str(
                    operation_staging / f"{int(master_idx):02d}-{operation_id}-prior.mp4"
                ),
                "prior_audio_backup_path": str(
                    operation_staging / f"{int(master_idx):02d}-{operation_id}-prior.wav"
                ),
                "prepared_video_path": str(
                    operation_staging / f"{int(master_idx):02d}-{operation_id}-candidate.mp4"
                ),
                "prepared_audio_path": str(
                    operation_staging / f"{int(master_idx):02d}-{operation_id}-candidate.wav"
                ),
            })
        elif normalized == "regenerate_master" and candidate:
            operation.update({
                "archive_path": str(
                    operation_staging / f"{int(master_idx):02d}-{operation_id}-regenerate.mp4"
                ),
                "archive_audio_path": str(
                    operation_staging / f"{int(master_idx):02d}-{operation_id}-regenerate.wav"
                ),
            })
        claimed_qa = dict(qa)
        claimed_qa["review_operation"] = operation
        try:
            self.database.execute("BEGIN IMMEDIATE")
            if normalized != "reject_job":
                recovery_plan.require_review_decision(self.database, job_id, normalized)
            if normalized in {'accept_candidate', 'keep_prior'}:
                locked_shot = self.database.execute('SELECT video_qa,video_path FROM shots WHERE job_id=? AND idx=?', (job_id, int(master_idx))).fetchone()
                locked_job = self.database.execute('SELECT storyboard FROM jobs WHERE job_id=?', (job_id,)).fetchone()
                if (locked_shot['video_qa'] != shot['video_qa'] or locked_shot['video_path'] != shot['video_path']
                    or locked_job['storyboard'] != job['storyboard']):
                    raise ValueError('content candidate review changed; refresh before accepting')
                content_review.require_selected(self.database, self.data_dir / 'assets', job_id, master_idx,
                    'candidate' if normalized == 'accept_candidate' else 'active')
                if normalized == 'accept_candidate' and _file_sha256(candidate) != operation['candidate_sha256']:
                    raise ValueError('content candidate source changed before claim')
            claimed = self.database.execute(
                "UPDATE shots SET state='REVIEW_DECIDING',video_qa=? "
                "WHERE job_id=? AND idx=? AND state=? "
                "AND EXISTS(SELECT 1 FROM jobs WHERE job_id=? AND state=?)",
                (
                    json.dumps(claimed_qa, ensure_ascii=False),
                    job_id, int(master_idx), claimed_state,
                    job_id, claimed_job_state,
                ),
            )
            if int(claimed.rowcount or 0) != 1:
                raise ValueError("该母片复核决定已被其他请求处理")
            leased = self.database.execute(
                "UPDATE jobs SET updated_at=updated_at "
                "WHERE job_id=? AND state=?",
                (job_id, claimed_job_state),
            )
            if int(leased.rowcount or 0) != 1:
                raise ValueError("该视频任务复核决定已被其他请求处理")
            self.database.commit()
        except Exception:
            self.database.rollback()
            raise
        qa = claimed_qa

        def restore_claim(*, files_restored: bool = True) -> None:
            restored_claim = self.database.execute(
                "UPDATE shots SET state=?,video_qa=? WHERE job_id=? AND idx=? "
                "AND state='REVIEW_DECIDING'",
                (
                    claimed_state, original_video_qa,
                    job_id, int(master_idx),
                ),
            )
            if int(restored_claim.rowcount or 0) != 1 and files_restored:
                rejected_shot = self.database.execute(
                    "SELECT state,video_qa FROM shots WHERE job_id=? AND idx=?",
                    (job_id, int(master_idx)),
                ).fetchone()
                if rejected_shot and rejected_shot["state"] == "FAILED":
                    try:
                        rejected_qa = json.loads(
                            rejected_shot["video_qa"] or "{}"
                        )
                    except (json.JSONDecodeError, TypeError):
                        rejected_qa = {}
                    rejected_operation = rejected_qa.get("review_operation")
                    if (
                        rejected_qa.get("human_decision") == "reject_job"
                        and isinstance(rejected_operation, dict)
                        and rejected_operation.get("operation_id") == operation_id
                    ):
                        rejected_qa.pop("review_operation", None)
                        self.database.execute(
                            "UPDATE shots SET video_qa=? WHERE job_id=? AND idx=? "
                            "AND state='FAILED'",
                            (
                                json.dumps(rejected_qa, ensure_ascii=False),
                                job_id, int(master_idx),
                            ),
                        )
            self.database.commit()

        def persist_review_phase(phase: str, **paths: object) -> None:
            operation.update({"phase": phase})
            operation.update({
                key: str(value) for key, value in paths.items() if value is not None
            })
            qa["review_operation"] = operation
            updated = self.database.execute(
                "UPDATE shots SET video_qa=? WHERE job_id=? AND idx=? "
                "AND state='REVIEW_DECIDING'",
                (
                    json.dumps(qa, ensure_ascii=False),
                    job_id, int(master_idx),
                ),
            )
            if int(updated.rowcount or 0) != 1:
                self.database.rollback()
                raise ValueError("该母片复核操作已被并发终止")
            self.database.commit()

        reviewed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if normalized == "reject_job":
            try:
                self.database.execute("BEGIN IMMEDIATE")
                unresolved_provider_tasks = provider_tasks.unresolved_task_ids(
                    self.database, job_id
                )
                rejection_state = (
                    "REJECTED_PENDING_RECONCILIATION"
                    if unresolved_provider_tasks else "REJECTED"
                )
                state_machine.transition(
                    self.database,
                    job_id,
                    "rejection_requested",
                    event_id=f"{job_id}:review-reject:{operation_id}",
                    target_state=rejection_state,
                    updates={
                        "error": (
                            "已人工终止，仍有远端成本待核对"
                            if unresolved_provider_tasks
                            else "人工终止视频任务；不会自动重试"
                        ),
                        "production_worker_pid": None,
                    },
                )
                review_rows = self.database.execute(
                    "SELECT idx,state,video_qa FROM shots WHERE job_id=? ORDER BY idx",
                    (job_id,),
                ).fetchall()
                for review_row in review_rows:
                    try:
                        review_qa = json.loads(review_row["video_qa"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        review_qa = {}
                    if not (
                        review_row["state"] in {"REVIEW_REQUIRED", "REVIEW_DECIDING"}
                        or (
                            review_row["state"] == "FAILED"
                            and review_qa.get("status") == "manual_review_required"
                        )
                    ):
                        continue
                    review_qa.update({
                        "status": "job_rejected",
                        "human_decision": "reject_job",
                        "reviewed_at": reviewed_at,
                    })
                    in_flight_operation = review_qa.get("review_operation")
                    if not (
                        review_row["state"] == "REVIEW_DECIDING"
                        and isinstance(in_flight_operation, dict)
                        and in_flight_operation.get("decision") in {
                            "accept_candidate", "regenerate_master",
                        }
                    ):
                        review_qa.pop("review_operation", None)
                    self.database.execute(
                        "UPDATE shots SET state='FAILED',video_qa=?,error=? "
                        "WHERE job_id=? AND idx=?",
                        (
                            json.dumps(review_qa, ensure_ascii=False),
                            "人工终止任务；候选产物已保留",
                            job_id, int(review_row["idx"]),
                        ),
                    )
                self.database.execute(
                    "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
                    (
                        job_id, "master_candidate_review", "info",
                        f"母片 #{int(master_idx)} 复核时人工终止任务；付费产物保留",
                    ),
                )
                self.database.commit()
            except Exception:
                self.database.rollback()
                restore_claim()
                raise
            return {
                "job_id": job_id,
                "master_idx": int(master_idx),
                "decision": normalized,
                "state": rejection_state,
                "remaining_review_count": 0,
            }

        decision_label = (
            "已接受候选" if normalized == "accept_candidate"
            else "已退回单条重做" if normalized == "regenerate_master"
            else "已保留旧版"
        )

        def commit_decision(shot_sql: str, shot_params: tuple) -> dict:
            """Linearize the final shot decision and its job state together."""
            try:
                self.database.execute("BEGIN IMMEDIATE")
                live = self.database.execute(
                    "SELECT state FROM jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                if not live or live["state"] != claimed_job_state:
                    raise ValueError("该视频任务已被人工终止或并发处理")
                changed = self.database.execute(shot_sql, shot_params)
                if int(changed.rowcount or 0) != 1:
                    raise ValueError("该视频任务已被人工终止或并发处理")
                if normalized in {'accept_candidate', 'keep_prior'}:
                    content_review.require_selected(self.database, self.data_dir / 'assets', job_id, master_idx, 'active')
                self._sync_graph_master_review_decision(
                    job_id,
                    int(master_idx),
                    decision=normalized,
                    operation_id=operation_id,
                )
                if regeneration_storyboard is not None:
                    self.database.execute(
                        "UPDATE jobs SET storyboard=? WHERE job_id=?",
                        (
                            json.dumps(
                                regeneration_storyboard, ensure_ascii=False
                            ),
                            job_id,
                        ),
                    )
                    if regeneration_duration:
                        self.database.execute(
                            "UPDATE shots SET duration_sec=? "
                            "WHERE job_id=? AND idx=?",
                            (
                                regeneration_duration,
                                job_id,
                                int(master_idx),
                            ),
                        )

                review_rows = self.database.execute(
                    "SELECT idx,state,video_qa FROM shots WHERE job_id=? ORDER BY idx",
                    (job_id,),
                ).fetchall()
                remaining_review_count = 0
                pending_accepts: list[tuple[sqlite3.Row, dict]] = []
                for review_row in review_rows:
                    try:
                        review_qa = json.loads(review_row["video_qa"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        review_qa = {}
                    if review_row["state"] in {
                        "REVIEW_REQUIRED", "REVIEW_DECIDING",
                    } or (
                        review_row["state"] == "FAILED"
                        and review_qa.get("status") == "manual_review_required"
                    ):
                        remaining_review_count += 1
                    if review_qa.get("status") == "candidate_accepted_pending":
                        pending_accepts.append((review_row, review_qa))

                remaining_generation_count = int(self.database.execute(
                    "SELECT COUNT(*) FROM shots WHERE job_id=? AND state!='OK'",
                    (job_id,),
                ).fetchone()[0])
                needs_local_compose = False
                music_provider = None
                needs_bgm_generation = False
                error_message = None
                unresolved = provider_tasks.unresolved_task_ids(
                    self.database, job_id
                )
                if unresolved:
                    next_state = "PROVIDER_ACTION_REQUIRED"
                    error_message = (
                        "已保存本地复核决定，但仍有远端任务待核对；"
                        "请先核对 task_id：" + ", ".join(unresolved[:5])
                    )
                elif remaining_review_count:
                    next_state = "PRODUCTION_REVIEW_REQUIRED"
                    error_message = (
                        f"仍有 {remaining_review_count} 条母片待人工复核"
                    )
                elif remaining_generation_count:
                    try:
                        self._assert_no_unresolved_provider_tasks(job_id)
                    except ValueError as exc:
                        next_state = "PROVIDER_ACTION_REQUIRED"
                        error_message = str(exc)
                    else:
                        next_state = "PRODUCTION_QUEUED"
                        if str(job["execution_engine"] or "legacy") == "graph_v2":
                            projection = production_projection.reconcile(self.database, job_id)
                            next_state = projection.projected_job_state
                            if next_state in {"FAILED", "RECOVERY_REQUIRED"}:
                                plan = recovery_plan.resolve(self.database, job_id)
                                error_message = plan["reason"] if plan else "Production graph requires diagnosis"
                        else:
                            self.database.execute(
                                "UPDATE shots SET state='PENDING',error=NULL,video_qa=NULL "
                                "WHERE job_id=? AND state='FAILED'",
                                (job_id,),
                            )
                elif str(job["execution_engine"] or "legacy") == "graph_v2":
                    next_state = "PRODUCTION_QUEUED"
                else:
                    needs_local_compose = bool(pending_accepts) or not (
                        final and final.is_file()
                    )
                    if needs_local_compose:
                        music_provider = self._music_provider()
                        needs_bgm_generation = bool(
                            not (bgm and bgm.is_file())
                            and (
                                not pending_accepts
                                or any(
                                    not pending_qa.get("prior_backup_path")
                                    for _, pending_qa in pending_accepts
                                )
                            )
                        )
                        if (
                            needs_bgm_generation
                            and music.may_submit_paid_work(music_provider)
                        ):
                            try:
                                self._assert_no_unresolved_provider_tasks(job_id)
                            except ValueError as exc:
                                next_state = "PROVIDER_ACTION_REQUIRED"
                                error_message = str(exc)
                            else:
                                next_state = "SHOTS_OK"
                        else:
                            next_state = "SHOTS_OK"
                    else:
                        next_state = "COMPOSED"

                transition_updates: dict[str, object] = {
                    "error": error_message,
                }
                if next_state in {"PRODUCTION_QUEUED", "COMPOSED"}:
                    transition_updates["retries"] = 0
                if next_state in {
                    "PRODUCTION_QUEUED", "PROVIDER_ACTION_REQUIRED",
                }:
                    transition_updates["production_worker_pid"] = None
                state_machine.transition(
                    self.database,
                    job_id,
                    "review_decided",
                    event_id=f"{job_id}:review-decision:{operation_id}",
                    target_state=next_state,
                    updates=transition_updates,
                )
                event_detail = error_message or decision_label
                self.database.execute(
                    "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
                    (
                        job_id, "master_candidate_review", "info",
                        f"母片 #{int(master_idx)} {decision_label}，{event_detail}",
                    ),
                )
                self.database.commit()
                return {
                    "state": next_state,
                    "remaining_review_count": remaining_review_count,
                    "remaining_generation_count": remaining_generation_count,
                    "pending_accepts": pending_accepts,
                    "needs_local_compose": needs_local_compose,
                    "music_provider": music_provider,
                    "needs_bgm_generation": needs_bgm_generation,
                }
            except Exception:
                self.database.rollback()
                raise

        decision_commit = None
        if normalized == "accept_candidate":
            candidate_url = str(qa.get("candidate_url") or "").strip() or None
            active = (
                prior if has_prior else
                candidate.parent.parent / f"{int(master_idx):02d}.mp4"
            )
            active = self._controlled_job_asset(job_id, active, label="当前母片")
            active.parent.mkdir(parents=True, exist_ok=True)
            staging_dir = (
                self.data_dir / "assets" / job_id / "review" / "staging"
            ).resolve()
            staging_dir.mkdir(parents=True, exist_ok=True)
            prior_backup = None
            prior_audio_backup = None
            active_audio = active.with_suffix(".wav")
            candidate_audio = candidate.with_suffix(".wav")
            active_changed = False
            prepared_video = None
            prepared_audio = None
            try:
                if has_prior:
                    prior_backup = self._controlled_job_asset(
                        job_id,
                        operation["prior_backup_path"],
                        label="旧母片备份",
                    )
                    shutil.copy2(active, prior_backup)
                    if not _files_have_same_content(active, prior_backup):
                        raise OSError("旧母片备份校验失败")
                    if active_audio.is_file():
                        prior_audio_backup = prior_backup.with_suffix(".wav")
                        shutil.copy2(active_audio, prior_audio_backup)
                        if not _files_have_same_content(
                            active_audio, prior_audio_backup
                        ):
                            raise OSError("旧母片音频备份校验失败")
                prepared_video = Path(operation["prepared_video_path"])
                shutil.copy2(candidate, prepared_video)
                if _file_sha256(prepared_video) != operation['candidate_sha256']:
                    raise ValueError('content candidate changed during file preparation')
                prepared_audio = prepared_video.with_suffix(".wav")
                if candidate_audio.is_file():
                    shutil.copy2(candidate_audio, prepared_audio)
                persist_review_phase(
                    "prepared",
                    prior_backup_path=prior_backup,
                    prior_audio_backup_path=prior_audio_backup,
                    prepared_video_path=prepared_video,
                    prepared_audio_path=prepared_audio,
                )
                prepared_video.replace(active)
                active_changed = True
                persist_review_phase("video_published")
                if prepared_audio.is_file():
                    prepared_audio.replace(active_audio)
                elif active_audio.is_file():
                    active_audio.unlink()
                persist_review_phase("audio_published")
                qa.update({
                    "status": "candidate_accepted_pending",
                    "human_decision": "accept_candidate",
                    "reviewed_at": reviewed_at,
                    "prior_backup_path": (
                        str(prior_backup) if prior_backup else None
                    ),
                    "prior_video_url": shot["video_url"],
                    "candidate_path": qa.get("candidate_path"),
                })
                operation["phase"] = "committed"
                qa["review_operation"] = operation
                if content_review.enabled(content_review.object_json(job['storyboard'])):
                    qa['content_candidate_acceptance'] = {'phase': 'committed', 'decision': normalized,
                        'selected_sha256': operation['candidate_sha256']}
                decision_commit = commit_decision(
                    "UPDATE shots SET state='OK',video_path=?,video_url=?,"
                    "video_qa=?,error=NULL WHERE job_id=? AND idx=? "
                    "AND state='REVIEW_DECIDING'",
                    (
                        str(active), candidate_url,
                        json.dumps(qa, ensure_ascii=False),
                        job_id, int(master_idx),
                    ),
                )
            except Exception:
                restore_error = None
                restored = not active_changed
                if active_changed:
                    try:
                        if prior_backup and prior_backup.is_file():
                            shutil.copy2(prior_backup, active)
                        else:
                            active.unlink(missing_ok=True)
                        if prior_audio_backup and prior_audio_backup.is_file():
                            shutil.copy2(prior_audio_backup, active_audio)
                        else:
                            active_audio.unlink(missing_ok=True)
                        restored = True
                    except Exception as exc:
                        restore_error = exc
                if restored:
                    for artifact in (
                        prepared_video, prepared_audio,
                        prior_backup, prior_audio_backup,
                    ):
                        if artifact:
                            try:
                                artifact.unlink(missing_ok=True)
                            except OSError:
                                pass
                restore_claim(files_restored=restored)
                if restore_error is not None:
                    raise RuntimeError(
                        "候选发布失败且旧母片恢复失败；恢复备份已保留"
                    ) from restore_error
                raise
        elif normalized == "regenerate_master":
            staging_dir = (
                self.data_dir / "assets" / job_id / "review" / "staging"
            ).resolve()
            staging_dir.mkdir(parents=True, exist_ok=True)
            staged_candidate = self._controlled_job_asset(
                job_id,
                operation["archive_path"],
                label="候选归档",
            )
            staged_audio = staged_candidate.with_suffix(".wav")
            candidate_audio = candidate.with_suffix(".wav")
            try:
                candidate.replace(staged_candidate)
                persist_review_phase(
                    "archived",
                    archive_path=staged_candidate,
                    archive_audio_path=staged_audio,
                )
                if candidate_audio.is_file():
                    candidate_audio.replace(staged_audio)
                    persist_review_phase("audio_archived")
                operation["phase"] = "committed"
                qa.update({
                    "status": "regenerate_committed",
                    "review_operation": operation,
                })
                decision_commit = commit_decision(
                    "UPDATE shots SET state='PENDING',video_qa=?,error=NULL "
                    "WHERE job_id=? AND idx=? AND state='REVIEW_DECIDING'",
                    (
                        json.dumps(qa, ensure_ascii=False),
                        job_id, int(master_idx),
                    ),
                )
            except Exception:
                if staged_candidate.is_file():
                    staged_candidate.replace(candidate)
                if staged_audio.is_file():
                    staged_audio.replace(candidate_audio)
                restore_claim()
                raise
            cleanup_ok = True
            for artifact in (staged_candidate, staged_audio):
                try:
                    artifact.unlink(missing_ok=True)
                except OSError:
                    cleanup_ok = False
            if cleanup_ok:
                try:
                    self.database.execute(
                        "UPDATE shots SET video_qa=NULL WHERE job_id=? AND idx=? "
                        "AND state='PENDING'",
                        (job_id, int(master_idx)),
                    )
                    self.database.commit()
                except sqlite3.Error:
                    self.database.rollback()
        else:  # keep_prior is a migration-only compatibility action.
            try:
                qa.update({
                    "status": "candidate_rejected",
                    "candidate_path": None,
                    "candidate_url": None,
                    "human_decision": "keep_prior",
                    "reviewed_at": reviewed_at,
                })
                operation.update({
                    "phase": "committed",
                    "candidate_path": str(candidate) if candidate else None,
                })
                qa["review_operation"] = operation
                if content_review.enabled(content_review.object_json(job['storyboard'])):
                    qa['content_candidate_acceptance'] = {'phase': 'committed', 'decision': normalized,
                        'selected_sha256': _file_sha256(prior)}
                decision_commit = commit_decision(
                    "UPDATE shots SET state='OK',video_qa=?,error=NULL "
                    "WHERE job_id=? AND idx=? AND state='REVIEW_DECIDING'",
                    (
                        json.dumps(qa, ensure_ascii=False),
                        job_id, int(master_idx),
                    ),
                )
            except Exception:
                restore_claim()
                raise
            if candidate:
                cleanup_ok = True
                for artifact in (candidate, candidate.with_suffix(".wav")):
                    try:
                        artifact.unlink(missing_ok=True)
                    except OSError:
                        cleanup_ok = False
                if cleanup_ok:
                    qa.pop("review_operation", None)
                    try:
                        self.database.execute(
                            "UPDATE shots SET video_qa=? WHERE job_id=? AND idx=? "
                            "AND state='OK'",
                            (
                                json.dumps(qa, ensure_ascii=False),
                                job_id, int(master_idx),
                            ),
                        )
                        self.database.commit()
                    except sqlite3.Error:
                        self.database.rollback()

        next_state = decision_commit["state"]
        if regeneration_storyboard is not None:
            storyboard_path = (
                self.data_dir / "assets" / job_id / "storyboard.json"
            )
            try:
                storyboard_path.parent.mkdir(parents=True, exist_ok=True)
                storyboard_path.write_text(
                    json.dumps(
                        regeneration_storyboard,
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except OSError:
                pass
        result = {
            "job_id": job_id,
            "master_idx": int(master_idx),
            "decision": normalized,
            "state": next_state,
            "remaining_review_count": decision_commit["remaining_review_count"],
        }
        if decision_commit["remaining_generation_count"]:
            result["remaining_generation_count"] = decision_commit[
                "remaining_generation_count"
            ]
        if next_state == "PRODUCTION_QUEUED":
            dispatch = self.dispatch_production_queue()
            result["dispatched_jobs"] = dispatch["dispatched_jobs"]
            return result
        if next_state in {
            "PRODUCTION_REVIEW_REQUIRED", "PROVIDER_ACTION_REQUIRED", "COMPOSED",
            "FAILED", "BUDGET_CONFIRMATION_REQUIRED", "RECOVERY_REQUIRED",
        }:
            return result

        pending_accepts = decision_commit["pending_accepts"]
        needs_bgm_generation = decision_commit["needs_bgm_generation"]
        music_provider = decision_commit["music_provider"]
        final_backup = None
        if final:
            staging_dir = (
                self.data_dir / "assets" / job_id / "review" / "staging"
            ).resolve()
            staging_dir.mkdir(parents=True, exist_ok=True)
            final_backup = self._controlled_job_asset(
                job_id,
                staging_dir / f"final-{time.time_ns()}-prior{final.suffix}",
                label="历史成片备份",
            )
            final_backup.unlink(missing_ok=True)
            if final.is_file():
                final.replace(final_backup)
        try:
            if needs_bgm_generation:
                asyncio.run(s5_bgm.run(job_id, provider=music_provider))
            state_machine.transition(
                self.database,
                job_id,
                "bgm_completed",
                event_id=f"{job_id}:review-bgm:{operation_id}",
                updates={"error": None, "failure_code": None},
            )
            asyncio.run(s6_compose.run(job_id))
            finalize_accepted_candidates(
                self.database, self.data_dir / "assets", job_id
            )
        except Exception as exc:
            if final:
                final.unlink(missing_ok=True)
            if final and final_backup and final_backup.exists():
                final_backup.replace(final)
            outcome = error_registry.describe_failure(
                exc, stage="local_recompose"
            )
            state_machine.transition(
                self.database,
                job_id,
                "failed",
                event_id=f"{job_id}:review-compose-failed:{operation_id}",
                updates={
                    "error": outcome.user_message,
                    "failure_code": outcome.code,
                },
            )
            raise RuntimeError(f"local recompose failed: {exc}") from exc
        if final_backup:
            try:
                final_backup.unlink(missing_ok=True)
            except OSError:
                # The new final and accepted-shot audit are already durable.
                # A stale backup is safe for later GC and must not turn a
                # successful production decision into an HTTP 500.
                pass
        state_machine.transition(
            self.database,
            job_id,
            "composed",
            event_id=f"{job_id}:review-composed:{operation_id}",
            updates={"error": None, "failure_code": None, "retries": 0},
        )
        self.database.execute(
            "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
            (
                job_id, "master_candidate_review", "info",
                f"母片 #{int(master_idx)} 已完成复核并完成本地重合成",
            ),
        )
        self.database.commit()
        result["state"] = "COMPOSED"
        return result

    def publishing(self) -> dict:
        ready = _rows(self.database.execute(
            "SELECT job_id,title,state,final_path,publish_url,updated_at FROM jobs "
            "WHERE state='READY_TO_PUBLISH' ORDER BY updated_at DESC LIMIT 60"
        ))
        publications = _rows(self.database.execute(
            "SELECT p.publication_id,p.job_id,p.platform,p.item_id,p.item_url,"
            "p.title,p.published_at,p.metadata_json,j.title AS job_title "
            "FROM platform_publications p JOIN jobs j ON j.job_id=p.job_id "
            "ORDER BY p.created_at DESC LIMIT 60"
        ))
        unverified = _rows(self.database.execute(
            "SELECT j.job_id,j.title,j.state,j.updated_at "
            "FROM jobs j WHERE j.state='PUBLISHED' "
            "AND NOT EXISTS (SELECT 1 FROM platform_publications p "
            "WHERE p.job_id=j.job_id) "
            "ORDER BY j.updated_at DESC LIMIT 60"
        ))
        supported = {"douyin", "weixin_channels"}
        for item in ready:
            channel_rows = self.database.execute(
                    "SELECT platform,metadata_json FROM platform_publications WHERE job_id=?",
                    (item["job_id"],),
                ).fetchall()
            pending = {
                row["platform"] for row in channel_rows
                if _publication_is_pending(row["metadata_json"])
            }
            published = {
                row["platform"] for row in channel_rows
                if not _publication_is_pending(row["metadata_json"])
            }
            item["published_platforms"] = sorted(published & supported)
            item["pending_platforms"] = sorted(pending & supported)
            item["remaining_platforms"] = sorted(supported - published - pending)
        for publication in publications:
            snapshot = self.database.execute(
                "SELECT snapshot_id,captured_at,views,completed_views,completion_rate,"
                "average_watch_sec,likes,comments,shares,saves,follows,leads "
                "FROM performance_snapshots WHERE job_id=? AND platform=? "
                "ORDER BY captured_at DESC LIMIT 1",
                (publication["job_id"], publication["platform"]),
            ).fetchone()
            publication["latest_snapshot"] = dict(snapshot) if snapshot else None
            metadata = _publication_metadata(publication.pop("metadata_json", None))
            publication["metadata"] = metadata
            publication["verification_status"] = (
                "pending_review" if _publication_is_pending(metadata) else "verified"
            )
        for item in unverified:
            item["verification_status"] = "legacy_unverified"
        return {
            "ready": ready,
            "publications": publications,
            "unverified": unverified,
            "performance_due": performance_feedback.performance_due_tasks(
                self.database
            ),
        }

    def recent_events(self, limit: int = 40) -> dict:
        """Return business milestones, never provider/debug implementation logs."""
        safe_limit = min(100, max(1, int(limit)))
        activity: list[dict] = []
        for row in self.database.execute(
            "SELECT job_id,title,state,updated_at FROM jobs ORDER BY updated_at DESC LIMIT ?",
            (safe_limit,),
        ).fetchall():
            state = str(row["state"])
            messages = {
                "STORYBOARDED": "剧本已通过审核，等待开始视频生成",
                "PENDING": "视频正在生成母片",
                "SHOTS_OK": "视频母片已生成，正在进行导演剪辑",
                "BGM_OK": "视频声音与配乐已就绪",
                "COMPOSED": "成片已完成，可预览并提交发布",
                "READY_TO_PUBLISH": "成片已提交发布流水线",
                "PUBLISHED": "视频已完成发布登记",
                "FAILED": "视频生成失败，需要处理",
                "BLOCKED": "视频任务已暂停，需要处理",
            }
            activity.append({
                "id": f"job-{row['job_id']}", "job_id": row["job_id"],
                "category": "视频", "level": "error" if state in {"FAILED", "BLOCKED"} else "info",
                "title": row["title"], "message": messages.get(state, "视频任务状态已更新"),
                "ts": row["updated_at"],
            })
        for row in self.database.execute(
            "SELECT draft_id,status,script_json,updated_at FROM script_drafts "
            "ORDER BY updated_at DESC LIMIT ?", (safe_limit,)
        ).fetchall():
            try:
                title = json.loads(row["script_json"] or "{}").get("title") or "未命名剧本"
            except json.JSONDecodeError:
                title = "未命名剧本"
            message = "剧本等待审核" if row["status"] == "candidate" else (
                "剧本已通过审核并提交视频流水线" if row["status"] == "approved" else "剧本未被选用"
            )
            activity.append({
                "id": f"draft-{row['draft_id']}", "draft_id": row["draft_id"],
                "category": "剧本", "level": "info", "title": title,
                "message": message, "ts": row["updated_at"],
            })
        for row in self.database.execute(
            "SELECT publication_id,job_id,platform,title,created_at FROM platform_publications "
            "ORDER BY created_at DESC LIMIT ?", (safe_limit,)
        ).fetchall():
            platform = {"douyin": "抖音", "weixin_channels": "微信视频号"}.get(
                row["platform"], row["platform"]
            )
            activity.append({
                "id": f"pub-{row['publication_id']}", "job_id": row["job_id"],
                "category": "发布", "level": "info", "title": row["title"] or platform,
                "message": f"已登记到{platform}", "ts": row["created_at"],
            })
        activity.sort(key=lambda item: str(item.get("ts") or ""), reverse=True)
        return {"events": activity[:safe_limit]}

    def _readiness(self) -> dict:
        values = _read_env(self.env_path)
        audio_mode = normalize_audio_mode(values.get("DIALOGUE_AUDIO_MODE"))
        groups = {
            "script": ("ARK_API_KEY",),
            "video": (
                "ARK_API_KEY", "VOLC_AK", "VOLC_SK", "TOS_BUCKET",
                *(("VOLC_TTS_APPID", "VOLC_TTS_API_KEY")
                  if audio_mode == AUDIO_MODE_FIXED_TTS else ()),
            ),
        }
        readiness = {
            name: {
                "ready": all(_configured(values.get(key)) for key in keys),
                "missing": [key for key in keys if not _configured(values.get(key))],
                "mode": "native",
            }
            for name, keys in groups.items()
        }
        asr_ready = _configured(values.get("VOLC_ASR_API_KEY")) or all(
            _configured(values.get(key))
            for key in ("VOLC_ASR_APPID", "VOLC_ASR_TOKEN")
        )
        douyin_account = self.douyin_account_status()
        channels = {
            "douyin": (
                all(_configured(values.get(key)) for key in (
                    "DOUYIN_CLIENT_KEY", "DOUYIN_CLIENT_SECRET", "DOUYIN_REDIRECT_URI",
                ))
                and douyin_account["connected"]
            ),
            # Creator login and upload stay on the official WeChat Channels
            # Assistant page, so this assisted path needs no local credential.
            "weixin_channels": True,
        }
        readiness["publish"] = {
            "mode": "direct_or_assisted",
            "ready": any(channels.values()),
            "missing": [key for key, configured in channels.items() if not configured],
        }
        return readiness

    def config_status(self) -> dict:
        defaults = _read_env(self.root / "templates" / "env.example")
        current = _read_env(self.env_path)
        keys = sorted(EDITABLE_CONFIG | set(defaults) | set(current))
        fields = []
        for key in keys:
            if key not in EDITABLE_CONFIG:
                continue
            value = current.get(key, defaults.get(key, ""))
            field = {
                "key": key,
                "secret": key in SECRET_KEYS,
                "configured": _configured(current.get(key)),
                "source": "local" if key in current else "default",
            }
            if key not in SECRET_KEYS:
                # Do not surface template placeholders such as <required> as if
                # they were user-entered values.
                field["value"] = value if _configured(value) else ""
            if key in {"CHARACTER_ANCHOR_GENERATOR", "SCENE_ANCHOR_GENERATOR"}:
                field["value"] = anchor_generation.normalize_generator(value)
                field["options"] = list(anchor_generation.GENERATOR_OPTIONS)
            if key == "DIALOGUE_AUDIO_MODE":
                field["value"] = normalize_audio_mode(value)
                field["options"] = [
                    {"value": "seedance_native", "label": "Seedance 原生声线（默认）"},
                    {"value": "fixed_tts", "label": "固定火山声线"},
                ]
            if key == "PRODUCTION_EXECUTION_ENGINE":
                field["value"] = production_config.resolve_default_execution_engine(
                    {"PRODUCTION_EXECUTION_ENGINE": value}
                )
                field["options"] = [
                    {"value": "graph_v2", "label": "Production Graph V2（正式）"},
                    {"value": "legacy", "label": "Legacy（回滚）"},
                ]
            fields.append(field)
        fields_by_key = {field["key"]: field for field in fields}
        required_groups = []
        audio_mode = normalize_audio_mode(
            current.get("DIALOGUE_AUDIO_MODE")
            or defaults.get("DIALOGUE_AUDIO_MODE")
        )
        for group in REQUIRED_CONFIG_GROUPS:
            group_fields = [fields_by_key[key] for key in group["keys"]]
            alternatives = group.get("alternative_keys")
            ready = (
                any(
                    all(fields_by_key[key]["configured"] for key in option)
                    for option in alternatives
                )
                if alternatives
                else all(field["configured"] for field in group_fields)
            )
            applies = not (
                group["id"] == "tts"
                and audio_mode != AUDIO_MODE_FIXED_TTS
            )
            if not applies:
                ready = True
            missing = (
                [] if ready
                else [field["key"] for field in group_fields if not field["configured"]]
            )
            required_groups.append({
                **group,
                "blocking": bool(group["blocking"] and applies),
                "applies": applies,
                "keys": list(group["keys"]),
                "guide": {
                    **group["guide"],
                    "steps": list(group["guide"]["steps"]),
                },
                "fields": group_fields,
                "ready": ready,
                "missing": missing,
            })
        completed = sum(1 for group in required_groups if group["ready"])
        return {
            "fields": fields,
            "providers": self.provider_statuses()["providers"],
            "required_groups": required_groups,
            "optional_fields": [
                field for field in fields if field["key"] not in REQUIRED_CONFIG_KEYS
            ],
            "required_progress": {
                "completed": completed,
                "total": len(required_groups),
            },
            "publish_platforms": [
                {
                    "id": "douyin", "name": "抖音", "mode": "oauth",
                    "configured": all(_configured(current.get(key)) for key in (
                        "DOUYIN_CLIENT_KEY", "DOUYIN_CLIENT_SECRET", "DOUYIN_REDIRECT_URI"
                    )),
                    "connected": self.douyin_account_status()["connected"],
                    "account": self.douyin_account_status(),
                    "impact": "通过抖音 OAuth 扫码绑定账号，成片确认后可直接上传并提交平台审核",
                },
                {
                    "id": "weixin_channels", "name": "微信视频号",
                    "mode": "official_assistant",
                    "configured": True,
                    "connected": None,
                    "entry_url": _weixin_channels_assistant_url(
                        current.get("WEIXIN_CHANNELS_ASSISTANT_URL")
                    ),
                    "account_binding": "official_qr_session",
                    "impact": "打开微信官方视频号助手扫码登录，下载成片并发布后回到管理台登记作品结果",
                },
            ],
            "readiness": self._readiness(),
        }

    def provider_statuses(self) -> dict:
        return {
            "providers": [
                provider_health.get_status(self.database, "seedance")
            ]
        }

    def clear_seedance_provider_block(self) -> dict:
        status = provider_health.clear(self.database, "seedance")
        return {
            "provider": status,
            "message": (
                "已解除本地阻断；系统不自动检测账号余额，也不自动恢复或续跑历史任务。"
            ),
        }

    def update_config(self, changes: dict) -> dict:
        current = _read_env(self.env_path)
        updated: list[str] = []
        for key, raw_value in changes.items():
            if key not in EDITABLE_CONFIG:
                continue
            value = str(raw_value or "").strip().replace("\r", "").replace("\n", "")
            if key in {"CHARACTER_ANCHOR_GENERATOR", "SCENE_ANCHOR_GENERATOR"}:
                value = anchor_generation.normalize_generator(value, strict=True)
            if key == "PRODUCTION_EXECUTION_ENGINE":
                value = production_config.resolve_default_execution_engine(
                    {"PRODUCTION_EXECUTION_ENGINE": value}
                )
            if key == "SEEDANCE_PRODUCTION_REFERENCE_VIDEO_BUDGET":
                try:
                    reference_budget = int(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("参考视频注意力预算必须是 1 到 10") from exc
                provider_limit = int(
                    seedance.capability_profile(seedance.DEFAULT_MODEL)[
                        "reference_limits"
                    ]["reference_video"]
                )
                if not 1 <= reference_budget <= provider_limit:
                    raise ValueError(
                        f"参考视频注意力预算必须是 1 到 {provider_limit}"
                    )
                value = str(reference_budget)
            if key in SECRET_KEYS and not value:
                continue
            current[key] = value
            updated.append(key)
        self.env_path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(f"{key}={value}" for key, value in current.items())
        self.env_path.write_text(content + ("\n" if content else ""), encoding="utf-8")
        return {"updated": sorted(updated), "readiness": self._readiness()}

    @staticmethod
    def _masked_account_id(account_id: str) -> str:
        value = str(account_id or "").strip()
        if len(value) <= 8:
            return value
        return f"{value[:4]}…{value[-4:]}"

    def douyin_account_status(self) -> dict:
        row = self.database.execute(
            "SELECT account_id,scope,access_expires_at,refresh_expires_at,status,updated_at "
            "FROM platform_accounts WHERE platform='douyin'"
        ).fetchone()
        if not row:
            return {"connected": False, "status": "unbound"}
        now = int(time.time())
        connected = row["status"] == "active" and int(row["refresh_expires_at"]) > now
        status = "connected" if connected else "expired"
        return {
            "connected": connected,
            "status": status,
            "open_id_masked": self._masked_account_id(row["account_id"]),
            "scope": row["scope"],
            "access_expires_at": int(row["access_expires_at"]),
            "refresh_expires_at": int(row["refresh_expires_at"]),
            "updated_at": row["updated_at"],
        }

    def start_douyin_oauth(self) -> dict:
        values = _read_env(self.env_path)
        client_key = str(values.get("DOUYIN_CLIENT_KEY") or "").strip()
        client_secret = str(values.get("DOUYIN_CLIENT_SECRET") or "").strip()
        redirect_uri = str(values.get("DOUYIN_REDIRECT_URI") or "").strip()
        missing = [name for name, value in (
            ("DOUYIN_CLIENT_KEY", client_key),
            ("DOUYIN_CLIENT_SECRET", client_secret),
            ("DOUYIN_REDIRECT_URI", redirect_uri),
        ) if not _configured(value)]
        if missing:
            raise ValueError("请先配置抖音开放平台应用：" + "、".join(missing))
        state = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + 600
        authorization_url = douyin.DouyinClient.authorization_url(
            client_key=client_key,
            redirect_uri=redirect_uri,
            state=state,
        )
        self.database.execute(
            "DELETE FROM platform_oauth_states WHERE platform='douyin' AND expires_at<?",
            (int(time.time()),),
        )
        self.database.execute(
            "INSERT INTO platform_oauth_states(state,platform,redirect_uri,expires_at) "
            "VALUES(?,?,?,?)",
            (state, "douyin", redirect_uri, expires_at),
        )
        self.database.commit()
        return {
            "platform": "douyin",
            "state": state,
            "authorization_url": authorization_url,
            "expires_at": expires_at,
        }

    def _save_douyin_account(self, token: dict) -> None:
        now = int(time.time())
        account_id = str(token.get("open_id") or "").strip()
        access_token = str(token.get("access_token") or "").strip()
        refresh_token = str(token.get("refresh_token") or "").strip()
        if not account_id or not access_token or not refresh_token:
            raise douyin.DouyinError("抖音授权返回缺少账号或令牌信息")
        access_expires_at = now + int(token.get("expires_in") or 0)
        refresh_expires_at = now + int(token.get("refresh_expires_in") or 0)
        self.database.execute(
            "INSERT INTO platform_accounts(platform,account_id,access_token,refresh_token,"
            "scope,access_expires_at,refresh_expires_at,status) VALUES(?,?,?,?,?,?,?,'active') "
            "ON CONFLICT(platform) DO UPDATE SET account_id=excluded.account_id,"
            "access_token=excluded.access_token,refresh_token=excluded.refresh_token,"
            "scope=excluded.scope,access_expires_at=excluded.access_expires_at,"
            "refresh_expires_at=excluded.refresh_expires_at,status='active',"
            "updated_at=CURRENT_TIMESTAMP",
            (
                "douyin", account_id, access_token, refresh_token,
                str(token.get("scope") or ""), access_expires_at, refresh_expires_at,
            ),
        )
        self.database.commit()

    def complete_douyin_oauth(self, *, code: str, state: str) -> dict:
        code = str(code or "").strip()
        state = str(state or "").strip()
        if not code or not state:
            raise ValueError("抖音授权回调缺少 code 或 state")
        row = self.database.execute(
            "SELECT redirect_uri,expires_at,used_at FROM platform_oauth_states "
            "WHERE state=? AND platform='douyin'", (state,),
        ).fetchone()
        if not row:
            raise ValueError("抖音授权请求不存在，请重新发起绑定")
        if row["used_at"] is not None:
            raise ValueError("抖音授权请求已使用，请重新发起绑定")
        if int(row["expires_at"]) <= int(time.time()):
            raise ValueError("抖音授权请求已过期，请重新发起绑定")
        claimed = self.database.execute(
            "UPDATE platform_oauth_states SET used_at=CURRENT_TIMESTAMP "
            "WHERE state=? AND used_at IS NULL", (state,),
        )
        self.database.commit()
        if claimed.rowcount != 1:
            raise ValueError("抖音授权请求已使用，请重新发起绑定")
        values = _read_env(self.env_path)
        client = douyin.DouyinClient()
        try:
            token = client.exchange_code(
                client_key=str(values.get("DOUYIN_CLIENT_KEY") or ""),
                client_secret=str(values.get("DOUYIN_CLIENT_SECRET") or ""),
                code=code,
            )
        finally:
            client.close()
        self._save_douyin_account(token)
        return self.douyin_account_status()

    def disconnect_douyin(self) -> dict:
        removed = self.database.execute(
            "DELETE FROM platform_accounts WHERE platform='douyin'"
        ).rowcount
        self.database.commit()
        return {"platform": "douyin", "disconnected": bool(removed)}

    def _douyin_credentials(self, client: douyin.DouyinClient) -> dict:
        row = self.database.execute(
            "SELECT * FROM platform_accounts WHERE platform='douyin' AND status='active'"
        ).fetchone()
        if not row:
            raise ValueError("请先绑定抖音账号")
        now = int(time.time())
        if int(row["refresh_expires_at"]) <= now:
            raise ValueError("抖音账号授权已过期，请重新绑定")
        if int(row["access_expires_at"]) <= now + 300:
            values = _read_env(self.env_path)
            token = client.refresh_token(
                client_key=str(values.get("DOUYIN_CLIENT_KEY") or ""),
                refresh_token=row["refresh_token"],
            )
            self._save_douyin_account(token)
            row = self.database.execute(
                "SELECT * FROM platform_accounts WHERE platform='douyin'"
            ).fetchone()
        return dict(row)

    @staticmethod
    def _character_preview_url(metadata: dict) -> str | None:
        """Return a provider-hosted preview without exposing arbitrary metadata."""
        keys = {
            "preview_url", "previewurl", "thumbnail_url", "thumbnailurl",
            "cover_url", "coverurl", "image_url", "imageurl", "portrait_url", "url",
        }
        pending: list[object] = [metadata]
        while pending:
            value = pending.pop(0)
            if isinstance(value, dict):
                for key, nested in value.items():
                    if str(key).lower() in keys and isinstance(nested, str):
                        candidate = nested.strip()
                        if candidate.startswith("https://"):
                            return candidate
                    if isinstance(nested, (dict, list)):
                        pending.append(nested)
            elif isinstance(value, list):
                pending.extend(value)
        return None

    def _character_usages(self, character_id: str, asset_id: str) -> list[dict]:
        usages: list[dict] = []
        jobs = self.database.execute(
            "SELECT job_id,title,state,characters_json,storyboard FROM jobs "
            "ORDER BY updated_at DESC"
        ).fetchall()
        for job in jobs:
            characters: list[dict] = []
            try:
                decoded = json.loads(job["characters_json"] or "[]")
                if isinstance(decoded, list):
                    characters = [item for item in decoded if isinstance(item, dict)]
            except (json.JSONDecodeError, TypeError):
                pass
            if not characters:
                try:
                    storyboard = json.loads(job["storyboard"] or "{}")
                    decoded = storyboard.get("characters", []) if isinstance(storyboard, dict) else []
                    if isinstance(decoded, list):
                        characters = [item for item in decoded if isinstance(item, dict)]
                except (json.JSONDecodeError, TypeError):
                    pass
            if any(
                str(item.get("id") or item.get("character_id") or "") == character_id
                or str(item.get("asset_id") or "") == asset_id
                for item in characters
            ):
                usages.append({
                    "job_id": job["job_id"], "title": job["title"],
                    "state": job["state"],
                })
        return usages

    def characters(self, *, keyword: str = "", identity_type: str = "",
                   status: str = "") -> dict:
        bindings = _rows(self.database.execute(
            "SELECT character_id,display_name,identity_type,gender,provider,asset_id,asset_uri,source,"
            "status,background_description,background_description_source,metadata_json,"
            "created_at,updated_at FROM character_asset_bindings "
            "ORDER BY updated_at DESC"
        ))
        keyword_normalized = keyword.strip().lower()
        result: list[dict] = []
        for binding in bindings:
            if identity_type and binding["identity_type"] != identity_type:
                continue
            if status and binding["status"] != status:
                continue
            haystack = " ".join(str(binding.get(key) or "") for key in (
                "character_id", "display_name", "asset_id"
            )).lower()
            if keyword_normalized and keyword_normalized not in haystack:
                continue
            try:
                metadata = json.loads(binding.pop("metadata_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            usages = self._character_usages(
                binding["character_id"], binding["asset_id"]
            )
            images = self._character_images(binding["character_id"])
            voice_binding = character_voices.get_binding(
                self.database, binding["character_id"]
            )
            binding.update({
                "preview_url": self._local_character_preview_url(binding["character_id"]),
                "images": images,
                "image_count": len(images),
                "usage_count": len(usages),
                "usages": usages,
                "deletable": not usages,
                "voice_binding": voice_binding,
            })
            result.append(binding)
        return {"characters": result, "total": len(result)}

    def _volc_speech_client(self) -> volc_tts.VolcSpeechClient:
        config = {**_read_env(self.env_path), **os.environ}
        return volc_tts.VolcSpeechClient(
            ak=config.get("VOLC_AK") or None,
            sk=config.get("VOLC_SK") or None,
            region=config.get("ARK_REGION") or "cn-beijing",
        )

    def voice_catalog(self) -> dict:
        client = self._volc_speech_client()
        try:
            speakers = client.list_speakers()
        finally:
            client.close()
        return {
            "voices": [{
                "voice_type": str(item.get("VoiceType") or ""),
                "voice_name": str(item.get("Name") or item.get("VoiceType") or ""),
                "gender": str(item.get("Gender") or ""),
                "age": str(item.get("Age") or ""),
                "description": str(item.get("Description") or ""),
                "labels": [
                    str(value) for field in (
                        "Categories", "NormalLabels", "SpecialLabels"
                    ) for value in (item.get(field) or [])
                ],
                "preview_url": str(
                    item.get("TrialURL") or item.get("ShortTrialURL") or ""
                ),
                "resource_id": str(
                    item.get("ResourceID") or "seed-tts-2.0"
                ),
            } for item in speakers if str(item.get("VoiceType") or "").strip()],
        }

    def _character_voice_profile(self, character_id: str) -> dict:
        row = self.database.execute(
            "SELECT character_id,display_name,gender,background_description "
            "FROM character_asset_bindings WHERE character_id=?",
            (character_id,),
        ).fetchone()
        if row is None:
            raise ValueError("角色资产不存在")
        return {
            "id": row["character_id"],
            "name": row["display_name"],
            "gender": row["gender"],
            "background_description": row["background_description"],
            "voice_directive": row["background_description"],
        }

    def auto_match_character_voice(self, character_id: str) -> dict:
        profile = self._character_voice_profile(character_id)
        client = self._volc_speech_client()
        try:
            match = character_voices.match_voice(
                profile, client.list_speakers()
            )
        finally:
            client.close()
        return character_voices.save_auto_binding(
            self.database, character=profile, match=match
        )

    def set_character_voice(
        self, character_id: str, *, voice_type: str, voice_name: str = "",
        resource_id: str = "seed-tts-2.0", preview_url: str = "",
    ) -> dict:
        profile = self._character_voice_profile(character_id)
        return character_voices.save_manual_binding(
            self.database,
            character_id=character_id,
            display_name=profile["name"],
            voice_type=voice_type,
            voice_name=voice_name,
            resource_id=resource_id,
            preview_url=preview_url,
        )

    def _character_images(self, character_id: str) -> list[dict]:
        rows = self.database.execute(
            "SELECT image_id,original_name,sort_order,created_at "
            "FROM character_reference_images WHERE character_id=? "
            "ORDER BY sort_order,created_at,image_id", (character_id,),
        ).fetchall()
        return [{
            "image_id": row["image_id"],
            "original_name": row["original_name"],
            "sort_order": row["sort_order"],
            "url": (
                f"/api/characters/{character_id}/images/{row['image_id']}"
            ),
        } for row in rows]

    def _character_image_paths(self, character_id: str) -> list[Path]:
        rows = self.database.execute(
            "SELECT relative_path FROM character_reference_images "
            "WHERE character_id=? ORDER BY sort_order,created_at,image_id",
            (character_id,),
        ).fetchall()
        return [self._character_image_path(row["relative_path"]) for row in rows]

    def _character_image_path(self, relative_path: str) -> Path:
        relative = Path(str(relative_path or ""))
        if relative.is_absolute():
            raise ValueError("角色图片路径无效")
        if relative.parts and relative.parts[0].casefold() == "data":
            relative = Path(*relative.parts[1:])
        path = (self.data_dir / relative).resolve()
        asset_root = (self.data_dir / "character_assets").resolve()
        if not path.is_relative_to(asset_root):
            raise ValueError("角色图片路径无效")
        return path

    def create_custom_character(
        self, *, character_id: str, display_name: str,
        background_description: str, gender: str = "",
    ) -> dict:
        character_id = str(character_id or "").strip()
        display_name = str(display_name or "").strip()
        description = str(background_description or "").strip()
        gender = str(gender or "").strip().lower()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,63}", character_id):
            raise ValueError("自定义角色 ID 需为 2-64 位英文、数字、点、横线或下划线")
        if not display_name:
            raise ValueError("角色名称不能为空")
        if not description:
            raise ValueError("自定义角色必须填写人物描述")
        if gender not in {"male", "female"}:
            raise ValueError("自定义角色必须选择人物性别")
        if self.database.execute(
            "SELECT 1 FROM character_asset_bindings WHERE character_id=?",
            (character_id,),
        ).fetchone():
            raise ValueError("角色 ID 已存在")
        self.database.execute(
            "INSERT INTO character_asset_bindings("
            "character_id,display_name,identity_type,gender,provider,asset_id,asset_uri,"
            "source,status,background_description,background_description_source,"
            "metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                character_id, display_name, "custom", gender, "local_custom",
                f"custom-{character_id}", f"local-character://{character_id}",
                "upload", "Inactive", description, "manual", "{}",
            ),
        )
        self.database.commit()
        return {
            "character_id": character_id,
            "display_name": display_name,
            "identity_type": "custom",
            "gender": gender,
            "status": "Inactive",
            "background_description": description,
            "images": [],
        }

    def add_custom_character_image(
        self, character_id: str, image_path: Path, *, original_name: str = ""
    ) -> dict:
        binding = self.database.execute(
            "SELECT identity_type FROM character_asset_bindings WHERE character_id=?",
            (character_id,),
        ).fetchone()
        if not binding or binding["identity_type"] != "custom":
            raise ValueError("自定义角色不存在")
        source = Path(image_path).resolve()
        suffix = source.suffix.lower()
        if suffix not in CUSTOM_CHARACTER_IMAGE_EXTENSIONS:
            raise ValueError("角色图片仅支持 JPG、PNG 或 WebP")
        size = source.stat().st_size if source.is_file() else 0
        if size <= 0:
            raise ValueError("角色图片为空或不存在")
        if size > CUSTOM_CHARACTER_IMAGE_MAX_BYTES:
            raise OverflowError("单张角色图片不能超过 20 MB")
        count = self.database.execute(
            "SELECT COUNT(*) FROM character_reference_images WHERE character_id=?",
            (character_id,),
        ).fetchone()[0]
        if count >= CUSTOM_CHARACTER_IMAGE_MAX_COUNT:
            raise ValueError("每个自定义角色最多上传 24 张图片")
        digest = hashlib.sha256(
            character_id.encode("utf-8") + source.read_bytes()
        ).hexdigest()[:16]
        image_id = f"IMG-{digest}"
        relative = Path("character_assets") / character_id / f"{image_id}{suffix}"
        target = self.data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(source, target)
        self.database.execute(
            "INSERT OR IGNORE INTO character_reference_images("
            "image_id,character_id,relative_path,original_name,sort_order) "
            "VALUES(?,?,?,?,?)",
            (image_id, character_id, relative.as_posix(), original_name or source.name, count),
        )
        self.database.execute(
            "UPDATE character_asset_bindings SET status='Active',"
            "updated_at=CURRENT_TIMESTAMP WHERE character_id=?", (character_id,),
        )
        self.database.commit()
        return {
            "character_id": character_id, "image_id": image_id,
            "url": f"/api/characters/{character_id}/images/{image_id}",
            "image_count": len(self._character_images(character_id)),
            "status": "Active",
        }

    def character_image_file(self, character_id: str, image_id: str) -> Path:
        row = self.database.execute(
            "SELECT relative_path FROM character_reference_images "
            "WHERE character_id=? AND image_id=?", (character_id, image_id),
        ).fetchone()
        if not row:
            raise ValueError("角色图片不存在")
        path = self._character_image_path(row["relative_path"])
        if not path.is_file():
            raise ValueError("角色图片不存在")
        return path

    def bind_character(self, *, character_id: str, display_name: str,
                       asset_id: str, identity_type: str = "real_person",
                       background_description: str = "", gender: str = "") -> dict:
        if identity_type == "custom":
            return self.create_custom_character(
                character_id=character_id, display_name=display_name,
                gender=gender,
                background_description=background_description,
            )
        if not character_id.strip() or not display_name.strip():
            raise ValueError("角色 ID 和名称不能为空")
        identity_type = identity_type.strip()
        metadata, preview_sync = self._fetch_character_metadata(
            asset_id.strip(), identity_type
        )
        manual_background = str(background_description or "").strip()
        provider_background = self._background_from_metadata(metadata)
        effective_background = manual_background or provider_background
        background_source = (
            "manual" if manual_background else
            "volc_asset_group" if provider_background else ""
        )
        result = character_assets.bind_manual_asset(
            self.database,
            character_id=character_id.strip(),
            display_name=display_name.strip(),
            asset_id=asset_id.strip(),
            identity_type=identity_type,
            metadata=metadata,
            background_description=effective_background,
            background_description_source=background_source,
            group_id=str(metadata.get("GroupId") or "") or None,
        )
        preview_url = None
        if preview_sync == "synced":
            try:
                preview_url = self._cache_character_preview(character_id.strip(), metadata)
            except (httpx.HTTPError, OSError):
                preview_sync = "error"
        return {
            **result.as_dict(), "status": "Active",
            "preview_url": preview_url,
            "preview_sync": preview_sync,
            "background_description": effective_background,
            "background_description_source": background_source,
        }

    @staticmethod
    def _background_from_metadata(metadata: dict) -> str:
        group = metadata.get("AssetGroup") if isinstance(metadata, dict) else None
        if isinstance(group, dict):
            description = str(group.get("Description") or "").strip()
            if description:
                return description
        return str((metadata or {}).get("Description") or "").strip()

    def _fetch_character_metadata(
        self, asset_id: str, identity_type: str = "real_person"
    ) -> tuple[dict, str]:
        config = _read_env(self.env_path)
        client = None
        try:
            client = volc_assets.VolcAssetClient(
                ak=config.get("VOLC_AK") or None,
                sk=config.get("VOLC_SK") or None,
                region=config.get("VOLC_ASSETS_REGION") or None,
                endpoint=config.get("VOLC_ASSETS_ENDPOINT") or None,
                project_name=config.get("VOLC_ASSETS_PROJECT") or None,
            )
            metadata = client.get_asset(asset_id)
            metadata = metadata if isinstance(metadata, dict) else {}
            group_id = str(metadata.get("GroupId") or "").strip()
            if identity_type == "virtual_person" and group_id:
                group = client.get_asset_group(group_id)
                if isinstance(group, dict):
                    metadata["AssetGroup"] = group
            return metadata, "synced"
        except volc_assets.AssetApiUnavailable:
            return {}, "unavailable"
        except volc_assets.AssetApiError:
            return {}, "error"
        finally:
            if client is not None:
                client.close()

    def _cache_character_preview(self, character_id: str, metadata: dict) -> str | None:
        remote_url = self._character_preview_url(metadata)
        if not remote_url:
            return None
        response = httpx.get(remote_url, timeout=20, follow_redirects=True)
        response.raise_for_status()
        content_type = str(response.headers.get("content-type") or "").lower()
        suffix = ".png" if "png" in content_type else ".jpg"
        preview_root = self.data_dir / "character_previews"
        preview_root.mkdir(parents=True, exist_ok=True)
        preview_key = hashlib.sha256(character_id.encode("utf-8")).hexdigest()
        for old in preview_root.glob(f"{preview_key}.*"):
            old.unlink(missing_ok=True)
        target = preview_root / f"{preview_key}{suffix}"
        target.write_bytes(response.content)
        return f"/api/characters/{character_id}/preview"

    def character_preview_file(self, character_id: str) -> Path:
        custom_paths = self._character_image_paths(character_id)
        if custom_paths:
            return custom_paths[0]
        preview_root = self.data_dir / "character_previews"
        preview_key = hashlib.sha256(character_id.encode("utf-8")).hexdigest()
        matches = list(preview_root.glob(f"{preview_key}.*")) if preview_root.exists() else []
        if not matches:
            raise ValueError("角色缩略图不存在")
        return matches[0]

    def character_identity_preview_file(
        self, job_id: str, identity_token: str
    ) -> Path:
        rows = self.database.execute(
            "SELECT identity_key,preview_path FROM character_identity_anchors "
            "WHERE job_id=? AND state='READY'",
            (job_id,),
        ).fetchall()
        for row in rows:
            if character_assets.identity_token(row["identity_key"]) != identity_token:
                continue
            path = Path(str(row["preview_path"] or ""))
            if path.is_file():
                return path
        raise ValueError("角色定妆预览不存在")

    def scene_anchor_preview_file(
        self, job_id: str, anchor_token: str
    ) -> Path:
        rows = self.database.execute(
            "SELECT anchor_key,preview_path FROM scene_visual_anchors "
            "WHERE job_id=? AND state='READY'",
            (job_id,),
        ).fetchall()
        for row in rows:
            if scene_assets.anchor_token(row["anchor_key"]) != anchor_token:
                continue
            path = Path(str(row["preview_path"] or ""))
            if path.is_file():
                return path
        raise ValueError("场景锚点预览不存在")

    def _local_character_preview_url(self, character_id: str) -> str | None:
        primary = self.database.execute(
            "SELECT image_id FROM character_reference_images "
            "WHERE character_id=? ORDER BY sort_order,created_at,image_id LIMIT 1",
            (character_id,),
        ).fetchone()
        try:
            self.character_preview_file(character_id)
            version = f"?v={primary['image_id']}" if primary else ""
            return f"/api/characters/{character_id}/preview{version}"
        except ValueError:
            return None

    def refresh_character(self, character_id: str) -> dict:
        binding = self.database.execute(
            "SELECT character_id,asset_id,identity_type,metadata_json,"
            "background_description,background_description_source "
            "FROM character_asset_bindings "
            "WHERE character_id=?", (character_id,)
        ).fetchone()
        if not binding:
            raise ValueError("角色资产不存在")
        if binding["identity_type"] == "custom":
            return {
                "character_id": character_id,
                "preview_sync": "local",
                "preview_url": self._local_character_preview_url(character_id),
                "background_description": binding["background_description"],
                "background_description_source": binding["background_description_source"],
            }
        metadata, preview_sync = self._fetch_character_metadata(
            binding["asset_id"], binding["identity_type"]
        )
        preview_url = None
        background_description = binding["background_description"]
        background_source = binding["background_description_source"]
        if preview_sync == "synced":
            provider_background = self._background_from_metadata(metadata)
            if background_source != "manual" and provider_background:
                background_description = provider_background
                background_source = "volc_asset_group"
            self.database.execute(
                "UPDATE character_asset_bindings SET metadata_json=?,"
                "background_description=?,background_description_source=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE character_id=?",
                (
                    json.dumps(metadata, ensure_ascii=False),
                    background_description, background_source, character_id,
                ),
            )
            self.database.commit()
            try:
                preview_url = self._cache_character_preview(character_id, metadata)
            except (httpx.HTTPError, OSError):
                preview_sync = "error"
        else:
            try:
                metadata = json.loads(binding["metadata_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        return {
            "character_id": character_id,
            "preview_sync": preview_sync,
            "preview_url": preview_url or self._local_character_preview_url(character_id),
            "background_description": background_description,
            "background_description_source": background_source,
        }

    def update_character_background(
        self, character_id: str, background_description: str
    ) -> dict:
        description = str(background_description or "").strip()
        if not description:
            raise ValueError("角色背景描述不能为空")
        cursor = self.database.execute(
            "UPDATE character_asset_bindings SET background_description=?,"
            "background_description_source='manual',updated_at=CURRENT_TIMESTAMP "
            "WHERE character_id=?",
            (description, character_id),
        )
        self.database.commit()
        if cursor.rowcount != 1:
            raise ValueError("角色资产不存在")
        return {
            "character_id": character_id,
            "background_description": description,
            "background_description_source": "manual",
        }

    def set_character_status(self, character_id: str, status: str) -> dict:
        if status not in {"Active", "Inactive"}:
            raise ValueError("角色状态只能是 Active 或 Inactive")
        if status == "Active":
            binding = self.database.execute(
                "SELECT identity_type FROM character_asset_bindings WHERE character_id=?",
                (character_id,),
            ).fetchone()
            if (binding and binding["identity_type"] == "custom"
                    and not self._character_images(character_id)):
                raise ValueError("自定义角色至少上传一张图片后才能启用")
        cursor = self.database.execute(
            "UPDATE character_asset_bindings SET status=?,updated_at=CURRENT_TIMESTAMP "
            "WHERE character_id=?", (status, character_id)
        )
        self.database.commit()
        if cursor.rowcount != 1:
            raise ValueError("真人角色不存在")
        return {"character_id": character_id, "status": status}

    def delete_character(self, character_id: str) -> dict:
        binding = self.database.execute(
            "SELECT character_id,asset_id FROM character_asset_bindings "
            "WHERE character_id=?", (character_id,)
        ).fetchone()
        if not binding:
            raise ValueError("角色资产不存在")
        usages = self._character_usages(binding["character_id"], binding["asset_id"])
        if usages:
            raise ValueError(
                f"角色已被 {len(usages)} 个视频任务使用，不能删除；请改为停用"
            )
        image_paths = self._character_image_paths(character_id)
        self.database.execute(
            "DELETE FROM character_reference_images WHERE character_id=?", (character_id,)
        )
        self.database.execute(
            "DELETE FROM character_asset_bindings WHERE character_id=?", (character_id,)
        )
        self.database.commit()
        for path in image_paths:
            path.unlink(missing_ok=True)
        custom_root = self.data_dir / "character_assets" / character_id
        if custom_root.exists():
            shutil.rmtree(custom_root)
        return {"character_id": character_id, "deleted": True}

    def start_video_job(
        self, job_id: str, *, confirmed: bool,
        approved_budget_cents: int | None = None,
        high_risk_confirmed: bool = False,
    ) -> dict:
        if confirmed:
            self._require_transport_preflight()
        return self.production_service.start(
            job_id,
            confirmed=confirmed,
            approved_budget_cents=approved_budget_cents,
            high_risk_confirmed=high_risk_confirmed,
        )

    def confirm_video_budget(
        self, job_id: str, *, confirmed: bool,
        approved_budget_cents: int,
        max_auto_retries: int = 0,
    ) -> dict:
        """Confirm a per-job budget and resume only unpaid paused nodes."""
        if not confirmed:
            raise ValueError("继续付费生成前必须明确确认任务预算")
        self._repair_legacy_budget_pauses(job_id)
        job = self.database.execute(
            "SELECT state,storyboard,cost_cents FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not job:
            raise ValueError("视频任务不存在")
        if str(job["state"]) != "BUDGET_CONFIRMATION_REQUIRED":
            raise ValueError("当前任务不在预算待确认状态")
        self._assert_no_unresolved_provider_tasks(job_id)
        try:
            budget = int(approved_budget_cents)
            retries = int(max_auto_retries)
        except (TypeError, ValueError) as exc:
            raise ValueError("任务预算和自动重试次数必须是整数") from exc
        if budget <= 0:
            raise ValueError("任务预算必须大于 0")
        if retries not in {0, 1}:
            raise ValueError("自动重试次数只能为 0 或 1")
        nodes = self.database.execute(
            "SELECT node_id,estimated_cost_cents,actual_cost_cents,"
            "provider_task_id,output_json FROM production_nodes "
            "WHERE job_id=? AND state='BUDGET_REQUIRED' ORDER BY node_id",
            (job_id,),
        ).fetchall()
        if not nodes:
            raise ValueError("没有等待预算确认的生产节点")
        if any(
            int(row["actual_cost_cents"] or 0) > 0
            or str(row["provider_task_id"] or "").strip()
            for row in nodes
        ):
            raise ValueError("预算暂停节点存在远端任务或已记账成本，必须先核对")
        required_additional = 0
        resumed_indexes: list[int] = []
        for row in nodes:
            try:
                output = json.loads(row["output_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                output = {}
            single_attempt = max(0, int(row["estimated_cost_cents"] or 0))
            guarded_estimate = max(
                single_attempt,
                int(output.get("next_estimate_cents") or 0),
            )
            # The runtime estimate already represents one concrete provider
            # request, including billable reference-video duration.  Retry
            # policy must not replace that amount with the cheaper frozen
            # node estimate, otherwise confirmation immediately pauses again.
            required_additional += guarded_estimate
            node_id = str(row["node_id"])
            if node_id.startswith("master:"):
                try:
                    resumed_indexes.append(int(node_id.split(":", 1)[1]))
                except ValueError:
                    pass
        current_cost = max(0, int(job["cost_cents"] or 0))
        required_total = current_cost + required_additional
        if budget < required_total:
            raise ValueError(
                f"确认预算不足：至少需要 ¥{required_total / 100:.2f}"
            )
        try:
            storyboard = json.loads(job["storyboard"] or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("任务制作设置已损坏，无法确认预算") from exc
        settings = storyboard.setdefault("production_settings", {})
        settings["budget_cents"] = budget
        snapshot = settings.setdefault(
            production_config.GENERATION_SNAPSHOT_KEY, {}
        )
        snapshot["job_budget_cents"] = budget
        snapshot["max_auto_retries"] = retries
        if retries == 0:
            snapshot["auto_retry_budget_cents"] = 0
        confirmed_cost = settings.get("confirmed_cost_estimate")
        if isinstance(confirmed_cost, dict):
            confirmed_cost["budget_cents"] = budget
            confirmed_cost["projected_total_cost_cents"] = max(
                int(confirmed_cost.get("projected_total_cost_cents") or 0),
                required_total,
            )
        self._require_transport_preflight()
        with self.database:
            for row in nodes:
                node_id = str(row["node_id"])
                node_state_machine.transition_node_in_transaction(
                    self.database, job_id, node_id, "resume",
                    event_id=(
                        f"{job_id}:{node_id}:budget-confirmed:"
                        f"{budget}:{retries}:{secrets.token_hex(6)}"
                    ),
                    payload={
                        "approved_budget_cents": budget,
                        "max_auto_retries": retries,
                    },
                )
                self.database.execute(
                    "UPDATE production_nodes SET error_code=NULL,next_action=NULL,"
                    "output_json='{}',updated_at=CURRENT_TIMESTAMP "
                    "WHERE job_id=? AND node_id=? AND state='READY'",
                    (job_id, node_id),
                )
            if resumed_indexes:
                placeholders = ",".join("?" for _ in resumed_indexes)
                self.database.execute(
                    f"UPDATE shots SET state='PENDING',error=NULL "
                    f"WHERE job_id=? AND idx IN ({placeholders})",
                    (job_id, *resumed_indexes),
                )
            state_machine.transition(
                self.database, job_id, "budget_confirmed",
                event_id=(
                    f"{job_id}:budget-confirmed:{budget}:{retries}:"
                    f"{secrets.token_hex(6)}"
                ),
                updates={
                    "storyboard": json.dumps(storyboard, ensure_ascii=False),
                    "error": None,
                    "failure_code": None,
                    "production_worker_pid": None,
                },
                payload={
                    "approved_budget_cents": budget,
                    "max_auto_retries": retries,
                    "required_total_cents": required_total,
                    "resumed_master_indexes": sorted(resumed_indexes),
                },
            )
        dispatch = self.dispatch_production_queue()
        return {
            "job_id": job_id,
            "state": "PRODUCTION_QUEUED",
            "approved_budget_cents": budget,
            "max_auto_retries": retries,
            "required_total_cents": required_total,
            "resumed_master_indexes": sorted(resumed_indexes),
            "dispatched_jobs": dispatch.get("dispatched_jobs", []),
        }

    def _start_video_job_impl(
        self, job_id: str, *, confirmed: bool,
        approved_budget_cents: int | None = None,
        high_risk_confirmed: bool = False,
    ) -> dict:
        if not confirmed:
            raise ValueError("开始付费生成前必须明确确认预算")
        job = self.database.execute(
            "SELECT state,execution_engine FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not job:
            raise ValueError("视频任务不存在")
        if job["state"] in {"PRODUCTION_QUEUED", "PRODUCTION_RUNNING"}:
            raise ValueError("视频生成已经启动，请勿重复提交")
        if job["state"] != "STORYBOARDED":
            raise ValueError("只有已确认剧本的任务才能开始生成")
        self._assert_no_unresolved_provider_tasks(job_id)
        storyboard_row = self.database.execute(
            "SELECT storyboard,cost_cents FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if os.environ.get("FACTORY_V2_SHADOW", "").strip() == "1":
            from . import production_shadow

            production_shadow.compile_and_compare(
                self.database, job_id, self.root,
            )
        execution_engine = str(job["execution_engine"] or "legacy")
        target_spec_version = 2 if execution_engine == "graph_v2" else 1
        contract = production_planner.compile_job_spec(
            self.database, job_id, self.root, persist=False,
            target_version=target_spec_version,
        )
        blockers = [
            issue for issue in contract.issues if issue.severity == "blocking"
        ]
        if blockers:
            details = "；".join(
                f"{issue.code}: {issue.message}" for issue in blockers
            )
            raise ValueError(f"生产规格未通过：{details}")
        try:
            storyboard = json.loads(storyboard_row["storyboard"] or "{}")
        except json.JSONDecodeError:
            storyboard = {}
        dialogue_speakers = []
        for master in (storyboard.get("masters") or []):
            if master.get("type") != "dialogue":
                continue
            performance_beats = [
                beat for beat in (master.get("performance_beats") or [])
                if isinstance(beat, dict) and str(beat.get("dialogue") or "").strip()
            ]
            audio_only_dialogue = bool(performance_beats) and all(
                str(beat.get("speech_mode") or "on_screen")
                in {"off_screen", "narration"}
                for beat in performance_beats
            )
            speaker = str(master.get("speaker") or "").strip()
            if speaker and not audio_only_dialogue:
                dialogue_speakers.append(speaker)
        dialogue_speakers = list(dict.fromkeys(dialogue_speakers))
        configured_speakers = {
            str(character.get("id") or "").strip()
            for character in (storyboard.get("characters") or [])
            if str(character.get("id") or "").strip()
            and (
                character.get("generation_mode") == "ai_generated"
                or
                character.get("asset_id")
                or character.get("asset_uri")
                or character.get("seedance_asset_uri")
            )
        }
        missing_speakers = [
            speaker for speaker in dialogue_speakers
            if speaker not in configured_speakers
        ]
        if missing_speakers:
            raise ValueError(
                "以下剧本人物尚未指定角色资产：" + "、".join(missing_speakers)
            )
        if storyboard.get("production_mode") == "native_master_local_director":
            from factory.steps import s1_storyboard
            try:
                s1_storyboard._validate(storyboard)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"分镜未通过生产校验：{exc}") from exc
        config = _read_env(self.env_path)
        storyboard.setdefault("production_settings", {}).setdefault(
            "music_provider", music.resolve_provider(configured=config)
        )
        completed_master_indexes = {
            int(row["idx"])
            for row in self.database.execute(
                "SELECT idx FROM shots WHERE job_id=? AND state='OK'",
                (job_id,),
            ).fetchall()
        }
        preflight = production_planner.analyze_storyboard(
            self.database,
            storyboard,
            budget_cents=(
                storyboard.get("production_settings", {}).get("budget_cents")
                or config.get("SEEDANCE_JOB_BUDGET_CENTS")
            ),
            resolution=config.get("ARK_VIDEO_RESOLUTION"),
            completed_master_indexes=completed_master_indexes,
            completed_identity_keys=_completed_identity_keys(
                self.database, job_id
            ),
            completed_scene_keys=_completed_scene_keys(
                self.database, job_id
            ),
            current_cost_cents=int(storyboard_row["cost_cents"] or 0),
        )
        if execution_engine == "graph_v2" and contract.spec is not None:
            completed_node_ids = {
                str(row["node_id"])
                for row in self.database.execute(
                    "SELECT node_id,state FROM production_nodes WHERE job_id=?",
                    (job_id,),
                ).fetchall()
                if str(row["state"]) in {"SUCCEEDED", "SKIPPED"}
            }
            preflight = production_planner.align_preflight_with_compiled_spec(
                preflight,
                contract.spec,
                completed_node_ids=completed_node_ids,
                current_cost_cents=int(storyboard_row["cost_cents"] or 0),
            )
        requires_feasibility_confirmation = preflight.get(
            "requires_master_feasibility_confirmation",
            preflight.get("requires_long_master_confirmation"),
        )
        if requires_feasibility_confirmation and not high_risk_confirmed:
            high_complexity_indexes = preflight.get(
                "high_complexity_master_indexes",
                preflight.get("high_risk_master_indexes", []),
            )
            indexes = "、".join(
                f"#{idx}" for idx in high_complexity_indexes
            )
            raise ValueError(
                f"母片存在高复杂度生成风险（{indexes}）；"
                "请明确确认可生成性风险后再开始生成"
            )
        if not preflight["budget_ok"]:
            try:
                approved_budget = int(approved_budget_cents or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError("提高后的任务预算必须是整数分") from exc
            projected = int(preflight["projected_total_cost_cents"] or 0)
            if approved_budget < projected:
                raise ValueError(
                    "预计累计成本超过当前任务预算；请在预检中明确提高预算后再开始生成"
                )
            settings = storyboard.setdefault("production_settings", {})
            settings["budget_cents"] = approved_budget
            snapshot = settings.get(
                production_config.GENERATION_SNAPSHOT_KEY
            )
            if isinstance(snapshot, dict):
                snapshot["job_budget_cents"] = approved_budget
        settings = storyboard.setdefault("production_settings", {})
        settings["confirmed_cost_estimate"] = {
            "estimated_additional_cost_cents": int(
                preflight.get("estimated_cost_cents") or 0
            ),
            "projected_total_cost_cents": int(
                preflight.get("projected_total_cost_cents") or 0
            ),
            "current_cost_cents": int(
                preflight.get("current_cost_cents") or 0
            ),
            "budget_cents": int(
                settings.get("budget_cents")
                or preflight.get("budget_cents")
                or 0
            ),
            "pricing_version": seedance.ESTIMATION_PRICING_VERSION,
            "paid_masters": [
                {
                    key: item.get(key)
                    for key in (
                        "master_idx", "resolution", "duration_bucket",
                        "reference_type", "reference_manifest",
                        "estimated_cost_cents",
                    )
                }
                for item in preflight.get("paid_masters") or []
            ],
        }
        final_contract = production_planner.compile_job_spec(
            self.database,
            job_id,
            self.root,
            storyboard_override=storyboard,
            persist=(execution_engine != "graph_v2"),
            target_version=target_spec_version,
        )
        if not final_contract.payable:
            raise ValueError("生产规格在预算确认后失效，请重新执行预检")
        if execution_engine == "graph_v2":
            self.production_service.persist_compiled_execution(
                final_contract.spec,
            )
        executable_storyboard = (
            final_contract.spec.runtime_storyboard
            if execution_engine == "graph_v2"
            else storyboard
        )
        state_machine.transition(
            self.database,
            job_id,
            "queued",
            event_id=(
                f"{job_id}:queued:{final_contract.spec.spec_hash}:"
                f"{secrets.token_hex(8)}"
            ),
            updates={
                "storyboard": json.dumps(
                    executable_storyboard, ensure_ascii=False
                ),
                "error": None,
                "failure_code": None,
            },
        )
        dispatch = self.dispatch_production_queue()
        status = "started" if job_id in dispatch["dispatched_jobs"] else "queued"
        return {"job_id": job_id, "status": status}

    def approve_continuity_anchors(
        self, job_id: str, *, note: str = ""
    ) -> dict:
        """Approve canonical makeup/location anchors and resume production."""
        job = self.database.execute(
            "SELECT state,execution_engine,storyboard,characters_json "
            "FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not job:
            raise ValueError("视频任务不存在")
        if job["state"] != "ANCHOR_REVIEW_REQUIRED":
            raise ValueError("当前任务不在人物与场景锚点审核阶段")

        identity_rows = self.database.execute(
            "SELECT state,approval_state FROM character_identity_anchors "
            "WHERE job_id=? AND source_type!='volc_asset'", (job_id,)
        ).fetchall()
        scene_rows = self.database.execute(
            "SELECT state,approval_state FROM scene_visual_anchors WHERE job_id=?",
            (job_id,),
        ).fetchall()
        review_rows = [*identity_rows, *scene_rows]
        if not review_rows:
            raise ValueError("没有需要审核的人物或场景锚点")
        try:
            storyboard = json.loads(job["storyboard"] or "{}")
            characters = json.loads(job["characters_json"] or "[]")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("continuity anchor contract is invalid") from exc
        units = storyboard.get("masters") or storyboard.get("shots") or []
        required_ids = s3_shots._required_character_ids(units)
        required_characters = [
            character for character in characters
            if str(character.get("id") or "") in required_ids
        ]
        required_identity_keys = {
            entry["identity_key"]
            for entry in character_assets.character_makeup_plan(required_characters)
            if entry["requires_paid_call"]
        }
        materialized_identity_keys = {
            str(row["identity_key"])
            for row in self.database.execute(
                "SELECT identity_key FROM character_identity_anchors "
                "WHERE job_id=? AND source_type!='volc_asset'",
                (job_id,),
            ).fetchall()
        }
        missing_identity_keys = sorted(
            required_identity_keys - materialized_identity_keys
        )
        if missing_identity_keys:
            raise ValueError(
                "missing required character anchors: "
                + ", ".join(missing_identity_keys)
            )
        if any(str(row["state"]) != "READY" for row in review_rows):
            raise ValueError("人物与场景锚点尚未全部生成完成")

        review_note = str(note or "").strip()
        # Capture only fallback dependencies being approved now. A generic
        # candidate review (or a marker without this dependency) must not be
        # treated as permission to re-run a paid master.
        fallback_dependents = self.database.execute(
            "SELECT n.node_id,a.identity_key,a.video_url,a.video_path,a.cost_cents "
            "FROM production_nodes n JOIN production_edges e "
            "ON e.job_id=n.job_id AND e.to_node_id=n.node_id "
            "AND e.edge_type='identity_reference' "
            "JOIN character_identity_anchors a ON a.job_id=n.job_id "
            "AND e.from_node_id='character_anchor:' || a.character_id "
            "WHERE n.job_id=? AND n.node_type='master' "
            "AND n.state='REVIEW_REQUIRED' AND n.error_code=? "
            "AND a.source_type='seedance_identity_fallback' AND a.state='READY' "
            "AND a.approval_state!='APPROVED' ORDER BY n.node_id,a.identity_key",
            (job_id, s3_shots.ContinuityAnchorReviewPendingError.stable_code),
        ).fetchall()
        self.database.execute(
            "UPDATE character_identity_anchors SET approval_state='APPROVED',"
            "approved_at=CURRENT_TIMESTAMP,review_note=?,"
            "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND state='READY' "
            "AND source_type!='volc_asset'",
            (review_note, job_id),
        )
        self.database.execute(
            "UPDATE scene_visual_anchors SET approval_state='APPROVED',"
            "approved_at=CURRENT_TIMESTAMP,review_note=?,"
            "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND state='READY'",
            (review_note, job_id),
        )
        if str(job["execution_engine"] or "legacy") == "graph_v2":
            review_nodes = self.database.execute(
                "SELECT node_id,provider_task_id,actual_cost_cents "
                "FROM production_nodes WHERE job_id=? "
                "AND node_type IN ('character_anchor','scene_anchor') "
                "AND state='REVIEW_REQUIRED' ORDER BY node_id",
                (job_id,),
            ).fetchall()
            for node in review_nodes:
                node_id = str(node["node_id"])
                node_state_machine.transition_node(
                    self.database,
                    job_id,
                    node_id,
                    "approve",
                    event_id=(
                        f"{job_id}:{node_id}:anchor-approved:"
                        f"{str(node['provider_task_id'] or 'local')}:"
                        f"{int(node['actual_cost_cents'] or 0)}"
                    ),
                    payload={"review_note": review_note},
                )
            approved_fallbacks: dict[str, list[dict[str, str]]] = {}
            for fallback in fallback_dependents:
                # Identity rows have no provider_task_id column. Bind the
                # event to location + cumulative billed cost, never review time.
                # A newly paid task can overwrite the same object path; an
                # idempotent replay has zero cost delta and stays the same event.
                artifact = hashlib.sha256(json.dumps(
                    [fallback["video_url"], fallback["video_path"], int(fallback["cost_cents"] or 0)],
                    ensure_ascii=False,
                ).encode("utf-8")).hexdigest()
                approved_fallbacks.setdefault(str(fallback["node_id"]), []).append({
                    "identity_key": str(fallback["identity_key"]), "artifact": artifact,
                })
            for node_id, fallbacks in approved_fallbacks.items():
                artifact_set = hashlib.sha256(json.dumps(
                    fallbacks, ensure_ascii=False, sort_keys=True,
                ).encode("utf-8")).hexdigest()
                result = node_state_machine.transition_node_in_transaction(
                    self.database, job_id, node_id, "resume_anchor_dependency",
                    event_id=f"{job_id}:{node_id}:anchor-dependency-approved:{artifact_set}",
                    payload={"review_note": review_note, "approved_fallbacks": fallbacks},
                )
                if result.applied:
                    self.database.execute(
                        "UPDATE production_nodes SET error_code=NULL,next_action=NULL "
                        "WHERE job_id=? AND node_id=? AND state='READY'",
                        (job_id, node_id),
                    )
        queued = self.database.execute(
            "UPDATE jobs SET state='PRODUCTION_QUEUED',error=NULL,"
            "production_worker_pid=NULL,updated_at=CURRENT_TIMESTAMP "
            "WHERE job_id=? AND state='ANCHOR_REVIEW_REQUIRED'",
            (job_id,),
        )
        self.database.commit()
        if queued.rowcount != 1:
            raise ValueError("锚点审核状态已经发生变化，请刷新后重试")
        dispatch = self.dispatch_production_queue()
        return {
            "job_id": job_id,
            "state": "PRODUCTION_QUEUED",
            "dispatched_jobs": dispatch["dispatched_jobs"],
        }

    def register_publication(self, payload: dict) -> dict:
        with self._final_review_lock, node_state_machine.atomic(self.database):
            return self._register_publication_in_transaction(payload)

    def _publication_registration_snapshot(self, job_id: str) -> tuple[str, str | None]:
        job = self.database.execute('SELECT state FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if not job:
            raise ValueError('视频任务不存在')
        if job['state'] not in {'READY_TO_PUBLISH', 'PUBLISHED'}:
            raise ValueError('content: 只有待发布成片才能登记渠道发布结果；状态已变化，请刷新后重试')
        report = content_review.require_final(self.database, self.data_dir / 'assets', job_id)
        revision = content_review.publication_revision(self.database, job_id, report) if report else None
        return str(job['state']), revision

    def _require_publication_registration_snapshot(self, job_id: str, expected: tuple[str, str | None]) -> None:
        if self._publication_registration_snapshot(job_id) != expected:
            raise ValueError('content publication registration state/report revision is stale')

    def _register_publication_in_transaction(self, payload: dict) -> dict:
        required = ("job_id", "platform", "item_id")
        missing = [key for key in required if not str(payload.get(key) or "").strip()]
        if missing:
            raise ValueError("缺少发布字段：" + ", ".join(missing))
        job_id = str(payload["job_id"]).strip()
        snapshot = self._publication_registration_snapshot(job_id)
        platform = str(payload["platform"]).strip()
        if platform not in {"douyin", "weixin_channels"}:
            raise ValueError("发布渠道仅支持抖音或微信视频号")
        publication_id = f"PUB-{payload['platform']}-{payload['item_id']}"
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("发布元数据格式错误")
        self.database.execute(
            "INSERT INTO platform_publications(publication_id,job_id,platform,"
            "item_id,item_url,title,published_at,metadata_json) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(job_id,platform) DO UPDATE SET item_id=excluded.item_id,"
            "item_url=excluded.item_url,title=excluded.title,"
            "published_at=excluded.published_at,metadata_json=excluded.metadata_json,"
            "updated_at=CURRENT_TIMESTAMP",
            (
                publication_id, job_id, platform,
                str(payload["item_id"]).strip(), str(payload.get("item_url") or "").strip(),
                str(payload.get("title") or "").strip(),
                str(payload.get("published_at") or "").strip() or None,
                json.dumps(metadata, ensure_ascii=False),
            ),
        )
        if platform == "douyin":
            submission_state = (
                "pending_review"
                if _publication_is_pending(json.dumps(metadata, ensure_ascii=False))
                else "published"
            )
            self.database.execute(
                "UPDATE platform_submissions SET state=?,item_id=?,last_error='',"
                "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND platform=?",
                (
                    submission_state,
                    str(payload["item_id"]).strip(),
                    job_id,
                    platform,
                ),
            )
        self._require_publication_registration_snapshot(job_id, snapshot)
        return {"publication_id": publication_id, "status": "saved"}

    def _douyin_publish_identity(self, job_id: str) -> dict:
        path = self.video_file(job_id)
        sha = _file_sha256(path)
        report = content_review.require_final(self.database, self.data_dir / 'assets', job_id)
        if (self.video_file(job_id) != path or _file_sha256(path) != sha
            or (report and report.get('final_sha256') != sha)):
            raise ValueError('content selected upload path changed during identity verification')
        revision = (content_review.publication_revision(self.database, job_id, report)
            if report else content_review.digest({'legacy_final_sha256': sha}))
        return {'final_path': str(path), 'final_sha256': sha, 'verified_revision': revision}

    def _require_douyin_publish_identity(self, job_id: str, expected: dict) -> None:
        row = self.database.execute('SELECT state FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None or row['state'] not in {'READY_TO_PUBLISH', 'PUBLISHED'}:
            raise ValueError('content publication authorization changed; reconcile before retry')
        actual_upload_path = Path(expected['final_path'])
        if _file_sha256(actual_upload_path) != expected['final_sha256']:
            raise ValueError('content actual upload file changed; reconcile before retry')
        if self._douyin_publish_identity(job_id) != expected:
            raise ValueError('content final/review revision changed during publication; reconcile before retry')
        if _file_sha256(actual_upload_path) != expected['final_sha256']:
            raise ValueError('content actual upload file changed during identity verification')

    @staticmethod
    def _matching_douyin_upload_binding(submission, identity: dict) -> bool:
        if not submission:
            return False
        binding = content_review.object_json(submission['metadata_json']).get('upload_binding')
        return (isinstance(binding, dict) and binding.get('version') == 1
            and binding.get('authority') == 'management_douyin_upload_v1'
            and bool(submission['upload_id']) and binding.get('upload_id') == submission['upload_id']
            and all(binding.get(key) == value for key, value in identity.items()))

    def publish_to_douyin(self, job_id: str, payload: dict) -> dict:
        with self._final_review_lock:
            return self._publish_to_douyin_impl(job_id, payload)

    def _publish_to_douyin_impl(self, job_id: str, payload: dict) -> dict:
        content_review.require_final(self.database, self.data_dir / 'assets', job_id)
        if payload.get("confirmed") is not True:
            raise ValueError("请确认标题、封面和目标账号后再提交抖音发布")
        job_id = str(job_id or "").strip()
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("抖音视频标题不能为空")
        if len(text) > 1000:
            raise ValueError("抖音视频标题不能超过 1000 个字符")
        job = self.database.execute(
            "SELECT title,state FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not job:
            raise ValueError("视频任务不存在")
        if job["state"] not in {"READY_TO_PUBLISH", "PUBLISHED"}:
            raise ValueError("只有已验收并提交发布的成片才能推送到抖音")
        identity = self._douyin_publish_identity(job_id)
        # The upload consumes the path in this verified snapshot. Never select
        # a path separately and let a later job read authorize different bytes.
        final_path = Path(identity['final_path'])
        cover_tsp = payload.get("cover_tsp")
        submission = self.database.execute(
            "SELECT * FROM platform_submissions WHERE job_id=? AND platform='douyin'",
            (job_id,),
        ).fetchone()
        upload_matches = self._matching_douyin_upload_binding(submission, identity)
        if submission and submission['state'] in {'submitted', 'pending_review', 'published'}:
            if not upload_matches or not submission['item_id'] or submission['state'] == 'submitted':
                raise ValueError('content existing remote submission requires reconciliation; do not create another item')
            self._require_douyin_publish_identity(job_id, identity)
            return {
                "job_id": job_id,
                "platform": "douyin",
                "status": "submitted_for_review",
                "verification_status": submission["state"],
                "item_id": str(submission["item_id"] or ""),
                "video_id": "",
                "job_state": job["state"],
                "reused": True,
            }
        if job['state'] == 'PUBLISHED':
            raise ValueError('content published job requires reconciliation; no new upload is authorized')
        submission_id = (
            str(submission["submission_id"])
            if submission else
            "SUB-douyin-" + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]
        )
        upload_id = str(submission['upload_id']) if upload_matches else ''
        self.database.execute(
            "INSERT INTO platform_submissions("
            "submission_id,job_id,platform,state,title,cover_tsp,last_error"
            ") VALUES(?,?,?,'uploading',?,?, '') "
            "ON CONFLICT(job_id,platform) DO UPDATE SET state='uploading',"
            "title=excluded.title,cover_tsp=excluded.cover_tsp,last_error='',"
            "updated_at=CURRENT_TIMESTAMP",
            (
                submission_id, job_id, "douyin", text,
                float(cover_tsp) if cover_tsp not in (None, "") else None,
            ),
        )
        self.database.commit()
        client = douyin.DouyinClient()
        try:
            credentials = self._douyin_credentials(client)
            if not upload_id:
                self._require_douyin_publish_identity(job_id, identity)
                uploaded = client.upload_video(
                    access_token=credentials["access_token"],
                    open_id=credentials["account_id"],
                    path=final_path,
                )
                upload_id = str(uploaded["video_id"])
                with node_state_machine.atomic(self.database):
                    self._require_douyin_publish_identity(job_id, identity)
                    metadata_row = self.database.execute('SELECT metadata_json FROM platform_submissions WHERE submission_id=?', (submission_id,)).fetchone()
                    metadata = content_review.object_json(metadata_row['metadata_json'])
                    metadata['upload_binding'] = {'version': 1, 'authority': 'management_douyin_upload_v1',
                        'upload_id': upload_id, **identity}
                    self.database.execute(
                        "UPDATE platform_submissions SET upload_id=?,metadata_json=?,last_error='',"
                        "updated_at=CURRENT_TIMESTAMP WHERE submission_id=?",
                        (upload_id, json.dumps(metadata, ensure_ascii=False), submission_id),
                    )
            self._require_douyin_publish_identity(job_id, identity)
            created = client.create_video(
                access_token=credentials["access_token"],
                open_id=credentials["account_id"],
                video_id=upload_id,
                text=text,
                cover_tsp=float(cover_tsp) if cover_tsp not in (None, "") else None,
            )
            self.database.execute(
                "UPDATE platform_submissions SET state='submitted',item_id=?,"
                "last_error='',updated_at=CURRENT_TIMESTAMP WHERE submission_id=?",
                (str(created["item_id"]), submission_id),
            )
            self.database.commit()
            # A returned item is persisted before this check. If authorization
            # changed during create, retries must reconcile that known remote
            # item rather than silently create it a second time.
            self._require_douyin_publish_identity(job_id, identity)
        except Exception as exc:
            expired = bool(re.search(
                r"expired|invalid[^\n]{0,40}(?:access[_ -]?token|token)|"
                r"access[_ -]?token[^\n]{0,40}invalid|授权[^\n]{0,20}(?:失效|过期)",
                str(exc), re.I,
            ))
            public_error = (
                "抖音授权已失效，请重新绑定账号"
                if expired else str(exc)[:1000]
            )
            self.database.execute(
                "UPDATE platform_submissions SET last_error=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE submission_id=?",
                (public_error, submission_id),
            )
            if expired:
                self.database.execute(
                    "UPDATE platform_accounts SET status='rebind_required',"
                    "updated_at=CURRENT_TIMESTAMP WHERE platform='douyin'"
                )
            self.database.execute(
                "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
                (job_id, "publish_douyin", "error", public_error),
            )
            self.database.commit()
            if expired:
                raise ValueError(public_error) from None
            raise
        finally:
            client.close()
        saved = self.register_publications({
            "job_id": job_id,
            "channels": [{
                "platform": "douyin",
                "item_id": str(created["item_id"]),
                "title": text,
                "metadata": {
                    "review_status": "pending",
                    "uploaded_video_id": upload_id,
                    "public_video_id": str(created.get("video_id") or ""),
                    "publish_source": "openapi",
                },
            }],
        })
        self.database.execute(
            "UPDATE platform_submissions SET state='pending_review',last_error='',"
            "updated_at=CURRENT_TIMESTAMP WHERE submission_id=?",
            (submission_id,),
        )
        self.database.execute(
            "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
            (job_id, "publish_douyin", "info", "抖音投稿已提交，等待平台审核"),
        )
        self.database.commit()
        return {
            "job_id": job_id,
            "platform": "douyin",
            "status": "submitted_for_review",
            "verification_status": "pending_review",
            "item_id": str(created["item_id"]),
            "video_id": str(created.get("video_id") or ""),
            "job_state": saved["state"],
        }

    def register_publications(self, payload: dict) -> dict:
        with self._final_review_lock, node_state_machine.atomic(self.database):
            return self._register_publications_in_transaction(payload)

    def _register_publications_in_transaction(self, payload: dict) -> dict:
        job_id = str(payload.get("job_id") or "").strip()
        snapshot = self._publication_registration_snapshot(job_id)
        channels = payload.get("channels") or []
        if not job_id or not isinstance(channels, list) or not channels:
            raise ValueError("请选择待发布成片和至少一个渠道")
        normalized: list[dict] = []
        for channel in channels:
            if not isinstance(channel, dict):
                raise ValueError("发布渠道格式错误")
            platform = str(channel.get("platform") or "").strip()
            item_id = str(channel.get("item_id") or "").strip()
            if platform not in {"douyin", "weixin_channels"}:
                raise ValueError("发布渠道仅支持抖音或微信视频号")
            if not item_id:
                raise ValueError("平台作品 ID 不能为空")
            normalized.append({**channel, "platform": platform, "item_id": item_id})
        saved = []
        for channel in normalized:
            saved.append(self._register_publication_in_transaction({**channel, "job_id": job_id}))
        publication_rows = self.database.execute(
            "SELECT platform,metadata_json FROM platform_publications WHERE job_id=?",
            (job_id,),
        ).fetchall()
        published = {
            row["platform"] for row in publication_rows
            if not _publication_is_pending(row["metadata_json"])
        }
        target_state = (
            "PUBLISHED"
            if snapshot[0] == 'PUBLISHED' or {"douyin", "weixin_channels"}.issubset(published)
            else "READY_TO_PUBLISH"
        )
        self._require_publication_registration_snapshot(job_id, snapshot)
        changed = self.database.execute(
            "UPDATE jobs SET state=?,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND state=?",
            (target_state, job_id, snapshot[0]),
        )
        if changed.rowcount != 1:
            raise ValueError('content publication registration lost job-state compare-and-swap')
        self._require_publication_registration_snapshot(job_id, (target_state, snapshot[1]))
        return {"job_id": job_id, "saved": len(saved), "state": target_state}

    def record_performance_snapshot(self, publication_id: str, payload: dict) -> dict:
        publication = self.database.execute(
            "SELECT job_id,platform FROM platform_publications WHERE publication_id=?",
            (str(publication_id).strip(),),
        ).fetchone()
        if not publication:
            raise ValueError("发布记录不存在，无法录入绩效快照")
        fields = (
            "captured_at", "views", "completed_views", "completion_rate",
            "average_watch_sec", "likes", "comments", "shares", "saves",
            "follows", "leads",
        )
        values = {
            key: payload[key] for key in fields
            if payload.get(key) not in (None, "")
        }
        snapshot_id = performance_feedback.record_snapshot(
            self.database, publication["job_id"],
            platform=publication["platform"], **values,
        )
        snapshot = self.database.execute(
            "SELECT snapshot_id,captured_at,views,completed_views,completion_rate,"
            "average_watch_sec,likes,comments,shares,saves,follows,leads "
            "FROM performance_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        return {"snapshot_id": snapshot_id, "latest_snapshot": dict(snapshot)}
