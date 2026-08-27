"""Stable, sanitized production failure outcomes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


FailureCategory = Literal[
    "configuration", "contract", "provider", "quality", "local", "policy"
]
RetryPolicy = Literal["auto", "reconcile", "manual", "never"]
CostEffect = Literal["none", "reserved", "charged", "unknown"]


@dataclass(frozen=True)
class NextAction:
    id: str
    label: str


@dataclass(frozen=True)
class FailureOutcome:
    code: str
    category: FailureCategory
    stage: str
    retry_policy: RetryPolicy
    user_message: str
    technical_summary: str
    cost_effect: CostEffect
    next_action: NextAction


@dataclass(frozen=True)
class _FailureTemplate:
    category: FailureCategory
    retry_policy: RetryPolicy
    user_message: str
    technical_summary: str
    cost_effect: CostEffect
    action_id: str
    action_label: str


_REGISTRY: dict[str, _FailureTemplate] = {
    "execution_contract_invalid": _FailureTemplate(
        category="contract",
        retry_policy="manual",
        user_message=(
            "生产数据未能自动对齐，系统已在新的付费调用前暂停并保留现场"
        ),
        technical_summary="frozen production contract invariant failed",
        cost_effect="none",
        action_id="repair_execution_contract",
        action_label="重新同步生产状态",
    ),
    "internal_error": _FailureTemplate(
        category="local",
        retry_policy="manual",
        user_message="生产过程出现未识别异常，已保留现场，请重新诊断",
        technical_summary="unclassified production failure",
        cost_effect="unknown",
        action_id="preserve_and_diagnose",
        action_label="保留现场并重新诊断",
    ),
    "request_invalid": _FailureTemplate(
        category="contract",
        retry_policy="manual",
        user_message="生产参数与模型能力不匹配，请重新编译生产规格",
        technical_summary="provider request rejected by deterministic validation",
        cost_effect="none",
        action_id="recompile_production_spec",
        action_label="重新编译生产规格",
    ),
    "input_privacy_rejected": _FailureTemplate(
        category="policy",
        retry_policy="never",
        user_message="输入素材未通过真人隐私校验，请更换已授权素材",
        technical_summary="input privacy policy rejected",
        cost_effect="none",
        action_id="replace_private_reference",
        action_label="更换已授权素材",
    ),
    "output_policy_rejected": _FailureTemplate(
        category="policy",
        retry_policy="never",
        user_message="生成结果未通过平台内容政策，请调整画面要求",
        technical_summary="generated output policy rejected",
        cost_effect="none",
        action_id="revise_output_prompt",
        action_label="调整画面要求",
    ),
    "output_copyright_rejected": _FailureTemplate(
        category="policy",
        retry_policy="never",
        user_message="生成结果涉及版权风险，请更换参考素材",
        technical_summary="generated output copyright policy rejected",
        cost_effect="none",
        action_id="replace_copyright_reference",
        action_label="更换参考素材",
    ),
    "account_unavailable": _FailureTemplate(
        category="provider",
        retry_policy="manual",
        user_message="模型账号当前不可用，请处理账号后再恢复任务",
        technical_summary="provider account unavailable",
        cost_effect="none",
        action_id="view_provider_account_guide",
        action_label="查看账号处理指引",
    ),
    "transient_network": _FailureTemplate(
        category="provider",
        retry_policy="auto",
        user_message="模型网络暂时不可用，系统将自动重试",
        technical_summary="transient provider transport failure before task creation",
        cost_effect="none",
        action_id="retry_automatically",
        action_label="等待系统自动重试",
    ),
    "transient_retry_exhausted": _FailureTemplate(
        category="provider",
        retry_policy="manual",
        user_message="网络重试已达到上限，请确认网络后重新尝试",
        technical_summary="automatic transport retries exhausted",
        cost_effect="none",
        action_id="retry_failed_job",
        action_label="重新尝试任务",
    ),
    "music_configuration_invalid": _FailureTemplate(
        category="configuration",
        retry_policy="manual",
        user_message="音乐素材库配置不可用，请检查音乐配置",
        technical_summary="configured music provider is unavailable",
        cost_effect="none",
        action_id="configure_music_provider",
        action_label="检查音乐配置",
    ),
    "budget_confirmation_required": _FailureTemplate(
        category="configuration",
        retry_policy="manual",
        user_message="下一次付费调用超过已确认预算，已在扣费前暂停",
        technical_summary="confirmed production budget is insufficient",
        cost_effect="none",
        action_id="increase_job_budget",
        action_label="确认预算并继续",
    ),
}


def _from_template(
    code: str, template: _FailureTemplate, *, stage: str,
) -> FailureOutcome:
    return FailureOutcome(
        code=code,
        category=template.category,
        stage=stage,
        retry_policy=template.retry_policy,
        user_message=template.user_message,
        technical_summary=template.technical_summary,
        cost_effect=template.cost_effect,
        next_action=NextAction(
            id=template.action_id,
            label=template.action_label,
        ),
    )


def describe_code(code: object, *, stage: str = "unknown") -> FailureOutcome:
    """Resolve an already-persisted stable code for UI presentation."""
    normalized = str(code or "").strip()
    if normalized not in _REGISTRY:
        normalized = "internal_error"
    return _from_template(normalized, _REGISTRY[normalized], stage=stage)


def describe_failure(error: BaseException, *, stage: str = "") -> FailureOutcome:
    """Map an exception to a fixed safe outcome without echoing raw details."""
    resolved_stage = stage or "unknown"
    code = str(getattr(error, "stable_code", "") or "").strip()
    pause_state = str(getattr(error, "pause_state", "") or "").strip()
    class_name = error.__class__.__name__
    if class_name == "MusicProviderConfigError":
        return describe_code(
            "music_configuration_invalid", stage=resolved_stage,
        )
    if code == "transient_network" and str(
        getattr(error, "pause_state", "") or ""
    ).strip() == "PROVIDER_ACTION_REQUIRED":
        return FailureOutcome(
            code=code,
            category="provider",
            stage=resolved_stage,
            retry_policy="reconcile",
            user_message="远端任务提交结果未知，请核对原任务状态",
            technical_summary="provider submission outcome unknown",
            cost_effect="unknown",
            next_action=NextAction(
                id="reconcile_provider_task",
                label="核对远端任务",
            ),
        )
    template = _REGISTRY.get(code)
    if template is not None:
        return _from_template(code, template, stage=resolved_stage)

    if pause_state == "PRODUCTION_REVIEW_REQUIRED":
        return FailureOutcome(
            code=code or "quality_review_required",
            category="quality",
            stage=resolved_stage,
            retry_policy="manual",
            user_message="母片已生成但需要人工复核，请处理候选母片",
            technical_summary="generated master requires human quality review",
            cost_effect="charged",
            next_action=NextAction(
                id="review_master_candidates",
                label="预览并处理候选母片",
            ),
        )
    if pause_state == "ANCHOR_REVIEW_REQUIRED":
        return FailureOutcome(
            code=code or "anchor_review_required",
            category="quality",
            stage=resolved_stage,
            retry_policy="manual",
            user_message="人物与场景锚点已生成，请完成一致性审核",
            technical_summary="continuity anchors require human approval",
            cost_effect="charged",
            next_action=NextAction(
                id="review_continuity_anchors",
                label="审核人物与场景锚点",
            ),
        )
    if pause_state == "PROVIDER_ACTION_REQUIRED":
        return FailureOutcome(
            code=code or "provider_task_unknown",
            category="provider",
            stage=resolved_stage,
            retry_policy="reconcile",
            user_message="远端任务状态尚未确认，请先核对原任务",
            technical_summary="provider task requires reconciliation",
            cost_effect="unknown",
            next_action=NextAction(
                id="reconcile_provider_task",
                label="核对远端任务",
            ),
        )
    if "BudgetError" in class_name:
        return FailureOutcome(
            code="budget_confirmation_required",
            category="configuration",
            stage=resolved_stage,
            retry_policy="manual",
            user_message="预计成本超过已确认预算，请调整预算或生产方案",
            technical_summary="confirmed production budget is insufficient",
            cost_effect="none",
            next_action=NextAction(
                id="increase_job_budget",
                label="调整任务预算",
            ),
        )
    if "QualityError" in class_name:
        return FailureOutcome(
            code="quality_rejected",
            category="quality",
            stage=resolved_stage,
            retry_policy="manual",
            user_message="母片质量未达到自动验收标准，请查看质量结果",
            technical_summary="generated master failed the quality gate",
            cost_effect="charged",
            next_action=NextAction(
                id="review_quality_failure",
                label="查看质量结果",
            ),
        )

    message = str(error).casefold()
    if "precision-sensitive shot blocked" in message:
        return FailureOutcome(
            code="prompt_auto_repair_required",
            category="contract",
            stage=stage or "preflight",
            retry_policy="manual",
            user_message="当前镜头包含模型无法稳定执行的精确动作，请重新编译生产规格",
            technical_summary="precision-sensitive production instruction blocked",
            cost_effect="none",
            next_action=NextAction(
                id="recompile_production_spec",
                label="重新编译生产规格",
            ),
        )
    if any(token in message for token in (
        "temporary timeout", "temporarily unavailable", "connection reset",
    )):
        return _from_template(
            "transient_network", _REGISTRY["transient_network"],
            stage=resolved_stage,
        )
    return describe_code("internal_error", stage=stage or "unknown")


def describe_retry_exhausted(
    outcome: FailureOutcome, *, stage: str = ""
) -> FailureOutcome:
    """Convert a finite automatic retry into an actionable terminal result."""
    if outcome.retry_policy != "auto":
        return outcome
    return describe_code(
        "transient_retry_exhausted", stage=stage or outcome.stage,
    )
