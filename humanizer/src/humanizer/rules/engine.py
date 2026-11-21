"""
规则匹配引擎 —— 支持 literal / regex / density 三种匹配类型。
"""

from __future__ import annotations

import logging

from humanizer.models import RuleHit, Severity
from .schema import RuleEntry

from .loader import (
    build_compiled_patterns,
    compute_line_map,
    hit_to_rule_hit,
    load_bans_a,
    load_bans_b,
    load_c_forbidden,
)

logger = logging.getLogger(__name__)


class RuleEngine:
    """
    规则扫描引擎。

    用法：
        engine = RuleEngine()
        hits = engine.scan(text)
    """

    def __init__(self, bans_a_path=None, bans_b_path=None, c_forbidden_path=None):
        self._a_rules = load_bans_a(bans_a_path)
        self._b_rules = load_bans_b(bans_b_path)
        self._c_items = load_c_forbidden(c_forbidden_path)

        # 预编译所有正则
        self._compiled_a = build_compiled_patterns(self._a_rules)
        self._compiled_b = build_compiled_patterns(self._b_rules)

        # 收集所有字面量匹配模式
        self._literal_a: dict[str, RuleEntry] = {}
        self._literal_b: dict[str, RuleEntry] = {}
        for rule, _ in self._compiled_a:
            if rule.type.value == "literal":
                self._literal_a[rule.pattern] = rule
        for rule, _ in self._compiled_b:
            if rule.type.value == "literal":
                self._literal_b[rule.pattern] = rule

    @property
    def a_rule_ids(self) -> set[str]:
        return {r.id for r, _ in self._compiled_a}

    @property
    def b_rule_ids(self) -> set[str]:
        return {r.id for r, _ in self._compiled_b}

    @property
    def c_item_ids(self) -> set[str]:
        return {item.id for item in self._c_items}

    def scan(
        self,
        text: str,
        include_c: bool = False,
    ) -> list[RuleHit]:
        """
        对全文执行规则扫描，返回按位置排序的 RuleHit 列表。

        Args:
            text: 待检测文本
            include_c: 是否同时扫描 C 级（通常不扫描，C 级仅作仲裁参考）

        Returns:
            按 start 升序排列的 RuleHit 列表
        """
        hits: list[RuleHit] = []

        # --- A 级扫描 ---
        hits.extend(self._scan_regex(text, self._compiled_a, Severity.A))
        hits.extend(self._scan_literals(text, self._literal_a, Severity.A))

        # --- B 级扫描 ---
        hits.extend(self._scan_regex(text, self._compiled_b, Severity.B))
        hits.extend(self._scan_literals(text, self._literal_b, Severity.B))
        hits.extend(self._scan_density(text, self._b_rules))

        # --- C 级（可选，默认不开启）---
        if include_c:
            # C 级不做自动扫描，只记录禁动项清单供仲裁使用
            pass

        # 去重并排序
        seen: set[tuple[str, int, int]] = set()
        unique: list[RuleHit] = []
        for h in sorted(hits, key=lambda x: (x.start, -x.end)):
            key = (h.rule_id, h.start, h.end)
            if key not in seen:
                seen.add(key)
                unique.append(h)

        logger.debug("规则扫描完成：共命中 %d 处（A=%d, B=%d）",
                      len(unique),
                      sum(1 for h in unique if h.severity == "A"),
                      sum(1 for h in unique if h.severity == "B"))
        return unique

    def _scan_regex(
        self,
        text: str,
        compiled: list[tuple],
        severity: Severity,
    ) -> list[RuleHit]:
        """对文本执行所有正则规则扫描。"""
        line_map = compute_line_map(text)
        hits: list[RuleHit] = []
        for rule, pattern in compiled:
            for m in pattern.finditer(text):
                ln = line_map[m.start()] if m.start() < len(line_map) else None
                hits.append(hit_to_rule_hit(rule, text, m.span(), line_number=ln))
        return hits

    def _scan_literals(
        self,
        text: str,
        literals: dict[str, RuleEntry],
        severity: Severity,
    ) -> list[RuleHit]:
        """对文本执行所有字面量匹配。"""
        line_map = compute_line_map(text)
        hits: list[RuleHit] = []
        for pat, rule in literals.items():
            idx = 0
            while True:
                pos = text.find(pat, idx)
                if pos == -1:
                    break
                ln = line_map[pos] if pos < len(line_map) else None
                hits.append(hit_to_rule_hit(rule, text, (pos, pos + len(pat)), line_number=ln))
                idx = pos + 1
        return hits

    def _scan_density(
        self,
        text: str,
        rules: list,
    ) -> list[RuleHit]:
        """对密度触发型规则进行统计扫描（B 级特有）。"""
        hits: list[RuleHit] = []
        text_len_chars = len(text)
        if text_len_chars == 0:
            return hits

        for rule in rules:
            if rule.type.value != "density":
                continue
            threshold = rule.threshold or 5.0
            count = text.count(rule.pattern)
            rate_per_thousand = (count / text_len_chars) * 1000 if text_len_chars > 0 else 0
            if rate_per_thousand >= threshold:
                # 整段标记为一个密度违规
                hits.append(RuleHit(
                    rule_id=rule.id,
                    severity="B",
                    start=0,
                    end=text_len_chars,
                    matched_text=f"[密度 {rule.pattern}: {count}次/{rate_per_thousand:.1f}次/千字]",
                    suggestion=rule.suggestion,
                ))
        return hits

    def summarize(self, hits: list[RuleHit]) -> dict:
        """生成命中的摘要统计。"""
        summary: dict[str, list[str]] = {"A": [], "B": []}
        by_id: dict[str, int] = {}
        for h in hits:
            by_id[h.rule_id] = by_id.get(h.rule_id, 0) + 1

        for rid, cnt in sorted(by_id.items(), key=lambda x: -x[1]):
            label = rid
            # 尝试从规则库中提取人类可读名称
            all_rules = [r for r, _ in self._compiled_a] + [r for r, _ in self._compiled_b]
            matching = [r for r in all_rules if r.id == rid]
            if matching:
                label = f"{rid} ({matching[0].pattern[:30]}…)"
            summary["A" if any(h.severity == "A" for h in hits if h.rule_id == rid) else "B"].append(
                f"{label}  ×{cnt}"
            )
        return summary
