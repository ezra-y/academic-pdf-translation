"""质检的页面级启发式规则：表格、分栏、排版参数、留白与行距。

这些断言原本是 scripts/self_test.py 的 run() 里的内联判断。它们共用同一批
伪造的页面数据结构，判断的又都是同一层问题——这一页看起来是不是坏了——
所以归到一个文件，按检查项切成命名用例。
断言逐字保留原样，中文说明本身就是判据文档。

单独运行：
    python3 -m pytest -q tests/test_qa_page_heuristics.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from types import SimpleNamespace  # noqa: E402

from qa_pdf import (  # noqa: E402
    _body_width_collapsed,
    _bottom_whitespace_is_unbalanced,
    _column_blank_ratio,
    _document_typography_locked,
    _horizontal_width_change_justified,
    _interline_gap_outliers,
    _low_table_spans,
    _orphan_single_han_lines,
    _paragraph_gap_inflation_justified,
    _regions_for_page,
    _unit_is_substantive_body_prose,
)
from typography_fit import (  # noqa: E402
    PageFitMeasurement,
    PageTextProfile,
    select_document_typography,
)
from validate_job import (  # noqa: E402
    _has_reference_heading,
    _has_source_citation_block,
)


def test_low_table_spans_and_column_blank_ratio() -> None:
    """小字号表格文本要被找出来；单栏与双栏页的空白率必须能区分开。"""

    small_table_hits = _low_table_spans(
        [
            {
                "text": "0.154**",
                "size": 5.4,
                "bbox": [40, 40, 80, 50],
            },
            {
                "text": "正文不在表格区域",
                "size": 5.4,
                "bbox": [300, 300, 390, 312],
            },
        ],
        [{"bbox": [20, 20, 200, 200], "category": "structured-table"}],
        7.0,
    )
    if len(small_table_hits) != 1 or small_table_hits[0]["text"] != "0.154**":
        raise AssertionError("表格字号门禁必须只检查声明的结构化表格区域")

    fake_page = SimpleNamespace(
        rect=SimpleNamespace(x0=0.0, x1=600.0, width=600.0, height=800.0)
    )
    single_column_spans = [
        {
            "text": "单栏正文用于验证页面中线识别" * 2,
            "bbox": [50.0, float(y), 545.0 + index % 2 * 10, float(y + 12)],
        }
        for index, y in enumerate(range(80, 641, 40))
    ]
    if _column_blank_ratio(fake_page, single_column_spans) >= 0.18:
        raise AssertionError("全宽单栏正文不得按行中心误拆成左右两栏")

    double_column_spans = [
        {
            "text": "左栏正文用于验证真实双栏留白",
            "bbox": [40.0, float(y), 275.0, float(y + 12)],
        }
        for y in range(80, 641, 80)
    ] + [
        {
            "text": "右栏正文用于验证真实双栏留白",
            "bbox": [325.0, float(y), 560.0, float(y + 12)],
        }
        for y in range(80, 321, 40)
    ]
    if _column_blank_ratio(fake_page, double_column_spans) <= 0.4:
        raise AssertionError("真实双栏中较短栏的大面积留白仍须被识别")


def test_document_typography_selection() -> None:
    """全文字号行距由最密的一页决定，并把测量过程一并记录下来。"""

    typography_profiles = [
        PageTextProfile(
            page=1,
            translated_chars=180,
            paragraph_count=2,
            heading_count=1,
            note_count=0,
            available_width_pt=440,
            available_height_pt=700,
        ),
        PageTextProfile(
            page=2,
            translated_chars=760,
            paragraph_count=6,
            heading_count=1,
            note_count=1,
            available_width_pt=440,
            available_height_pt=700,
        ),
    ]

    def measure_typography(
        profile: PageTextProfile,
        body_size: float,
        leading_ratio: float,
    ) -> PageFitMeasurement:
        content_height = (
            profile.translated_chars
            * body_size
            * body_size
            * leading_ratio
            / 260
        )
        return PageFitMeasurement(
            page=profile.page,
            fits=content_height <= profile.available_height_pt,
            content_width_pt=profile.available_width_pt,
            content_height_pt=content_height,
            available_height_pt=profile.available_height_pt,
            fill_ratio=content_height / profile.available_height_pt,
        )

    typography_choice = select_document_typography(
        typography_profiles,
        measure_typography,
        body_font_range_pt=(8.0, 13.0),
        body_font_step_pt=0.25,
        leading_range=(1.5, 1.8),
        leading_step=0.1,
        max_densest_fill_ratio=0.95,
    )
    if typography_choice["algorithm"] != "translated-page-fit-v1":
        raise AssertionError("文档级排版算法版本未记录")
    if typography_choice["leading_ratio"] != 1.8:
        raise AssertionError("默认策略应先保持优选行距，再计算最大统一字号")
    if typography_choice["densest_page"] != 2:
        raise AssertionError("应根据每页实际译文字量识别最密页")
    if typography_choice["total_translated_chars"] != 940:
        raise AssertionError("文档级排版报告应记录参与计算的实际译文字数")
    if len(typography_choice["page_measurements"]) != 2:
        raise AssertionError("选定字号必须保留全部普通正文页的实测结果")


def test_reference_heading_and_citation_block_detection() -> None:
    """参考文献标题与紧凑引文块要能认出来，普通正文不得误判。"""

    for heading in (
        "REFERENCES",
        "Bibliography",
        "LITERATURE CITED",
        "Works Cited",
        "参考文献",
    ):
        if not _has_reference_heading(heading):
            raise AssertionError(f"未识别参考文献标题: {heading}")
    if _has_reference_heading("This paragraph cites the literature."):
        raise AssertionError("普通正文不应被识别为参考文献标题")
    compact_citations = (
        "Adler JM, Lodi-Smith J. 2016. Narrative identity and well-being.\n"
        "Lamport L. 1978. Time, clocks, and event ordering."
    )
    if not _has_source_citation_block(compact_citations):
        raise AssertionError("作者缩写加裸年份的连续题录应被识别")
    if _has_source_citation_block(
        "The study began in 2016.\nThe second wave followed in 2020."
    ):
        raise AssertionError("普通含年份正文不应被识别为连续题录")


def test_regions_for_page_selection() -> None:
    """按页取保留区域，只能取到属于这一页的。"""

    region_probe = [
        {"pages": [2, 4], "bbox": [0, 0, 10, 10]},
        {"page": 3, "bbox": [0, 0, 10, 10]},
    ]
    if len(_regions_for_page(region_probe, 2)) != 1:
        raise AssertionError("批量 pages 区域选择器应作用于对应页面")
    if len(_regions_for_page(region_probe, 3)) != 1:
        raise AssertionError("单页 page 区域选择器应继续有效")
    if _regions_for_page(region_probe, 1):
        raise AssertionError("区域选择器不应泄漏到未声明页面")


def test_horizontal_width_change_and_body_collapse() -> None:
    """正文宽度变化什么时候算合理，什么时候算塌缩。"""

    if not _horizontal_width_change_justified(
        {
            "page_overrides": [
                {
                    "page": 2,
                    "horizontal_width_change_justified": True,
                    "reason": "任务明确批准新版式。",
                }
            ]
        },
        2,
    ):
        raise AssertionError("有理由的横向版心变更应被识别")
    if _horizontal_width_change_justified(
        {
            "page_overrides": [
                {
                    "page": 2,
                    "horizontal_width_change_justified": True,
                    "reason": "",
                }
            ]
        },
        2,
    ):
        raise AssertionError("无理由的横向版心变更不得被识别为例外")
    if not _body_width_collapsed(0.72, 0.38, 0.72, 0.12):
        raise AssertionError("原文通栏被压成窄栏时应被横向版心门禁阻断")
    if _body_width_collapsed(0.72, 0.58, 0.72, 0.12):
        raise AssertionError("保留大部分原文版心宽度时不应误报")
    if _body_width_collapsed(0.36, 0.28, 0.72, 0.12):
        raise AssertionError("小幅双栏宽度变化不应被绝对差值门槛误报")


def test_substantive_body_prose_and_bottom_whitespace() -> None:
    """哪些单元算实质正文；页底留白什么时候算失衡。"""

    if _unit_is_substantive_body_prose(
        {
            "kind": "body",
            "source": (
                "Mindfulness Broadens Awareness and Builds Eudaimonic "
                "Meaning: A Process Model"
            ),
        }
    ):
        raise AssertionError("封面题名或元数据值不应被当成普通正文")
    if not _unit_is_substantive_body_prose(
        {
            "kind": "body",
            "source": (
                "This study examines how people construct meaning after "
                "loss and explains why the process changes across social "
                "contexts, while preserving uncertainty about causality."
            ),
        }
    ):
        raise AssertionError("完整论述段落必须继续进入正文排版门禁")
    if not _bottom_whitespace_is_unbalanced(0.30, 0.38, 0.08):
        raise AssertionError("上挤下空且相对原文差异显著时应被阻断")
    if _bottom_whitespace_is_unbalanced(0.30, 0.34, 0.15):
        raise AssertionError("上下相对平衡的天然短页不应仅因底部差值被阻断")


def test_orphan_single_han_lines() -> None:
    """孤字行判定：只剩一个汉字才算，带标点或换了位置都不算。"""

    fake_text_dict = {
        "blocks": [
            {
                "type": 0,
                "lines": [
                    {
                        "spans": [
                            {
                                "text": "这是一个长度足够的中文标题续行测试",
                                "bbox": [42, 60, 300, 70],
                                "size": 9.0,
                            }
                        ]
                    },
                    {
                        "spans": [
                            {
                                "text": "例",
                                "bbox": [42, 74, 51, 84],
                                "size": 9.0,
                            }
                        ]
                    },
                ],
            }
        ]
    }
    fake_body_spans = [
        span
        for block in fake_text_dict["blocks"]
        for line in block["lines"]
        for span in line["spans"]
    ]
    if not _orphan_single_han_lines(fake_text_dict, fake_body_spans):
        raise AssertionError("紧跟长行的单个汉字续行应被识别")
    fake_text_dict["blocks"][0]["lines"][1]["spans"][0]["text"] = "例。"
    fake_body_spans[-1]["text"] = "例。"
    if not _orphan_single_han_lines(fake_text_dict, fake_body_spans):
        raise AssertionError("单个汉字加闭合标点的续行也应被识别")
    fake_text_dict["blocks"][0]["lines"][1]["spans"][0]["text"] = "例"
    fake_body_spans[-1]["text"] = "例"
    fake_text_dict["blocks"][0]["lines"][1]["spans"][0]["bbox"] = [
        42,
        110,
        51,
        120,
    ]
    if _orphan_single_han_lines(fake_text_dict, fake_body_spans):
        raise AssertionError("具有充分章节间距的单字标题不应被误报")


def test_interline_gap_outliers() -> None:
    """行间距异常要报出来，但中间插入内容后就不再是异常。"""

    gap_probe = {
        "blocks": [
            {
                "type": 0,
                "lines": [
                    {
                        "spans": [
                            {
                                "text": "第一段正文结束。",
                                "bbox": [42, 60, 180, 70],
                                "size": 10.0,
                            }
                        ]
                    }
                ],
            },
            {
                "type": 0,
                "lines": [
                    {
                        "spans": [
                            {
                                "text": "第二段正文开始。",
                                "bbox": [42, 150, 180, 160],
                                "size": 10.0,
                            }
                        ]
                    }
                ],
            },
        ]
    }
    gap_spans = [
        span
        for block in gap_probe["blocks"]
        for line in block["lines"]
        for span in line["spans"]
    ]
    gap_hits = _interline_gap_outliers(gap_probe, gap_spans, 10.0)
    if not gap_hits or gap_hits[0]["gap_to_font_ratio"] < 8:
        raise AssertionError("超大段间距应被识别为段距膨胀风险")
    gap_probe["blocks"].insert(
        1,
        {
            "type": 0,
            "lines": [
                {
                    "spans": [
                        {
                            "text": "保留的参考文献题录占据此区域。",
                            "bbox": [42, 95, 220, 105],
                            "size": 8.0,
                        }
                    ]
                }
            ],
        },
    )
    if _interline_gap_outliers(gap_probe, gap_spans, 10.0):
        raise AssertionError("段落之间已有可见内容时不得误报为空白段距")


def test_paragraph_gap_inflation_justified() -> None:
    """段间距被撑大，什么情况下有正当理由。"""

    if not _paragraph_gap_inflation_justified(
        {
            "page_overrides": [
                {
                    "page": 2,
                    "paragraph_gap_inflation_justified": True,
                    "reason": "特殊表单分区。",
                }
            ]
        },
        2,
    ):
        raise AssertionError("有明确理由的特殊页面段距例外应被识别")
    if _paragraph_gap_inflation_justified(
        {
            "page_overrides": [
                {
                    "page": 2,
                    "paragraph_gap_inflation_justified": True,
                    "reason": "",
                }
            ]
        },
        2,
    ):
        raise AssertionError("无理由的段距膨胀不得被识别为例外")


def test_document_typography_locked() -> None:
    """全文排版参数一旦锁定，判断口径必须稳定。"""

    if not _document_typography_locked(
        {
            "document_typography": {
                "selection_method": "densest-page-fit",
                "all_body_pages_locked": True,
                "body_font_pt": 10.8,
                "leading_ratio": 1.7,
                "paragraph_spacing_policy": "natural",
                "reason": "以最密页试排冻结全篇。",
            }
        }
    ):
        raise AssertionError("完整的文档级字号锁定记录应被识别")
    if not _document_typography_locked(
        {
            "document_typography": {
                "selection_method": "densest-page-fit",
                "font_locked_across_document": True,
                "body_font_pt": 10.0,
                "body_leading": 1.8,
                "paragraph_space_em": 0.62,
                "reason": "旧作业字段已记录全篇统一排版。",
            }
        }
    ):
        raise AssertionError("旧作业的等价字号与段距字段应被识别")
    if _document_typography_locked(
        {
            "document_typography": {
                "selection_method": "densest-page-fit",
                "all_body_pages_locked": True,
                "body_font_pt": 10.8,
                "leading_ratio": 1.7,
                "paragraph_spacing_policy": "natural",
                "reason": "",
            }
        }
    ):
        raise AssertionError("无理由的文档级字号声明不得改变留白门禁")
