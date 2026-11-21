"""用人类语料复算基线阈值的脚本。

用法：
    python scripts/calibrate.py --input-dir ./corpus/human_fiction/ --output config/baseline_cache.json

原理：
    收集一批已知的人类小说段落，计算突发性、TTR、简化困惑度，
    然后统计分位数来确定人类分布带的合理上下界。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable


def collect_texts(directory: Path, extensions: tuple = (".txt", ".md")) -> list[str]:
    """递归收集目录下所有文本文件的内容。"""
    texts = []
    for ext in extensions:
        for p in directory.rglob(f"*{ext}"):
            try:
                texts.append(p.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[warn] 跳过 {p}: {e}", file=sys.stderr)
    return texts


def compute_stats(texts: list[str]):
    """对所有文本计算统计指标并返回聚合统计。"""
    from humanizer.metrics import compute_full_metrics
    import statistics

    burstiness_vals = []
    ttr_vals = []
    ppl_vals = []

    for text in texts:
        if len(text) < 100:
            continue  # 太短的文本没有统计意义
        snap = compute_full_metrics(text)
        burstiness_vals.append(snap.burstiness)
        ttr_vals.append(snap.ttr)
        ppl_vals.append(snap.perplexity_simple)

    def quantiles(vals: list[float]) -> dict:
        if not vals:
            return {}
        s = sorted(vals)
        n = len(s)
        return {
            "count": n,
            "mean": statistics.mean(s),
            "std": statistics.pstdev(s),
            "q10": s[int(n * 0.1)],
            "q25": s[int(n * 0.25)],
            "median": s[int(n * 0.5)],
            "q75": s[int(n * 0.75)],
            "q90": s[int(n * 0.9)],
            "min": s[0],
            "max": s[-1],
        }

    return {
        "burstiness": quantiles(burstiness_vals),
        "ttr": quantiles(ttr_vals),
        "perplexity_simple": quantiles(ppl_vals),
    }


def main():
    parser = argparse.ArgumentParser(description="Calibrate human baseline thresholds")
    parser.add_argument("--input-dir", required=True, help="包含人类小说文本的目录")
    parser.add_argument("--output", default="config/baseline_cache.json", help="输出 JSON 路径")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    if not input_dir.is_dir():
        print(f"错误：目录不存在 {input_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"正在扫描 {input_dir} ...")
    texts = collect_texts(input_dir)
    print(f"共收集到 {len(texts)} 个文本文件")

    if not texts:
        print("错误：未找到任何有效文本", file=sys.stderr)
        sys.exit(1)

    stats = compute_stats(texts)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"统计结果已写入 {output_path}")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
