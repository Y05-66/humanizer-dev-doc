"""保真性测试 —— 确保改写不丢失事实和人物名。"""

from __future__ import annotations

import pytest
from pathlib import Path

from humanizer.models import Mode, FidelityStatus
from humanizer.stages.diagnose import run_diagnose
from humanizer.stages.plan import run_plan
from humanizer.stages.rewrite import run_rewrite
from humanizer.stages.verify import run_verify
from humanizer.rules.engine import RuleEngine

RULES_DIR = Path(__file__).parents[1] / "config" / "rules"


class TestFidelity:
    @pytest.fixture
    def factual_text(self) -> str:
        return """Zhang San left Beijing for Shanghai on May 15, 2023.
He took G1 high-speed train, ticket price 660 yuan.
After arriving at Shanghai Hongqiao Station, he met Li Si at the exit.
Li Si said: "Long time no see, you look exactly the same."
Zhang San smiled and said: "Yes, three years have passed."
They went to Jing An Temple together and stayed all afternoon."""

    @pytest.fixture
    def engine(self) -> RuleEngine:
        return RuleEngine(
            bans_a_path=RULES_DIR / "bans_a.yaml",
            bans_b_path=RULES_DIR / "bans_b.yaml",
        )

    def test_names_preserved(self, factual_text: str, engine: RuleEngine):
        plan = run_plan(run_diagnose(factual_text, Mode.CLEAN, engine), Mode.CLEAN)
        rewritten, _ = run_rewrite(factual_text, plan, Mode.CLEAN, rule_engine=engine)
        assert "Zhang San" in rewritten
        assert "Li Si" in rewritten

    def test_numbers_preserved(self, factual_text: str, engine: RuleEngine):
        plan = run_plan(run_diagnose(factual_text, Mode.CLEAN, engine), Mode.CLEAN)
        rewritten, _ = run_rewrite(factual_text, plan, Mode.CLEAN, rule_engine=engine)
        assert "2023" in rewritten
        assert "660" in rewritten
        assert "G1" in rewritten

    def test_dialogue_preserved(self, factual_text: str, engine: RuleEngine):
        plan = run_plan(run_diagnose(factual_text, Mode.CLEAN, engine), Mode.CLEAN)
        rewritten, _ = run_rewrite(factual_text, plan, Mode.CLEAN, rule_engine=engine)
        assert "Long time no see" in rewritten

    def test_verify_report_correct(self, factual_text: str, engine: RuleEngine):
        plan = run_plan(run_diagnose(factual_text, Mode.CLEAN, engine), Mode.CLEAN)
        rewritten, _ = run_rewrite(factual_text, plan, Mode.CLEAN, rule_engine=engine)
        report = run_verify(factual_text, rewritten, a_before=3, a_after=1)
        assert report.overall_fidelity == FidelityStatus.PASS

    def test_no_false_addition_of_names(self, factual_text: str, engine: RuleEngine):
        """Rewrite should not introduce new person names beyond what's expected."""
        plan = run_plan(run_diagnose(factual_text, Mode.CLEAN, engine), Mode.CLEAN)
        rewritten, _ = run_rewrite(factual_text, plan, Mode.CLEAN, rule_engine=engine)
        orig_names = {"Zhang San", "Li Si"}
        import re
        # Look for capitalized two-word sequences that look like person names
        # (but allow known place names like Shanghai, Jing An)
        caps_phrases = re.findall(r'\b[A-Z][a-z]+(?: [A-Z][a-z]+)\b', rewritten)
        allowed = {"Shanghai Hongqiao", "Jing An"}
        extra = [p for p in caps_phrases if p not in orig_names and p not in allowed]
        assert len(extra) == 0, f"Unexpected new name-like phrases: {extra}"


class TestHumanTextNoFalsePositive:
    @pytest.fixture
    def human_sample(self, fixture_dir: Path) -> str:
        return (fixture_dir / "human_sample_01.md").read_text(encoding="utf-8")

    @pytest.fixture
    def engine(self) -> RuleEngine:
        return RuleEngine(
            bans_a_path=RULES_DIR / "bans_a.yaml",
            bans_b_path=RULES_DIR / "bans_b.yaml",
        )

    def test_human_text_low_a_hits(self, human_sample: str, engine: RuleEngine):
        hits = engine.scan(human_sample)
        a_hits = [h for h in hits if h.severity == "A"]
        assert len(a_hits) <= 5, f"Human text has too many A hits: {[h.rule_id for h in a_hits]}"

    def test_human_text_writeback_unchanged(self, human_sample: str, engine: RuleEngine):
        """Clean rewrite of human text should stay close to original."""
        plan = run_plan(run_diagnose(human_sample, Mode.CLEAN, engine), Mode.CLEAN)
        rewritten, _ = run_rewrite(human_sample, plan, Mode.CLEAN, rule_engine=engine)
        orig_set = set(human_sample)
        rewrite_set = set(rewritten)
        jaccard = len(orig_set & rewrite_set) / max(len(orig_set | rewrite_set), 1)
        assert jaccard > 0.85, f"Human text changed too much, Jaccard={jaccard:.3f}"


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"
