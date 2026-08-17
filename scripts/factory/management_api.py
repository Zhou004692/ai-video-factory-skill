"""Application service for the local AI Video Factory management console."""
from __future__ import annotations

import asyncio
from collections import Counter
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
import time
from urllib.parse import urlparse

import httpx

from . import (
    character_assets, media_sources, performance_feedback, production_metrics, production_planner,
    scene_assets, script_drafts, script_sources, source_analysis, source_scheduler, topic_briefs,
)
from .clients import douyin, volc_assets
from .steps import s6_compose


CUSTOM_CHARACTER_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
CUSTOM_CHARACTER_IMAGE_MAX_BYTES = 20 * 1024 * 1024
CUSTOM_CHARACTER_IMAGE_MAX_COUNT = 12
WEIXIN_CHANNELS_ASSISTANT_URL = "https://channels.weixin.qq.com/login"


SECRET_KEYS = {
    "ANTHROPIC_API_KEY",
    "ARK_API_KEY",
    "VOLC_ASR_API_KEY",
    "VOLC_ASR_TOKEN",
    "VOLC_AK",
    "VOLC_SK",
    "SUNO_API_KEY",
    "DOUYIN_CLIENT_SECRET",
    "DOUYIN_ACCESS_TOKEN",
}

EDITABLE_CONFIG = {
    "DEFAULT_LLM_PROVIDER",
    "CLAUDE_MODEL",
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
    "VOLC_ASR_API_KEY",
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

    def __init__(self, database: sqlite3.Connection, root: Path):
        self.database = database
        self.root = Path(root).resolve()
        self.env_path = self.root / ".env"
        self._production_workers: dict[str, subprocess.Popen] = {}
        character_assets.ensure_schema(self.database)
        source_scheduler.ensure_schema(self.database)
        job_columns = {
            row[1] for row in self.database.execute("PRAGMA table_info(jobs)")
        }
        for column, ddl in (
            ("characters_json", "TEXT"),
            ("production_worker_pid", "INTEGER"),
            ("production_started_at", "TIMESTAMP"),
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
        self._repair_dangling_job_links()
        self._reconcile_orphaned_production_workers()
        self._clear_stale_success_errors()

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
            updated = self.database.execute(
                "UPDATE jobs SET state='PRODUCTION_QUEUED',"
                "production_worker_pid=NULL,updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=? AND state='PRODUCTION_RUNNING'",
                (row["job_id"],),
            )
            if updated.rowcount != 1:
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
        return {
            "supervisor_alive": bool(supervisor_alive),
            "concurrency_limit": self._production_limit(),
            "running": running,
            "queued": queued,
            "orphaned_recovered": int(recovered or 0),
            "last_error": str(error["message"]) if error else None,
        }

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
            "SELECT j.job_id,j.state,j.cost_cents FROM jobs j WHERE NOT ("
            "j.state='PENDING' AND NOT EXISTS (SELECT 1 FROM script_drafts d "
            "WHERE d.approved_job_id=j.job_id AND d.status='approved') "
            "AND NOT EXISTS (SELECT 1 FROM shots s WHERE s.job_id=j.job_id))"
        ).fetchall()
        job_ids = {str(row["job_id"]) for row in jobs}
        job_state_by_id = {
            str(row["job_id"]): str(row["state"]) for row in jobs
        }
        manual_review_job_ids: set[str] = set()
        for row in self.database.execute(
            "SELECT job_id,video_qa FROM shots WHERE video_qa IS NOT NULL"
        ).fetchall():
            job_id = str(row["job_id"])
            if job_state_by_id.get(job_id) not in {"FAILED", "BLOCKED"}:
                continue
            try:
                qa = json.loads(row["video_qa"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if qa.get("status") == "manual_review_required":
                manual_review_job_ids.add(job_id)
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
                "failed": max(0, failures_total - len(manual_review_job_ids)),
                "failures_total": failures_total,
                "cost_cents": total_cost_cents,
                "tracked_call_cost_cents": tracked_call_cost_cents,
                "unallocated_cost_cents": max(0, cost_delta_cents),
                "cost_delta_cents": cost_delta_cents,
                "reconciliation_status": (
                    "reconciled" if cost_delta_cents == 0 else "partial"
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
                    f"SELECT job_id,title FROM jobs WHERE job_id IN ({placeholders})",
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
        groups = [
            ("content_decision", "需内容决策", content_items),
            ("production_setup", "需制作设置", job_items(
                "STORYBOARDED", kind="production_setup", stage="STORYBOARDED"
            )),
            ("manual_review", "需人工复核", manual_review_items),
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
            raise ValueError("仅支持常见音视频格式")
        env = _read_env(self.env_path)
        api_key = str(env.get("VOLC_ASR_API_KEY") or "").strip()
        app_id = str(env.get("VOLC_ASR_APPID") or "").strip()
        access_token = str(env.get("VOLC_ASR_TOKEN") or "").strip()
        if not api_key and not (app_id and access_token):
            raise ValueError(
                "本地音视频转写需要先配置火山语音识别："
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
        reused_source = not result.created
        source_id = result.source_id
        if reused_source:
            instance = script_sources.instantiate_content_source(
                self.database, source_id, title=str(title or "").strip()
            )
            source_id = instance.source_id
        if suffix in media_sources.VIDEO_EXTENSIONS:
            self._load_runtime_env()
            source_analysis.analyze_source(self.database, source_id)
        response = {
            "source_id": source_id,
            "created": True,
            "source_type": (
                "video" if suffix in media_sources.VIDEO_EXTENSIONS else "audio"
            ),
        }
        if reused_source:
            response["reused_source"] = True
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
        self._load_runtime_env()
        report = source_analysis.analyze_source(
            self.database, source_id, force=force
        )
        return {"source_id": source_id, "analysis": report}

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
            "j.storyboard,j.created_at,j.updated_at FROM jobs j WHERE NOT ("
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
            planned_masters = list(
                storyboard.get("masters") or storyboard.get("shots") or []
            )
            job["director_plan"] = storyboard.get("director_plan") or {
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
            job["director_timeline"] = _director_timeline(storyboard)
            shots = _rows(self.database.execute(
                "SELECT idx,duration_sec,state,provider,cost_cents,video_path,"
                "video_qa,error "
                "FROM shots WHERE job_id=? ORDER BY idx", (job["job_id"],)
            ))
            for shot in shots:
                try:
                    qa = json.loads(shot.pop("video_qa") or "{}")
                except (json.JSONDecodeError, TypeError):
                    qa = {}
                shot["manual_review_required"] = (
                    qa.get("status") == "manual_review_required"
                )
                shot["quality_reasons"] = list(qa.get("reasons") or [])
                shot["quality_status"] = str(qa.get("status") or "")
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
                )
            job["completed_master_count"] = len(completed_indexes)
            job["remaining_master_count"] = max(
                0, (len(shots) or len(planned_masters)) - len(completed_indexes)
            )
            job["partial_production"] = bool(
                job["state"] == "STORYBOARDED"
                and completed_indexes
                and job["remaining_master_count"]
            )
            job["resumable"] = bool(
                job["state"] == "FAILED"
                and job["remaining_master_count"]
            )
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
                    "review_note,error "
                    "FROM character_identity_anchors WHERE job_id=?",
                    (job["job_id"],),
                ).fetchall()
            }
            job["character_makeup_plan"] = []
            for makeup in character_assets.character_makeup_plan(
                job["characters"]
            ):
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
                    preview = str(persisted.get("preview_path") or "")
                    if preview and Path(preview).is_file():
                        makeup["preview_url"] = (
                            f"/api/videos/{job['job_id']}/identities/"
                            f"{makeup['identity_token']}/preview"
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
                    "approved_at,review_note,error "
                    "FROM scene_visual_anchors WHERE job_id=?",
                    (job["job_id"],),
                ).fetchall()
            }
            scene_specs = {
                item["anchor_key"]: item
                for item in scene_assets.recurring_scene_anchors(storyboard)
            }
            job["scene_anchor_plan"] = []
            for anchor_key in dict.fromkeys([*scene_specs, *scene_rows]):
                planned = scene_specs.get(anchor_key, {})
                persisted = scene_rows.get(anchor_key, {})
                try:
                    master_indexes = json.loads(
                        persisted.get("master_indexes") or "[]"
                    )
                except json.JSONDecodeError:
                    master_indexes = []
                entry = {
                    "anchor_key": anchor_key,
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
                    "error": str(persisted.get("error") or ""),
                }
                preview = Path(str(persisted.get("preview_path") or ""))
                if preview.is_file():
                    entry["preview_url"] = (
                        f"/api/videos/{job['job_id']}/scenes/"
                        f"{entry['anchor_token']}/preview"
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

    def resume_failed_video(self, job_id: str) -> dict:
        job = self.database.execute(
            "SELECT state FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not job:
            raise ValueError("视频任务不存在")
        if job["state"] != "FAILED":
            raise ValueError("只有失败的视频任务可以继续生成")
        shots = self.database.execute(
            "SELECT idx,state,video_qa FROM shots WHERE job_id=? ORDER BY idx",
            (job_id,),
        ).fetchall()
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
            has_review_artifact = bool(
                str(qa.get("candidate_path") or "").strip()
                or str(qa.get("candidate_url") or "").strip()
            )
            if (
                qa.get("status") == "manual_review_required"
                and has_review_artifact
            ):
                review_indexes.append(int(row["idx"]))
            else:
                retry_indexes.append(int(row["idx"]))
        remaining = len(retry_indexes)
        if not remaining:
            if review_indexes:
                raise ValueError("该任务有母片等待人工复核，请先预览候选并处理")
            raise ValueError("该任务没有需要继续生成的母片")
        placeholders = ",".join("?" for _ in retry_indexes)
        self.database.execute(
            f"UPDATE shots SET state='PENDING',error=NULL,video_qa=NULL "
            f"WHERE job_id=? AND idx IN ({placeholders})",
            (job_id, *retry_indexes),
        )
        self.database.execute(
            "UPDATE jobs SET state='STORYBOARDED',retries=0,error=NULL,"
            "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND state='FAILED'",
            (job_id,),
        )
        self.database.commit()
        return {
            "job_id": job_id,
            "state": "STORYBOARDED",
            "preserved_masters": completed,
            "remaining_masters": remaining,
            "manual_review_masters": len(review_indexes),
        }

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
            "UPDATE shots SET state='PENDING',prompt=?,provider=NULL,"
            "video_model=NULL,native_audio=0,video_qa=NULL,error=NULL "
            "WHERE job_id=? AND idx=?",
            (master_prompt, job_id, int(master_idx)),
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

    def configure_video(self, job_id: str, settings: dict) -> dict:
        row = self.database.execute(
            "SELECT state,storyboard FROM jobs WHERE job_id=?", (job_id,),
        ).fetchone()
        if not row:
            raise ValueError("视频任务不存在")
        if row["state"] != "STORYBOARDED":
            raise ValueError("只有待生成任务可以修改制作设置")
        try:
            storyboard = json.loads(row["storyboard"] or "{}")
        except json.JSONDecodeError:
            storyboard = {}
        speakers = list(dict.fromkeys(
            str(master.get("speaker") or "").strip()
            for master in (storyboard.get("masters") or [])
            if master.get("type") == "dialogue" and str(master.get("speaker") or "").strip()
        ))
        if not speakers:
            speakers = [
                str(item.get("id") or "").strip()
                for item in (storyboard.get("characters") or [])
                if str(item.get("id") or "").strip()
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
                runtime_characters.append({
                    **script_character,
                    "id": speaker,
                    "name": str(script_character.get("name") or speaker),
                    "script_character_name": str(
                        script_character.get("name") or speaker
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
            "budget_cents": budget,
        }
        storyboard["production_profile"] = normalized["production_profile"]
        storyboard["production_settings"] = normalized
        if runtime_characters:
            storyboard["characters"] = runtime_characters
        scene_assets.apply_continuity_metadata(
            storyboard, runtime_characters
        )
        generation_units = storyboard.get("masters") or storyboard.get("shots") or []
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
        return {"job_id": job_id, "production_settings": normalized}

    def review_video(
        self, job_id: str, *, decision: str, manual_minutes: float = 0,
        notes: str = "", first_pass: bool | None = None,
        checks: dict[str, bool] | None = None,
    ) -> dict:
        row = self.database.execute(
            "SELECT state,final_path FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not row:
            raise ValueError("视频任务不存在")
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
                    "UPDATE shots SET video_qa=? WHERE job_id=? AND idx=?",
                    (json.dumps(qa, ensure_ascii=False), job_id, shot["idx"]),
                )
        production_metrics.record_review(
            self.database, job_id, str(decision or "").strip(),
            manual_minutes=float(manual_minutes or 0),
            notes=str(notes or "").strip() or None,
            first_pass=first_pass,
            checks=normalized_checks,
        )
        return {
            "job_id": job_id, "decision": decision,
            "publish_unlocked": decision == "approved",
        }

    def delete_video(self, job_id: str) -> dict:
        row = self.database.execute(
            "SELECT state FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not row:
            raise ValueError("视频任务不存在")
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
        return [
            str(python), str(self.root / "scripts" / "cli.py"),
            "produce-approved", job_id, "--yes", "--dispatched",
        ]

    def dispatch_production_queue(self) -> dict:
        """Atomically claim queued jobs up to the local concurrency limit."""
        exited_jobs: list[str] = []
        for job_id, worker in list(self._production_workers.items()):
            returncode = worker.poll()
            if returncode is None or not isinstance(returncode, int):
                continue
            self._production_workers.pop(job_id, None)
            updated = self.database.execute(
                "UPDATE jobs SET state='FAILED',error=?,"
                "production_worker_pid=NULL,"
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=? AND state='PRODUCTION_RUNNING'",
                (f"production worker exited before completion (code {returncode})", job_id),
            )
            self.database.commit()
            if updated.rowcount == 1:
                exited_jobs.append(job_id)
            else:
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
            claimed = self.database.execute(
                "UPDATE jobs SET state='PRODUCTION_RUNNING',error=NULL,"
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=? AND state='PRODUCTION_QUEUED'", (job_id,),
            )
            self.database.commit()
            if claimed.rowcount != 1:
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
                self.database.execute(
                    "UPDATE jobs SET state='PRODUCTION_QUEUED',"
                    "production_worker_pid=NULL,"
                    "updated_at=CURRENT_TIMESTAMP "
                    "WHERE job_id=? AND state='PRODUCTION_RUNNING'", (job_id,),
                )
                self.database.commit()
                raise
            dispatched.append(job_id)
        return {"dispatched_jobs": dispatched, "failed_workers": exited_jobs}

    def submit_for_publish(self, job_id: str) -> dict:
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
        self.database.commit()
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
        path = Path(raw_path).resolve()
        allowed_root = (
            self.root / "data" / "assets" / str(job_id)
        ).resolve()
        if not path.is_relative_to(allowed_root) or not path.is_file():
            raise ValueError("待复核候选母片不存在")
        return path

    def review_master_candidate(
        self, job_id: str, master_idx: int, *, decision: str
    ) -> dict:
        normalized = str(decision or "").strip()
        if normalized not in {"keep_prior", "accept_candidate"}:
            raise ValueError("请选择保留旧版或接受候选母片")
        job = self.database.execute(
            "SELECT state,final_path FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        shot = self.database.execute(
            "SELECT state,video_path,video_url,video_qa FROM shots "
            "WHERE job_id=? AND idx=?",
            (job_id, int(master_idx)),
        ).fetchone()
        if not job or not shot:
            raise ValueError("待复核候选母片不存在")
        if job["state"] != "FAILED" or shot["state"] != "FAILED":
            raise ValueError("当前母片不在人工复核状态")
        candidate = self.master_candidate_file(job_id, master_idx)
        prior_value = str(shot["video_path"] or "").strip()
        prior = Path(prior_value).resolve() if prior_value else None
        has_prior = bool(prior and prior.is_file())
        try:
            qa = json.loads(shot["video_qa"] or "{}")
        except (json.JSONDecodeError, TypeError):
            qa = {}
        reviewed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if normalized == "accept_candidate":
            candidate_url = str(qa.get("candidate_url") or "").strip() or None
            active = (
                prior if has_prior else
                candidate.parent.parent / f"{int(master_idx):02d}.mp4"
            )
            active.parent.mkdir(parents=True, exist_ok=True)
            prior_backup = None
            if has_prior:
                prior_backup = active.with_name(
                    f".{active.stem}.prior-review{active.suffix}"
                )
                prior_backup.unlink(missing_ok=True)
                active.replace(prior_backup)
                shutil.copy2(candidate, active)
            else:
                candidate.replace(active)
                candidate_audio = candidate.with_suffix(".wav")
                if candidate_audio.is_file():
                    candidate_audio.replace(active.with_suffix(".wav"))
            qa.update({
                "status": (
                    "candidate_accepted_pending" if has_prior
                    else "candidate_accepted"
                ),
                "human_decision": "accept_candidate",
                "reviewed_at": reviewed_at,
                "prior_backup_path": (
                    str(prior_backup) if prior_backup else None
                ),
                "prior_video_url": shot["video_url"],
                "candidate_path": (
                    None if not has_prior else qa.get("candidate_path")
                ),
            })
            self.database.execute(
                "UPDATE shots SET state='OK',video_path=?,video_url=?,"
                "video_qa=?,error=NULL WHERE job_id=? AND idx=?",
                (
                    str(active), candidate_url,
                    json.dumps(qa, ensure_ascii=False),
                    job_id, int(master_idx),
                ),
            )
        else:
            if not has_prior:
                candidate.unlink(missing_ok=True)
                candidate.with_suffix(".wav").unlink(missing_ok=True)
                qa.update({
                    "status": "candidate_rejected",
                    "candidate_path": None,
                    "candidate_url": None,
                    "human_decision": "keep_prior",
                    "reviewed_at": reviewed_at,
                })
                self.database.execute(
                    "UPDATE shots SET state='FAILED',video_qa=?,error=? "
                    "WHERE job_id=? AND idx=?",
                    (
                        json.dumps(qa, ensure_ascii=False),
                        "候选已拒绝，请继续生成该母片",
                        job_id, int(master_idx),
                    ),
                )
            else:
                candidate.unlink(missing_ok=True)
                candidate.with_suffix(".wav").unlink(missing_ok=True)
                qa.update({
                    "status": "candidate_rejected",
                    "candidate_path": None,
                    "candidate_url": None,
                    "human_decision": "keep_prior",
                    "reviewed_at": reviewed_at,
                })
                self.database.execute(
                    "UPDATE shots SET state='OK',video_qa=?,error=NULL "
                    "WHERE job_id=? AND idx=?",
                    (json.dumps(qa, ensure_ascii=False), job_id, int(master_idx)),
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
            if (
                review_row["state"] == "FAILED"
                and review_qa.get("status") == "manual_review_required"
            ):
                remaining_review_count += 1
            if review_qa.get("status") == "candidate_accepted_pending":
                pending_accepts.append((review_row, review_qa))

        decision_label = (
            "已接受候选" if normalized == "accept_candidate" else "已保留旧版"
        )
        if remaining_review_count:
            waiting_message = f"仍有 {remaining_review_count} 条母片待人工复核"
            self.database.execute(
                "UPDATE jobs SET state='FAILED',error=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
                (waiting_message, job_id),
            )
            self.database.execute(
                "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
                (job_id, "master_candidate_review", "info",
                 f"母片 #{int(master_idx)} {decision_label}，{waiting_message}"),
            )
            self.database.commit()
            return {
                "job_id": job_id,
                "master_idx": int(master_idx),
                "decision": normalized,
                "state": "FAILED",
                "remaining_review_count": remaining_review_count,
            }

        remaining_generation_count = int(self.database.execute(
            "SELECT COUNT(*) FROM shots WHERE job_id=? AND state!='OK'",
            (job_id,),
        ).fetchone()[0])
        if remaining_generation_count:
            waiting_message = (
                f"仍有 {remaining_generation_count} 条母片需要继续生成"
            )
            self.database.execute(
                "UPDATE jobs SET state='FAILED',error=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
                (waiting_message, job_id),
            )
            self.database.execute(
                "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
                (
                    job_id, "master_candidate_review", "info",
                    f"母片 #{int(master_idx)} {decision_label}，{waiting_message}",
                ),
            )
            self.database.commit()
            return {
                "job_id": job_id,
                "master_idx": int(master_idx),
                "decision": normalized,
                "state": "FAILED",
                "remaining_review_count": 0,
                "remaining_generation_count": remaining_generation_count,
            }

        final = Path(str(job["final_path"] or "")).resolve()
        final_backup = final.with_name(
            f".{final.stem}.prior-review{final.suffix}"
        )
        if pending_accepts:
            final_backup.unlink(missing_ok=True)
            if final.is_file():
                final.replace(final_backup)
            self.database.execute(
                "UPDATE jobs SET state='BGM_OK',error=NULL,"
                "updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
                (job_id,),
            )
            self.database.commit()
            try:
                asyncio.run(s6_compose.run(job_id))
            except Exception as exc:
                final.unlink(missing_ok=True)
                if final_backup.exists():
                    final_backup.replace(final)
                for pending_row, pending_qa in pending_accepts:
                    pending_prior = Path(str(
                        pending_qa.get("prior_backup_path") or ""
                    )).resolve()
                    active_prior = Path(str(
                        self.database.execute(
                            "SELECT video_path FROM shots WHERE job_id=? AND idx=?",
                            (job_id, int(pending_row["idx"])),
                        ).fetchone()["video_path"]
                    )).resolve()
                    active_prior.unlink(missing_ok=True)
                    if pending_prior.exists():
                        pending_prior.replace(active_prior)
                    rolled_back_qa = {
                        key: value for key, value in pending_qa.items()
                        if key not in {
                            "human_decision", "reviewed_at",
                            "prior_backup_path", "prior_video_url",
                        }
                    }
                    rolled_back_qa["status"] = "manual_review_required"
                    self.database.execute(
                        "UPDATE shots SET state='FAILED',video_url=?,video_qa=?,"
                        "error=? WHERE job_id=? AND idx=?",
                        (
                            pending_qa.get("prior_video_url"),
                            json.dumps(rolled_back_qa, ensure_ascii=False),
                            f"local recompose failed: {exc}",
                            job_id, int(pending_row["idx"]),
                        ),
                    )
                self.database.execute(
                    "UPDATE jobs SET state='FAILED',error=?,"
                    "updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
                    (f"local recompose failed: {exc}", job_id),
                )
                self.database.commit()
                raise RuntimeError(f"本地重新合成失败：{exc}") from exc

            for pending_row, pending_qa in pending_accepts:
                Path(str(pending_qa.get("prior_backup_path") or "")).unlink(
                    missing_ok=True
                )
                pending_candidate = Path(str(
                    pending_qa.get("candidate_path") or ""
                ))
                pending_candidate.unlink(missing_ok=True)
                pending_candidate.with_suffix(".wav").unlink(missing_ok=True)
                finalized_qa = {
                    key: value for key, value in pending_qa.items()
                    if key not in {"prior_backup_path", "prior_video_url"}
                }
                finalized_qa.update({
                    "status": "candidate_accepted",
                    "candidate_path": None,
                })
                self.database.execute(
                    "UPDATE shots SET video_qa=? WHERE job_id=? AND idx=?",
                    (
                        json.dumps(finalized_qa, ensure_ascii=False),
                        job_id, int(pending_row["idx"]),
                    ),
                )
            final_backup.unlink(missing_ok=True)

        self.database.execute(
            "UPDATE jobs SET state='COMPOSED',error=NULL,retries=0,"
            "updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
            (job_id,),
        )
        event_message = (
            f"母片 #{int(master_idx)} 已完成复核并完成本地重合成"
            if pending_accepts
            else f"母片 #{int(master_idx)} 已保留旧版"
        )
        self.database.execute(
            "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
            (job_id, "master_candidate_review", "info", event_message),
        )
        self.database.commit()
        return {
            "job_id": job_id,
            "master_idx": int(master_idx),
            "decision": normalized,
            "state": "COMPOSED",
            "remaining_review_count": 0,
        }

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
        groups = {
            "script": ("ARK_API_KEY",),
            "video": ("ARK_API_KEY", "VOLC_AK", "VOLC_SK", "TOS_BUCKET"),
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
        if not asr_ready:
            readiness["video"]["ready"] = False
            readiness["video"]["missing"].append("VOLC_ASR_API_KEY")
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
            fields.append(field)
        fields_by_key = {field["key"]: field for field in fields}
        required_groups = []
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
            missing = (
                [] if ready
                else [field["key"] for field in group_fields if not field["configured"]]
            )
            required_groups.append({
                **group,
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

    def update_config(self, changes: dict) -> dict:
        current = _read_env(self.env_path)
        updated: list[str] = []
        for key, raw_value in changes.items():
            if key not in EDITABLE_CONFIG:
                continue
            value = str(raw_value or "").strip().replace("\r", "").replace("\n", "")
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
            binding.update({
                "preview_url": self._local_character_preview_url(binding["character_id"]),
                "images": images,
                "image_count": len(images),
                "usage_count": len(usages),
                "usages": usages,
                "deletable": not usages,
            })
            result.append(binding)
        return {"characters": result, "total": len(result)}

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
        return [(self.root / row["relative_path"]).resolve() for row in rows]

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
            raise ValueError("每个自定义角色最多上传 12 张图片")
        digest = hashlib.sha256(
            character_id.encode("utf-8") + source.read_bytes()
        ).hexdigest()[:16]
        image_id = f"IMG-{digest}"
        relative = Path("data") / "character_assets" / character_id / f"{image_id}{suffix}"
        target = self.root / relative
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
        path = (self.root / row["relative_path"]).resolve()
        asset_root = (self.root / "data" / "character_assets").resolve()
        if asset_root not in path.parents or not path.is_file():
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
        preview_root = self.root / "data" / "character_previews"
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
        preview_root = self.root / "data" / "character_previews"
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
        custom_root = self.root / "data" / "character_assets" / character_id
        if custom_root.exists():
            shutil.rmtree(custom_root)
        return {"character_id": character_id, "deleted": True}

    def start_video_job(self, job_id: str, *, confirmed: bool) -> dict:
        if not confirmed:
            raise ValueError("开始付费生成前必须明确确认预算")
        job = self.database.execute(
            "SELECT state FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not job:
            raise ValueError("视频任务不存在")
        if job["state"] in {"PRODUCTION_QUEUED", "PRODUCTION_RUNNING"}:
            raise ValueError("视频生成已经启动，请勿重复提交")
        if job["state"] != "STORYBOARDED":
            raise ValueError("只有已确认剧本的任务才能开始生成")
        storyboard_row = self.database.execute(
            "SELECT storyboard,cost_cents FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        try:
            storyboard = json.loads(storyboard_row["storyboard"] or "{}")
        except json.JSONDecodeError:
            storyboard = {}
        dialogue_speakers = list(dict.fromkeys(
            str(master.get("speaker") or "").strip()
            for master in (storyboard.get("masters") or [])
            if master.get("type") == "dialogue"
            and str(master.get("speaker") or "").strip()
        ))
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
        if not preflight["budget_ok"]:
            raise ValueError(
                "预计累计成本超过已确认的单任务预算上限；"
                "请返回制作设置提高预算后再开始生成"
            )
        claimed = self.database.execute(
            "UPDATE jobs SET state='PRODUCTION_QUEUED',updated_at=CURRENT_TIMESTAMP "
            "WHERE job_id=? AND state='STORYBOARDED'", (job_id,),
        )
        self.database.commit()
        if claimed.rowcount != 1:
            raise ValueError("视频生成已经启动，请勿重复提交")
        dispatch = self.dispatch_production_queue()
        status = "started" if job_id in dispatch["dispatched_jobs"] else "queued"
        return {"job_id": job_id, "status": status}

    def approve_continuity_anchors(
        self, job_id: str, *, note: str = ""
    ) -> dict:
        """Approve canonical makeup/location anchors and resume production."""
        job = self.database.execute(
            "SELECT state FROM jobs WHERE job_id=?", (job_id,)
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
        if any(str(row["state"]) != "READY" for row in review_rows):
            raise ValueError("人物与场景锚点尚未全部生成完成")

        review_note = str(note or "").strip()
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
        required = ("job_id", "platform", "item_id")
        missing = [key for key in required if not str(payload.get(key) or "").strip()]
        if missing:
            raise ValueError("缺少发布字段：" + ", ".join(missing))
        job_id = str(payload["job_id"]).strip()
        platform = str(payload["platform"]).strip()
        if platform not in {"douyin", "weixin_channels"}:
            raise ValueError("发布渠道仅支持抖音或微信视频号")
        job = self.database.execute(
            "SELECT state FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not job:
            raise ValueError("视频任务不存在")
        if job["state"] not in {"READY_TO_PUBLISH", "PUBLISHED"}:
            raise ValueError("只有待发布成片才能登记渠道发布结果")
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
        self.database.commit()
        return {"publication_id": publication_id, "status": "saved"}

    def publish_to_douyin(self, job_id: str, payload: dict) -> dict:
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
        final_path = self.video_file(job_id)
        cover_tsp = payload.get("cover_tsp")
        submission = self.database.execute(
            "SELECT * FROM platform_submissions WHERE job_id=? AND platform='douyin'",
            (job_id,),
        ).fetchone()
        if submission and submission["state"] in {"pending_review", "published"}:
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
        submission_id = (
            str(submission["submission_id"])
            if submission else
            "SUB-douyin-" + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]
        )
        upload_id = str(submission["upload_id"] or "") if submission else ""
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
                uploaded = client.upload_video(
                    access_token=credentials["access_token"],
                    open_id=credentials["account_id"],
                    path=final_path,
                )
                upload_id = str(uploaded["video_id"])
                self.database.execute(
                    "UPDATE platform_submissions SET upload_id=?,last_error='',"
                    "updated_at=CURRENT_TIMESTAMP WHERE submission_id=?",
                    (upload_id, submission_id),
                )
                self.database.commit()
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
        job_id = str(payload.get("job_id") or "").strip()
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
            saved.append(self.register_publication({**channel, "job_id": job_id}))
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
            if {"douyin", "weixin_channels"}.issubset(published)
            else "READY_TO_PUBLISH"
        )
        self.database.execute(
            "UPDATE jobs SET state=?,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
            (target_state, job_id),
        )
        self.database.commit()
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
