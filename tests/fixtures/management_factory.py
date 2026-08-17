"""Deterministic factory for management-console end-to-end tests."""
from __future__ import annotations

import json
import os
import sqlite3


FIXTURE_IDS = {
    "source": "SRC-E2E-SOURCE",
    "brief": "SRC-E2E-BRIEF",
    "script": "SRC-E2E-SCRIPT",
    "setup": "SRC-E2E-SETUP",
    "running": "SRC-E2E-RUNNING",
    "review": "SRC-E2E-REVIEW",
}


def seed_management_factory(database: sqlite3.Connection) -> dict[str, str]:
    """Seed the six user-facing workflow stages into an isolated database."""
    source_paragraph = (
        "设备租赁企业真正需要解决的不是单次获客，而是设备周转、合同履约、"
        "回款节奏和客户信任之间的协同。内容必须保留事实、明确受众，并给出"
        "能够被验证的行动建议，才能成为可靠的短视频选题素材。"
    )
    source_content = "\n\n".join(source_paragraph for _ in range(12))
    source_rows = []
    for index, (key, source_id) in enumerate(FIXTURE_IDS.items(), start=1):
        source_rows.append(
            (
                source_id,
                f"fixture-{key}",
                "manual",
                f"E2E {key} 阶段内容",
                f"manual://e2e/{key}",
                source_content,
                json.dumps({"fixture_stage": key}, ensure_ascii=False),
                f"2026-08-15 09:{index:02d}:00",
            )
        )
    database.executemany(
        "INSERT INTO script_sources("
        "source_id,fingerprint,source_type,title,source_uri,content,metadata_json,created_at"
        ") VALUES(?,?,?,?,?,?,?,?)",
        source_rows,
    )
    database.execute(
        "INSERT INTO source_feeds("
        "feed_id,name,feed_type,endpoint,source_kind,enabled,last_collected_at"
        ") VALUES(?,?,?,?,?,?,?)",
        (
            "FEED-E2E-HOTSPOT",
            "E2E 行业公开热点",
            "rss",
            "https://example.com/e2e-feed.xml",
            "hotspot",
            1,
            "2026-08-15 08:30:00",
        ),
    )
    database.execute(
        "INSERT INTO source_candidates("
        "candidate_id,feed_id,external_id,title,source_url,excerpt,published_at,status,raw_json"
        ") VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "CAND-E2E-HOTSPOT",
            "FEED-E2E-HOTSPOT",
            "e2e-hotspot-1",
            "设备更新政策释放租赁行业新机会",
            "https://example.com/news/e2e-hotspot-1",
            "公开政策提出设备更新支持方向，适合进入人工选题判断。",
            "2026-08-15T08:00:00Z",
            "candidate",
            json.dumps({"fixture": True}, ensure_ascii=False),
        ),
    )

    brief_by_stage = {
        key: f"BRIEF-E2E-{key.upper()}"
        for key in ("brief", "script", "setup", "running", "review")
    }
    job_by_stage = {
        "setup": "J-E2E-SETUP",
        "running": "J-E2E-RUNNING",
        "review": "J-E2E-REVIEW",
    }
    for index, (key, brief_id) in enumerate(brief_by_stage.items(), start=1):
        approved_job_id = job_by_stage.get(key)
        brief_payload = {
            "audience": "设备租赁企业经营者",
            "recommended_index": 0,
            "angles": [
                {"title": f"{key} 经营误区", "hook": "多数人先看错了指标"},
                {"title": f"{key} 周转效率", "hook": "设备闲置正在吞掉利润"},
                {"title": f"{key} 回款方法", "hook": "现金流不是财务部门的独角戏"},
            ],
        }
        database.execute(
            "INSERT INTO topic_briefs("
            "brief_id,request_fingerprint,status,recommended_title,brief_json,"
            "approved_job_id,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                brief_id,
                f"brief-request-{key}",
                "approved" if approved_job_id else "candidate",
                f"E2E {key} 阶段选题",
                json.dumps(brief_payload, ensure_ascii=False),
                approved_job_id,
                f"2026-08-15 10:{index:02d}:00",
                f"2026-08-15 10:{index:02d}:00",
            ),
        )
        database.execute(
            "INSERT INTO topic_brief_sources(brief_id,source_id) VALUES(?,?)",
            (brief_id, FIXTURE_IDS[key]),
        )

    def script_payload(stage: str, variant_index: int) -> dict:
        return {
            "title": f"E2E {stage} 剧本 {variant_index + 1}",
            "total_duration_sec": 30,
            "beats": [
                {
                    "start_sec": 0,
                    "end_sec": 10,
                    "speaker": "经营者",
                    "dialogue": "设备租赁最怕的不是没订单，而是设备和现金流一起闲置",
                    "visual_intent": "仓库广角推进，人物边走边检查设备",
                },
                {
                    "start_sec": 10,
                    "end_sec": 20,
                    "speaker": "经营者",
                    "dialogue": "先盯周转天数，再看合同履约和回款节点",
                    "visual_intent": "切换到调度屏和合同特写，镜头横移",
                },
                {
                    "start_sec": 20,
                    "end_sec": 30,
                    "speaker": "经营者",
                    "dialogue": "把每台设备变成可追踪的经营单元，利润才会回来",
                    "visual_intent": "设备出库，人物转身面对镜头收束观点",
                },
            ],
        }

    script_brief_id = brief_by_stage["script"]
    for variant_index in range(3):
        database.execute(
            "INSERT INTO script_drafts("
            "draft_id,brief_id,variant_index,status,request_fingerprint,script_json,"
            "recommended_index,recommendation_reason,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                f"SCRIPT-E2E-CANDIDATE-{variant_index + 1}",
                script_brief_id,
                variant_index,
                "candidate",
                "script-request-candidate",
                json.dumps(script_payload("script", variant_index), ensure_ascii=False),
                0,
                "固定测试候选",
                f"2026-08-15 11:0{variant_index}:00",
                f"2026-08-15 11:0{variant_index}:00",
            ),
        )

    job_states = {
        "setup": "STORYBOARDED",
        "running": "PRODUCTION_RUNNING",
        "review": "COMPOSED",
    }
    storyboard = {
        "title": "E2E 确定性母片",
        "total_duration_sec": 30,
        "shots": [
            {
                "idx": 1,
                "duration_sec": 30,
                "speaker": "经营者",
                "dialogue": "用确定性数据验证本地视频流水线",
                "visual_intent": "仓库场景中的移动跟拍与设备特写",
            }
        ],
    }
    for index, (key, state) in enumerate(job_states.items(), start=1):
        job_id = job_by_stage[key]
        brief_id = brief_by_stage[key]
        draft_id = f"SCRIPT-E2E-{key.upper()}"
        database.execute(
            "INSERT INTO jobs("
            "job_id,topic_id,title,state,storyboard,cost_cents,production_worker_pid,"
            "production_started_at,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                f"TOPIC-E2E-{key.upper()}",
                f"E2E {key} 视频任务",
                state,
                json.dumps(storyboard, ensure_ascii=False),
                0,
                os.getpid() if key == "running" else None,
                "2026-08-15 12:00:00" if key == "running" else None,
                f"2026-08-15 12:0{index}:00",
                f"2026-08-15 12:0{index}:00",
            ),
        )
        database.execute(
            "INSERT INTO script_drafts("
            "draft_id,brief_id,variant_index,status,request_fingerprint,script_json,"
            "recommended_index,recommendation_reason,approved_job_id,storyboard_json,"
            "created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                draft_id,
                brief_id,
                0,
                "approved",
                f"script-request-{key}",
                json.dumps(script_payload(key, 0), ensure_ascii=False),
                0,
                "固定测试已审核剧本",
                job_id,
                json.dumps(storyboard, ensure_ascii=False),
                f"2026-08-15 11:1{index}:00",
                f"2026-08-15 11:1{index}:00",
            ),
        )

    database.commit()
    return dict(FIXTURE_IDS)


