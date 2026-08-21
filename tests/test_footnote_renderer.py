"""脚注渲染器：页底独立区域，编号跟着正文走。

单独运行：
    python3 -m pytest -q tests/test_footnote_renderer.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.render.footnote_renderer import (  # noqa: E402
    MIN_FOOTNOTE_FONT_PT,
    FootnoteEntry,
    FootnoteRenderError,
    RenderedFootnotes,
    body_marker_numbers,
    check_marker_consistency,
    footnote_font_size,
    render_footnotes,
    split_footnote_entries,
    verify_footnote_output,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"

BODY_FONT_PT = 9.96


def _real_footnotes():
    source = REAL_JOB / "source.pdf"
    elements = REAL_JOB / "source_elements.json"
    job = REAL_JOB / "job.json"
    if not all(path.is_file() for path in (source, elements, job)):
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    data = json.loads(elements.read_text(encoding="utf-8"))
    notes = [
        element for element in data["elements"] if element["type"] == "footnote"
    ]
    if not notes:
        pytest.skip("样本论文没有脚注")
    fonts = re.findall(
        r'"([^"]+\.tt[cf])"', job.read_text(encoding="utf-8")
    )
    cjk = [path for path in fonts if Path(path).is_file()]
    if not cjk:
        pytest.skip("作业里没有可用的中文字体路径")
    return fitz.open(source), notes, cjk[0]


def _entries_from(element: dict, page, marker_page: int = 1):
    raw = page.get_text("text", clip=fitz.Rect(*element["bbox"]))
    return [
        FootnoteEntry(
            element_id=element["id"],
            marker=marker,
            translation="这是一条脚注的中文译文，用来占出真实的折行宽度。",
            translation_unit_id=f"u-{marker}",
            marker_page=marker_page,
        )
        for marker, _ in split_footnote_entries(raw)
    ]


# --- 拆条 -------------------------------------------------------------------


def test_a_footnote_block_is_split_into_separate_entries() -> None:
    """抽取出来的脚注区常常是几条连在一起，不拆开就没法逐条核编号。"""

    entries = split_footnote_entries(
        "1 第一条脚注\n继续第一条\n2 第二条脚注\n3 第三条"
    )
    assert [marker for marker, _ in entries] == ["1", "2", "3"]
    assert entries[0][1] == "第一条脚注 继续第一条"


def test_text_without_markers_yields_nothing() -> None:
    assert split_footnote_entries("这段话没有编号") == []
    assert split_footnote_entries("") == []


def test_bracketed_markers_are_recognized() -> None:
    entries = split_footnote_entries("(1) 甲\n2. 乙")
    assert [marker for marker, _ in entries] == ["1", "2"]


# --- 字号 -------------------------------------------------------------------


def test_footnote_is_smaller_than_body_but_still_readable() -> None:
    size = footnote_font_size(10.5)
    assert size < 10.5
    assert size >= MIN_FOOTNOTE_FONT_PT


def test_readability_floor_wins_over_being_smaller() -> None:
    """正文本来就小的时候，宁可与正文同大，也不缩到看不清。"""

    size = footnote_font_size(8.0)
    assert size == MIN_FOOTNOTE_FONT_PT


def test_source_font_size_caps_the_result() -> None:
    assert footnote_font_size(12.0, source_font_size=8.9) == pytest.approx(8.9)


def test_zero_body_font_is_rejected() -> None:
    with pytest.raises(FootnoteRenderError):
        footnote_font_size(0.0)


# --- 编号一致 ---------------------------------------------------------------


def test_marker_mismatch_is_reported_both_ways() -> None:
    assert check_marker_consistency(["1", "2"], ["1", "2"]) == []
    assert check_marker_consistency(["1", "9"], ["1"])
    assert check_marker_consistency(["1"], ["1", "2"])


def test_real_footnote_markers_match_the_body_superscripts() -> None:
    """样本论文第 7 页是 1/2/3，第 8 页是 4，一个不差。"""

    source, notes, _ = _real_footnotes()
    seen = []
    for element in notes:
        page = source[element["page"] - 1]
        raw = page.get_text("text", clip=fitz.Rect(*element["bbox"]))
        markers = [marker for marker, _ in split_footnote_entries(raw)]
        body = body_marker_numbers(
            page,
            body_bbox=[100, 90, 500, element["bbox"][1] - 10],
            body_font_size=BODY_FONT_PT,
        )
        assert markers, f"{element['id']} 应当能拆出脚注条目"
        assert check_marker_consistency(markers, body) == []
        seen.extend(markers)
    assert seen == sorted(seen, key=int), "脚注编号应当全篇递增"


# --- 渲染 -------------------------------------------------------------------


def test_real_footnotes_render_into_a_page_bottom_zone(tmp_path: Path) -> None:
    source, notes, font_path = _real_footnotes()
    element = notes[0]
    entries = _entries_from(element, source[element["page"] - 1])
    assert len(entries) >= 2

    output = fitz.open()
    page = output.new_page(width=595, height=842)
    rendered = render_footnotes(
        page,
        entries,
        body_bottom=500.0,
        font_path=font_path,
        body_font_size=10.0,
        source_font_size=element["detail"]["font_size"],
    )
    problems = verify_footnote_output(
        rendered, page, body_bbox=[60, 80, 535, 500]
    )
    saved = tmp_path / "footnotes.pdf"
    output.save(saved)
    output.close()

    assert rendered.font_size < 10.0
    assert rendered.font_size >= MIN_FOOTNOTE_FONT_PT
    assert rendered.markers == [entry.marker for entry in entries]
    assert problems == [], problems

    with fitz.open(saved) as check:
        text = check[0].get_text()
        for entry in entries:
            assert entry.marker in text
        assert check[0].get_drawings(), "分隔线必须真的画在页面上"


def test_footnotes_never_push_into_the_body(tmp_path: Path) -> None:
    """放不下就报错，让页面合成器换页——绝不挤进正文把句子劈开。"""

    source, notes, font_path = _real_footnotes()
    element = notes[0]
    entries = _entries_from(element, source[element["page"] - 1])
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    with pytest.raises(FootnoteRenderError) as excinfo:
        render_footnotes(
            page,
            entries,
            body_bottom=800.0,
            font_path=font_path,
            body_font_size=10.0,
        )
    output.close()
    assert "不得打断正文" in str(excinfo.value)


def test_separator_line_is_short_and_above_the_text(tmp_path: Path) -> None:
    source, notes, font_path = _real_footnotes()
    element = notes[0]
    entries = _entries_from(element, source[element["page"] - 1])
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    rendered = render_footnotes(
        page,
        entries,
        body_bottom=500.0,
        font_path=font_path,
        body_font_size=10.0,
    )
    output.close()
    width = rendered.separator_bbox[2] - rendered.separator_bbox[0]
    content_width = rendered.area_bbox[2] - rendered.area_bbox[0]
    assert 0 < width < content_width * 0.5, "分隔线应当是短短一条，不是通栏"
    assert rendered.separator_bbox[1] <= rendered.area_bbox[1]


def test_untranslated_footnote_without_a_unit_is_rejected(
    tmp_path: Path,
) -> None:
    source, notes, font_path = _real_footnotes()
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    with pytest.raises(FootnoteRenderError) as excinfo:
        render_footnotes(
            page,
            [
                FootnoteEntry(
                    element_id="p0007-footnote-001",
                    marker="1",
                    translation="我自己编的脚注",
                    translation_unit_id="",
                    marker_page=1,
                )
            ],
            body_bottom=500.0,
            font_path=font_path,
            body_font_size=10.0,
        )
    output.close()
    assert "translation_unit_id" in str(excinfo.value)


def test_empty_entry_list_is_rejected(tmp_path: Path) -> None:
    source, notes, font_path = _real_footnotes()
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    with pytest.raises(FootnoteRenderError):
        render_footnotes(
            page, [], body_bottom=500.0, font_path=font_path
        )
    output.close()


def test_bad_font_path_is_reported(tmp_path: Path) -> None:
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    with pytest.raises(FootnoteRenderError) as excinfo:
        render_footnotes(
            page,
            [
                FootnoteEntry(
                    element_id="f1",
                    marker="1",
                    translation="甲",
                    translation_unit_id="u1",
                    marker_page=1,
                )
            ],
            body_bottom=500.0,
            font_path=str(tmp_path / "does-not-exist.ttf"),
        )
    output.close()
    assert "字体加载失败" in str(excinfo.value)


# --- 核对 -------------------------------------------------------------------


def test_footnote_dumped_at_the_end_of_the_document_is_caught(
    tmp_path: Path,
) -> None:
    """脚注堆到全文末尾，读者顺着正文那个 1 就再也找不回来了。"""

    source, notes, font_path = _real_footnotes()
    element = notes[0]
    entries = _entries_from(element, source[element["page"] - 1], marker_page=7)
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    rendered = render_footnotes(
        page,
        entries,
        body_bottom=500.0,
        font_path=font_path,
        body_font_size=10.0,
    )
    problems = verify_footnote_output(rendered, page)
    output.close()
    assert any("不得堆到全文末尾" in problem for problem in problems)


def test_overlap_with_the_reference_list_is_caught(tmp_path: Path) -> None:
    source, notes, font_path = _real_footnotes()
    element = notes[0]
    entries = _entries_from(element, source[element["page"] - 1])
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    rendered = render_footnotes(
        page,
        entries,
        body_bottom=500.0,
        font_path=font_path,
        body_font_size=10.0,
    )
    problems = verify_footnote_output(
        rendered, page, reference_bbox=[60, 700, 535, 800]
    )
    output.close()
    assert any("不得混进参考文献" in problem for problem in problems)


def test_missing_separator_is_caught(tmp_path: Path) -> None:
    """没有分隔线，脚注就跟正文糊成一片。"""

    output = fitz.open()
    page = output.new_page(width=595, height=842)
    rendered = RenderedFootnotes(
        candidate_page=1,
        area_bbox=[60, 700, 535, 760],
        separator_bbox=[60, 700, 200, 700],
        font_size=8.5,
        body_font_size=10.0,
        entries=[],
    )
    problems = verify_footnote_output(rendered, page)
    output.close()
    assert any("没有分隔线" in problem for problem in problems)


def test_footnote_intruding_into_the_body_is_caught(tmp_path: Path) -> None:
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    rendered = RenderedFootnotes(
        candidate_page=1,
        area_bbox=[60, 400, 535, 460],
        separator_bbox=[60, 400, 200, 400],
        font_size=8.5,
        body_font_size=10.0,
        entries=[],
    )
    problems = verify_footnote_output(
        rendered, page, body_bbox=[60, 80, 535, 600]
    )
    output.close()
    assert any("会打断正文" in problem for problem in problems)


def test_too_small_a_footnote_font_is_caught(tmp_path: Path) -> None:
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    rendered = RenderedFootnotes(
        candidate_page=1,
        area_bbox=[60, 700, 535, 760],
        separator_bbox=[60, 700, 200, 700],
        font_size=5.0,
        body_font_size=10.0,
        entries=[],
    )
    problems = verify_footnote_output(rendered, page)
    output.close()
    assert any("低于可读门槛" in problem for problem in problems)
