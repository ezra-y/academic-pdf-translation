"""统一排版中间层：绑在一起的东西不会被分页拆开。

单独运行：
    python3 -m pytest -q tests/test_layout_blocks.py
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
from academic_pdf_translation.analysis.unit_binding import (  # noqa: E402
    bind_units,
)
from academic_pdf_translation.contracts.enums import (  # noqa: E402
    ElementType,
    QualityMode,
)
from academic_pdf_translation.planning.render_plan import (  # noqa: E402
    build_render_plan,
)
from academic_pdf_translation.render.layout_blocks import (  # noqa: E402
    KIND_FIGURE,
    KIND_FOOTNOTE,
    KIND_FORMULA,
    KIND_TABLE,
    UNSPLITTABLE_KINDS,
    BlockGroup,
    LayoutBlock,
    build_blocks,
    kind_for,
)
from academic_pdf_translation.render.page_composer import (  # noqa: E402
    PageArea,
    candidate_page_map,
    compose,
)
from academic_pdf_translation.render.text_renderer import (  # noqa: E402
    TEXT_KINDS,
    measure_text_block,
    register_font,
    render_text_blocks,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _real_pipeline():
    structure = REAL_JOB / "source_structure.json"
    units = REAL_JOB / "source_units.json"
    if not structure.is_file() or not units.is_file():
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    inventory = build_inventory(
        json.loads(structure.read_text(encoding="utf-8")), pymupdf_version="1"
    )
    bind_units(
        json.loads(units.read_text(encoding="utf-8"))["units"], inventory
    )
    plan = build_render_plan(inventory, QualityMode.BALANCED)
    blocks, groups = build_blocks(inventory, plan)
    return inventory, plan, blocks, groups


def _block(**kwargs) -> LayoutBlock:
    base = {
        "id": "b1",
        "source_element_id": "e1",
        "kind": "text",
        "minimum_height": 40.0,
    }
    base.update(kwargs)
    return LayoutBlock(**base)


def _fixed_measure(block: LayoutBlock, width: float) -> float:
    return float(block.minimum_height or 40.0)


# --- 块与组 -----------------------------------------------------------------


def test_complex_kinds_are_unsplittable() -> None:
    for kind in (KIND_FIGURE, KIND_TABLE, KIND_FORMULA):
        assert kind in UNSPLITTABLE_KINDS


def test_kind_mapping_covers_content_types() -> None:
    assert kind_for(ElementType.VECTOR_FIGURE) == KIND_FIGURE
    assert kind_for(ElementType.TABLE) == KIND_TABLE
    assert kind_for(ElementType.DISPLAY_FORMULA) == KIND_FORMULA
    assert kind_for(ElementType.FOOTNOTE) == KIND_FOOTNOTE


def test_figure_and_caption_form_one_group() -> None:
    _, _, blocks, groups = _real_pipeline()
    by_id = {block.id: block for block in blocks}
    figure_groups = [
        group for group in groups if "图与图题" in group.reason
    ]
    assert figure_groups, "样本论文应当有图与图题的绑定组"
    for group in figure_groups:
        kinds = {by_id[block_id].kind for block_id in group.block_ids}
        assert KIND_FIGURE in kinds
        assert "caption" in kinds
        for block_id in group.block_ids:
            assert by_id[block_id].splittable is False


def test_table_title_and_table_stay_together() -> None:
    _, _, blocks, groups = _real_pipeline()
    by_id = {block.id: block for block in blocks}
    table_groups = [group for group in groups if "表格与表题" in group.reason]
    assert table_groups, "样本论文应当有表格与表题的绑定组"
    for group in table_groups:
        kinds = {by_id[block_id].kind for block_id in group.block_ids}
        assert KIND_TABLE in kinds
        assert "caption" in kinds


def test_layout_block_keeps_source_element_id() -> None:
    _, plan, blocks, _ = _real_pipeline()
    planned_ids = {
        item.element_id for item in plan.elements if item.status != "omitted"
    }
    for block in blocks:
        assert block.source_element_id
        assert block.source_element_id in planned_ids


def test_omitted_elements_do_not_become_blocks() -> None:
    _, plan, blocks, _ = _real_pipeline()
    omitted = {
        item.element_id for item in plan.elements if item.status == "omitted"
    }
    assert omitted, "样本论文应当有被省略的页面家具"
    block_elements = {block.source_element_id for block in blocks}
    assert not (omitted & block_elements)


# --- 分页 -------------------------------------------------------------------


def test_unsplittable_block_moves_to_next_page() -> None:
    """放不下的整块挪到下一页，而不是被切开。"""

    area = PageArea(width=595, height=842, top_margin=48, bottom_margin=48)
    filler = _block(id="b1", source_element_id="e1", minimum_height=600.0)
    figure = _block(
        id="b2",
        source_element_id="e2",
        kind=KIND_FIGURE,
        minimum_height=300.0,
        splittable=False,
        order=1,
    )
    document = compose([filler, figure], [], area, measure=_fixed_measure)
    assert document.page_of("b1") == 1
    assert document.page_of("b2") == 2
    assert document.problems == []


def test_group_moves_as_a_whole() -> None:
    area = PageArea(width=595, height=842, top_margin=48, bottom_margin=48)
    filler = _block(id="b1", source_element_id="e1", minimum_height=600.0)
    figure = _block(
        id="b2",
        source_element_id="e2",
        kind=KIND_FIGURE,
        minimum_height=200.0,
        group_id="g1",
        splittable=False,
        order=1,
    )
    caption = _block(
        id="b3",
        source_element_id="e3",
        kind="caption",
        minimum_height=40.0,
        group_id="g1",
        splittable=False,
        order=2,
    )
    group = BlockGroup(id="g1", block_ids=["b2", "b3"], reason="图与图题必须同页")
    document = compose(
        [filler, figure, caption], [group], area, measure=_fixed_measure
    )
    assert document.page_of("b2") == document.page_of("b3") == 2
    assert document.problems == []


def test_oversized_group_is_reported_not_silently_split() -> None:
    area = PageArea(width=595, height=400, top_margin=20, bottom_margin=20)
    figure = _block(
        id="b1",
        source_element_id="e1",
        kind=KIND_FIGURE,
        minimum_height=500.0,
        group_id="g1",
        splittable=False,
    )
    caption = _block(
        id="b2",
        source_element_id="e2",
        kind="caption",
        minimum_height=40.0,
        group_id="g1",
        splittable=False,
        order=1,
    )
    group = BlockGroup(id="g1", block_ids=["b1", "b2"], reason="图与图题必须同页")
    document = compose([figure, caption], [group], area, measure=_fixed_measure)
    assert document.problems, "放不下必须报出来，不能悄悄拆开"


def test_footnote_is_sent_to_footer_area() -> None:
    _, _, blocks, groups = _real_pipeline()
    area = PageArea(width=595, height=842)
    document = compose(blocks, groups, area, measure=_fixed_measure)
    footnotes = [
        placed for placed in document.placements if placed.kind == KIND_FOOTNOTE
    ]
    assert footnotes, "样本论文应当有脚注"
    for placed in footnotes:
        assert placed.area == "footer", "脚注不得进正文流"


def test_real_document_composes_without_split_groups() -> None:
    _, _, blocks, groups = _real_pipeline()
    document = compose(
        blocks, groups, PageArea(width=595, height=842), measure=_fixed_measure
    )
    assert document.problems == []
    for group in groups:
        pages = {
            document.page_of(block_id) for block_id in group.block_ids
        }
        assert len(pages) == 1, f"{group.id} 被拆到了多页"


def test_candidate_page_map_comes_from_layout_blocks() -> None:
    _, _, blocks, groups = _real_pipeline()
    document = compose(
        blocks, groups, PageArea(width=595, height=842), measure=_fixed_measure
    )
    mapping = candidate_page_map(document)
    assert len(mapping) == len({block.source_element_id for block in blocks})
    for element_id, pages in mapping.items():
        assert pages, f"{element_id} 没有候选页"
        assert pages == sorted(pages)


# --- 正文走新中间层 ---------------------------------------------------------


def test_body_renders_through_the_middle_layer(tmp_path: Path) -> None:
    """正文可以完全通过新中间层生成，分页由合成器决定。"""

    _, _, blocks, groups = _real_pipeline()
    translation = json.loads(
        (REAL_JOB / "translation.json").read_text(encoding="utf-8")
    )
    texts = {
        unit["id"]: (unit.get("translation") or unit.get("source") or "")
        for unit in translation["units"]
    }
    area = PageArea(width=595, height=842)
    document = compose(
        blocks,
        groups,
        area,
        measure=lambda block, width: measure_text_block(
            block, width, text_by_unit=texts
        ),
    )
    assert document.problems == []

    job = json.loads((REAL_JOB / "job.json").read_text(encoding="utf-8"))
    evidence = job["quality"]["selected_font_evidence"][0]
    font_name = register_font(
        "LayoutBodyTest", evidence["path"], evidence.get("subfont_index")
    )
    output = tmp_path / "body.pdf"
    report = render_text_blocks(
        blocks,
        document,
        area,
        output,
        font_name=font_name,
        text_by_unit=texts,
    )
    assert output.is_file()
    assert report["pages"] == document.pages
    assert report["rendered_blocks"], "应当有文字块被画出来"

    import fitz

    with fitz.open(output) as pdf:
        assert pdf.page_count == document.pages
        assert pdf[0].get_text().strip(), "首页必须有文字"


def test_renderer_only_handles_text_kinds() -> None:
    """文字渲染器只画文字块，复杂元素交给各自的渲染器。"""

    assert KIND_FIGURE not in TEXT_KINDS
    assert KIND_TABLE not in TEXT_KINDS
    assert KIND_FORMULA not in TEXT_KINDS
    assert KIND_FOOTNOTE not in TEXT_KINDS
