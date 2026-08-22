"""复杂页的路由与质检口径：哪些页算复杂页，复杂页该怎么查。

候选页从两条来源汇总，占位符、图片缺失、残留原文、字体名、
参考文献标题、压缩页返修，都要在同一套口径下判断。
断言逐字保留原样，中文说明本身就是判据文档。

单独运行：
    python3 -m pytest -q tests/test_complex_page_routing.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_candidate import _is_reference_heading_unit  # noqa: E402
from qa_pdf import (  # noqa: E402
    _all_complex_candidate_pages,
    _allowed_latin_corpus,
    _complex_localized_source_labels,
    _compressed_page_requires_repair,
    _expected_literal_placeholder_tokens,
    _font_name_token,
    _inventory_accounts_for_missing_image,
    _meaningful_image_bbox,
    _placeholder_token,
    _pre_complex_break_pages,
    _residual_source_prose,
    _structured_complex_candidate_pages,
)
from validate_job import _replace_page_unit_pages  # noqa: E402


def test_complex_page_qa_routing() -> None:
    structured_page = {
        "compressed_despite_blank_space": True,
        "whole_page_reference_exception": False,
        "structured_table_visual_check": True,
        "complex_visual_page": True,
    }
    if _compressed_page_requires_repair(structured_page):
        raise AssertionError("结构化表格不得套用普通正文缩排门槛")
    normal_page = {
        **structured_page,
        "structured_table_visual_check": False,
        "complex_visual_page": False,
    }
    if not _compressed_page_requires_repair(normal_page):
        raise AssertionError("普通正文缩字且留白时仍应阻断")
    if _compressed_page_requires_repair(
        {**normal_page, "is_final_candidate_page": True}
    ):
        raise AssertionError("最后一页内容完整时应允许自然收尾留白")
    if not _inventory_accounts_for_missing_image(
        {
            "translation_policy": "omit-nonsemantic",
            "text_status": "not-applicable",
            "translation_policy_reason": "装饰背景不承载信息。",
        }
    ):
        raise AssertionError("明确省略的无语义图像应计入图像处理清单")
    if _inventory_accounts_for_missing_image(
        {
            "translation_policy": "preserve-original",
            "text_status": "not-applicable",
            "translation_policy_reason": "原图保留。",
        }
    ):
        raise AssertionError("声明保留原图时，候选缺图仍应阻断")
    if not _inventory_accounts_for_missing_image(
        {
            "method": "vector-rebuild",
            "status": "payload-ready",
            "payload_status": "ready",
            "text_status": "translated",
            "complex_payload_id": "p0004-figure-1",
        }
    ):
        raise AssertionError("已绑定的结构化矢量载荷应解释源位图替换")
    if _meaningful_image_bbox(
        [512.5, 531.1, 514.8, 533.4],
        page_width=595.3,
        page_height=841.9,
    ):
        raise AssertionError("PDF 内部的微小图像标记不得触发缺图返修")
    if not _meaningful_image_bbox(
        [100.0, 120.0, 260.0, 260.0],
        page_width=595.3,
        page_height=841.9,
    ):
        raise AssertionError("具有可见面积的图片必须进入缺图检查")
    spans = [
        {
            "text": "Meta-Analysis Of Observational Studies in Epidemiology",
            "bbox": [10, 10, 300, 24],
        }
    ]
    if not _residual_source_prose(
        spans,
        1,
        {"regions": []},
        [],
    ):
        raise AssertionError("未说明的拉丁语句仍应被识别")
    if _residual_source_prose(
        spans,
        1,
        {"regions": []},
        [],
        "metaanalysisofobservationalstudiesinepidemiology",
    ):
        raise AssertionError("译文中明确保留的正式名称不应被误判为残留")
    acronym_corpus = _allowed_latin_corpus(
        "On responsible applications of generative AI "
        "in the digital afterlife industry"
    )
    if _residual_source_prose(
        [
            {
                "text": (
                    "On responsible applications of generative "
                    "in the digital afterlife industry"
                ),
                "bbox": [10, 10, 420, 24],
            }
        ],
        1,
        {"regions": []},
        [],
        acronym_corpus,
    ):
        raise AssertionError("检测器省略短缩写时不得误报合法保留题名")
    if _complex_localized_source_labels(
        {
            "payload": {
                "regions": [
                    {
                        "localized_labels": [
                            {
                                "source": "Emotional Support",
                                "translation": "情感支持",
                            }
                        ]
                    }
                ]
            }
        }
    ) != ["Emotional Support"]:
        raise AssertionError("图内对照格的原图文字必须形成可验证残留白名单")
    if _pre_complex_break_pages(
        {
            "candidate_pages": [
                {"candidate_page": 12, "source_pages": [9, 10]},
                {"candidate_page": 13, "source_pages": [10]},
            ]
        },
        {13},
    ) != {12}:
        raise AssertionError("大型复杂内容前的同源自然分页必须可识别")
    if _pre_complex_break_pages(
        {
            "candidate_pages": [
                {"candidate_page": 8, "source_pages": [6]},
                {"candidate_page": 9, "source_pages": [7]},
            ]
        },
        {9},
    ) != {8}:
        raise AssertionError("紧邻原文页的大型复杂内容自然分页必须可识别")
    if _all_complex_candidate_pages(
        {
            "items": [
                {
                    "id": "photo-complex",
                    "status": "ready",
                    "method": "image-text-localization",
                }
            ]
        },
        {
            "complex_items": [
                {
                    "complex_item_id": "photo-complex",
                    "candidate_pages": [15],
                }
            ]
        },
    ) != {15}:
        raise AssertionError("自然分页必须覆盖图片和图表等全部已就绪复杂载荷")
    literal_placeholders = _expected_literal_placeholder_tokens(
        {
            "units": [
                {
                    "page": 38,
                    "source": "Return JSON: {{ \"key\": [definitions] }}",
                    "translation": "返回 JSON：{{",
                },
                {
                    "page": 38,
                    "source": "continued code",
                    "translation": '"key": [定义列表] }}',
                },
            ]
        }
    )
    if _placeholder_token(
        '{{\n"key": [定义列表] }}'
    ) not in literal_placeholders:
        raise AssertionError("原文代码中的双花括号模板不得被误报为占位符")
    replaced = _replace_page_unit_pages(
        {
            "items": [
                {
                    "page": 8,
                    "status": "ready",
                    "payload": {"render_policy": "replace-page-units"},
                },
                {
                    "page": 9,
                    "status": "draft",
                    "payload": {"render_policy": "replace-page-units"},
                },
            ]
        }
    )
    if replaced != {8}:
        raise AssertionError("只有就绪的复杂页载荷可以替换普通译文单元")
    structured_pages = _structured_complex_candidate_pages(
        {
            "items": [
                {
                    "id": "p0003-complex",
                    "status": "ready",
                    "method": "structured-table-rebuild",
                },
                {
                    "id": "p0004-complex",
                    "status": "draft",
                    "method": "structured-table-rebuild",
                },
            ]
        },
        {
            "complex_items": [
                {
                    "complex_item_id": "p0003-complex",
                    "candidate_pages": [4, 5],
                },
                {
                    "complex_item_id": "p0004-complex",
                    "candidate_pages": [6],
                },
            ]
        },
    )
    if structured_pages != {4, 5}:
        raise AssertionError("结构表候选页必须直接由复杂载荷与页映射识别")
    if not _is_reference_heading_unit(
        {
            "kind": "heading",
            "translation": "参考文献",
        }
    ):
        raise AssertionError("参考文献标题不得使纯题录页重复渲染")
    if _is_reference_heading_unit(
        {
            "kind": "heading",
            "translation": "讨论",
        }
    ):
        raise AssertionError("普通章节标题不得被误判为题录页标题")
    if _font_name_token("AAAAAA+STHeitiTC-Medium-0") != (
        _font_name_token("STHeitiTC-Medium")
    ):
        raise AssertionError("嵌入字体的子集前缀和编号不得影响使用判断")
