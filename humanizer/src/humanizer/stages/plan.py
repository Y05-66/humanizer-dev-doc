"""Stage 2：规划 —— 根据诊断结果生成 RewritePlan。"""

from __future__ import annotations

import logging

from humanizer.models import DeferredHit, DiagnosisReport, Mode, RewritePlan

logger = logging.getLogger(__name__)


def run_plan(
    diagnosis: DiagnosisReport,
    mode: Mode,
    max_structural_actions: int = 0,
) -> RewritePlan:
    """
    根据诊断报告生成改写计划。

    - clean 模式：A 级直接命中 → deferred_to_llm，B 级标记但不改
    - refactor 模式：额外允许最多 N 个结构层动作

    Args:
        diagnosis: Stage 1 输出的诊断报告
        mode: 当前运行模式
        max_structural_actions: refactor 模式下允许的最大结构调整数

    Returns:
        RewritePlan
    """
    plan = RewritePlan(mode=mode)

    # 收集需要 LLM 处理的命中（A 级 defer + B 级标记）
    for hit in diagnosis.rule_hits:
        if hit.severity == "A":
            context = _extract_context(diagnosis.text, hit.start, hit.end, window=80)
            plan.deferred_hits.append(DeferredHit(hit=hit, context_window=context))
        elif hit.severity == "B" and mode in (Mode.CLEAN, Mode.REFACTOR):
            # B 级在 clean 模式下仅标记，交给 LLM 判断；refactor 下同样处理
            context = _extract_context(diagnosis.text, hit.start, hit.end, window=80)
            plan.deferred_hits.append(DeferredHit(hit=hit, context_window=context))

    # refactor 模式的额外结构层动作
    if mode == Mode.REFACTOR:
        structural = _plan_structural_actions(diagnosis, max_allowed=max_structural_actions)
        plan.structural_actions = structural[:max_structural_actions]

    logger.info(
        "规划完成：模式=%s, deferred=%d, structural_actions=%d",
        mode.value,
        len(plan.deferred_hits),
        len(plan.structural_actions),
    )
    return plan


def _extract_context(text: str, start: int, end: int, window: int = 80) -> str:
    """提取命中位置前后的上下文窗口。"""
    ctx_start = max(0, start - window)
    ctx_end = min(len(text), end + window)
    return text[ctx_start:ctx_end]


def _plan_structural_actions(
    diagnosis: DiagnosisReport,
    max_allowed: int,
) -> list[str]:
    """
    生成结构层改写动作列表。

    当前只做最简单的几类：
    - 若突发率低，建议"引入短句打散节奏"
    - 若 TTR 低，建议"增加词汇变体"
    - 若有连续均长短句，建议"合并/拆分句子"
    """
    actions: list[str] = []
    metrics = diagnosis.metrics
    if metrics is None:
        return actions

    if metrics.burstiness_verdict == "too_flat":
        actions.append("引入短句打散节奏（目标 burstiness > 0.5）")

    if metrics.ttr_verdict == "too_flat":
        actions.append("增加词汇变体以提升 TTR")

    if len(actions) > max_allowed:
        actions = actions[:max_allowed]

    return actions
