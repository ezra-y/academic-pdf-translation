"""QA 区域几何：搬进包之后行为要和搬出来之前逐字一致。

单独运行：
    python3 -m pytest -q tests/test_qa_geometry.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.qa.geometry import (  # noqa: E402
    all_complex_candidate_pages,
    in_any_region,
    page_selector_matches,
    pre_complex_break_pages,
    reference_area_ratio,
    region_covers_page,
    region_union_area,
    regions_for_page,
    structured_complex_candidate_pages,
    structured_table_page,
    whole_page_reference,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _real_pages():
    source = REAL_JOB / "source.pdf"
    if not source.is_file():
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    return fitz.open(source)


def _real_elements() -> list[dict]:
    path = REAL_JOB / "source_elements.json"
    if not path.is_file():
        pytest.skip("缺少真实论文的元素清单")
    return json.loads(path.read_text(encoding="utf-8"))["elements"]


# --- 页面选择 ---------------------------------------------------------------


def test_a_single_page_field_matches() -> None:
    assert page_selector_matches({"page": 3}, 3) is True
    assert page_selector_matches({"page": 3}, 4) is False


def test_a_pages_list_matches() -> None:
    assert page_selector_matches({"pages": [2, 5]}, 5) is True
    assert page_selector_matches({"pages": [2, 5]}, 4) is False


def test_a_malformed_selector_matches_nothing() -> None:
    assert page_selector_matches({}, 1) is False
    assert page_selector_matches({"pages": "3"}, 3) is False


def test_regions_are_filtered_by_page() -> None:
    items = [{"page": 1, "id": "a"}, {"page": 2, "id": "b"}, "不是字典"]
    assert [item["id"] for item in regions_for_page(items, 1)] == ["a"]


# --- 覆盖率 -----------------------------------------------------------------


def test_a_region_covering_most_of_the_page_is_detected() -> None:
    document = fitz.open()
    page = document.new_page(width=100, height=100)
    assert region_covers_page(page, {"bbox": [0, 0, 95, 95]}) is True
    assert region_covers_page(page, {"bbox": [0, 0, 10, 10]}) is False
    document.close()


def test_a_malformed_bbox_covers_nothing() -> None:
    document = fitz.open()
    page = document.new_page(width=100, height=100)
    assert region_covers_page(page, {"bbox": [0, 0, 1]}) is False
    assert region_covers_page(page, {}) is False
    document.close()


def test_overlapping_regions_are_counted_once() -> None:
    """两块区域重叠时并集面积不能重复计。"""

    regions = [
        {"bbox": [0, 0, 10, 10]},
        {"bbox": [5, 0, 15, 10]},
    ]
    assert region_union_area(regions) == pytest.approx(150.0)


def test_degenerate_regions_contribute_nothing() -> None:
    assert region_union_area([{"bbox": [5, 5, 5, 5]}, {}]) == 0.0
    assert region_union_area([]) == 0.0


def test_a_page_of_references_is_recognized() -> None:
    document = fitz.open()
    page = document.new_page(width=100, height=100)
    regions = [{"category": "references", "bbox": [0, 0, 100, 80]}]
    assert reference_area_ratio(page, regions) == pytest.approx(0.8)
    assert whole_page_reference(page, regions) is True
    document.close()


def test_a_small_reference_block_is_not_a_reference_page() -> None:
    document = fitz.open()
    page = document.new_page(width=100, height=100)
    regions = [{"category": "references", "bbox": [0, 0, 100, 10]}]
    assert whole_page_reference(page, regions) is False
    document.close()


# --- 中心点归属 -------------------------------------------------------------


def test_a_span_belongs_to_the_region_holding_its_centre() -> None:
    """按中心点判，不按相交判——一行文字常比它所属区域宽出一点。"""

    regions = [{"bbox": [0, 0, 100, 100]}]
    assert in_any_region([40, 40, 60, 60], regions) is True
    assert in_any_region([-30, 40, 10, 60], regions) is False


# --- 复杂内容落页 -----------------------------------------------------------


def test_structured_items_report_their_candidate_pages() -> None:
    complex_content = {
        "items": [
            {"id": "c1", "status": "ready", "method": "structured-table-rebuild"},
            {"id": "c2", "status": "ready", "method": "preserve-region"},
        ]
    }
    mapping = {
        "complex_items": [
            {"complex_item_id": "c1", "candidate_pages": [4]},
            {"complex_item_id": "c2", "candidate_pages": [5]},
        ]
    }
    assert structured_complex_candidate_pages(complex_content, mapping) == {4}
    assert all_complex_candidate_pages(complex_content, mapping) == {4, 5}


def test_an_absent_mapping_yields_no_pages() -> None:
    assert structured_complex_candidate_pages({"items": []}, None) == set()
    assert all_complex_candidate_pages({"items": []}, None) == set()


def test_the_page_before_a_structured_break_is_found() -> None:
    mapping = {
        "candidate_pages": [
            {"candidate_page": 3, "source_pages": [2]},
            {"candidate_page": 4, "source_pages": [2, 3]},
        ]
    }
    assert pre_complex_break_pages(mapping, {4}) == {3}


def test_the_first_page_has_nothing_before_it() -> None:
    assert pre_complex_break_pages({"candidate_pages": []}, {1}) == set()


# --- 表格页 -----------------------------------------------------------------


def test_a_table_region_marks_the_page() -> None:
    assert structured_table_page(1, [], [{"category": "Table"}]) is True


def test_an_override_can_mark_a_table_page() -> None:
    overrides = [{"page": 2, "preserve_column_structure": True}]
    assert structured_table_page(2, overrides, []) is True
    assert structured_table_page(3, overrides, []) is False


def test_an_unrelated_page_is_not_a_table_page() -> None:
    assert structured_table_page(1, [{"page": 1, "layout": "prose"}], []) is False


# --- 真实论文 ---------------------------------------------------------------


def test_real_table_bboxes_do_not_cover_their_pages() -> None:
    """真实论文里的表只占页面一小块，不该被当成整页区域。"""

    document = _real_pages()
    tables = [item for item in _real_elements() if item["type"] == "table"]
    assert tables
    for table in tables:
        page = document[table["page"] - 1]
        assert region_covers_page(page, {"bbox": table["bbox"]}) is False


def test_real_figure_and_caption_centres_fall_in_their_own_boxes() -> None:
    elements = _real_elements()
    by_id = {item["id"]: item for item in elements}
    captions = [
        item
        for item in elements
        if item["type"] == "caption"
        and (item.get("relations") or {}).get("captions-for")
    ]
    assert captions
    for caption in captions:
        assert in_any_region(caption["bbox"], [{"bbox": caption["bbox"]}])
        target = by_id.get(
            (caption["relations"]["captions-for"] or [""])[0]
        )
        if target is None:
            continue
        assert not in_any_region(caption["bbox"], [{"bbox": target["bbox"]}])


def test_real_reference_page_is_mostly_references() -> None:
    document = _real_pages()
    references = [
        item for item in _real_elements() if item["type"] == "reference-entry"
    ]
    assert references
    entry = references[0]
    page = document[entry["page"] - 1]
    regions = [{"category": "references", "bbox": entry["bbox"]}]
    assert 0.0 < reference_area_ratio(page, regions) <= 1.0
