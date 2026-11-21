"""主管线编排 —— 将各 Stage 串联起来，协调数据流。"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from humanizer.config import get_settings
from humanizer.io import read_text_strict, write_text
from humanizer.metrics import compute_full_metrics
from humanizer.models import Mode
from humanizer.rules.engine import RuleEngine
from humanizer.stages.diagnose import run_diagnose
from humanizer.stages.plan import run_plan
from humanizer.stages.precheck import run_precheck
from humanizer.stages.rewrite import run_rewrite
from humanizer.stages.verify import run_verify

logger = logging.getLogger(__name__)


def run_pipeline(
    input_path: Path,
    output_path: Path | None = None,
    mode: Mode = Mode.CLEAN,
    max_structural_actions: int = 0,
    use_mock_llm: bool = False,
    rule_engine: RuleEngine | None = None,
    llm_client=None,
) -> dict:
    """
    端到端运行 Humanizer 管线。

    Args:
        input_path: 输入文件路径
        output_path: 输出文件路径（None 则自动生成）
        mode: 运行模式（detect / clean / refactor）
        max_structural_actions: refactor 模式下允许的最大结构动作数
        use_mock_llm: 是否使用 Mock LLM（离线测试）
        rule_engine: 测试注入的规则引擎
        llm_client: 测试注入的 LLM 客户端

    Returns:
        结构化运行日志（JSON 序列化友好）
    """
    t_start = time.time()
    pipeline_log: dict = {
        "input": str(input_path),
        "output": str(output_path) if output_path else None,
        "mode": mode.value,
        "stages": {},
        "duration_seconds": 0.0,
    }

    settings = get_settings()

    # ── Stage 0: 预检 ──
    t0 = time.time()
    raw_bytes = read_text_strict(input_path).encode("utf-8")
    precheck = run_precheck(raw_bytes, mode_str=mode.value)
    text = raw_bytes.decode(precheck.encoding)
    pipeline_log["stages"]["precheck"] = {
        "encoding": precheck.encoding,
        "format_valid": precheck.format_valid,
        "mode": precheck.mode.value,
        "duration_s": round(time.time() - t0, 3),
    }
    if not precheck.format_valid:
        logger.warning("文件格式校验不通过，继续执行但请检查 %s", input_path)

    # ── Stage 1: 诊断 ──
    t1 = time.time()
    engine = rule_engine or RuleEngine()
    diagnosis = run_diagnose(text, mode=mode, rule_engine=engine)
    a_before = len(diagnosis.a_hits)
    pipeline_log["stages"]["diagnose"] = {
        "text_length": diagnosis.text_length,
        "a_hits": a_before,
        "b_hits": len(diagnosis.b_hits),
        "c_hits": len(diagnosis.c_hits),
        "metrics": diagnosis.metrics.summary_dict() if diagnosis.metrics else None,
        "duration_s": round(time.time() - t1, 3),
    }

    # detect 模式：只做诊断，直接输出报告
    if mode == Mode.DETECT:
        pipeline_log["stages"]["diagnose"]["report"] = _diagnosis_to_dict(diagnosis)
        pipeline_log["duration_seconds"] = round(time.time() - t_start, 3)
        return pipeline_log

    # ── Stage 2: 规划 ──
    t2 = time.time()
    plan = run_plan(
        diagnosis,
        mode=mode,
        max_structural_actions=max_structural_actions,
    )
    pipeline_log["stages"]["plan"] = {
        "needs_llm": plan.needs_llm,
        "deferred_count": len(plan.deferred_hits),
        "structural_actions": plan.structural_actions,
        "duration_s": round(time.time() - t2, 3),
    }

    # ── Stage 3: 改写 ──
    t3 = time.time()
    # 决定使用哪个 LLM 客户端
    effective_llm = llm_client
    if effective_llm is None and not use_mock_llm:
        # 尝试创建真实 LLM 客户端
        try:
            from humanizer.llm.client import LLMAgent
            effective_llm = LLMAgent(settings)
        except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
            logger.warning("无法初始化 LLM 客户端，启用 mock 模式：%s", exc)
            from humanizer.llm.client import MockLLMAgent
            effective_llm = MockLLMAgent()
    elif effective_llm is None and use_mock_llm:
        from humanizer.llm.client import MockLLMAgent
        effective_llm = MockLLMAgent()

    rewritten_text, _verify_results = run_rewrite(
        text,
        plan,
        mode=mode,
        rule_engine=engine,
        llm_client=effective_llm,
    )
    pipeline_log["stages"]["rewrite"] = {
        "original_length": len(text),
        "rewritten_length": len(rewritten_text),
        "duration_s": round(time.time() - t3, 3),
    }

    # ── Stage 4: 验证 ──
    t4 = time.time()
    # 对改写后文本重新扫描规则，获取 a_after
    remaining_hits = engine.scan(rewritten_text)
    a_after = len([h for h in remaining_hits if h.severity == "A"])
    metrics_after = compute_full_metrics(rewritten_text)

    report = run_verify(
        original_text=text,
        rewritten_text=rewritten_text,
        metrics_before=diagnosis.metrics,
        metrics_after=metrics_after,
        a_before=a_before,
        a_after=a_after,
    )
    pipeline_log["stages"]["verify"] = {
        "fidelity": report.overall_fidelity.value,
        "a_reduction_before": a_before,
        "a_reduction_after": a_after,
        "a_reduction_pct": round(report.a_reduction_pct, 1),
        "issues": report.fidelity_issues,
        "metrics_after": report.metrics_after.summary_dict() if report.metrics_after else None,
        "duration_s": round(time.time() - t4, 3),
    }

    # ── 写入输出 ──
    out_path = output_path or _default_output_path(input_path, mode)
    write_text(out_path, rewritten_text, encoding=settings.output.encoding)
    pipeline_log["output_path"] = str(out_path)
    pipeline_log["duration_seconds"] = round(time.time() - t_start, 3)

    logger.info("管线完成：输入=%s, 输出=%s, 耗时=%.2fs",
                input_path, out_path, pipeline_log["duration_seconds"])
    return pipeline_log


def _diagnosis_to_dict(report) -> dict:
    """将 DiagnosisReport 序列化为字典（供 detect 模式输出）。"""
    return {
        "text_length": report.text_length,
        "a_hits": len(report.a_hits),
        "b_hits": len(report.b_hits),
        "c_hits": len(report.c_hits),
        "metrics": report.metrics.summary_dict() if report.metrics else None,
        "hits_preview": [
            {
                "rule_id": h.rule_id,
                "severity": h.severity,
                "start": h.start,
                "matched_text": h.matched_text[:50],
                "line": h.line_number,
            }
            for h in report.rule_hits[:20]
        ],
    }


def _default_output_path(input_path: Path, mode: Mode) -> Path:
    """根据输入路径和模式推断默认输出路径。"""
    stem = input_path.stem
    ext = input_path.suffix
    suffix_map = {Mode.CLEAN: "_clean", Mode.REFACTOR: "_refactored"}
    suffix = suffix_map.get(mode, "_out")
    return input_path.with_name(f"{stem}{suffix}{ext}")
