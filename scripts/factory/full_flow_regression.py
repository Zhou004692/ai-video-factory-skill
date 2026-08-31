"""Deterministic zero-cost regression through the management-service API.

This module deliberately exercises the same service methods as the management
console.  Only provider responses are replayed; content validation, database
state, script approval, storyboard creation and Production Graph V2 compilation
remain the real application code.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from typing import Mapping

from . import (
    character_assets,
    production_adapters,
    production_executor,
    production_graph_runner,
    provider_replay,
    script_drafts,
    topic_briefs,
)
from .clients import doubao_llm


def _write_replay(
    root: Path,
    *,
    fixture_id: str,
    operation: str,
    request: Mapping[str, object],
    result: dict,
) -> Path:
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "fixture_schema_version": provider_replay.FIXTURE_SCHEMA_VERSION,
        "fixture_id": fixture_id,
        "operations": [{
            "operation": operation,
            "request_hash": provider_replay.fingerprint(operation, request),
            "responses": [{
                "status": "succeeded",
                "result": result,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }],
        }],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return root


def _source_record(database, source_id: str) -> dict:
    row = database.execute(
        "SELECT source_id,fingerprint,title,source_uri,content,metadata_json "
        "FROM script_sources WHERE source_id=?",
        (source_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"script source not found: {source_id}")
    metadata = json.loads(row["metadata_json"] or "{}")
    return {
        "source_id": row["source_id"],
        "fingerprint": row["fingerprint"],
        "title": row["title"],
        "source_uri": row["source_uri"],
        "content": row["content"],
        "structure_analysis": metadata.get("structure_analysis") or {},
    }


def _brief_result(source: dict) -> dict:
    source_id = str(source["source_id"])
    quote = str(source["content"]).split("。", 1)[0].strip() or str(
        source["content"]
    )[:120]
    fact = {
        "fact_id": "F1",
        "statement": quote,
        "quote": quote,
        "source_id": source_id,
        "importance": "must_keep",
    }
    titles = (
        "三人跨场景协同如何让高空车快速复工",
        "从告警到复工：高空车双保险调度",
        "一条故障证据链如何减少现场停工",
    )
    arcs = (
        "告警出现→三人分工→跨场景核验→现场复工",
        "故障风险→备用设备复检→双保险调度→兑现承诺",
        "现场异常→证据回传→联合判断→闭环交付",
    )
    angles = []
    for title, story_arc in zip(titles, arcs):
        angles.append({
            "title": title,
            "hook": "同一台高空车故障，为什么需要三个人在三个地点同时行动？",
            "promise": "看懂调度、复检和现场处置怎样组成可验证的复工闭环",
            "story_arc": story_arc,
            "evidence": [quote],
            "fact_ids": ["F1"],
            "risk_notes": ["不得扩写来源没有提供的客户、金额或时效承诺"],
            "production_notes": "三处空间分别建立连续性锚点，以人物动作衔接场景切换",
            "creative_tags": {
                "hook_type": "pain_point",
                "narrative_pattern": "problem_solution",
                "proof_type": "source_quote",
                "cta_type": "consult",
            },
        })
    return {
        "audience": "设备租赁经营者与施工项目负责人",
        "source_score": {
            "relevance": 95,
            "story_potential": 92,
            "evidence_quality": 90,
            "production_fit": 94,
            "reason": "来源包含人物、地点、冲突、证据与完成状态",
        },
        "source_digest": {"facts": [fact]},
        "angles": angles,
        "recommended_index": 0,
        "recommendation_reason": "三人跨场景结构覆盖人物、场景和连续性能力",
    }


def _script_result(source_id: str, *, scenario_id: str = "") -> dict:
    characters = [
        {
            "id": "lin_xia",
            "name": "林夏",
            "gender": "female",
            "look": "27岁亚洲女性，深色短发，浅色工装，佩戴调度耳机",
            "background_description": "应急调度负责人，判断清晰、表达克制",
        },
        {
            "id": "gao_yuan",
            "name": "高远",
            "gender": "male",
            "look": "31岁亚洲男性，短发，深蓝检修工装，手持检测仪",
            "background_description": "高空车维修工程师，行动准确、重视复检",
        },
        {
            "id": "zhou_gong",
            "name": "周工",
            "gender": "male",
            "look": "35岁亚洲男性，黄色安全帽，反光背心，携带对讲机",
            "background_description": "施工现场负责人，关注安全与复工结果",
        },
    ]
    beat_specs = (
        ("hook", "lin_xia", "告警刚到，先别让现场盲目重启。", "调度中心", "dispatch"),
        ("conflict", "lin_xia", "我核对故障码，同时启动备用设备复检。", "调度中心", "dispatch"),
        ("evidence", "gao_yuan", "支腿传感器正常，备用高空车可以出场。", "设备场", "depot"),
        ("reversal", "gao_yuan", "再做一次载荷测试，把风险挡在出场前。", "设备场", "depot"),
        ("solution", "zhou_gong", "现场故障点已隔离，备用车现在接替作业。", "白天施工现场", "site"),
        ("cta", "zhou_gong", "人、车、现场三线闭环，施工恢复。", "白天施工现场", "site"),
    )
    beats = []
    for beat, speaker, dialogue, scene, continuity_id in beat_specs:
        beats.append({
            "beat": beat,
            "speaker": speaker,
            "dialogue": dialogue,
            "duration_sec": 4,
            "visual_intent": f"{scene}中，{speaker}完成与台词一致的明确动作",
            "source_ids": [source_id],
            "broll_hint": "",
            "continuity_id": continuity_id,
            "scene": scene,
            "characters": [speaker],
            "emotion": "专注",
            "tone": "坚定",
            "gesture": "配合任务做简洁手势",
            "body_action": "完成当前工作动作并保持自然视线",
            "camera": "中景缓慢推进",
        })
    if scenario_id == "manual_background_voiceover":
        beats[0].update({
            "speaker": "narrator",
            "speakers": ["narrator"],
            "characters": [],
            "speech_mode": "narration",
            "scene": "画外解说",
            "scene_id": "narration_booth",
        })
    variants = []
    for index, title in enumerate((
        "三人三地完成高空车复工闭环",
        "高空车双保险调度实录",
        "从故障告警到施工恢复",
    )):
        variants.append({
            "title": title,
            "creative_thesis": "用三人跨场景行动证明设备租赁的履约能力",
            "target_duration_sec": 24,
            "characters": characters,
            "beats": beats,
        })
    return {
        "variants": variants,
        "recommended_index": 0,
        "recommendation_reason": "第一版最完整覆盖三人、多场景和连续性锚点",
    }


def run_source_to_compiled(
    service,
    scenario: dict,
    *,
    fixture_root: Path,
    asr_enabled: bool = True,
) -> dict:
    """Run source → brief → scripts → approval → graph compilation.

    The caller supplies a real ``ManagementService``.  The function creates
    exact-hash LLM replay fixtures just before each provider boundary, so any
    unexpected request shape fails closed instead of reaching the network.
    """
    source_input = dict(scenario.get("source") or {})
    source = service.add_source(
        source_type=str(source_input.get("type") or "manual"),
        title=str(source_input.get("title") or "零费用全流程回归"),
        content=str(source_input.get("content") or ""),
    )
    source_id = str(source["source_id"])
    source_record = _source_record(service.database, source_id)

    brief_prompt = topic_briefs._brief_prompt([source_record])
    brief_request = doubao_llm.request_payload(brief_prompt, max_tokens=3000)
    brief_root = _write_replay(
        Path(fixture_root) / "brief",
        fixture_id=f"{scenario.get('scenario_id', 'scenario')}:brief",
        operation="doubao.llm.complete_json",
        request=brief_request,
        result=_brief_result(source_record),
    )
    os.environ.update({
        "FACTORY_PROVIDER_MODE": "replay",
        "FACTORY_REPLAY_FIXTURE_ROOT": str(brief_root),
        "PRODUCTION_EXECUTION_ENGINE": "graph_v2",
    })
    brief_payload = service.generate_brief_from_source(
        source_id, risk_confirmed=True
    )
    brief_id = str(brief_payload["brief_id"])

    brief = brief_payload["brief"]
    topic_id = "TOPIC-" + brief_id.removeprefix("BRIEF-")
    topic_briefs.approve_brief(
        service.database,
        brief_id,
        topic_id=topic_id,
        target_audience=str(brief.get("audience") or ""),
        angle_index=0,
    )
    preferences = {
        "target_duration_sec": 24,
        "suggested_beat_count": 6,
        "suggested_character_count": 3,
    }
    context, sources = script_drafts._load_context(service.database, brief_id)
    script_prompt = script_drafts._prompt(context, sources, preferences)
    script_request = doubao_llm.request_payload(script_prompt, max_tokens=6000)
    scripts_root = _write_replay(
        Path(fixture_root) / "scripts",
        fixture_id=f"{scenario.get('scenario_id', 'scenario')}:scripts",
        operation="doubao.llm.complete_json",
        request=script_request,
        result=_script_result(
            source_id,
            scenario_id=str(scenario.get("scenario_id") or ""),
        ),
    )
    os.environ["FACTORY_REPLAY_FIXTURE_ROOT"] = str(scripts_root)
    generated = service.confirm_brief_and_generate_scripts(
        brief_id,
        angle_index=0,
        generation_preferences=preferences,
    )
    drafts = service.database.execute(
        "SELECT draft_id,variant_index FROM script_drafts "
        "WHERE brief_id=? ORDER BY variant_index",
        (brief_id,),
    ).fetchall()
    if not drafts:
        raise RuntimeError("script replay did not create any drafts")
    approved = service.approve_script(str(drafts[0]["draft_id"]))
    job_id = str(approved["job_id"])
    configured = service.configure_video(job_id, {
        "character_mappings": {},
        "orientation": "9:16",
        "production_profile": "standard",
        "subtitle_style": "clean",
        "asr_enabled": bool(asr_enabled),
        "audio_mode": "seedance_native",
        "anchor_review_mode": "manual",
        "budget_cents": 10_000,
        "character_anchor_generator": "seedance_2_5",
        "scene_anchor_generator": "seedance_2_5",
        "max_auto_retries": 0,
    })
    job = service.database.execute(
        "SELECT execution_engine,production_spec_hash,storyboard,cost_cents "
        "FROM jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    storyboard = json.loads(job["storyboard"] or "{}")
    return {
        "scenario_id": str(scenario.get("scenario_id") or "scenario"),
        "source_id": source_id,
        "brief_id": brief_id,
        "job_id": job_id,
        "draft_count": int(generated["draft_count"]),
        "master_count": len(storyboard.get("masters") or []),
        "execution_engine": str(job["execution_engine"]),
        "production_spec_hash": str(
            job["production_spec_hash"]
            or (configured.get("compilation") or {}).get("spec_hash")
            or ""
        ),
        "provider_mode": "replay",
        "external_cost_cents": int(job["cost_cents"] or 0),
        "completed_stages": [
            "source", "brief", "scripts", "approved", "compiled",
        ],
    }


def _run_ffmpeg(command: list[str]) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *command],
        check=True,
        capture_output=True,
    )


def _fixture_image(path: Path, *, color: str = "0xD9E7E0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg([
        "-f", "lavfi", "-i", f"color=c={color}:s=320x568",
        "-frames:v", "1", str(path),
    ])
    return path


def _fixture_video(path: Path, *, duration_sec: float, index: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    colors = ("0x426B5B", "0x55718A", "0x8A6C55", "0x6C668A")
    frequency = 440 + (max(1, int(index)) * 80)
    duration = max(0.5, float(duration_sec))
    _run_ffmpeg([
        "-f", "lavfi", "-i",
        f"color=c={colors[(index - 1) % len(colors)]}:s=320x568:r=25:d={duration}",
        "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration={duration}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ])
    return path


def _fixture_bgm(path: Path, *, duration_sec: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg([
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", str(max(1.0, float(duration_sec))),
        "-codec:a", "libmp3lame", "-q:a", "9", str(path),
    ])
    return path


def build_zero_cost_handlers(
    database: sqlite3.Connection,
    *,
    media_root: Path,
):
    """Build graph handlers that replace only paid provider boundaries.

    Graph scheduling, state transitions, human gates and local composition are
    untouched.  Fixture handlers materialize the artifacts a paid provider
    would return and always record zero cost.
    """
    media_root = Path(media_root).resolve()

    async def character_anchor(node: sqlite3.Row):
        spec, node_input = production_adapters._frozen_spec(database, node)
        character_id = str(node_input.get("character_id") or "").strip()
        character = next((
            deepcopy(item) for item in (spec.get("characters") or [])
            if isinstance(item, dict)
            and str(item.get("id") or "") == character_id
        ), None)
        if character is None:
            raise RuntimeError(f"fixture character missing: {character_id}")
        plan = character_assets.character_makeup_plan([character])
        if not plan:
            raise RuntimeError(f"fixture identity plan missing: {character_id}")
        entry = plan[0]
        token = character_assets.identity_token(str(entry["identity_key"]))
        image = _fixture_image(
            media_root / str(node["job_id"]) / "anchors" / f"character-{token}.png"
        )
        database.execute(
            "INSERT INTO character_identity_anchors("
            "job_id,identity_key,character_id,display_name,source_type,state,"
            "anchor_mode,image_path,preview_path,duration_sec,cost_cents,"
            "approval_state,generator,model) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(job_id,identity_key) DO UPDATE SET state='READY',"
            "image_path=excluded.image_path,preview_path=excluded.preview_path,"
            "cost_cents=0,approval_state='PENDING_REVIEW'",
            (
                node["job_id"], entry["identity_key"], character_id,
                entry["display_name"], "fixture_identity_anchor", "READY",
                "static_image", str(image), str(image), 0, 0,
                "PENDING_REVIEW", "fixture", "zero-cost-v1",
            ),
        )
        database.commit()
        return production_executor.NodeOutcome.require_review({
            "character_id": character_id,
            "identity_key": str(entry["identity_key"]),
            "image_path": str(image),
            "fixture": True,
        })

    async def scene_anchor(node: sqlite3.Row):
        _spec, node_input = production_adapters._frozen_spec(database, node)
        scene_key = str(node_input.get("scene_key") or "").strip().casefold()
        if not scene_key:
            raise RuntimeError("fixture scene key missing")
        token = hashlib.sha256(scene_key.encode("utf-8")).hexdigest()[:20]
        image = _fixture_image(
            media_root / str(node["job_id"]) / "anchors" / f"scene-{token}.png",
            color="0xC9D6DF",
        )
        database.execute(
            "INSERT INTO scene_visual_anchors("
            "job_id,anchor_key,description,master_indexes,state,image_path,"
            "preview_path,cost_cents,generator,model,approval_state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(job_id,anchor_key) DO UPDATE SET state='READY',"
            "image_path=excluded.image_path,preview_path=excluded.preview_path,"
            "cost_cents=0,approval_state='PENDING_REVIEW'",
            (
                node["job_id"], scene_key,
                str(node_input.get("description") or scene_key), "[]", "READY",
                str(image), str(image), 0, "fixture", "zero-cost-v1",
                "PENDING_REVIEW",
            ),
        )
        database.commit()
        return production_executor.NodeOutcome.require_review({
            "scene_key": scene_key,
            "image_path": str(image),
            "fixture": True,
        })

    async def master(node: sqlite3.Row):
        _spec, node_input = production_adapters._frozen_spec(database, node)
        master_idx = int(node_input.get("master_idx") or 0)
        row = database.execute(
            "SELECT duration_sec FROM shots WHERE job_id=? AND idx=?",
            (node["job_id"], master_idx),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"fixture master row missing: {master_idx}")
        video = _fixture_video(
            media_root / str(node["job_id"]) / "masters" / f"{master_idx:02d}.mp4",
            duration_sec=float(row["duration_sec"] or 1),
            index=master_idx,
        )
        database.execute(
            "UPDATE shots SET state='OK',video_path=?,provider='fixture',"
            "video_model='zero-cost-v1',native_audio=1,cost_cents=0,error=NULL "
            "WHERE job_id=? AND idx=?",
            (str(video), node["job_id"], master_idx),
        )
        database.commit()
        return {
            "master_idx": master_idx,
            "video_path": str(video),
            "fixture": True,
        }

    async def bgm(node: sqlite3.Row):
        job = database.execute(
            "SELECT storyboard FROM jobs WHERE job_id=?", (node["job_id"],)
        ).fetchone()
        storyboard = json.loads(job["storyboard"] or "{}") if job else {}
        duration = sum(
            float(master.get("duration") or 0)
            for master in (storyboard.get("masters") or [])
        )
        path = _fixture_bgm(
            media_root / str(node["job_id"]) / "bgm.mp3",
            duration_sec=duration,
        )
        database.execute(
            "UPDATE jobs SET bgm_path=? WHERE job_id=?",
            (str(path), node["job_id"]),
        )
        database.commit()
        return {"bgm_path": str(path), "fixture": True}

    return {
        "character_anchor": character_anchor,
        "scene_anchor": scene_anchor,
        "master": master,
        "bgm": bgm,
        "local_compose": (
            lambda node: production_adapters.run_local_compose_node(database, node)
        ),
        "final_review": (
            lambda node: production_adapters.run_final_review_node(database, node)
        ),
    }


def run_source_to_ready_to_publish(
    service,
    scenario: dict,
    *,
    fixture_root: Path,
    media_root: Path,
) -> dict:
    """Exercise the complete console workflow and stop before external publish."""
    compiled = run_source_to_compiled(
        service,
        scenario,
        fixture_root=fixture_root,
        asr_enabled=False,
    )
    job_id = str(compiled["job_id"])
    service.start_video_job(
        job_id,
        confirmed=True,
        approved_budget_cents=10_000,
        high_risk_confirmed=True,
    )
    handlers = build_zero_cost_handlers(
        service.database, media_root=media_root
    )
    observed_states = ["PRODUCTION_QUEUED"]
    first = asyncio.run(production_graph_runner.run_graph_job(
        service.database,
        job_id,
        configured_limit=4,
        handlers=handlers,
    ))
    observed_states.append(first.job_state)
    if first.job_state != "ANCHOR_REVIEW_REQUIRED":
        raise RuntimeError(
            "zero-cost graph did not stop at the continuity-anchor review gate: "
            f"{first.job_state}"
        )
    service.approve_continuity_anchors(
        job_id, note="zero-cost regression fixture approved"
    )
    observed_states.append("PRODUCTION_QUEUED")

    from .steps import s6_compose

    previous_assets_dir = s6_compose.ASSETS_DIR
    s6_compose.ASSETS_DIR = Path(media_root).resolve()
    try:
        second = asyncio.run(production_graph_runner.run_graph_job(
            service.database,
            job_id,
            configured_limit=4,
            handlers=handlers,
        ))
    finally:
        s6_compose.ASSETS_DIR = previous_assets_dir
    observed_states.append(second.job_state)
    if second.job_state != "COMPOSED":
        raise RuntimeError(
            f"zero-cost graph did not produce a reviewable final: {second.job_state}"
        )
    service.review_video(
        job_id,
        decision="approved",
        first_pass=True,
        checks={
            "identity": True,
            "lip_sync": True,
            "subtitle_sync": True,
            "visual_integrity": True,
            "clean_frame": True,
            "editing_rhythm": True,
        },
    )
    published = service.submit_for_publish(job_id)
    observed_states.append(str(published["state"]))
    job = service.database.execute(
        "SELECT state,final_path,cost_cents FROM jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    nodes = service.database.execute(
        "SELECT node_id,state FROM production_nodes WHERE job_id=? "
        "ORDER BY node_id",
        (job_id,),
    ).fetchall()
    failed_nodes = [
        str(node["node_id"]) for node in nodes
        if str(node["state"]) == "FAILED"
    ]
    return {
        **compiled,
        "job_state": str(job["state"]),
        "final_path": str(job["final_path"] or ""),
        "external_cost_cents": int(job["cost_cents"] or 0),
        "observed_states": observed_states,
        "failed_nodes": failed_nodes,
        "all_nodes_succeeded": bool(nodes) and all(
            str(node["state"]) == "SUCCEEDED" for node in nodes
        ),
        "completed_stages": [
            "source", "brief", "scripts", "approved", "compiled",
            "anchors", "anchor_review", "masters", "local_compose",
            "final_review", "ready_to_publish",
        ],
    }
