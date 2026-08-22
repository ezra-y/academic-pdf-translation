"""首次交付的唯一入口：生成、核查、最多返修一次、再核查，给一个结论。

之前每一步都有自己的脚本，各自打印一份"看起来还行"。合起来就出事了：
生成脚本说 READY_TO_REGISTER，核查脚本说少了一张图，谁也不为最后那个
"能不能给读者"负责。这个脚本负责。

退出码就是结论，方便脚本串联：
    0 = 可以交付
    2 = 交给人处理（机器已经修过一轮，剩下的需要判断）
    1 = 停下别交（生成失败，或者返修把别的地方弄坏了）
"""

from __future__ import annotations

import sys
from pathlib import Path

# 按 README 的写法 `python3 scripts/X.py` 运行时，sys.path 里只有 scripts/，
# 没有仓库根，academic_pdf_translation 包就 import 不到。先把根加进去。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

from academic_pdf_translation.analysis.element_overrides import (  # noqa: E402
    apply_all,
    load_overrides,
)
from academic_pdf_translation.analysis.source_elements import (  # noqa: E402
    analyze_job_elements,
)
from academic_pdf_translation.contracts.migration import (  # noqa: E402
    derive_quality_mode,
)
from academic_pdf_translation.delivery.first_delivery import (  # noqa: E402
    STATUS_BLOCKED,
    STATUS_DELIVERED,
    STATUS_HANDOVER,
    FirstDeliveryError,
    format_result,
    run_first_delivery,
)
from academic_pdf_translation.planning.render_plan import (  # noqa: E402
    build_render_plan,
    write_plan,
)
from academic_pdf_translation.verify.repair import (  # noqa: E402
    ACTION_PRESERVE_REGION,
    RepairPlan,
)

from _common import (  # noqa: E402
    SkillError,
    import_fitz,
    load_json,
    write_json,
)
from build_first_candidate import build_first_candidate  # noqa: E402
from build_render_plan import (  # noqa: E402
    FORCED_STRATEGIES_FILE,
    load_forced_strategies,
)

EXIT_CODES = {
    STATUS_DELIVERED: 0,
    STATUS_HANDOVER: 2,
    STATUS_BLOCKED: 1,
}

#: 返修计划里只有这些动作能落到渲染计划上。其余的（比如图题绑组）
#: 由页面合成器按绑定组处理，不经过这里。
STRATEGY_ACTIONS = frozenset({ACTION_PRESERVE_REGION})


def _job_inputs(job_dir: Path) -> tuple[list, list, list]:
    elements = load_json(job_dir / "source_elements.json").get("elements") or []
    units = load_json(job_dir / "translation.json").get("units") or []
    bindings = load_json(job_dir / "unit_bindings.json").get("bindings") or []
    if not elements:
        raise SkillError(f"{job_dir} 的 source_elements.json 里没有元素")
    if not bindings:
        raise SkillError(
            f"{job_dir} 的 unit_bindings.json 里没有绑定；"
            "没有绑定就没法按译文定位元素"
        )
    return (elements, units, bindings)


def rebuild_render_plan(job_dir: Path) -> dict[str, str]:
    """按返修指定的降级重算渲染计划，并返回实际生效的降级。"""

    forced = load_forced_strategies(job_dir)
    fitz = import_fitz()
    inventory = analyze_job_elements(
        job_dir, pymupdf_version=getattr(fitz, "VersionBind", "0")
    )
    apply_all(inventory, load_overrides(job_dir))
    plan = build_render_plan(
        inventory,
        derive_quality_mode(load_json(job_dir / "job.json")),
        forced_strategies=forced,
    )
    write_plan(job_dir, plan)
    applied = {
        item.element_id: item.strategy
        for item in plan.elements
        if item.element_id in forced and item.strategy == forced[item.element_id]
    }
    write_json(
        job_dir / "repair" / "applied_strategies.json",
        {
            "requested": forced,
            "applied": applied,
            "plan_problems": list(plan.problems),
        },
    )
    return applied


def make_builder(job_dir: Path, output: Path | None):
    """把真实的候选生成器包成交付流程要的 build(round_index)。"""

    def build(round_index: int) -> Path:
        label = "first" if round_index == 0 else f"repair-{round_index}"
        if round_index > 0:
            # 返修那一轮必须先重算渲染计划，降级指令才有机会生效。
            rebuild_render_plan(job_dir)
        report = build_first_candidate(
            job_dir, output, attempt_label=label
        )
        # 生成器在输入没准备好时会走 BLOCKED_BEFORE_PREFLIGHT，
        # 那一条分支的 candidate_pdf 是 None。把它的 issues 原样带出来，
        # 比只说一句"没有产出候选"有用得多。
        candidate = report.get("candidate_pdf")
        if not candidate:
            issues = report.get("issues") or []
            detail = "；".join(str(item) for item in issues[:3])
            raise SkillError(
                f"{label} 轮没有产出候选 PDF"
                f"（{report.get('status')}"
                f"{' / ' + report.get('blocked_stage', '') if report.get('blocked_stage') else ''}）"
                + (f": {detail}" if detail else "")
            )
        return Path(candidate)

    return build


def make_repair_applier(job_dir: Path):
    """把返修计划落成渲染计划能读的降级指定。"""

    def apply_repair(plan: RepairPlan) -> None:
        forced = {
            item.element_id: item.action
            for item in plan.actions
            if item.action in STRATEGY_ACTIONS and item.element_id
        }
        write_json(
            job_dir / FORCED_STRATEGIES_FILE,
            {
                "schema_version": "1.0",
                "round_index": plan.round_index,
                "forced_strategies": forced,
                "actions": [item.as_dict() for item in plan.actions],
                "manual": [item.as_dict() for item in plan.manual],
            },
        )

    return apply_repair


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--delivery-dir",
        type=Path,
        default=None,
        help="证据写到哪里，默认 <job_dir>/delivery",
    )
    parser.add_argument(
        "--page-budget",
        type=int,
        default=6,
        help="最多渲染几页给人细看",
    )
    parser.add_argument(
        "--no-repair",
        action="store_true",
        help="只核查，不执行那一轮返修",
    )
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args()

    job_dir = args.job_dir.resolve()
    delivery_dir = (args.delivery_dir or job_dir / "delivery").resolve()
    try:
        elements, units, bindings = _job_inputs(job_dir)
        result = run_first_delivery(
            job_dir / "source.pdf",
            elements,
            units,
            bindings,
            build=make_builder(job_dir, args.output),
            apply_repair=(
                None if args.no_repair else make_repair_applier(job_dir)
            ),
            output_dir=delivery_dir,
            page_budget=args.page_budget,
        )
    except (SkillError, FirstDeliveryError, OSError, ValueError) as exc:
        print(f"错误: {exc}")
        return 1

    write_json(delivery_dir / "delivery.json", result.as_dict())
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_result(result))
    return EXIT_CODES.get(result.status, 1)


if __name__ == "__main__":
    raise SystemExit(main())
