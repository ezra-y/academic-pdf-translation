"""统一首次交付入口：一个结论，三种可能。

单独运行：
    python3 -m pytest -q tests/test_first_delivery.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.delivery.first_delivery import (  # noqa: E402
    STAGE_REBUILD,
    STATUS_BLOCKED,
    STATUS_DELIVERED,
    STATUS_HANDOVER,
    DeliveryResult,
    FirstDeliveryError,
    format_result,
    run_first_delivery,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"

PRESENT = "AlphaBetaGammaDelta"
ABSENT = "EpsilonZetaEtaTheta"


def _real_job():
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
    return (
        needed[0],
        needed[1],
        json.loads(needed[2].read_text(encoding="utf-8"))["elements"],
        json.loads(needed[3].read_text(encoding="utf-8"))["units"],
        json.loads(needed[4].read_text(encoding="utf-8"))["bindings"],
    )


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


# --- 三种结论 ---------------------------------------------------------------


def test_a_clean_candidate_is_delivered(tmp_path: Path) -> None:
    source, elements, units, bindings = _tiny_job(tmp_path)
    good = _make_pdf(tmp_path / "good.pdf", [PRESENT, ABSENT])
    result = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=lambda _round: good,
        output_dir=tmp_path / "out",
        render_pages=False,
    )
    assert result.status == STATUS_DELIVERED
    assert result.delivered is True
    assert result.rebuilds == 0
    assert result.problems == []


def test_a_broken_candidate_without_a_repairer_is_handed_over(
    tmp_path: Path,
) -> None:
    source, elements, units, bindings = _tiny_job(tmp_path)
    bad = _make_pdf(tmp_path / "bad.pdf", [PRESENT])
    result = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=lambda _round: bad,
        output_dir=tmp_path / "out",
        render_pages=False,
    )
    assert result.status == STATUS_HANDOVER
    assert result.rebuilds == 0
    assert result.problems
    assert any(
        stage.name == STAGE_REBUILD and not stage.ok for stage in result.stages
    )


def test_a_build_failure_stops_the_delivery(tmp_path: Path) -> None:
    source, elements, units, bindings = _tiny_job(tmp_path)

    def broken(_round: int) -> Path:
        raise RuntimeError("渲染器炸了")

    result = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=broken,
        output_dir=tmp_path / "out",
        render_pages=False,
    )
    assert result.status == STATUS_BLOCKED
    assert any("渲染器炸了" in problem for problem in result.problems)


# --- 一次返修 ---------------------------------------------------------------


def test_one_repair_round_can_turn_a_handover_into_a_delivery(
    tmp_path: Path,
) -> None:
    source, elements, units, bindings = _tiny_job(tmp_path)
    bad = _make_pdf(tmp_path / "bad.pdf", [PRESENT])
    fixed = _make_pdf(tmp_path / "fixed.pdf", [PRESENT, ABSENT])
    applied: list[int] = []

    result = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=lambda round_index: bad if round_index == 0 else fixed,
        apply_repair=lambda plan: applied.append(len(plan.actions)),
        output_dir=tmp_path / "out",
        render_pages=False,
    )
    assert result.status == STATUS_DELIVERED
    assert result.rebuilds == 1
    assert applied and applied[0] > 0


def test_the_pipeline_never_rebuilds_twice(tmp_path: Path) -> None:
    """第二轮返修计划被拒绝，流程里再加一道断言防止有人绕过去。"""

    source, elements, units, bindings = _tiny_job(tmp_path)
    bad = _make_pdf(tmp_path / "bad.pdf", [PRESENT])
    builds: list[int] = []

    def build(round_index: int) -> Path:
        builds.append(round_index)
        return bad

    result = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=build,
        apply_repair=lambda _plan: None,
        output_dir=tmp_path / "out",
        render_pages=False,
    )
    assert builds == [0, 1]
    assert result.rebuilds == 1
    assert result.status == STATUS_HANDOVER


def test_a_repair_that_breaks_something_else_blocks_delivery(
    tmp_path: Path,
) -> None:
    """修好一个、弄坏一个，绝不能算作"改善"。"""

    source, elements, units, bindings = _tiny_job(tmp_path)
    bad = _make_pdf(tmp_path / "bad.pdf", [PRESENT])
    swapped = _make_pdf(tmp_path / "swapped.pdf", [ABSENT])

    result = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=lambda round_index: bad if round_index == 0 else swapped,
        apply_repair=lambda _plan: None,
        output_dir=tmp_path / "out",
        render_pages=False,
    )
    assert result.status == STATUS_BLOCKED
    assert any("弄坏了" in problem for problem in result.problems)


def test_a_failed_rebuild_blocks_delivery(tmp_path: Path) -> None:
    source, elements, units, bindings = _tiny_job(tmp_path)
    bad = _make_pdf(tmp_path / "bad.pdf", [PRESENT])

    def build(round_index: int) -> Path:
        if round_index == 0:
            return bad
        raise RuntimeError("返修渲染失败")

    result = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=build,
        apply_repair=lambda _plan: None,
        output_dir=tmp_path / "out",
        render_pages=False,
    )
    assert result.status == STATUS_BLOCKED
    assert any("返修重建失败" in problem for problem in result.problems)


# --- 证据 -------------------------------------------------------------------


def test_evidence_is_written_for_every_round(tmp_path: Path) -> None:
    """没有证据的结论不算结论。"""

    source, elements, units, bindings = _tiny_job(tmp_path)
    bad = _make_pdf(tmp_path / "bad.pdf", [PRESENT])
    fixed = _make_pdf(tmp_path / "fixed.pdf", [PRESENT, ABSENT])
    result = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=lambda round_index: bad if round_index == 0 else fixed,
        apply_repair=lambda _plan: None,
        output_dir=tmp_path / "out",
        render_pages=False,
    )
    for key in (
        "round-1-mapping",
        "round-1-audit",
        "round-1-review",
        "repair-plan",
        "round-2-mapping",
        "repair-outcome",
    ):
        assert key in result.evidence, key
        assert Path(result.evidence[key]).is_file()


def test_the_status_cannot_be_written_into_the_evidence(tmp_path: Path) -> None:
    """交付与否由核查结果算出来，不看任何人写进 JSON 的字段。"""

    source, elements, units, bindings = _tiny_job(tmp_path)
    bad = _make_pdf(tmp_path / "bad.pdf", [PRESENT])
    result = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=lambda _round: bad,
        output_dir=tmp_path / "out",
        render_pages=False,
    )
    mapping_file = json.loads(
        Path(result.evidence["round-1-mapping"]).read_text(encoding="utf-8")
    )
    assert mapping_file["complete"] is False
    audit_file = json.loads(
        Path(result.evidence["round-1-audit"]).read_text(encoding="utf-8")
    )
    assert audit_file["passed"] is False


def test_delivered_is_derived_from_status() -> None:
    assert DeliveryResult(status=STATUS_DELIVERED).delivered is True
    assert DeliveryResult(status=STATUS_HANDOVER).delivered is False
    with pytest.raises(AttributeError):
        DeliveryResult().delivered = True  # type: ignore[misc]


# --- 边界 -------------------------------------------------------------------


def test_an_empty_inventory_is_rejected(tmp_path: Path) -> None:
    source, _, units, bindings = _tiny_job(tmp_path)
    with pytest.raises(FirstDeliveryError):
        run_first_delivery(
            source,
            [],
            units,
            bindings,
            build=lambda _round: source,
            output_dir=tmp_path / "out",
        )


def test_a_missing_source_is_rejected(tmp_path: Path) -> None:
    _, elements, units, bindings = _tiny_job(tmp_path)
    with pytest.raises(FirstDeliveryError):
        run_first_delivery(
            tmp_path / "nope.pdf",
            elements,
            units,
            bindings,
            build=lambda _round: tmp_path / "nope.pdf",
            output_dir=tmp_path / "out",
        )


# --- 真实论文 ---------------------------------------------------------------


def test_the_real_bad_candidate_is_handed_over(tmp_path: Path) -> None:
    """人工复审判为不合格的那份候选，交付入口不能说"可以交付"。"""

    source, candidate, elements, units, bindings = _real_job()
    result = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=lambda _round: candidate,
        output_dir=tmp_path / "out",
    )
    assert result.status == STATUS_HANDOVER
    assert result.delivered is False
    assert result.problems
    assert result.manual_items


def test_the_real_run_renders_the_pages_a_human_should_look_at(
    tmp_path: Path,
) -> None:
    source, candidate, elements, units, bindings = _real_job()
    run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=lambda _round: candidate,
        output_dir=tmp_path / "out",
    )
    pages = sorted((tmp_path / "out" / "round-1-pages").glob("*.png"))
    assert pages
    assert all(path.stat().st_size > 1000 for path in pages)


def test_the_real_report_names_the_lost_figure(tmp_path: Path) -> None:
    source, candidate, elements, units, bindings = _real_job()
    result = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=lambda _round: candidate,
        output_dir=tmp_path / "out",
        render_pages=False,
    )
    report = format_result(result)
    assert "交给人处理" in report
    assert "vector-figure" in report
    assert "证据:" in report
