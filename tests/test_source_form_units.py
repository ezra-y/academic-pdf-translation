"""署名、DOI、题录按原文形态收；正文的占比门槛一字不动。

作者署名被目标语言占比门槛判为"没翻"，逼出来的是一句
"本文共有三位作者，中文译名分别为……"顶在标题位置上。门槛本来就不该
管人名和网址。这里把两件事同时钉住：这些单元免检，正文仍旧受检。

单独运行：
    python3 -m pytest -q tests/test_source_form_units.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from translation_truthfulness import (  # noqa: E402
    UNIT_TARGET_SCRIPT_RATIO_MIN,
    evaluate_translation,
    is_source_form_kind,
)

AUTHORS = "Laura A. King, Samantha J. Heintzelman, and Sarah J. Ward"
BODY = (
    "Automated document pipelines increasingly report their own completion "
    "status, and reviewers rarely verify the reported value."
)
#: 一段中文占比明显低于单元下限的"译文"。
THIN_TRANSLATION = "本文 discusses automated document pipeline completion status reporting."


def _translated_body(unit_id: str = "p0001-u0900") -> dict:
    """全篇保留原文有字符占比上限，补一段正常译文当分母。"""

    return _unit(
        unit_id,
        BODY,
        "自动化文档流水线越来越多地自行报告完成状态，而复审很少去核实"
        "这个报告出来的值。",
    )


def _document(units: list[dict]) -> dict:
    return {
        "source_language": "und-Latn",
        "target_language": "zh-Hans",
        "terminology": [],
        "units": units,
    }


def _unit(unit_id: str, source: str, translation: str, **extra) -> dict:
    unit = {
        "id": unit_id,
        "page": 1,
        "kind": "body",
        "source": source,
        "source_bbox": [50, 50, 500, 90],
        "translation": translation,
        "keep_source_code": None,
        "keep_source_reason": None,
    }
    unit.update(extra)
    return unit


def test_body_still_fails_the_ratio_floor() -> None:
    """正文门槛不动：中文占比不够，照旧判不合格。"""

    report = evaluate_translation(
        _document([_unit("p0001-u0001", BODY, THIN_TRANSLATION)])
    )
    codes = {problem["code"] for problem in report["problems"]}
    assert "TARGET_LANGUAGE_RATIO_LOW" in codes
    assert report["invalid_or_unverified_units"] == 1


def test_the_author_block_is_exempt_from_the_ratio_floor() -> None:
    """人名原样留着就是对的，不该被门槛判成没翻。"""

    report = evaluate_translation(
        _document(
            [
                _unit(
                    "p0001-u0011",
                    AUTHORS,
                    "劳拉·A·King, Samantha J. Heintzelman, Sarah J. Ward",
                    element_role="author",
                )
            ]
        )
    )
    assert report["problems"] == []


def test_the_author_block_may_keep_the_source_outright() -> None:
    """署名整段保留原文，用结构化 code 说明理由。"""

    report = evaluate_translation(
        _document(
            [
                _unit(
                    "p0001-u0011",
                    AUTHORS,
                    None,
                    element_role="author",
                    keep_source_code="publication-front-matter",
                ),
                _translated_body(),
            ]
        )
    )
    assert report["problems"] == []
    assert report["validated_kept_source_units"] == 1


def test_a_reference_entry_may_keep_the_source_by_its_role() -> None:
    """题录按学术惯例保留原文；角色由元素清单算出，不靠自由文本理由。"""

    entry = (
        "King, L. A., & Hicks, J. A. (2021). The science of meaning in life. "
        "Annual Review of Psychology, 72, 561-584."
    )
    report = evaluate_translation(
        _document(
            [
                _unit(
                    "p0006-u0003",
                    entry,
                    None,
                    element_role="reference-entry",
                    keep_source_code="bibliography-entry",
                ),
                _translated_body(),
            ]
        )
    )
    assert report["problems"] == []


def test_front_matter_code_does_not_work_on_body() -> None:
    """免检只按单元角色给，不是谁写上这个 code 谁就免检。"""

    report = evaluate_translation(
        _document(
            [
                _unit(
                    "p0001-u0002",
                    BODY,
                    None,
                    keep_source_code="publication-front-matter",
                )
            ]
        )
    )
    assert report["invalid_or_unverified_units"] == 1


def test_source_form_units_do_not_drag_down_the_document_ratio() -> None:
    """署名和题录的原文字符不该把全篇占比拖到门槛以下。"""

    units = [
        _unit(
            "p0001-u0011",
            AUTHORS,
            AUTHORS.replace("Laura", "劳拉"),
            element_role="author",
        ),
        _unit(
            "p0001-u0002",
            BODY,
            "自动化文档流水线越来越多地自行报告完成状态，而复审很少去核实"
            "这个报告出来的值。",
        ),
    ]
    report = evaluate_translation(_document(units))
    assert report["problems"] == []
    assert report["document_target_script_ratio"] >= UNIT_TARGET_SCRIPT_RATIO_MIN


def test_roles_and_kinds_both_count() -> None:
    assert is_source_form_kind({"element_role": "affiliation"})
    assert is_source_form_kind({"kind": "reference-entry"})
    assert not is_source_form_kind({"kind": "body", "element_role": "body"})
    assert not is_source_form_kind({"kind": "heading"})
