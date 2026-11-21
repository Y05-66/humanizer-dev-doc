"""
统计指标的人类基线常量。

注意：以下为初步估计值，需用真实中文小说语料复算校准。
参见 scripts/calibrate.py。
"""

from __future__ import annotations


class Baseline:
    """人类写作 vs AI 写作的统计指标参考区间。"""

    # 突发性：句长标准差 / 句长均值
    # AI 偏向均匀（低突发），人类有更长短句交替（高突发）
    BURSTINESS_AI_MAX = 0.3
    BURSTINESS_HUMAN_MIN = 0.5

    # 句长标准差（字符数）
    SENT_LEN_STD_AI = 1.2
    SENT_LEN_STD_HUMAN_MIN = 4.7

    # TTR（原始型次比，仅作参考，不用于判定）
    TTR_AI_RANGE = (0.15, 0.25)
    TTR_HUMAN_RANGE = (0.30, 0.45)

    # MATTR（移动平均型次比，滑动窗口=50 tokens，主判定指标）
    # MATTR 消除了文本长度的影响，更适合跨长度比较
    MATTAR_AI_MAX = 0.35     # < 此值表明用词过于重复
    MATTAR_HUMAN_MIN = 0.42  # >= 此值进入人类分布带

    # 简化困惑度（基于词频熵 2^H）
    PPL_SIMPLE_AI_RANGE = (20, 50)
    PPL_SIMPLE_HUMAN_RANGE = (60, 120)

    @classmethod
    def classify_burstiness(cls, value: float) -> str:
        if value < cls.BURSTINESS_AI_MAX:
            return "too_flat"
        if value >= cls.BURSTINESS_HUMAN_MIN:
            return "within_human_band"
        return "borderline"

    @classmethod
    def classify_ttr(cls, value: float) -> str:
        lo, hi = cls.TTR_HUMAN_RANGE
        if lo <= value <= hi:
            return "within_human_band"
        if value < lo:
            return "too_flat"  # 用词过于保守
        return "too_diverse"

    @classmethod
    def classify_mattar(cls, value: float) -> str:
        """基于 MATTR 判定：与原始 TTR 不同，MATTR 受长度影响小。"""
        if value < cls.MATTAR_AI_MAX:
            return "too_flat"       # 词汇重复度高
        if value >= cls.MATTAR_HUMAN_MIN:
            return "within_human_band"
        return "borderline"

    @classmethod
    def classify_perplexity(cls, value: float) -> str:
        lo, hi = cls.PPL_SIMPLE_HUMAN_RANGE
        if lo <= value <= hi:
            return "within_human_band"
        if value < lo:
            return "too_predictable"
        return "too_unpredictable"

    @classmethod
    def describe(cls) -> dict:
        return {
            "burstiness": {
                "ai_max": cls.BURSTINESS_AI_MAX,
                "human_min": cls.BURSTINESS_HUMAN_MIN,
                "note": "句长标准差 / 句长均值；越高越不均匀（人类特征）",
            },
            "ttr": {
                "ai_range": cls.TTR_AI_RANGE,
                "human_range": cls.TTR_HUMAN_RANGE,
                "note": "独特词数 / 总词数；越高词汇越丰富",
            },
            "mattar": {
                "ai_max": cls.MATTAR_AI_MAX,
                "human_min": cls.MATTAR_HUMAN_MIN,
                "note": "移动平均型次比（滑动窗口=50 tokens）；越高词汇越丰富",
            },
            "perplexity_simple": {
                "ai_range": cls.PPL_SIMPLE_AI_RANGE,
                "human_range": cls.PPL_SIMPLE_HUMAN_RANGE,
                "note": "基于词频熵的近似困惑度；越高表示文本越不可预测",
            },
        }
