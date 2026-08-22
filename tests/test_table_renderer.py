"""表格渲染器：表格不能变成普通段落。

单独运行：
    python3 -m pytest -q tests/test_table_renderer.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.render.table_renderer import (  # noqa: E402
    EMPTY_CELL,
    EXPLICIT_MISSING,
    MIN_TABLE_FONT_PT,
    MODE_PRESERVED,
    MODE_STRUCTURED,
    TableRenderError,
    assess_reliability,
    build_column_key,
    classify_cell,
    count_explicit_missing,
    decimals_preserved,
    detect_bold_cells,
    is_bold_font,
    render_table,
    verify_table_output,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _real_tables():
    source = REAL_JOB / "source.pdf"
    elements = REAL_JOB / "source_elements.json"
    if not source.is_file() or not elements.is_file():
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    data = json.loads(elements.read_text(encoding="utf-8"))
    by_id = {element["id"]: element for element in data["elements"]}
    tables = [
        element for element in data["elements"] if element["type"] == "table"
    ]
    if not tables:
        pytest.skip("样本论文没有表格")
    return fitz.open(source), tables, by_id


def _reliable_element(**detail) -> dict:
    base = {
        "id": "p0001-table-001",
        "page": 1,
        "bbox": [50, 100, 500, 300],
        "confidence": 0.95,
        "risk_flags": [],
        "detail": {"estimated_rows": 8, "estimated_columns": 5},
    }
    base["detail"].update(detail)
    return base


# --- 单元格语义 -------------------------------------------------------------


def test_blank_and_explicit_missing_are_different() -> None:
    """空白单元格是"没测"，"-" 是"测了但不适用"，两回事。"""

    assert classify_cell("   ") == EMPTY_CELL
    assert classify_cell("-") == EXPLICIT_MISSING
    assert classify_cell("–") == EXPLICIT_MISSING
    assert classify_cell("0.46") == "0.46"


def test_explicit_missing_cells_are_counted() -> None:
    assert count_explicit_missing(["0.53", "-", "", "0.46"]) == 1


def test_decimal_places_must_survive() -> None:
    """0.000420 不能变成 0.00042。"""

    assert decimals_preserved("a 0.000420 b", "a 0.00042 b") == ["0.000420"]
    assert decimals_preserved("a 0.000420 b", "a 0.000420 b") == []


def test_integers_are_not_decimal_checked() -> None:
    assert decimals_preserved("rank 1 and 10", "rank 1") == []


# --- 粗体语义 ---------------------------------------------------------------


def test_bold_font_names_are_recognized() -> None:
    for name in ("CMBX9", "Arial-Bold", "NotoSans-SemiBold", "Helvetica-Black"):
        assert is_bold_font(name) is True
    assert is_bold_font("CMR9") is False


def test_real_tables_expose_their_bold_best_values() -> None:
    """学术表格用粗体标"本列最优"，这是语义不是装饰。"""

    source, tables, _ = _real_tables()
    found: list[str] = []
    for table in tables:
        page = source[table["page"] - 1]
        found.extend(detect_bold_cells(page, table["bbox"]))
    assert found, "样本论文的表里应当有粗体标出的最优值"
    for value in found:
        assert value.strip()


# --- 可靠性判定 -------------------------------------------------------------


def test_all_conditions_needed_for_structured_rebuild() -> None:
    reliability = assess_reliability(
        _reliable_element(),
        confidence_floor=0.85,
        bold_cells=["0.9"],
        caption_element_id="c1",
        note_element_id=None,
        merged_cells_known=True,
    )
    assert reliability.reliable is True
    assert reliability.missing() == []


def test_unresolved_columns_block_structured_rebuild() -> None:
    element = _reliable_element(estimated_columns=1)
    element["risk_flags"] = [{"code": "table-columns-unresolved"}]
    reliability = assess_reliability(
        element,
        confidence_floor=0.85,
        bold_cells=[],
        caption_element_id="c1",
        note_element_id=None,
        merged_cells_known=True,
    )
    assert reliability.reliable is False
    assert "columns_known" in reliability.missing()


def test_low_grid_confidence_blocks_structured_rebuild() -> None:
    element = _reliable_element()
    element["confidence"] = 0.4
    reliability = assess_reliability(
        element,
        confidence_floor=0.85,
        bold_cells=[],
        caption_element_id="c1",
        note_element_id=None,
        merged_cells_known=True,
    )
    assert "grid_confidence_ok" in reliability.missing()


def test_missing_caption_blocks_structured_rebuild() -> None:
    reliability = assess_reliability(
        _reliable_element(),
        confidence_floor=0.85,
        bold_cells=[],
        caption_element_id=None,
        note_element_id=None,
        merged_cells_known=True,
    )
    assert "caption_found" in reliability.missing()


# --- 列头翻译键 -------------------------------------------------------------


def test_column_key_requires_a_source_unit() -> None:
    with pytest.raises(TableRenderError) as excinfo:
        build_column_key([{"translation": "我自己编的列头"}])
    assert "translation_unit_id" in str(excinfo.value)


def test_column_key_pairs_source_and_translation() -> None:
    key = build_column_key(
        [
            {
                "translation_unit_id": "u1",
                "source": "Warping Error",
                "translation": "翘曲误差",
            }
        ]
    )
    assert key == ["Warping Error -> 翘曲误差"]


# --- 真实论文 ---------------------------------------------------------------


def test_real_tables_fall_back_to_preservation(tmp_path: Path) -> None:
    """列数定不下来时保留原表，绝不硬重建。"""

    source, tables, by_id = _real_tables()
    output = fitz.open()
    for table in tables:
        page = output.new_page(width=595, height=842)
        captions = table["relations"].get("caption", [])
        rendered = render_table(
            source,
            page,
            table,
            target_bbox=[60, 120, 535, 330],
            caption_element_id=captions[0] if captions else None,
            caption_page=page.number + 1,
        )
        assert rendered.mode == MODE_PRESERVED
        assert rendered.mode != "flatten-table-to-paragraph"
        assert "columns_known" in rendered.reliability["missing"]
        assert rendered.warnings
    output.close()


def test_real_tables_keep_grid_lines(tmp_path: Path) -> None:
    """保留模式下网格线必须还在——没有线就等于压成了段落。"""

    source, tables, _ = _real_tables()
    output = fitz.open()
    rendered_list = []
    for table in tables:
        page = output.new_page(width=595, height=842)
        rendered_list.append(
            render_table(
                source,
                page,
                table,
                target_bbox=[60, 120, 535, 330],
                caption_element_id="c1",
                caption_page=page.number + 1,
            )
        )
    saved = tmp_path / "tables.pdf"
    output.save(saved)
    output.close()
    with fitz.open(saved) as check:
        for index, rendered in enumerate(rendered_list):
            assert len(check[index].get_drawings()) > 0, (
                f"{rendered.element_id} 保留后没有网格线"
            )


def test_real_tables_keep_bold_values_and_decimals(tmp_path: Path) -> None:
    source, tables, _ = _real_tables()
    output = fitz.open()
    pairs = []
    for table in tables:
        page = output.new_page(width=595, height=842)
        rendered = render_table(
            source,
            page,
            table,
            target_bbox=[60, 120, 535, 330],
            caption_element_id="c1",
            caption_page=page.number + 1,
        )
        source_page = source[table["page"] - 1]
        pairs.append(
            (
                rendered,
                source_page.get_text(
                    "text", clip=fitz.Rect(*table["bbox"])
                ),
            )
        )
    saved = tmp_path / "tables2.pdf"
    output.save(saved)
    output.close()
    with fitz.open(saved) as check:
        for index, (rendered, source_text) in enumerate(pairs):
            assert rendered.bold_cells, "应当检出粗体最优值"
            problems = verify_table_output(
                rendered,
                source_text,
                check[index].get_text(),
                candidate_drawing_count=len(check[index].get_drawings()),
            )
            assert problems == [], problems


def test_flattened_table_is_detected() -> None:
    """压平成段落的表：数字一个不少，但网格线没了。"""

    from academic_pdf_translation.render.table_renderer import RenderedTable

    rendered = RenderedTable(
        element_id="p0006-table-001",
        source_page=6,
        candidate_page=1,
        candidate_bbox=[0, 0, 1, 1],
        mode=MODE_PRESERVED,
        reliability={"reliable": False, "missing": ["columns_known"]},
        rows=8,
        columns=1,
        bold_cells=["0.000353"],
    )
    flattened = "人工水平 0.000005 0.0021 0.0010 1. U-Net 0.000353 0.0382"
    problems = verify_table_output(
        rendered,
        "0.000353 0.0382",
        flattened,
        candidate_drawing_count=0,
    )
    assert problems
    assert "压平成了段落" in problems[0]


def test_missing_bold_value_is_reported() -> None:
    from academic_pdf_translation.render.table_renderer import RenderedTable

    rendered = RenderedTable(
        element_id="t1",
        source_page=1,
        candidate_page=1,
        candidate_bbox=[0, 0, 1, 1],
        mode=MODE_PRESERVED,
        reliability={},
        rows=3,
        columns=3,
        bold_cells=["0.9203"],
    )
    problems = verify_table_output(rendered, "0.9203", "没有那个值")
    assert any("粗体最优值" in problem for problem in problems)


def test_caption_on_a_different_page_is_reported() -> None:
    from academic_pdf_translation.render.table_renderer import RenderedTable

    rendered = RenderedTable(
        element_id="t1",
        source_page=1,
        candidate_page=3,
        candidate_bbox=[0, 0, 1, 1],
        mode=MODE_PRESERVED,
        reliability={},
        rows=3,
        columns=3,
        caption_page=2,
    )
    problems = verify_table_output(rendered, "", "")
    assert any("不在同一页" in problem for problem in problems)


def test_table_font_floor_is_declared() -> None:
    assert MIN_TABLE_FONT_PT >= 7.0


def test_invalid_page_is_rejected(tmp_path: Path) -> None:
    source, tables, _ = _real_tables()
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    broken = dict(tables[0])
    broken["page"] = 999
    with pytest.raises(TableRenderError):
        render_table(source, page, broken, target_bbox=[60, 120, 535, 330])


def test_structured_mode_name_is_never_flatten() -> None:
    assert MODE_STRUCTURED != "flatten-table-to-paragraph"
    assert MODE_PRESERVED != "flatten-table-to-paragraph"
