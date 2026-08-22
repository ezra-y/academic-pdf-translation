"""视觉门槛：计划和结果对得上，才算看过。

纯函数。输入是计划、结果和当前候选哈希，输出一个判定，不写文件。

判定码：

- ``NOT_REQUIRED``：没有风险页，不需要看，门放行。
- ``WAITING_FOR_VISUAL_REVIEW``：有风险页但还没有结果，禁止交付。
- ``STALE``：结果绑的不是当前候选，等于没看，禁止交付。
- ``INCOMPLETE``：该看的页或该答的问题没答全，禁止交付。
- ``TRUNCATED``：计划本身被预算截断，有高风险页没进清单，禁止直接通过。
- ``FAIL``：看了，发现问题。带出失败条目供返修/人工。
- ``PASS``：每页每项都有答案且全部通过。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from academic_pdf_translation.verify.visual_plan import VisualReviewPlan
from academic_pdf_translation.verify.visual_result import (
    DECISION_PASS,
    ReviewItem,
    VisualReviewResult,
)

VISUAL_PASS = "PASS"
VISUAL_NOT_REQUIRED = "NOT_REQUIRED"
VISUAL_WAITING = "WAITING_FOR_VISUAL_REVIEW"
VISUAL_STALE = "STALE"
VISUAL_INCOMPLETE = "INCOMPLETE"
VISUAL_TRUNCATED = "TRUNCATED"
VISUAL_FAIL = "FAIL"

#: 这些判定放行。注意 NOT_REQUIRED 是"不用看"，不是"看过了"。
PASSING_CODES = frozenset({VISUAL_PASS, VISUAL_NOT_REQUIRED})


@dataclass
class VisualGateResult:
    """视觉门的判定。"""

    code: str
    reasons: list[str] = field(default_factory=list)
    failed_items: list[ReviewItem] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.code in PASSING_CODES


def required_answers(plan: VisualReviewPlan) -> dict[int, set[str]]:
    """计划要求的答案清单：每个选中页 → 该页出现过的信号码集合。"""

    answers: dict[int, set[str]] = {}
    for page in plan.selected:
        answers[page.candidate_page] = {
            signal.code for signal in page.signals
        }
    return answers


def check_visual_gate(
    plan: VisualReviewPlan,
    result: VisualReviewResult | None,
    *,
    candidate_sha256: str,
) -> VisualGateResult:
    """计划 + 结果 + 当前候选哈希 → 一个判定。"""

    if plan.truncated:
        pages = "、".join(
            f"第 {item.candidate_page} 页（风险分 {item.score}）"
            for item in plan.skipped
        )
        return VisualGateResult(
            VISUAL_TRUNCATED,
            [
                f"视觉检查计划被页数预算截断，{pages} 有风险但没进清单；"
                "增加预算重新计划，或交给人"
            ],
        )

    if not plan.selected:
        return VisualGateResult(
            VISUAL_NOT_REQUIRED, ["没有风险页，本轮不需要视觉检查"]
        )

    if result is None:
        return VisualGateResult(
            VISUAL_WAITING,
            [
                f"计划要求细看 {len(plan.selected)} 页，"
                "但还没有任何真实检查结果；计划不是结果，禁止交付"
            ],
        )

    if result.candidate_sha256 != candidate_sha256:
        return VisualGateResult(
            VISUAL_STALE,
            [
                "检查结果绑定的候选哈希 "
                f"{result.candidate_sha256[:12]}… 与当前候选 "
                f"{candidate_sha256[:12]}… 不一致——看的不是这份文件，"
                "结果作废"
            ],
        )

    missing: list[str] = []
    for page, codes in required_answers(plan).items():
        for code in sorted(codes):
            if not result.answered(page, code):
                missing.append(f"第 {page} 页的 {code}")
    if missing:
        return VisualGateResult(
            VISUAL_INCOMPLETE,
            [
                "以下检查项没有答案，只答一部分不算看过: "
                + "；".join(missing[:8])
                + (f"；…另有 {len(missing) - 8} 项" if len(missing) > 8 else "")
            ],
        )

    failed = result.failed_items
    if failed:
        return VisualGateResult(
            VISUAL_FAIL,
            [
                f"视觉检查发现 {len(failed)} 项问题: "
                + "；".join(
                    f"第 {item.candidate_page} 页 {item.check_code}"
                    f"（{item.detail or '无说明'}）"
                    for item in failed[:6]
                )
            ],
            failed_items=list(failed),
        )

    assert result.decision == DECISION_PASS
    return VisualGateResult(VISUAL_PASS)
