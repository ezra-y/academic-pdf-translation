"""结构对账：逐类型清点、阅读顺序、图题同页、页数增长。

单独运行：
    python3 -m pytest -q tests/test_structural_audit.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.verify.candidate_mapping import (  # noqa: E402
    CandidateMapping,
    ElementLocation,
    build_mapping,
    element_texts_from_units,
)
from academic_pdf_translation.verify.structural_audit import (  # noqa: E402
    MAX_PAGE_GROWTH,
    StructuralAudit,
    StructuralAuditError,
    audit_structure,
    caption_splits,
    format_report,
    reading_order_inversions,
    tally_by_type,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _real_audit():
    needed = [
        REAL_JOB / "source.pdf",
        REAL_JOB / "candidate.pdf",
        REAL_JOB / "source_elements.json",
        REAL_JOB / "translation.json",
        REAL_JOB / "unit_bindings.json",
    ]
    if not all(path.is_file() for path in needed):
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    elements = json.loads(needed[2].read_text(encoding="utf-8"))["elements"]
    units = json.loads(needed[3].read_text(encoding="utf-8"))["units"]
    bindings = json.loads(needed[4].read_text(encoding="utf-8"))["bindings"]
    mapping = build_mapping(
        fitz.open(needed[0]),
        fitz.open(needed[1]),
        elements,
        element_texts=element_texts_from_units(
            elements, units, bindings=bindings
        ),
    )
    return audit_structure(mapping, elements), mapping, elements


def _location(
    element_id: str,
    element_type: str,
    page: int,
    candidate_pages: list[int],
    *,
    required: bool = True,
    bbox: list[float] | None = None,
) -> ElementLocation:
    return ElementLocation(
        element_id=element_id,
        element_type=element_type,
        source_page=page,
        required=required,
        candidate_pages=list(candidate_pages),
        candidate_bbox=bbox,
    )


# --- 逐类型清点 -------------------------------------------------------------


def test_tally_counts_each_type_separately() -> None:
    mapping = CandidateMapping(
        locations=[
            _location("b1", "body", 1, [1]),
            _location("b2", "body", 1, []),
            _location("f1", "vector-figure", 2, [2]),
        ]
    )
    tallies = {item.element_type: item for item in tally_by_type(mapping)}
    assert tallies["body"].source_count == 2
    assert tallies["body"].located_count == 1
    assert tallies["body"].missing_required == 1
    assert tallies["body"].coverage == 0.5
    assert tallies["vector-figure"].coverage == 1.0


def test_optional_elements_do_not_count_as_missing_required() -> None:
    mapping = CandidateMapping(
        locations=[_location("n1", "page-number", 1, [], required=False)]
    )
    tally = tally_by_type(mapping)[0]
    assert tally.located_count == 0
    assert tally.missing_required == 0


# --- 阅读顺序 ---------------------------------------------------------------


def test_order_inversion_is_counted() -> None:
    elements = [
        {"id": "a", "page": 1, "bbox": [0, 100, 10, 110]},
        {"id": "b", "page": 1, "bbox": [0, 200, 10, 210]},
    ]
    mapping = CandidateMapping(
        locations=[
            _location("a", "body", 1, [3]),
            _location("b", "body", 1, [1]),
        ]
    )
    examples, total, comparable = reading_order_inversions(mapping, elements)
    assert total == 1
    assert comparable == 1
    assert examples[0].earlier_in_source == "a"


def test_ambiguous_elements_are_left_out_of_the_order_check() -> None:
    """落点说不清的两个元素之间，谈不上谁先谁后。"""

    elements = [
        {"id": "a", "page": 1, "bbox": [0, 100, 10, 110]},
        {"id": "b", "page": 1, "bbox": [0, 200, 10, 210]},
    ]
    mapping = CandidateMapping(
        locations=[
            _location("a", "body", 1, [3, 4]),
            _location("b", "body", 1, [1]),
        ]
    )
    _, total, comparable = reading_order_inversions(mapping, elements)
    assert total == 0
    assert comparable == 0


def test_the_inversion_ratio_uses_the_full_count_not_the_samples() -> None:
    """样本截断到 40 条，占比还得按全数算，否则比例会凭空变小。"""

    elements = [
        {"id": f"e{index}", "page": 1, "bbox": [0, index * 10, 5, index * 10 + 5]}
        for index in range(60)
    ]
    mapping = CandidateMapping(
        locations=[
            _location(f"e{index}", "body", 1, [60 - index])
            for index in range(60)
        ]
    )
    examples, total, comparable = reading_order_inversions(mapping, elements)
    assert len(examples) == 40
    assert total == comparable == 60 * 59 // 2
    audit = StructuralAudit(
        order_inversions=examples,
        inversion_count=total,
        comparable_pairs=comparable,
    )
    assert audit.inversion_ratio == 1.0


# --- 图题同页 ---------------------------------------------------------------


def test_a_caption_away_from_its_figure_is_reported() -> None:
    elements = [
        {
            "id": "c1",
            "page": 1,
            "bbox": [0, 0, 1, 1],
            "relations": {"captions-for": ["f1"]},
        },
        {"id": "f1", "page": 1, "bbox": [0, 0, 1, 1]},
    ]
    mapping = CandidateMapping(
        locations=[
            _location("c1", "caption", 1, [4]),
            _location("f1", "raster-figure", 1, [5]),
        ]
    )
    splits = caption_splits(mapping, elements)
    assert len(splits) == 1
    assert splits[0].caption_pages == [4]
    assert splits[0].target_pages == [5]


def test_a_caption_sharing_a_page_is_fine() -> None:
    elements = [
        {
            "id": "c1",
            "page": 1,
            "bbox": [0, 0, 1, 1],
            "relations": {"captions-for": ["f1"]},
        },
        {"id": "f1", "page": 1, "bbox": [0, 0, 1, 1]},
    ]
    mapping = CandidateMapping(
        locations=[
            _location("c1", "caption", 1, [4]),
            _location("f1", "raster-figure", 1, [4]),
        ]
    )
    assert caption_splits(mapping, elements) == []


# --- 真实论文 ---------------------------------------------------------------


def test_the_real_candidate_fails_the_audit() -> None:
    """这份候选被人工复审判为不合格，对账必须得出同样的结论。"""

    audit, _, elements = _real_audit()
    assert audit.passed is False
    assert audit.missing_required > 0
    assert len(audit.tallies) > 5
    assert sum(item.source_count for item in audit.tallies) == len(elements)


def test_the_real_candidate_splits_captions_from_their_figures() -> None:
    """独立复审 R-006 说图和图题被分到了不同页，这里数出来是几处。"""

    audit, _, _ = _real_audit()
    assert audit.caption_splits, "样本候选里应当有图题与图分页的情况"
    for split in audit.caption_splits:
        assert not set(split.caption_pages) & set(split.target_pages)
    assert any("必须同页" in problem for problem in audit.problems)


def test_the_real_candidate_loses_a_figure_and_a_formula() -> None:
    audit, _, _ = _real_audit()
    by_type = {item.element_type: item for item in audit.tallies}
    assert by_type["vector-figure"].missing_required >= 1
    assert by_type["display-formula"].missing_required >= 1
    assert by_type["raster-figure"].coverage == 1.0


def test_page_growth_is_measured_against_the_source() -> None:
    audit, _, _ = _real_audit()
    assert audit.source_pages == 8
    assert audit.page_growth > 1.0
    assert audit.page_growth <= MAX_PAGE_GROWTH


def test_the_report_only_restates_the_numbers() -> None:
    audit, _, _ = _real_audit()
    report = format_report(audit)
    assert "不通过" in report
    assert f"{audit.source_pages}" in report
    assert str(audit.inversion_count) in report
    for tally in audit.tallies:
        assert tally.element_type in report


# --- 边界 -------------------------------------------------------------------


def test_an_empty_inventory_is_rejected() -> None:
    with pytest.raises(StructuralAuditError):
        audit_structure(CandidateMapping(), [])


def test_a_mapping_that_does_not_match_the_inventory_is_rejected() -> None:
    """映射和清单条数对不上，后面所有的数都不能信。"""

    with pytest.raises(StructuralAuditError) as excinfo:
        audit_structure(
            CandidateMapping(locations=[_location("a", "body", 1, [1])]),
            [{"id": "a"}, {"id": "b"}],
        )
    assert "一一对应" in str(excinfo.value)


def test_runaway_pagination_is_reported() -> None:
    mapping = CandidateMapping(
        source_pages=8,
        candidate_pages=20,
        locations=[_location("a", "body", 1, [1], bbox=[0, 0, 1, 1])],
    )
    audit = audit_structure(mapping, [{"id": "a", "page": 1, "bbox": [0, 0, 1, 1]}])
    assert audit.passed is False
    assert any("分页失控" in problem for problem in audit.problems)


def test_a_clean_candidate_passes() -> None:
    elements = [
        {"id": "a", "page": 1, "bbox": [0, 100, 10, 110]},
        {"id": "b", "page": 1, "bbox": [0, 200, 10, 210]},
    ]
    mapping = CandidateMapping(
        source_pages=1,
        candidate_pages=1,
        locations=[
            _location("a", "body", 1, [1], bbox=[0, 100, 10, 110]),
            _location("b", "body", 1, [1], bbox=[0, 200, 10, 210]),
        ],
    )
    audit = audit_structure(mapping, elements)
    assert audit.passed is True
    assert audit.problems == []
    assert audit.inversion_ratio == 0.0


# --- 量不到坐标就别排序 -----------------------------------------------------


def test_same_page_pairs_without_coordinates_are_not_comparable() -> None:
    """拿 0 顶替纵坐标，等于宣称"它在这一页最上面"——那是编出来的。"""

    elements = [
        {"id": "a", "page": 1, "bbox": [0, 100, 10, 110]},
        {"id": "b", "page": 1, "bbox": [0, 200, 10, 210]},
    ]
    mapping = CandidateMapping(
        locations=[
            _location("a", "body", 1, [1]),
            _location("b", "body", 1, [1], bbox=[0, 50, 10, 60]),
        ]
    )
    _, total, comparable = reading_order_inversions(mapping, elements)
    assert comparable == 0
    assert total == 0


def test_same_page_pairs_with_coordinates_are_compared() -> None:
    elements = [
        {"id": "a", "page": 1, "bbox": [0, 100, 10, 110]},
        {"id": "b", "page": 1, "bbox": [0, 200, 10, 210]},
    ]
    mapping = CandidateMapping(
        locations=[
            _location("a", "body", 1, [1], bbox=[0, 300, 10, 310]),
            _location("b", "body", 1, [1], bbox=[0, 50, 10, 60]),
        ]
    )
    _, total, comparable = reading_order_inversions(mapping, elements)
    assert comparable == 1
    assert total == 1


def test_cross_page_pairs_need_no_coordinates() -> None:
    """页码不同的一对，按页码比就够了。"""

    elements = [
        {"id": "a", "page": 1, "bbox": [0, 100, 10, 110]},
        {"id": "b", "page": 1, "bbox": [0, 200, 10, 210]},
    ]
    mapping = CandidateMapping(
        locations=[
            _location("a", "body", 1, [5]),
            _location("b", "body", 1, [2]),
        ]
    )
    _, total, comparable = reading_order_inversions(mapping, elements)
    assert comparable == 1
    assert total == 1
