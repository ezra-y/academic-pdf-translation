"""位图渲染器：原样搬、不放大、子图标签不掉进正文。

单独运行：
    python3 -m pytest -q tests/test_image_renderer.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.render.image_renderer import (  # noqa: E402
    LAYOUT_COLUMN,
    LAYOUT_ROW,
    MIN_EFFECTIVE_DPI,
    UPSCALE_SAFE_DPI,
    ImageRenderError,
    build_overlay_notes,
    clamp_scale,
    effective_dpi,
    is_subfigure_label,
    layout_image_group,
    render_image,
    render_image_group,
    verify_image_output,
    verify_label_sequence,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _real_images():
    source = REAL_JOB / "source.pdf"
    elements = REAL_JOB / "source_elements.json"
    if not source.is_file() or not elements.is_file():
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    data = json.loads(elements.read_text(encoding="utf-8"))
    images = [
        element
        for element in data["elements"]
        if element["type"] == "raster-figure"
    ]
    if not images:
        pytest.skip("样本论文没有位图")
    return fitz.open(source), images


def _group_on_page(images: list[dict], page: int) -> list[dict]:
    return sorted(
        [item for item in images if item["page"] == page],
        key=lambda item: item["bbox"][0],
    )


def _labels_for(group: list[dict]) -> dict[str, str]:
    return {
        item["id"]: chr(ord("a") + index) for index, item in enumerate(group)
    }


# --- 分辨率 -----------------------------------------------------------------


def test_effective_dpi_is_pixels_over_inches() -> None:
    assert effective_dpi(300, 72.0) == pytest.approx(300.0)
    assert effective_dpi(0, 72.0) == 0.0
    assert effective_dpi(300, 0.0) == 0.0


def test_low_resolution_images_are_never_enlarged() -> None:
    """150 DPI 的图铺得再大也不会变清楚，只会变糊。"""

    scale, note = clamp_scale(2.5, source_dpi=150.0)
    assert scale == 1.0
    assert note is not None and "收回到 1.00" in note


def test_high_resolution_images_may_be_enlarged() -> None:
    scale, note = clamp_scale(1.4, source_dpi=UPSCALE_SAFE_DPI + 1)
    assert scale == pytest.approx(1.4)
    assert note is None


def test_shrinking_is_always_allowed() -> None:
    scale, note = clamp_scale(0.5, source_dpi=90.0)
    assert scale == 0.5
    assert note is None


# --- 子图标签 ---------------------------------------------------------------


def test_subfigure_label_forms_are_recognized() -> None:
    for text in ("a", "(a)", "a)", "A.", " b "):
        assert is_subfigure_label(text) in {"a", "b"}
    assert is_subfigure_label("图 1") is None
    assert is_subfigure_label("") is None


def test_label_sequence_must_be_complete() -> None:
    assert verify_label_sequence(["a", "b", "c", "d"]) == []
    assert verify_label_sequence(["a", "b", "d"])
    assert verify_label_sequence(["a", "a", "b"])


def test_non_label_text_is_rejected(tmp_path: Path) -> None:
    source, images = _real_images()
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    with pytest.raises(ImageRenderError):
        render_image(
            source,
            page,
            images[0],
            target_bbox=[60, 150, 160, 250],
            subfigure_label="这是一整句说明",
        )
    output.close()


# --- 浮层说明 ---------------------------------------------------------------


def test_overlay_note_requires_a_source_unit() -> None:
    """图上不许出现原文没有依据的中文。"""

    with pytest.raises(ImageRenderError) as excinfo:
        build_overlay_notes([{"translation": "我自己编的说明"}])
    assert "translation_unit_id" in str(excinfo.value)


def test_overlay_note_with_a_source_unit_is_kept() -> None:
    assert build_overlay_notes(
        [{"translation_unit_id": "u1", "translation": "输入图像"}]
    ) == ["输入图像"]


# --- 排列 -------------------------------------------------------------------


def test_real_subfigure_group_fits_in_one_row() -> None:
    source, images = _real_images()
    group = _group_on_page(images, 5)
    assert len(group) == 4
    layout, placements, warnings = layout_image_group(
        group, [60, 150, 535, 330]
    )
    assert layout == LAYOUT_ROW
    assert warnings == []
    assert all(item.scale <= 1.0 for item in placements)
    for left, right in zip(placements, placements[1:], strict=False):
        assert right.target_bbox[0] >= left.target_bbox[2]


def test_narrow_area_switches_to_vertical_stacking() -> None:
    """横排要缩到看不清时，改纵排——占地方，但图还能看。"""

    source, images = _real_images()
    group = _group_on_page(images, 5)
    layout, placements, warnings = layout_image_group(
        group, [60, 150, 200, 760]
    )
    assert layout == LAYOUT_COLUMN
    assert any("改为纵向排列" in warning for warning in warnings)
    for upper, lower in zip(placements, placements[1:], strict=False):
        assert lower.target_bbox[1] >= upper.target_bbox[3]


def test_group_too_tall_for_the_area_is_reported() -> None:
    source, images = _real_images()
    group = _group_on_page(images, 5)
    _, _, warnings = layout_image_group(group, [60, 150, 200, 300])
    assert any("另起一页" in warning for warning in warnings)
    assert any("不得拆散" in warning for warning in warnings)


def test_layout_needs_a_valid_area() -> None:
    source, images = _real_images()
    with pytest.raises(ImageRenderError):
        layout_image_group(_group_on_page(images, 5), None)


# --- 真实论文 ---------------------------------------------------------------


def test_real_group_keeps_resolution_and_labels(tmp_path: Path) -> None:
    """四联子图整组同页，a/b/c/d 齐全，谁也没被放大。"""

    source, images = _real_images()
    group = _group_on_page(images, 5)
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    layout, rendered, _ = render_image_group(
        source,
        page,
        group,
        area_bbox=[60, 150, 535, 330],
        subfigure_labels=_labels_for(group),
        caption_element_id="p0005-caption-001",
        caption_page=1,
    )
    assert layout == LAYOUT_ROW
    assert [item.subfigure_label for item in rendered] == ["a", "b", "c", "d"]
    assert all(item.scale <= 1.0 for item in rendered)
    assert all(item.effective_dpi >= MIN_EFFECTIVE_DPI for item in rendered)
    assert verify_image_output(rendered, page, body_bbox=[60, 400, 535, 780]) == []
    output.close()


def test_real_group_survives_a_save_reload(tmp_path: Path) -> None:
    """存盘再读，标签还在图上——渲染器说画了不算数。"""

    source, images = _real_images()
    group = _group_on_page(images, 5)
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    _, rendered, _ = render_image_group(
        source,
        page,
        group,
        area_bbox=[60, 150, 535, 330],
        subfigure_labels=_labels_for(group),
    )
    saved = tmp_path / "images.pdf"
    output.save(saved)
    output.close()
    with fitz.open(saved) as check:
        assert len(check[0].get_images()) >= len(group)
        assert verify_image_output(rendered, check[0]) == []


def test_real_low_resolution_images_are_flagged(tmp_path: Path) -> None:
    """样本第 7 页有两张 150 DPI 的图，必须被点名，不许悄悄放过。"""

    source, images = _real_images()
    group = _group_on_page(images, 7)
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    _, rendered, _ = render_image_group(
        source,
        page,
        group,
        area_bbox=[60, 150, 535, 330],
        subfigure_labels=_labels_for(group),
    )
    output.close()
    low = [item for item in rendered if item.source_dpi < 200]
    assert low, "样本第 7 页应当有低分辨率图"
    for item in low:
        assert any("看不清" in warning for warning in item.warnings)


def test_label_inside_the_body_area_is_reported(tmp_path: Path) -> None:
    """a/b/c/d 掉进正文就是排版事故，必须报出来。"""

    source, images = _real_images()
    group = _group_on_page(images, 5)
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    _, rendered, _ = render_image_group(
        source,
        page,
        group,
        area_bbox=[60, 150, 535, 330],
        subfigure_labels=_labels_for(group),
    )
    problems = verify_image_output(
        rendered, page, body_bbox=[0, 0, 595, 842]
    )
    output.close()
    assert any("落进了正文区域" in problem for problem in problems)


def test_caption_on_another_page_is_reported(tmp_path: Path) -> None:
    source, images = _real_images()
    group = _group_on_page(images, 5)
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    _, rendered, _ = render_image_group(
        source,
        page,
        group,
        area_bbox=[60, 150, 535, 330],
        subfigure_labels=_labels_for(group),
        caption_element_id="p0005-caption-001",
        caption_page=9,
    )
    problems = verify_image_output(rendered, page)
    output.close()
    assert any(
        "必须同页" in warning for item in rendered for warning in item.warnings
    )
    assert any("不在同一页" in problem for problem in problems)


def test_upscaling_a_low_resolution_image_is_caught(tmp_path: Path) -> None:
    """就算有人绕过渲染器硬填一个放大倍数，核对也要拦住。"""

    from academic_pdf_translation.render.image_renderer import RenderedImage

    rendered = RenderedImage(
        element_id="p0007-image-003",
        source_page=7,
        candidate_page=1,
        candidate_bbox=[0, 0, 1, 1],
        mode="preserve-image-as-is",
        preserve_mode="vector",
        content_sha256="0" * 64,
        pixel_width=174,
        pixel_height=118,
        source_dpi=150.0,
        effective_dpi=75.0,
        scale=2.0,
    )
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    problems = verify_image_output([rendered], page)
    output.close()
    assert any("被放大" in problem for problem in problems)
    assert any("低于" in problem for problem in problems)


def test_group_split_across_pages_is_reported(tmp_path: Path) -> None:
    from academic_pdf_translation.render.image_renderer import RenderedImage

    def _stub(element_id: str, page: int) -> RenderedImage:
        return RenderedImage(
            element_id=element_id,
            source_page=5,
            candidate_page=page,
            candidate_bbox=[0, 0, 1, 1],
            mode="preserve-image-as-is",
            preserve_mode="vector",
            content_sha256="0" * 64,
            pixel_width=512,
            pixel_height=512,
            source_dpi=520.0,
            effective_dpi=520.0,
            scale=1.0,
        )

    output = fitz.open()
    page = output.new_page(width=595, height=842)
    problems = verify_image_output([_stub("i1", 1), _stub("i2", 2)], page)
    output.close()
    assert any("分到了第" in problem for problem in problems)


def test_missing_bbox_is_rejected(tmp_path: Path) -> None:
    source, images = _real_images()
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    broken = dict(images[0])
    broken["bbox"] = None
    with pytest.raises(ImageRenderError):
        render_image(source, page, broken, target_bbox=[60, 150, 160, 250])
    output.close()
