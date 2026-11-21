"""指标计算统一入口 —— 从原始文本一次性计算出完整 MetricsSnapshot。"""

from __future__ import annotations

from humanizer.models import MetricsSnapshot

from .burstiness import compute_burstiness, compute_sentence_length_std, snapshot_from_text
from .perplexity import compute_simple_perplexity, snapshot_with_perplexity
from .ttr import compute_ttr, snapshot_with_ttr


def compute_full_metrics(text: str) -> MetricsSnapshot:
    """
    对给定文本计算所有基础统计指标。

    顺序：
      1. 突发性 + 句长标准差
      2. TTR
      3. 简化困惑度
    """
    snap = snapshot_from_text(text)
    snap = snapshot_with_ttr(snap, text)
    snap = snapshot_with_perplexity(snap, text)
    return snap
