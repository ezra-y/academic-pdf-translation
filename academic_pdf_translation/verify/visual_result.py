"""视觉检查的**结果**。计划说"该看什么"，这里记"看到了什么"。

之前只有计划没有结果——"已生成检查任务"被当成了"检查已通过"。
结果必须满足三条，缺一不可：

1. **绑定候选。** ``candidate_sha256`` 指明看的是哪一份文件。
   候选换了，旧结果自动作废，谁也不能拿旧结论证明新文件。
2. **逐页逐项。** 计划里每个选中页的每个检查码都要有一条明确的
   PASS/FAIL。只写一个总 PASS 不算看过。
3. **结论是算出来的。** ``decision`` 由条目推导，不接受手写。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"

DECISION_PASS = "PASS"
DECISION_FAIL = "FAIL"

VALID_DECISIONS = frozenset({DECISION_PASS, DECISION_FAIL})


class VisualResultError(RuntimeError):
    """结果文件不合法。"""


@dataclass
class ReviewItem:
    """一页上一项检查的答案。"""

    candidate_page: int
    check_code: str
    decision: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VisualReviewResult:
    """一次真正做过的视觉检查。"""

    schema_version: str = SCHEMA_VERSION
    run_id: str = ""
    attempt_id: str = ""
    candidate_sha256: str = ""
    reviewer_type: str = ""
    items: list[ReviewItem] = field(default_factory=list)

    @property
    def reviewed_pages(self) -> list[int]:
        return sorted({item.candidate_page for item in self.items})

    @property
    def decision(self) -> str:
        """总结论由条目推导。没有条目就没有 PASS。"""

        if not self.items:
            return DECISION_FAIL
        if any(item.decision != DECISION_PASS for item in self.items):
            return DECISION_FAIL
        return DECISION_PASS

    @property
    def failed_items(self) -> list[ReviewItem]:
        return [
            item for item in self.items if item.decision != DECISION_PASS
        ]

    def answered(self, candidate_page: int, check_code: str) -> bool:
        return any(
            item.candidate_page == candidate_page
            and item.check_code == check_code
            for item in self.items
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "candidate_sha256": self.candidate_sha256,
            "reviewer_type": self.reviewer_type,
            "reviewed_pages": self.reviewed_pages,
            "decision": self.decision,
            "items": [item.as_dict() for item in self.items],
        }


def result_from_dict(data: dict[str, Any]) -> VisualReviewResult:
    """从 JSON 载入结果并校验形状。手写的 ``decision`` 字段一律忽略。"""

    if not isinstance(data, dict):
        raise VisualResultError("结果必须是对象")
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise VisualResultError("结果缺少 items 列表")
    items: list[ReviewItem] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise VisualResultError(f"items[{index}] 不是对象")
        page = raw.get("candidate_page")
        code = raw.get("check_code")
        decision = raw.get("decision")
        if not isinstance(page, int) or page < 1:
            raise VisualResultError(f"items[{index}] 的 candidate_page 非法")
        if not code or not isinstance(code, str):
            raise VisualResultError(f"items[{index}] 缺少 check_code")
        if decision not in VALID_DECISIONS:
            raise VisualResultError(
                f"items[{index}] 的 decision 必须是 PASS 或 FAIL，"
                f"拿到 {decision!r}"
            )
        items.append(
            ReviewItem(
                candidate_page=page,
                check_code=code,
                decision=decision,
                detail=str(raw.get("detail") or ""),
            )
        )
    candidate_sha256 = str(data.get("candidate_sha256") or "")
    if not candidate_sha256:
        raise VisualResultError(
            "结果没有 candidate_sha256，无法证明看的是哪一份候选"
        )
    return VisualReviewResult(
        schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
        run_id=str(data.get("run_id") or ""),
        attempt_id=str(data.get("attempt_id") or ""),
        candidate_sha256=candidate_sha256,
        reviewer_type=str(data.get("reviewer_type") or ""),
        items=items,
    )
