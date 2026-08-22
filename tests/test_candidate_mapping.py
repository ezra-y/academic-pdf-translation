"""候选元素映射：只看产出的 PDF，不看计划。

单独运行：
    python3 -m pytest -q tests/test_candidate_mapping.py
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
    MAX_FINGERPRINT_DISTANCE,
    METHOD_IMAGE_DIGEST,
    METHOD_NO_EVIDENCE,
    METHOD_NOT_FOUND,
    MIN_TEXT_PROBE_CHARS,
    CandidateMapping,
    CandidateMappingError,
    ElementLocation,
    build_mapping,
    candidate_image_fingerprints,
    element_texts_from_units,
    fingerprint_contrast,
    fingerprint_distance,
    image_digests,
    locate_by_anchors,
    locate_by_pixels,
    locate_by_text,
    normalize_text,
    region_fingerprint,
    source_image_digest,
    text_anchors,
    verify_mapping,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _real_job():
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
    return (
        fitz.open(needed[0]),
        fitz.open(needed[1]),
        elements,
        units,
        bindings,
    )


def _real_mapping():
    source, candidate, elements, units, bindings = _real_job()
    texts = element_texts_from_units(elements, units, bindings=bindings)
    return (
        build_mapping(source, candidate, elements, element_texts=texts),
        elements,
        texts,
    )


# --- 文字规范化 -------------------------------------------------------------


def test_whitespace_and_control_characters_are_stripped() -> None:
    """控制字符是数学字体的抽取残渣，留着只会制造假的"缺失"。"""

    assert normalize_text("图 像\n分割") == "图像分割"
    assert normalize_text("\x10PK\x11") == "PK"


def test_a_probe_below_the_minimum_is_not_searched() -> None:
    assert locate_by_text(["图像分割在这里"], "X") == []
    assert MIN_TEXT_PROBE_CHARS >= 2


def test_short_section_headings_are_still_searchable() -> None:
    """「致谢」只有两个字，门槛定高了章节标题就会被当成找不到。"""

    assert locate_by_text(["前面的文字致谢后面的文字"], "致谢") == [1]


def test_a_probe_on_several_pages_reports_every_page() -> None:
    assert locate_by_text(["图像分割", "别的", "图像分割"], "图像分割") == [1, 3]


# --- 锚点 -------------------------------------------------------------------


def test_anchors_need_more_than_one_hit() -> None:
    pages, hits = locate_by_anchors(["1024only"], ["1024", "512", "256"])
    assert pages == []
    assert hits == 0


def test_the_page_with_the_most_anchors_wins() -> None:
    pages, hits = locate_by_anchors(
        ["1024 512", "1024 512 256"], ["1024", "512", "256"]
    )
    assert pages == [2]
    assert hits == 3


def test_real_vector_figure_exposes_its_numeric_anchors() -> None:
    source, _, elements, _, _ = _real_job()
    figures = [
        element
        for element in elements
        if element["type"] == "vector-figure"
        and (element.get("detail") or {}).get("drawing_count", 0) > 100
    ]
    if not figures:
        pytest.skip("样本论文没有大矢量图")
    anchors = text_anchors(source, figures[0])
    assert len(anchors) >= 4


# --- 单元归属 ---------------------------------------------------------------


def test_bindings_win_over_the_empty_field_on_elements() -> None:
    """元素清单自己的 translation_unit_ids 常常是空的，绑定在另一个文件里。

    拿空字段当归属，结果就是每个文字元素都"找不到"。
    """

    elements = [{"id": "e1", "translation_unit_ids": []}]
    units = [{"id": "u1", "translation": "中文译文"}]
    bindings = [{"element_id": "e1", "unit_id": "u1"}]
    assert element_texts_from_units(elements, units) == {}
    assert element_texts_from_units(elements, units, bindings=bindings) == {
        "e1": "中文译文"
    }


def test_kept_source_units_still_provide_a_probe() -> None:
    elements = [{"id": "e1"}]
    units = [{"id": "u1", "translation": "", "source": "Bioinformatics"}]
    bindings = [{"element_id": "e1", "unit_id": "u1"}]
    assert element_texts_from_units(elements, units, bindings=bindings) == {
        "e1": "Bioinformatics"
    }


def test_the_real_job_binds_most_elements_to_text() -> None:
    _, _, elements, units, bindings = _real_job()
    texts = element_texts_from_units(elements, units, bindings=bindings)
    assert len(texts) > len(elements) * 0.8


# --- 图像哈希 ---------------------------------------------------------------


def test_every_real_bitmap_is_matched_by_its_bytes(tmp_path: Path) -> None:
    """xref 搬进新文档会变，字节不会。所以按字节认。"""

    source, candidate, elements, _, _ = _real_job()
    digests = image_digests(candidate)
    images = [
        element for element in elements if element["type"] == "raster-figure"
    ]
    assert images
    for element in images:
        digest = source_image_digest(source, element)
        assert digest, f"{element['id']} 应当能算出原图哈希"
        assert digest in digests, f"{element['id']} 的图在候选里找不到"


def test_real_bitmaps_all_locate_by_digest() -> None:
    mapping, elements, _ = _real_mapping()
    images = [
        item
        for item in mapping.locations
        if item.element_type == "raster-figure"
    ]
    assert len(images) == 10
    assert all(item.method == METHOD_IMAGE_DIGEST for item in images)
    assert all(item.confidence == 1.0 for item in images)
    assert all(len(item.candidate_pages) == 1 for item in images)


# --- 几何 -------------------------------------------------------------------


def test_geometry_is_not_judged_when_there_is_none() -> None:
    location = ElementLocation(
        element_id="e1",
        element_type="body",
        source_page=1,
        required=True,
        candidate_pages=[1],
    )
    assert location.geometry_ok is None


def test_real_architecture_figure_lost_its_geometry() -> None:
    """样本候选里图内文字漏进了正文，213 个绘图对象一个都没跟过来。

    这正是只看文字会漏掉的那类失败：字都在，图没了。
    """

    mapping, _, _ = _real_mapping()
    big = [
        item
        for item in mapping.locations
        if item.source_drawing_count > 100
    ]
    assert big, "样本应当有一张大矢量图"
    item = big[0]
    assert item.located, "它的文字锚点确实能查到"
    assert item.geometry_ok is False
    assert item.candidate_drawing_count < item.source_drawing_count


def test_the_geometry_gap_is_reported() -> None:
    mapping, _, _ = _real_mapping()
    problems = verify_mapping(mapping)
    assert any("几何结构没跟过来" in problem for problem in problems)


def test_a_figure_with_no_page_holding_enough_drawings_is_missing() -> None:
    mapping, _, _ = _real_mapping()
    gone = [
        item
        for item in mapping.locations
        if item.element_type == "vector-figure" and not item.located
    ]
    assert gone, "样本候选里应当有整张丢失的矢量图"
    assert all("几何结构没有搬过来" in item.evidence for item in gone)


# --- 完整性是数出来的 -------------------------------------------------------


def test_completeness_is_computed_not_declared() -> None:
    mapping = CandidateMapping(
        locations=[
            ElementLocation(
                element_id="e1",
                element_type="body",
                source_page=1,
                required=True,
            )
        ]
    )
    assert mapping.complete is False
    with pytest.raises(AttributeError):
        mapping.complete = True  # type: ignore[misc]


def test_the_real_candidate_is_not_complete() -> None:
    """这份候选被人工复审判为不合格，映射也必须得出同样的结论。"""

    mapping, elements, _ = _real_mapping()
    assert len(mapping.locations) == len(elements)
    assert mapping.complete is False
    assert mapping.missing_required
    assert mapping.as_dict()["complete"] is False


def test_mapping_records_both_page_counts() -> None:
    mapping, _, _ = _real_mapping()
    assert mapping.source_pages == 8
    assert mapping.candidate_pages >= mapping.source_pages


# --- 边界 -------------------------------------------------------------------


def test_an_empty_inventory_is_rejected() -> None:
    source, candidate, _, _, _ = _real_job()
    with pytest.raises(CandidateMappingError):
        build_mapping(source, candidate, [])


def test_a_single_character_probe_is_reported_as_unlocatable() -> None:
    """一个 'X' 查不到，不等于内容丢了——那是数学字体的残渣。"""

    mapping, _, texts = _real_mapping()
    unlocatable = [
        item
        for item in mapping.locations
        if item.method == METHOD_NO_EVIDENCE
    ]
    assert unlocatable
    assert all(not item.located for item in unlocatable)
    assert any("无法定位" in item.evidence for item in unlocatable)


def test_missing_and_unlocatable_are_different_methods() -> None:
    mapping, _, _ = _real_mapping()
    methods = {item.method for item in mapping.locations}
    assert METHOD_NOT_FOUND in methods
    assert METHOD_NO_EVIDENCE in methods


def test_required_but_unlocatable_is_still_reported() -> None:
    mapping = CandidateMapping(
        locations=[
            ElementLocation(
                element_id="e1",
                element_type="body",
                source_page=1,
                required=True,
                method=METHOD_NO_EVIDENCE,
                evidence="没有证据",
            )
        ]
    )
    problems = verify_mapping(mapping)
    assert any("没有任何可定位的证据" in problem for problem in problems)


# --- 像素指纹 ---------------------------------------------------------------


def test_a_region_matches_itself() -> None:
    """同一块区域，指纹差应当接近 0。"""

    source, _, elements, _, _ = _real_job()
    figures = [
        item for item in elements if item["type"] == "vector-figure"
    ]
    assert figures
    element = figures[0]
    page = source[element["page"] - 1]
    rect = fitz.Rect(*element["bbox"])
    first = region_fingerprint(page, rect)
    second = region_fingerprint(page, rect)
    assert first is not None
    assert fingerprint_distance(first, second) == 0.0


def test_two_different_regions_do_not_match() -> None:
    source, _, elements, _, _ = _real_job()
    by_type: dict[str, dict] = {}
    for item in elements:
        by_type.setdefault(item["type"], item)
    first_element = by_type["vector-figure"]
    second_element = by_type["table"]
    first = region_fingerprint(
        source[first_element["page"] - 1], fitz.Rect(*first_element["bbox"])
    )
    second = region_fingerprint(
        source[second_element["page"] - 1], fitz.Rect(*second_element["bbox"])
    )
    assert fingerprint_distance(first, second) > MAX_FINGERPRINT_DISTANCE


def test_a_blank_region_has_no_contrast() -> None:
    """两块空白永远长得一样，所以不拿它们比。"""

    document = fitz.open()
    page = document.new_page(width=200, height=200)
    grid = region_fingerprint(page, page.rect)
    document.close()
    assert grid is not None
    assert fingerprint_contrast(grid) < 1.0


def test_a_flat_region_is_not_matched_by_pixels() -> None:
    document = fitz.open()
    document.new_page(width=200, height=200)
    element = {"id": "e1", "page": 1, "bbox": [10, 10, 190, 190]}
    pages, bbox, _distance = locate_by_pixels(
        document, element, [(1, [0, 0, 1, 1], [128] * 256)]
    )
    document.close()
    assert pages == []
    assert bbox is None


def test_a_tiny_region_yields_no_fingerprint() -> None:
    source, _, _, _, _ = _real_job()
    grid = region_fingerprint(source[0], fitz.Rect(10, 10, 12, 12))
    assert grid is None


def test_mismatched_fingerprints_are_infinitely_far() -> None:
    assert fingerprint_distance([], []) == float("inf")
    assert fingerprint_distance([1, 2], [1]) == float("inf")


def test_the_first_candidate_has_fingerprints_for_its_images() -> None:
    _, candidate, _, _, _ = _real_job()
    fingerprints = candidate_image_fingerprints(candidate)
    assert fingerprints
    for page, bbox, grid in fingerprints:
        assert page >= 1
        assert len(bbox) == 4
        assert len(grid) == 256
