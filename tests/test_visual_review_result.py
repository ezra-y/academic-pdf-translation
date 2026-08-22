"""视觉检查结果与门槛：计划不是结果，没看完不许交付。

单独运行：
    python3 -m pytest -q tests/test_visual_review_result.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.delivery import first_delivery as fd  # noqa: E402
from academic_pdf_translation.delivery.models import file_sha256  # noqa: E402
from academic_pdf_translation.delivery.evidence import (  # noqa: E402
    RunIdentity,
)
from academic_pdf_translation.verify.visual_gate import (  # noqa: E402
    VISUAL_DUPLICATE_ANSWER,
    VISUAL_FAIL,
    VISUAL_INCOMPLETE,
    VISUAL_NOT_REQUIRED,
    VISUAL_PASS,
    VISUAL_STALE,
    VISUAL_TRUNCATED,
    VISUAL_UNKNOWN_ELEMENT,
    VISUAL_WAITING,
    check_visual_gate,
    required_answers,
)
from academic_pdf_translation.verify.visual_plan import (  # noqa: E402
    PLAN_NOT_REQUIRED,
    PLAN_REQUIRED,
    SIGNAL_DRAWING_BOUND,
    SIGNAL_MISSING,
    PageRisk,
    RiskSignal,
    VisualReviewPlan,
)
from academic_pdf_translation.verify.visual_result import (  # noqa: E402
    DECISION_FAIL,
    DECISION_PASS,
    ReviewItem,
    VisualResultError,
    VisualReviewResult,
    result_from_dict,
)

PRESENT = "AlphaBetaGammaDelta"
ABSENT = "EpsilonZetaEtaTheta"

SHA_A = "a" * 64
SHA_B = "b" * 64
PLAN_SHA_A = "c" * 64
PLAN_SHA_B = "d" * 64
BUILD_A = "renderer-2026.08.01"
BUILD_B = "renderer-2026.08.02"


def _plan(*pages: PageRisk, skipped: tuple[PageRisk, ...] = ()) -> VisualReviewPlan:
    return VisualReviewPlan(selected=list(pages), skipped=list(skipped))


def _page(page: int, *codes: str, element: str = "") -> PageRisk:
    element_id = element or f"e-{page}"
    return PageRisk(
        candidate_page=page,
        signals=[RiskSignal(code, element_id, "") for code in codes],
    )


def _identity(
    *,
    run_id: str = "run-1",
    attempt_id: int = 1,
    sha: str = SHA_A,
    plan_sha: str = PLAN_SHA_A,
    renderer: str = BUILD_A,
) -> RunIdentity:
    return RunIdentity(
        run_id=run_id,
        attempt_id=attempt_id,
        candidate_sha256=sha,
        render_plan_sha256=plan_sha,
        renderer_build_id=renderer,
    )


def _result(
    *items: ReviewItem,
    sha: str = SHA_A,
    reviewer: str = "targeted-agent",
    binding: RunIdentity | None = None,
) -> VisualReviewResult:
    return VisualReviewResult(
        binding=binding if binding is not None else _identity(sha=sha),
        reviewer_type=reviewer,
        items=list(items),
    )


def _item(
    page: int, code: str, decision: str, detail: str = "", element: str = ""
) -> ReviewItem:
    return ReviewItem(
        candidate_page=page,
        element_id=element or f"e-{page}",
        check_code=code,
        decision=decision,
        detail=detail,
    )


# --- 门槛单元判定 -----------------------------------------------------------


def test_review_plan_is_not_review_result() -> None:
    """有计划没结果 → WAITING，不放行。这正是被评审抓到的洞。"""

    plan = _plan(_page(2, SIGNAL_MISSING))
    assert plan.status == PLAN_REQUIRED
    gate = check_visual_gate(plan, None, candidate_sha256=SHA_A)
    assert gate.code == VISUAL_WAITING
    assert gate.passed is False


def test_no_risk_pages_are_marked_not_required() -> None:
    """没有风险页写 NOT_REQUIRED——是"不用看"，不是"看过了"。"""

    plan = _plan()
    assert plan.status == PLAN_NOT_REQUIRED
    gate = check_visual_gate(plan, None, candidate_sha256=SHA_A)
    assert gate.code == VISUAL_NOT_REQUIRED
    assert gate.passed is True


def test_visual_result_must_cover_every_selected_page() -> None:
    plan = _plan(_page(2, SIGNAL_MISSING), _page(5, SIGNAL_MISSING))
    partial = _result(
        _item(2, SIGNAL_MISSING, DECISION_PASS)
    )
    gate = check_visual_gate(plan, partial, candidate_sha256=SHA_A)
    assert gate.code == VISUAL_INCOMPLETE
    assert any("第 5 页" in reason for reason in gate.reasons)


def test_visual_result_must_answer_every_check() -> None:
    """一页上有两个检查码，只答一个不算看过；总 PASS 更不算。"""

    plan = _plan(_page(2, SIGNAL_MISSING, SIGNAL_DRAWING_BOUND))
    partial = _result(_item(2, SIGNAL_MISSING, DECISION_PASS))
    gate = check_visual_gate(plan, partial, candidate_sha256=SHA_A)
    assert gate.code == VISUAL_INCOMPLETE
    assert any(SIGNAL_DRAWING_BOUND in reason for reason in gate.reasons)


def test_stale_visual_result_is_rejected() -> None:
    plan = _plan(_page(2, SIGNAL_MISSING))
    stale = _result(_item(2, SIGNAL_MISSING, DECISION_PASS), sha=SHA_B)
    gate = check_visual_gate(plan, stale, candidate_sha256=SHA_A)
    assert gate.code == VISUAL_STALE
    assert gate.passed is False


def test_truncated_review_plan_cannot_deliver() -> None:
    """被预算砍掉的高风险页在，计划就不能当"全看过了"。"""

    plan = _plan(
        _page(2, SIGNAL_MISSING), skipped=(_page(7, SIGNAL_MISSING),)
    )
    full = _result(_item(2, SIGNAL_MISSING, DECISION_PASS))
    gate = check_visual_gate(plan, full, candidate_sha256=SHA_A)
    assert gate.code == VISUAL_TRUNCATED
    assert gate.passed is False


def test_visual_pass_and_fail_are_computed_from_items() -> None:
    plan = _plan(_page(2, SIGNAL_MISSING))
    passed = _result(_item(2, SIGNAL_MISSING, DECISION_PASS))
    assert (
        check_visual_gate(plan, passed, candidate_sha256=SHA_A).code
        == VISUAL_PASS
    )
    failed = _result(_item(2, SIGNAL_MISSING, DECISION_FAIL, "图糊了"))
    gate = check_visual_gate(plan, failed, candidate_sha256=SHA_A)
    assert gate.code == VISUAL_FAIL
    assert gate.failed_items


def test_handwritten_total_decision_is_ignored() -> None:
    """载入时手写的总 decision 一律忽略，结论只从条目算。"""

    data = {
        "candidate_sha256": SHA_A,
        "decision": "PASS",
        "items": [
            {
                "candidate_page": 2,
                "element_id": "e-2",
                "check_code": SIGNAL_MISSING,
                "decision": "FAIL",
            }
        ],
    }
    assert result_from_dict(data).decision == DECISION_FAIL
    with pytest.raises(VisualResultError):
        result_from_dict({"candidate_sha256": SHA_A, "items": "yes"})


# --- 逐元素闭环 -------------------------------------------------------------


def test_two_elements_with_same_page_and_code_need_two_answers() -> None:
    """第 7 页两个表格都是同一个信号：答一条不算把这一页看完。

    这是被评审抓到的洞。按"页 → 检查码"折叠时，table-001 的答案会
    连 table-002 一起算作看过——而 table-002 可能根本没人看。
    """

    plan = _plan(
        PageRisk(
            candidate_page=7,
            signals=[
                RiskSignal(SIGNAL_DRAWING_BOUND, "table-001", ""),
                RiskSignal(SIGNAL_DRAWING_BOUND, "table-002", ""),
            ],
        )
    )
    assert required_answers(plan) == {
        (7, "table-001", SIGNAL_DRAWING_BOUND),
        (7, "table-002", SIGNAL_DRAWING_BOUND),
    }
    half = _result(
        _item(7, SIGNAL_DRAWING_BOUND, DECISION_PASS, element="table-001")
    )
    gate = check_visual_gate(plan, half, candidate_sha256=SHA_A)
    assert gate.code == VISUAL_INCOMPLETE
    assert any("table-002" in reason for reason in gate.reasons)

    both = _result(
        _item(7, SIGNAL_DRAWING_BOUND, DECISION_PASS, element="table-001"),
        _item(7, SIGNAL_DRAWING_BOUND, DECISION_PASS, element="table-002"),
    )
    assert (
        check_visual_gate(plan, both, candidate_sha256=SHA_A).code
        == VISUAL_PASS
    )


def test_visual_item_requires_element_id() -> None:
    """没有 element_id 的答案说明不了看的是哪一个元素，一律不收。"""

    data = {
        "candidate_sha256": SHA_A,
        "items": [
            {
                "candidate_page": 7,
                "check_code": SIGNAL_DRAWING_BOUND,
                "decision": "PASS",
            }
        ],
    }
    with pytest.raises(VisualResultError) as excinfo:
        result_from_dict(data)
    assert "element_id" in str(excinfo.value)


def test_unknown_visual_element_id_is_rejected() -> None:
    """结果里冒出计划中不存在的元素：对不上任何检查对象，拒收。"""

    plan = _plan(_page(7, SIGNAL_DRAWING_BOUND, element="table-001"))
    result = _result(
        _item(7, SIGNAL_DRAWING_BOUND, DECISION_PASS, element="table-001"),
        _item(7, SIGNAL_DRAWING_BOUND, DECISION_PASS, element="table-999"),
    )
    gate = check_visual_gate(plan, result, candidate_sha256=SHA_A)
    assert gate.code == VISUAL_UNKNOWN_ELEMENT
    assert gate.passed is False
    assert any("table-999" in reason for reason in gate.reasons)


def test_duplicate_visual_answer_is_rejected() -> None:
    """同一个 (页, 元素, 检查码) 答两次，会掩盖两条答案的矛盾。"""

    plan = _plan(_page(7, SIGNAL_DRAWING_BOUND, element="table-001"))
    result = _result(
        _item(7, SIGNAL_DRAWING_BOUND, DECISION_PASS, element="table-001"),
        _item(7, SIGNAL_DRAWING_BOUND, DECISION_PASS, element="table-001"),
    )
    gate = check_visual_gate(plan, result, candidate_sha256=SHA_A)
    assert gate.code == VISUAL_DUPLICATE_ANSWER
    assert gate.passed is False


# --- 五元绑定 ---------------------------------------------------------------


def _bound_case(**overrides):
    """一份本来完全合格的结果，只在某一元上做手脚。"""

    plan = _plan(_page(2, SIGNAL_MISSING))
    result = _result(
        _item(2, SIGNAL_MISSING, DECISION_PASS),
        binding=_identity(**overrides),
    )
    return plan, result


def test_full_binding_passes_the_visual_gate() -> None:
    """五元全对才放行——这是下面四条否定用例的对照组。"""

    plan, result = _bound_case()
    gate = check_visual_gate(plan, result, identity=_identity())
    assert gate.code == VISUAL_PASS


def test_visual_result_rejects_wrong_run_id() -> None:
    plan, result = _bound_case(run_id="run-other")
    gate = check_visual_gate(plan, result, identity=_identity())
    assert gate.code == VISUAL_STALE
    assert any("run_id" in reason for reason in gate.reasons)


def test_visual_result_rejects_wrong_attempt_id() -> None:
    plan, result = _bound_case(attempt_id=2)
    gate = check_visual_gate(plan, result, identity=_identity())
    assert gate.code == VISUAL_STALE
    assert any("attempt_id" in reason for reason in gate.reasons)


def test_visual_result_rejects_wrong_render_plan_hash() -> None:
    """候选字节可以一样，渲染计划换了，旧结论对新链路就不成立。"""

    plan, result = _bound_case(plan_sha=PLAN_SHA_B)
    gate = check_visual_gate(plan, result, identity=_identity())
    assert gate.code == VISUAL_STALE
    assert any("render_plan_sha256" in reason for reason in gate.reasons)


def test_visual_result_rejects_wrong_renderer_build_id() -> None:
    plan, result = _bound_case(renderer=BUILD_B)
    gate = check_visual_gate(plan, result, identity=_identity())
    assert gate.code == VISUAL_STALE
    assert any("renderer_build_id" in reason for reason in gate.reasons)


def test_visual_result_without_binding_is_stale() -> None:
    """五元缺一即 EVIDENCE_STALE：空绑定不是"没要求"，是"证明不了"。"""

    plan = _plan(_page(2, SIGNAL_MISSING))
    result = VisualReviewResult(
        binding=None, items=[_item(2, SIGNAL_MISSING, DECISION_PASS)]
    )
    gate = check_visual_gate(plan, result, identity=_identity())
    assert gate.code == VISUAL_STALE
    assert len(gate.reasons) == 5


# --- 交付级行为 -------------------------------------------------------------


def _make_pdf(path: Path, texts: list[str]) -> Path:
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    cursor = 100.0
    for text in texts:
        page.insert_text((60, cursor), text, fontsize=12)
        cursor += 40
    document.save(path)
    document.close()
    return path


def _tiny_job(tmp_path: Path):
    source = _make_pdf(tmp_path / "source.pdf", [PRESENT, ABSENT])
    elements = [
        {
            "id": "e1",
            "type": "body",
            "page": 1,
            "bbox": [60, 90, 400, 110],
            "required": True,
        },
        {
            "id": "e2",
            "type": "body",
            "page": 1,
            "bbox": [60, 130, 400, 150],
            "required": True,
        },
    ]
    units = [
        {"id": "u1", "translation": PRESENT},
        {"id": "u2", "translation": ABSENT},
    ]
    bindings = [
        {"unit_id": "u1", "element_id": "e1"},
        {"unit_id": "u2", "element_id": "e2"},
    ]
    return source, elements, units, bindings


def _run_with_risky_plan(tmp_path, monkeypatch, visual_result, good=None):
    """结构核查全绿，但视觉计划挑出了 1 页——考察视觉门单独把关。

    ``good`` 允许复用同一份候选文件：PDF 字节里有时间戳，重新生成
    内容相同哈希也会变，结果就会被判 STALE。
    """

    source, elements, units, bindings = _tiny_job(tmp_path)
    if good is None:
        good = _make_pdf(tmp_path / "good.pdf", [PRESENT, ABSENT])
    monkeypatch.setattr(
        fd,
        "build_review_plan",
        lambda mapping, audit, page_budget: _plan(
            _page(1, SIGNAL_DRAWING_BOUND)
        ),
    )
    return good, fd.run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=lambda _round: good,
        output_dir=tmp_path / "out",
        render_pages=False,
        visual_result=visual_result,
    )


def test_selected_pages_without_result_block_delivery(
    tmp_path: Path, monkeypatch
) -> None:
    _, result = _run_with_risky_plan(tmp_path, monkeypatch, None)
    assert result.status == fd.STATUS_HANDOVER
    assert result.delivered is False
    assert any(VISUAL_WAITING in problem for problem in result.problems)
    review_stage = [s for s in result.stages if s.name == fd.STAGE_REVIEW]
    assert review_stage and review_stage[0].ok is False


def test_failed_visual_item_creates_repair_task(
    tmp_path: Path, monkeypatch
) -> None:
    good, _ = _run_with_risky_plan(tmp_path, monkeypatch, None)
    failed = _result(
        _item(1, SIGNAL_DRAWING_BOUND, DECISION_FAIL, "图形对不上"),
        sha=file_sha256(good),
    )
    _, result = _run_with_risky_plan(tmp_path, monkeypatch, failed, good=good)
    assert result.status == fd.STATUS_HANDOVER
    assert any(
        item["signal"] == "visual-fail" for item in result.manual_items
    )


def test_passed_visual_result_allows_next_gate(
    tmp_path: Path, monkeypatch
) -> None:
    good, _ = _run_with_risky_plan(tmp_path, monkeypatch, None)
    passed = _result(
        _item(1, SIGNAL_DRAWING_BOUND, DECISION_PASS, "图形完整"),
        sha=file_sha256(good),
    )
    _, result = _run_with_risky_plan(tmp_path, monkeypatch, passed, good=good)
    assert result.status == fd.STATUS_DELIVERED
    review_stage = [s for s in result.stages if s.name == fd.STAGE_REVIEW]
    assert review_stage and review_stage[0].ok is True
    # 结果作为证据落盘
    assert "round-1-visual-result" in result.evidence
