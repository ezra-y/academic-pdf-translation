"""表格自动重建：网格是几何事实，译文从既有单元里收割。

单独运行：
    python3 -m pytest -q tests/test_table_autobuild.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.render.table_autobuild import (  # noqa: E402
    build_table_payload,
    extract_grid,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _real_tables():
    needed = [
        REAL_JOB / "source.pdf",
        REAL_JOB / "source_elements.json",
        REAL_JOB / "translation.json",
    ]
    if not all(path.is_file() for path in needed):
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    document = fitz.open(needed[0])
    elements = json.loads(needed[1].read_text(encoding="utf-8"))["elements"]
    units = json.loads(needed[2].read_text(encoding="utf-8"))["units"]
    tables = [item for item in elements if item["type"] == "table"]
    if not tables:
        pytest.skip("样本论文没有表格")
    return document, tables, units


def test_real_grids_are_extracted_with_confidence() -> None:
    """两张真实表都该抽出规整网格：列间必有空白带。"""

    document, tables, _ = _real_tables()
    for element in tables:
        grid = extract_grid(document[element["page"] - 1], element["bbox"])
        assert grid.confident, (element["id"], grid.issues)
        assert grid.column_count >= 2
        assert len(grid.rows) >= 3


def test_numbers_are_copied_verbatim_never_translated() -> None:
    """数字格逐字进中文表，一个小数位都不能动。"""

    document, tables, units = _real_tables()
    element = tables[0]
    payload = build_table_payload(
        document[element["page"] - 1], element, units, caption="表题"
    )
    assert payload is not None
    flat = [
        cell
        for row in payload["payload"]["tables"][0]["rows"]
        for cell in row
    ]
    source_text = document[element["page"] - 1].get_text(
        "text", clip=fitz.Rect(*element["bbox"])
    )
    import re

    for number in re.findall(r"\d+\.\d+", source_text):
        assert number in flat, f"数字 {number} 没有原样进表"


def test_headers_are_harvested_into_chinese() -> None:
    """表头译文从既有单元里收割：程序对齐，模型只出过字符串的力。"""

    document, tables, units = _real_tables()
    harvested_headers = 0
    for element in tables:
        payload = build_table_payload(
            document[element["page"] - 1], element, units
        )
        if payload is None:
            continue
        header = payload["payload"]["tables"][0]["rows"][0]
        import re

        if any(re.search(r"[㐀-鿿]", cell) for cell in header):
            harvested_headers += 1
    assert harvested_headers >= 1, "至少一张表的表头应当收割出中文"


def test_bold_best_values_carry_through() -> None:
    """原表粗体标各列最优值，这是语义，必须跟到中文表。"""

    document, tables, units = _real_tables()
    element = tables[0]
    payload = build_table_payload(
        document[element["page"] - 1], element, units
    )
    assert payload is not None
    table = payload["payload"]["tables"][0]
    bold_texts = [
        table["rows"][r][c]
        for r, row in enumerate(table["bold_cells"])
        for c, bold in enumerate(row)
        if bold
    ]
    assert bold_texts, "应当检出粗体最优值"
    for text in bold_texts:
        assert text.replace(".", "").replace("0", "").strip() != "", text


def test_explicit_dash_cells_survive() -> None:
    """空白格是"没测"，"-" 是"测了但不适用"，两回事，都得保住。"""

    document, tables, units = _real_tables()
    dashes = 0
    for element in tables:
        payload = build_table_payload(
            document[element["page"] - 1], element, units
        )
        if payload is None:
            continue
        for row in payload["payload"]["tables"][0]["rows"]:
            dashes += sum(1 for cell in row if cell.strip() == "-")
    assert dashes >= 1, "样本表里的显式 '-' 应当保留"


def test_unconfident_input_falls_back_to_none() -> None:
    """没把握就返回 None 走贴图保底，绝不硬拼一张错表。"""

    document, tables, units = _real_tables()
    element = dict(tables[0])
    element["bbox"] = [10, 10, 40, 30]
    assert (
        build_table_payload(document[element["page"] - 1], element, units)
        is None
    )


def test_payload_carries_coordinates_for_unit_replacement() -> None:
    """载荷必须带 page 和 source_bbox，生成器靠它把流水文字移出正文。"""

    document, tables, units = _real_tables()
    element = tables[0]
    payload = build_table_payload(
        document[element["page"] - 1], element, units
    )
    assert payload is not None
    table = payload["payload"]["tables"][0]
    assert table["page"] == element["page"]
    assert len(table["source_bbox"]) == 4
    assert payload["payload"]["suppress_texts"]


def test_unresolved_cells_are_declared_not_hidden() -> None:
    document, tables, units = _real_tables()
    for element in tables:
        payload = build_table_payload(
            document[element["page"] - 1], element, units
        )
        if payload is None:
            continue
        assert "untranslated_cells" in payload["payload"]["tables"][0]
