"""Stage 4：验证 —— 保真性检查 + 指标复检 + 朗读代理。

注：中文人名锚点在没有分词器的情况下无法精确提取，因此本阶段
主要依赖数字/日期/对话引用作为事实锚点，人名保真性留待后续接入 jieba。
"""

from __future__ import annotations

import logging
import re

from humanizer.metrics import compute_full_metrics
from humanizer.models import (
    FidelityStatus,
    MetricsSnapshot,
    VerifyReport,
)

logger = logging.getLogger(__name__)

# 数值与日期是可靠的事实锚点
_NUMBER_RE = re.compile(r'\d+(?:\.\d+)?(?:%|万|亿|千|百|十)?')
_DATE_RE = re.compile(r'\d{4}[年/-]\d{1,2}[月/-]\d{1,2}')
_DIALOGUE_RE = re.compile(r'''["\"""'"'"'「『][^\n"\"""'"'"'」』]+["\"""'"'"'」』]''')


def run_verify(
    original_text: str,
    rewritten_text: str,
    metrics_before: MetricsSnapshot | None = None,
    metrics_after: MetricsSnapshot | None = None,
    a_before: int = 0,
    a_after: int = 0,
) -> VerifyReport:
    """多信号验证：保真性 + 指标方向 + 规则清零 + 朗读代理。"""
    report = VerifyReport(
        metrics_before=metrics_before,
        metrics_after=metrics_after or compute_full_metrics(rewritten_text),
        a_reduction_before=a_before,
        a_reduction_after=a_after,
    )

    if a_before > 0:
        report.a_reduction_pct = ((a_before - a_after) / a_before) * 100.0
    else:
        report.a_reduction_pct = 100.0 if a_after == 0 else 0.0

    # === 1. 保真性检查（仅基于数字/日期/对话）===
    orig_nums = set(_NUMBER_RE.findall(original_text))
    rewrite_nums = set(_NUMBER_RE.findall(rewritten_text))
    missing_nums = orig_nums - rewrite_nums
    altered_nums = _find_altered_numbers(orig_nums, rewrite_nums)

    fidelity_issues: list[str] = []
    for n in missing_nums:
        fidelity_issues.append(f"数字丢失: {n!r}")
    for old_n, new_n in altered_nums:
        fidelity_issues.append(f"数字变更: {old_n!r} → {new_n!r}")

    if fidelity_issues:
        report.overall_fidelity = FidelityStatus.FAIL
        report.fidelity_issues = fidelity_issues
        logger.error("保真性验证失败：%s", fidelity_issues)
    else:
        report.overall_fidelity = FidelityStatus.PASS
        logger.info("保真性验证通过")

    # === 2. 指标方向检查 ===
    if metrics_before and report.metrics_after:
        mb = metrics_before
        ma = report.metrics_after
        direction_ok = True
        if mb.burstiness < 0.5 and ma.burstiness < mb.burstiness:
            direction_ok = False
            report.fidelity_issues.append(f"突发性未改善：{mb.burstiness:.4f} → {ma.burstiness:.4f}")
        if mb.ttr < 0.30 and ma.ttr < mb.ttr:
            direction_ok = False
            report.fidelity_issues.append(f"TTR 未改善：{mb.ttr:.4f} → {ma.ttr:.4f}")
        if direction_ok and not report.fidelity_issues:
            logger.info("指标方向验证通过")

    # === 3. 规则清零复检 ===
    if report.a_reduction_pct < 80.0:
        report.fidelity_issues.append(
            f"A 级规则命中未达标：仅减少 {report.a_reduction_pct:.1f}%（要求 ≥ 80%）"
        )

    # === 4. 朗读代理：检测过度碎片化 ===
    short_streak = _check_short_sentence_streak(rewritten_text, max_consecutive=3, max_len=5)
    if short_streak:
        report.fidelity_issues.append(
            f"检测到连续 {short_streak} 个 ≤5 字的短句，可能存在过度碎片化"
        )

    logger.info(
        "验证完成：fidelity=%s, a_reduction=%.1f%%, issues=%d",
        report.overall_fidelity.value, report.a_reduction_pct, len(report.fidelity_issues),
    )
    return report


def _find_altered_numbers(orig_nums: set, rewrite_nums: set) -> list[tuple]:
    def parse_num(n: str):
        try:
            return int(float(n.replace(',', '')))
        except ValueError:
            return None
    diffs = []
    for n in orig_nums:
        v = parse_num(n)
        if v is None:
            continue
        for r in rewrite_nums:
            rv = parse_num(r)
            if rv == v and r != n:
                diffs.append((n, r))
    return diffs


def _check_short_sentence_streak(text: str, max_consecutive: int = 3, max_len: int = 5) -> int:
    sentences = re.split(r'[。！？\n]', text)
    streak = max_streak = 0
    for s in sentences:
        s = s.strip()
        if 0 < len(s) <= max_len:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return max_streak if max_streak >= max_consecutive else 0
