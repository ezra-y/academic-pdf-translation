"""渲染计划到生成器的翻译：只翻保留级，缺坐标就如实跳过。

单独运行：
    python3 -m pytest -q tests/test_plan_bridge.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from academic_pdf_translation.analysis.source_elements import (  # noqa: E402
    build_inventory,
)
from academic_pdf_translation.planning.mode_policy import (  # noqa: E402
    FALLBACK_PRESERVE_ELEMENT_REGION,
    FALLBACK_PRESERVE_FULL_PAGE,
)
from academic_pdf_translation.planning.render_plan import (  # noqa: E402
    build_render_plan,
)
from academic_pdf_translation.render.plan_bridge import (  # noqa: E402
    KIND_PRESERVED,
    PlanBridgeError,
    build_preservation_items,
    merge_into_complex_content,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _real_job():
    needed = [
        REAL_JOB / "source_structure.json",
        REAL_JOB / "source_elements.json",
    ]
    if not all(path.is_file() for path in needed):
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    inventory = build_inventory(
        json.loads(needed[0].read_text(encoding="utf-8")), pymupdf_version="1"
    )
    elements = json.loads(needed[1].read_text(encoding="utf-8"))["elements"]
    return inventory, elements


def _plan(strategy: str, element_id: str = "e1") -> dict:
    return {"elements": [{"element_id": element_id, "strategy": strategy}]}


def _element(**overrides) -> dict:
    base = {
        "id": "e1",
        "type": "vector-figure",
        "page": 2,
        "bbox": [10, 20, 200, 300],
    }
    base.update(overrides)
    return base


# --- 只翻保留级 -------------------------------------------------------------


def test_only_preservation_strategies_are_translated() -> None:
    """别的策略生成器原来就会走，这里不替它决定。"""

    result = build_preservation_items(
        _plan("preserve-geometry-with-label-overlay"), [_element()]
    )
    assert result.items == []
    assert result.skipped == []


def test_a_region_preservation_becomes_one_item() -> None:
    result = build_preservation_items(
        _plan(FALLBACK_PRESERVE_ELEMENT_REGION), [_element()]
    )
    assert len(result.items) == 1
    item = result.items[0]
    assert item["kind"] == KIND_PRESERVED
    assert item["method"] == FALLBACK_PRESERVE_ELEMENT_REGION
    assert item["page"] == 2
    region = item["payload"]["regions"][0]
    assert region["bbox"] == [10.0, 20.0, 200.0, 300.0]
    assert region["full_page"] is False


def test_a_full_page_preservation_carries_no_bbox() -> None:
    result = build_preservation_items(
        _plan(FALLBACK_PRESERVE_FULL_PAGE), [_element()]
    )
    region = result.items[0]["payload"]["regions"][0]
    assert region["full_page"] is True
    assert region["bbox"] is None


def test_a_full_page_preservation_works_without_a_bbox() -> None:
    """整页保留不需要坐标，缺坐标不该被跳过。"""

    result = build_preservation_items(
        _plan(FALLBACK_PRESERVE_FULL_PAGE), [_element(bbox=None)]
    )
    assert len(result.items) == 1


# --- 缺东西就说 -------------------------------------------------------------


def test_a_region_preservation_without_a_bbox_is_skipped() -> None:
    """没有坐标就保留不了区域。不猜一个框。"""

    result = build_preservation_items(
        _plan(FALLBACK_PRESERVE_ELEMENT_REGION), [_element(bbox=None)]
    )
    assert result.items == []
    assert "不猜一个框" in result.skipped[0]["reason"]


def test_an_element_missing_from_the_inventory_is_skipped() -> None:
    result = build_preservation_items(
        _plan(FALLBACK_PRESERVE_ELEMENT_REGION, "ghost"), [_element()]
    )
    assert result.items == []
    assert result.skipped[0]["element_id"] == "ghost"


def test_an_element_without_a_page_is_skipped() -> None:
    result = build_preservation_items(
        _plan(FALLBACK_PRESERVE_ELEMENT_REGION), [_element(page=0)]
    )
    assert result.items == []
    assert "页码" in result.skipped[0]["reason"]


def test_a_non_dict_plan_is_rejected() -> None:
    with pytest.raises(PlanBridgeError):
        build_preservation_items([], [])  # type: ignore[arg-type]


# --- 生成器的要求 -----------------------------------------------------------


def test_the_evidence_is_a_list_of_human_checkable_strings() -> None:
    """生成器要求每条复杂内容都能被人拿着原文对回去。"""

    item = build_preservation_items(
        _plan(FALLBACK_PRESERVE_ELEMENT_REGION), [_element()]
    ).items[0]
    evidence = item["source_evidence"]
    assert isinstance(evidence, list)
    assert all(isinstance(value, str) and value.strip() for value in evidence)
    assert any("第 2 页" in value for value in evidence)


def test_the_render_policy_is_one_the_generator_accepts() -> None:
    item = build_preservation_items(
        _plan(FALLBACK_PRESERVE_ELEMENT_REGION), [_element()]
    ).items[0]
    assert item["payload"]["render_policy"] in {
        "insert-before",
        "insert-after",
        "replace-page-units",
    }


# --- 合并 -------------------------------------------------------------------


def test_an_existing_item_for_the_same_element_wins() -> None:
    """别处按自己的判断安排好的，这里不覆盖。"""

    bridged = build_preservation_items(
        _plan(FALLBACK_PRESERVE_ELEMENT_REGION), [_element()]
    )
    existing = {
        "items": [
            {"id": "manual-1", "source_element_id": "e1", "method": "custom"}
        ]
    }
    merged = merge_into_complex_content(existing, bridged)
    assert len(merged["items"]) == 1
    assert merged["items"][0]["method"] == "custom"
    assert merged["plan_bridge"]["added"] == 0


def test_unrelated_items_are_kept() -> None:
    bridged = build_preservation_items(
        _plan(FALLBACK_PRESERVE_ELEMENT_REGION), [_element()]
    )
    merged = merge_into_complex_content(
        {"items": [{"id": "other", "method": "vector-rebuild"}]}, bridged
    )
    assert len(merged["items"]) == 2
    assert merged["plan_bridge"]["added"] == 1


def test_skipped_elements_are_recorded_in_the_merge() -> None:
    bridged = build_preservation_items(
        _plan(FALLBACK_PRESERVE_ELEMENT_REGION), [_element(bbox=None)]
    )
    merged = merge_into_complex_content({"items": []}, bridged)
    assert merged["plan_bridge"]["skipped"]


# --- 真实论文 ---------------------------------------------------------------


def test_a_plan_with_no_preservations_produces_nothing() -> None:
    """没有返修时，管线不该凭空多出条目。"""

    inventory, elements = _real_job()
    plan = build_render_plan(inventory, "balanced")
    result = build_preservation_items(plan.as_dict(), elements)
    assert result.items == []


def test_real_forced_downgrades_become_real_regions() -> None:
    """返修把三个元素压到保留级，翻出来的坐标必须落在原文页内。"""

    inventory, elements = _real_job()
    plan = build_render_plan(inventory, "balanced")
    targets = {
        item.element_id: item.fallback
        for item in plan.elements
        if item.fallback == FALLBACK_PRESERVE_ELEMENT_REGION
    }
    if not targets:
        pytest.skip("样本论文里没有可降到区域保留的元素")

    downgraded = build_render_plan(
        inventory, "balanced", forced_strategies=targets
    )
    result = build_preservation_items(downgraded.as_dict(), elements)
    assert len(result.items) == len(targets)
    assert result.skipped == []

    by_id = {element["id"]: element for element in elements}
    for item in result.items:
        region = item["payload"]["regions"][0]
        source = by_id[item["source_element_id"]]
        assert region["page"] == source["page"]
        assert region["bbox"] == [float(value) for value in source["bbox"]]
