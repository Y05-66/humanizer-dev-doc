"""诊断模块的集成测试。"""

from __future__ import annotations

import pytest
from pathlib import Path

from humanizer.models import Mode
from humanizer.stages.diagnose import run_diagnose
from humanizer.metrics import compute_full_metrics


class TestDiagnose:
    """诊断阶段的完整流程测试。"""

    @pytest.fixture
    def ai_sample_text(self, fixture_dir: Path) -> str:
        return (fixture_dir / "ai_sample_01.md").read_text(encoding="utf-8")

    @pytest.fixture
    def human_sample_text(self, fixture_dir: Path) -> str:
        return (fixture_dir / "human_sample_01.md").read_text(encoding="utf-8")

    def test_diagnose_ai_text_has_a_hits(self, ai_sample_text: str):
        report = run_diagnose(ai_sample_text, mode=Mode.DETECT)
        assert len(report.a_hits) > 0, "AI 样本应有多处 A 级命中"
        assert report.text_length > 0
        assert report.metrics is not None

    def test_diagnose_human_text_few_a_hits(self, human_sample_text: str):
        report = run_diagnose(human_sample_text, mode=Mode.DETECT)
        # 人类文本应极少或没有 A 级命中（放宽到 <=5，因为规则会捕获个别常见词）
        assert len(report.a_hits) <= 5, f"人类样本 A 级命中过多: {[h.rule_id for h in report.a_hits]}"

    def test_diagnose_detect_mode_returns_report(self, ai_sample_text: str):
        report = run_diagnose(ai_sample_text, mode=Mode.DETECT)
        assert report.source_file == ""
        assert isinstance(report.rule_hits, list)
        assert isinstance(report.metrics, object)

    def test_burstiness_calculation(self):
        """突发性计算正确性：有明显长短句的文本突发率更高。"""
        short_text = "你好。再见。"
        varied_text = "你好。这是一个相当长的句子，包含了丰富的信息和各种细节描述，远远超过了简单问候的长度。再见。"
        from humanizer.metrics.burstiness import compute_burstiness
        b_short = compute_burstiness(short_text)
        b_varied = compute_burstiness(varied_text)
        # 变化的文本应该有更高的突发性
        assert b_varied > b_short


class TestMetrics:
    """统计指标计算测试。"""

    def test_ttr_simple(self):
        from humanizer.metrics.ttr import compute_ttr
        text = "abc abc def def ghi"
        ttr = compute_ttr(text)
        # 5 unique chars out of 15 total
        assert 0 < ttr <= 1.0

    def test_ttr_empty(self):
        from humanizer.metrics.ttr import compute_ttr
        assert compute_ttr("") == 0.0

    def test_perplexity_nonzero(self):
        from humanizer.metrics.perplexity import compute_simple_perplexity
        text = "这是一个测试文本，用来检验简化困惑度的计算是否正确。"
        ppl = compute_simple_perplexity(text)
        assert ppl > 0

    def test_full_metrics(self):
        text = "你好世界。这是一个测试。\n第二行。"
        snap = compute_full_metrics(text)
        assert snap.text_length > 0
        assert snap.sentence_count > 0
        assert snap.burstiness >= 0
        assert 0 <= snap.ttr <= 1.0
        assert snap.perplexity_simple > 0


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"
