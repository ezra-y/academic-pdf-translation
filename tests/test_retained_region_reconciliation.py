"""保留区域对账：从真实 PDF 抽出来的区域，要和原文结构对得上。

和上一支不同，这里真的开 PDF：抽结构、抽保留区域、按页归位，
再看坐标过滤后的原文、页面家具判定、参考文献信号是否一致。

单独运行：
    python3 -m pytest -q tests/test_retained_region_reconciliation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from _common import import_fitz  # noqa: E402
from audit_translation_completeness import (
    _coordinate_filtered_source_text,  # noqa: E402
)
from build_candidate import _adaptive_page_expansion_limit  # noqa: E402
from extract_source_structure import extract_source_structure  # noqa: E402
from retained_source import (  # noqa: E402
    _is_page_furniture,
    _records_have_reference_signal,
    extract_retained_regions,
    retained_regions_by_page,
)


def test_retained_region_reconciliation() -> None:
    if not _is_page_furniture(
        {"bbox": [20, 47, 28, 56], "text": "6"},
        790,
    ):
        raise AssertionError("参考文献续页顶部的孤立页码必须排除")
    if _is_page_furniture(
        {"bbox": [40, 47, 140, 65], "text": "Methods"},
        790,
    ):
        raise AssertionError("顶部短标题不能仅因位置靠上被当作页眉")
    if _is_page_furniture(
        {
            "bbox": [330, 735, 520, 750],
            "text": "org/10.1590/1980-549720200021",
        },
        800,
    ):
        raise AssertionError("正文区底部的参考文献 DOI 续行不得被当作页脚")
    if not _is_page_furniture(
        {
            "bbox": [330, 770, 520, 790],
            "text": "www.example-journal.org",
        },
        800,
    ):
        raise AssertionError("真正贴近页底的期刊页脚必须排除")
    if not _is_page_furniture(
        {
            "bbox": [40, 20, 520, 78],
            "text": (
                "Author: Article title. Qualitative Studies 6(1), "
                "pp. 91-115 ©2021"
            ),
        },
        842,
    ):
        raise AssertionError("参考文献续页顶部的多行期刊页眉必须排除")
    if not _records_have_reference_signal(
        [
            {
                "text": (
                    "Zisook, S., Shear, K., 2009. Grief and bereavement. "
                    "World Psychiatry 8, 67-74."
                )
            }
        ]
    ):
        raise AssertionError("逗号年份制参考文献续页必须被识别为题录")
    ordered_regions = retained_regions_by_page(
        [
            {
                "id": "right-column",
                "page": 1,
                "category": "references",
                "bbox": [300, 50, 560, 760],
                "effective_bbox": [300, 50, 560, 760],
                "page_width": 595,
            },
            {
                "id": "right-column-continuation",
                "page": 1,
                "category": "references",
                "bbox": [318, 20, 560, 45],
                "effective_bbox": [318, 20, 560, 45],
                "page_width": 595,
            },
            {
                "id": "left-column",
                "page": 1,
                "category": "references",
                "bbox": [30, 500, 290, 760],
                "effective_bbox": [30, 500, 290, 760],
                "page_width": 595,
            },
        ]
    )
    if [
        item["id"] for item in ordered_regions[1]
    ] != [
        "left-column",
        "right-column-continuation",
        "right-column",
    ]:
        raise AssertionError("双栏参考文献必须按栏位和栏内纵向顺序排版")

    fitz = import_fitz()
    with tempfile.TemporaryDirectory(
        prefix="academic-pdf-retained-self-test-"
    ) as tmp:
        source = Path(tmp) / "source.pdf"
        document = fitz.open()
        page = document.new_page(width=595.276, height=841.89)
        page.insert_text(
            (50, 500),
            "References\n"
            "1. Smith J. A reusable reference test. (2020).",
            fontsize=10,
        )
        page.insert_text(
            (315, 80),
            "Ethics statement\n"
            "The participants provided written informed consent.\n"
            "Author contributions\n"
            "AA drafted the manuscript.",
            fontsize=10,
        )
        source_text = page.get_text("text")
        document.save(source)
        document.close()

        retained = {
            "regions": [
                {
                    "id": "reference-region",
                    "page": 1,
                    "bbox": [20, 470, 295, 800],
                    "category": "references",
                },
                {
                    "id": "translated-statements",
                    "page": 1,
                    "bbox": [295, 20, 575, 800],
                    "category": "references",
                },
            ]
        }
        translation = {
            "units": [
                {
                    "id": "p0001-u0001",
                    "page": 1,
                    "source": source_text,
                    "translation": (
                        "伦理声明\n参与者均提供书面知情同意。\n"
                        "作者贡献\nAA负责起草稿件。"
                    ),
                }
            ]
        }
        document = fitz.open(source)
        payloads = extract_retained_regions(
            document,
            retained,
            translation,
        )
        by_id = {payload["id"]: payload for payload in payloads}
        if not by_id["reference-region"]["blocks"]:
            raise AssertionError("真实参考文献区域必须保留可排版题录")
        translated = by_id["translated-statements"]
        if translated["blocks"]:
            raise AssertionError("已翻译的声明区域不得误作参考文献再次插入")
        if (
            translated.get("resolution")
            != "translated-nonreference-region"
            or translated.get("already_present_in_translation") is not True
        ):
            raise AssertionError("误标参考文献区域必须自动归回已翻译正文")
        if float(translated["effective_bbox"][1]) != 20:
            raise AssertionError("参考文献标题不得跨出区域下边界误吸附")
        structure_page = extract_source_structure(source)["pages"][0]
        filtered_source = _coordinate_filtered_source_text(
            structure_page,
            payloads,
        )
        if (
            not filtered_source
            or "participants provided written informed consent"
            not in filtered_source
            or "reusable reference test" in filtered_source
        ):
            raise AssertionError(
                "完整性审计必须按坐标排除参考文献，并保留同页已翻译正文"
            )
        expansion_limit = _adaptive_page_expansion_limit(
            document,
            payloads,
        )
        if not 1.6 < expansion_limit <= 2.4:
            raise AssertionError("参考文献占比必须提高可读排版的页数保护上限")
        dense_complex_limit = _adaptive_page_expansion_limit(
            document,
            payloads,
            {
                "items": [
                    {
                        "status": "ready",
                        "method": "structured-table-rebuild",
                        "payload": {
                            "tables": [
                                {"rows": [["A", "B"]] * 30},
                            ]
                        },
                    },
                    {
                        "status": "ready",
                        "method": "image-text-localization",
                        "payload": {
                            "regions": [
                                {
                                    "localized_labels": [
                                        {
                                            "source": f"Label {index}",
                                            "translation": f"标签 {index}",
                                        }
                                        for index in range(20)
                                    ]
                                }
                            ]
                        },
                    },
                ]
            },
        )
        if not expansion_limit < dense_complex_limit <= 2.4:
            raise AssertionError("密集图表和本地化标签必须进入页数保护预算")
        document.close()
