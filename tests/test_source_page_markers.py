"""源页标记：映射靠不可见锚点，正式 PDF 里不许有调试文字。

单独运行：
    python3 -m pytest -q tests/test_source_page_markers.py
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

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


@pytest.fixture(scope="module")
def real_build(tmp_path_factory):
    """把真实作业拷到临时目录构建一次，模块内共享产物。"""

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
    job_dir = tmp_path_factory.mktemp("marker-job") / "job"
    shutil.copytree(REAL_JOB, job_dir)
    from build_first_candidate import build_first_candidate

    report = build_first_candidate(job_dir, None)
    candidate = report.get("candidate_pdf")
    if not candidate or not Path(candidate).is_file():
        pytest.skip(f"真实作业构建未产出候选: {report.get('status')}")
    return job_dir, Path(candidate)


def test_source_page_anchor_is_not_visible_text(real_build) -> None:
    """R-P1-01：任何语言的"原文第 X 页"都不许出现在文字层。"""

    from qa_pdf import visible_source_page_marker_pages

    _, candidate = real_build
    document = fitz.open(candidate)
    assert visible_source_page_marker_pages(document) == []


def test_candidate_page_map_still_exists_without_marker(real_build) -> None:
    """删掉可见标记后，源页→候选页映射必须原样可用。"""

    job_dir, _ = real_build
    mapping = json.loads(
        (job_dir / "candidate-page-map.json").read_text(encoding="utf-8")
    )
    entries = mapping["source_pages"]
    assert len(entries) == mapping["source_page_count"]
    for entry in entries:
        assert entry["candidate_pages"], entry["source_page"]


def test_visible_source_page_marker_blocks_delivery(tmp_path: Path) -> None:
    """检测函数对着带标记的 PDF 必须报页；QA 把它列为硬失败。"""

    from qa_pdf import visible_source_page_marker_pages

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((60, 100), "正文内容")
    tampered = document.new_page(width=595, height=842)
    # 英文模板不依赖 CJK 字体，命中任何语言模板都算
    tampered.insert_text((60, 140), "Source page 3", fontname="helv")
    assert visible_source_page_marker_pages(document) == [2]
    # QA 源码里必须把该信号列为 hard failure
    qa_source = (
        Path(__file__).resolve().parent.parent / "scripts" / "qa_pdf.py"
    ).read_text(encoding="utf-8")
    assert "VISIBLE_SOURCE_PAGE_MARKER" in qa_source
    before = qa_source.split("VISIBLE_SOURCE_PAGE_MARKER")[0][-300:]
    assert "hard_failures.append" in before, "该信号必须是硬失败，不是提醒"


def test_bookmark_uses_heading_not_debug_marker(real_build) -> None:
    _, candidate = real_build
    toc = fitz.open(candidate).get_toc()
    assert toc, "真实论文有章节标题，书签不该为空"
    titles = [entry[1] for entry in toc]
    assert not any("原文第" in title for title in titles)
    assert any("引言" in title for title in titles)
    # 图内标签、作者单位这类"长得像标题"的不进书签
    assert not any("输入图像" in title for title in titles)
