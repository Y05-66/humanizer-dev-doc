"""规则引擎单元测试。"""

from __future__ import annotations

import pytest
from pathlib import Path

from humanizer.rules.engine import RuleEngine
from humanizer.rules.loader import load_bans_a, load_bans_b, load_c_forbidden
from humanizer.models import Severity


RULES_DIR = Path(__file__).parents[1] / "config" / "rules"


@pytest.fixture
def rule_files() -> dict:
    return {
        "a": RULES_DIR / "bans_a.yaml",
        "b": RULES_DIR / "bans_b.yaml",
        "c": RULES_DIR / "c_forbidden.yaml",
    }


class TestRuleLoading:
    def test_load_bans_a(self, rule_files):
        rules = load_bans_a(rule_files["a"])
        assert len(rules) > 0
        for r in rules:
            assert r.severity == "A"
            assert r.type.value in ("literal", "regex", "density")
            assert len(r.pattern) > 0

    def test_load_bans_b(self, rule_files):
        rules = load_bans_b(rule_files["b"])
        assert len(rules) > 0
        for r in rules:
            assert r.severity == "B"

    def test_load_c_forbidden(self, rule_files):
        items = load_c_forbidden(rule_files["c"])
        assert len(items) > 0
        for item in items:
            assert hasattr(item, "id")
            assert hasattr(item, "category")
            assert hasattr(item, "rationale")

    def test_schema_validation_missing_id(self, tmp_path: Path):
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("rules:\n  - severity: A\n    type: literal\n    pattern: test\n", encoding="utf-8")
        from humanizer.rules.loader import load_bans_a
        with pytest.raises(ValueError):
            load_bans_a(bad_yaml)

    def test_schema_validation_invalid_severity(self, tmp_path: Path):
        bad_yaml = tmp_path / "bad2.yaml"
        bad_yaml.write_text("rules:\n  - id: TEST_01\n    severity: X\n    type: literal\n    pattern: test\n", encoding="utf-8")
        from humanizer.rules.loader import load_bans_a
        with pytest.raises((ValueError, Exception)):
            load_bans_a(bad_yaml)


class TestRuleScanning:
    @pytest.fixture
    def engine(self, rule_files) -> RuleEngine:
        return RuleEngine(
            bans_a_path=rule_files["a"],
            bans_b_path=rule_files["b"],
        )

    def test_literal_match(self, engine: RuleEngine):
        text = "他不禁露出了微笑"
        hits = engine.scan(text)
        a_hits = [h for h in hits if h.severity == "A"]
        ban_ids = {h.rule_id for h in a_hits}
        assert any("MODAL" in rid or "WORD_MODAL" in rid for rid in ban_ids), \
            f"应命中不禁相关规则，实际: {ban_ids}"

    def test_regex_pattern_match(self, engine: RuleEngine):
        text = "这不是我的选择，而是命运的安排"
        hits = engine.scan(text)
        a_hits = [h for h in hits if h.severity == "A"]
        assert len(a_hits) > 0, f"应命中 BAN_A_01 句式规则，实际: {[h.rule_id for h in a_hits]}"

    def test_no_false_positive_on_human_text(self, engine: RuleEngine):
        """人类写作文本不应触发大量 A 级命中。"""
        text = "他推开门走了出去，外面下雨了。"
        hits = engine.scan(text)
        a_hits = [h for h in hits if h.severity == "A"]
        assert len(a_hits) <= 2, f"人类风格短句不应有多个 A 级命中: {a_hits}"

    def test_density_trigger(self, engine: RuleEngine):
        """高密度出现同一词时应触发密度规则。"""
        text = "非常非常好非常非常非常非常非常非常非常" * 10
        hits = engine.scan(text)
        b_hits = [h for h in hits if h.severity == "B"]
        density_b = [h for h in b_hits if "密度" in h.matched_text]
        assert len(density_b) > 0, "高密度'非常'应触发密度规则"

    def test_c_not_scanned_by_default(self, engine: RuleEngine):
        """C 级默认不应被扫描。"""
        text = "他被一脚踢飞了出去"  # 包含被动句
        hits = engine.scan(text, include_c=False)
        c_hits = [h for h in hits if h.severity == "C"]
        assert len(c_hits) == 0

    def test_hit_ordering(self, engine: RuleEngine):
        """命中应按位置升序排列。"""
        text = "不禁心中涌起一股暖意"
        hits = engine.scan(text)
        positions = [h.start for h in hits]
        assert positions == sorted(positions), "命中应按 start 升序排列"


class TestMatchers:
    def test_literal_matcher_finds_all(self):
        from humanizer.rules.matchers import LiteralMatcher
        m = LiteralMatcher("abc")
        results = m.find_all("x abc y abc z")
        assert len(results) == 2
        assert results[0].start == 2
        assert results[1].start == 8

    def test_literal_matcher_none(self):
        from humanizer.rules.matchers import LiteralMatcher
        m = LiteralMatcher("zzz")
        results = m.find_all("hello world")
        assert len(results) == 0

    def test_regex_matcher(self):
        from humanizer.rules.matchers import RegexMatcher
        m = RegexMatcher(r"\d+")
        results = m.find_all("abc 123 def 456")
        assert len(results) == 2
        assert results[0].match_text == "123"
        assert results[1].match_text == "456"

    def test_density_matcher_below_threshold(self):
        from humanizer.rules.matchers import DensityMatcher
        # Use a non-existent word so count is always 0, guaranteeing rate < threshold
        m = DensityMatcher("xyznonexistentqwerty", threshold_per_thousand=10.0)
        result = m.check("a very long piece of text with no such word anywhere " * 50)
        assert result is None

    def test_density_matcher_above_threshold(self):
        from humanizer.rules.matchers import DensityMatcher
        text = "test " * 2000
        m = DensityMatcher("test", threshold_per_thousand=5.0)
        result = m.check(text)
        assert result is not None
