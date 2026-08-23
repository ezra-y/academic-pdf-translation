"""脚注位置：正文读完再读注。

通讯作者地址按坐标插在两段正文之间，读者的句子就被劈成两半。

单独运行：
    python3 -m pytest -q tests/test_footnote_placement.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from academic_pdf_translation.render.story import _footnotes_last  # noqa: E402


def _unit(unit_id: str, role: str) -> dict[str, Any]:
    return {"id": unit_id, "_element_role": role}


def test_footnotes_move_after_the_page_body() -> None:
    """脚注排到本页正文之后，并在第一条前留下分隔线标记。"""

    units = [
        _unit("body-1", "body"),
        _unit("note-1", "footnote"),
        _unit("body-2", "body"),
        _unit("note-2", "footnote"),
    ]
    ordered = _footnotes_last(units)
    assert [unit["id"] for unit in ordered] == [
        "body-1",
        "body-2",
        "note-1",
        "note-2",
    ]
    assert ordered[2]["_footnote_zone_start"] is True
    assert "_footnote_zone_start" not in ordered[3]


def test_pages_made_only_of_footnotes_are_untouched() -> None:
    units = [_unit("note-1", "footnote"), _unit("note-2", "footnote")]
    assert _footnotes_last(units) == units
