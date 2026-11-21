"""
突发性指标计算（V2）—— 句长标准差 / 句长均值 + 新增段内节奏分析。

V2 改进：
  1. 分句逻辑更健壮（支持中英混排）
  2. 新增"段内句长变异系数"作为子维度，用于精细定位节奏呆板段落
  3. 保留原始 burstiness 计算不变，扩展输出字段
"""

from __future__ import annotations

import re
import statistics

from humanizer.models import MetricsSnapshot, MetricVerdict

from .baseline import Baseline

# 中文/英文句子终结符 + 省略号（单独占位符）
_SENTENCE_END_RE = re.compile(r'(?<=[。！？!?.;；])(?=\s*[^\s])')
_ELLIPSIS_RE    = re.compile(r'\.{3,}|…{1,}')  # ... 或 ……


def split_sentences(text: str) -> list[str]:
    """
    按句子终结符切分文本，保留非空结果。
    支持：句号、问号、感叹号、分号、省略号
    """
    if not text.strip():
        return []
    # 先将省略号替换为句号（统一处理），然后再切分
    normalized = _ELLIPSIS_RE.sub('。', text)
    raw_parts = _SENTENCE_END_RE.split(normalized)
    sentences: list[str] = []
    for part in raw_parts:
        s = part.strip()
        if s:
            sentences.append(s)
    return sentences


def compute_burstiness(text: str) -> float:
    """
    全篇突发性 = 句长标准差 / 句长均值。

    值越高表示句长变化越大（更接近人类写作）；
    值越低表示句长越均匀（AI 典型特征）。
    """
    sentences = split_sentences(text)
    if len(sentences) < 2:
        return 0.0
    lengths = [len(s) for s in sentences]
    mean_len = statistics.mean(lengths)
    if mean_len == 0:
        return 0.0
    std_len = statistics.pstdev(lengths) if len(lengths) > 1 else 0.0
    return std_len / mean_len


def compute_paragraph_rhythm_variability(text: str) -> dict:
    """
    按段落计算每段的句长变异系数（CV），返回各段 CV 及总体方差。
    目的：检测是否存在大面积"句长高度一致"的段落（AI 结构特征）。

    Returns:
        {
            "paragraph_cvs": [float, ...],   # 每段的 CV
            "mean_cv": float,                 # 平均段内 CV
            "cv_std": float,                  # CV 的标准差（越小→越均匀→越 AI）
            "low_rhythm_paras": int,          # CV < 0.2 的段落数（节奏呆板段落）
        }
    """
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    cvs: list[float] = []
    low_count = 0
    for para in paragraphs:
        sents = split_sentences(para)
        if len(sents) < 2:
            continue
        lens = [len(s) for s in sents]
        m = statistics.mean(lens)
        if m == 0:
            continue
        cv = statistics.pstdev(lens) / m
        cvs.append(cv)
        if cv < 0.2:
            low_count += 1

    mean_cv = statistics.mean(cvs) if cvs else 0.0
    cv_std  = statistics.pstdev(cvs) if len(cvs) > 1 else 0.0

    return {
        "paragraph_cvs": cvs,
        "mean_cv": round(mean_cv, 4),
        "cv_std": round(cv_std, 4),
        "low_rhythm_paras": low_count,
    }


def compute_sentence_length_std(text: str) -> float:
    """计算句长的总体标准差（字符数）。"""
    sentences = split_sentences(text)
    if len(sentences) < 2:
        return 0.0
    lengths = [len(s) for s in sentences]
    return statistics.pstdev(lengths)


def snapshot_from_text(text: str) -> MetricsSnapshot:
    """基于纯文本计算完整的 MetricsSnapshot（V2 扩展）。"""
    sentences = split_sentences(text)
    burstiness = compute_burstiness(text)
    sent_std   = compute_sentence_length_std(text)
    rhythm     = compute_paragraph_rhythm_variability(text)

    verdict_burst = Baseline.classify_burstiness(burstiness)
    _ = (
        MetricVerdict.TOO_FLAT if sent_std < Baseline.SENT_LEN_STD_AI
        else MetricVerdict.WITHIN_HUMAN_BAND
    )

    return MetricsSnapshot(
        text_length=len(text),
        sentence_count=len(sentences),
        burstiness=burstiness,
        sentence_length_std=sent_std,
        burstiness_verdict=verdict_burst,
        # V2 新增字段
        paragraph_rhythm=rhythm,
    )
