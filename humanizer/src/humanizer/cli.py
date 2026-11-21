"""CLI 入口 —— 基于 Typer 的命令行界面。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from humanizer.io import read_text_strict
from humanizer.models import Mode
from humanizer.pipeline import run_pipeline
from humanizer.rules.engine import RuleEngine
from humanizer.stages.diagnose import run_diagnose

app = typer.Typer(
    name="humanizer",
    help="「去AI味」中文文本改写程序",
    add_completion=False,
)

console = Console()

# Fix Windows console encoding for Unicode characters
import sys as _sys

if _sys.platform == "win32":
    import io as _io
    _original_stdout = _sys.stdout
    _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", errors="replace")


@app.command("detect")
def detect(
    input_path: str = typer.Argument(..., help="输入 .txt / .md 文件路径"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示详细规则命中列表"),
) -> None:
    """诊断模式：只报告病灶，不改写文本。"""
    path = Path(input_path)
    if not path.exists():
        console.print(f"[red]错误：文件不存在 {path}[/red]")
        raise SystemExit(1)

    text = read_text_strict(path)
    engine = RuleEngine()
    report = run_diagnose(text, mode=Mode.DETECT, rule_engine=engine)

    _print_detect_report(report, verbose=verbose)


@app.command("clean")
def clean(
    input_path: str = typer.Argument(..., help="输入 .txt / .md 文件路径"),
    output: str | None = typer.Option(None, "--output", "-o", help="输出文件路径（默认自动生成）"),
    mock_llm: bool = typer.Option(False, "--mock-llm", help="使用 Mock LLM（离线测试，不联网）"),
) -> None:
    """清理模式：语言层改写，不改变剧情和事实。"""
    _run_mode(input_path, output, Mode.CLEAN, mock_llm=mock_llm)


@app.command("refactor")
def refactor(
    input_path: str = typer.Argument(..., help="输入 .txt / .md 文件路径"),
    output: str | None = typer.Option(None, "--output", "-o", help="输出文件路径"),
    max_actions: int = typer.Option(3, "--max-actions", help="最大结构动作数（refactor 专用）"),
    mock_llm: bool = typer.Option(False, "--mock-llm", help="使用 Mock LLM（离线测试）"),
) -> None:
    """重构模式：在清理基础上允许调整段落节奏和叙事结构。"""
    _run_mode(input_path, output, Mode.REFACTOR, max_actions=max_actions, mock_llm=mock_llm)


@app.command("metrics")
def metrics_cmd(
    input_path: str = typer.Argument(..., help="输入 .txt / .md 文件路径"),
) -> None:
    """只看统计指标，不做规则扫描。"""
    path = Path(input_path)
    if not path.exists():
        console.print(f"[red]错误：文件不存在 {path}[/red]")
        raise SystemExit(1)

    text = read_text_strict(path)
    from humanizer.metrics import compute_full_metrics
    snap = compute_full_metrics(text)
    _print_metrics(snap)


def _run_mode(
    input_path: str,
    output: str | None,
    mode: Mode,
    max_actions: int = 0,
    mock_llm: bool = False,
) -> None:
    """内部：运行指定模式的完整管线。"""
    path = Path(input_path)
    if not path.exists():
        console.print(f"[red]错误：文件不存在 {path}[/red]")
        raise SystemExit(1)

    out = Path(output) if output else None
    log = run_pipeline(
        input_path=path,
        output_path=out,
        mode=mode,
        max_structural_actions=max_actions,
        use_mock_llm=mock_llm,
    )

    # 打印结果摘要
    _print_pipeline_summary(log)


def _print_detect_report(report, verbose: bool = False) -> None:
    """格式化输出 detect 模式的诊断报告。"""
    m = report.metrics
    title = f"诊断报告 · {report.source_file or 'stdin'}"
    console.rule(title, style="bold cyan")

    console.print(f"长度：{report.text_length:,} 字")
    console.print(f"句子数：{m.sentence_count if m else '?'}")

    if m:
        b_val = m.burstiness
        b_label = _burstiness_label(b_val)
        console.print(f"突发性：{b_val:.4f}  {b_label}")

        ttr_val = m.ttr
        ttr_label = "正常范围偏低" if ttr_val < 0.30 else "正常"
        console.print(f"TTR：{ttr_val:.4f}  {ttr_label}")

        ppl_val = m.perplexity_simple
        ppl_label = "偏可预测" if ppl_val < 60 else "正常"
        console.print(f"简化困惑度：{ppl_val:.2f}  {ppl_label}")

    console.print()

    # 规则命中摘要
    counts = report.hit_counts_by_severity()
    console.print(f"规则命中：A={counts['A']}  B={counts['B']}  C={counts['C']}")

    if verbose and report.rule_hits:
        console.print("\n命中详情：")
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("行", justify="right", width=6)
        table.add_column("等级", width=4)
        table.add_column("规则 ID", width=18)
        table.add_column("匹配文本", max_width=50)
        table.add_column("建议", max_width=40)
        for h in report.rule_hits[:50]:
            table.add_row(
                str(h.line_number or "?"),
                h.severity,
                h.rule_id,
                h.matched_text[:50],
                (h.suggestion or "")[:40],
            )
        if len(report.rule_hits) > 50:
            table.add_row("", "", f"... 还有 {len(report.rule_hits) - 50} 条命中", "", "")
        console.print(table)

    # 建议
    suggestions = []
    if m and m.burstiness < 0.5:
        suggestions.append("突发性过低，建议在改写中强制引入短句")
    if m and m.ttr < 0.30:
        suggestions.append("TTR 偏低，可增加词汇变体")
    if counts["A"] > 0:
        suggestions.append(f"优先处理 {counts['A']} 处 A 级命中")
    if suggestions:
        console.print("\n[bold yellow]建议：[/bold yellow]" + "；".join(suggestions))
    console.print()


def _burstiness_label(value: float) -> str:
    if value < 0.3:
        return "→ 过于均匀（AI典型）"
    if value >= 0.5:
        return "→ 人类分布带内 ✓"
    return "→ 接近边界"


def _verdict_label(v) -> str:
    """Convert verdict (enum or str) to display label."""
    if hasattr(v, 'value'):
        v = v.value
    return {
        "too_flat": "[red]过于均匀[/red]",
        "too_diverse": "[yellow]偏高[/yellow]",
        "too_predictable": "[red]偏可预测[/red]",
        "too_unpredictable": "[yellow]偏高[/yellow]",
        "within_human_band": "[green]人类分布带内[/green]",
        "borderline": "[yellow]接近边界[/yellow]",
        "na": "",
    }.get(str(v), "")


def _print_metrics(snapshot) -> None:
    console.rule("统计指标", style="bold blue")
    console.print(f"文本长度：{snapshot.text_length} 字符")
    console.print(f"句子数：{snapshot.sentence_count}")
    console.print(f"突发性（CV）：{snapshot.burstiness:.4f}  ", end="")
    console.print(_verdict_label(snapshot.burstiness_verdict))
    console.print(f"TTR：{snapshot.ttr:.4f}  ", end="")
    console.print(_verdict_label(snapshot.ttr_verdict))
    console.print(f"简化困惑度：{snapshot.perplexity_simple:.2f}  ", end="")
    console.print(_verdict_label(snapshot.perplexity_verdict))
    console.print()


def _print_pipeline_summary(log: dict) -> None:
    """打印管线执行的最终汇总。"""
    console.rule("改写完成", style="bold green")
    console.print(f"输入：{log.get('input', '?')}")
    console.print(f"输出：{log.get('output_path', '未知')}")
    console.print(f"模式：{log.get('mode', '?')}")
    console.print(f"总耗时：{log.get('duration_seconds', 0):.2f}s\n")

    stages = log.get("stages", {})
    if "verify" in stages:
        v = stages["verify"]
        console.print(f"保真性：[{'green' if v['fidelity'] == 'pass' else 'red'}]{v['fidelity']}[/]")
        console.print(f"A 级命中减少：{v['a_reduction_before']} → {v['a_reduction_after']} ({v['a_reduction_pct']:.1f}%)")
        if v.get("issues"):
            for issue in v["issues"]:
                console.print(f"  [yellow]⚠ {issue}[/yellow]")

    if "rewrite" in stages:
        r = stages["rewrite"]
        console.print(f"\n改写前后长度：{r['original_length']} → {r['rewritten_length']}")
        console.print(f"改写耗时：{r['duration_s']:.2f}s")

    if "diagnose" in stages:
        d = stages["diagnose"]
        console.print(f"\n诊断：A={d['a_hits']} B={d['b_hits']} C={d['c_hits']}")
        if d.get("metrics"):
            met = d["metrics"]
            console.print(f"  突发性：{met.get('burstiness', 'N/A')}  TTR：{met.get('ttr', 'N/A')}")

    console.print()


# 让 typer 可以直接作为脚本运行
if __name__ == "__main__":
    app()
