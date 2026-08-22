"""高风险视觉检查：挑出真正值得用眼睛看的那几页。

前两个阶段把能数的都数了。但有一类问题数不出来——字压在图上、
表格线歪了、中文和公式挤成一团。这些只有看才知道。

看又不能全看。八页的论文全看一遍，十篇就是八十页，评审会累到不看。
所以这里做一件事：**把风险信号折成分数，按分数排页，只渲染最值得看的几页，
并且逐页写清楚要看什么。**

风险信号全部来自阶段 9 的映射和阶段 10 的对账，这里不产生新的判断，
只做加权和排序。被预算砍掉的页会原样记在 ``skipped`` 里——
悄悄截断会让报告读起来像"全看过了"。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from academic_pdf_translation.verify.candidate_mapping import (
    METHOD_DRAWING_BOUND,
    METHOD_NO_EVIDENCE,
    CandidateMapping,
)
from academic_pdf_translation.verify.structural_audit import StructuralAudit

SCHEMA_VERSION = "1.0"

SIGNAL_MISSING = "element-missing"
SIGNAL_GEOMETRY_GAP = "geometry-gap"
SIGNAL_CAPTION_SPLIT = "caption-split"
SIGNAL_AMBIGUOUS = "ambiguous-location"
SIGNAL_DRAWING_BOUND = "drawing-bound-only"
SIGNAL_NO_EVIDENCE = "required-without-evidence"
SIGNAL_ORDER = "order-inversion"

#: 各信号的权重。数字本身不重要，重要的是相对次序：
#: 整块内容不见了，永远比顺序抖动更该先看。
RISK_WEIGHTS = {
    SIGNAL_MISSING: 10.0,
    SIGNAL_GEOMETRY_GAP: 9.0,
    SIGNAL_CAPTION_SPLIT: 7.0,
    SIGNAL_NO_EVIDENCE: 4.0,
    SIGNAL_DRAWING_BOUND: 3.0,
    SIGNAL_AMBIGUOUS: 2.0,
    SIGNAL_ORDER: 1.0,
}

#: 每个信号对应的检查项。写成"看什么"，不是"哪里有风险"。
SIGNAL_CHECKLIST = {
    SIGNAL_MISSING: "这一页附近应当出现的内容是不是真的没了，还是挪到了别处",
    SIGNAL_GEOMETRY_GAP: "图里的线条和箭头在不在，还是只剩下一堆孤立的数字",
    SIGNAL_CAPTION_SPLIT: "图题和它说明的图是不是隔着页",
    SIGNAL_NO_EVIDENCE: "这一页有没有该出现却查不到的内容",
    SIGNAL_DRAWING_BOUND: "这一页上的图形是不是真是它，而不是碰巧数量够",
    SIGNAL_AMBIGUOUS: "同一段内容是不是被重复排了两处",
    SIGNAL_ORDER: "这一页的内容顺序读起来顺不顺",
}

#: 默认最多渲染几页。评审看得完才有意义。
DEFAULT_PAGE_BUDGET = 6
#: 渲染分辨率。看排版够用，文件也不会大到打不开。
REVIEW_DPI = 150
#: 低于这个分数的页不值得占用评审的注意力。
MIN_REVIEW_SCORE = 1.0

#: 计划状态：没有风险页就是 NOT_REQUIRED——不是"看过了"，是"不用看"。
PLAN_NOT_REQUIRED = "NOT_REQUIRED"
PLAN_REQUIRED = "REQUIRED"
#: 有风险页被预算砍掉。截断的计划不许当成"全看过了"。
PLAN_TRUNCATED = "TRUNCATED"


class VisualReviewError(RuntimeError):
    """视觉检查计划构建失败。"""


@dataclass
class RiskSignal:
    """一条风险信号。"""

    code: str
    element_id: str
    detail: str

    @property
    def weight(self) -> float:
        return RISK_WEIGHTS.get(self.code, 0.0)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["weight"] = self.weight
        return data


@dataclass
class PageRisk:
    """一页的风险汇总。"""

    candidate_page: int
    signals: list[RiskSignal] = field(default_factory=list)

    @property
    def score(self) -> float:
        return round(sum(signal.weight for signal in self.signals), 2)

    @property
    def element_ids(self) -> list[str]:
        seen: list[str] = []
        for signal in self.signals:
            if signal.element_id and signal.element_id not in seen:
                seen.append(signal.element_id)
        return seen

    @property
    def checklist(self) -> list[str]:
        seen: list[str] = []
        for signal in sorted(
            self.signals, key=lambda item: item.weight, reverse=True
        ):
            item = SIGNAL_CHECKLIST.get(signal.code)
            if item and item not in seen:
                seen.append(item)
        return seen

    @property
    def checks(self) -> list[tuple[str, str, str]]:
        """逐元素的检查项：(元素 ID, 检查码, 看什么)。

        视觉门按 (页, 元素, 检查码) 对账，所以清单也必须写到元素这一级：
        同一页两个表格触发同一个码，就是两条要分别回答的检查项。
        """

        seen: list[tuple[str, str, str]] = []
        for signal in sorted(
            self.signals, key=lambda item: item.weight, reverse=True
        ):
            entry = (
                signal.element_id,
                signal.code,
                SIGNAL_CHECKLIST.get(signal.code, signal.code),
            )
            if entry not in seen:
                seen.append(entry)
        return seen

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_page": self.candidate_page,
            "score": self.score,
            "element_ids": self.element_ids,
            "checklist": self.checklist,
            "checks": [
                {"element_id": item[0], "check_code": item[1], "look_for": item[2]}
                for item in self.checks
            ],
            "signals": [signal.as_dict() for signal in self.signals],
        }


@dataclass
class VisualReviewPlan:
    """一次视觉检查的计划。"""

    schema_version: str = SCHEMA_VERSION
    page_budget: int = DEFAULT_PAGE_BUDGET
    selected: list[PageRisk] = field(default_factory=list)
    #: 有风险但被预算砍掉的页。悄悄截断会让报告读起来像"全看过了"。
    skipped: list[PageRisk] = field(default_factory=list)
    rendered: list[str] = field(default_factory=list)

    @property
    def truncated(self) -> bool:
        return bool(self.skipped)

    @property
    def status(self) -> str:
        if self.truncated:
            return PLAN_TRUNCATED
        if self.selected:
            return PLAN_REQUIRED
        return PLAN_NOT_REQUIRED

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "page_budget": self.page_budget,
            "selected_count": len(self.selected),
            "skipped_count": len(self.skipped),
            "truncated": self.truncated,
            "selected": [item.as_dict() for item in self.selected],
            "skipped": [item.as_dict() for item in self.skipped],
            "rendered": list(self.rendered),
        }


def collect_signals(
    mapping: CandidateMapping,
    audit: StructuralAudit,
) -> dict[int, list[RiskSignal]]:
    """把阶段 9、10 的结果折成逐页的风险信号。

    找不到的元素没有候选页码，挂到它原文所在的页上——评审要去看的，
    正是"这块内容本该出现的地方"。
    """

    by_page: dict[int, list[RiskSignal]] = {}

    def add(page: int, signal: RiskSignal) -> None:
        if page <= 0:
            return
        by_page.setdefault(page, []).append(signal)

    for item in mapping.locations:
        if not item.located and item.required:
            # 「查不到」和「判不了」要分开：探针只剩一两个数学字体残渣字符时，
            # 我们并不知道内容丢没丢，不该按最高权重把评审引过去。
            code = (
                SIGNAL_NO_EVIDENCE
                if item.method == METHOD_NO_EVIDENCE
                else SIGNAL_MISSING
            )
            add(
                item.source_page,
                RiskSignal(code, item.element_id, item.evidence),
            )
            continue
        if item.geometry_ok is False:
            for page in item.candidate_pages:
                add(
                    page,
                    RiskSignal(
                        SIGNAL_GEOMETRY_GAP,
                        item.element_id,
                        f"原文 {item.source_drawing_count} 个绘图对象，"
                        f"候选这几页最多 {item.candidate_drawing_count} 个",
                    ),
                )
        if item.ambiguous:
            for page in item.candidate_pages:
                add(
                    page,
                    RiskSignal(
                        SIGNAL_AMBIGUOUS,
                        item.element_id,
                        f"同时疑似命中第 {item.candidate_pages} 页",
                    ),
                )
        if item.located and item.method == METHOD_DRAWING_BOUND:
            for page in item.candidate_pages:
                add(
                    page,
                    RiskSignal(
                        SIGNAL_DRAWING_BOUND, item.element_id, item.evidence
                    ),
                )

    for split in audit.caption_splits:
        for page in set(split.caption_pages) | set(split.target_pages):
            add(
                page,
                RiskSignal(
                    SIGNAL_CAPTION_SPLIT,
                    split.caption_id,
                    f"图题在第 {split.caption_pages} 页，"
                    f"{split.target_id} 在第 {split.target_pages} 页",
                ),
            )

    for inversion in audit.order_inversions:
        for page in (
            inversion.earlier_candidate_page,
            inversion.later_candidate_page,
        ):
            add(
                page,
                RiskSignal(
                    SIGNAL_ORDER,
                    inversion.earlier_in_source,
                    f"{inversion.earlier_in_source} 排到了 "
                    f"{inversion.later_in_source} 后面",
                ),
            )

    return by_page


def rank_pages(
    signals: dict[int, list[RiskSignal]],
    *,
    page_budget: int = DEFAULT_PAGE_BUDGET,
    min_score: float = MIN_REVIEW_SCORE,
) -> tuple[list[PageRisk], list[PageRisk]]:
    """按分数排页，返回（选中的，被预算砍掉的）。"""

    if page_budget < 1:
        raise VisualReviewError("视觉检查至少要看一页")

    ranked = [
        PageRisk(candidate_page=page, signals=list(items))
        for page, items in signals.items()
    ]
    ranked = [item for item in ranked if item.score >= min_score]
    ranked.sort(key=lambda item: (-item.score, item.candidate_page))
    return (ranked[:page_budget], ranked[page_budget:])


def build_review_plan(
    mapping: CandidateMapping,
    audit: StructuralAudit,
    *,
    page_budget: int = DEFAULT_PAGE_BUDGET,
    min_score: float = MIN_REVIEW_SCORE,
) -> VisualReviewPlan:
    """从映射和对账结果算出该看哪几页。"""

    selected, skipped = rank_pages(
        collect_signals(mapping, audit),
        page_budget=page_budget,
        min_score=min_score,
    )
    return VisualReviewPlan(
        page_budget=page_budget, selected=selected, skipped=skipped
    )


def format_plan(plan: VisualReviewPlan) -> str:
    """给评审看的一页纸：看哪几页，每页看什么。"""

    if not plan.selected:
        return "没有需要人工细看的页。"
    lines = [f"需要人工细看 {len(plan.selected)} 页（预算 {plan.page_budget}）:"]
    for item in plan.selected:
        lines.append("")
        lines.append(f"候选第 {item.candidate_page} 页（风险分 {item.score}）")
        for element_id, code, look_for in item.checks:
            lines.append(f"  - [{element_id}] {code}: {look_for}")
    if plan.truncated:
        lines.append("")
        lines.append(
            f"另有 {len(plan.skipped)} 页有风险但超出预算未渲染: "
            + "、".join(
                f"第 {item.candidate_page} 页（{item.score}）"
                for item in plan.skipped
            )
        )
    return "\n".join(lines)
