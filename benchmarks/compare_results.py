"""从两份基准报告生成对比文档。

对比文档不手写：全部数字都从 `baseline.json` 与 `optimized.json` 读出来，
两份报告又都由 `run_benchmark.py` 生成，因此文档、JSON 和脚本三者结构一致。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

COUNTER_NOTES = {
    "pdf_open": (
        "基线是 0。计数器只统计 `_common.open_pdf`，"
        "而生产代码全部直接调用 `fitz.open`，等于完全没测。"
    ),
    "source_pdf_open": "同上。",
    "candidate_pdf_open": (
        "同上。修改后一次预检里候选被打开 2 次"
        "（待检 PDF 与影子副本各一次），"
        "由 `test_candidate_pdf_analysis_is_reused` 固定上界。"
    ),
    "sha256_file_read": "修改后增加的部分来自新增的字体证据哈希与批次计划哈希。",
}


def _pct(before: float, after: float) -> str:
    return f"{(after - before) / before * 100:+.1f}%" if before else "n/a"


def _spread(case: dict[str, Any], state: str) -> float:
    values = case[state]["seconds"]
    median = case[state]["median_seconds"]
    return (max(values) - min(values)) / median * 100 if median else 0.0


def build_comparison(baseline: dict[str, Any], optimized: dict[str, Any]) -> str:
    bp = baseline["provenance"]
    op = optimized["provenance"]
    lines = [
        "# 基准对比：审查前 vs 审查后",
        "",
        "本文件由 `benchmarks/compare_results.py` 从两份 JSON 报告生成，",
        "不手工整理。两份报告由同一个脚本 `benchmarks/run_benchmark.py`",
        "背靠背跑出，结构完全一致。",
        "",
        "语料是 `benchmarks/corpus.json` 描述的五类合成论文：单栏正文、双栏正文、",
        "复杂图表、图片密集、参考文献密集。合成语料只比较耗时与重复读取，",
        "不能替代真实论文的视觉抽查。",
        "",
        "## 口径",
        "",
        "| 项目 | 基线 | 修改后 |",
        "| --- | --- | --- |",
        f"| git 提交 | `{bp['git_commit'][:10]}` | `{op['git_commit'][:10]}` |",
        f"| renderer_build_id | `{bp['renderer_build_id']}` | "
        f"`{op['renderer_build_id']}` |",
        f"| 每种状态重复次数 | {baseline['repeats']} | {optimized['repeats']} |",
        f"| 运行时 1 分钟负载 | {bp['load_average_1m']} | {op['load_average_1m']} |",
        f"| CPU 核数 | {bp['cpu_count']} | {op['cpu_count']} |",
        f"| Python | {bp['python']} | {op['python']} |",
        "",
        "- **模型翻译未测量**。本次没有调用真实模型；译文是确定性伪译文，",
        "  只为触发同一批代码路径。模型标识、调用次数、重试次数、",
        "  输入/输出 Token 和翻译耗时在两份报告里都是 `null`，不做任何估算。",
        "- **翻译时间与 PDF 时间分开记**：`translation_stage_seconds` 只含初始化、",
        "  原文结构提取、批次编排与写回；`pdf_stage_seconds` 含试排、注册、QA、",
        "  作业校验、完整性审查与预检。",
        "- 两次运行的 `job.quality.selected_fonts` 对齐成同一组真实中文字体。",
        "  基线原本用拉丁字体排中文，不对齐就不是同一件事。",
        "",
        "## 端到端耗时（秒，中位数）",
        "",
        "| 案例 | 基线冷启动 | 修改后冷启动 | 变化 | 基线缓存 | 修改后缓存 | 变化 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for cb, co in zip(baseline["cases"], optimized["cases"], strict=True):
        lines.append(
            f"| {cb['id']} | {cb['cold']['median_seconds']:.3f} | "
            f"{co['cold']['median_seconds']:.3f} | "
            f"{_pct(cb['cold']['median_seconds'], co['cold']['median_seconds'])} | "
            f"{cb['warm']['median_seconds']:.3f} | "
            f"{co['warm']['median_seconds']:.3f} | "
            f"{_pct(cb['warm']['median_seconds'], co['warm']['median_seconds'])} |"
        )
    bt, ot = baseline["totals"], optimized["totals"]
    lines += [
        f"| **合计** | **{bt['cold_median_seconds']:.3f}** | "
        f"**{ot['cold_median_seconds']:.3f}** | "
        f"**{_pct(bt['cold_median_seconds'], ot['cold_median_seconds'])}** | "
        f"**{bt['warm_median_seconds']:.3f}** | "
        f"**{ot['warm_median_seconds']:.3f}** | "
        f"**{_pct(bt['warm_median_seconds'], ot['warm_median_seconds'])}** |",
        "",
        "### 单次运行内部的抖动",
        "",
        "| 案例 | 基线冷启动极差 | 修改后冷启动极差 |",
        "| --- | ---: | ---: |",
    ]
    for cb, co in zip(baseline["cases"], optimized["cases"], strict=True):
        lines.append(
            f"| {cb['id']} | {_spread(cb, 'cold'):.0f}% | "
            f"{_spread(co, 'cold'):.0f}% |"
        )
    lines += [
        "",
        "把抖动和版本间差异放在一起看：**本次不宣称任何提速**。",
        "本次性能工作的可验证结论在下一节，是重复读取的账终于对得上了。",
        "",
        "## 重复读取计数（冷启动，五个案例合计）",
        "",
        "| 计数器 | 基线 | 修改后 | 说明 |",
        "| --- | ---: | ---: | --- |",
    ]
    for name, note in COUNTER_NOTES.items():
        total_b = sum(
            case["cold"]["counters"].get(name, 0) for case in baseline["cases"]
        )
        total_o = sum(
            case["cold"]["counters"].get(name, 0) for case in optimized["cases"]
        )
        lines.append(f"| `{name}` | {total_b} | {total_o} | {note} |")
    lines += [
        "",
        "基线那一列的 0 不代表没有打开 PDF，代表打开了但没被记下来。",
        "这就是旧性能报告不能直接当作当前版本证据的原因。",
        "",
        "## 已知限制",
        "",
        "1. **模型翻译未测量**：没有真实模型端到端数据，不估算。",
        "2. `get_text_dict` / `get_text_blocks` / `get_text_plain` 三个计数器目前",
        "   只统计经过 `CandidateAnalysis` 的抽取。QA 与完整性审查内部直接调用",
        "   `page.get_text(...)` 的次数仍未计数，这一项尚未完成。",
        "3. 合成语料的 reference-heavy 案例在基线和修改后都触发页数扩张保护",
        "   （11 页 / 扩张比 1.833，上限 1.6），两侧行为一致，不是本次引入的回归。",
        "   它的耗时覆盖到排版搜索失败为止。",
        "4. 其余四个案例判定为 NEEDS_REPAIR：合成伪译文本来就过不了内容完整性",
        "   审查。全部阶段都已执行，因此耗时口径不受影响。",
        "5. 测量机不是空闲机器。要得到更硬的耗时结论，需要在空闲机器上重跑。",
        "",
        "## 复现",
        "",
        "```bash",
        "python3 benchmarks/make_benchmark_jobs.py",
        "python3 benchmarks/run_benchmark.py --repeats 5 --label optimized \\",
        "  --output benchmarks/results/optimized.json",
        "python3 benchmarks/compare_results.py",
        "python3 -m pytest -q tests/test_benchmark_provenance.py",
        "```",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=here / "results" / "baseline.json",
    )
    parser.add_argument(
        "--optimized",
        type=Path,
        default=here / "results" / "optimized.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=here / "results" / "comparison.md",
    )
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    optimized = json.loads(args.optimized.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        build_comparison(baseline, optimized),
        encoding="utf-8",
    )
    print(f"对比文档已写入: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
