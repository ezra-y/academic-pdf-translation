"""统一首次交付入口。

之前每一步都有自己的脚本，各自打印一份"看起来还行"。合起来就出事了：
生成脚本说 READY_TO_REGISTER，核查脚本说少了一张图，谁也不为最后那个
"能不能给读者"负责。

这里只给**一个**结论，而且只有三种：

- ``delivered``：核查全过。
- ``handover``：还有问题，但机器已经修过一轮，剩下的必须人来看。
- ``blocked``：返修把别的地方弄坏了，或者生成本身失败了。停下，别交。

三条硬规矩写在流程里，不靠调用方自觉：

1. **最多重建一次。** 第二轮返修计划会被 :mod:`repair` 直接拒绝，
   这里再加一道断言，防止有人绕过去。
2. **"通过"是算出来的。** 只有结构对账 passed 且映射核查零问题才算交付，
   任何人往结果里写 ``status`` 都不作数。
3. **证据落盘。** 映射、对账、视觉检查计划、返修计划、渲染出来的页，
   全部写进交付目录。没有证据的结论不算结论。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from academic_pdf_translation.delivery.gates import (
    GATE_REPAIR,
    check_build_gate,
)
from academic_pdf_translation.delivery.models import (
    BUILD_READY,
    BuildOutcome,
)
from academic_pdf_translation.verify.candidate_mapping import (
    CandidateMapping,
    build_mapping,
    element_texts_from_units,
    verify_mapping,
)
from academic_pdf_translation.verify.repair import (
    MAX_REPAIR_ROUNDS,
    RepairPlan,
    compare_rounds,
    plan_repair,
)
from academic_pdf_translation.verify.structural_audit import (
    StructuralAudit,
    audit_structure,
)
from academic_pdf_translation.verify.visual_review import (
    DEFAULT_PAGE_BUDGET,
    VisualReviewPlan,
    build_review_plan,
    render_review_pages,
)

SCHEMA_VERSION = "1.0"

STATUS_DELIVERED = "delivered"
STATUS_HANDOVER = "handover"
STATUS_BLOCKED = "blocked"

#: 返修跑完却一个字没改，说明降级根本没落到生成器上。
#: 这比"修了没修好"严重得多，必须单独报出来。
REPAIR_MADE_NO_DIFFERENCE = (
    "返修重建出来的候选与返修前内容完全相同——降级指令没有落到生成器上，这一轮返修等于没跑"
)

STAGE_BUILD = "build"
STAGE_MAP = "map"
STAGE_AUDIT = "audit"
STAGE_REVIEW = "visual-review"
STAGE_REPAIR = "repair"
STAGE_REBUILD = "rebuild"
STAGE_REVERIFY = "re-verify"


class FirstDeliveryError(RuntimeError):
    """首次交付流程本身出了问题。"""


@dataclass
class StageRecord:
    """一步的结果。写清楚做了什么、成没成、凭什么。"""

    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeliveryResult:
    """一次首次交付的完整结论与证据。"""

    schema_version: str = SCHEMA_VERSION
    status: str = STATUS_BLOCKED
    rebuilds: int = 0
    stages: list[StageRecord] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    manual_items: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)
    candidate_path: str = ""
    #: 每一轮生成的完整构建状态（含构建报告哈希），一轮一条。
    builds: list[dict[str, Any]] = field(default_factory=list)

    @property
    def delivered(self) -> bool:
        return self.status == STATUS_DELIVERED

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "delivered": self.delivered,
            "rebuilds": self.rebuilds,
            "candidate_path": self.candidate_path,
            "stages": [item.as_dict() for item in self.stages],
            "problem_count": len(self.problems),
            "problems": list(self.problems),
            "manual_count": len(self.manual_items),
            "manual_items": list(self.manual_items),
            "evidence": dict(self.evidence),
            "builds": list(self.builds),
        }


def _open(path: Path) -> Any:
    import fitz

    if not Path(path).is_file():
        raise FirstDeliveryError(f"PDF 不存在: {path}")
    return fitz.open(path)


def candidate_content_hash(path: Path) -> str:
    """候选的内容哈希。

    用页面文字、绘图对象数量、图像数量算，不用文件字节——PDF 里有时间戳，
    字节每次都不一样，内容却可能一模一样。要判断"返修到底改没改东西"，
    只能看内容。
    """

    import hashlib

    digest = hashlib.sha256()
    document = _open(path)
    for index in range(document.page_count):
        page = document[index]
        digest.update(page.get_text("text").encode("utf-8"))
        digest.update(str(len(page.get_drawings())).encode("ascii"))
        digest.update(str(len(page.get_images())).encode("ascii"))
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return str(path)


@dataclass
class VerifyRound:
    """一轮核查的三件产物。"""

    mapping: CandidateMapping
    audit: StructuralAudit
    review: VisualReviewPlan
    mapping_problems: list[str]

    @property
    def clean(self) -> bool:
        """通过与否是算出来的，不看任何人写进来的字段。"""

        return self.audit.passed and not self.mapping_problems


def verify_candidate(
    source_document: Any,
    candidate_path: Path,
    elements: list[dict[str, Any]],
    *,
    element_texts: dict[str, str],
    output_dir: Path,
    label: str,
    page_budget: int = DEFAULT_PAGE_BUDGET,
    render_pages: bool = True,
) -> tuple[VerifyRound, dict[str, str]]:
    """对一份候选跑完阶段 9、10、11，并把证据落盘。"""

    candidate = _open(candidate_path)
    mapping = build_mapping(
        source_document, candidate, elements, element_texts=element_texts
    )
    audit = audit_structure(mapping, elements)
    review = build_review_plan(mapping, audit, page_budget=page_budget)
    if render_pages and review.selected:
        render_review_pages(candidate, review, output_dir / f"{label}-pages")

    evidence = {
        f"{label}-mapping": _write_json(
            output_dir / f"{label}-mapping.json", mapping.as_dict()
        ),
        f"{label}-audit": _write_json(
            output_dir / f"{label}-audit.json", audit.as_dict()
        ),
        f"{label}-review": _write_json(
            output_dir / f"{label}-review.json", review.as_dict()
        ),
    }
    return (
        VerifyRound(
            mapping=mapping,
            audit=audit,
            review=review,
            mapping_problems=verify_mapping(mapping),
        ),
        evidence,
    )


def _collect_problems(round_result: VerifyRound) -> list[str]:
    return list(round_result.audit.problems) + list(
        round_result.mapping_problems
    )


def _as_outcome(value: Any) -> BuildOutcome:
    """把构建回调的返回值规整成 :class:`BuildOutcome`。

    正式调用方（CLI）必须返回带真实状态的 ``BuildOutcome``。裸路径
    只留给测试里的合成候选用——返回裸路径等于调用方**显式声明**这一轮
    是 READY_TO_REGISTER，声明错了责任在调用方，不在门槛。
    """

    if isinstance(value, BuildOutcome):
        return value
    return BuildOutcome(status=BUILD_READY, candidate_path=Path(value))


def _record_build(
    result: DeliveryResult, outcome: BuildOutcome, output_dir: Path, label: str
) -> None:
    """构建状态与报告哈希落进结论和证据，谁也删不掉。"""

    record = outcome.as_dict()
    record["report_sha256"] = outcome.report_sha256()
    result.builds.append(record)
    result.evidence[f"{label}-build"] = _write_json(
        output_dir / f"{label}-build.json", record
    )


def run_first_delivery(
    source_path: Path,
    elements: list[dict[str, Any]],
    units: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    *,
    build: Callable[[int], BuildOutcome | Path],
    output_dir: Path,
    apply_repair: Callable[[RepairPlan], None] | None = None,
    page_budget: int = DEFAULT_PAGE_BUDGET,
    render_pages: bool = True,
) -> DeliveryResult:
    """跑完首次交付，给出唯一结论。

    ``build(round_index)`` 由调用方提供，返回这一轮的 :class:`BuildOutcome`。
    交付流程不关心是谁生成的，但**关心生成器自己怎么说**：生成器说
    BLOCKED，这里就是 blocked，候选文件存在与否都改变不了这一点。
    """

    if not elements:
        raise FirstDeliveryError("元素清单为空，无法交付")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = DeliveryResult()
    element_texts = element_texts_from_units(elements, units, bindings=bindings)
    source_document = _open(Path(source_path))

    try:
        outcome = _as_outcome(build(0))
    except Exception as exc:  # noqa: BLE001 - 生成失败要变成结论，不是崩溃
        result.stages.append(StageRecord(STAGE_BUILD, False, f"生成失败: {exc}"))
        result.problems.append(f"候选生成失败: {exc}")
        result.status = STATUS_BLOCKED
        return result

    _record_build(result, outcome, output_dir, "round-1")
    if outcome.candidate_path is not None:
        # 候选留作证据——它是不是"能交付的候选"由门槛说了算。
        result.candidate_path = str(outcome.candidate_path)

    build_gate = check_build_gate(outcome)
    result.stages.append(
        StageRecord(
            STAGE_BUILD,
            build_gate.passed,
            f"生成器状态 {outcome.status}"
            + (
                f"，候选 {outcome.candidate_path.name}"
                if outcome.candidate_path is not None
                else "，没有候选文件"
            ),
        )
    )
    if build_gate.blocked:
        # 生成器自己说不行：不做候选映射，不生成视觉结论，立即停。
        result.problems = list(build_gate.reasons)
        result.status = STATUS_BLOCKED
        return result

    build_needs_repair = build_gate.verdict == GATE_REPAIR
    if build_needs_repair:
        result.problems.extend(build_gate.reasons)
        if (
            outcome.candidate_path is None
            or not outcome.candidate_path.is_file()
        ):
            result.problems.append(
                "生成器要求返修却没有留下候选文件，无从核查，停下"
            )
            result.status = STATUS_BLOCKED
            return result

    candidate_path = Path(outcome.candidate_path)

    first, evidence = verify_candidate(
        source_document,
        candidate_path,
        elements,
        element_texts=element_texts,
        output_dir=output_dir,
        label="round-1",
        page_budget=page_budget,
        render_pages=render_pages,
    )
    result.evidence.update(evidence)
    result.stages.append(
        StageRecord(
            STAGE_MAP,
            not first.mapping_problems,
            f"{len(first.mapping.located)}/{len(first.mapping.locations)} 个元素定位到候选",
        )
    )
    result.stages.append(
        StageRecord(
            STAGE_AUDIT,
            first.audit.passed,
            f"{len(first.audit.problems)} 条结构问题",
        )
    )
    result.stages.append(
        StageRecord(
            STAGE_REVIEW,
            True,
            f"挑出 {len(first.review.selected)} 页待人工细看，"
            f"另有 {len(first.review.skipped)} 页超预算",
        )
    )

    if first.clean:
        if build_needs_repair:
            # 核查层没查出可修的点，但生成器自己说要修——两边说法对不上，
            # 这种矛盾要人看，不能当没听见直接交付。
            result.status = STATUS_HANDOVER
            return result
        result.status = STATUS_DELIVERED
        return result

    repair = plan_repair(first.mapping, first.audit, round_index=0)
    result.evidence["repair-plan"] = _write_json(
        output_dir / "repair-plan.json", repair.as_dict()
    )
    result.manual_items = [item.as_dict() for item in repair.manual]

    if not repair.actions:
        result.stages.append(
            StageRecord(
                STAGE_REPAIR, False, "没有机器能安全执行的返修动作"
            )
        )
        result.problems = result.problems + _collect_problems(first)
        result.status = STATUS_HANDOVER
        return result

    result.stages.append(
        StageRecord(
            STAGE_REPAIR,
            True,
            f"{len(repair.actions)} 条降级/重排动作，"
            f"{len(repair.manual)} 条交给人",
        )
    )

    if apply_repair is None:
        result.stages.append(
            StageRecord(
                STAGE_REBUILD, False, "调用方没有提供返修执行器，不重建"
            )
        )
        result.problems = result.problems + _collect_problems(first)
        result.status = STATUS_HANDOVER
        return result

    try:
        apply_repair(repair)
        repaired_outcome = _as_outcome(build(1))
    except Exception as exc:  # noqa: BLE001 - 返修失败同样要变成结论
        result.stages.append(
            StageRecord(STAGE_REBUILD, False, f"返修重建失败: {exc}")
        )
        result.problems = (
            result.problems
            + _collect_problems(first)
            + [f"返修重建失败: {exc}"]
        )
        result.status = STATUS_BLOCKED
        return result

    _record_build(result, repaired_outcome, output_dir, "round-2")
    rebuild_gate = check_build_gate(repaired_outcome)
    if rebuild_gate.blocked or repaired_outcome.candidate_path is None:
        result.stages.append(
            StageRecord(
                STAGE_REBUILD,
                False,
                f"返修重建后生成器状态 {repaired_outcome.status}，停下",
            )
        )
        result.problems = (
            result.problems
            + _collect_problems(first)
            + list(rebuild_gate.reasons)
        )
        result.status = STATUS_BLOCKED
        return result
    rebuild_needs_repair = rebuild_gate.verdict == GATE_REPAIR
    if rebuild_needs_repair:
        result.problems.extend(rebuild_gate.reasons)

    repaired_path = Path(repaired_outcome.candidate_path)
    result.rebuilds = 1
    result.candidate_path = str(repaired_path)
    unchanged = candidate_content_hash(candidate_path) == (
        candidate_content_hash(repaired_path)
    )
    result.stages.append(
        StageRecord(
            STAGE_REBUILD,
            not unchanged,
            f"重建候选 {repaired_path.name}"
            + ("（内容与返修前完全相同）" if unchanged else ""),
        )
    )

    second, evidence = verify_candidate(
        source_document,
        repaired_path,
        elements,
        element_texts=element_texts,
        output_dir=output_dir,
        label="round-2",
        page_budget=page_budget,
        render_pages=render_pages,
    )
    result.evidence.update(evidence)

    outcome = compare_rounds(first.mapping, second.mapping)
    result.evidence["repair-outcome"] = _write_json(
        output_dir / "repair-outcome.json", outcome.as_dict()
    )
    result.stages.append(
        StageRecord(STAGE_REVERIFY, second.clean, outcome.verdict)
    )

    # 这里再挡一道：返修只有一轮，任何绕过 repair 模块的调用都到不了第三轮。
    if plan_repair(
        second.mapping, second.audit, round_index=MAX_REPAIR_ROUNDS
    ).allowed:
        raise FirstDeliveryError("返修轮数上限失效，流程必须停下")

    result.problems = result.problems + _collect_problems(second)
    if unchanged:
        result.problems.insert(0, REPAIR_MADE_NO_DIFFERENCE)
        result.status = STATUS_BLOCKED
        return result
    if outcome.regressions:
        result.problems.insert(
            0,
            "返修把原本没问题的地方弄坏了: " + "、".join(outcome.regressions),
        )
        result.status = STATUS_BLOCKED
    elif second.clean:
        # 重建那一轮生成器若仍报 NEEDS_REPAIR，唯一的返修已经用掉，
        # 剩下的矛盾交给人。
        result.status = (
            STATUS_HANDOVER if rebuild_needs_repair else STATUS_DELIVERED
        )
    else:
        result.status = STATUS_HANDOVER
    return result


def format_result(result: DeliveryResult) -> str:
    """给人看的一页纸。"""

    label = {
        STATUS_DELIVERED: "可以交付",
        STATUS_HANDOVER: "交给人处理",
        STATUS_BLOCKED: "停下，别交",
    }.get(result.status, result.status)
    lines = [f"结论: {label}（重建 {result.rebuilds} 次）"]
    for stage in result.stages:
        lines.append(f"  [{'ok' if stage.ok else '!!'}] {stage.name}: {stage.detail}")
    if result.problems:
        lines.append("")
        lines.append(f"剩余问题 {len(result.problems)} 条:")
        lines.extend(f"  - {problem}" for problem in result.problems[:12])
        if len(result.problems) > 12:
            lines.append(f"  …另有 {len(result.problems) - 12} 条见证据文件")
    if result.manual_items:
        lines.append("")
        lines.append(f"需要人判断 {len(result.manual_items)} 条")
    if result.evidence:
        lines.append("")
        lines.append("证据:")
        lines.extend(
            f"  {name}: {path}" for name, path in sorted(result.evidence.items())
        )
    return "\n".join(lines)
