"""矢量图渲染器：保留几何，只处理文字标签。

单独运行：
    python3 -m pytest -q tests/test_figure_renderer.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.analysis.source_elements import (  # noqa: E402
    build_inventory,
)
from academic_pdf_translation.analysis.unit_binding import (  # noqa: E402
    bind_units,
)
from academic_pdf_translation.render.figure_renderer import (  # noqa: E402
    MODE_LEGEND,
    MODE_OVERLAY,
    FigureRenderError,
    build_numbered_legend,
    label_mapping_confidence,
    render_figure,
    verify_figure_output,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _real_figure_with_labels():
    needed = [
        REAL_JOB / "source.pdf",
        REAL_JOB / "source_structure.json",
        REAL_JOB / "source_units.json",
        REAL_JOB / "translation.json",
    ]
    if not all(path.is_file() for path in needed):
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    inventory = build_inventory(
        json.loads(needed[1].read_text(encoding="utf-8")), pymupdf_version="1"
    )
    bind_units(
        json.loads(needed[2].read_text(encoding="utf-8"))["units"], inventory
    )
    units = {
        unit["id"]: unit
        for unit in json.loads(needed[3].read_text(encoding="utf-8"))["units"]
    }
    figures = [
        element
        for element in inventory.elements
        if element.type.value == "vector-figure"
    ]
    if not figures:
        pytest.skip("样本论文没有矢量图")
    figure = max(
        figures, key=lambda item: item.detail.get("drawing_count", 0)
    )
    labels = []
    for label_id in figure.relations.get("embedded-label", []):
        label_element = inventory.by_id(label_id)
        if label_element is None:
            continue
        for unit_id in label_element.translation_unit_ids:
            unit = units.get(unit_id)
            if unit is None:
                continue
            labels.append(
                {
                    "translation_unit_id": unit_id,
                    "source_text": unit["source"],
                    "translation": unit.get("translation") or "",
                    "source_bbox": label_element.bbox,
                }
            )
    element = {
        "id": figure.id,
        "page": figure.page,
        "bbox": list(figure.bbox),
        "detail": dict(figure.detail),
    }
    return fitz.open(needed[0]), element, labels


# --- 标签映射置信度 ---------------------------------------------------------


def test_untranslated_labels_do_not_drag_confidence_down() -> None:
    """图里的数字尺寸本来就保留原文，不该被算成映射失败。"""

    labels = [
        {
            "translation_unit_id": "u1",
            "translation": "复制并裁剪",
            "source_bbox": [1, 2, 3, 4],
        },
        {"translation_unit_id": "u2", "translation": "", "source_bbox": [5, 6, 7, 8]},
        {"translation_unit_id": "u3", "translation": None, "source_bbox": None},
    ]
    assert label_mapping_confidence(labels) == 1.0


def test_missing_unit_id_lowers_confidence() -> None:
    labels = [
        {"translation_unit_id": "u1", "translation": "甲", "source_bbox": [1, 2, 3, 4]},
        {"translation_unit_id": "", "translation": "乙", "source_bbox": [1, 2, 3, 4]},
    ]
    assert label_mapping_confidence(labels) == 0.5


def test_missing_bbox_lowers_confidence() -> None:
    labels = [
        {"translation_unit_id": "u1", "translation": "甲", "source_bbox": None},
        {"translation_unit_id": "u2", "translation": "乙", "source_bbox": [1, 2, 3, 4]},
    ]
    assert label_mapping_confidence(labels) == 0.5


def test_no_translatable_labels_means_zero_confidence() -> None:
    assert label_mapping_confidence([{"translation": ""}]) == 0.0
    assert label_mapping_confidence([]) == 0.0


# --- 编号图例 ---------------------------------------------------------------


def test_legend_requires_a_source_unit_for_every_line() -> None:
    """图例不许出现没有来源的中文。"""

    with pytest.raises(FigureRenderError) as excinfo:
        build_numbered_legend([{"translation": "我自己编的图例"}])
    assert "translation_unit_id" in str(excinfo.value)


def test_legend_is_one_line_per_label() -> None:
    """不许把几个图例并成一句话，读者要能按编号找回具体标签。"""

    lines = build_numbered_legend(
        [
            {
                "translation_unit_id": "u1",
                "source_text": "max pool 2x2",
                "translation": "最大池化 2x2",
            },
            {
                "translation_unit_id": "u2",
                "source_text": "up-conv 2x2",
                "translation": "上卷积 2x2",
            },
        ]
    )
    assert lines == [
        "1. max pool 2x2 -> 最大池化 2x2",
        "2. up-conv 2x2 -> 上卷积 2x2",
    ]


# --- 渲染 -------------------------------------------------------------------


def test_real_figure_preserves_all_drawings(tmp_path: Path) -> None:
    """真实结构图的绘图对象一个都不能少。"""

    source, element, labels = _real_figure_with_labels()
    assert element["detail"]["drawing_count"] > 100

    output = fitz.open()
    page = output.new_page(width=595, height=842)
    rendered = render_figure(
        source, page, element, target_bbox=[50, 100, 545, 420], labels=labels
    )
    assert rendered.preserve_mode == "vector"
    saved = tmp_path / "figure.pdf"
    output.save(saved)
    output.close()
    with fitz.open(saved) as check:
        assert len(check[0].get_drawings()) == element["detail"][
            "drawing_count"
        ]


def test_real_figure_keeps_numeric_anchors(tmp_path: Path) -> None:
    """通道数与特征图尺寸是文字，几何检查看不见，必须单独核。"""

    source, element, labels = _real_figure_with_labels()
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    rendered = render_figure(
        source, page, element, target_bbox=[50, 100, 545, 420], labels=labels
    )
    saved = tmp_path / "anchors.pdf"
    output.save(saved)
    output.close()

    source_page = source[element["page"] - 1]
    source_text = source_page.get_text("text", clip=fitz.Rect(*element["bbox"]))
    anchors = [
        token
        for token in ("1024", "512", "256", "128", "64")
        if token in source_text
    ]
    assert anchors, "样本图应当有通道数标注"
    with fitz.open(saved) as check:
        problems = verify_figure_output(
            rendered,
            len(check[0].get_drawings()),
            check[0].get_text(),
            expected_anchors=anchors,
        )
    assert problems == []


def test_drawing_count_alone_is_not_enough(tmp_path: Path) -> None:
    """绘图对象数量对得上，不等于图是对的：数字可能全丢了。"""

    source, element, labels = _real_figure_with_labels()
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    rendered = render_figure(
        source, page, element, target_bbox=[50, 100, 545, 420], labels=labels
    )
    output.close()
    problems = verify_figure_output(
        rendered,
        rendered.source_drawing_count,  # 几何数量完全一致
        "这里只有中文，没有任何数字",  # 但数字锚点全丢
        expected_anchors=["1024", "512"],
    )
    assert problems, "只看几何数量会漏掉数字丢失"
    assert "数字锚点丢失" in problems[0]


def test_missing_geometry_is_reported() -> None:
    from academic_pdf_translation.render.figure_renderer import RenderedFigure

    rendered = RenderedFigure(
        element_id="p0002-figure-001",
        source_page=2,
        candidate_page=1,
        candidate_bbox=[0, 0, 1, 1],
        mode=MODE_OVERLAY,
        preserve_mode="vector",
        content_sha256="0" * 64,
        source_drawing_count=213,
        preserved_area_ratio=1.0,
    )
    problems = verify_figure_output(rendered, 0, "")
    assert problems
    assert "几何结构丢了" in problems[0]


def test_reliable_labels_use_overlay(tmp_path: Path) -> None:
    source, element, labels = _real_figure_with_labels()
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    rendered = render_figure(
        source, page, element, target_bbox=[50, 100, 545, 420], labels=labels
    )
    output.close()
    assert rendered.mode == MODE_OVERLAY
    assert rendered.legend_lines == []
    covered = [item for item in rendered.labels if item.candidate_bbox]
    assert covered, "覆盖模式下标签必须有候选坐标"


def test_unreliable_labels_fall_back_to_legend(tmp_path: Path) -> None:
    """映射不可靠时改用编号图例，不把中文盖到可能错误的位置。"""

    source, element, labels = _real_figure_with_labels()
    degraded = []
    for index, label in enumerate(labels):
        entry = dict(label)
        if entry["translation"].strip() and index % 2 == 0:
            entry["source_bbox"] = None
        degraded.append(entry)
    assert label_mapping_confidence(degraded) < 0.8

    output = fitz.open()
    page = output.new_page(width=595, height=842)
    rendered = render_figure(
        source,
        page,
        element,
        target_bbox=[50, 100, 545, 420],
        labels=degraded,
    )
    output.close()
    assert rendered.mode == MODE_LEGEND
    assert rendered.legend_lines
    assert any("编号图例" in warning for warning in rendered.warnings)


def test_shrinking_the_figure_too_much_warns(tmp_path: Path) -> None:
    source, element, labels = _real_figure_with_labels()
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    rendered = render_figure(
        source,
        page,
        element,
        target_bbox=[50, 100, 150, 140],
        labels=labels,
    )
    output.close()
    assert any("看不清" in warning for warning in rendered.warnings)


def test_figure_records_caption_binding(tmp_path: Path) -> None:
    source, element, labels = _real_figure_with_labels()
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    rendered = render_figure(
        source,
        page,
        element,
        target_bbox=[50, 100, 545, 420],
        labels=labels,
        caption_element_id="p0002-caption-001",
    )
    output.close()
    assert rendered.caption_element_id == "p0002-caption-001"


def test_invalid_bbox_is_rejected(tmp_path: Path) -> None:
    source, element, labels = _real_figure_with_labels()
    output = fitz.open()
    page = output.new_page(width=595, height=842)
    broken = dict(element)
    broken["bbox"] = None
    with pytest.raises(FigureRenderError):
        render_figure(
            source, page, broken, target_bbox=[50, 100, 545, 420], labels=labels
        )
