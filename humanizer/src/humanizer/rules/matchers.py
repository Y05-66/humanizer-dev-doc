"""
基础匹配器 —— literal / regex / density 三种类型的统一接口。
目前主要作为 RuleEngine 的内部实现，也可独立测试。
"""

from __future__ import annotations

from dataclasses import dataclass

import regex


@dataclass
class MatcherResult:
    """单次匹配的上下文无关结果。"""

    match_text: str
    start: int
    end: int


class LiteralMatcher:
    """字符串包含匹配器。"""

    def __init__(self, pattern: str):
        self.pattern = pattern

    def find_all(self, text: str) -> list[MatcherResult]:
        results = []
        idx = 0
        while True:
            pos = text.find(self.pattern, idx)
            if pos == -1:
                break
            results.append(MatcherResult(
                match_text=self.pattern,
                start=pos,
                end=pos + len(self.pattern),
            ))
            idx = pos + 1
        return results


class RegexMatcher:
    """正则表达式匹配器。"""

    def __init__(self, pattern: str):
        self._pattern = regex.compile(pattern)

    def find_all(self, text: str) -> list[MatcherResult]:
        return [
            MatcherResult(match_text=m.group(), start=m.start(), end=m.end())
            for m in self._pattern.finditer(text)
        ]

    def search(self, text: str) -> MatcherResult | None:
        m = self._pattern.search(text)
        if m is None:
            return None
        return MatcherResult(match_text=m.group(), start=m.start(), end=m.end())


class DensityMatcher:
    """
    密度触发匹配器 —— 当某字符串在整个文本中出现的频率超过阈值时才报告。
    用于 B 级弱化副词的高频检测。
    """

    def __init__(self, pattern: str, threshold_per_thousand: float):
        self.pattern = pattern
        self.threshold = threshold_per_thousand

    def check(self, text: str) -> MatcherResult | None:
        count = text.count(self.pattern)
        length = len(text)
        if length == 0:
            return None
        rate = (count / length) * 1000
        if rate >= self.threshold:
            return MatcherResult(
                match_text=f"[密度 {self.pattern}: {count}次/{rate:.1f}次/千字]",
                start=0,
                end=length,
            )
        return None
