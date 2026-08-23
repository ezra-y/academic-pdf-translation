"""构建门槛：生成器自己说不行，外层就不许说行。

单独运行：
    python3 -m pytest -q tests/test_build_gate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
from academic_pdf_translation.delivery.first_delivery import (  # noqa: E402
    STATUS_BLOCKED,
    STATUS_DELIVERED,
    STATUS_HANDOVER,
    run_first_delivery,
)
from academic_pdf_translation.delivery.gates import (  # noqa: E402
    BLOCKED_UNKNOWN_BUILD_STATUS,
    GATE_BLOCKED,
    GATE_CONTINUE,
    GATE_REPAIR,
    check_build_gate,
)
from academic_pdf_translation.delivery.models import (  # noqa: E402
    BUILD_BLOCKED,
    BUILD_NEEDS_REPAIR,
    BUILD_READY,
    BuildOutcome,
)

PRESENT = "AlphaBetaGammaDelta"
ABSENT = "EpsilonZetaEtaTheta"


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


def _run(tmp_path: Path, outcome: BuildOutcome):
    source, elements, units, bindings = _tiny_job(tmp_path)
    return run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=lambda _round: outcome,
        output_dir=tmp_path / "out",
        # 合成作业没有渲染计划：显式关掉要求，不靠默认值静默放过
        require_render_plan=False,
        render_pages=False,
    )


# --- 核心：内部 BLOCKED 时外层必须 BLOCKED ---------------------------------


def test_blocked_build_with_existing_pdf_is_not_delivered(
    tmp_path: Path,
) -> None:
    """候选文件哪怕完美存在，生成器说 BLOCKED 就是 blocked。

    这正是评审抓到的洞：之前只要路径存在就继续走。
    """

    good = _make_pdf(tmp_path / "good.pdf", [PRESENT, ABSENT])
    outcome = BuildOutcome(
        status=BUILD_BLOCKED,
        candidate_path=good,
        blocked_stage="render-contract",
        issues=[{"code": "MISSING_FIGURE", "detail": "图 1 没进候选"}],
    )
    result = _run(tmp_path, outcome)
    assert result.status == STATUS_BLOCKED
    assert result.delivered is False
    # 候选留作证据，但结论仍是 blocked。
    assert result.candidate_path == str(good)
    assert any("render-contract" in problem for problem in result.problems)


def test_blocked_build_stops_before_candidate_mapping(tmp_path: Path) -> None:
    """BLOCKED 之后一步都不许走：没有映射、没有对账、没有视觉计划。"""

    good = _make_pdf(tmp_path / "good.pdf", [PRESENT, ABSENT])
    outcome = BuildOutcome(
        status=BUILD_BLOCKED,
        candidate_path=good,
        blocked_stage="input-readiness",
    )
    result = _run(tmp_path, outcome)
    stage_names = [stage.name for stage in result.stages]
    assert "map" not in stage_names
    assert "audit" not in stage_names
    assert "visual-review" not in stage_names
    assert not any("mapping" in key for key in result.evidence)
    # 构建报告本身要留证据。
    assert "round-1-build" in result.evidence


def test_needs_repair_build_cannot_skip_repair(tmp_path: Path) -> None:
    """生成器说要修，核查层却全绿——矛盾交给人，不许直接 delivered。"""

    good = _make_pdf(tmp_path / "good.pdf", [PRESENT, ABSENT])
    outcome = BuildOutcome(
        status=BUILD_NEEDS_REPAIR,
        candidate_path=good,
        issues=["字体覆盖存在缺口"],
    )
    result = _run(tmp_path, outcome)
    assert result.status == STATUS_HANDOVER
    assert result.delivered is False
    assert any("NEEDS_REPAIR" in problem for problem in result.problems)


def test_ready_build_can_continue(tmp_path: Path) -> None:
    good = _make_pdf(tmp_path / "good.pdf", [PRESENT, ABSENT])
    outcome = BuildOutcome(status=BUILD_READY, candidate_path=good)
    result = _run(tmp_path, outcome)
    assert result.status == STATUS_DELIVERED
    # 完整构建状态保存在结论里，含报告哈希。
    assert result.builds and result.builds[0]["status"] == BUILD_READY
    assert len(result.builds[0]["report_sha256"]) == 64


def test_unknown_build_status_is_blocked(tmp_path: Path) -> None:
    """未知不是"大概没事"，未知就是停。"""

    good = _make_pdf(tmp_path / "good.pdf", [PRESENT, ABSENT])
    outcome = BuildOutcome(status="SOMETHING_NEW", candidate_path=good)
    result = _run(tmp_path, outcome)
    assert result.status == STATUS_BLOCKED
    assert any(
        BLOCKED_UNKNOWN_BUILD_STATUS in problem for problem in result.problems
    )


def test_delivery_exit_code_matches_build_gate(tmp_path: Path) -> None:
    """CLI 退出码必须与状态一致：blocked=1、handover=2、delivered=0。"""

    scripts = Path(__file__).resolve().parent.parent / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        from deliver_first_candidate import EXIT_CODES
    finally:
        sys.path.remove(str(scripts))

    assert EXIT_CODES[STATUS_DELIVERED] == 0
    assert EXIT_CODES[STATUS_HANDOVER] == 2
    assert EXIT_CODES[STATUS_BLOCKED] == 1

    good = _make_pdf(tmp_path / "good.pdf", [PRESENT, ABSENT])
    blocked = _run(
        tmp_path, BuildOutcome(status=BUILD_BLOCKED, candidate_path=good)
    )
    assert EXIT_CODES[blocked.status] == 1


# --- 门本身的单元判定 -------------------------------------------------------


def test_gate_verdicts_directly(tmp_path: Path) -> None:
    good = _make_pdf(tmp_path / "good.pdf", [PRESENT])
    assert (
        check_build_gate(
            BuildOutcome(status=BUILD_READY, candidate_path=good)
        ).verdict
        == GATE_CONTINUE
    )
    assert (
        check_build_gate(
            BuildOutcome(status=BUILD_NEEDS_REPAIR, candidate_path=good)
        ).verdict
        == GATE_REPAIR
    )
    assert (
        check_build_gate(BuildOutcome(status=BUILD_BLOCKED)).verdict
        == GATE_BLOCKED
    )
    # READY 却没有文件：报告与产物对不上，也要停。
    assert (
        check_build_gate(
            BuildOutcome(
                status=BUILD_READY, candidate_path=tmp_path / "nope.pdf"
            )
        ).verdict
        == GATE_BLOCKED
    )
