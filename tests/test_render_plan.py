"""渲染计划：每个必需元素都有去处，complete 由程序算。

单独运行：
    python3 -m pytest -q tests/test_render_plan.py
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
from academic_pdf_translation.contracts.enums import (  # noqa: E402
    ElementType,
    QualityMode,
)
from academic_pdf_translation.contracts.models import (  # noqa: E402
    ElementRisk,
    SourceElement,
    SourceElementInventory,
)
from academic_pdf_translation.planning import mode_policy  # noqa: E402
from academic_pdf_translation.planning.fallback_policy import (  # noqa: E402
    build_chain,
)
from academic_pdf_translation.planning.mode_policy import (  # noqa: E402
    FALLBACK_PRESERVE_ELEMENT_REGION,
    FALLBACK_PRESERVE_FULL_PAGE,
    policy_for,
)
from academic_pdf_translation.planning.render_plan import (  # noqa: E402
    STRATEGY_FORMULA_PRESERVE,
    STRATEGY_TABLE_PRESERVE,
    STRATEGY_TABLE_REBUILD,
    STRATEGY_VECTOR_LEGEND,
    STRATEGY_VECTOR_OVERLAY,
    RenderPlanError,
    build_figure_inventory,
    build_render_plan,
    plan_element,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _real_inventory() -> SourceElementInventory:
    path = REAL_JOB / "source_structure.json"
    if not path.is_file():
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    return build_inventory(
        json.loads(path.read_text(encoding="utf-8")), pymupdf_version="1"
    )


def _table(**detail) -> SourceElement:
    element = SourceElement(
        id="p0001-table-001",
        page=1,
        type=ElementType.TABLE,
        bbox=(50, 100, 500, 300),
        confidence=detail.pop("confidence", 0.95),
        detail={"estimated_rows": 8, "estimated_columns": 5, **detail},
    )
    return element


def _inventory(elements: list[SourceElement]) -> SourceElementInventory:
    return SourceElementInventory(
        source_sha256="a" * 64, page_count=1, elements=elements
    )


# --- 策略选择 ---------------------------------------------------------------


def test_reliable_table_is_rebuilt() -> None:
    planned = plan_element(_table(), policy_for(QualityMode.FAST))
    assert planned.strategy == STRATEGY_TABLE_REBUILD


def test_low_confidence_table_never_becomes_paragraph() -> None:
    """列数定不下来时保留原表，绝不压平成段落。"""

    element = _table(estimated_columns=1)
    element.risk_flags.append(ElementRisk(code="table-columns-unresolved"))
    for mode in QualityMode:
        planned = plan_element(element, policy_for(mode))
        assert planned.strategy == STRATEGY_TABLE_PRESERVE
        assert planned.strategy != mode_policy.TABLE_FLATTEN_FORBIDDEN
        assert "压成段落" in planned.reason


def test_weak_grid_confidence_falls_back_to_preserve() -> None:
    planned = plan_element(
        _table(confidence=0.5), policy_for(QualityMode.FAST)
    )
    assert planned.strategy == STRATEGY_TABLE_PRESERVE


def test_precise_mode_demands_higher_table_confidence() -> None:
    """精细档对识别要求更高：够不到就走保留，不硬重建。"""

    element = _table(confidence=0.88)
    assert plan_element(element, policy_for(QualityMode.FAST)).strategy == (
        STRATEGY_TABLE_REBUILD
    )
    assert plan_element(element, policy_for(QualityMode.PRECISE)).strategy == (
        STRATEGY_TABLE_PRESERVE
    )


def test_fast_dense_vector_uses_preserve_strategy() -> None:
    """快速档不重画复杂矢量图，只处理文字标签。"""

    element = SourceElement(
        id="p0002-figure-001",
        page=2,
        type=ElementType.VECTOR_FIGURE,
        bbox=(40, 90, 570, 700),
        detail={"drawing_count": 213},
    )
    planned = plan_element(element, policy_for(QualityMode.FAST))
    assert planned.strategy in {
        STRATEGY_VECTOR_OVERLAY,
        STRATEGY_VECTOR_LEGEND,
    }
    assert planned.strategy != mode_policy.VECTOR_FULL_REBUILD


def test_unmappable_labels_fall_back_to_numbered_legend() -> None:
    element = SourceElement(
        id="p0002-figure-001",
        page=2,
        type=ElementType.VECTOR_FIGURE,
        bbox=(40, 90, 570, 700),
        detail={"label_mapping_confidence": 0.3},
    )
    element.link("embedded-label", "p0002-label-001")
    planned = plan_element(element, policy_for(QualityMode.FAST))
    assert planned.strategy == STRATEGY_VECTOR_LEGEND


def test_fast_formula_uses_preserve_strategy() -> None:
    element = SourceElement(
        id="p0004-formula-001",
        page=4,
        type=ElementType.DISPLAY_FORMULA,
        bbox=(100, 200, 400, 240),
    )
    planned = plan_element(element, policy_for(QualityMode.FAST))
    assert planned.strategy == STRATEGY_FORMULA_PRESERVE
    assert planned.strategy != mode_policy.FORMULA_FULL_REBUILD


def test_forbidden_strategy_is_rejected() -> None:
    """禁止的策略一旦出现在计划里就直接失败。"""

    policy = policy_for(QualityMode.FAST)
    assert mode_policy.VECTOR_FULL_REBUILD in policy.forbidden_strategies
    element = SourceElement(
        id="p0001-table-001",
        page=1,
        type=ElementType.TABLE,
        bbox=(1, 2, 3, 4),
    )
    element.detail["omitted"] = True
    with pytest.raises(RenderPlanError):
        plan_element(element, policy)


# --- 完整性由程序计算 -------------------------------------------------------


def test_every_required_element_has_render_strategy() -> None:
    inventory = _real_inventory()
    plan = build_render_plan(inventory, QualityMode.BALANCED)
    planned_ids = {item.element_id for item in plan.elements}
    for element in inventory.required_elements():
        assert element.id in planned_ids, f"{element.id} 没有安排去处"
    assert plan.complete is True
    assert plan.problems == []


def test_missing_visual_element_blocks_render() -> None:
    """原文有元素但计划里没有，必须直接失败。"""

    inventory = _real_inventory()
    plan = build_render_plan(inventory, QualityMode.FAST)
    assert plan.complete is True
    # 人为抽掉一个必需元素的计划条目。
    victim = next(
        item
        for item in plan.elements
        if item.element_type == ElementType.TABLE.value
    )
    plan.elements.remove(victim)
    plan.planned_elements -= 1
    assert plan.complete is False


def test_unresolved_elements_block_the_plan() -> None:
    inventory = _real_inventory()
    inventory.unresolved_elements.append(
        {"element_id": "p0001-unknown-001", "reason": ["shape-unclear"]}
    )
    plan = build_render_plan(inventory, QualityMode.FAST)
    assert plan.complete is False
    assert any("未解决元素" in problem for problem in plan.problems)


def test_completeness_is_program_computed() -> None:
    inventory = _real_inventory()
    plan = build_render_plan(inventory, QualityMode.FAST)
    payload = plan.as_dict()
    assert payload["completeness"]["computed_by"] == "program"
    # 手工把 complete 改成 True 没有用：它是算出来的。
    plan.problems.append("人为注入的问题")
    assert plan.complete is False


def test_same_page_elements_get_independent_plans() -> None:
    inventory = _real_inventory()
    plan = build_render_plan(inventory, QualityMode.FAST)
    by_page: dict[int, list] = {}
    for item in plan.elements:
        by_page.setdefault(item.page, []).append(item)
    mixed = [
        page
        for page, items in by_page.items()
        if any(item.element_type == ElementType.TABLE.value for item in items)
        and any(
            item.element_type
            in {
                ElementType.RASTER_FIGURE.value,
                ElementType.VECTOR_FIGURE.value,
            }
            for item in items
        )
    ]
    assert mixed, "样本论文应当有一页同时含图和表"
    for page in mixed:
        strategies = {
            item.element_id: item.strategy for item in by_page[page]
        }
        assert len(strategies) == len(by_page[page]), "同一元素不得有两个策略"


# --- 安全降级 ---------------------------------------------------------------


def test_complex_elements_have_three_level_fallback() -> None:
    policy = policy_for(QualityMode.FAST)
    chain = build_chain(
        "p0002-figure-001",
        ElementType.VECTOR_FIGURE,
        STRATEGY_VECTOR_OVERLAY,
        policy,
    )
    assert chain.levels[0] == STRATEGY_VECTOR_OVERLAY
    assert FALLBACK_PRESERVE_ELEMENT_REGION in chain.levels
    assert chain.levels[-1] == FALLBACK_PRESERVE_FULL_PAGE
    assert chain.next_after(STRATEGY_VECTOR_OVERLAY) == (
        FALLBACK_PRESERVE_ELEMENT_REGION
    )
    assert chain.next_after(FALLBACK_PRESERVE_FULL_PAGE) is None


def test_plain_text_does_not_need_region_fallback() -> None:
    chain = build_chain(
        "p0001-body-001",
        ElementType.BODY,
        "translate-and-reflow",
        policy_for(QualityMode.FAST),
    )
    assert chain.levels == ("translate-and-reflow",)


def test_every_complex_planned_element_has_a_fallback() -> None:
    inventory = _real_inventory()
    plan = build_render_plan(inventory, QualityMode.FAST)
    complex_types = {
        ElementType.TABLE.value,
        ElementType.VECTOR_FIGURE.value,
        ElementType.RASTER_FIGURE.value,
        ElementType.DISPLAY_FORMULA.value,
    }
    for item in plan.elements:
        if item.element_type in complex_types and item.status == "ready":
            assert item.fallback, f"{item.element_id} 没有降级方案"
            assert item.fallback_levels[-1] == FALLBACK_PRESERVE_FULL_PAGE


# --- 图表清单改为程序派生 ---------------------------------------------------


def test_inventory_complete_is_program_computed() -> None:
    inventory = _real_inventory()
    plan = build_render_plan(inventory, QualityMode.FAST)
    figure_inventory = build_figure_inventory(inventory, plan)
    assert figure_inventory["generated_by"] == "program"
    assert figure_inventory["inventory_complete"] is True
    assert figure_inventory["required_visual_elements"] == (
        figure_inventory["arranged_visual_elements"]
        + figure_inventory["omitted_visual_elements"]
    )
    assert figure_inventory["render_plan_sha256"] == plan.plan_hash()


def test_figure_inventory_covers_tables_and_figures() -> None:
    inventory = _real_inventory()
    plan = build_render_plan(inventory, QualityMode.FAST)
    figure_inventory = build_figure_inventory(inventory, plan)
    types = {item["element_type"] for item in figure_inventory["items"]}
    assert ElementType.TABLE.value in types
    assert (
        ElementType.RASTER_FIGURE.value in types
        or ElementType.VECTOR_FIGURE.value in types
    )
    for item in figure_inventory["items"]:
        assert item["render_strategy"], f"{item['id']} 没有渲染策略"


def test_incomplete_visual_arrangement_shows_in_inventory() -> None:
    inventory = _real_inventory()
    plan = build_render_plan(inventory, QualityMode.FAST)
    victim = next(
        item
        for item in plan.elements
        if item.element_type == ElementType.TABLE.value
    )
    plan.elements.remove(victim)
    figure_inventory = build_figure_inventory(inventory, plan)
    assert figure_inventory["inventory_complete"] is False
