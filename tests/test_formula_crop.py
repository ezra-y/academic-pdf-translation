"""公式裁切三步法：内容并集、方向边距、边缘墨迹检查。

单独运行：
    python3 -m pytest -q tests/test_formula_crop.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.render.formula_crop import (  # noqa: E402
    STATUS_OK,
    STATUS_UNCERTAIN,
    compute_formula_crop,
    full_line_fallback_box,
    span_is_prose,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


@pytest.fixture()
def formula_page():
    """合成一页：求和号带上下界、分式、根号、上下标、行末编号、
    公式后的英文散文、两行公式、页底公式。"""

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    # 上方散文（挡板）
    page.insert_text(
        (60, 96), "The energy is computed over all the pixels here.",
        fontsize=10,
    )
    # 求和号上界（在检出框上方）
    page.insert_text((200, 148), "K", fontsize=7)
    # 公式主体（检出框附近）
    page.insert_text((160, 166), "E = SUM w(x) log(p(x))", fontsize=12)
    # 求和号下界（在检出框下方）
    page.insert_text((198, 180), "x", fontsize=7)
    # 行末编号
    page.insert_text((480, 166), "(1)", fontsize=10)
    # 下方散文（挡板）
    page.insert_text(
        (60, 224),
        "where the weight map is described in the following section.",
        fontsize=10,
    )
    yield document, page
    document.close()


SEED = (160.0, 156.0, 300.0, 170.0)


def test_formula_top_is_not_cropped(formula_page) -> None:
    """求和号上界在检出框上方，最终框必须盖住它。"""

    _, page = formula_page
    crop = compute_formula_crop(page, SEED)
    assert crop.status == STATUS_OK
    assert crop.box[1] <= 141.5, crop.as_dict()


def test_formula_bottom_is_not_cropped(formula_page) -> None:
    _, page = formula_page
    crop = compute_formula_crop(page, SEED)
    assert crop.status == STATUS_OK
    assert crop.box[3] >= 181.0, crop.as_dict()


def test_formula_number_is_included(formula_page) -> None:
    _, page = formula_page
    crop = compute_formula_crop(page, SEED)
    assert crop.box[2] >= 495.0, crop.as_dict()
    assert any("行末编号" in note for note in crop.reason.split("；"))


def test_formula_caption_prose_is_not_inside_crop(formula_page) -> None:
    """公式后的完整英文句子是挡板，不并进公式区域。"""

    _, page = formula_page
    crop = compute_formula_crop(page, SEED)
    # 散文行在 y≈214..226，最终框底不越过它的上缘
    assert crop.box[3] <= 216.5, crop.as_dict()
    assert crop.box[1] >= 95.0, crop.as_dict()


def test_uncertain_formula_falls_back_to_full_line() -> None:
    """扩到上限仍贴边 → 如实报不确定，保底框只收整行不切半行。"""

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    # 密集散文块，行距小到边缘永远有墨迹
    for row in range(20):
        page.insert_text(
            (40, 120 + row * 11),
            "activation in feature channel at the pixel position with",
            fontsize=10,
        )
    seed = (200.0, 175.0, 320.0, 186.0)
    crop = compute_formula_crop(page, seed)
    assert crop.status == STATUS_UNCERTAIN
    band = full_line_fallback_box(page, seed)
    # 保底带覆盖版心整宽，且上下缘不落在任何文字行中间
    assert band[0] == 0.0 and band[2] == 595.0
    for block in page.get_text("dict")["blocks"]:
        for line in block["lines"]:
            top, bottom = line["bbox"][1], line["bbox"][3]
            intersects = top < band[3] and bottom > band[1]
            if intersects:
                assert band[1] <= top and band[3] >= bottom
    document.close()


def test_prose_detector_separates_sentences_from_fragments() -> None:
    assert span_is_prose("where the weight map is described here")
    assert span_is_prose("其中 wc 是用于平衡类别频率的权重图")
    assert not span_is_prose("E =")
    assert not span_is_prose("w(x) log(p(x))")
    assert not span_is_prose("(1)")


# --- 真实 U-Net 检查 --------------------------------------------------------


@pytest.fixture(scope="module")
def real_candidate(tmp_path_factory):
    needed = [
        REAL_JOB / "source.pdf",
        REAL_JOB / "translation.json",
        REAL_JOB / "render_plan.json",
    ]
    if not all(path.is_file() for path in needed):
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    job_dir = tmp_path_factory.mktemp("formula-job") / "job"
    shutil.copytree(REAL_JOB, job_dir)
    from build_first_candidate import build_first_candidate

    report = build_first_candidate(job_dir, None)
    candidate = report.get("candidate_pdf")
    if not candidate or not Path(candidate).is_file():
        pytest.skip(f"真实作业构建未产出候选: {report.get('status')}")
    return job_dir, fitz.open(candidate)


def test_formula_fragments_are_not_rendered_twice(real_candidate) -> None:
    """保留区域里的英文解释不许再以文字形式出现在候选文字层。"""

    _, candidate = real_candidate
    text = "\n".join(page.get_text("text") for page in candidate)
    assert "denotes the" not in text
    assert text.count("其中 ak(x) 表示") <= 1


def test_real_formula_numbers_survive(real_candidate) -> None:
    """公式编号 (1)(2) 必须留在页面上（文字层或保留图内）。"""

    job_dir, candidate = real_candidate
    import json

    items = json.loads(
        (job_dir / "complex_content.json").read_text(encoding="utf-8")
    )["items"]
    crops = [
        (item["id"], item["payload"].get("formula_crop"))
        for item in items
        if isinstance(item.get("payload"), dict)
        and item["payload"].get("formula_crop")
    ]
    assert crops, "真实论文应当有公式裁切记录"
    for item_id, crop in crops:
        assert crop["status"] in (STATUS_OK, STATUS_UNCERTAIN)
        assert len(crop["box"]) == 4
        assert crop["reason"], item_id
