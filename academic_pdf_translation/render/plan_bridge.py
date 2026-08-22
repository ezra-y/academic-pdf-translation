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

#: 元素指向它图题的关系名。
CAPTION_RELATION = "caption"


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


def caption_text_for(
    element: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    unit_texts_by_element: dict[str, str],
) -> str:
    """这个元素的图题译文。

    图题必须跟着图走：图在第 4 页、图题在第 5 页，两样东西都废了。
    把图题挂在保留条目上，生成器就能把它和图锁成一块，
    正文里那一份也会被自动抑制，不会印两遍。
    """

    for caption_id in (element.get("relations") or {}).get(
        CAPTION_RELATION, []
    ):
        text = unit_texts_by_element.get(str(caption_id), "").strip()
        if text:
            return text
        caption_element = by_id.get(str(caption_id))
        if caption_element is None:
            continue
        excerpt = str(caption_element.get("text_excerpt") or "").strip()
        if excerpt:
            return excerpt
    return ""


def build_preservation_items(
    render_plan: dict[str, Any],
    elements: list[dict[str, Any]],
    *,
    unit_texts_by_element: dict[str, str] | None = None,
) -> BridgeResult:
    """把计划里定到保留级的元素翻成复杂内容条目。

    ``unit_texts_by_element`` 是元素到译文的映射，用来取图题。
    取不到就不带图题——宁可图题留在正文里，也不要凭空造一句。
    """

    if not isinstance(render_plan, dict):
        raise PlanBridgeError("渲染计划必须是字典")

    by_id = {
        str(element.get("id") or ""): element
        for element in elements
        if isinstance(element, dict)
    }
    texts = unit_texts_by_element or {}
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
        caption = caption_text_for(element, by_id, texts)
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
                            # 生成器认这个键当图题：它会把图题和图锁成一块，
                            # 并把正文里重复的那一份抑制掉。
                            "translation": caption,
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


#: 复杂条目里挂图级图题用的键。
FIGURE_CAPTION_KEY = "figure_caption"


def _item_xrefs(item: dict[str, Any]) -> set[int]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    found: set[int] = set()
    for region in payload.get("regions", []):
        if not isinstance(region, dict):
            continue
        xref = region.get("xref")
        if isinstance(xref, int):
            found.add(xref)
    return found


def attach_figure_captions(
    complex_content: dict[str, Any],
    elements: list[dict[str, Any]],
    unit_texts_by_element: dict[str, str],
) -> tuple[dict[str, Any], list[str]]:
    """把图级图题挂到它那个复杂条目上。

    四联子图里每一格自己的 (a)(b)(c)(d) 说明本来就内嵌在条目里，跟着图走。
    但整张图的图题（"图 3. ……"）是一条独立的译文单元，排在正文流里，
    随时可能被分到上一页或下一页去。

    挂上之后，生成器会把它和图锁成一块，并把正文里重复的那一份抑制掉。

    按图像 xref 对应。矢量图没有 xref，这里认不出来——如实返回，
    不按页码猜，猜错会把图题挂到别的图上。
    """

    merged = dict(complex_content or {})
    items = [item for item in merged.get("items", []) if isinstance(item, dict)]
    by_id = {
        str(element.get("id") or ""): element
        for element in elements
        if isinstance(element, dict)
    }

    caption_by_xref: dict[int, str] = {}
    for element in elements:
        if not isinstance(element, dict):
            continue
        xref = (element.get("detail") or {}).get("xref")
        if not isinstance(xref, int):
            continue
        text = caption_text_for(element, by_id, unit_texts_by_element)
        if text:
            caption_by_xref[xref] = text

    attached: list[str] = []
    for item in items:
        payload = item.get("payload")
        if not isinstance(payload, dict) or payload.get(FIGURE_CAPTION_KEY):
            continue
        for xref in sorted(_item_xrefs(item)):
            caption = caption_by_xref.get(xref)
            if caption:
                payload[FIGURE_CAPTION_KEY] = caption
                attached.append(str(item.get("id") or ""))
                break

    merged["items"] = items
    return (merged, attached)
