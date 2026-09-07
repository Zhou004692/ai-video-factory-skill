"""Describe evidence scope, not product quality or a production E2E verdict.

Only the known built-in runner and its returned observations establish current
suite provenance. A mode/flag, an injected runner, or legacy ``passed`` does not.
This metadata is a scope declaration, not a cryptographic execution attestation.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from .explorer import explore_scenario
from .incidents import run_historical_incident_replays
from .journeys import run_full_journey_suite
from .mutations import run_historical_mutation_calibration
from .perceptual_media import run_perceptual_media_golden
from .shadow_audit import audit_production_shadow
from .video_golden import run_video_performance_golden
from .strict_recovery import run_strict_master_recovery, validate_runner_result


_HANDLERS = (
    "full_flow_regression.build_zero_cost_handlers 替换 character_anchor、"
    "scene_anchor、master、bgm 业务 handler，并写入夹具产物和状态；"
    "不是仅替换 Seedance HTTP transport。"
)
_CATALOG = {
    "strict_master_recovery": (
        "strict_master_recovery", run_strict_master_recovery, "bounded_strict_master_recovery",
        "browser_http_service_cli_with_transport_fixtures",
        "浏览器 HTTP → 正式 ManagementService 队列 → 原生 CLI Worker；"
        "仅单个旁白 master 的损坏下载恢复与脚本终审。替换 HTTP transport、固定时钟/UUID 并隔离存储；"
        "保留生产 handler，本地 FFmpeg/ffprobe 按白名单执行。process_tree_verified=false，不是 OS 进程树隔离。",
        ["两个独立 Worker 的损坏下载与恢复；恢复不重复提交生成请求",
         "浏览器恢复与脚本终审动作经正式 HTTP 服务传播；当前源码指纹绑定"],
        ["完整生产链严格 transport 回放", "素材生成与锚点生成", "OS 进程树/网络隔离",
         "真实识别准确率", "真实成片质量或人工验收"],
    ),
    "component_propagation": (
        "graph_scenarios", explore_scenario, "component_propagation",
        "synthetic_media_with_scripted_graph_outcomes",
        _HANDLERS + "暂停、异常与人工审核动作为脚本化场景。",
        ["已观察场景的图调度、状态流转、契约与语义断言"],
        ["真实生产 handler 的端到端执行", "模型输出质量", "真实人工审核结果"],
    ),
    "video_performance_propagation": (
        "video_source_golden", run_video_performance_golden, "component_propagation",
        "synthetic_media_with_prefilled_answers",
        "合成视频/图片；ASR 文本、说话人、情绪和视觉/表演答案预填，analyze_json 返回脚本答案。",
        ["预填答案在解析、归一化与重建剧本之间的传播和绑定"],
        ["从真实音视频识别台词、说话人、情绪或手势的准确率"],
    ),
    "formal_service_with_high_level_fixture": (
        "journey_suite", run_full_journey_suite, "formal_service_with_high_level_fixture",
        "service_and_browser_with_high_level_fixtures",
        _HANDLERS + "核心服务旅程 ASR 关闭；最终审核检查由脚本提交为通过。"
        "简报/剧本使用高层 Replay 响应；UI/后端/Worker 事实仍以动作证据矩阵为准。",
        ["夹具条件下已运行服务旅程的编译、持久化、审核门与本地合成衔接",
         "单独记录的 UI 派发、后端动作与 Worker 调度"],
        ["真实生产 handler 的端到端执行", "真实 ASR 验收", "最终内容质量", "真实人工审核结果"],
    ),
    "media_technical_checks": (
        "perceptual_media_golden", run_perceptual_media_golden, "media_technical_checks",
        "synthetic_media",
        "FFmpeg 合成运动图案和音频，人工注入冻结、黑帧、时间戳断层与音画时长错位。",
        ["已运行合成样本的技术缺陷检测与变异检出"],
        ["人物一致性、口型、表演、叙事或剪辑审美", "真实成片内容质量"],
    ),
    "production_shadow_lookup": (
        "production_shadow_audit", audit_production_shadow, "read_only_action_lookup",
        "read_only_production_snapshot",
        "只读结构检查与管理台动作映射；不执行恢复，不改写正式任务。",
        ["快照时刻的结构问题及处理入口映射"],
        ["恢复动作已执行", "任务已恢复", "人工待审核等同程序故障"],
    ),
    "historical_incident_replays": (
        "historical_incident_replays", run_historical_incident_replays, "component_propagation",
        "historical_regression_fixtures",
        "历史事故测试夹具；各事故检测边界见 detection_boundary，不能升级为整条生产链证据。",
        ["已运行事故测试中指定护栏的回归结果"],
        ["未覆盖故障的恢复能力", "严格生产端到端执行"],
    ),
    "structural_mutation_checks": (
        "mutation_calibration", run_historical_mutation_calibration, "component_propagation",
        "mutated_structure_fixtures",
        "主动修改结构夹具以触发检测器。",
        ["已执行结构变异的检出结果"],
        ["真实媒体识别准确率", "最终内容质量"],
    ),
}
_UNIMPLEMENTED = {
    "strict_transport_replay": "当前 Flow Guard 未接入保留全部生产 handler 的严格 transport 回放旅程。",
    "recognition_accuracy": "核心服务旅程 ASR 关闭；视频金标答案预填，未运行真实识别准确率评估。",
    "actual_content_quality": "合成媒体、技术检测和脚本审核不构成真实成片内容质量验收。",
}
_OUTCOMES = {"passed", "failed", "partial", "attention"}


def _empty_boundaries() -> dict:
    dimensions = {}
    for key, (_, _, level, _, _, _, not_proven) in _CATALOG.items():
        dimensions[key] = {
            "level": level, "status": "unknown", "source_kind": "unknown",
            "replacement_boundary": "未知：缺少执行来源或结果元数据。",
            "proves": [], "does_not_prove": list(not_proven), "observed_checks": [],
        }
    for key, reason in _UNIMPLEMENTED.items():
        dimensions[key] = {
            "level": key, "status": "not_run", "source_kind": "none",
            "replacement_boundary": reason, "proves": [],
            "does_not_prove": [reason], "observed_checks": [],
        }
    return {
        "schema_version": 1, "status_scope": "technical_suite",
        "metadata_status": "unknown", "dimensions": dimensions,
    }


def _observed_checks(check_id: str, result: Mapping) -> list[str]:
    if check_id == "strict_master_recovery":
        if result.get("status") == "passed" and validate_runner_result(dict(result)):
            return []
        checks = result.get("observed_checks")
        return [check for check in checks if isinstance(check, str) and check.strip()] if isinstance(checks, list) else []
    if check_id == "production_shadow_audit":
        count = result.get("audited_job_count")
        return [f"audited_job_count={count}"] if type(count) is int and count >= 0 else []
    if check_id == "video_source_golden":
        return [value for field in ("passed_guards", "failed_guards")
                if isinstance(result.get(field), (list, tuple))
                for value in result.get(field, []) if isinstance(value, str) and value.strip()]
    field, name = {
        "graph_scenarios": ("reports", "scenario_id"),
        "journey_suite": ("journeys", "scenario_id"),
        "perceptual_media_golden": ("results", "mutation_id"),
        "historical_incident_replays": ("results", "incident_id"),
        "mutation_calibration": ("results", "mutation_id"),
    }[check_id]
    items = result.get(field)
    if not isinstance(items, (list, tuple)):
        return []
    return [item[name] for item in items
            if isinstance(item, Mapping) and isinstance(item.get(name), str)
            and item[name].strip()]


def build_evidence_boundaries(observations: Mapping, implementations: Mapping) -> dict:
    """Classify observed built-in results without inferring evidence from flags."""
    payload = _empty_boundaries()
    for key, (check_id, expected, level, kind, boundary, proves, not_proven) in _CATALOG.items():
        result = observations.get(check_id)
        if not isinstance(result, Mapping) or implementations.get(check_id) is not expected:
            continue
        status = result.get("status")
        if not isinstance(status, str):
            continue
        if status in {"skipped", "not_run"}:
            payload["dimensions"][key]["status"] = "not_run"
            payload["metadata_status"] = "recorded"
            continue
        checks = _observed_checks(check_id, result)
        if status not in _OUTCOMES or not checks:
            continue
        payload["dimensions"][key] = {
            "level": level, "status": status, "source_kind": kind,
            "replacement_boundary": boundary, "proves": list(proves),
            "does_not_prove": list(not_proven), "observed_checks": checks,
        }
        payload["metadata_status"] = "recorded"
    return payload


def normalize_evidence_boundaries(value: object) -> dict:
    """Read new or legacy reports fail-closed; never reconstruct run provenance."""
    payload = _empty_boundaries()
    if not isinstance(value, Mapping) or value.get("schema_version") != 1:
        return payload
    dimensions = value.get("dimensions")
    if not isinstance(dimensions, Mapping):
        return payload
    for key in _CATALOG:
        item = dimensions.get(key)
        if not isinstance(item, Mapping):
            continue
        status = item.get("status")
        if not isinstance(status, str):
            continue
        strings = ("level", "source_kind", "replacement_boundary")
        lists = ("proves", "does_not_prove", "observed_checks")
        if not all(isinstance(item.get(field), str) and item[field].strip()
                   for field in strings):
            continue
        if not all(isinstance(item.get(field), list)
                   and all(isinstance(part, str) and part.strip() for part in item[field])
                   for field in lists):
            continue
        if item["level"] != _CATALOG[key][2] or not item["does_not_prove"]:
            continue
        if status == "not_run":
            if item["source_kind"] != "unknown" or item["proves"] or item["observed_checks"]:
                continue
            payload["dimensions"][key]["status"] = "not_run"
            payload["metadata_status"] = "recorded"
            continue
        if status not in _OUTCOMES:
            continue
        if item["source_kind"] != _CATALOG[key][3]:
            continue
        if not item["proves"] or not item["observed_checks"]:
            continue
        payload["dimensions"][key] = deepcopy({
            field: item[field] for field in ("status", *strings, *lists)
        })
        payload["metadata_status"] = "recorded"
    return payload
