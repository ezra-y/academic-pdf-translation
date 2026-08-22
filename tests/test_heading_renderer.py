"""标题与正文渲染器：角色只来自 source_elements.json。

单独运行：
    python3 -m pytest -q tests/test_heading_renderer.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from academic_pdf_translation.render.heading_renderer import (  # noqa: E402
    HEADING_ROLES,
    ROLE_FONT_SCALE,
    HeadingRenderError,
    ResolvedRoles,
    detect_tail_duplication,
    font_size_for,
    heading_level,
    longest_repeated_tail,
    resolve_roles,
    verify_text_roles,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"

BAD_TITLE = "U-Net：用于生物医学图像分割的卷积网络图像分割"
GOOD_TITLE = "U-Net：用于生物医学图像分割的卷积网络"
TITLE_SOURCE = "U-Net: Convolutional Networks for Biomedical Image Segmentation"


def _real_job():
    needed = [
        REAL_JOB / "unit_bindings.json",
        REAL_JOB / "source_elements.json",
        REAL_JOB / "translation.json",
    ]
    if not all(path.is_file() for path in needed):
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    bindings = json.loads(needed[0].read_text(encoding="utf-8"))["bindings"]
    elements = {
        element["id"]: element
        for element in json.loads(needed[1].read_text(encoding="utf-8"))[
            "elements"
        ]
    }
    units = json.loads(needed[2].read_text(encoding="utf-8"))["units"]
    translations = {
        unit["id"]: (unit.get("translation") or "") for unit in units
    }
    sources = {unit["id"]: (unit.get("source") or "") for unit in units}
    return bindings, elements, translations, sources


# --- 字号只由角色决定 -------------------------------------------------------


def test_only_titles_and_headings_get_heading_sizes() -> None:
    body = 10.0
    heading = font_size_for("heading", body)
    for role in ("affiliation", "publication-metadata", "figure-label", "body"):
        assert font_size_for(role, body) < heading


def test_deeper_headings_shrink_but_stay_above_body() -> None:
    body = 10.0
    first = font_size_for("heading", body, level=1)
    second = font_size_for("heading", body, level=2)
    assert body <= second < first


def test_zero_body_font_is_rejected() -> None:
    with pytest.raises(HeadingRenderError):
        font_size_for("body", 0.0)


def test_heading_level_only_comes_from_the_element() -> None:
    assert heading_level(None) == 1
    assert heading_level({"detail": {"heading_level": 2}}) == 2
    assert heading_level({"detail": {"level": "x"}}) == 1
    assert heading_level({"detail": {"level": 99}}) == 4


# --- 角色解析 ---------------------------------------------------------------


def test_unbound_units_default_to_body_never_heading() -> None:
    """宁可漏掉一个标题，也不要凭空造出一个。"""

    resolved = resolve_roles(
        [{"unit_id": "u1", "element_id": "", "element_role": ""}],
        body_font_size=10.0,
    )
    assert resolved.roles[0]["is_heading"] is False
    assert resolved.roles[0]["source"] == "unbound-defaults-to-body"
    assert any("不得当标题" in warning for warning in resolved.warnings)


def test_a_binding_without_a_unit_id_is_rejected() -> None:
    with pytest.raises(HeadingRenderError):
        resolve_roles([{"element_id": "e1", "element_role": "heading"}])


def test_role_is_recorded_as_coming_from_the_inventory() -> None:
    resolved = resolve_roles(
        [
            {
                "unit_id": "u1",
                "element_id": "p0001-heading-001",
                "element_role": "heading",
                "element_type": "heading",
            }
        ]
    )
    assert resolved.roles[0]["source"] == "source_elements.json"
    assert resolved.roles[0]["is_heading"] is True


# --- 尾部重复 ---------------------------------------------------------------


def test_the_real_bad_title_is_caught() -> None:
    """R-008：「……的卷积网络图像分割」句末多出一个重复的「图像分割」。"""

    assert detect_tail_duplication(TITLE_SOURCE, BAD_TITLE) == "图像分割"


def test_the_corrected_title_passes() -> None:
    assert detect_tail_duplication(TITLE_SOURCE, GOOD_TITLE) is None


def test_repetition_present_in_the_source_is_not_a_defect() -> None:
    """原文里 (b) 和 (d) 都以同一句收尾，照搬不算错。"""

    source = (
        "(b) Segmentation result with manual ground truth. "
        "(d) Segmentation result with manual ground truth."
    )
    translation = (
        "(b) 分割结果与人工真值。(d) 分割结果与人工真值。"
    )
    assert detect_tail_duplication(source, translation) is None


def test_tokens_are_language_aware() -> None:
    """中文按字、拉丁按词，否则拿中文子串去英文里数永远是零。"""

    assert longest_repeated_tail(
        "deep image segmentation then deep image segmentation"
    ) == ["deep", "image", "segmentation"]
    assert longest_repeated_tail("图像分割的卷积网络图像分割")
    assert longest_repeated_tail("一句没有重复的话") == []


# --- 真实论文 ---------------------------------------------------------------


def test_real_paper_promotes_exactly_the_real_headings() -> None:
    """R-007：作者单位、arXiv 版本戳、图内标签都不许被提成标题。"""

    bindings, elements, _, _ = _real_job()
    resolved = resolve_roles(
        bindings, body_font_size=10.0, elements_by_id=elements
    )
    headings = [item for item in resolved.roles if item["is_heading"]]
    assert len(resolved.roles) == len(bindings)
    assert len(headings) == 9, [item["role"] for item in headings]
    assert {item["role"] for item in headings} == set(HEADING_ROLES)

    promoted_roles = {item["role"] for item in headings}
    for role in (
        "affiliation",
        "publication-metadata",
        "figure-label",
        "figure-caption",
        "body",
        "page-furniture",
    ):
        assert role not in promoted_roles


def test_real_paper_headings_are_the_section_titles() -> None:
    bindings, elements, translations, _ = _real_job()
    resolved = resolve_roles(
        bindings, body_font_size=10.0, elements_by_id=elements
    )
    texts = [
        translations.get(item["unit_id"], "")
        for item in resolved.roles
        if item["is_heading"]
    ]
    assert any("引言" in text for text in texts)
    assert any("参考文献" in text for text in texts)
    assert all(len(text) < 40 for text in texts if text), texts


def test_real_paper_passes_role_verification() -> None:
    bindings, elements, translations, sources = _real_job()
    resolved = resolve_roles(
        bindings, body_font_size=10.0, elements_by_id=elements
    )
    assert (
        verify_text_roles(
            resolved,
            body_font_size=10.0,
            translations=translations,
            sources=sources,
        )
        == []
    )


def test_the_bad_title_would_fail_verification_on_the_real_job() -> None:
    bindings, elements, translations, sources = _real_job()
    resolved = resolve_roles(
        bindings, body_font_size=10.0, elements_by_id=elements
    )
    title = next(
        item for item in resolved.roles if item["role"] == "document-title"
    )
    broken = dict(translations)
    broken[title["unit_id"]] = BAD_TITLE
    problems = verify_text_roles(
        resolved, body_font_size=10.0, translations=broken, sources=sources
    )
    assert any("译文尾部重复" in problem for problem in problems)


def test_long_body_paragraphs_are_not_tail_checked() -> None:
    """中文常把宾语挪到句尾，长段落做尾部检查会大量误报。"""

    bindings, elements, translations, sources = _real_job()
    resolved = resolve_roles(
        bindings, body_font_size=10.0, elements_by_id=elements
    )
    body_units = [
        item["unit_id"] for item in resolved.roles if item["role"] == "body"
    ]
    flagged = [
        unit_id
        for unit_id in body_units
        if detect_tail_duplication(
            sources.get(unit_id, ""), translations.get(unit_id, "")
        )
    ]
    assert flagged, "样本里确实有语序造成的尾部重复，正是要绕开的情况"
    assert verify_text_roles(
        resolved,
        body_font_size=10.0,
        translations=translations,
        sources=sources,
    ) == []


# --- 核对 -------------------------------------------------------------------


def test_a_non_heading_at_heading_size_is_caught() -> None:
    resolved = ResolvedRoles(
        roles=[
            {
                "unit_id": "u1",
                "element_id": "e1",
                "element_type": "affiliation",
                "role": "affiliation",
                "font_size": 10.0 * ROLE_FONT_SCALE["heading"],
                "is_heading": False,
                "level": 0,
                "source": "source_elements.json",
            }
        ]
    )
    problems = verify_text_roles(resolved, body_font_size=10.0)
    assert any("用了标题字号" in problem for problem in problems)


def test_a_heading_without_a_binding_is_caught() -> None:
    resolved = ResolvedRoles(
        roles=[
            {
                "unit_id": "u1",
                "element_id": "",
                "element_type": "",
                "role": "heading",
                "font_size": 12.5,
                "is_heading": True,
                "level": 1,
                "source": "unbound-defaults-to-body",
            }
        ]
    )
    problems = verify_text_roles(resolved, body_font_size=10.0)
    assert any("没有绑定元素却当了标题" in problem for problem in problems)


def test_a_figure_label_in_the_heading_flow_is_caught() -> None:
    """图内标签「复制并裁剪」被排成标题，就是 R-007 的原样。"""

    resolved = ResolvedRoles(
        roles=[
            {
                "unit_id": "u1",
                "element_id": "e1",
                "element_type": "vector-figure",
                "role": "figure-label",
                "font_size": 12.5,
                "is_heading": True,
                "level": 1,
                "source": "source_elements.json",
            }
        ]
    )
    problems = verify_text_roles(resolved, body_font_size=10.0)
    assert any("不是标题，却被标成了标题" in problem for problem in problems)
    assert any("不得进标题行流" in problem for problem in problems)
