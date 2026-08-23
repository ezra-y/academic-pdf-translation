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
    FIGURE_CAPTION_KEY,
    KIND_PRESERVED,
    PlanBridgeError,
    attach_figure_captions,
    build_preservation_items,
    caption_text_for,
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


def test_non_preservation_strategies_are_left_alone() -> None:
    """位图与普通正文走生成器既有路径，这里不替它决定。"""

    for strategy in ("preserve-original-image", "translate-and-reflow"):
        result = build_preservation_items(_plan(strategy), [_element()])
        assert result.items == []
        assert result.skipped == []


def test_per_type_preservation_strategies_become_region_items() -> None:
    """表格、公式、矢量图的保留决定在首版就生效，不用等返修。"""

    for strategy in (
        "preserve-table-region-with-translation-key",
        "preserve-formula-region",
        "preserve-geometry-with-label-overlay",
        "preserve-geometry-with-numbered-legend",
    ):
        result = build_preservation_items(_plan(strategy), [_element()])
        assert len(result.items) == 1, strategy
        item = result.items[0]
        assert item["method"] == FALLBACK_PRESERVE_ELEMENT_REGION
        assert item["plan_strategy"] == strategy


def test_an_overlapping_existing_item_blocks_duplication() -> None:
    """同页已有条目盖住的区域不重复保留，否则同一块出现两遍。"""

    bridged = build_preservation_items(
        _plan("preserve-formula-region"), [_element()]
    )
    existing = {
        "items": [
            {
                "id": "manual-x",
                "page": 2,
                "status": "ready",
                "payload": {"regions": [{"bbox": [10, 20, 200, 300]}]},
            }
        ]
    }
    merged = merge_into_complex_content(existing, bridged)
    assert len(merged["items"]) == 1
    assert merged["plan_bridge"]["added"] == 0
    assert any("盖住" in item["reason"] for item in merged["plan_bridge"]["skipped"])


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


def test_the_default_plan_yields_exactly_its_preservation_family() -> None:
    """首版就执行计划里的保留决定：条目数等于计划里保留族策略的数量。

    位图（preserve-original-image）走生成器既有路径，不在其中。
    """

    inventory, elements = _real_job()
    plan = build_render_plan(inventory, "balanced")
    from academic_pdf_translation.render.plan_bridge import (
        PRESERVATION_STRATEGIES,
    )

    expected = [
        item
        for item in plan.elements
        if item.strategy in PRESERVATION_STRATEGIES
    ]
    result = build_preservation_items(plan.as_dict(), elements)
    assert len(result.items) == len(expected)
    assert result.skipped == []
    types = {
        (item["source_evidence"][0].split("（")[1].rstrip("）"))
        for item in result.items
    }
    assert {"table", "display-formula", "vector-figure"} <= types


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


# --- 图题跟着图走 -----------------------------------------------------------


def test_a_caption_is_looked_up_through_the_relation() -> None:
    element = {"id": "f1", "relations": {"caption": ["c1"]}}
    by_id = {"c1": {"id": "c1"}}
    assert caption_text_for(element, by_id, {"c1": "图 3. 说明"}) == "图 3. 说明"


def test_a_caption_falls_back_to_the_source_excerpt() -> None:
    """译文取不到时用原文摘录，总好过什么都不挂。"""

    element = {"id": "f1", "relations": {"caption": ["c1"]}}
    by_id = {"c1": {"id": "c1", "text_excerpt": "Fig. 3. HeLa cells"}}
    assert caption_text_for(element, by_id, {}) == "Fig. 3. HeLa cells"


def test_no_relation_means_no_caption() -> None:
    assert caption_text_for({"id": "f1"}, {}, {}) == ""


def test_a_preserved_item_carries_its_caption() -> None:
    element = _element(relations={"caption": ["c1"]})
    result = build_preservation_items(
        _plan(FALLBACK_PRESERVE_ELEMENT_REGION),
        [element, {"id": "c1"}],
        unit_texts_by_element={"c1": "图 1. 网络结构"},
    )
    region = result.items[0]["payload"]["regions"][0]
    assert region["translation"] == "图 1. 网络结构"


def test_a_figure_caption_is_attached_by_xref() -> None:
    complex_content = {
        "items": [
            {"id": "i1", "page": 5, "payload": {"regions": [{"xref": 119}]}}
        ]
    }
    elements = [
        {
            "id": "img",
            "detail": {"xref": 119},
            "relations": {"caption": ["c1"]},
        },
        {"id": "c1"},
    ]
    merged, attached = attach_figure_captions(
        complex_content, elements, {"c1": "图 3. 说明"}
    )
    assert attached == ["i1"]
    assert merged["items"][0]["payload"][FIGURE_CAPTION_KEY] == "图 3. 说明"


def test_an_existing_figure_caption_is_not_overwritten() -> None:
    complex_content = {
        "items": [
            {
                "id": "i1",
                "payload": {
                    "regions": [{"xref": 119}],
                    FIGURE_CAPTION_KEY: "已有的图题",
                },
            }
        ]
    }
    elements = [
        {"id": "img", "detail": {"xref": 119}, "relations": {"caption": ["c1"]}},
        {"id": "c1"},
    ]
    merged, attached = attach_figure_captions(
        complex_content, elements, {"c1": "新的图题"}
    )
    assert attached == []
    assert merged["items"][0]["payload"][FIGURE_CAPTION_KEY] == "已有的图题"


def test_an_item_without_a_matching_xref_gets_nothing() -> None:
    """矢量图没有 xref，这里认不出来。不按页码猜——猜错会挂到别的图上。"""

    complex_content = {"items": [{"id": "i1", "payload": {"regions": [{}]}}]}
    elements = [
        {"id": "img", "detail": {"xref": 9}, "relations": {"caption": ["c1"]}},
        {"id": "c1"},
    ]
    merged, attached = attach_figure_captions(
        complex_content, elements, {"c1": "图 3."}
    )
    assert attached == []
    assert FIGURE_CAPTION_KEY not in merged["items"][0]["payload"]


def test_real_subfigure_groups_get_their_figure_level_caption() -> None:
    """样本论文的两组四联子图，图级图题必须挂上。"""

    _, elements = _real_job()
    complex_path = REAL_JOB / "complex_content.json"
    bindings_path = REAL_JOB / "unit_bindings.json"
    translation_path = REAL_JOB / "translation.json"
    if not all(
        path.is_file()
        for path in (complex_path, bindings_path, translation_path)
    ):
        pytest.skip("缺少真实作业的复杂内容或绑定")

    units = {
        unit["id"]: unit
        for unit in json.loads(
            translation_path.read_text(encoding="utf-8")
        )["units"]
    }
    texts: dict[str, list[str]] = {}
    for binding in json.loads(bindings_path.read_text(encoding="utf-8"))[
        "bindings"
    ]:
        unit = units.get(binding["unit_id"])
        value = str((unit or {}).get("translation") or "").strip()
        if value:
            texts.setdefault(binding["element_id"], []).append(value)

    merged, attached = attach_figure_captions(
        json.loads(complex_path.read_text(encoding="utf-8")),
        elements,
        {key: " ".join(value) for key, value in texts.items()},
    )
    assert len(attached) >= 2, attached
    for item in merged["items"]:
        caption = (item.get("payload") or {}).get(FIGURE_CAPTION_KEY)
        if caption:
            assert caption.strip()
