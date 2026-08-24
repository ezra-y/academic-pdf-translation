"""阅读顺序：两种顺序打架时按几何那份切单元。

期刊首页的标题、作者、单位横跨栏顶，PDF 自带的块顺序常把双栏正文排在
它们前面。照抄自带顺序，译本第 1 页就是正文在上、标题在下。

单独运行：
    python3 -m pytest -q tests/test_reading_order.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from prepare_translation_units import build_source_units  # noqa: E402


def _page(layout: dict, blocks: list[dict]) -> dict:
    return {
        "page": 1,
        "signals": {},
        "layout": layout,
        "blocks": blocks,
    }


def _block(block_id: int, text: str) -> dict:
    return {
        "id": block_id,
        "text": text,
        "page_furniture": False,
        "segments": [
            {
                "index": 0,
                "role": "body",
                "heading_level": None,
                "text": text,
                "bbox": [40, 40, 300, 80],
            }
        ],
    }


def _sources(structure: dict) -> list[str]:
    return [unit["source"] for unit in structure["units"]]


def test_the_geometric_order_wins_when_the_two_disagree() -> None:
    """标题排在正文前面，不按 PDF 自带的块顺序。"""

    structure = {
        "source_sha256": "x",
        "schema_version": "1.0",
        "pages": [
            _page(
                {
                    "two_column": True,
                    "native_order": [0, 1],
                    "layout_order": [1, 0],
                    "order_disagreement_ratio": 1.0,
                    "selected_order": "visual-confirmation-required",
                    "reading_order": [1, 0],
                },
                [_block(0, "正文第一段。"), _block(1, "论文标题")],
            )
        ],
    }
    assert _sources(build_source_units(structure)) == ["论文标题", "正文第一段。"]


def test_an_undisputed_page_keeps_the_native_order() -> None:
    structure = {
        "source_sha256": "x",
        "schema_version": "1.0",
        "pages": [
            _page(
                {
                    "two_column": True,
                    "native_order": [0, 1],
                    "layout_order": [0, 1],
                    "order_disagreement_ratio": 0.0,
                    "selected_order": "native",
                    "reading_order": [0, 1],
                },
                [_block(0, "第一段。"), _block(1, "第二段。")],
            )
        ],
    }
    assert _sources(build_source_units(structure)) == ["第一段。", "第二段。"]


def test_an_old_job_without_the_field_falls_back_to_the_native_order() -> None:
    """老作业的结构文件没有 reading_order，行为保持不变。"""

    structure = {
        "source_sha256": "x",
        "schema_version": "1.0",
        "pages": [
            _page(
                {
                    "two_column": False,
                    "native_order": [0, 1],
                    "layout_order": [1, 0],
                    "order_disagreement_ratio": 1.0,
                    "selected_order": "layout",
                },
                [_block(0, "第一段。"), _block(1, "第二段。")],
            )
        ],
    }
    assert _sources(build_source_units(structure)) == ["第一段。", "第二段。"]
