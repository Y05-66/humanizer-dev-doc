"""TTR / MATTR 词汇多样性指标。

原始 TTR 存在严重的长度依赖问题（文本越长 TTR 越低），MATTR
使用固定长度滑动窗口计算后取平均，消除这一偏差。

分词策略：
  - 优先使用 jieba（可选依赖）获得准确的中文分词
  - 无 jieba 时回退到字符级 bigram 混合切分
  - 不同分词器会对绝对值产生影响，因此阈值仅做参考区间而非硬判据
"""

from __future__ import annotations

from humanizer.models import MetricsSnapshot

from .baseline import Baseline


def _tokenize_chinese(text: str) -> list[str]:
    """
    轻量中文分词。

    有 jieba 时用 jieba，否则用字符 bigram 回退。
    过滤掉纯标点/空白 token。
    """
    try:
        import jieba
        words = [w for w in jieba.cut(text)
                 if w.strip()
                 and not all(' ' <= c <= '⁯' or
                             '＀' <= c <= '￯'
                             for c in w)]
        return words
    except ImportError:
        chars = [ch for ch in text
                 if ch.isalnum() or '一' <= ch <= '鿿']
        words: list[str] = []
        for i, ch in enumerate(chars):
            words.append(ch)
            if i + 1 < len(chars):
                words.append(ch + chars[i + 1])
        return words


def compute_ttr(text: str) -> float:
    """
    计算原始 TTR = 独特 token 数 / 总 token 数。

    注意：原始 TTR 受文本长度影响较大（长文 TTR 天然偏低），
    仅作对照展示，不作为主要判定依据。
    """
    if not text.strip():
        return 0.0
    words = _tokenize_chinese(text)
    if not words:
        return 0.0
    return len(set(words)) / len(words)


def compute_mattar(text: str, window_size: int = 50) -> float:
    """
    计算 MATTR（Moving-Average Type-Token Ratio）。

    将文本切分为固定长度滑动窗口（默认 50 tokens，步长 25），
    计算每个窗口的 TTR 再取平均，从而消除长度依赖。

    参数:
        text:           输入文本
        window_size:    滑动窗口大小（token 数）
    返回:
        0.0–1.0 之间的浮点数，越高表示词汇多样性越好
    """
    if not text.strip():
        return 0.0
    words = _tokenize_chinese(text)
    if not words:
        return 0.0

    n = len(words)
    if n < window_size:
        # 文本短于窗口：整段计算一次
        return len(set(words)) / n

    step = max(1, window_size // 2)
    values: list[float] = []
    for start in range(0, n - window_size + 1, step):
        chunk = words[start:start + window_size]
        values.append(len(set(chunk)) / len(chunk))
    return sum(values) / len(values) if values else 0.0


def snapshot_with_ttr(snapshot: MetricsSnapshot, text: str) -> MetricsSnapshot:
    """
    在已有 snapshot 上补充 TTR / MATTR 指标。

    - ttr      → 原始 TTR（参照展示，不做判定）
    - mattar   → MATTR（主判定指标）
    - ttr_verdict → 基于 MATTR 的判定结果
    """
    raw_ttr = compute_ttr(text)
    mattar  = compute_mattar(text)
    verdict = Baseline.classify_mattar(mattar)
    snapshot.ttr       = raw_ttr
    snapshot.mattar    = mattar
    snapshot.ttr_verdict = verdict
    return snapshot
