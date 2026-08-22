"""证据新鲜度：旧报告不能验证新候选。

单独运行：
    python3 -m pytest -q tests/test_evidence_freshness.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.delivery.evidence import (  # noqa: E402
    EVIDENCE_STALE,
    RunIdentity,
    attempt_dir,
    read_current_run,
    verify_binding,
    write_current_run,
)
from academic_pdf_translation.delivery.first_delivery import (  # noqa: E402
    run_first_delivery,
)
from academic_pdf_translation.verify.visual_gate import (  # noqa: E402
    VISUAL_STALE,
    check_visual_gate,
)
from academic_pdf_translation.verify.visual_plan import (  # noqa: E402
    SIGNAL_MISSING,
    PageRisk,
    RiskSignal,
    VisualReviewPlan,
)
from academic_pdf_translation.verify.visual_result import (  # noqa: E402
    DECISION_PASS,
    ReviewItem,
    VisualReviewResult,
)

PRESENT = "AlphaBetaGammaDelta"
ABSENT = "EpsilonZetaEtaTheta"

IDENTITY = RunIdentity(
    run_id="run-a",
    attempt_id=1,
    candidate_sha256="c" * 64,
    render_plan_sha256="p" * 64,
    renderer_build_id="build-1",
)


def _binding(**overrides) -> dict:
    data = IDENTITY.as_dict()
    data.update(overrides)
    return data


# --- 五元绑定 ---------------------------------------------------------------


def test_old_preflight_cannot_validate_new_candidate() -> None:
    """候选换了，旧 READY 报告的哈希对不上 → EVIDENCE_STALE。"""

    stale = _binding(candidate_sha256="d" * 64)
    problems = verify_binding(stale, IDENTITY)
    assert problems
    assert all(EVIDENCE_STALE in problem for problem in problems)


def test_old_visual_result_cannot_validate_new_candidate() -> None:
    plan = VisualReviewPlan(
        selected=[
            PageRisk(
                candidate_page=2,
                signals=[RiskSignal(SIGNAL_MISSING, "e1", "")],
            )
        ]
    )
    old = VisualReviewResult(
        binding=RunIdentity(
            run_id="run-1",
            attempt_id=1,
            candidate_sha256="d" * 64,
            render_plan_sha256="p" * 64,
            renderer_build_id="renderer-1",
        ),
        items=[ReviewItem(2, "e1", SIGNAL_MISSING, DECISION_PASS)],
    )
    gate = check_visual_gate(plan, old, candidate_sha256="c" * 64)
    assert gate.code == VISUAL_STALE


def test_changed_render_plan_invalidates_downstream_evidence() -> None:
    stale = _binding(render_plan_sha256="q" * 64)
    problems = verify_binding(stale, IDENTITY)
    assert any("render_plan_sha256" in problem for problem in problems)


def test_changed_renderer_build_invalidates_downstream_evidence() -> None:
    stale = _binding(renderer_build_id="build-2")
    problems = verify_binding(stale, IDENTITY)
    assert any("renderer_build_id" in problem for problem in problems)


def test_missing_binding_field_is_stale_not_trusted() -> None:
    """没有绑定字段的证据不是"默认有效"，是默认作废。"""

    unbound = _binding()
    del unbound["candidate_sha256"]
    problems = verify_binding(unbound, IDENTITY)
    assert any("candidate_sha256" in problem for problem in problems)


# --- 运行目录与指针 ---------------------------------------------------------


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


def test_repair_attempt_has_new_candidate_hash(tmp_path: Path) -> None:
    """返修产生新候选：attempt-2 的哈希必须不同，指针指向 2。"""

    source, elements, units, bindings = _tiny_job(tmp_path)
    bad = _make_pdf(tmp_path / "bad.pdf", [PRESENT])
    fixed = _make_pdf(tmp_path / "fixed.pdf", [PRESENT, ABSENT])
    out = tmp_path / "out"
    result = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=lambda round_index: bad if round_index == 0 else fixed,
        apply_repair=lambda plan: None,
        output_dir=out,
        # 合成作业没有渲染计划：显式关掉要求，不靠默认值静默放过
        require_render_plan=False,
        render_pages=False,
    )
    assert result.rebuilds == 1
    assert len(result.builds) == 2
    sha1 = result.builds[0]["binding"]["candidate_sha256"]
    sha2 = result.builds[1]["binding"]["candidate_sha256"]
    assert sha1 and sha2 and sha1 != sha2
    current = read_current_run(out)
    assert current is not None
    assert current.attempt_id == 2
    assert current.candidate_sha256 == sha2


def test_history_is_kept_but_not_used_as_current(tmp_path: Path) -> None:
    source, elements, units, bindings = _tiny_job(tmp_path)
    bad = _make_pdf(tmp_path / "bad.pdf", [PRESENT])
    fixed = _make_pdf(tmp_path / "fixed.pdf", [PRESENT, ABSENT])
    out = tmp_path / "out"
    result = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=lambda round_index: bad if round_index == 0 else fixed,
        apply_repair=lambda plan: None,
        output_dir=out,
        # 合成作业没有渲染计划：显式关掉要求，不靠默认值静默放过
        require_render_plan=False,
        render_pages=False,
    )
    current = read_current_run(out)
    old_dir = attempt_dir(out, current.run_id, 1)
    new_dir = attempt_dir(out, current.run_id, 2)
    # 历史还在磁盘上
    assert old_dir.is_dir() and any(old_dir.iterdir())
    assert new_dir.is_dir()
    # 但旧证据的绑定对不上当前身份
    old_build = json.loads(
        (old_dir / "round-1-build.json").read_text(encoding="utf-8")
    )
    assert verify_binding(old_build["binding"], current)
    # 结论只认当前 attempt
    assert result.attempt_id == 2


def test_current_run_pointer_is_atomic(tmp_path: Path) -> None:
    """指针写坏了也不许污染旧值：临时文件 + 原子改名。"""

    first = RunIdentity("run-a", 1, "c" * 64)
    write_current_run(tmp_path, first)
    # 模拟第二次写在落盘前崩溃：临时文件残留不影响正式指针
    broken = tmp_path / ".current-run.json.tmp"
    broken.write_text("{ 半截", encoding="utf-8")
    current = read_current_run(tmp_path)
    assert current == first
    # 正常的第二次写覆盖成功，且没有留下临时文件
    second = RunIdentity("run-b", 1, "d" * 64)
    write_current_run(tmp_path, second)
    assert read_current_run(tmp_path) == second
    assert not broken.exists()


def test_run_identity_survives_round_trip(tmp_path: Path) -> None:
    identity = RunIdentity("run-x", 3, "e" * 64, "p" * 64, "build-9")
    write_current_run(tmp_path, identity)
    assert read_current_run(tmp_path) == identity
    assert verify_binding(identity.as_dict(), identity) == []


def test_delivery_json_references_current_attempt(tmp_path: Path) -> None:
    """delivery 结论要指明 run_id/attempt_id，证据路径落在运行目录下。"""

    source, elements, units, bindings = _tiny_job(tmp_path)
    good = _make_pdf(tmp_path / "good.pdf", [PRESENT, ABSENT])
    out = tmp_path / "out"
    result = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=lambda _round: good,
        output_dir=out,
        # 合成作业没有渲染计划：显式关掉要求，不靠默认值静默放过
        require_render_plan=False,
        render_pages=False,
    )
    data = result.as_dict()
    assert data["run_id"]
    assert data["attempt_id"] == 1
    expected = str(attempt_dir(out, data["run_id"], 1))
    for name, path in data["evidence"].items():
        assert path.startswith(expected), (name, path)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
