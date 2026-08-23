"""内部返修：只修一次，只往安全的方向修。

单独运行：
    python3 -m pytest -q tests/test_repair.py
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
    METHOD_DRAWING_BOUND,
    METHOD_NO_EVIDENCE,
    CandidateMapping,
    ElementLocation,
    build_mapping,
    element_texts_from_units,
)
from academic_pdf_translation.verify.repair import (  # noqa: E402
    ACTION_KEEP_CAPTION_WITH_TARGET,
    ACTION_PRESERVE_FULL_PAGE,
    ACTION_PRESERVE_REGION,
    ALLOWED_ACTIONS,
    FORBIDDEN_ACTIONS,
    MAX_REPAIR_ROUNDS,
    RepairError,
    compare_rounds,
    escalate,
    format_plan,
    plan_repair,
    validate_action,
)
from academic_pdf_translation.verify.structural_audit import (  # noqa: E402
    CaptionSplit,
    StructuralAudit,
    audit_structure,
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
    mapping = build_mapping(
        fitz.open(needed[0]),
        fitz.open(needed[1]),
        elements,
        element_texts=element_texts_from_units(
            elements, units, bindings=bindings
        ),
    )
    return mapping, audit_structure(mapping, elements)


def _missing(element_id: str, element_type: str) -> ElementLocation:
    return ElementLocation(
        element_id=element_id,
        element_type=element_type,
        source_page=1,
        required=True,
        evidence="没搬过来",
    )


# --- 只修一次 ---------------------------------------------------------------


def test_the_second_round_is_refused_outright() -> None:
    """不是少修几条，是一条都不修。跑够多轮，任何检查都能被磨过去。"""

    mapping = CandidateMapping(locations=[_missing("f1", "vector-figure")])
    first = plan_repair(mapping, StructuralAudit(), round_index=0)
    assert first.allowed is True
    assert first.actions

    second = plan_repair(mapping, StructuralAudit(), round_index=1)
    assert second.allowed is False
    assert second.actions == []
    assert second.manual == []
    assert "交给人" in second.refused


def test_the_round_cap_is_one() -> None:
    assert MAX_REPAIR_ROUNDS == 1


def test_a_refused_plan_says_so_in_the_report() -> None:
    plan = plan_repair(CandidateMapping(), StructuralAudit(), round_index=1)
    assert "不再返修" in format_plan(plan)


# --- 不许改判据 -------------------------------------------------------------


def test_threshold_lowering_is_rejected() -> None:
    """降阈值让报告变好看，产出一个字没变。"""

    for action in FORBIDDEN_ACTIONS:
        with pytest.raises(RepairError) as excinfo:
            validate_action(action)
        assert "禁止" in str(excinfo.value) or "不在允许清单" in str(
            excinfo.value
        )


def test_marking_something_complete_is_not_a_repair() -> None:
    with pytest.raises(RepairError):
        validate_action("mark-complete")


def test_an_unknown_action_is_rejected() -> None:
    with pytest.raises(RepairError):
        validate_action("just-try-again")


def test_allowed_and_forbidden_do_not_overlap() -> None:
    assert not (ALLOWED_ACTIONS & FORBIDDEN_ACTIONS)


def test_every_allowed_action_passes_validation() -> None:
    for action in ALLOWED_ACTIONS:
        validate_action(action)


# --- 动作选择 ---------------------------------------------------------------


def test_a_missing_figure_falls_back_to_region_preservation() -> None:
    plan = plan_repair(
        CandidateMapping(locations=[_missing("f1", "vector-figure")]),
        StructuralAudit(),
    )
    assert [item.action for item in plan.actions] == [ACTION_PRESERVE_REGION]


def test_missing_text_also_falls_back_rather_than_being_dropped() -> None:
    """宁可留英文也不丢内容。"""

    plan = plan_repair(
        CandidateMapping(locations=[_missing("b1", "body")]),
        StructuralAudit(),
    )
    assert plan.actions[0].action == ACTION_PRESERVE_REGION
    assert "不丢内容" in plan.actions[0].reason


def test_a_geometry_gap_escalates_instead_of_retrying() -> None:
    """文字在、几何不在，说明主策略已经走过并失败了，重试同一级没有意义。"""

    mapping = CandidateMapping(
        locations=[
            ElementLocation(
                element_id="f1",
                element_type="vector-figure",
                source_page=2,
                required=True,
                candidate_pages=[1],
                source_drawing_count=213,
                candidate_drawing_count=1,
            )
        ]
    )
    plan = plan_repair(mapping, StructuralAudit())
    assert plan.actions[0].action == ACTION_PRESERVE_REGION
    assert plan.actions[0].signal == "geometry-gap"


def test_a_drawing_bound_only_hit_is_not_treated_as_proof() -> None:
    mapping = CandidateMapping(
        locations=[
            ElementLocation(
                element_id="f1",
                element_type="vector-figure",
                source_page=2,
                required=True,
                candidate_pages=[3],
                method=METHOD_DRAWING_BOUND,
                source_drawing_count=10,
                candidate_drawing_count=99,
            )
        ]
    )
    plan = plan_repair(mapping, StructuralAudit())
    assert plan.actions[0].action == ACTION_PRESERVE_REGION


def test_a_split_caption_is_regrouped_with_its_figure() -> None:
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
    plan = plan_repair(CandidateMapping(), audit)
    assert plan.actions[0].action == ACTION_KEEP_CAPTION_WITH_TARGET
    assert "整组同页" in plan.actions[0].reason


def test_the_same_action_is_not_queued_twice() -> None:
    mapping = CandidateMapping(
        locations=[_missing("f1", "table"), _missing("f1", "table")]
    )
    plan = plan_repair(mapping, StructuralAudit())
    assert len(plan.actions) == 1


# --- 交给人 -----------------------------------------------------------------


def test_an_ambiguous_location_is_not_auto_repaired() -> None:
    """到底是重复排了还是探针太短，机器重试多少次都得不出新信息。"""

    mapping = CandidateMapping(
        locations=[
            ElementLocation(
                element_id="e1",
                element_type="body",
                source_page=1,
                required=True,
                candidate_pages=[1, 2],
            )
        ]
    )
    plan = plan_repair(mapping, StructuralAudit())
    assert plan.actions == []
    assert plan.manual[0].signal == "ambiguous-location"


def test_a_required_element_without_evidence_is_not_auto_repaired() -> None:
    mapping = CandidateMapping(
        locations=[
            ElementLocation(
                element_id="e1",
                element_type="body",
                source_page=4,
                required=True,
                method=METHOD_NO_EVIDENCE,
                evidence="可用文字只有 1 个字符",
            )
        ]
    )
    plan = plan_repair(mapping, StructuralAudit())
    assert plan.actions == []
    assert plan.manual[0].signal == "required-without-evidence"


def test_manual_items_are_printed_not_swallowed() -> None:
    mapping = CandidateMapping(
        locations=[
            ElementLocation(
                element_id="e1",
                element_type="body",
                source_page=4,
                required=True,
                method=METHOD_NO_EVIDENCE,
                evidence="判不了",
            )
        ]
    )
    report = format_plan(plan_repair(mapping, StructuralAudit()))
    assert "交给人处理" in report
    assert "e1" in report


# --- 退无可退 ---------------------------------------------------------------


def test_region_preservation_escalates_to_the_full_page() -> None:
    assert escalate(ACTION_PRESERVE_REGION) == ACTION_PRESERVE_FULL_PAGE


def test_the_last_level_does_not_wrap_around() -> None:
    """退无可退时应当如实报失败，不是绕回去重试。"""

    assert escalate(ACTION_PRESERVE_FULL_PAGE) is None
    assert escalate(ACTION_KEEP_CAPTION_WITH_TARGET) is None


# --- 前后对比 ---------------------------------------------------------------


def test_a_repair_that_breaks_something_else_is_reported() -> None:
    before = CandidateMapping(locations=[_missing("a", "body")])
    after = CandidateMapping(locations=[_missing("b", "body")])
    outcome = compare_rounds(before, after)
    assert outcome.fixed == ["a"]
    assert outcome.regressions == ["b"]
    assert outcome.improved is False
    assert outcome.verdict == "返修引入了新问题"


def test_a_repair_that_fixes_everything_says_so() -> None:
    before = CandidateMapping(locations=[_missing("a", "body")])
    after = CandidateMapping(
        locations=[
            ElementLocation(
                element_id="a",
                element_type="body",
                source_page=1,
                required=True,
                candidate_pages=[1],
            )
        ]
    )
    outcome = compare_rounds(before, after)
    assert outcome.verdict == "全部修好"
    assert outcome.improved is True
    assert outcome.before_missing == 1
    assert outcome.after_missing == 0


def test_a_repair_that_changes_nothing_says_so() -> None:
    mapping = CandidateMapping(locations=[_missing("a", "body")])
    outcome = compare_rounds(mapping, mapping)
    assert outcome.verdict == "返修没有修好任何一条"
    assert outcome.improved is False


def test_a_partial_repair_hands_the_rest_over() -> None:
    before = CandidateMapping(
        locations=[_missing("a", "body"), _missing("b", "body")]
    )
    after = CandidateMapping(locations=[_missing("b", "body")])
    outcome = compare_rounds(before, after)
    assert outcome.verdict == "部分修好，剩下的交给人"
    assert outcome.still_broken == ["b"]


# --- 真实论文 ---------------------------------------------------------------


def test_the_real_bad_candidate_gets_a_bounded_plan() -> None:
    mapping, audit = _real_inputs()
    plan = plan_repair(mapping, audit)
    assert plan.allowed is True
    assert plan.actions
    for item in plan.actions:
        assert item.action in ALLOWED_ACTIONS


def test_the_real_lost_figures_fall_back_to_region_preservation() -> None:
    """样本候选丢了一张矢量图和一条公式，两者都退到保留原文区域。"""

    mapping, audit = _real_inputs()
    plan = plan_repair(mapping, audit)
    preserved = {
        item.element_id
        for item in plan.actions
        if item.action == ACTION_PRESERVE_REGION
    }
    assert any("figure" in element_id for element_id in preserved)
    assert any("formula" in element_id for element_id in preserved)


def test_the_real_split_captions_are_regrouped() -> None:
    mapping, audit = _real_inputs()
    plan = plan_repair(mapping, audit)
    regrouped = [
        item
        for item in plan.actions
        if item.action == ACTION_KEEP_CAPTION_WITH_TARGET
    ]
    assert len(regrouped) == len(audit.caption_splits)


def test_real_residue_is_neither_repaired_nor_escalated() -> None:
    """数学残渣元素已分类为非必需：不修（没什么可修）也不转人工吓人。

    它们仍留在映射结果里、证据写明"随所在区域整块保留"，人要查随时查得到。
    """

    mapping, audit = _real_inputs()
    plan = plan_repair(mapping, audit)
    residue_ids = {
        item.element_id
        for item in mapping.locations
        if "残渣" in item.evidence
    }
    assert residue_ids, "样本里应当有残渣元素"
    for item in plan.actions:
        assert item.element_id not in residue_ids
    for item in plan.manual:
        assert item.element_id not in residue_ids


def test_the_real_second_round_is_refused() -> None:
    mapping, audit = _real_inputs()
    second = plan_repair(mapping, audit, round_index=1)
    assert second.allowed is False
    assert second.actions == []


# --- 动作必须落得进渲染计划 -------------------------------------------------


def _missing_body_mapping() -> CandidateMapping:
    return CandidateMapping(
        source_pages=1,
        candidate_pages=1,
        locations=[
            ElementLocation(
                element_id="p0001-body-009",
                element_type="body",
                source_page=1,
                required=True,
            )
        ],
    )


def test_an_action_off_the_chain_becomes_a_manual_item() -> None:
    """正文元素没有区域级降级；发这个动作只会让整轮返修卡在 BLOCKED。"""

    plan = plan_repair(
        _missing_body_mapping(),
        StructuralAudit(),
        fallback_levels={"p0001-body-009": ["translate-and-reflow"]},
    )
    assert plan.actions == []
    assert [item.element_id for item in plan.manual] == ["p0001-body-009"]
    assert "降级链" in plan.manual[0].reason


def test_an_action_on_the_chain_is_still_issued() -> None:
    plan = plan_repair(
        _missing_body_mapping(),
        StructuralAudit(),
        fallback_levels={
            "p0001-body-009": ["translate-and-reflow", ACTION_PRESERVE_REGION]
        },
    )
    assert [item.action for item in plan.actions] == [ACTION_PRESERVE_REGION]
    assert plan.manual == []


def test_without_chain_information_nothing_changes() -> None:
    """拿不到降级链时保持原样，不凭空多挡一层。"""

    plan = plan_repair(_missing_body_mapping(), StructuralAudit())
    assert [item.action for item in plan.actions] == [ACTION_PRESERVE_REGION]


def test_fallback_levels_are_read_from_the_render_plan() -> None:
    from academic_pdf_translation.verify.repair import (
        fallback_levels_by_element,
    )

    levels = fallback_levels_by_element(
        {
            "elements": [
                {
                    "element_id": "e1",
                    "fallback_levels": ["translate-and-reflow"],
                },
                {"element_id": "e2"},
            ]
        }
    )
    assert levels == {"e1": ["translate-and-reflow"]}
