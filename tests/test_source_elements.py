"""原文元素清单：程序自己数出每一页有几个东西。

真实论文回归用 benchmarks/papers-real 下的开放获取论文。这些 PDF 受版权
保护、不入库，缺失时测试会带着明确原因跳过，而不是假装通过。

单独运行：
    python3 -m pytest -q tests/test_source_elements.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from _fixtures import make_job  # noqa: E402
from academic_pdf_translation.analysis.detectors.figures import (  # noqa: E402
    cluster_boxes,
    detect_vector_figures,
)
from academic_pdf_translation.analysis.detectors.formulas import (  # noqa: E402
    looks_like_formula,
)
from academic_pdf_translation.analysis.detectors.text_roles import (  # noqa: E402
    is_affiliation,
    is_publication_stamp,
    looks_like_heading,
)
from academic_pdf_translation.analysis.source_elements import (  # noqa: E402
    ELEMENTS_FILE_NAME,
    analyze_job_elements,
    build_inventory,
    cache_key,
)
from academic_pdf_translation.contracts.enums import ElementType  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REAL_PAPERS = ROOT / "benchmarks" / "papers-real"
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _real_structure() -> dict:
    path = REAL_JOB / "source_structure.json"
    if not path.is_file():
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库，请先本地重建该作业"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _page(structure: dict, number: int) -> dict:
    for page in structure["pages"]:
        if int(page["page"]) == number:
            return page
    raise AssertionError(f"原文没有第 {number} 页")


def _visual_page(structure: dict) -> dict:
    """找绘图对象最多的一页，不写死页码。"""

    return max(
        structure["pages"],
        key=lambda page: int(page.get("drawing_count") or 0),
    )


# --- 纯单元测试：不依赖任何论文 ---------------------------------------------


def test_dense_drawings_form_one_vector_figure() -> None:
    """几百个相邻绘图对象必须聚成一个矢量图，不是几百张图。"""

    boxes = [
        [40 + (index % 20) * 12, 90 + (index // 20) * 12,
         48 + (index % 20) * 12, 100 + (index // 20) * 12]
        for index in range(213)
    ]
    page = {
        "page": 1,
        "width": 600.0,
        "height": 800.0,
        "drawing_bboxes": boxes,
        "drawing_count": len(boxes),
    }
    figures = detect_vector_figures(page)
    assert len(figures) == 1
    assert figures[0]["drawing_count"] == 213
    assert figures[0]["confidence"] >= 0.9


def test_distant_drawings_do_not_merge() -> None:
    """离得远的两组图形必须是两个元素。"""

    # 两组都要够大，否则会（正确地）被当成装饰线过滤掉。
    left = [[10 + i * 20, 100, 26 + i * 20, 400] for i in range(8)]
    right = [[400 + i * 20, 100, 416 + i * 20, 400] for i in range(8)]
    page = {
        "page": 1,
        "width": 600.0,
        "height": 800.0,
        "drawing_bboxes": left + right,
        "drawing_count": 16,
    }
    assert len(detect_vector_figures(page)) == 2


def test_thin_rules_are_not_a_figure() -> None:
    """几条细横线是分隔线，不是图。"""

    page = {
        "page": 1,
        "width": 600.0,
        "height": 800.0,
        "drawing_bboxes": [[100, 100 + i * 20, 500, 101 + i * 20] for i in range(6)],
        "drawing_count": 6,
    }
    assert detect_vector_figures(page) == []


def test_cluster_boxes_is_transitive() -> None:
    boxes = [(0, 0, 10, 10), (12, 0, 22, 10), (24, 0, 34, 10)]
    assert len(cluster_boxes(boxes, gap=5.0)) == 1


def test_arxiv_stamp_is_not_heading() -> None:
    stamp = "arXiv:1505.04597v1 [cs.CV] 18 May 2015"
    assert looks_like_heading(stamp) is False
    assert is_publication_stamp(stamp) is True


def test_affiliation_is_not_heading() -> None:
    text = "University of Freiburg, Germany"
    assert looks_like_heading(text) is False
    assert is_affiliation(text) is True


def test_mid_sentence_fragment_is_not_heading() -> None:
    assert looks_like_heading("where ak(x) denotes the") is False


def test_figure_label_is_not_heading() -> None:
    for label in ("max pool 2x2", "conv 3x3, ReLU", "up-conv 2x2"):
        assert looks_like_heading(label) is False


def test_real_headings_are_recognized() -> None:
    for text in ("1 Introduction", "3.1 Data Augmentation", "References"):
        assert looks_like_heading(text) is True


def test_display_formula_shape() -> None:
    assert looks_like_formula("E = Σ x∈Ω w(x) log(p(x))") is True
    assert looks_like_formula(
        "The energy function is computed by a pixel-wise soft-max."
    ) is False


def test_stable_element_ids_do_not_change_between_runs() -> None:
    structure = _real_structure()
    first = build_inventory(structure, pymupdf_version="1")
    second = build_inventory(structure, pymupdf_version="1")
    assert [element.id for element in first.elements] == [
        element.id for element in second.elements
    ]
    assert first.cache_key == second.cache_key


def test_cache_key_changes_with_detector_version() -> None:
    base = cache_key("abc", pymupdf_version="1.28.0")
    assert base != cache_key("abc", pymupdf_version="2.0.0")
    assert base != cache_key(
        "abc", pymupdf_version="1.28.0", detector_version="elements-v2"
    )
    assert base != cache_key("def", pymupdf_version="1.28.0")


# --- 真实论文回归 -----------------------------------------------------------


def test_dense_vector_page_becomes_one_figure_element() -> None:
    """真实论文里绘图对象最密的那一页，必须聚成一个矢量图元素。"""

    structure = _real_structure()
    page = _visual_page(structure)
    assert int(page["drawing_count"]) > 100, "样本论文应当有一页密集矢量图"
    inventory = build_inventory(structure, pymupdf_version="1")
    figures = [
        element
        for element in inventory.by_page(int(page["page"]))
        if element.type is ElementType.VECTOR_FIGURE
    ]
    assert len(figures) == 1
    assert figures[0].detail["drawing_count"] == int(page["drawing_count"])
    assert any(risk.code == "dense-vector" for risk in figures[0].risk_flags)


def test_real_tables_are_detected_with_captions() -> None:
    """真实论文的表格必须被检出，而且绑上表题。"""

    structure = _real_structure()
    inventory = build_inventory(structure, pymupdf_version="1")
    tables = [
        element
        for element in inventory.elements
        if element.type is ElementType.TABLE
    ]
    assert len(tables) >= 2, "样本论文至少有两张表"
    for table in tables:
        assert table.confidence >= 0.7
        assert table.relations.get("caption"), f"{table.id} 没有绑定表题"


def test_page_can_contain_multiple_complex_elements() -> None:
    """同一页有图又有表时，必须是两个独立元素。"""

    structure = _real_structure()
    inventory = build_inventory(structure, pymupdf_version="1")
    mixed = [
        page
        for page in range(1, inventory.page_count + 1)
        if any(
            element.type is ElementType.TABLE
            for element in inventory.by_page(page)
        )
        and any(
            element.type
            in {ElementType.RASTER_FIGURE, ElementType.VECTOR_FIGURE}
            for element in inventory.by_page(page)
        )
    ]
    assert mixed, "样本论文应当有一页同时含图和表"
    for page in mixed:
        elements = inventory.by_page(page)
        tables = [e for e in elements if e.type is ElementType.TABLE]
        figures = [
            e
            for e in elements
            if e.type in {ElementType.RASTER_FIGURE, ElementType.VECTOR_FIGURE}
        ]
        assert tables and figures
        for table in tables:
            for figure in figures:
                assert table.id != figure.id
                assert table.bbox != figure.bbox


def test_caption_is_linked_to_nearest_visual() -> None:
    structure = _real_structure()
    inventory = build_inventory(structure, pymupdf_version="1")
    captions = [
        element
        for element in inventory.elements
        if element.type is ElementType.CAPTION
    ]
    assert captions, "样本论文应当有图题或表题"
    bound = [
        caption for caption in captions if caption.relations.get("captions-for")
    ]
    assert len(bound) >= len(captions) - 1, "绝大多数图题必须绑上主体"
    for caption in bound:
        target_id = caption.relations["captions-for"][0]
        target = inventory.by_id(target_id)
        assert target is not None
        assert target.page == caption.page, "图题与主体必须在同一页登记"


def test_footnote_is_not_body() -> None:
    structure = _real_structure()
    inventory = build_inventory(structure, pymupdf_version="1")
    footnotes = [
        element
        for element in inventory.elements
        if element.type is ElementType.FOOTNOTE
    ]
    assert footnotes, "样本论文应当有脚注"
    for footnote in footnotes:
        assert footnote.type is not ElementType.BODY
        assert "footnote-zone" in footnote.signals


def test_display_formulas_become_elements() -> None:
    structure = _real_structure()
    inventory = build_inventory(structure, pymupdf_version="1")
    formulas = [
        element
        for element in inventory.elements
        if element.type is ElementType.DISPLAY_FORMULA
    ]
    assert formulas, "样本论文应当有独立公式"


def test_embedded_labels_are_not_headings() -> None:
    """图里的文字标签绝不能被排成章节标题。"""

    structure = _real_structure()
    inventory = build_inventory(structure, pymupdf_version="1")
    labels = [
        element
        for element in inventory.elements
        if element.detail.get("role") == "embedded-label"
    ]
    assert labels, "密集矢量图里应当有图内标签"
    for label in labels:
        assert label.type is not ElementType.HEADING


def test_publication_stamp_is_not_heading_in_real_paper() -> None:
    structure = _real_structure()
    inventory = build_inventory(structure, pymupdf_version="1")
    stamps = [
        element
        for element in inventory.elements
        if element.type is ElementType.PUBLICATION_METADATA
    ]
    assert stamps, "样本论文应当有预印本标识戳"
    for stamp in stamps:
        assert stamp.type is not ElementType.HEADING


def test_element_analysis_reuses_source_scan(tmp_path: Path) -> None:
    """元素分析只读已经在磁盘上的结构文件，不重新扫描原文。"""

    job_dir = make_job(tmp_path)
    source_pdf = job_dir / "source.pdf"
    assert source_pdf.is_file()
    stamp_before = source_pdf.stat().st_mtime_ns
    source_pdf.chmod(0o444)
    try:
        inventory = analyze_job_elements(job_dir, pymupdf_version="1")
    finally:
        source_pdf.chmod(0o644)
    assert inventory.elements
    assert source_pdf.stat().st_mtime_ns == stamp_before
    assert (job_dir / ELEMENTS_FILE_NAME).is_file()


def test_every_required_element_has_a_stable_id(tmp_path: Path) -> None:
    job_dir = make_job(tmp_path)
    inventory = analyze_job_elements(job_dir, pymupdf_version="1")
    ids = [element.id for element in inventory.elements]
    assert len(ids) == len(set(ids)), "元素 ID 不能重复"
    for element in inventory.required_elements():
        assert element.id
        assert element.page >= 1
