"""公式渲染器：不重新输入数学结构。

单独运行：
    python3 -m pytest -q tests/test_formula_renderer.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.render.formula_renderer import (  # noqa: E402
    FORMULA_SIDE_PADDING_PT,
    FormulaRenderError,
    detect_retypeset_formula,
    fits_on_one_page,
    formula_region,
    math_artifacts,
    render_formula,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _real_formulas():
    source = REAL_JOB / "source.pdf"
    elements = REAL_JOB / "source_elements.json"
    if not source.is_file() or not elements.is_file():
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    data = json.loads(elements.read_text(encoding="utf-8"))
    formulas = [
        element
        for element in data["elements"]
        if element["type"] == "display-formula"
    ]
    if not formulas:
        pytest.skip("样本论文没有独立公式")
    return fitz.open(source), formulas


class _Rect:
    def __init__(self, x0: float, y0: float, x1: float, y1: float) -> None:
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1


# --- 区域计算 ---------------------------------------------------------------


def test_formula_region_extends_to_reach_the_number() -> None:
    """公式编号在行末靠版心右缘，按元素坐标直接截会切掉它。"""

    page = _Rect(0, 0, 595, 842)
    region = formula_region([100, 200, 300, 240], page)
    assert region[2] > 300, "区域必须向右展到能包住公式编号"
    assert region[2] <= 595


def test_formula_region_pads_the_left_edge() -> None:
    page = _Rect(0, 0, 595, 842)
    region = formula_region([100, 200, 300, 240], page)
    assert region[0] == pytest.approx(100 - FORMULA_SIDE_PADDING_PT)


def test_formula_region_without_number_stays_tight() -> None:
    page = _Rect(0, 0, 595, 842)
    region = formula_region([100, 200, 300, 240], page, include_number=False)
    assert region[2] == pytest.approx(300 + FORMULA_SIDE_PADDING_PT)


def test_formula_region_needs_valid_bbox() -> None:
    with pytest.raises(FormulaRenderError):
        formula_region(None, _Rect(0, 0, 595, 842))


def test_formula_must_fit_on_one_page() -> None:
    assert fits_on_one_page((0, 0, 400, 120), 300.0) is True
    assert fits_on_one_page((0, 0, 400, 400), 300.0) is False


# --- 伪影语义 ---------------------------------------------------------------


def test_math_artifacts_are_inherited_not_introduced() -> None:
    """原文文字层本来就有 X、P 这类伪影，光看候选说明不了问题。"""

    source = "PK\nX\nk=1 exp(a(x))"
    candidate = "PK\nX\nk=1 exp(a(x))"
    assert math_artifacts(source) == ["X"]
    assert detect_retypeset_formula(source, candidate) == []


def test_retypeset_formula_is_detected() -> None:
    """把公式当文字重打，会多出原文没有的伪影。"""

    source = "E = Σ w(x) log p(x)  (1)"
    retypeset = "E =\nX\nx∈Ω\nw(x) log(p(x)) (1)"
    assert detect_retypeset_formula(source, retypeset) == ["X"]


def test_inline_letters_are_not_artifacts() -> None:
    """正文里正常出现的 X、P、K 不算伪影，只认孤立成行的。"""

    assert math_artifacts("The X axis shows P values for K classes.") == []


# --- 真实论文 ---------------------------------------------------------------


def test_real_formulas_are_preserved_not_retypeset(tmp_path: Path) -> None:
    """真实论文的独立公式原样搬过去，候选不得比原文多出伪影。"""

    source, formulas = _real_formulas()
    output = fitz.open()
    source_texts: list[str] = []
    for element in formulas:
        page = output.new_page(width=595, height=842)
        result = render_formula(
            source, page, element, target_bbox=[60, 100, 535, 200]
        )
        assert result.mode == "vector", "公式应当以矢量方式保留"
        assert result.element_id == element["id"]
        source_page = source[element["page"] - 1]
        source_texts.append(
            source_page.get_text(
                "text",
                clip=fitz.Rect(*formula_region(element["bbox"], source_page.rect)),
            )
        )
    saved = tmp_path / "formulas.pdf"
    output.save(saved)
    output.close()

    with fitz.open(saved) as check:
        assert check.page_count == len(formulas)
        for index, source_text in enumerate(source_texts):
            new_artifacts = detect_retypeset_formula(
                source_text, check[index].get_text()
            )
            assert new_artifacts == [], (
                f"第 {index + 1} 个公式多出了原文没有的伪影 {new_artifacts}，"
                "说明它被重新排过"
            )


def test_multi_fragment_formula_is_rendered_as_one_block(
    tmp_path: Path,
) -> None:
    """被抽取拆散的公式，渲染出来必须是一块，不是几行碎片。"""

    source, formulas = _real_formulas()
    multi = [
        element
        for element in formulas
        if int(element["detail"].get("fragment_count") or 1) > 1
    ]
    if not multi:
        pytest.skip("样本论文没有被拆散的公式")
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    result = render_formula(
        source, page, multi[0], target_bbox=[60, 100, 535, 220]
    )
    assert result.fragment_count > 1
    assert len(result.candidate_bbox) == 4
    saved = tmp_path / "multi.pdf"
    output.save(saved)
    output.close()
    with fitz.open(saved) as check:
        assert check.page_count == 1, "整个公式必须落在同一页"


def test_formula_render_rejects_bad_page(tmp_path: Path) -> None:
    source, formulas = _real_formulas()
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    broken = dict(formulas[0])
    broken["page"] = 999
    with pytest.raises(FormulaRenderError):
        render_formula(source, page, broken, target_bbox=[60, 100, 535, 200])


def test_formula_raster_fallback_still_preserves(tmp_path: Path) -> None:
    source, formulas = _real_formulas()
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    result = render_formula(
        source,
        page,
        formulas[0],
        target_bbox=[60, 100, 535, 200],
        force_raster=True,
    )
    assert result.mode == "raster"
    saved = tmp_path / "raster-formula.pdf"
    output.save(saved)
    output.close()
    with fitz.open(saved) as check:
        assert len(check[0].get_images()) == 1
