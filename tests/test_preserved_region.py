"""原文区域保留：别的渲染器失败时，都退到这里来。

单独运行：
    python3 -m pytest -q tests/test_preserved_region.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.render.preserved_region_renderer import (  # noqa: E402
    EDGE_PADDING_PT,
    MIN_RASTER_DPI,
    MODE_RASTER,
    MODE_VECTOR,
    PreservedRegionError,
    build_translation_key,
    preserve_full_page,
    preserve_region,
    region_content_hash,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _real_source_and_figure():
    source = REAL_JOB / "source.pdf"
    elements = REAL_JOB / "source_elements.json"
    if not source.is_file() or not elements.is_file():
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    data = json.loads(elements.read_text(encoding="utf-8"))
    figures = [
        element
        for element in data["elements"]
        if element["type"] == "vector-figure"
    ]
    if not figures:
        pytest.skip("样本论文没有矢量图元素")
    densest = max(
        figures, key=lambda item: item["detail"].get("drawing_count", 0)
    )
    return fitz.open(source), densest


def _synthetic_source(tmp_path: Path) -> Path:
    """一份可控的合成原文：一页上有若干矩形和一段文字。"""

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    for index in range(12):
        page.draw_rect(
            fitz.Rect(
                60 + (index % 4) * 100,
                120 + (index // 4) * 80,
                140 + (index % 4) * 100,
                180 + (index // 4) * 80,
            ),
            color=(0, 0, 0),
            width=1,
        )
    page.insert_text((60, 400), "region content marker", fontsize=11)
    path = tmp_path / "synthetic-source.pdf"
    document.save(path)
    document.close()
    return path


def test_min_raster_dpi_is_enforced(tmp_path: Path) -> None:
    """栅格化降级不得低于 300 DPI。"""

    assert MIN_RASTER_DPI == 300
    source = fitz.open(_synthetic_source(tmp_path))
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    with pytest.raises(PreservedRegionError) as excinfo:
        preserve_region(
            source,
            page,
            source_page=1,
            source_bbox=[60, 120, 540, 380],
            target_bbox=[50, 100, 545, 400],
            element_id="e1",
            dpi=150,
        )
    assert "300 DPI" in str(excinfo.value)


def test_vector_preservation_keeps_drawings(tmp_path: Path) -> None:
    """矢量保留：绘图对象一个不少地进候选。"""

    source = fitz.open(_synthetic_source(tmp_path))
    expected = len(source[0].get_drawings())
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    result = preserve_region(
        source,
        page,
        source_page=1,
        source_bbox=[60, 120, 540, 380],
        target_bbox=[50, 100, 545, 400],
        element_id="e1",
    )
    assert result.mode == MODE_VECTOR
    assert result.dpi is None
    saved = tmp_path / "vector.pdf"
    output.save(saved)
    output.close()
    with fitz.open(saved) as check:
        assert len(check[0].get_drawings()) == expected


def test_raster_fallback_produces_an_image(tmp_path: Path) -> None:
    source = fitz.open(_synthetic_source(tmp_path))
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    result = preserve_region(
        source,
        page,
        source_page=1,
        source_bbox=[60, 120, 540, 380],
        target_bbox=[50, 100, 545, 400],
        element_id="e1",
        force_raster=True,
    )
    assert result.mode == MODE_RASTER
    assert result.dpi >= MIN_RASTER_DPI
    saved = tmp_path / "raster.pdf"
    output.save(saved)
    output.close()
    with fitz.open(saved) as check:
        assert len(check[0].get_images()) == 1


def test_edges_are_not_cropped(tmp_path: Path) -> None:
    """区域向外扩一点，宁可留白边也不切掉箭头和数字。"""

    source = fitz.open(_synthetic_source(tmp_path))
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    requested = [100.0, 200.0, 300.0, 320.0]
    result = preserve_region(
        source,
        page,
        source_page=1,
        source_bbox=requested,
        target_bbox=[50, 100, 545, 400],
        element_id="e1",
    )
    assert result.source_bbox[0] == pytest.approx(
        requested[0] - EDGE_PADDING_PT
    )
    assert result.source_bbox[2] == pytest.approx(
        requested[2] + EDGE_PADDING_PT
    )


def test_padding_never_leaves_the_page(tmp_path: Path) -> None:
    source = fitz.open(_synthetic_source(tmp_path))
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    result = preserve_region(
        source,
        page,
        source_page=1,
        source_bbox=[0, 0, 595, 842],
        target_bbox=[0, 0, 595, 842],
        element_id="e1",
    )
    assert result.source_bbox[0] >= 0
    assert result.source_bbox[2] <= 595


def test_content_hash_is_about_content_not_pixels(tmp_path: Path) -> None:
    """矢量与栅格两种模式保的是同一块内容，哈希必须一致。"""

    source = fitz.open(_synthetic_source(tmp_path))
    output = fitz.open()
    box = [60, 120, 540, 380]
    vector = preserve_region(
        source,
        output.new_page(width=595, height=842),
        source_page=1,
        source_bbox=box,
        target_bbox=[50, 100, 545, 400],
        element_id="e1",
    )
    raster = preserve_region(
        source,
        output.new_page(width=595, height=842),
        source_page=1,
        source_bbox=box,
        target_bbox=[50, 100, 545, 400],
        element_id="e1",
        force_raster=True,
    )
    assert vector.content_sha256 == raster.content_sha256
    assert len(vector.content_sha256) == 64


def test_different_regions_have_different_hashes(tmp_path: Path) -> None:
    source = fitz.open(_synthetic_source(tmp_path))
    first = region_content_hash(source, 1, (60, 120, 300, 250))
    second = region_content_hash(source, 1, (60, 380, 540, 420))
    assert first != second


def test_invalid_page_is_rejected(tmp_path: Path) -> None:
    source = fitz.open(_synthetic_source(tmp_path))
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    with pytest.raises(PreservedRegionError):
        preserve_region(
            source,
            page,
            source_page=99,
            source_bbox=[1, 2, 3, 4],
            target_bbox=[1, 2, 3, 4],
            element_id="e1",
        )


def test_invalid_bbox_is_rejected(tmp_path: Path) -> None:
    source = fitz.open(_synthetic_source(tmp_path))
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    with pytest.raises(PreservedRegionError):
        preserve_region(
            source,
            page,
            source_page=1,
            source_bbox=None,
            target_bbox=[1, 2, 3, 4],
            element_id="e1",
        )


def test_full_page_preservation(tmp_path: Path) -> None:
    """第三级降级：整张原文页面原样保留。"""

    source = fitz.open(_synthetic_source(tmp_path))
    expected = len(source[0].get_drawings())
    output = fitz.open()
    result = preserve_full_page(source, output, source_page=1)
    assert result.mode == MODE_VECTOR
    saved = tmp_path / "fullpage.pdf"
    output.save(saved)
    output.close()
    with fitz.open(saved) as check:
        assert check.page_count == 1
        assert len(check[0].get_drawings()) == expected
        assert "region content marker" in check[0].get_text()


# --- 翻译键必须有来源 -------------------------------------------------------


def test_translation_key_requires_unit_id() -> None:
    with pytest.raises(PreservedRegionError) as excinfo:
        build_translation_key([{"translation": "我自己编的说明"}])
    assert "translation_unit_id" in str(excinfo.value)


def test_translation_key_with_sources_is_built() -> None:
    key = build_translation_key(
        [
            {
                "translation": "最大池化 2x2",
                "translation_unit_id": "p0002-u0011",
                "source": "max pool 2x2",
            }
        ]
    )
    assert key == ["1. max pool 2x2 -> 最大池化 2x2"]


def test_empty_translation_is_skipped() -> None:
    assert build_translation_key([{"translation": "  "}]) == []


# --- 真实论文 ---------------------------------------------------------------


def test_real_dense_vector_figure_survives_preservation(
    tmp_path: Path,
) -> None:
    """真实论文里最密的那张矢量图，绘图对象一个都不能丢。"""

    source, figure = _real_source_and_figure()
    expected = len(source[figure["page"] - 1].get_drawings())
    assert expected > 100, "样本论文应当有一页密集矢量图"

    output = fitz.open()
    page = output.new_page(width=595, height=842)
    result = preserve_region(
        source,
        page,
        source_page=figure["page"],
        source_bbox=figure["bbox"],
        target_bbox=[50, 100, 545, 420],
        element_id=figure["id"],
    )
    assert result.mode == MODE_VECTOR
    saved = tmp_path / "real-figure.pdf"
    output.save(saved)
    output.close()
    with fitz.open(saved) as check:
        assert len(check[0].get_drawings()) == expected, "图形对象丢了"
        assert check[0].get_text().strip(), "图内文字标签也要保住"


def test_real_figure_raster_fallback_keeps_content_hash(
    tmp_path: Path,
) -> None:
    source, figure = _real_source_and_figure()
    output = fitz.open()
    vector = preserve_region(
        source,
        output.new_page(width=595, height=842),
        source_page=figure["page"],
        source_bbox=figure["bbox"],
        target_bbox=[50, 100, 545, 420],
        element_id=figure["id"],
    )
    raster = preserve_region(
        source,
        output.new_page(width=595, height=842),
        source_page=figure["page"],
        source_bbox=figure["bbox"],
        target_bbox=[50, 100, 545, 420],
        element_id=figure["id"],
        force_raster=True,
    )
    assert raster.mode == MODE_RASTER
    assert raster.dpi >= MIN_RASTER_DPI
    assert raster.content_sha256 == vector.content_sha256
