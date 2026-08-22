"""题录体字重闸门：参考文献不许整页粗体或斜体。

单独运行：
    python3 -m pytest -q tests/test_reference_font_weight.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fitz  # noqa: E402
from academic_pdf_translation.contracts.fonts import (  # noqa: E402
    REFERENCE_ALLOWED_WEIGHTS,
    reference_weight_ok,
)


def test_arial_black_is_not_selected_for_reference_body() -> None:
    """"Arial Black" 名字里带 Arial，按名字匹配曾把它当普通 Arial 用。"""

    assert not reference_weight_ok(Path("Arial Black.ttf"))
    assert not reference_weight_ok(Path("Arial Bold Italic.ttf"))
    assert not reference_weight_ok(Path("SomeFont-Heavy.otf"))
    assert not reference_weight_ok(Path("Family-SemiBold.ttf"))


def test_regular_reference_font_outranks_bold() -> None:
    assert reference_weight_ok(Path("Arial.ttf"))
    assert reference_weight_ok(Path("Times New Roman.ttf"))
    assert reference_weight_ok(Path("NotoSans-Regular.ttf"))
    assert reference_weight_ok(Path("SourceSerif-Book.otf"))
    # medium 是允许清单里的中等字重
    assert "medium" in REFERENCE_ALLOWED_WEIGHTS
    assert reference_weight_ok(Path("Font-Medium.ttf"))


def test_italic_is_rejected_for_reference_body() -> None:
    """整页参考文献也不该是斜体——Arial Italic 曾被选中。"""

    assert not reference_weight_ok(Path("Arial Italic.ttf"))
    assert not reference_weight_ok(Path("Helvetica-Oblique.ttf"))


def test_frozen_bold_reference_font_is_invalidated() -> None:
    """旧作业冻结了粗体题录时，冻结作废并强制重选。"""

    source = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "font_preparation.py"
    ).read_text(encoding="utf-8")
    assert "reference_weight_ok" in source
    assert "stale_reference" in source


def test_reference_bold_is_only_used_for_marked_spans() -> None:
    """QA 有整页过粗检查：某页字符六成以上来自粗体家族字体即硬失败。

    局部粗体（卷号等标记片段）不会触发六成阈值，仍然允许。
    """

    qa_source = (
        Path(__file__).resolve().parent.parent / "scripts" / "qa_pdf.py"
    ).read_text(encoding="utf-8")
    assert "REFERENCE_FONT_TOO_BOLD" in qa_source
    before = qa_source.split("REFERENCE_FONT_TOO_BOLD")[0][-400:]
    assert "hard_failures.append" in before


def test_real_reference_page_is_not_bold(tmp_path) -> None:
    """真实 U-Net：参考文献页九成以上字符必须是正常字重。"""

    import json
    import shutil

    real_job = (
        Path(__file__).resolve().parent.parent
        / "benchmarks"
        / "jobs-real"
        / "real-translation"
    )
    import pytest

    if not (real_job / "source.pdf").is_file():
        pytest.skip("缺少真实论文作业；真实论文受版权保护不入库")
    job_dir = tmp_path / "job"
    shutil.copytree(real_job, job_dir)
    from build_first_candidate import build_first_candidate

    report = build_first_candidate(job_dir, None)
    candidate = report.get("candidate_pdf")
    if not candidate or not Path(candidate).is_file():
        pytest.skip(f"真实作业构建未产出候选: {report.get('status')}")
    # 冻结的题录体必须是正常字重
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    reference_font = Path(job["quality"]["selected_fonts"][2])
    assert reference_weight_ok(reference_font), reference_font
    # 参考文献页（最后一页）粗体字符占比 < 10%
    document = fitz.open(candidate)
    bold = other = 0
    for block in document[-1].get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                chars = len(str(span.get("text") or "").strip())
                if "bold" in str(span.get("font") or "").casefold() or (
                    "black" in str(span.get("font") or "").casefold()
                ):
                    bold += chars
                else:
                    other += chars
    assert other > 0
    assert bold / (bold + other) < 0.10
