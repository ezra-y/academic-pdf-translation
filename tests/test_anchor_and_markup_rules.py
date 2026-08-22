"""锚点归一化、字体安全标记、跨页续段：三条纯文本层面的判定规则。

三支都不碰文件系统，只判断字符串与行片段，跑起来是毫秒级，
所以合成一个文件。统计量锚点、可嵌入字符的标记转义、
上一页末尾是否续到下一页，属于同一层的文本判定。
断言逐字保留原样，中文说明本身就是判据文档。

单独运行：
    python3 -m pytest -q tests/test_anchor_and_markup_rules.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from audit_translation_completeness import (  # noqa: E402
    _candidate_stage_has_current_pdf,
    _remove_percent_marker_only_mismatches,
    _unit_compression_flags,
)
from build_candidate import (  # noqa: E402
    _column_widths,
    _is_cross_page_continuation,
    _markup,
    _source_ends_paragraph,
)
from content_anchors import (  # noqa: E402
    anchors_present,
    present_acronyms,
    required_anchors,
)
from content_anchors import (
    statistics as content_statistics,
)
from qa_pdf import _low_table_spans  # noqa: E402
from retained_source import _clean_block_text  # noqa: E402
from review_risk_report import _year_present  # noqa: E402


def test_statistical_anchor_normalization() -> None:
    values = content_statistics(
        "Prevalence ranged from 1.8-25.4% and the interval was 2.2–36.4%."
    )
    expected = {"1.8%", "25.4%", "2.2%", "36.4%"}
    if values != expected:
        raise AssertionError(
            f"百分比区间两端必须采用同一单位: {values}"
        )
    equivalent_decimals = content_statistics(
        "alpha=0.70; eta=.07; effect=− .38; p=0.001"
    )
    if equivalent_decimals != {"0.7", "0.07", "-0.38", "0.001"}:
        raise AssertionError(
            "统计锚点必须统一前导零、尾随零和负号后的空格"
        )
    split_url_anchors = required_anchors(
        "See https://\u200bdoi.\u200borg/10.1000/test and "
        "http://\u200bcreat\u200biveco\u200bmmons.org/licenses/by/4.0/."
    )
    if split_url_anchors["urls"] != [
        "http://creativecommons.org/licenses/by/4.0/",
        "https://doi.org/10.1000/test",
    ]:
        raise AssertionError("PDF零宽断行符不得破坏URL锚点")
    wrapped_link_missing = anchors_present(
        required_anchors(
            "https://doi.org/10.1007/s10902-022-00585-4"
        ),
        "https://doi.org/10.1007/s10902-\n022-00585-4",
    )
    if wrapped_link_missing["urls"] or wrapped_link_missing["dois"]:
        raise AssertionError("链接仅因换行产生空白时仍应视为同一检索锚点")
    adjacent_acronyms = present_acronyms(
        "FDI-24各分量表与MLQ得分、SBQ-R得分均已报告。"
    )
    if not {"FDI-24", "MLQ", "SBQ-R"}.issubset(adjacent_acronyms):
        raise AssertionError("紧贴中文字符的正式缩写必须被审计器识别")
    if not _year_present("2019", "收稿：2019年8月30日"):
        raise AssertionError("紧贴中文字符的年份必须被风险报告识别")
    if not _year_present("1980s", "自20世纪80年代以来"):
        raise AssertionError("英文年代与中文世纪年代写法必须视为等值")
    if not _year_present("1980’s", "自20世纪80年代以来"):
        raise AssertionError("带弯引号的英文年代必须识别等值中文年代")
    if _year_present("2016", "发表于较近时期"):
        raise AssertionError("未出现的年份不得被误判为已保留")
    retained_link = _clean_block_text(
        "https://\u200bdoi. \u200borg/ 10. 1000/test\n1 3\n"
    )
    if retained_link != "https://doi.org/10.1000/test":
        raise AssertionError(
            "保留题录必须清除零宽断行符和独立页脚标记"
        )
    if _clean_block_text("artifi\u00ad cial intelli\u00ad gence") != (
        "artificial intelligence"
    ):
        raise AssertionError("软连字符换行必须恢复为完整单词")
    retained = _remove_percent_marker_only_mismatches(
        {"0.243%", "0.575%", "0.831%"},
        "表中 P 值依次为 0.243、0.575，另一个值未提供。",
    )
    if retained != {"0.831%"}:
        raise AssertionError(
            "结构化表格只应豁免数值已出现的百分号文字层错配"
        )
    if _candidate_stage_has_current_pdf("translated"):
        raise AssertionError("重译中的作业不得把旧候选用于完整性比对")
    if not _candidate_stage_has_current_pdf("candidate"):
        raise AssertionError("候选阶段必须读取当前候选进行完整性比对")
    if _unit_compression_flags("heading", 46, 0.17, 0.2, 0.25):
        raise AssertionError("短标题不得按正文译源字量比阻断")
    if _unit_compression_flags("metadata", 80, 0.15, 0.2, 0.25):
        raise AssertionError("元数据的简洁本地化不得误判为摘要化")
    if _unit_compression_flags("body", 160, 0.1, 0.2, 0.25) != [
        "SEVERE_TRANSLATION_COMPRESSION"
    ]:
        raise AssertionError("真正大幅压缩的正文仍必须被完整性审计阻断")


def test_font_safe_markup() -> None:
    markup = _markup("脚注¹⁰、ᵃ与 R²；^³ 保持显式脱字符")
    if "<super>10</super>" not in markup or "<super>2</super>" not in markup:
        raise AssertionError("独立上标数字未转换为字体安全的上标标记")
    if "<super>a</super>" not in markup:
        raise AssertionError("上标字母未转换为字体安全的上标标记")
    if "^3" not in markup:
        raise AssertionError("显式脱字符后的上标数字应规范为普通数字")

    reference_markup = _markup(
        "State Council. 国务院政策题名",
        cjk_font="AcademicUnifiedRegular",
    )
    if (
        '<font name="AcademicUnifiedRegular">国务院政策题名</font>'
        not in reference_markup
    ):
        raise AssertionError("混合文字参考文献未为 CJK 字符选择回退字体")
    table_regions = [{"bbox": [0.0, 0.0, 100.0, 100.0]}]
    if _low_table_spans(
        [
            {
                "text": "12",
                "size": 6.56,
                "flags": 1,
                "bbox": [10.0, 10.0, 16.0, 17.0],
            }
        ],
        table_regions,
        7.0,
    ):
        raise AssertionError("表格上标引文号不得误报为表格正文字号过小")
    if not _low_table_spans(
        [
            {
                "text": "正文",
                "size": 6.56,
                "flags": 0,
                "bbox": [10.0, 10.0, 26.0, 17.0],
            }
        ],
        table_regions,
        7.0,
    ):
        raise AssertionError("真正过小的表格正文仍必须被检查器拦截")
    widths = _column_widths(
        [
            [
                "时间范围与程度",
                "较长的诊断标准说明文字用于测试",
                "另一列较长说明文字用于测试",
            ]
        ],
        500.0,
    )
    if widths[0] < 70.0:
        raise AssertionError("少列表格的短标签列必须保留可读宽度")
    weighted_widths = _column_widths(
        [["long model name", "Dev", "Test"]],
        300.0,
        [4.0, 1.0, 1.0],
    )
    if weighted_widths != [200.0, 50.0, 50.0]:
        raise AssertionError("宽表显式列宽权重必须按比例生效")


def test_cross_page_continuation_detection() -> None:
    previous = {
        "id": "p0006-u0010",
        "page": 6,
        "kind": "body",
        "source_bbox": [51.0, 466.0, 391.0, 529.0],
        "source": "Newer products are",
        "translation": "这些新产品被",
    }
    following = {
        "id": "p0007-u0003",
        "page": 7,
        "kind": "body",
        "source_bbox": [51.0, 55.0, 391.0, 154.0],
        "source": "designed for intimate relationships.",
        "translation": "设计用于亲密关系。",
    }
    if not _is_cross_page_continuation(
        previous,
        following,
        previous_page_width=442.0,
        previous_page_height=612.0,
        following_page_width=442.0,
        following_page_height=612.0,
    ):
        raise AssertionError("页尾未完句与下一页页首先续句应合并")
    wrapped_previous = {
        **previous,
        "source_bbox": [317.0, 691.0, 535.0, 719.0],
    }
    wrapped_following = {
        **following,
        "source_bbox": [62.0, 54.0, 280.0, 82.0],
    }
    if not _is_cross_page_continuation(
        wrapped_previous,
        wrapped_following,
        previous_page_width=598.0,
        previous_page_height=792.0,
        following_page_width=598.0,
        following_page_height=792.0,
    ):
        raise AssertionError("双栏页尾右栏到下一页左栏的续句应合并")
    if not _source_ends_paragraph("a real human person.6"):
        raise AssertionError("句末脚注编号不得把完整句误判为跨页续句")
    completed = {**previous, "source": "A complete sentence."}
    if _is_cross_page_continuation(
        completed,
        following,
        previous_page_width=442.0,
        previous_page_height=612.0,
        following_page_width=442.0,
        following_page_height=612.0,
    ):
        raise AssertionError("完整句不得与下一页正文错误合并")
