"""把渲染计划翻成生成器认识的条目。

阶段 15 的基准查出一件事：渲染计划里的降级决定，生成器根本看不见。
计划说"这张图重建不了，退到保留原文区域"，生成器照旧按老路子走，
返修跑完产出一个字没变。

这里补的就是那一段缺失的翻译：读渲染计划，把落到保留级的元素，
变成生成器已经认识的复杂内容条目。**只翻译保留这两级**——
其余策略仍由生成器原来的路径处理，一块一块换，不一次掀桌子。

两条边界：

- 只翻译计划里确实定到保留级的元素。计划没说的，这里不替它决定。
- 元素必须有页码和坐标。没有坐标就没法保留区域，如实报出来，
  不猜一个框。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from academic_pdf_translation.contracts.models import normalize_bbox
from academic_pdf_translation.planning.mode_policy import (
    FALLBACK_PRESERVE_ELEMENT_REGION,
    FALLBACK_PRESERVE_FULL_PAGE,
)

SCHEMA_VERSION = "1.0"

#: 只有这两级需要翻译。别的策略生成器原来就会走。
PRESERVATION_STRATEGIES = (
    FALLBACK_PRESERVE_ELEMENT_REGION,
    FALLBACK_PRESERVE_FULL_PAGE,
)

#: 生成的条目走这个 kind，方便在产物里一眼认出它来自返修降级。
KIND_PRESERVED = "preserved-source"
STATUS_READY = "ready"

#: 保留区域插在这一页原有内容之前还是之后。
#: 保留的是原文那一块，放在译文之前，读者先看到实物再看译文。
RENDER_POLICY = "insert-before"


class PlanBridgeError(RuntimeError):
    """渲染计划翻不成生成器条目。"""


@dataclass
class BridgeResult:
    """一次翻译的结果与说不通的地方。"""

    schema_version: str = SCHEMA_VERSION
    items: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    @property
    def element_ids(self) -> list[str]:
        return [str(item["source_element_id"]) for item in self.items]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "item_count": len(self.items),
            "skipped_count": len(self.skipped),
            "items": list(self.items),
            "skipped": list(self.skipped),
        }


@dataclass
class _Planned:
    element_id: str
    strategy: str


def _planned_preservations(render_plan: dict[str, Any]) -> list[_Planned]:
    planned: list[_Planned] = []
    for entry in render_plan.get("elements", []):
        if not isinstance(entry, dict):
            continue
        strategy = str(entry.get("strategy") or "")
        if strategy not in PRESERVATION_STRATEGIES:
            continue
        element_id = str(entry.get("element_id") or "")
        if not element_id:
            continue
        planned.append(_Planned(element_id=element_id, strategy=strategy))
    return planned


def build_preservation_items(
    render_plan: dict[str, Any],
    elements: list[dict[str, Any]],
) -> BridgeResult:
    """把计划里定到保留级的元素翻成复杂内容条目。"""

    if not isinstance(render_plan, dict):
        raise PlanBridgeError("渲染计划必须是字典")

    by_id = {
        str(element.get("id") or ""): element
        for element in elements
        if isinstance(element, dict)
    }
    result = BridgeResult()

    for planned in _planned_preservations(render_plan):
        element = by_id.get(planned.element_id)
        if element is None:
            result.skipped.append(
                {
                    "element_id": planned.element_id,
                    "reason": "元素清单里找不到它，无法取得坐标",
                }
            )
            continue
        page = int(element.get("page") or 0)
        box = normalize_bbox(element.get("bbox"))
        if page <= 0:
            result.skipped.append(
                {"element_id": planned.element_id, "reason": "元素没有页码"}
            )
            continue
        if planned.strategy == FALLBACK_PRESERVE_ELEMENT_REGION and box is None:
            result.skipped.append(
                {
                    "element_id": planned.element_id,
                    "reason": "元素没有有效坐标，保留不了区域；不猜一个框",
                }
            )
            continue

        full_page = planned.strategy == FALLBACK_PRESERVE_FULL_PAGE
        result.items.append(
            {
                "id": f"plan-{planned.element_id}",
                "page": page,
                "kind": KIND_PRESERVED,
                "method": planned.strategy,
                "status": STATUS_READY,
                "source_element_id": planned.element_id,
                # source_evidence 是一串给人核对的字符串，不是结构体。
                # 生成器要求每条复杂内容都能被人拿着原文对回去。
                "source_evidence": [
                    f"原文第 {page} 页元素 {planned.element_id}"
                    f"（{element.get('type') or '未知类型'}）",
                    f"渲染计划定为 {planned.strategy}",
                ],
                "payload": {
                    "render_policy": RENDER_POLICY,
                    "regions": [
                        {
                            "page": page,
                            "bbox": None if full_page else list(box or ()),
                            "full_page": full_page,
                            "source_element_id": planned.element_id,
                        }
                    ],
                },
                "notes": (
                    "渲染计划把它定到保留级：重建不可靠，原样搬原文那一块"
                ),
            }
        )
    return result


def merge_into_complex_content(
    complex_content: dict[str, Any], bridged: BridgeResult
) -> dict[str, Any]:
    """把翻译出来的条目并进复杂内容。

    同一个元素已经有条目的，**不覆盖**——那是别处按自己的判断安排好的，
    这里只补计划里定了、生成器却没有安排的那些。
    """

    merged = dict(complex_content or {})
    items = [item for item in merged.get("items", []) if isinstance(item, dict)]
    existing_ids = {str(item.get("id") or "") for item in items}
    existing_elements = {
        str(item.get("source_element_id") or "")
        for item in items
        if item.get("source_element_id")
    }

    added = 0
    for item in bridged.items:
        if item["id"] in existing_ids:
            continue
        if item["source_element_id"] in existing_elements:
            continue
        items.append(item)
        added += 1

    merged["items"] = items
    merged["plan_bridge"] = {
        "schema_version": SCHEMA_VERSION,
        "added": added,
        "skipped": list(bridged.skipped),
    }
    return merged
