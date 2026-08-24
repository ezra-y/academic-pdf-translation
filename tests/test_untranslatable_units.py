"""没有字母的单元：照抄就是正确译法。

坐标轴刻度 "0 5 10 15 20"、页码 "561"、纯符号片段在任何语言里都是同一串
字符。要求它们必须和原文不同，只会逼出把刻度改写成中文数字这种做法，
或者让整篇卡在一个画不出译文的单元上。

单独运行：
    python3 -m pytest -q tests/test_untranslatable_units.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from translation_truthfulness import (  # noqa: E402
    evaluate_translation,
    has_translatable_letters,
)


def _document(source: str, translation: str) -> dict[str, Any]:
    return {
        "source_language": "und-Latn",
        "target_language": "zh-Hans",
        "terminology": [],
        "units": [
            {
                "id": "p0006-u0004",
                "page": 6,
                "kind": "body",
                "source": source,
                "source_bbox": [50, 50, 200, 60],
                "translation": translation,
                "keep_source_code": None,
                "keep_source_reason": None,
            }
        ],
    }


def test_letters_detection() -> None:
    assert not has_translatable_letters("0 5 10 15 20 25 30 35 40 45 50")
    assert not has_translatable_letters("561")
    assert not has_translatable_letters("(%) — 12.5")
    assert has_translatable_letters("Meaning in life")
    assert has_translatable_letters("参考文献")


def test_axis_ticks_may_stay_identical() -> None:
    """直方图刻度照抄不算原文冒充译文。"""

    report = evaluate_translation(
        _document(
            "0 5 10 15 20 25 30 35 40 45 50",
            "0 5 10 15 20 25 30 35 40 45 50",
        )
    )
    assert report["invalid_or_unverified_units"] == 0
    assert report["units"][0]["state"] == "translated"


def test_page_number_may_stay_identical() -> None:
    report = evaluate_translation(_document("561", "561"))
    assert report["invalid_or_unverified_units"] == 0


def test_prose_still_cannot_be_copied() -> None:
    """正文照抄原文的门槛一字未动。"""

    body = (
        "The human experience of meaning in life is widely viewed as a "
        "cornerstone of well-being across the reported cohorts."
    )
    report = evaluate_translation(_document(body, body))
    assert report["invalid_or_unverified_units"] == 1
    codes = {
        problem["code"]
        for verdict in report["units"]
        for problem in verdict["problems"]
    }
    assert "TRANSLATION_EQUALS_SOURCE" in codes


def test_short_latin_phrase_still_cannot_be_copied() -> None:
    """带字母的短语照抄仍然被拒，哪怕它短到不算散文。"""

    report = evaluate_translation(_document("Meaning in life", "Meaning in life"))
    assert report["invalid_or_unverified_units"] == 1
