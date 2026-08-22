"""图片本地化与版面控制：图注、裁剪框、表格与复杂载荷的排布。

这一支管的是图片放进译文页之后长什么样：标签重排、行片段拼接、
裁剪边界、页内单元顺序、留白是否过量。
断言逐字保留原样，中文说明本身就是判据文档。

单独运行：
    python3 -m pytest -q tests/test_image_localization.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import base64  # noqa: E402
import re  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from build_candidate import (  # noqa: E402
    _bounded_float,
    _complex_flowables,
    _image_clip_bbox,
    _image_flowables,
    _join_target_fragments,
    _localized_image_label_flowables,
    _localized_image_labels,
    _markup,
    _ordered_page_units,
    _should_join_line_fragment,
    _styles,
    _table_flowables,
)
from qa_pdf import _excessive_unused_space_unjustified  # noqa: E402
from set_complex_payload import validate_complex_payload_item  # noqa: E402


def test_image_localization_layout_controls() -> None:
    fragment_a = {
        "id": "p0003-u0008",
        "page": 3,
        "kind": "body",
        "source": (
            "Being able to imagine one's future life is highly important "
            "for our ability to adjust to society,"
        ),
        "translation": "能够想象自己的未来生活，对于我们适应社会、",
        "source_bbox": [92.6, 290.3, 541.1, 306.1],
    }
    fragment_b = {
        "id": "p0003-u0009",
        "page": 3,
        "kind": "body",
        "source": (
            "set personal goals and keep a direction in life "
            "(McAdams, 2001)."
        ),
        "translation": "设定个人目标并保持人生方向十分重要。",
        "source_bbox": [56.6, 317.9, 497.5, 333.7],
    }
    if not _should_join_line_fragment(
        [fragment_a],
        fragment_b,
        page_width=595.3,
    ):
        raise AssertionError("同一原段落的连续行片段必须合并排版")
    new_paragraph = {
        **fragment_b,
        "id": "p0003-u0010",
        "source": "A new paragraph begins here.",
        "translation": "新段落从这里开始。",
        "source_bbox": [92.6, 345.5, 360.0, 361.3],
    }
    if _should_join_line_fragment(
        [fragment_a, fragment_b],
        new_paragraph,
        page_width=595.3,
    ):
        raise AssertionError("句末后的新缩进不得被并入上一段")
    heading_a = {
        **fragment_a,
        "id": "p0003-u0002",
        "kind": "heading",
        "source": "The Future is Bright and Predictable:",
        "translation": "未来光明且可预测：",
        "source_bbox": [67.9, 124.7, 530.2, 140.7],
    }
    heading_b = {
        **fragment_b,
        "id": "p0003-u0003",
        "kind": "heading",
        "source": "Childhood and Adolescence",
        "translation": "童年与青春期",
        "source_bbox": [226.6, 152.3, 371.6, 168.3],
    }
    if not _should_join_line_fragment(
        [heading_a],
        heading_b,
        page_width=595.3,
    ):
        raise AssertionError("同一标题的换行片段必须合并")
    if _join_target_fragments(
        ["想象个人", "未来。"],
        target_language="zh-Hans",
    ) != "想象个人未来。":
        raise AssertionError("中文行片段拼接不得凭空插入西文空格")
    if _join_target_fragments(
        ["Tulving,", "2002"],
        target_language="zh-Hans",
    ) != "Tulving, 2002":
        raise AssertionError("中文译文中的连续西文片段必须保留词间空格")
    if _bounded_float(
        0.95,
        default=0.72,
        lower=0.3,
        upper=1.0,
    ) != 0.95:
        raise AssertionError("单幅统计图应支持接近版心宽度的显示比例")
    if _bounded_float(
        9,
        default=0.48,
        lower=0.3,
        upper=0.49,
    ) != 0.49:
        raise AssertionError("多图并排时显示比例必须受栏宽上限约束")
    if _bounded_float(
        "invalid",
        default=260,
        lower=120,
        upper=520,
    ) != 260:
        raise AssertionError("无效图像尺寸参数必须回落到稳定默认值")
    labels = _localized_image_labels(
        {
            "localized_labels": [
                {"source": "Sensitivity", "translation": "敏感度"},
                {"label": "Optimal", "target": "最优点"},
                {
                    "source_text": ["0.00", "1.00"],
                    "translation": ["0.00", "1.00"],
                },
                "N 表示症状总数；k 表示必须满足的症状数。",
            ]
        }
    )
    if labels != [
        ("Sensitivity", "敏感度"),
        ("Optimal", "最优点"),
        ("", "N 表示症状总数；k 表示必须满足的症状数。"),
    ]:
        raise AssertionError("统计图内文字必须形成可机读的源文—译文对应")
    image_label_styles = _styles(
        regular_font="Helvetica",
        bold_font="Helvetica-Bold",
        reference_font="Helvetica",
        body_font_pt=9,
        leading_ratio=1.6,
        reference_font_pt=8.5,
    )
    image_label_flowables = _localized_image_label_flowables(
        labels,
        styles=image_label_styles,
        available_width=280,
    )
    if type(image_label_flowables[1]).__name__ != "KeepTogether":
        raise AssertionError("图内文字对照标题必须与第一行映射保持同页")
    grouped_image_label_flowables = _localized_image_label_flowables(
        labels,
        styles=image_label_styles,
        available_width=280,
        keep_heading_with_first=False,
    )
    if any(
        type(flowable).__name__ == "KeepTogether"
        for flowable in grouped_image_label_flowables
    ):
        raise AssertionError(
            "并排图片单元格内不得嵌套无界高度的整组绑定"
        )
    translated_title_flowables = _table_flowables(
        {
            "payload": {
                "tables": [
                    {
                        "title": "Original table title",
                        "translated_title": "Translated table title",
                        "header_rows": 1,
                        "font_size_pt": 7.0,
                        "cell_padding_pt": 2.5,
                        "rows": [["Column"], ["Value"]],
                    }
                ]
            }
        },
        styles=image_label_styles,
        available_width=280,
    )
    if (
        not translated_title_flowables
        or translated_title_flowables[0].getPlainText()
        != "Translated table title"
    ):
        raise AssertionError("结构化表格必须优先渲染目标语言标题")
    if translated_title_flowables[1]._cellvalues[0][0].style.fontSize != 7.0:
        raise AssertionError("密集宽表必须支持显式可验收字号")
    mixed_flowables = _complex_flowables(
        {
            "id": "mixed-complex",
            "method": "structured-table-rebuild",
            "payload": {
                "tables": [
                    {
                        "translated_title": "Primary table",
                        "header_rows": 1,
                        "rows": [["Column"], ["Value"]],
                    }
                ],
                "components": [
                    {
                        "method": "structured-table-rebuild",
                        "payload": {
                            "tables": [
                                {
                                    "translated_title": "Component table",
                                    "header_rows": 1,
                                    "rows": [["Column"], ["Value"]],
                                }
                            ]
                        },
                    }
                ],
            },
        },
        styles=image_label_styles,
        source_document=[],
        available_width=280,
        available_height=720,
        regular_font="Helvetica",
        bold_font="Helvetica-Bold",
        body_font_pt=9,
    )
    mixed_titles = [
        flowable.getPlainText()
        for flowable in mixed_flowables
        if hasattr(flowable, "getPlainText")
    ]
    if mixed_titles[:2] != ["Primary table", "Component table"]:
        raise AssertionError(
            "混合复杂页必须同时渲染顶层载荷和全部子组件"
        )
    reordered_units = _ordered_page_units(
        [
            {
                "id": "sidebar-1",
                "source": "Publication metadata",
                "source_bbox": [10, 100, 80, 120],
            },
            {
                "id": "main-1",
                "source": "Article title",
                "source_bbox": [100, 20, 280, 50],
            },
            {
                "id": "main-2",
                "source": "Article abstract",
                "source_bbox": [100, 55, 280, 100],
            },
        ],
        [
            {
                "method": "manual-reading-order-rebuild",
                "payload": {
                    "ordered_block_ids": [1, 2, 0],
                    "layout_groups": [
                        {
                            "role": "primary-reading-flow",
                            "block_ids": [1, 2],
                        },
                        {
                            "role": "publication-metadata",
                            "block_ids": [0],
                        },
                    ],
                },
            }
        ],
        {
            "blocks": [
                {
                    "id": 0,
                    "bbox": [10, 100, 80, 120],
                    "text": "Publication metadata",
                },
                {
                    "id": 1,
                    "bbox": [100, 20, 280, 50],
                    "text": "Article title",
                },
                {
                    "id": 2,
                    "bbox": [100, 55, 280, 100],
                    "text": "Article abstract",
                },
            ]
        },
    )
    if [unit["id"] for unit in reordered_units] != [
        "main-1",
        "main-2",
        "sidebar-1",
    ]:
        raise AssertionError("复杂页必须按已确认的源块顺序重排翻译单元")
    if [
        unit.get("_layout_role")
        for unit in reordered_units
    ] != [
        "primary-reading-flow",
        "primary-reading-flow",
        "publication-metadata",
    ]:
        raise AssertionError("复杂页单元必须携带可检查的布局角色")
    vector_note_flowables = _complex_flowables(
        {
            "id": "vector-note",
            "method": "vector-rebuild",
            "payload": {
                "figures": [
                    {
                        "title": "Model figure",
                        "type": "layout",
                        "height_pt": 180,
                        "nodes": [],
                        "edges": [],
                        "note": "The curves show fluctuation only.",
                        "annotations": [
                            {
                                "translation": "Unpositioned explanatory note.",
                            },
                            {
                                "translation": "Positioned node label.",
                                "x_ratio": 0.5,
                                "y_ratio": 0.5,
                            },
                        ],
                    }
                ]
            },
        },
        styles=image_label_styles,
        source_document=[],
        available_width=280,
        available_height=720,
        regular_font="Helvetica",
        bold_font="Helvetica-Bold",
        body_font_pt=9,
    )
    vector_note_text = [
        flowable.getPlainText()
        for flowable in vector_note_flowables
        if hasattr(flowable, "getPlainText")
    ]
    if "The curves show fluctuation only." not in vector_note_text:
        raise AssertionError("矢量图的图后说明必须进入候选正文")
    if "Unpositioned explanatory note." in vector_note_text:
        raise AssertionError("存在正式说明时不得重复追加未定位注释")
    if "Positioned node label." in vector_note_text:
        raise AssertionError("图内已定位标签不得在图后重复追加")
    fallback_note_flowables = _complex_flowables(
        {
            "id": "vector-fallback-note",
            "method": "vector-rebuild",
            "payload": {
                "figures": [
                    {
                        "title": "Model figure without note",
                        "type": "layout",
                        "height_pt": 180,
                        "nodes": [],
                        "edges": [],
                        "annotations": [
                            {
                                "translation": (
                                    "Fallback explanatory annotation."
                                ),
                            }
                        ],
                    }
                ]
            },
        },
        styles=image_label_styles,
        source_document=[],
        available_width=280,
        available_height=720,
        regular_font="Helvetica",
        bold_font="Helvetica-Bold",
        body_font_pt=9,
    )
    fallback_note_text = [
        flowable.getPlainText()
        for flowable in fallback_note_flowables
        if hasattr(flowable, "getPlainText")
    ]
    if "Fallback explanatory annotation." not in fallback_note_text:
        raise AssertionError("没有正式说明时应使用未定位注释兜底")
    duplicate_labels = _localized_image_labels(
        {
            "localized_labels": [
                {"source": "Top", "translation": "前"},
                {"source": "Top", "translation": "前"},
            ]
        }
    )
    if duplicate_labels != [("Top", "前")]:
        raise AssertionError("图内文字对照必须去除重复映射")
    if "\u0302" in _markup("σ̂²") or "σ^2" not in _markup("σ̂²"):
        raise AssertionError("组合帽符号必须转为无缺字的可检索数学记法")
    nul_markup = _markup("pro\x00social")
    if "\x00" in nul_markup or "pro-social" not in nul_markup:
        raise AssertionError("词内隐藏空字符必须恢复为连字符")
    mean_markup = _markup("均值（x̄）")
    mean_text = re.sub(r"<[^>]+>", "", mean_markup)
    if "\u0304" in mean_markup or "x-bar" not in mean_text:
        raise AssertionError("均值组合横线必须转为无缺字的可检索数学记法")
    ligature_markup = _markup("Matthew Ratcliﬀe")
    ligature_text = re.sub(r"<[^>]+>", "", ligature_markup)
    if "\ufb00" in ligature_markup or "Ratcliffe" not in ligature_text:
        raise AssertionError("印刷连字必须展开为普通可检索拉丁字母")
    author_star_markup = _markup("Emily S. Cross∗")
    author_star_text = re.sub(r"<[^>]+>", "", author_star_markup)
    if "\u2217" in author_star_markup or "Cross*" not in author_star_text:
        raise AssertionError("通讯作者星号必须转为普通可检索字符")
    clipped_image_bbox = _image_clip_bbox(
        {
            "source_bbox": [79.45, 204.65, 504.2, 336.6],
            "localized_caption": {
                "source_bbox": [72.03, 332.02, 241.1, 347.78],
            },
        }
    )
    if (
        clipped_image_bbox is None
        or clipped_image_bbox[:3] != [79.45, 204.65, 504.2]
        or clipped_image_bbox[3] >= 332.02
    ):
        raise AssertionError("图片截图必须排除与下边缘重叠的已登记图注")
    sparse_candidate_page = {
        "page": 3,
        "target_chars": 260,
        "mapped_has_body_prose": True,
        "mapped_has_retained_regions": False,
        "whole_page_reference_exception": False,
        "complex_visual_page": False,
        "excess_bottom_blank_ratio": 0.49,
        "largest_column_bottom_blank_ratio": 0.51,
        "top_blank_ratio": 0.02,
    }
    justified_sparse_override = {
        "page_overrides": [
            {
                "page": 3,
                "sparse_layout_justified": True,
                "reason": "下一页复杂图需按可读尺寸完整展示。",
            }
        ]
    }
    if _excessive_unused_space_unjustified(
        sparse_candidate_page,
        justified_sparse_override,
        set(),
    ):
        raise AssertionError("有明确理由的自然分页不得被重复判为异常留白")
    if not _excessive_unused_space_unjustified(
        sparse_candidate_page,
        {"page_overrides": []},
        set(),
    ):
        raise AssertionError("无说明的异常留白仍必须被自动检查拦截")
    final_sparse_page = {
        **sparse_candidate_page,
        "is_final_candidate_page": True,
    }
    if _excessive_unused_space_unjustified(
        final_sparse_page,
        {"page_overrides": []},
        set(),
    ):
        raise AssertionError("内容完整的最后一页应允许自然收尾留白")
    one_pixel_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    source_document = SimpleNamespace(
        extract_image=lambda xref: {"image": one_pixel_png}
    )
    multi_image_flowables = _image_flowables(
        {
            "page": 1,
            "payload": {
                "regions": [
                    {"xref": 1, "translation": "照片一"},
                    {"xref": 2, "translation": "照片二"},
                ]
            },
        },
        source_document=source_document,
        styles=image_label_styles,
        available_width=500,
        available_height=762,
    )
    _, multi_image_height = multi_image_flowables[0].wrap(500, 762)
    if not 0 < multi_image_height < 762:
        raise AssertionError("并排图组不得因单图绑定产生异常表格高度")
    sequential_image_flowables = _image_flowables(
        {
            "page": 1,
            "payload": {
                "regions": [
                    {"xref": 1, "translation": "照片一"},
                    {"xref": 2, "translation": "照片二"},
                ]
            },
        },
        source_document=source_document,
        styles=image_label_styles,
        available_width=500,
        available_height=100,
    )
    if len(sequential_image_flowables) <= 1:
        raise AssertionError("并排图组超过整页高度时必须自动改为可分页纵排")
    errors = validate_complex_payload_item(
        {
            "method": "image-text-localization",
            "source_evidence": ["已核对原图。"],
            "payload": {
                "regions": [
                    {
                        "xref": 12,
                        "caption": "图1",
                        "semantic_text_expected": True,
                    }
                ]
            },
        }
    )
    if not any("localized_labels" in error for error in errors):
        raise AssertionError("承载研究信息的统计图不得只翻译图题")
    anchored_errors = validate_complex_payload_item(
        {
            "method": "image-text-localization",
            "source_evidence": ["已核对原图。"],
            "payload": {
                "render_policy": "insert-after",
                "insert_before_unit_id": "p0002-u0004",
                "insert_after_unit_id": "p0002-u0005",
                "regions": [
                    {
                        "xref": 12,
                        "caption": "图1",
                    }
                ],
            },
        }
    )
    if not any("只能选择一个" in error for error in anchored_errors):
        raise AssertionError("复杂内容不得同时声明前后两个单元锚点")
