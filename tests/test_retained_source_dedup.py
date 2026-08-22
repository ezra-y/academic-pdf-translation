"""保留原文的去重规则：同一块内容不能既被保留、又被译文重排一遍。

这一支覆盖单元与保留区域的覆盖关系、复杂内容替换掉哪些单元、
页眉页脚这类非语义家具怎么识别、参考文献块的顺序约束。
断言逐字保留原样，中文说明本身就是判据文档。

单独运行：
    python3 -m pytest -q tests/test_retained_source_dedup.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _common import (  # noqa: E402
    complex_payload_replaced_unit_ids,
    complex_payload_replaces_unit,
    is_nonsemantic_source_furniture_unit,
    remove_suppressed_texts,
)
from audit_translation_completeness import _heading_expectations  # noqa: E402
from build_candidate import (  # noqa: E402
    _edge_label_lines,
    _markup,
    _retained_references_precede_visible_units,
    _unit_fully_covered_by_retained,
)
from review_risk_report import _running_values  # noqa: E402


def test_retained_source_unit_deduplication() -> None:
    unit = {
        "id": "p0016-u0011",
        "page": 16,
        "source_bbox": [35.7, 475.5, 560.8, 772.2],
        "keep_source_reason": "参考文献题录保留原文。",
    }
    retained = [
        {
            "bbox": [35.7, 475.5, 560.8, 772.2],
            "blocks": [{"text": "Reference entry"}],
            "already_present_in_translation": False,
        }
    ]
    if not _unit_fully_covered_by_retained(unit, retained):
        raise AssertionError("坐标保留区完整覆盖的原文单元不得重复渲染")
    retained[0]["already_present_in_translation"] = True
    if _unit_fully_covered_by_retained(unit, retained):
        raise AssertionError("保留区不再渲染时不得同时抑制原文单元")
    if not is_nonsemantic_source_furniture_unit(
        {
            "source": "1 3",
            "keep_source_reason": "原刊页码与版式标记",
        }
    ):
        raise AssertionError("原刊孤立页码不得进入连续阅读版正文")
    if is_nonsemantic_source_furniture_unit(
        {
            "source": "13 participants completed the study.",
            "keep_source_reason": "样本量按原文保留",
        }
    ):
        raise AssertionError("正文统计数字不得被误判为原刊页码")
    if not is_nonsemantic_source_furniture_unit(
        {
            "source": "Repeated publication watermark",
            "source_bbox": [18.2, 75.9, 34.7, 680.1],
            "keep_source_reason": "旋转页边出版标记，不承载论文论证。",
        }
    ):
        raise AssertionError("极窄且纵向贯穿页面的旋转附属物不得进入正文")
    if is_nonsemantic_source_furniture_unit(
        {
            "source": "Official instrument name",
            "source_bbox": [100.0, 100.0, 260.0, 125.0],
            "keep_source_reason": "正式量表名称按原文保留。",
        }
    ):
        raise AssertionError("普通横排正式名称不得被误判为旋转页边附属物")
    if not is_nonsemantic_source_furniture_unit(
        {
            "source": "17",
            "source_bbox": [300.6, 742.3, 310.6, 752.3],
            "keep_source_reason": "原文页码作为定位标识保留。",
        },
        page_width=612.0,
        page_height=792.0,
    ):
        raise AssertionError("页边紧凑的纯页码不得进入连续阅读版正文")
    if is_nonsemantic_source_furniture_unit(
        {
            "source": "17",
            "source_bbox": [300.6, 360.0, 310.6, 370.0],
            "keep_source_reason": "量表分值按原文保留。",
        },
        page_width=612.0,
        page_height=792.0,
    ):
        raise AssertionError("正文区域的独立数值不得被误判为页码")
    if not is_nonsemantic_source_furniture_unit(
        {
            "source": "14",
            "source_bbox": [533.0, 755.3, 541.1, 769.4],
            "keep_source_reason": "原稿页码保留原文，用于源译定位。",
        },
        page_width=595.3,
        page_height=841.9,
    ):
        raise AssertionError("宽页脚带中的紧凑页码也不得进入正文")
    if not is_nonsemantic_source_furniture_unit(
        {
            "source": "38",
            "source_bbox": [121.8, 780.9, 541.1, 794.9],
            "keep_source_reason": "原稿页码保留原文，用于源译定位。",
        },
        page_width=595.3,
        page_height=841.9,
    ):
        raise AssertionError("坐标框异常宽的页边纯页码仍应被识别")
    if not is_nonsemantic_source_furniture_unit(
        {
            "source": (
                "931914 HEA0010.1177/1363459320931914"
                "HealthPearce research-article2020"
            ),
            "source_bbox": [4.2, -3.2, 61.9, 7.3],
            "keep_source_reason": "出版制作元数据保留原文。",
        },
        page_width=612.0,
        page_height=792.0,
    ):
        raise AssertionError("完全位于页边的紧凑出版元数据不得进入正文")
    mixed_page_retained = [
        {
            "bbox": [306.0, 55.0, 546.0, 426.6],
            "category": "references",
            "blocks": [{"text": "Final reference."}],
            "already_present_in_translation": False,
        }
    ]
    trailing_declaration = {
        "id": "p0017-u0014",
        "page": 17,
        "source_bbox": [306.0, 435.0, 544.0, 456.5],
        "translation": "出版者声明。",
    }
    if not _retained_references_precede_visible_units(
        [trailing_declaration],
        mixed_page_retained,
        [],
    ):
        raise AssertionError("参考文献下方的已译声明必须排在题录之后")
    leading_declaration = {
        **trailing_declaration,
        "source_bbox": [306.0, 25.0, 544.0, 45.0],
    }
    if _retained_references_precede_visible_units(
        [leading_declaration],
        mixed_page_retained,
        [],
    ):
        raise AssertionError("参考文献上方的已译内容不得被移到题录之后")
    complex_item = {
        "page": 10,
        "status": "ready",
        "payload": {
            "tables": [
                {
                    "title": "表2 死亡技术伦理",
                    "rows": [
                        ["原则", "（未来）逝者"],
                        ["控制", "生者应如何同意数字遗存被使用？"],
                    ],
                }
            ]
        },
    }
    if not complex_payload_replaces_unit(
        {
            "page": 10,
            "translation": (
                "表2 死亡技术伦理。原则：（未来）逝者。"
                "控制：生者应如何同意数字遗存被使用？"
            ),
        },
        [complex_item],
    ):
        raise AssertionError("结构化载荷已完整承载的原始表格单元不得重复输出")
    if complex_payload_replaces_unit(
        {
            "page": 10,
            "translation": "正文继续讨论问责、治理和市场风险。",
        },
        [complex_item],
    ):
        raise AssertionError("同页普通正文不得因存在结构化表格而被抑制")
    fragmented_table_units = [
        {
            "id": "p0010-u0001",
            "page": 10,
            "kind": "heading",
            "translation": "参与者",
        },
        {
            "id": "p0010-u0002",
            "page": 10,
            "kind": "table-or-caption",
            "translation": "表2 死亡技术伦理",
        },
        {
            "id": "p0010-u0003",
            "page": 10,
            "kind": "body",
            "translation": "原则 （未来）逝者",
        },
        {
            "id": "p0010-u0004",
            "page": 10,
            "kind": "body",
            "translation": "控制 生者应如何同意数字遗存被使用？",
        },
        {
            "id": "p0010-u0005",
            "page": 10,
            "kind": "heading",
            "translation": "β",
        },
    ]
    fragmented_replaced = complex_payload_replaced_unit_ids(
        fragmented_table_units,
        [complex_item],
    )
    if "p0010-u0001" in fragmented_replaced:
        raise AssertionError("表格前的普通短标题不得被复杂载荷抑制")
    if not {
        "p0010-u0002",
        "p0010-u0003",
        "p0010-u0004",
    }.issubset(fragmented_replaced):
        raise AssertionError("连续碎表头和短表格行必须作为一组被复杂载荷替代")
    if "p0010-u0005" in fragmented_replaced:
        raise AssertionError("载荷中不存在的孤立短单位不得被连带抑制")
    cross_page_item = {
        "page": 10,
        "status": "ready",
        "method": "structured-table-rebuild",
        "payload": {
            "tables": [
                {
                    "source_pages": [10, 11],
                    "rows": [
                        ["开发者", "终端用户", "其他利益相关者"],
                        [
                            "演变功能如何影响他们对信任与可信赖性的理解",
                            "相关伦理关切",
                            "",
                        ],
                    ],
                }
            ]
        },
    }
    cross_page_replaced = complex_payload_replaced_unit_ids(
        [
            {
                "id": "p0011-u0001",
                "page": 11,
                "kind": "body",
                "translation": "开发者　终端用户　其他利益相关者",
            },
            {
                "id": "p0011-u0002",
                "page": 11,
                "kind": "body",
                "translation": (
                    "演变功能如何影响他们对信任与可信赖性的理解"
                    "及相关伦理关切"
                ),
            },
            {
                "id": "p0011-u0003",
                "page": 11,
                "kind": "body",
                "translation": "随后进入普通分析正文。",
            },
        ],
        [cross_page_item],
    )
    if not {
        "p0011-u0001",
        "p0011-u0002",
    }.issubset(cross_page_replaced):
        raise AssertionError("跨页结构化表格覆盖的续页碎片不得重复输出")
    if "p0011-u0003" in cross_page_replaced:
        raise AssertionError("跨页表格后的普通正文不得被连带抑制")
    coordinate_item = {
        "page": 10,
        "status": "ready",
        "method": "structured-table-rebuild",
        "payload": {
            "tables": [
                {
                    "source_bboxes": [
                        {
                            "page": 11,
                            "bbox": [10, 20, 180, 220],
                        }
                    ],
                    "rows": [
                        ["开发者", "终端用户"],
                        ["完整的结构化表格内容", "相关伦理关切"],
                    ],
                }
            ]
        },
    }
    coordinate_replaced = complex_payload_replaced_unit_ids(
        [
            {
                "id": "p0011-u-coordinate-fragment",
                "page": 11,
                "kind": "body",
                "source_bbox": [20, 150, 70, 170],
                "translation": "他们用于",
            },
            {
                "id": "p0011-u-coordinate-body",
                "page": 11,
                "kind": "body",
                "source_bbox": [20, 240, 170, 300],
                "translation": "2.3　分析方法正文。",
            },
        ],
        [coordinate_item],
    )
    if "p0011-u-coordinate-fragment" not in coordinate_replaced:
        raise AssertionError("结构化载荷坐标内的短碎片必须被替代")
    if "p0011-u-coordinate-body" in coordinate_replaced:
        raise AssertionError("结构化载荷坐标外的正文不得被抑制")
    later_table_item = {
        "page": 11,
        "status": "ready",
        "method": "structured-table-rebuild",
        "payload": {
            "insert_before_unit_id": "p0011-u0005",
            "tables": [
                {
                    "rows": [
                        ["类别", "年龄", "族裔", "性别", "残障情况"],
                        [
                            "终端用户",
                            "18–29岁",
                            "英国白人",
                            "顺性别男性",
                            "否",
                        ],
                    ]
                }
            ],
        },
    }
    adjacent_complex_units = [
        {
            "id": "p0011-u0001",
            "page": 11,
            "kind": "body",
            "translation": "开发者　终端用户　其他利益相关者",
        },
        {
            "id": "p0011-u0002",
            "page": 11,
            "kind": "body",
            "translation": (
                "演变功能如何影响他们对信任与可信赖性的理解"
                "及相关伦理关切"
            ),
        },
        {
            "id": "p0011-u0003",
            "page": 11,
            "kind": "heading",
            "translation": "2.3　分析",
        },
        {
            "id": "p0011-u0004",
            "page": 11,
            "kind": "body",
            "translation": "数据分析采用归纳方法并开展多轮编码。",
        },
        {
            "id": "p0011-u0005",
            "page": 11,
            "kind": "table-or-caption",
            "translation": "类别　年龄　族裔　性别　残障情况",
        },
        {
            "id": "p0011-u0006",
            "page": 11,
            "kind": "body",
            "translation": "终端用户　18–29岁　英国白人　顺性别男性　否",
        },
    ]
    adjacent_replaced = complex_payload_replaced_unit_ids(
        adjacent_complex_units,
        [cross_page_item, later_table_item],
    )
    if {
        "p0011-u0003",
        "p0011-u0004",
    } & adjacent_replaced:
        raise AssertionError(
            "同页两张复杂表格不得吞掉夹在中间的普通正文"
        )
    if not {
        "p0011-u0001",
        "p0011-u0002",
        "p0011-u0005",
        "p0011-u0006",
    }.issubset(adjacent_replaced):
        raise AssertionError("相邻复杂表格必须分别抑制自己的原始碎片")
    short_header_item = {
        "page": 35,
        "status": "ready",
        "method": "structured-table-rebuild",
        "payload": {
            "insert_before_unit_id": "p0035-u0003",
            "tables": [
                {
                    "title": "表3 各年级生命故事评分分布",
                    "rows": [
                        ["生命故事连贯性", "三年级", "五年级"],
                        ["", "N=27", "N=32"],
                        ["", "过去", "未来"],
                        ["单一事件", "37.5", "20.8"],
                        [
                            "多个事件，按时间顺序组织",
                            "41.7",
                            "58.3",
                            "53.6",
                            "50.0",
                        ],
                    ],
                }
            ],
        },
    }
    short_header_units = [
        {
            "id": "p0035-u0001",
            "page": 35,
            "kind": "body",
            "translation": "表格前的出版说明。",
        },
        {
            "id": "p0035-u0002",
            "page": 35,
            "kind": "body",
            "translation": (
                "正文先讨论表3各年级生命故事评分分布，"
                "包括三年级五年级以及过去未来。"
            ),
        },
        {
            "id": "p0035-u0003",
            "page": 35,
            "kind": "table-or-caption",
            "translation": "表3",
        },
        {
            "id": "p0035-u0004",
            "page": 35,
            "kind": "body",
            "translation": "各年级生命故事评分分布",
        },
        {
            "id": "p0035-u0005",
            "page": 35,
            "kind": "body",
            "translation": "—",
        },
        {
            "id": "p0035-u0006",
            "page": 35,
            "kind": "body",
            "translation": "三年级 五年级",
        },
        {
            "id": "p0035-u0007",
            "page": 35,
            "kind": "body",
            "translation": "N=27 N=32",
        },
        {
            "id": "p0035-u0008",
            "page": 35,
            "kind": "body",
            "translation": "—",
        },
        {
            "id": "p0035-u0009",
            "page": 35,
            "kind": "body",
            "translation": "过去 未来",
        },
        {
            "id": "p0035-u0010",
            "page": 35,
            "kind": "body",
            "translation": "单一事件 37.5 20.8",
        },
        {
            "id": "p0035-u0011",
            "page": 35,
            "kind": "body",
            "translation": (
                "多个事件，按时间顺序组织 "
                "41.7 58.3 53.6 50.0"
            ),
        },
    ]
    short_header_replaced = complex_payload_replaced_unit_ids(
        short_header_units,
        [short_header_item],
    )
    if not {
        "p0035-u0003",
        "p0035-u0004",
        "p0035-u0005",
        "p0035-u0006",
        "p0035-u0007",
        "p0035-u0008",
        "p0035-u0009",
        "p0035-u0010",
        "p0035-u0011",
    }.issubset(short_header_replaced):
        raise AssertionError(
            "结构化表格锚点范围内的短表头不得在重建后重复输出"
        )
    if {"p0035-u0001", "p0035-u0002"} & short_header_replaced:
        raise AssertionError(
            "复杂表格锚点前的普通正文即使复用表中术语也不得被抑制"
        )
    table_header = "SE β b SE β b SE β b"
    if remove_suppressed_texts(
        table_header,
        ["b", table_header],
    ):
        raise AssertionError("复杂页长文本必须先于其短子串执行抑制")
    semantic_heading = (
        "2.6 AI 聊天机器人使用与心理福祉之间的关系，"
        "与用户的线下社会支持相关"
    )
    if remove_suppressed_texts(
        semantic_heading,
        ["聊天机器人", "支持", "影响"],
    ) != semantic_heading:
        raise AssertionError("复杂图短标签不得从普通标题或正文中全局删除")
    long_table_text = (
        "表1 变量相关矩阵\n"
        "变量一与变量二呈显著正相关，变量二与结果变量呈显著负相关。"
        "样本量为684，所有检验均为双尾检验，并报告完整置信区间。"
    )
    following_discussion = (
        "本研究进一步讨论上述关系在不同社会支持水平下的边界条件。"
    )
    if remove_suppressed_texts(
        f"{long_table_text}\n\n{following_discussion}",
        [long_table_text],
    ) != following_discussion:
        raise AssertionError("段落边界上的长表格文本必须去重并保留后续正文")
    embedded_long_text = (
        "作者在正文中引用表1 变量相关矩阵，变量一与变量二呈显著正相关，"
        "变量二与结果变量呈显著负相关，并据此讨论机制。"
    )
    if remove_suppressed_texts(
        embedded_long_text,
        [long_table_text],
    ) != embedded_long_text:
        raise AssertionError("正文句内相似内容不得按复杂载荷整块删除")
    if _running_values(
        {
            "10.1234/article": {1, 2, 3, 4, 5},
            "10.1234/body-link": {3},
        },
        7,
    ) != {"10.1234/article"}:
        raise AssertionError("跨多数正文页重复的期刊链接应与单次正文链接区分")
    if _edge_label_lines("直接效应：.331***\n间接效应：.077**") != [
        "直接效应：.331***",
        "间接效应：.077**",
    ]:
        raise AssertionError("模型图多行边标签必须拆成可独立绘制的文本行")
    if "\u02d2" in _markup("Chui-Shan Yung²˒⁴"):
        raise AssertionError("罕见上标分隔符必须转换为字体可检索字符")
    if _heading_expectations(
        [
            {
                "id": "p0008-u0001",
                "page": 8,
                "kind": "heading",
                "source": "Dependent Variable: Well-being",
                "translation": "因变量：心理福祉",
            }
        ],
        [
            {
                "page": 8,
                "status": "ready",
                "payload": {
                    "tables": [
                        {
                            "rows": [["因变量：心理福祉"]],
                        }
                    ]
                },
            }
        ],
    ):
        raise AssertionError("已由结构化复杂载荷承载的表头不得误报标题丢失")
