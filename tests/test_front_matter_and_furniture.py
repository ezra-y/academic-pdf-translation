"""首页顺序与页眉页脚。

两件事在真实期刊排版里同时发生，读者只看到一个结果：题名不在最上面。

- 首页顶端的生产代码条被当成题名或编号章节标题；
- 左右页轮换的页眉（一半印刊名、一半印作者）文字指纹各自不够重复，
  没被认成页眉，混进正文。

单独运行：
    python3 -m pytest -q tests/test_front_matter_and_furniture.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.pdfgen.canvas import Canvas  # noqa: E402

from academic_pdf_translation.analysis.detectors.text_roles import (  # noqa: E402
    is_publication_stamp,
)
from academic_pdf_translation.analysis.source_elements import (  # noqa: E402
    build_inventory,
)
from academic_pdf_translation.contracts.enums import ElementType  # noqa: E402
from academic_pdf_translation.render.story import (  # noqa: E402
    _front_matter_ordered,
)
from extract_source_structure import extract_source_structure  # noqa: E402

PRODUCTION_STRIP = (
    "656354 CDPXXX10.1177/0963721416656354King et al."
    "Science of Meaning in Life research-article2016"
)
TITLE = "Beyond the Search for Meaning: A Contemporary Science of Experience"


def _paper(path: Path) -> Path:
    """六页论文：首页有生产代码条，其余页左右轮换页眉。"""

    width, height = A4
    pdf = Canvas(str(path), pagesize=A4)

    pdf.setFont("Helvetica", 5)
    pdf.drawString(8, height - 8, PRODUCTION_STRIP)
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(48, height - 110, TITLE)
    pdf.setFont("Helvetica", 10)
    pdf.drawString(48, height - 150, "Ada Lovelace, Grace Hopper, and Alan Kay")
    pdf.drawString(48, height - 168, "Department of Psychology, Example State")
    y = height - 220
    for index in range(20):
        pdf.drawString(
            48,
            y,
            f"Body sentence {index} discusses the reported effect in detail.",
        )
        y -= 14
    pdf.showPage()

    for page_number in range(2, 7):
        pdf.setFont("Helvetica", 9)
        running = (
            f"{page_number}    Lovelace et al."
            if page_number % 2 == 0
            else f"Science of Experience    {page_number}"
        )
        pdf.drawString(48, height - 40, running)
        pdf.setFont("Helvetica", 10)
        y = height - 90
        for index in range(30):
            pdf.drawString(
                48,
                y,
                f"Page {page_number} sentence {index} reports a measured value.",
            )
            y -= 14
        pdf.showPage()
    pdf.save()
    return path


def _unit(unit_id: str, role: str) -> dict[str, Any]:
    return {"id": unit_id, "_element_role": role}


def test_bare_doi_production_strip_is_publication_metadata() -> None:
    """没有 doi: 前缀的生产代码条也是出版元数据。"""

    assert is_publication_stamp(PRODUCTION_STRIP)
    assert not is_publication_stamp(TITLE)
    assert not is_publication_stamp("3.1 Data Augmentation")
    assert not is_publication_stamp("We report a ratio of 10.5/2 per cohort.")


def test_alternating_running_heads_are_page_furniture(tmp_path: Path) -> None:
    """左右轮换的页眉，两半都要认出来。"""

    structure = extract_source_structure(_paper(tmp_path / "paper.pdf"))
    furniture = {
        page["page"]: [
            block["text"]
            for block in page["blocks"]
            if block["page_furniture"]
        ]
        for page in structure["pages"]
    }
    for page_number in range(2, 7):
        assert furniture[page_number], f"第 {page_number} 页页眉未被认出"
    assert not furniture[1]


def test_title_is_not_displaced_by_the_production_strip(
    tmp_path: Path,
) -> None:
    """题名的位置不被首页顶端的代码条顶掉。"""

    structure = extract_source_structure(_paper(tmp_path / "paper.pdf"))
    inventory = build_inventory(structure, pymupdf_version="0")
    first_page = [
        element for element in inventory.elements if element.page == 1
    ]
    titles = [
        element
        for element in first_page
        if element.type is ElementType.DOCUMENT_TITLE
    ]
    assert len(titles) == 1
    assert TITLE.split(":")[0] in titles[0].text
    strips = [
        element
        for element in first_page
        if element.type is ElementType.PUBLICATION_METADATA
    ]
    assert any("CDPXXX" in element.text for element in strips)


def test_front_matter_order_puts_the_title_first() -> None:
    """题名之前的出版元数据挪到题名之后，正文一条不动。"""

    units = [
        _unit("strip", "publication-metadata"),
        _unit("title", "document-title"),
        _unit("authors", "author"),
        _unit("affil", "affiliation"),
        _unit("body-1", "body"),
        _unit("body-2", "body"),
    ]
    ordered = [unit["id"] for unit in _front_matter_ordered(units)]
    assert ordered[0] == "title"
    assert ordered.index("strip") < ordered.index("body-1")
    assert ordered[-2:] == ["body-1", "body-2"]


def test_front_matter_order_leaves_other_pages_alone() -> None:
    """没有题名的页面原样返回。"""

    units = [_unit("a", "body"), _unit("b", "publication-metadata")]
    assert _front_matter_ordered(units) == units
