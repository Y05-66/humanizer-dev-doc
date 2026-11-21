"""数据模型定义 —— DiagnosisReport, RewritePlan, VerifyReport 等核心数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class Severity(str, Enum):
    A = "A"
    B = "B"
    C = "C"


class Mode(str, Enum):
    DETECT = "detect"
    CLEAN = "clean"
    REFACTOR = "refactor"


class MetricVerdict(str, Enum):
    WITHIN_HUMAN_BAND = "within_human_band"
    TOO_FLAT = "too_flat"
    TOO_DIVENSE = "too_diverse"
    TOO_PREDICTABLE = "too_predictable"
    TOO_UNPREDICTABLE = "too_unpredictable"
    NA = "na"


class FidelityStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"


@dataclass
class RuleHit:
    """单次规则命中的记录。"""

    rule_id: str
    severity: Literal["A", "B", "C"]
    start: int           # 字符偏移（全文范围内）
    end: int
    matched_text: str
    suggestion: str | None = None
    line_number: int | None = None  # 所在行号（方便定位）

    def __repr__(self) -> str:
        return f"RuleHit(id={self.rule_id}, sev={self.severity}, pos={self.start}-{self.end})"


@dataclass
class MetricsSnapshot:
    """某一时刻的统计指标快照。"""

    text_length: int = 0
    sentence_count: int = 0
    burstiness: float = 0.0          # 句长标准差 / 句长均值
    sentence_length_std: float = 0.0 # 句长标准差
    ttr: float = 0.0                 # 原始型次比（仅对照，不用于判定）
    mattar: float = 0.0              # 移动平均型次比（MATTR，主判定指标）
    perplexity_simple: float = 0.0   # 基于词频熵的简化困惑度

    # 各指标的判定结果
    burstiness_verdict: MetricVerdict = MetricVerdict.NA
    ttr_verdict: MetricVerdict = MetricVerdict.NA   # 实际反映 mattar 判定
    perplexity_verdict: MetricVerdict = MetricVerdict.NA

    # V2 新增：段落内节奏变异分析（仅 V2 burstiness 计算产生）
    paragraph_rhythm: dict = field(default_factory=dict)

    def summary_dict(self) -> dict:
        def _v(v):
            return v.value if isinstance(v, Enum) else str(v)
        rhythm = self.paragraph_rhythm or {}
        return {
            "text_length": self.text_length,
            "sentence_count": self.sentence_count,
            "burstiness": round(self.burstiness, 4),
            "sentence_length_std": round(self.sentence_length_std, 4),
            "ttr": round(self.ttr, 4),
            "mattar": round(self.mattar, 4),
            "perplexity_simple": round(self.perplexity_simple, 2),
            "burstiness_verdict": _v(self.burstiness_verdict),
            "ttr_verdict": _v(self.ttr_verdict),
            "perplexity_verdict": _v(self.perplexity_verdict),
            "rhythm_cv_mean": rhythm.get("mean_cv"),
            "rhythm_cv_std": rhythm.get("cv_std"),
            "rhythm_low_paras": rhythm.get("low_rhythm_paras", 0),
        }


@dataclass
class DiagnosisReport:
    """Stage 1 诊断结果的完整报告。"""

    source_file: str
    text: str
    text_length: int
    rule_hits: list[RuleHit] = field(default_factory=list)
    metrics: MetricsSnapshot | None = None
    metric_verdicts: dict[str, str] = field(default_factory=dict)
    structure_flags: list[str] = field(default_factory=list)  # 仅 refactor 模式填充

    @property
    def a_hits(self) -> list[RuleHit]:
        return [h for h in self.rule_hits if h.severity == "A"]

    @property
    def b_hits(self) -> list[RuleHit]:
        return [h for h in self.rule_hits if h.severity == "B"]

    @property
    def c_hits(self) -> list[RuleHit]:
        return [h for h in self.rule_hits if h.severity == "C"]

    def hit_counts_by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {"A": 0, "B": 0, "C": 0}
        for h in self.rule_hits:
            counts[h.severity] += 1
        return counts


@dataclass
class DeferredHit:
    """需要交给 LLM 处理的命中项。"""

    hit: RuleHit
    context_window: str = ""  # 命中位置前后的上下文

    def __repr__(self) -> str:
        return f"DeferredHit(rule={self.hit.rule_id}, text={self.hit.matched_text!r})"


@dataclass
class RewritePlan:
    """Stage 2 生成的改写计划。"""

    mode: Mode
    deferred_hits: list[DeferredHit] = field(default_factory=list)
    structural_actions: list[str] = field(default_factory=list)  # refactor 模式下最多 N 个
    direct_replacements: list[tuple[int, int, str]] = field(default_factory=list)  # (start, end, replacement)

    @property
    def needs_llm(self) -> bool:
        return len(self.deferred_hits) > 0 or len(self.structural_actions) > 0


@dataclass
class VerifyResult:
    """单段落的验证结果。"""

    original_segment: str
    rewritten_segment: str
    fidelity_status: FidelityStatus = FidelityStatus.PASS
    fidelity_issues: list[str] = field(default_factory=list)
    metrics_before: MetricsSnapshot | None = None
    metrics_after: MetricsSnapshot | None = None
    a_hit_reduction_pct: float = 0.0


@dataclass
class VerifyReport:
    """Stage 4 验证结果的完整报告。"""

    overall_fidelity: FidelityStatus = FidelityStatus.PASS
    fidelity_issues: list[str] = field(default_factory=list)
    results: list[VerifyResult] = field(default_factory=list)
    metrics_before: MetricsSnapshot | None = None
    metrics_after: MetricsSnapshot | None = None
    a_reduction_before: int = 0
    a_reduction_after: int = 0
    a_reduction_pct: float = 0.0

    @property
    def passed(self) -> bool:
        return self.overall_fidelity == FidelityStatus.PASS and self.a_reduction_pct >= 80.0