def seed_management_scale(
    database: sqlite3.Connection,
    *,
    sources: int = 50,
    briefs: int = 20,
    drafts: int = 20,
    jobs: int = 30,
) -> None:
    """Expand an existing deterministic fixture to the first operating scale."""
    current_sources = database.execute(
        "SELECT COUNT(*) FROM script_sources"
    ).fetchone()[0]
    for index in range(current_sources, sources):
        database.execute(
            "INSERT INTO script_sources(source_id,fingerprint,source_type,title,"
            "source_uri,content,metadata_json,created_at) VALUES(?,?, 'manual', ?, ?, ?, '{}', ?)",
            (
                f"SRC-E2E-SCALE-{index:03d}",
                f"fixture-scale-{index:03d}",
                f"规模来源 {index + 1:02d}",
                f"manual://e2e/scale/{index:03d}",
                "用于验证大量内容来源下的列表、筛选、滚动和详情入口。" * 8,
                f"2026-08-15 13:{index % 60:02d}:00",
            ),
        )

    current_briefs = database.execute(
        "SELECT COUNT(*) FROM topic_briefs"
    ).fetchone()[0]
    scale_brief_ids: list[str] = []
    for index in range(current_briefs, briefs):
        brief_id = f"BRIEF-E2E-SCALE-{index:03d}"
        scale_brief_ids.append(brief_id)
        database.execute(
            "INSERT INTO topic_briefs(brief_id,request_fingerprint,status,"
            "recommended_title,brief_json,created_at,updated_at) "
            "VALUES(?,?, 'candidate', ?, ?, ?, ?)",
            (
                brief_id,
                f"brief-scale-{index:03d}",
                f"规模简报 {index + 1:02d}",
                json.dumps({
                    "audience": "设备租赁经营者",
                    "angles": [{"title": "规模验收", "hook": "列表再长也能继续工作"}],
                }, ensure_ascii=False),
                f"2026-08-15 14:{index % 60:02d}:00",
                f"2026-08-15 14:{index % 60:02d}:00",
            ),
        )

    all_briefs = [row[0] for row in database.execute(
        "SELECT brief_id FROM topic_briefs ORDER BY brief_id"
    ).fetchall()]
    current_drafts = database.execute(
        "SELECT COUNT(*) FROM script_drafts"
    ).fetchone()[0]
    for index in range(current_drafts, drafts):
        database.execute(
            "INSERT INTO script_drafts(draft_id,brief_id,variant_index,status,"
            "request_fingerprint,script_json,created_at,updated_at) "
            "VALUES(?,?,0,'candidate',?,?,?,?)",
            (
                f"SCRIPT-E2E-SCALE-{index:03d}",
                all_briefs[index % len(all_briefs)],
                f"script-scale-{index:03d}",
                json.dumps({
                    "title": f"规模剧本 {index + 1:02d}",
                    "beats": [{"dialogue": "规模增加后仍然可以清楚审核每一条剧本。"}],
                }, ensure_ascii=False),
                f"2026-08-15 15:{index % 60:02d}:00",
                f"2026-08-15 15:{index % 60:02d}:00",
            ),
        )

    current_jobs = database.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    for index in range(current_jobs, jobs):
        database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,created_at,updated_at) "
            "VALUES(?,?,?,'STORYBOARDED','{}',?,?)",
            (
                f"J-E2E-SCALE-{index:03d}",
                f"TOPIC-E2E-SCALE-{index:03d}",
                f"规模视频 {index + 1:02d}",
                f"2026-08-15 16:{index % 60:02d}:00",
                f"2026-08-15 16:{index % 60:02d}:00",
            ),
        )
    database.commit()
