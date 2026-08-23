"""中文标记与流式排版：标点禁则、转义、换行、装箱与溢出。

这些断言原本是 scripts/self_test.py 的 run() 里一长串内联判断，没有名字，
失败时只能看堆栈行号。现在按主题切成命名用例，每条可以单独运行。
断言逐字保留原样，中文说明本身就是判据文档。

单独运行：
    python3 -m pytest -q tests/test_cjk_markup_and_flow.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_candidate import _markup  # noqa: E402
from cjk_markup import (  # noqa: E402
    install_reportlab_cjk_nobr_patch,
    reportlab_cjk_markup,
)
from qa_pdf import SOURCE_MAPPING_LABEL_PATTERN  # noqa: E402
from reportlab_layout import (  # noqa: E402
    FlowItem,
    layout_flow,
    make_cjk_style,
)


def test_source_mapping_label_pattern_matches_debug_label() -> None:
    """原文页映射标签必须能被识别出来，才能从正文指标里排除。"""

    if not SOURCE_MAPPING_LABEL_PATTERN.fullmatch("原文第 18 页"):
        raise AssertionError("源页映射标签必须可从正文指标中识别并排除")


def test_cjk_markup_kinsoku_and_escaping() -> None:
    """中文禁则标记：不插不可见字符、标点成组、XML 正确转义、统计 token 不拆。"""

    punctuation_markup = reportlab_cjk_markup(
        "正文结束。”下一句（说明）\n第二行 & <标签>"
    )
    if "&#8288;" in punctuation_markup or "\u2060" in punctuation_markup:
        raise AssertionError("中文禁则标记不得插入不可见 Unicode 连接符")
    if "<nobr>束。”</nobr>" not in punctuation_markup:
        raise AssertionError("闭合标点必须与前一字符组成不可拆分短组")
    if "<nobr>（说</nobr>" not in punctuation_markup:
        raise AssertionError("开放标点必须与后一字符组成不可拆分短组")
    if "&amp;" not in punctuation_markup or "&lt;标签&gt;" not in punctuation_markup:
        raise AssertionError("中文禁则标记仍须正确转义 ReportLab XML 文本")
    if "<br/>" not in punctuation_markup:
        raise AssertionError("中文禁则标记必须保留显式换行")
    safe_hyphen_markup = _markup("Pfeifer‐Chomiczewska")
    if "\u2010" in safe_hyphen_markup or "Pfeifer-Chomiczewska" not in (
        safe_hyphen_markup
    ):
        raise AssertionError("PDF 不支持的连字符必须转换为可检索 ASCII 连字符")
    statistical_markup = reportlab_cjk_markup(
        ".02 -.32*** 95% 1.55 **p"
    )
    for token in (".02", "-.32***", "95%", "1.55", "**p"):
        if f"<nobr>{token}</nobr>" not in statistical_markup:
            raise AssertionError(
                f"统计 token 必须作为不可拆分短组: {token}"
            )


def test_reportlab_line_breaking_avoids_stray_punctuation() -> None:
    """打上补丁的 ReportLab 真正分行后，不得出现行首闭合标点或行末开放标点。"""

    install_reportlab_cjk_nobr_patch()
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph

    punctuation_paragraph = Paragraph(
        reportlab_cjk_markup("123456789束。”下一句（说明）"),
        ParagraphStyle(
            "kinsoku-probe",
            fontName="Helvetica",
            fontSize=10,
            leading=15,
            wordWrap="CJK",
        ),
    )
    punctuation_paragraph.wrap(64, 300)
    extracted_lines = [
        "".join(fragment.text for fragment in line.words)
        for line in punctuation_paragraph.blPara.lines
    ]
    if any(
        line and line[0] in "，。；：！？、）》】”’」』〉〕〗〙〛）"
        for line in extracted_lines
    ):
        raise AssertionError("ReportLab CJK 分行不得产生闭合标点行首")
    if any(
        line and line[-1] in "（《【“‘「『〈〔〖〘〚"
        for line in extracted_lines
    ):
        raise AssertionError("ReportLab CJK 分行不得产生开放标点行末")


def test_flow_layout_fit_and_overflow() -> None:
    """流式装箱：装得下要报剩余高度，装不下要报第一个溢出项。"""

    flow_style = make_cjk_style(
        "flow-self-test",
        font_name="Helvetica",
        font_size=10,
        leading_ratio=1.6,
        first_line_indent_em=2,
        space_after_em=0.5,
    )
    flow_result = layout_flow(
        [
            FlowItem("body", "第一段用于验证通用中文流排测量。", flow_style),
            FlowItem("body", "第二段用于验证统一段距与剩余高度。", flow_style),
        ],
        width_pt=260,
        height_pt=180,
    )
    if not flow_result.fits or len(flow_result.placements) != 2:
        raise AssertionError("通用流排模块未能放置正常中文段落")
    if flow_result.remaining_height <= 0:
        raise AssertionError("通用流排模块未记录剩余高度")
    overflow_result = layout_flow(
        [FlowItem("body", "内容" * 400, flow_style)],
        width_pt=80,
        height_pt=40,
    )
    if overflow_result.fits or overflow_result.overflow_index != 0:
        raise AssertionError("通用流排模块必须报告确定性溢出")
    trailing_space_style = make_cjk_style(
        "flow-trailing-space-test",
        font_name="Helvetica",
        font_size=10,
        leading_ratio=1.5,
        space_after_em=3,
    )
    trailing_space_result = layout_flow(
        [FlowItem("body", "短段落", trailing_space_style)],
        width_pt=200,
        height_pt=30,
    )
    if trailing_space_result.fits:
        raise AssertionError("段后距越过底边时不得误报为可容纳")
