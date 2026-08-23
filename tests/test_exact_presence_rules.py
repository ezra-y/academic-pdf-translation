"""候选稿的精确存在性规则：哪些原文必须原样出现，哪些必须排除。

两支合在一起，因为它们是同一条规则的正反面：一面规定与内容无关的
精确存在要求，另一面规定已映射的参考文献不重复计入存在性检查。
断言逐字保留原样，中文说明本身就是判据文档。

单独运行：
    python3 -m pytest -q tests/test_exact_presence_rules.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from _self_test_helpers import _make_pdf  # noqa: E402

from build_candidate import _unit_text_blocks  # noqa: E402
from qa_pdf import _mapped_entry_has_visible_retained_content  # noqa: E402
from validate_job import (  # noqa: E402
    _candidate_page_text,
    _is_nonsemantic_divider_source,
    _normalize_source_text,
    _requires_exact_candidate_presence,
    _source_bbox_fuzzy_match,
    _validate_candidate_text_presence,
)


def test_content_independent_exact_presence_rules() -> None:
    if _unit_text_blocks(
        {"source": "84\n\nSource paragraph."},
        "完整译文。\n\n84",
    ) != ["完整译文。"]:
        raise AssertionError("旧整页译文中的原页页码不得进入正文")
    if _unit_text_blocks(
        {"source": "Source paragraph."},
        "样本量如下。\n\n84",
    ) != ["样本量如下。", "84"]:
        raise AssertionError("没有原页页码证据时不得删除正文数字块")
    candidate_page = SimpleNamespace(
        rect=SimpleNamespace(height=800),
        get_text=lambda mode: [
            (48, 12, 180, 20, "中文译制阅读版\n", 0, 0),
            (
                48,
                100,
                520,
                130,
                "样本量 N = 2074，效应量为 0.48。\n",
                1,
                0,
            ),
            (300, 770, 310, 780, "7\n", 2, 0),
        ],
    )
    candidate_text = _candidate_page_text(candidate_page)
    if "2074" not in candidate_text or "048" not in candidate_text:
        raise AssertionError("候选文本清理不得删除正文统计数字")
    if candidate_text.endswith("7"):
        raise AssertionError("候选页脚页码不得插入跨页译文比对")
    if (
        _normalize_source_text("Zürich")
        != _normalize_source_text("Zurich")
    ):
        raise AssertionError("PDF文字层丢失拉丁重音时仍应定位原文单位")
    if not _is_nonsemantic_divider_source("---------------"):
        raise AssertionError("纯表格分隔线不得被误判为原文定位失败")
    if not _is_nonsemantic_divider_source("•"):
        raise AssertionError("独立项目符号不得被误判为原文定位失败")
    if _is_nonsemantic_divider_source("p < .001"):
        raise AssertionError("统计表达式不得被当成非语义分隔线")
    if _is_nonsemantic_divider_source("−"):
        raise AssertionError("单个正负号或运算符不得被当成分隔线")
    if not _source_bbox_fuzzy_match(
        _normalize_source_text(
            "Agreement was κ = 0.68 and Cronbach’s α = 0.82 in the "
            "independent diagnostic validation sample reported here."
        ),
        _normalize_source_text(
            "Agreement was k = 0.68 and Cronbach’s a = 0.82 in the "
            "independent diagnostic validation sample reported here."
        ),
    ):
        raise AssertionError("坐标框文字的字体编码替代不应破坏原文定位")
    if _source_bbox_fuzzy_match(
        _normalize_source_text(
            "The intervention improved sleep quality for participants."
        ),
        _normalize_source_text(
            "The control condition showed no measurable difference."
        ),
    ):
        raise AssertionError("语义不同的坐标框文字不得通过模糊原文核验")
    if not _requires_exact_candidate_presence(
        {"kind": "heading", "review_flags": []}
    ):
        raise AssertionError("任意学科的标题都必须逐单元进入候选")
    if not _requires_exact_candidate_presence(
        {
            "kind": "body",
            "review_flags": ["instrument-item-or-scoring"],
        }
    ):
        raise AssertionError("通用测量工具题项必须逐单元进入候选")
    if not _requires_exact_candidate_presence(
        {
            "kind": "body",
            "review_flags": ["legacy-scale-item-or-scoring"],
        }
    ):
        raise AssertionError("历史工具专属题项标记必须兼容，但不能绑定具体量表")
    if _requires_exact_candidate_presence(
        {"kind": "body", "review_flags": []}
    ):
        raise AssertionError("普通正文不应被错误升级为逐字硬匹配")


def test_mapped_reference_presence_exclusion() -> None:
    if not _mapped_entry_has_visible_retained_content(
        {"retained_region_ids": ["p0002-retained-001"]}
    ):
        raise AssertionError("映射到候选页的保留区域必须计入可见页面内容")
    if _mapped_entry_has_visible_retained_content(
        {"retained_region_ids": []}
    ):
        raise AssertionError("空保留区域列表不得伪造可见页面内容")
    with tempfile.TemporaryDirectory(prefix="reference-presence-test-") as tmp:
        candidate = Path(tmp) / "candidate.pdf"
        _make_pdf(
            candidate,
            [["Translated body paragraph.", "Reference entry retained."]],
        )
        translation = {
            "coverage": {"minimum_candidate_text_presence_ratio": 0.85},
            "units": [
                {
                    "id": "p01-body",
                    "page": 1,
                    "kind": "body",
                    "source": "Original body paragraph.",
                    "translation": "Translated body paragraph.",
                },
                {
                    "id": "p02-references",
                    "page": 2,
                    "kind": "references",
                    "source": "Long source bibliography text.",
                    "translation": "",
                    "keep_source_reason": "题录保留原文",
                },
            ],
        }
        mapping = {
            "source_pages": [
                {
                    "source_page": 1,
                    "candidate_pages": [1],
                },
                {
                    "source_page": 2,
                    "candidate_pages": [1],
                },
            ],
            "units": [
                {
                    "unit_id": "p01-body",
                    "source_page": 1,
                    "candidate_pages": [1],
                },
                {
                    "unit_id": "p02-references",
                    "source_page": 2,
                    "candidate_pages": [1],
                },
            ],
            "retained_regions": [
                {
                    "retained_region_id": "p0002-retained-001",
                    "source_page": 2,
                    "category": "references",
                    "candidate_pages": [1],
                    "candidate_regions": [
                        {
                            "candidate_page": 1,
                            "bbox": [72, 72, 520, 760],
                        }
                    ],
                }
            ],
        }
        errors: list[str] = []
        warnings: list[str] = []
        _validate_candidate_text_presence(
            candidate,
            translation,
            mapping,
            errors,
            warnings,
        )
        if errors:
            raise AssertionError(
                "已由保留区域映射的参考文献不得重复计入译文正文出现率"
            )
        source_mapped_errors: list[str] = []
        source_mapped = {
            **mapping,
            "retained_regions": [
                {
                    **mapping["retained_regions"][0],
                    "candidate_pages": [],
                }
            ],
        }
        _validate_candidate_text_presence(
            candidate,
            translation,
            source_mapped,
            source_mapped_errors,
            [],
            retained_payloads=[
                {
                    "id": "p0002-retained-001",
                    "page": 2,
                    "category": "references",
                    "resolution": "retained-source",
                    "blocks": [
                        {
                            "role": "body",
                            "text": "Reference entry retained.",
                        }
                    ],
                }
            ],
        )
        if source_mapped_errors:
            raise AssertionError(
                "保留题录已在对应原文页输出时不得受末端锚点误报"
            )
        missing_retained = Path(tmp) / "missing-retained-tail.pdf"
        _make_pdf(
            missing_retained,
            [["Reference entry retained. doi: 10.1000/"]],
        )
        retained_errors: list[str] = []
        _validate_candidate_text_presence(
            missing_retained,
            translation,
            mapping,
            retained_errors,
            [],
            retained_payloads=[
                {
                    "id": "p0002-retained-001",
                    "category": "references",
                    "resolution": "retained-source",
                    "blocks": [
                        {
                            "role": "body",
                            "text": (
                                "Reference entry retained. "
                                "doi: 10.1000/missing-tail"
                            ),
                        }
                    ],
                }
            ],
        )
        if not any(
            "p0002-retained-001" in error
            and "未完整体现" in error
            for error in retained_errors
        ):
            raise AssertionError(
                "候选中缺少保留题录尾段时必须阻止注册"
            )
        wrapped_doi = Path(tmp) / "wrapped-doi.pdf"
        _make_pdf(
            wrapped_doi,
            [["Citation doi:10.1371/journal.pmed.", "1000121"]],
        )
        doi_translation = {
            "coverage": {"minimum_candidate_text_presence_ratio": 0.85},
            "units": [
                {
                    "id": "p01-citation",
                    "page": 1,
                    "kind": "metadata",
                    "source": "Citation doi:10.1371/journal.pmed.1000121",
                    "translation": "Citation doi:10.1371/journal.pmed.1000121",
                    "review_flags": ["statistics-or-sample"],
                }
            ],
        }
        doi_mapping = {
            "units": [
                {
                    "unit_id": "p01-citation",
                    "source_page": 1,
                    "candidate_pages": [1],
                }
            ],
            "retained_regions": [],
        }
        doi_errors: list[str] = []
        _validate_candidate_text_presence(
            wrapped_doi,
            doi_translation,
            doi_mapping,
            doi_errors,
            [],
        )
        if doi_errors:
            raise AssertionError(
                "换行后成为纯数字行的DOI尾段不得被当作页码删除"
            )
