"""QA 排版度量：只量，不判。

单独运行：
    python3 -m pytest -q tests/test_qa_typography.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.qa.text_signals import (  # noqa: E402
    HAN_CHARACTER_PATTERN,
    LATIN_PROSE_PATTERN,
    PLACEHOLDER_PATTERN,
    SOURCE_MAPPING_LABEL_PATTERN,
)
from academic_pdf_translation.qa.typography import (  # noqa: E402
    body_line_width_ratio,
    body_spans,
    column_blank_ratio,
    interline_gap_outliers,
    leading_ratios,
    low_table_spans,
    orphan_single_han_lines,
    top_blank_ratio,
    weighted_font_mode,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _real_source():
    path = REAL_JOB / "source.pdf"
    if not path.is_file():
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    return fitz.open(path)


def _page_spans(page) -> tuple[list[dict], dict]:
    """按 qa_pdf 里的用法取一页的正文跨度。"""

    text_dict = page.get_text("dict")
    spans = [
        span
        for block in text_dict.get("blocks", [])
        for line in block.get("lines", [])
        for span in line.get("spans", [])
    ]
    body, _ = body_spans(page, page.number + 1, spans, {}, [])
    return (body, text_dict)


def _span(text: str, size: float, bbox: list[float]) -> dict:
    return {"text": text, "size": size, "bbox": bbox, "font": "Test"}


def _text_dict(spans: list[dict]) -> dict:
    """把一串跨度包成 PyMuPDF 的 dict 结构，每个跨度自成一行。"""

    return {
        "blocks": [
            {
                "lines": [
                    {"bbox": span["bbox"], "spans": [span]} for span in spans
                ]
            }
        ]
    }


# --- 文本信号 ---------------------------------------------------------------


def test_placeholders_that_never_got_replaced_are_recognized() -> None:
    for text in ("{{name}}", "{v 12}", "<x3>", "TODO_TRANSLATE"):
        assert PLACEHOLDER_PATTERN.search(text), text
    assert PLACEHOLDER_PATTERN.search("正常的中文") is None


def test_latin_prose_needs_several_words_in_a_row() -> None:
    assert LATIN_PROSE_PATTERN.search("this is residual english prose")
    assert LATIN_PROSE_PATTERN.search("图像分割 network") is None


def test_han_characters_are_detected() -> None:
    assert HAN_CHARACTER_PATTERN.search("图像")
    assert HAN_CHARACTER_PATTERN.search("abc") is None


def test_source_page_labels_are_recognized_in_several_languages() -> None:
    for text in ("原文第 3 页", "source page 4", "página original 5"):
        assert SOURCE_MAPPING_LABEL_PATTERN.match(text), text
    assert SOURCE_MAPPING_LABEL_PATTERN.match("原文第 3 页的图") is None


# --- 字号 -------------------------------------------------------------------


def test_the_dominant_font_size_is_weighted_by_text_length() -> None:
    """一行 40 字的正文比一个两字的标题更能代表这一页的字号。"""

    spans = [
        _span("正" * 40, 10.0, [0, 0, 100, 12]),
        _span("标题", 20.0, [0, 20, 40, 44]),
    ]
    assert weighted_font_mode(spans) == pytest.approx(10.0)


def test_no_spans_means_no_font_size() -> None:
    assert weighted_font_mode([]) is None


def test_spans_below_the_readable_floor_are_listed() -> None:
    spans = [
        _span("正文", 10.0, [0, 0, 40, 12]),
        _span("小字", 6.0, [0, 20, 40, 27]),
    ]
    regions = [{"bbox": [0, 0, 200, 200]}]
    small = low_table_spans(spans, regions, 8.0)
    assert [item["text"] for item in small] == ["小字"]


def test_spans_outside_the_table_region_are_ignored() -> None:
    spans = [_span("小字", 6.0, [500, 500, 540, 507])]
    assert low_table_spans(spans, [{"bbox": [0, 0, 100, 100]}], 8.0) == []


# --- 留白 -------------------------------------------------------------------


def test_a_page_with_no_body_text_reports_no_blank() -> None:
    """没有正文就没有"用剩的空间"可谈，返回 0 而不是 1。"""

    document = fitz.open()
    page = document.new_page(width=100, height=100)
    assert column_blank_ratio(page, []) == 0.0
    assert top_blank_ratio(page, []) == 0.0
    document.close()


def test_bottom_blank_grows_when_text_stops_higher() -> None:
    """column_blank_ratio 量的是页底用剩的竖向空间。"""

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    # 度量要求每列至少 80 个字才给结论，短了它宁可不说。
    long_text = "这是一整段足够长的正文内容用来占满统计门槛" * 5
    full = column_blank_ratio(page, [_span(long_text, 10.0, [60, 60, 500, 780])])
    short = column_blank_ratio(page, [_span(long_text, 10.0, [60, 60, 500, 300])])
    assert short > full
    document.close()


def test_top_blank_needs_enough_text_to_be_measured() -> None:
    """字太少时不给结论——几个字得不出"这页从哪里开始"。"""

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    assert top_blank_ratio(page, [_span("文", 10.0, [60, 400, 80, 412])]) == 0.0
    # 度量要求每列至少 80 个字才给结论，短了它宁可不说。
    long_text = "这是一整段足够长的正文内容用来占满统计门槛" * 5
    assert top_blank_ratio(
        page, [_span(long_text, 10.0, [60, 400, 500, 412])]
    ) > 0.0
    document.close()


# --- 行宽 -------------------------------------------------------------------


def test_line_width_returns_a_ratio_and_a_line_count() -> None:
    document = fitz.open()
    page = document.new_page(width=200, height=200)
    spans = [
        _span("满行", 10.0, [0, 0, 100, 12]),
        _span("半行", 10.0, [0, 20, 50, 32]),
    ]
    ratio, count = body_line_width_ratio(page, _text_dict(spans), spans)
    assert isinstance(count, int)
    assert ratio is None or 0.0 < ratio <= 1.0
    document.close()


# --- 行距 -------------------------------------------------------------------


def test_a_single_line_has_no_leading_to_measure() -> None:
    spans = [_span("只有一行", 10.0, [0, 0, 60, 10])]
    assert leading_ratios(_text_dict(spans), 10.0, 1, {}, []) == []


def test_leading_needs_a_known_body_font_size() -> None:
    """不知道正文字号就算不出行距倍数，返回空而不是瞎猜一个。"""

    spans = [
        _span("第一行内容", 10.0, [0, 0, 60, 10]),
        _span("第二行内容", 10.0, [0, 15, 60, 25]),
    ]
    assert leading_ratios(_text_dict(spans), None, 1, {}, []) == []


def test_a_normal_gap_is_not_reported() -> None:
    spans = [
        _span("第一行内容", 10.0, [0, 0, 60, 10]),
        _span("第二行内容", 10.0, [0, 14, 60, 24]),
    ]
    assert interline_gap_outliers(_text_dict(spans), spans, 10.0) == []


# --- 孤字行 -----------------------------------------------------------------


def test_a_full_line_is_not_an_orphan() -> None:
    spans = [_span("这是一整行正常的中文内容", 10.0, [0, 0, 200, 12])]
    assert orphan_single_han_lines(_text_dict(spans), spans) == []


def test_a_page_without_han_has_no_orphans() -> None:
    spans = [_span("plain english line", 10.0, [0, 0, 200, 12])]
    assert orphan_single_han_lines(_text_dict(spans), spans) == []


# --- 真实论文 ---------------------------------------------------------------


def test_the_real_paper_has_a_stable_body_font_size() -> None:
    """真实论文的正文字号应当稳定在一个合理区间，不是忽大忽小。"""

    document = _real_source()
    sizes = []
    for index in range(document.page_count):
        body, _ = _page_spans(document[index])
        mode = weighted_font_mode(body)
        if mode:
            sizes.append(round(mode, 2))
    assert len(sizes) >= 6, sizes
    assert 8.0 <= min(sizes) <= max(sizes) <= 12.0, sizes
    assert len(set(sizes)) <= 3, f"正文字号不该有 {len(set(sizes))} 种: {sizes}"


def test_the_real_paper_has_no_orphan_single_han_lines() -> None:
    """英文原文里不该有孤立单汉字行——有的话就是度量本身在误报。"""

    document = _real_source()
    for index in range(document.page_count):
        page = document[index]
        body, text_dict = _page_spans(page)
        assert orphan_single_han_lines(text_dict, body) == [], (
            f"第 {index + 1} 页"
        )


def test_the_real_paper_leading_is_within_a_normal_band() -> None:
    document = _real_source()
    page = document[3]
    body, text_dict = _page_spans(page)
    ratios = leading_ratios(text_dict, weighted_font_mode(body), 4, {}, [])
    assert ratios
    ordered = sorted(ratios)
    median = ordered[len(ordered) // 2]
    assert 0.5 <= median <= 3.0, median


def test_real_pages_are_not_mostly_blank() -> None:
    document = _real_source()
    checked = 0
    for index in range(document.page_count):
        page = document[index]
        body, _ = _page_spans(page)
        if not body:
            continue
        checked += 1
        assert column_blank_ratio(page, body) < 0.6, f"第 {index + 1} 页"
    assert checked >= 6


def test_every_real_gap_outlier_sits_where_a_figure_is() -> None:
    """原文排版正常，度量报出来的大空隙必须都有实物解释。

    样本论文里报出 3 处，每一处紧跟着的都是图题——那就是图占掉的位置，
    是真阳性，不是误报。如果哪天报出一处后面不是图题，那才要查。
    """

    document = _real_source()
    found = 0
    for index in range(document.page_count):
        page = document[index]
        body, text_dict = _page_spans(page)
        if not body:
            continue
        mode = weighted_font_mode(body)
        for outlier in interline_gap_outliers(text_dict, body, mode):
            found += 1
            assert outlier["gap_to_font_ratio"] >= 4.0
            assert outlier["next_text"].lstrip().lower().startswith("fig"), (
                f"第 {index + 1} 页的大空隙后面不是图题: "
                f"{outlier['next_text'][:40]!r}"
            )
    assert found >= 2, f"样本论文的图位应当被认出来，实际 {found} 处"


def test_real_pages_leave_only_a_little_blank() -> None:
    """页底和页顶都不该有大片没用上的空间。"""

    document = _real_source()
    for index in range(document.page_count):
        page = document[index]
        body, _ = _page_spans(page)
        if not body:
            continue
        assert column_blank_ratio(page, body) < 0.2, f"第 {index + 1} 页"
        assert top_blank_ratio(page, body) < 0.2, f"第 {index + 1} 页"
