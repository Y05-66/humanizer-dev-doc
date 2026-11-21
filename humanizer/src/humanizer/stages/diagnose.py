"""Stage 1：诊断 —— 规则扫描 + 统计指标计算。"""

from __future__ import annotations

import logging

from humanizer.metrics import compute_full_metrics
from humanizer.models import DiagnosisReport, Mode
from humanizer.rules.engine import RuleEngine

logger = logging.getLogger(__name__)


def run_diagnose(
    text: str,
    mode: Mode,
    rule_engine: RuleEngine | None = None,
) -> DiagnosisReport:
    """
    执行诊断阶段。

    Args:
        text: 待分析文本
        mode: 运行模式（detect/clean/refactor）
        rule_engine: 预创建的规则引擎实例（用于测试注入 mock）

    Returns:
        DiagnosisReport 包含所有规则命中和指标快照
    """
    engine = rule_engine or RuleEngine()
    hits = engine.scan(text, include_c=(mode == Mode.REFACTOR))

    metrics = compute_full_metrics(text)

    report = DiagnosisReport(
        source_file="",  # 由调用方填充
        text=text,
        text_length=len(text),
        rule_hits=hits,
        metrics=metrics,
    )

    logger.info(
        "诊断完成：长度=%d, A=%d, B=%d, burstiness=%.4f, TTR=%.4f, PPL=%.2f",
        report.text_length,
        len(report.a_hits),
        len(report.b_hits),
        metrics.burstiness,
        metrics.ttr,
        metrics.perplexity_simple,
    )
    return report
