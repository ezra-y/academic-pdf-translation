"""结构化抑制：抑制看绑定与字体，不靠猜；名单落渲染日志。

单独运行：
    python3 -m pytest -q tests/test_structural_suppression.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.render.plan_bridge import (  # noqa: E402
    _is_formula_fragment,
    build_preservation_items,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


# --- 判定单元 ---------------------------------------------------------------


def test_complete_english_sentence_is_not_formula_fragment() -> None:
    """完整英文句子不因为短就被当碎片删掉。"""

    assert not _is_formula_fragment(
        {"translation": "", "source": "where the map is described below."}
    )
    assert _is_formula_fragment({"translation": "", "source": "w(x) log"})
    assert _is_formula_fragment({"translation": "", "source": "(2)"})


def test_chinese_sentences_are_never_suppressed() -> None:
    assert not _is_formula_fragment(
        {"translation": "其中 ak(x) 表示激活值", "source": "where..."}
    )


def _bridge_job():
    plan = {
        "elements": [
            {
                "element_id": "f1",
                "strategy": "preserve-formula-region",
                "page": 1,
            }
        ]
    }
    elements = [
        {
            "id": "f1",
            "type": "display-formula",
            "page": 1,
            "bbox": [100, 300, 300, 330],
        }
    ]
    units = [
        # 绑定到公式元素：结构判据，必抑制
        {
            "id": "u-frag",
            "page": 1,
            "source_bbox": [110, 305, 200, 320],
            "source": "E = sum",
            "translation": "",
            "_element_id": "f1",
        },
        # 同带中文句子：绝不抑制
        {
            "id": "u-zh",
            "page": 1,
            "source_bbox": [110, 322, 290, 334],
            "source": "where ...",
            "translation": "其中权重图定义见下文",
            "_element_id": "b9",
        },
    ]
    return plan, elements, units


def test_only_bound_formula_fragments_are_suppressed() -> None:
    plan, elements, units = _bridge_job()
    result = build_preservation_items(
        plan,
        elements,
        page_sizes={1: (595.0, 842.0)},
        units=units,
    )
    assert result.items
    suppress = result.items[0]["payload"]["suppress_texts"]
    assert "E = sum" in suppress
    assert all("其中权重图" not in text for text in suppress)


def test_translated_formula_explanation_remains_visible() -> None:
    """有中文译文的解释句即使坐标落在公式带里，也留在正文。"""

    plan, elements, units = _bridge_job()
    units[1]["source_bbox"] = [110, 305, 290, 320]  # 硬塞进公式带
    result = build_preservation_items(
        plan,
        elements,
        page_sizes={1: (595.0, 842.0)},
        units=units,
    )
    suppress = result.items[0]["payload"]["suppress_texts"]
    assert all("其中权重图" not in text for text in suppress)


# --- 真实 U-Net -------------------------------------------------------------


@pytest.fixture(scope="module")
def real_build(tmp_path_factory):
    needed = [
        REAL_JOB / "source.pdf",
        REAL_JOB / "translation.json",
        REAL_JOB / "unit_bindings.json",
    ]
    if not all(path.is_file() for path in needed):
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    job_dir = tmp_path_factory.mktemp("suppress-job") / "job"
    shutil.copytree(REAL_JOB, job_dir)
    from build_first_candidate import build_first_candidate

    report = build_first_candidate(job_dir, None)
    candidate = report.get("candidate_pdf")
    if not candidate or not Path(candidate).is_file():
        pytest.skip(f"真实作业构建未产出候选: {report.get('status')}")
    return job_dir, fitz.open(candidate)


def test_math_font_glyph_is_decoded_not_typeset_as_latin(real_build) -> None:
    """CMEX 的 'p' 是根号：行内解码成 √，不再出现孤立的 'p'。"""

    job_dir, candidate = real_build
    text = "\n".join(page.get_text("text") for page in candidate)
    assert "标准差取为" in text
    tail = text.split("标准差取为", 1)[1][:12]
    assert "√" in tail, repr(tail)
    # 任何一页都不再以孤立的 'p' 收尾
    for index in range(candidate.page_count):
        lines = candidate[index].get_text("text").strip().splitlines()
        assert lines[-1].strip() != "p", f"第 {index + 1} 页仍有孤立 p"


def test_title_continuation_is_not_duplicated(real_build) -> None:
    _, candidate = real_build
    first = candidate[0].get_text("text")
    assert "卷积网络图像分割" not in first
    assert "用于生物医学图像分割的卷积网络" in first


def test_suppression_manifest_is_written_to_render_log(real_build) -> None:
    """抑制名单必须落盘：谁被抑制、凭什么，一条条可核对。"""

    job_dir, _ = real_build
    log = json.loads(
        (job_dir / "generator-layout-log.json").read_text(encoding="utf-8")
    )
    manifest = log.get("suppressed_units")
    assert manifest, "真实论文应当有结构化抑制记录"
    for entry in manifest:
        assert entry["unit_id"]
        assert entry["reason"]
    reasons = {entry["reason"][:6] for entry in manifest}
    assert any("标题续行" in entry["reason"] for entry in manifest)
    assert any("数学" in entry["reason"] for entry in manifest), reasons


def test_source_formula_explanation_does_not_repeat(real_build) -> None:
    _, candidate = real_build
    text = "\n".join(page.get_text("text") for page in candidate)
    assert "denotes the" not in text
    assert text.count("其中 ak(x) 表示") <= 1
