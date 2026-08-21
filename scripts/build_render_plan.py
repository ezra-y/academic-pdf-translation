"""从元素清单生成渲染计划 render_plan.json。"""

from __future__ import annotations

import argparse
from pathlib import Path

from academic_pdf_translation.analysis.element_overrides import (
    apply_all,
    load_overrides,
)
from academic_pdf_translation.analysis.source_elements import (
    analyze_job_elements,
)
from academic_pdf_translation.contracts.migration import derive_quality_mode
from academic_pdf_translation.planning.render_plan import (
    build_figure_inventory,
    build_render_plan,
    write_plan,
)

from _common import SkillError, import_fitz, load_json, write_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path)
    parser.add_argument(
        "--write-figure-inventory",
        action="store_true",
        help="同时把程序派生的图表清单写回 figure_inventory.json",
    )
    args = parser.parse_args()
    job_dir = args.job_dir.resolve()
    try:
        job = load_json(job_dir / "job.json")
        quality_mode = derive_quality_mode(job)
        fitz = import_fitz()
        inventory = analyze_job_elements(
            job_dir,
            pymupdf_version=getattr(fitz, "VersionBind", "0"),
        )
        apply_all(inventory, load_overrides(job_dir))
        plan = build_render_plan(inventory, quality_mode)
        path = write_plan(job_dir, plan)
        if args.write_figure_inventory:
            write_json(
                job_dir / "figure_inventory.json",
                build_figure_inventory(inventory, plan),
            )
    except (SkillError, OSError, ValueError) as exc:
        print(f"错误: {exc}")
        return 1

    print(f"渲染计划已写入: {path}")
    print(f"质量档位: {plan.quality_mode}")
    print(f"必需元素: {plan.required_elements}")
    print(f"已安排: {plan.planned_elements}  合法省略: {plan.omitted_elements}")
    print(f"未解决: {plan.unresolved_elements}")
    print(f"完整（程序计算）: {plan.complete}")
    counts: dict[str, int] = {}
    for item in plan.elements:
        counts[item.strategy] = counts.get(item.strategy, 0) + 1
    print("策略分布:")
    for name, count in sorted(counts.items()):
        print(f"  {name:<48}{count:>4}")
    if plan.problems:
        print("问题:")
        for problem in plan.problems:
            print(f"  - {problem}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
