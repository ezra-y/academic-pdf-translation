"""参考文献渲染器：断词要判、URL 不许插空格。

单独运行：
    python3 -m pytest -q tests/test_reference_renderer.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.render.reference_renderer import (  # noqa: E402
    DECISION_JOIN,
    DECISION_KEEP,
    ReferenceRenderError,
    build_hyphenated_forms,
    build_vocabulary,
    decide_hyphen,
    extract_urls,
    join_lines,
    normalize_reference_text,
    render_references,
    source_urls,
    split_reference_entries,
    verify_reference_output,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _real_references():
    source = REAL_JOB / "source.pdf"
    elements = REAL_JOB / "source_elements.json"
    if not source.is_file() or not elements.is_file():
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    data = json.loads(elements.read_text(encoding="utf-8"))
    refs = [
        element
        for element in data["elements"]
        if element["type"] == "reference-entry"
    ]
    if not refs:
        pytest.skip("样本论文没有参考文献元素")
    document = fitz.open(source)
    element = refs[0]
    raw = document[element["page"] - 1].get_text(
        "text", clip=fitz.Rect(*element["bbox"])
    )
    full = "\n".join(page.get_text("text") for page in document)
    return element, raw, full


# --- 词表 -------------------------------------------------------------------


def test_vocabulary_excludes_the_fragments_it_is_judging() -> None:
    """词表不能"自己证明自己"：断词的两个碎片必须先剔掉。"""

    vocabulary = build_vocabulary("neural net-\nworks here")
    assert vocabulary.get("net") is None
    assert vocabulary.get("works") is None
    assert vocabulary.get("neural") == 1


def test_vocabulary_excludes_url_fragments() -> None:
    """域名里的 com 不是英文词，混进词表会把 com-pensate 判错。"""

    vocabulary = build_vocabulary("see http://example.com/path for details")
    assert vocabulary.get("com") is None
    assert vocabulary.get("details") == 1


def test_hyphenated_forms_are_collected() -> None:
    forms = build_hyphenated_forms("state-of-the-art results and human-level work")
    assert "human-level" in forms


# --- 断词判定 ---------------------------------------------------------------


def test_joined_word_seen_elsewhere_means_soft_hyphen() -> None:
    vocabulary = build_vocabulary("convolutional networks are networks")
    decision = decide_hyphen("net", "works", vocabulary, set())
    assert decision.decision == DECISION_JOIN
    assert "出现过" in decision.evidence


def test_hyphenated_form_seen_elsewhere_means_real_hyphen() -> None:
    vocabulary = build_vocabulary("a human-level result")
    forms = build_hyphenated_forms("a human-level result")
    decision = decide_hyphen("human", "level", vocabulary, forms)
    assert decision.decision == DECISION_KEEP
    assert decision.uncertain is False


def test_no_evidence_but_a_real_left_word_is_flagged_not_guessed() -> None:
    """两样证据都没有时保留连字符并标记证据不足，不悄悄拼词。"""

    vocabulary = build_vocabulary("the human factor matters")
    decision = decide_hyphen("human", "level", vocabulary, set())
    assert decision.decision == DECISION_KEEP
    assert decision.uncertain is True


def test_a_meaningless_left_fragment_means_soft_hyphen() -> None:
    vocabulary = build_vocabulary("nothing relevant here")
    decision = decide_hyphen("Guadar", "rama", vocabulary, set())
    assert decision.decision == DECISION_JOIN
    assert decision.uncertain is False


# --- 合行 -------------------------------------------------------------------


def test_urls_are_joined_without_a_space() -> None:
    text = "see http://example.com/\npath/page.html now"
    assert "http://example.com/path/page.html" in join_lines(text)


def test_ordinary_lines_are_joined_with_a_space() -> None:
    assert join_lines("hello\nworld") == "hello world"


def test_order_matters_hyphen_first_then_join() -> None:
    """先合行会把断词痕迹抹掉，再也判不出来——所以顺序写死在函数里。"""

    vocabulary = build_vocabulary("neural networks everywhere networks")
    cleaned, decisions = normalize_reference_text(
        "Deep neural net-\nworks segment", vocabulary, set()
    )
    assert "networks" in cleaned
    assert "net- works" not in cleaned
    assert [item.decision for item in decisions] == [DECISION_JOIN]


# --- 拆条 -------------------------------------------------------------------


def test_entries_split_on_several_numbering_styles() -> None:
    entries = split_reference_entries("1. 甲\n[2] 乙\n(3) 丙")
    assert [entry.number for entry in entries] == ["1", "2", "3"]


def test_text_without_numbering_yields_nothing() -> None:
    assert split_reference_entries("没有编号的一段话") == []


# --- 真实论文 ---------------------------------------------------------------


def test_real_bibliography_is_fully_split() -> None:
    element, raw, full = _real_references()
    rendered = render_references(element["id"], raw, document_text=full)
    assert rendered.numbers == [str(index) for index in range(1, 15)]


def test_real_soft_hyphens_are_joined() -> None:
    """R-011：net-works 必须变回 networks。"""

    element, raw, full = _real_references()
    rendered = render_references(element["id"], raw, document_text=full)
    text = " ".join(entry["text"] for entry in rendered.entries)
    for broken, whole in (
        ("net-works", "networks"),
        ("un-supervised", "unsupervised"),
        ("seg-mentation", "segmentation"),
        ("con-volutional", "convolutional"),
        ("Guadar-rama", "Guadarrama"),
    ):
        assert broken not in text, f"{broken} 应当被合并"
        assert whole in text, f"{whole} 应当出现在合并后的文本里"


def test_real_compound_hyphen_survives_and_is_flagged() -> None:
    """human-level 是真连字符，合成 humanlevel 就是造词。"""

    element, raw, full = _real_references()
    rendered = render_references(element["id"], raw, document_text=full)
    text = " ".join(entry["text"] for entry in rendered.entries)
    assert "human-level" in text
    assert "humanlevel" not in text
    assert any("证据不足" in warning for warning in rendered.warnings)
    uncertain = [
        item for item in rendered.hyphen_decisions if item["uncertain"]
    ]
    assert [(item["left"], item["right"]) for item in uncertain] == [
        ("human", "level")
    ]


def test_real_urls_are_rejoined_without_spaces() -> None:
    """R-011：URL 被行末切断后合并时不许插空格，否则点不开。"""

    element, raw, full = _real_references()
    rendered = render_references(element["id"], raw, document_text=full)
    assert len(rendered.urls) == 2
    for url in rendered.urls:
        assert " " not in url
        assert url.startswith("http://")
    assert any("Welcome.html" in url for url in rendered.urls)


def test_source_urls_do_not_swallow_the_next_entry_number() -> None:
    """抽原文 URL 时若跨条目合行，会抽出一个根本不存在的地址。"""

    element, raw, _ = _real_references()
    for url in source_urls(raw):
        assert not url.rstrip("/").endswith(".")
        assert "WWW" not in url


def test_real_bibliography_passes_verification() -> None:
    element, raw, full = _real_references()
    rendered = render_references(element["id"], raw, document_text=full)
    candidate = "\n".join(
        f"{entry['number']}. {entry['text']}" for entry in rendered.entries
    )
    assert (
        verify_reference_output(
            rendered, candidate, expected_urls=source_urls(raw)
        )
        == []
    )


def test_a_space_inserted_into_a_url_is_caught() -> None:
    element, raw, full = _real_references()
    rendered = render_references(element["id"], raw, document_text=full)
    candidate = "\n".join(
        f"{entry['number']}. {entry['text']}" for entry in rendered.entries
    )
    target = rendered.urls[-1]
    broken = candidate.replace(target, target[:20] + " " + target[20:])
    problems = verify_reference_output(
        rendered, broken, expected_urls=source_urls(raw)
    )
    assert any("插入了空格" in problem for problem in problems)


def test_a_hyphen_left_behind_is_caught() -> None:
    element, raw, full = _real_references()
    rendered = render_references(element["id"], raw, document_text=full)
    problems = verify_reference_output(
        rendered, "Deep neural net-works segment neuronal membranes"
    )
    assert any("仍带着连字符" in problem for problem in problems)


def test_a_missing_url_is_caught() -> None:
    element, raw, full = _real_references()
    rendered = render_references(element["id"], raw, document_text=full)
    problems = verify_reference_output(
        rendered, "参考文献都在这里", expected_urls=source_urls(raw)
    )
    assert any("丢失" in problem for problem in problems)


# --- 边界 -------------------------------------------------------------------


def test_empty_reference_list_is_rejected() -> None:
    with pytest.raises(ReferenceRenderError):
        render_references("r1", "   ", document_text="whatever")


def test_unnumbered_reference_list_is_rejected() -> None:
    with pytest.raises(ReferenceRenderError) as excinfo:
        render_references("r1", "一段没有编号的文字", document_text="text")
    assert "逐条核对" in str(excinfo.value)


def test_non_contiguous_numbering_is_warned() -> None:
    rendered = render_references(
        "r1", "1. 甲\n3. 丙", document_text="text"
    )
    assert any("编号不连续" in warning for warning in rendered.warnings)


def test_extract_urls_finds_both_schemes() -> None:
    found = extract_urls("go to http://a.test/x or www.b.test/y now")
    assert len(found) == 2


# --- 固化伪影的二遍修复 -----------------------------------------------------


def test_baked_hyphens_are_repaired_with_the_same_vocabulary() -> None:
    """上游把换行折掉后 net-works 已拼死，仍按同一套词表判定修复。"""

    from academic_pdf_translation.render.reference_renderer import (
        repair_baked_line_artifacts,
    )

    document = "convolutional networks unsupervised human-level results"
    vocabulary = build_vocabulary(document)
    forms = build_hyphenated_forms(document)
    assert (
        repair_baked_line_artifacts("neural net-works here", vocabulary, forms)
        == "neural networks here"
    )
    assert (
        repair_baked_line_artifacts("human-level scores", vocabulary, forms)
        == "human-level scores"
    )


def test_baked_url_gaps_are_closed_but_entry_numbers_are_not_eaten() -> None:
    from academic_pdf_translation.render.reference_renderer import (
        repair_baked_line_artifacts,
    )

    vocabulary = build_vocabulary("")
    forms = build_hyphenated_forms("")
    joined = repair_baked_line_artifacts(
        "http://a.test/x/ y_z/Welcome.html", vocabulary, forms
    )
    assert joined == "http://a.test/x/y_z/Welcome.html"
    kept = repair_baked_line_artifacts(
        "http://a.test/x/ 14. WWW: next entry", vocabulary, forms
    )
    assert "14. WWW" in kept
    assert "x/14" not in kept
