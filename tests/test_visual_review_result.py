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
from academic_pdf_translation.verify.visual_gate import (  # noqa: E402
    VISUAL_FAIL,
    VISUAL_INCOMPLETE,
    VISUAL_NOT_REQUIRED,
    VISUAL_PASS,
    VISUAL_STALE,
    VISUAL_TRUNCATED,
    VISUAL_WAITING,
    check_visual_gate,
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


def _plan(*pages: PageRisk, skipped: tuple[PageRisk, ...] = ()) -> VisualReviewPlan:
    return VisualReviewPlan(selected=list(pages), skipped=list(skipped))


def _page(page: int, *codes: str) -> PageRisk:
    return PageRisk(
        candidate_page=page,
        signals=[RiskSignal(code, f"e-{page}", "") for code in codes],
    )


def _result(
    *items: ReviewItem, sha: str = SHA_A, reviewer: str = "targeted-agent"
) -> VisualReviewResult:
    return VisualReviewResult(
        candidate_sha256=sha, reviewer_type=reviewer, items=list(items)
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
        ReviewItem(2, SIGNAL_MISSING, DECISION_PASS)
    )
    gate = check_visual_gate(plan, partial, candidate_sha256=SHA_A)
    assert gate.code == VISUAL_INCOMPLETE
    assert any("第 5 页" in reason for reason in gate.reasons)


def test_visual_result_must_answer_every_check() -> None:
    """一页上有两个检查码，只答一个不算看过；总 PASS 更不算。"""

    plan = _plan(_page(2, SIGNAL_MISSING, SIGNAL_DRAWING_BOUND))
    partial = _result(ReviewItem(2, SIGNAL_MISSING, DECISION_PASS))
    gate = check_visual_gate(plan, partial, candidate_sha256=SHA_A)
    assert gate.code == VISUAL_INCOMPLETE
    assert any(SIGNAL_DRAWING_BOUND in reason for reason in gate.reasons)


def test_stale_visual_result_is_rejected() -> None:
    plan = _plan(_page(2, SIGNAL_MISSING))
    stale = _result(ReviewItem(2, SIGNAL_MISSING, DECISION_PASS), sha=SHA_B)
    gate = check_visual_gate(plan, stale, candidate_sha256=SHA_A)
    assert gate.code == VISUAL_STALE
    assert gate.passed is False


def test_truncated_review_plan_cannot_deliver() -> None:
    """被预算砍掉的高风险页在，计划就不能当"全看过了"。"""

    plan = _plan(
        _page(2, SIGNAL_MISSING), skipped=(_page(7, SIGNAL_MISSING),)
    )
    full = _result(ReviewItem(2, SIGNAL_MISSING, DECISION_PASS))
    gate = check_visual_gate(plan, full, candidate_sha256=SHA_A)
    assert gate.code == VISUAL_TRUNCATED
    assert gate.passed is False


def test_visual_pass_and_fail_are_computed_from_items() -> None:
    plan = _plan(_page(2, SIGNAL_MISSING))
    passed = _result(ReviewItem(2, SIGNAL_MISSING, DECISION_PASS))
    assert (
        check_visual_gate(plan, passed, candidate_sha256=SHA_A).code
        == VISUAL_PASS
    )
    failed = _result(ReviewItem(2, SIGNAL_MISSING, DECISION_FAIL, "图糊了"))
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
                "check_code": SIGNAL_MISSING,
                "decision": "FAIL",
            }
        ],
    }
    assert result_from_dict(data).decision == DECISION_FAIL
    with pytest.raises(VisualResultError):
        result_from_dict({"candidate_sha256": SHA_A, "items": "yes"})


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
        ReviewItem(1, SIGNAL_DRAWING_BOUND, DECISION_FAIL, "图形对不上"),
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
        ReviewItem(1, SIGNAL_DRAWING_BOUND, DECISION_PASS, "图形完整"),
        sha=file_sha256(good),
    )
    _, result = _run_with_risky_plan(tmp_path, monkeypatch, passed, good=good)
    assert result.status == fd.STATUS_DELIVERED
    review_stage = [s for s in result.stages if s.name == fd.STAGE_REVIEW]
    assert review_stage and review_stage[0].ok is True
    # 结果作为证据落盘
    assert "round-1-visual-result" in result.evidence
