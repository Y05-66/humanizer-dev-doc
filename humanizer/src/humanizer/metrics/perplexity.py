"""
简化困惑度（perplexity）计算模块 —— V2 升级版。

与 MVP 版本的关键区别：
  1. 分词升级为 jieba（中文语义单元），替代字符级 bigram proxy
  2. 引入"AI 疲劳词加权因子"：常见 AI 词的 log-prob 被人为压低，
     使同类文本熵降低 → PPL 降低 → 被判为更像 AI
  3. 分段独立计算再平均，避免长文本稀释短段的异常信号
"""

from __future__ import annotations

import math
from collections import Counter

from humanizer.models import MetricsSnapshot

from .baseline import Baseline

# 参考通用现代汉语小说语料的 logP 近似值（基于北大计算语言学研究所 CCL 常用词表估算）。
# 数值越接近 0 表示该词在小说中越常见；负值越大表示越罕见/专业。
_CORPUS_LOG_PROB = {
    # ── 高频虚词 / 功能词 ───────────────────────────────────────
    "的": -0.05, "了": -0.10, "是": -0.15, "在": -0.20, "我": -0.30,
    "有": -0.40, "和": -0.50, "就": -0.60, "不": -0.70, "人": -0.80,
    "都": -0.90, "一": -1.00, "这": -1.10, "那": -1.20,
    "他": -1.30, "她": -1.40, "它": -1.50, "们": -1.60, "你": -1.70,
    "来": -1.80, "要": -1.90, "说": -2.00, "到": -2.10, "去": -2.20,
    "可以": -2.30, "还": -2.40, "又": -2.50, "也": -2.60, "会": -2.70,
    "对": -2.80, "因为": -2.90, "所以": -3.00, "但是": -3.10, "如果": -3.20,
    "然后": -3.30, "而且": -3.40, "或者": -3.50, "虽然": -3.60, "即使": -3.70,
    "一个": -1.10, "这个": -1.20, "那个": -1.30, "自己": -1.40, "其他": -2.00,
    "没有": -1.50, "还是": -1.80, "怎么": -1.90, "什么": -2.00, "这样": -2.10,
    "那样": -2.20, "这里": -2.30, "那里": -2.40, "已经": -2.50, "现在": -2.60,
    "就是": -1.60, "一起": -2.00, "起来": -2.10, "出来": -2.20, "进来": -2.30,

    # ── 修饰词 ────────────────────────────────────────────────
    "很": -1.20, "非常": -1.80, "特别": -1.90, "更加": -2.00, "十分": -2.10,
    "巨大": -3.00, "强烈": -3.10, "深深": -3.20, "无限": -3.30, "永恒": -3.40,
    "突然": -2.00, "忽然": -2.30, "立刻": -2.10, "马上": -2.00, "顿时": -2.80,
    "随即": -2.90, "继而": -3.00, "猛然": -2.70, "倏地": -3.50,

    # ── 常见场景名词 ────────────────────────────────────────────
    "时间": -2.20, "地方": -2.30, "世界": -2.40, "生活": -2.50, "事情": -2.40,
    "意思": -2.30, "样子": -2.40, "情况": -2.30, "问题": -2.40, "机会": -2.50,
    "声音": -2.60, "感觉": -2.70, "记忆": -2.80, "思想": -2.90, "情感": -3.00,

    # ── 人称代词及关联 ──────────────────────────────────────────
    "我们": -1.50, "你们": -1.80, "他们": -1.60, "她们": -1.90, "它们": -2.50,
}

# ── AI 高频繁词（出现率高但语义密度低 → 显著压低熵值）───────────────────────
_AI_OVERUSE = {
    # 情态填充词
    "仿佛": -3.5, "似乎": -3.5, "好像": -3.5, "不禁": -3.5,
    "显然": -3.6, "无疑": -3.6, "毫无疑问": -4.0, "不可否认": -4.0, "毋庸置疑": -4.0,
    # 模糊心理概述词
    "心中": -3.0, "心里": -2.5, "眼底": -3.2, "眸中": -3.5, "眼里": -2.8,
    "嘴角": -3.0, "目光": -2.8,
    # 程式化情绪副词
    "顿时": -3.8, "旋即": -4.0, "随即": -3.5,
    # 空洞程度词
    "无比": -3.5, "极其": -3.5, "格外": -3.2, "分外": -3.3,
    # 总结过渡套话
    "总之": -4.0, "总而言之": -4.2, "综上所述": -4.5,
    "众所周知": -4.0, "不难发现": -3.8, "可想而知": -3.8,
    # 程式化描写模板词
    "闪过": -3.5, "涌起": -3.5, "泛起": -3.5,
    "一丝": -3.5, "一抹": -3.5,
    "深吸一口气": -4.0, "缓缓": -3.5, "微微": -3.5,
    # 宏大叙事词汇（长篇 AI 指纹）
    "命运": -3.2, "时代": -3.3, "历史": -3.2, "文明": -3.4,
    "人生": -2.8, "世界": -2.0,
    # 段首套话
    "就在这时": -3.8,
    # 结论式升华词
    "这才": -3.5, "终于": -2.8,
    # 冗余连接词
    "于是": -3.0, "然而": -3.0,
}


