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
from academic_pdf_translation.verify.candidate_mapping import (  # noqa: E402
    METHOD_NO_EVIDENCE,
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
    RISK_WEIGHTS,
    SIGNAL_AMBIGUOUS,
    SIGNAL_CAPTION_SPLIT,
    SIGNAL_GEOMETRY_GAP,
    SIGNAL_MISSING,
    SIGNAL_NO_EVIDENCE,
    SIGNAL_ORDER,
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
        assert {item.code for item in signals[page]} == {
            SIGNAL_GEOMETRY_GAP,
            SIGNAL_AMBIGUOUS,
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
