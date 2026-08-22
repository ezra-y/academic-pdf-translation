"""生成代表性合成 PDF，用于性能基线。

这些 PDF 不是真实论文，只复现五类版式特征：单栏正文、双栏正文、
表格与模型图、图片密集、参考文献密集。它们只用于测量重复读取和耗时，
不用于质量验收。真实论文的视觉抽查仍然单独执行。
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


WORDS = (
    "analysis model sample evidence variance cohort baseline estimate "
    "significant interval regression construct validity reliability "
    "participants procedure measurement outcome treatment control"
).split()


def _paragraph(rng: random.Random, words: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(words))


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _header_footer(pdf: canvas.Canvas, page_number: int, title: str) -> None:
    width, height = A4
    pdf.setFont("Helvetica", 7)
    pdf.drawString(20 * mm, height - 12 * mm, title)
    pdf.drawCentredString(width / 2, 10 * mm, str(page_number))


def _single_column(pdf: canvas.Canvas, rng: random.Random, pages: int) -> None:
    width, height = A4
    for page_number in range(1, pages + 1):
        _header_footer(pdf, page_number, "Synthetic Single Column Journal")
        y = height - 25 * mm
        if page_number == 1:
            pdf.setFont("Helvetica-Bold", 15)
            pdf.drawString(20 * mm, y, "A Synthetic Single Column Paper")
            y -= 12 * mm
        for _ in range(4):
            pdf.setFont("Helvetica-Bold", 11)
            pdf.drawString(20 * mm, y, f"Section {rng.randint(1, 9)}")
            y -= 7 * mm
            pdf.setFont("Helvetica", 9.5)
            for line in _wrap(_paragraph(rng, 130), 95):
                if y < 25 * mm:
                    break
                pdf.drawString(20 * mm, y, line)
                y -= 4.6 * mm
            y -= 3 * mm
        pdf.showPage()


def _two_column(pdf: canvas.Canvas, rng: random.Random, pages: int) -> None:
    width, height = A4
    column_width = 44
    for page_number in range(1, pages + 1):
        _header_footer(pdf, page_number, "Synthetic Two Column Proceedings")
        for column, left in enumerate((20 * mm, 108 * mm)):
            y = height - 25 * mm
            if page_number == 1 and column == 0:
                pdf.setFont("Helvetica-Bold", 13)
                pdf.drawString(left, y, "Two Column Study")
                y -= 9 * mm
            for _ in range(3):
                pdf.setFont("Helvetica-Bold", 10)
                pdf.drawString(left, y, f"{rng.randint(1, 6)}. Method")
                y -= 6 * mm
                pdf.setFont("Helvetica", 9)
                for line in _wrap(_paragraph(rng, 110), column_width):
                    if y < 25 * mm:
                        break
                    pdf.drawString(left, y, line)
                    y -= 4.2 * mm
                y -= 3 * mm
        pdf.showPage()


def _table_and_diagram(
    pdf: canvas.Canvas,
    rng: random.Random,
    pages: int,
) -> None:
    width, height = A4
    for page_number in range(1, pages + 1):
        _header_footer(pdf, page_number, "Synthetic Tables And Models")
        y = height - 25 * mm
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(20 * mm, y, f"Table {page_number}. Descriptive results")
        y -= 6 * mm
        rows, columns = 12, 6
        cell_w = (width - 40 * mm) / columns
        cell_h = 6 * mm
        pdf.setFont("Helvetica", 8)
        for row in range(rows):
            for column in range(columns):
                x = 20 * mm + column * cell_w
                top = y - row * cell_h
                pdf.rect(x, top - cell_h, cell_w, cell_h)
                pdf.drawString(
                    x + 1.5 * mm,
                    top - cell_h + 2 * mm,
                    f"{rng.randint(1, 99)}.{rng.randint(0, 9)}",
                )
        y -= rows * cell_h + 10 * mm
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(20 * mm, y, f"Figure {page_number}. Structural model")
        y -= 8 * mm
        for node in range(6):
            x = 25 * mm + (node % 3) * 55 * mm
            top = y - (node // 3) * 26 * mm
            pdf.roundRect(x, top - 14 * mm, 45 * mm, 14 * mm, 3, stroke=1)
            pdf.setFont("Helvetica", 8)
            pdf.drawString(x + 3 * mm, top - 9 * mm, f"Construct {node + 1}")
            if node % 3 < 2:
                pdf.line(
                    x + 45 * mm,
                    top - 7 * mm,
                    x + 55 * mm,
                    top - 7 * mm,
                )
        pdf.showPage()


def _image_heavy(pdf: canvas.Canvas, rng: random.Random, pages: int) -> None:
    width, height = A4
    for page_number in range(1, pages + 1):
        _header_footer(pdf, page_number, "Synthetic Figure Atlas")
        y = height - 25 * mm
        for figure in range(3):
            pdf.setFillGray(0.85)
            pdf.rect(20 * mm, y - 48 * mm, width - 40 * mm, 46 * mm, fill=1)
            pdf.setFillGray(0.35)
            for point in range(40):
                px = 25 * mm + rng.random() * (width - 50 * mm)
                py = y - 45 * mm + rng.random() * 40 * mm
                pdf.circle(px, py, 1.2, fill=1)
            pdf.setFillGray(0)
            pdf.setFont("Helvetica", 8.5)
            pdf.drawString(
                20 * mm,
                y - 53 * mm,
                f"Figure {page_number}.{figure + 1}. "
                + _paragraph(rng, 12),
            )
            y -= 62 * mm
            if y < 40 * mm:
                break
        pdf.showPage()


def _reference_heavy(
    pdf: canvas.Canvas,
    rng: random.Random,
    pages: int,
) -> None:
    width, height = A4
    for page_number in range(1, pages + 1):
        _header_footer(pdf, page_number, "Synthetic Reference List")
        y = height - 25 * mm
        if page_number == 1:
            pdf.setFont("Helvetica-Bold", 12)
            pdf.drawString(20 * mm, y, "References")
            y -= 9 * mm
        for column, left in enumerate((20 * mm, 108 * mm)):
            y_column = y
            while y_column > 25 * mm:
                entry = (
                    f"Author{rng.randint(1, 400)}, A. "
                    f"({rng.randint(1980, 2024)}). "
                    + _paragraph(rng, 14)
                    + f". Journal, {rng.randint(1, 60)}, "
                    f"{rng.randint(1, 300)}-{rng.randint(301, 600)}."
                )
                pdf.setFont("Helvetica", 8)
                for index, line in enumerate(_wrap(entry, 44)):
                    if y_column < 25 * mm:
                        break
                    pdf.drawString(
                        left + (0 if index == 0 else 4 * mm),
                        y_column,
                        line,
                    )
                    y_column -= 3.8 * mm
                y_column -= 1.5 * mm
            del column
        pdf.showPage()


BUILDERS = {
    "single-column-body": (_single_column, 12),
    "two-column-body": (_two_column, 12),
    "structured-table-and-model": (_table_and_diagram, 8),
    "image-heavy": (_image_heavy, 8),
    "reference-heavy": (_reference_heavy, 6),
}


def build_corpus(output_dir: Path, seed: int = 20260821) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for name, (builder, pages) in BUILDERS.items():
        target = output_dir / f"{name}.pdf"
        pdf = canvas.Canvas(str(target), pagesize=A4)
        builder(pdf, random.Random(seed + len(name)), pages)
        pdf.save()
        created.append(target)
    return created


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "papers",
    )
    args = parser.parse_args()
    for path in build_corpus(args.output_dir.resolve()):
        print(f"{path.name}: {path.stat().st_size / 1024:.1f} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
