"""生成复现用的英文合成论文 PDF。

只用于复现 P0 问题，不代表任何真实论文。包含标题、摘要、正文、
数字统计量、章节标题和参考文献段落。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

TITLE = "Measuring Reproducibility in Automated Document Pipelines"

ABSTRACT = (
    "This study evaluates whether automated translation pipelines preserve "
    "the meaning of academic prose. We analysed 1,248 documents drawn from "
    "12 venues and observed a mean coverage of 0.83 (SD = 0.07). The effect "
    "was significant, t(41) = 3.62, p = 0.004, and remained stable after "
    "controlling for document length. We conclude that coverage counters "
    "alone cannot certify that a document has been translated."
)

SECTIONS = [
    (
        "1 Introduction",
        [
            "Automated document pipelines increasingly report their own "
            "completion status. Prior work [1] shows that self reported "
            "status fields diverge from observable output in 37% of runs. "
            "We therefore ask whether a pipeline can be trusted to certify "
            "its own translation coverage.",
            "We contribute three findings. First, a coverage counter that "
            "counts non empty fields accepts source text copied verbatim "
            "into the translation field. Second, a free text exemption "
            "reason can convert an ordinary body page into a page that "
            "skips every downstream check. Third, font preparation ordered "
            "after the readiness audit blocks fresh jobs entirely.",
        ],
    ),
    (
        "2 Method",
        [
            "We sampled 1,248 documents between 2019 and 2024. Each "
            "document was processed twice, once with the default settings "
            "and once with an adversarial payload. Reviewers recorded the "
            "final decision and the reported coverage value for both runs.",
            "Statistical tests used a two sided alpha of 0.05. Effect "
            "sizes are reported as Cohen d with 95% confidence intervals. "
            "All analysis code is available at "
            "https://example.org/repro and archived under "
            "doi:10.5555/example.2024.001.",
        ],
    ),
    (
        "3 Results",
        [
            "The adversarial payload was accepted in every run. Reported "
            "coverage reached 1.00 while the observable target language "
            "content remained at 0.00. The difference was large, d = 2.41, "
            "95% CI [1.88, 2.94].",
            "Runs that supplied a free text exemption reason were marked "
            "as reference pages on 100% of pages, and the final decision "
            "was READY in 42 of 42 attempts.",
        ],
    ),
    (
        "4 Discussion",
        [
            "These results indicate that completion fields must be derived "
            "from verifiable evidence rather than asserted. A pipeline that "
            "counts filled fields measures effort, not output. We recommend "
            "structured exemption codes bound to unit type and to recorded "
            "coordinates.",
        ],
    ),
]

REFERENCES = [
    "[1] Almeida, R., & Novak, P. (2021). Self reported status in document "
    "automation. Journal of Reproducible Systems, 14(2), 118-140.",
    "[2] Berger, T. (2019). Coverage metrics considered harmful. "
    "Proceedings of the Conference on Document Engineering, 55-64.",
    "[3] Chen, L., Okafor, N., & Silva, M. (2023). Structured exemptions "
    "for machine translation review. Computational Linguistics, 49(1), "
    "77-102.",
]


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


def build(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    width, height = A4
    pdf = canvas.Canvas(str(output), pagesize=A4)
    left = 22 * mm
    line_step = 5.0 * mm
    y = height - 28 * mm

    pdf.setFont("Helvetica-Bold", 16)
    for line in _wrap(TITLE, 46):
        pdf.drawString(left, y, line)
        y -= 8 * mm
    y -= 2 * mm

    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(left, y, "Abstract")
    y -= 6 * mm
    pdf.setFont("Helvetica", 10)
    for line in _wrap(ABSTRACT, 88):
        pdf.drawString(left, y, line)
        y -= line_step
    y -= 4 * mm

    for heading, paragraphs in SECTIONS:
        if y < 45 * mm:
            pdf.showPage()
            y = height - 28 * mm
        pdf.setFont("Helvetica-Bold", 12)
        pdf.drawString(left, y, heading)
        y -= 7 * mm
        pdf.setFont("Helvetica", 10)
        for paragraph in paragraphs:
            for line in _wrap(paragraph, 88):
                if y < 25 * mm:
                    pdf.showPage()
                    y = height - 28 * mm
                    pdf.setFont("Helvetica", 10)
                pdf.drawString(left, y, line)
                y -= line_step
            y -= 3 * mm

    if y < 60 * mm:
        pdf.showPage()
        y = height - 28 * mm
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(left, y, "References")
    y -= 7 * mm
    pdf.setFont("Helvetica", 9)
    for entry in REFERENCES:
        for line in _wrap(entry, 96):
            if y < 20 * mm:
                pdf.showPage()
                y = height - 28 * mm
                pdf.setFont("Helvetica", 9)
            pdf.drawString(left, y, line)
            y -= 4.4 * mm
        y -= 2 * mm

    pdf.showPage()
    pdf.save()
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(build(args.output.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