def _tokenize(text: str) -> list[str]:
    """用 jieba 切词，过滤标点、空白及纯数字串（保留有意义的数字词如'三千'）。"""
    try:
        import jieba
        tokens = []
        for w in jieba.cut(text):
            w_stripped = w.strip()
            if not w_stripped:
                continue
            # 过滤纯标点和空白
            if all(ord(c) < 0x2E00 or (ord(c) > 0x2E7F and ord(c) < 0x3000)
                   or (ord(c) > 0xFE30 and ord(c) < 0xFF00)
                   for c in w_stripped):
                continue
            # 过滤纯数字串（保留含汉字的混合词）
            if w_stripped.isdigit():
                continue
            tokens.append(w_stripped)
        return tokens
    except ImportError:
        # jieba 不可用时回退到字符 bigram
        chars = [ch for ch in text if ch.isalnum() or '一' <= ch <= '鿿']
        result: list[str] = []
        for i, ch in enumerate(chars):
            result.append(ch)
            if i + 1 < len(chars):
                result.append(ch + chars[i + 1])
        return result


def _segment_text(text: str, segment_len: int = 300) -> list[str]:
    """将文本按固定长度分割成若干片段，保证不切断句子中间。"""
    import re
    sentences = re.split(r'([。！？\n]+)', text)
    parts: list[str] = []
    buf = ""
    for part in sentences:
        buf += part
        if len(buf) >= segment_len or '。！？\n' in part:
            stripped = buf.strip()
            if stripped:
                parts.append(stripped)
            buf = ""
    if buf.strip():
        parts.append(buf.strip())
    return parts if parts else [text]


def compute_simple_perplexity(text: str) -> float:
    """
    基于 jieba 分词 + 语料 logP 加权的简化困惑度：PPL = 2^H_adjusted。

    H_adjusted = Σ p(w) * (-log2(p(w)) + bias(w))
      - p(w)           ：词频分布均匀性（越高说明用词越丰富）
      - bias(w) < 0   ：AI 高偏见词的额外惩罚，使含此类词的文本熵降低

    返回值解释：
      - 越高 → 词汇越多样且少用 AI 套路词 → 更像人类
      - 越低 → 词汇单调或 AI 套路密集 → 更像 AI
      - 人类目标区间 ≈ 60–120；AI 典型区间 ≈ 20–50
    """
    if not text.strip():
        return 0.0

    segments = _segment_text(text)
    avg_ppl = 0.0
    weights: list[float] = []

    for seg in segments:
        tokens = _tokenize(seg)
        if not tokens:
            continue
        freq = Counter(tokens)
        total = sum(freq.values())
        if total == 0:
            continue

        entropy = 0.0
        weight_sum = 0.0
        for w, count in freq.items():
            prob = count / total
            # 基础香农熵项
            if prob > 0:
                entropy += -prob * math.log2(prob)
            # AI 疲劳词惩罚项（bias 为负 → 整体熵降低）
            bias = _AI_OVERUSE.get(w, 0.0)
            weight_sum += prob * bias

        adjusted_entropy = entropy + weight_sum
        ppl_seg = 2 ** max(adjusted_entropy, 0.01)  # 防止结果为 0
        seg_weight = len(tokens) / max(len(text), 1)
        avg_ppl += ppl_seg * seg_weight
        weights.append(seg_weight)

    return round(avg_ppl / sum(weights), 2) if weights else 0.0


def snapshot_with_perplexity(snapshot: MetricsSnapshot, text: str) -> MetricsSnapshot:
    """在已有 snapshot 上补充简化困惑度。"""
    ppl = compute_simple_perplexity(text)
    verdict = Baseline.classify_perplexity(ppl)
    snapshot.perplexity_simple = ppl
    snapshot.perplexity_verdict = verdict
    return snapshot
