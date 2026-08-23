"""高风险视觉检查：挑页、排序、渲染，不许悄悄截断。

单独运行：
    python3 -m pytest -q tests/test_visual_review.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.render.formula_crop import (  # noqa: E402
    STATUS_OK,
    STATUS_UNCERTAIN,
)
from academic_pdf_translation.verify.candidate_mapping import (  # noqa: E402
    METHOD_NO_EVIDENCE,
    METHOD_REGION_PIXELS,
    CandidateMapping,
    ElementLocation,
    build_mapping,
    element_texts_from_units,
)
from academic_pdf_translation.verify.structural_audit import (  # noqa: E402
    CaptionSplit,
    StructuralAudit,
    audit_structure,
)
from academic_pdf_translation.verify.visual_review import (  # noqa: E402
    PLAN_NOT_REQUIRED,
    RISK_WEIGHTS,
    SIGNAL_AMBIGUOUS,
    SIGNAL_CAPTION_SPLIT,
    SIGNAL_DENSE_VECTOR,
    SIGNAL_EMBEDDED_LABEL,
    SIGNAL_FORMULA_INTEGRITY,
    SIGNAL_GEOMETRY_GAP,
    SIGNAL_MISSING,
    SIGNAL_NO_EVIDENCE,
    SIGNAL_ORDER,
    SIGNAL_SAFE_FALLBACK,
    SIGNAL_TABLE_LAYOUT,
    PageRisk,
    RiskSignal,
    VisualReviewError,
    VisualReviewPlan,
    build_review_plan,
    collect_signals,
    format_plan,
    rank_pages,
    render_review_pages,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _real_inputs():
    needed = [
        REAL_JOB / "source.pdf",
        REAL_JOB / "candidate.pdf",
        REAL_JOB / "source_elements.json",
        REAL_JOB / "translation.json",
        REAL_JOB / "unit_bindings.json",
    ]
    if not all(path.is_file() for path in needed):
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    elements = json.loads(needed[2].read_text(encoding="utf-8"))["elements"]
    units = json.loads(needed[3].read_text(encoding="utf-8"))["units"]
    bindings = json.loads(needed[4].read_text(encoding="utf-8"))["bindings"]
    candidate = fitz.open(needed[1])
    mapping = build_mapping(
        fitz.open(needed[0]),
        candidate,
        elements,
        element_texts=element_texts_from_units(
            elements, units, bindings=bindings
        ),
    )
    return mapping, audit_structure(mapping, elements), candidate


# --- 权重 -------------------------------------------------------------------


def test_losing_content_outranks_order_jitter() -> None:
    """整块内容不见了，永远比顺序抖动更该先看。"""

    assert (
        RISK_WEIGHTS[SIGNAL_MISSING]
        > RISK_WEIGHTS[SIGNAL_GEOMETRY_GAP]
        > RISK_WEIGHTS[SIGNAL_CAPTION_SPLIT]
        > RISK_WEIGHTS[SIGNAL_AMBIGUOUS]
        > RISK_WEIGHTS[SIGNAL_ORDER]
    )


def test_an_unknown_signal_weighs_nothing() -> None:
    assert RiskSignal("made-up", "e1", "").weight == 0.0


# --- 信号归页 ---------------------------------------------------------------


def test_a_missing_element_is_pinned_to_its_source_page() -> None:
    """找不到的元素没有候选页码。评审要去看的是它本该出现的地方。"""

    mapping = CandidateMapping(
        locations=[
            ElementLocation(
                element_id="e1",
                element_type="vector-figure",
                source_page=3,
                required=True,
                evidence="几何结构没有搬过来",
            )
        ]
    )
    signals = collect_signals(mapping, StructuralAudit())
    assert list(signals) == [3]
    assert signals[3][0].code == SIGNAL_MISSING


def test_a_geometry_gap_is_pinned_to_the_candidate_pages() -> None:
    mapping = CandidateMapping(
        locations=[
            ElementLocation(
                element_id="e1",
                element_type="vector-figure",
                source_page=2,
                required=True,
                candidate_pages=[1, 9],
                source_drawing_count=213,
                candidate_drawing_count=1,
            )
        ]
    )
    signals = collect_signals(mapping, StructuralAudit())
    assert sorted(signals) == [1, 9]
    for page in (1, 9):
        # 213 个绘图对象的矢量图同时是"密集矢量图"，基础信号照给。
        assert {item.code for item in signals[page]} == {
            SIGNAL_GEOMETRY_GAP,
            SIGNAL_AMBIGUOUS,
            SIGNAL_DENSE_VECTOR,
        }


def test_a_caption_split_marks_both_pages() -> None:
    audit = StructuralAudit(
        caption_splits=[
            CaptionSplit(
                caption_id="c1",
                target_id="f1",
                caption_pages=[4],
                target_pages=[5],
            )
        ]
    )
    signals = collect_signals(CandidateMapping(), audit)
    assert sorted(signals) == [4, 5]


def test_a_required_element_without_evidence_is_a_signal() -> None:
    mapping = CandidateMapping(
        locations=[
            ElementLocation(
                element_id="e1",
                element_type="body",
                source_page=4,
                required=True,
                method=METHOD_NO_EVIDENCE,
                evidence="没有可定位的证据",
            )
        ]
    )
    signals = collect_signals(mapping, StructuralAudit())
    assert signals[4][0].code == SIGNAL_NO_EVIDENCE


# --- 基础风险信号：映射全绿也要看 -------------------------------------------


def _clean_location(
    element_id: str,
    element_type: str,
    *,
    pages: list[int] | None = None,
    method: str = "text-search",
    drawings: int = 0,
) -> ElementLocation:
    """一个结构映射完全正常的元素：找到了、几何够、不模糊。"""

    return ElementLocation(
        element_id=element_id,
        element_type=element_type,
        source_page=pages[0] if pages else 3,
        required=True,
        candidate_pages=list(pages or [3]),
        method=method,
        confidence=1.0,
        source_drawing_count=drawings,
        candidate_drawing_count=drawings,
    )


def _codes(signals: dict[int, list], page: int) -> set[str]:
    return {item.code for item in signals.get(page, [])}


def test_cleanly_mapped_table_still_requires_visual_review() -> None:
    """表格线错位、数字串列，映射一个都看不出来。表格默认进视觉检查。"""

    mapping = CandidateMapping(
        locations=[_clean_location("p0007-table-001", "table", pages=[7])]
    )
    plan = build_review_plan(mapping, StructuralAudit())
    assert [item.candidate_page for item in plan.selected] == [7]
    assert SIGNAL_TABLE_LAYOUT in _codes(
        collect_signals(mapping, StructuralAudit()), 7
    )


def test_cleanly_mapped_formula_still_requires_visual_review() -> None:
    """公式被裁掉一角，映射照样说"在"。免检的门只对三条齐全的证据开。"""

    mapping = CandidateMapping(
        locations=[
            _clean_location(
                "p0004-formula-001", "display-formula", pages=[4]
            )
        ]
    )
    assert SIGNAL_FORMULA_INTEGRITY in _codes(
        collect_signals(mapping, StructuralAudit()), 4
    )

    # 三条证据齐全才免检：原区域整块保留 + 裁切边界检测通过 + 像素指纹。
    preserved = CandidateMapping(
        locations=[
            _clean_location(
                "p0004-formula-001",
                "display-formula",
                pages=[4],
                method=METHOD_REGION_PIXELS,
            )
        ]
    )
    plan_doc = {
        "elements": [
            {
                "element_id": "p0004-formula-001",
                "strategy": "preserve-formula-region",
            }
        ]
    }
    exempt = collect_signals(
        preserved,
        StructuralAudit(),
        render_plan=plan_doc,
        formula_crops={"p0004-formula-001": {"status": STATUS_OK}},
    )
    assert exempt == {}

    # 裁切边界检测没通过就不免——这正是最容易裁掉一角的情况。
    uncertain = collect_signals(
        preserved,
        StructuralAudit(),
        render_plan=plan_doc,
        formula_crops={"p0004-formula-001": {"status": STATUS_UNCERTAIN}},
    )
    assert SIGNAL_FORMULA_INTEGRITY in _codes(uncertain, 4)

    # 少了像素指纹这一条也不免。
    no_fingerprint = collect_signals(
        mapping,
        StructuralAudit(),
        render_plan=plan_doc,
        formula_crops={"p0004-formula-001": {"status": STATUS_OK}},
    )
    assert SIGNAL_FORMULA_INTEGRITY in _codes(no_fingerprint, 4)


def test_dense_vector_page_requires_visual_review() -> None:
    """密集矢量图和带嵌入标签的图：糊了、标签压住图形，都只有看才知道。"""

    mapping = CandidateMapping(
        locations=[
            _clean_location(
                "p0002-figure-001",
                "vector-figure",
                pages=[2],
                method=METHOD_REGION_PIXELS,
                drawings=213,
            )
        ]
    )
    elements = [
        {
            "id": "p0002-figure-001",
            "type": "vector-figure",
            "risk_flags": [{"code": "dense-vector", "detail": "213 个绘图对象"}],
            "relations": {"embedded-label": ["p0002-label-001"]},
        }
    ]
    codes = _codes(
        collect_signals(mapping, StructuralAudit(), elements=elements), 2
    )
    assert SIGNAL_DENSE_VECTOR in codes
    assert SIGNAL_EMBEDDED_LABEL in codes
    # 按像素指纹认出来的元素是整块保留过来的，安全降级也要复看。
    assert SIGNAL_SAFE_FALLBACK in codes
    plan = build_review_plan(mapping, StructuralAudit(), elements=elements)
    assert [item.candidate_page for item in plan.selected] == [2]


def test_embedded_labels_do_not_each_get_their_own_check() -> None:
    """一张图 43 个标签各发一条基础信号只会把清单淹掉。宿主图那一条就够。"""

    mapping = CandidateMapping(
        locations=[
            _clean_location(
                "p0002-figure-001",
                "vector-figure",
                pages=[2],
                method=METHOD_REGION_PIXELS,
                drawings=213,
            ),
            _clean_location(
                "p0002-label-001",
                "unknown",
                pages=[2],
                method=METHOD_REGION_PIXELS,
            ),
        ]
    )
    elements = [
        {
            "id": "p0002-figure-001",
            "type": "vector-figure",
            "risk_flags": [{"code": "dense-vector", "detail": "213 个绘图对象"}],
            "relations": {"embedded-label": ["p0002-label-001"]},
        },
        {
            "id": "p0002-label-001",
            "type": "unknown",
            "relations": {"label-of": ["p0002-figure-001"]},
        },
    ]
    signals = collect_signals(mapping, StructuralAudit(), elements=elements)
    assert {item.element_id for item in signals[2]} == {"p0002-figure-001"}
    assert SIGNAL_EMBEDDED_LABEL in _codes(signals, 2)


def test_simple_body_page_does_not_require_visual_review() -> None:
    """普通正文页不进清单。范围扩大是为了盖住复杂内容，不是为了全看。"""

    mapping = CandidateMapping(
        locations=[
            _clean_location("p0005-body-001", "body", pages=[5]),
            _clean_location("p0005-heading-001", "heading", pages=[5]),
            _clean_location("p0005-caption-001", "caption", pages=[5]),
        ]
    )
    assert collect_signals(mapping, StructuralAudit()) == {}
    plan = build_review_plan(mapping, StructuralAudit())
    assert plan.selected == []
    assert plan.status == PLAN_NOT_REQUIRED


# --- 排序与预算 -------------------------------------------------------------


def test_pages_are_ranked_by_score_then_page_number() -> None:
    signals = {
        3: [RiskSignal(SIGNAL_ORDER, "a", "")],
        1: [RiskSignal(SIGNAL_ORDER, "b", "")],
        2: [RiskSignal(SIGNAL_MISSING, "c", "")],
    }
    selected, _ = rank_pages(signals, page_budget=3)
    assert [item.candidate_page for item in selected] == [2, 1, 3]


def test_quiet_pages_are_left_out() -> None:
    signals = {1: [RiskSignal("made-up", "a", "")]}
    selected, skipped = rank_pages(signals, page_budget=3)
    assert selected == []
    assert skipped == []


def test_pages_beyond_the_budget_are_recorded_not_dropped() -> None:
    """悄悄截断会让报告读起来像"全看过了"。"""

    signals = {
        page: [RiskSignal(SIGNAL_MISSING, f"e{page}", "")]
        for page in range(1, 6)
    }
    selected, skipped = rank_pages(signals, page_budget=2)
    assert len(selected) == 2
    assert len(skipped) == 3
    plan = VisualReviewPlan(page_budget=2, selected=selected, skipped=skipped)
    assert plan.truncated is True
    assert "超出预算未渲染" in format_plan(plan)


def test_a_budget_below_one_is_rejected() -> None:
    with pytest.raises(VisualReviewError):
        rank_pages({1: [RiskSignal(SIGNAL_MISSING, "e", "")]}, page_budget=0)


# --- 检查项 -----------------------------------------------------------------


def test_the_checklist_is_deduped_and_weighted() -> None:
    risk = PageRisk(
        candidate_page=1,
        signals=[
            RiskSignal(SIGNAL_ORDER, "a", ""),
            RiskSignal(SIGNAL_MISSING, "b", ""),
            RiskSignal(SIGNAL_MISSING, "c", ""),
        ],
    )
    checklist = risk.checklist
    assert len(checklist) == 2
    assert "真的没了" in checklist[0]
    assert risk.element_ids == ["a", "b", "c"]


# --- 真实论文 ---------------------------------------------------------------


def test_the_page_that_lost_the_figure_ranks_first() -> None:
    """样本候选里结构图的几何全丢了，那一页必须排在最前面。"""

    mapping, audit, _ = _real_inputs()
    plan = build_review_plan(mapping, audit)
    assert plan.selected
    top = plan.selected[0]
    assert SIGNAL_GEOMETRY_GAP in {item.code for item in top.signals}
    assert top.score == max(item.score for item in plan.selected)


def test_the_real_plan_reports_what_it_left_out() -> None:
    mapping, audit, _ = _real_inputs()
    plan = build_review_plan(mapping, audit, page_budget=6)
    assert len(plan.selected) == 6
    assert plan.truncated is True
    report = format_plan(plan)
    for item in plan.skipped:
        assert f"第 {item.candidate_page} 页" in report


def test_the_real_plan_renders_the_pages_it_selected(tmp_path: Path) -> None:
    mapping, audit, candidate = _real_inputs()
    plan = build_review_plan(mapping, audit, page_budget=3)
    written = render_review_pages(candidate, plan, tmp_path, dpi=72)
    assert len(written) == 3
    assert plan.rendered == [str(path) for path in written]
    for path in written:
        assert path.is_file()
        assert path.stat().st_size > 1000
        with fitz.open(path) as image:
            assert image[0].rect.width > 100


def test_the_review_pages_are_named_after_the_candidate_page(
    tmp_path: Path,
) -> None:
    mapping, audit, candidate = _real_inputs()
    plan = build_review_plan(mapping, audit, page_budget=2)
    written = render_review_pages(candidate, plan, tmp_path, dpi=72)
    for item, path in zip(plan.selected, written, strict=True):
        assert f"{item.candidate_page:04d}" in path.name


def test_a_page_outside_the_candidate_is_rejected(tmp_path: Path) -> None:
    _, _, candidate = _real_inputs()
    plan = VisualReviewPlan(
        selected=[
            PageRisk(
                candidate_page=999,
                signals=[RiskSignal(SIGNAL_MISSING, "e", "")],
            )
        ]
    )
    with pytest.raises(VisualReviewError):
        render_review_pages(candidate, plan, tmp_path, dpi=72)


def test_a_clean_run_needs_no_visual_review() -> None:
    plan = build_review_plan(CandidateMapping(), StructuralAudit())
    assert plan.selected == []
    assert plan.truncated is False
    assert format_plan(plan) == "没有需要人工细看的页。"
