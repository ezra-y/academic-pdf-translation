"""渲染计划身份必须跟着每一轮走。

返修会重算 render_plan.json。第二轮候选是按新计划渲染的，那么第二轮的
证据身份、结构合同、快照，全都必须是新计划的。用开跑那一刻读到的旧计划
去验第二轮，等于给新候选签了一张别人的身份证。

单独运行：
    python3 -m pytest -q tests/test_render_plan_identity.py
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
    attempt_dir,
    read_current_run,
    verify_binding,
)
from academic_pdf_translation.delivery.first_delivery import (  # noqa: E402
    STATUS_DELIVERED,
    run_first_delivery,
)
from academic_pdf_translation.delivery.models import (  # noqa: E402
    BUILD_READY,
    BuildOutcome,
    file_sha256,
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


def _write_plan(path: Path, element_ids: list[str]) -> tuple[str, dict]:
    """写一份渲染计划，返回（文件哈希, 计划正文）。

    这里刻意模仿 CLI 的做法：计划先落盘，哈希按落盘的字节算。
    """

    plan = {
        "schema_version": "2.0",
        "elements": [
            {
                "element_id": element_id,
                "element_type": "body",
                "page": 1,
                "strategy": "translate-and-reflow",
                "renderer": "text",
                "status": "ready",
            }
            for element_id in element_ids
        ],
    }
    path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return (file_sha256(path), plan)


def _run_with_repair(tmp_path: Path):
    """跑一遍「首版不合格 → 返修 → 合格」，两轮用两份不同的渲染计划。

    第一份计划漏了 e2（和第一版候选一样有毛病），返修那一轮把它补上。
    """

    source, elements, units, bindings = _tiny_job(tmp_path)
    bad = _make_pdf(tmp_path / "bad.pdf", [PRESENT])
    fixed = _make_pdf(tmp_path / "fixed.pdf", [PRESENT, ABSENT])
    sha_one, plan_one = _write_plan(tmp_path / "plan-1.json", ["e1"])
    sha_two, plan_two = _write_plan(tmp_path / "plan-2.json", ["e1", "e2"])
    assert sha_one != sha_two

    def build(round_index: int) -> BuildOutcome:
        if round_index == 0:
            return BuildOutcome(
                status=BUILD_READY,
                candidate_path=bad,
                renderer_build_id="build-x",
                run_id="run-plan",
                attempt_id="attempt-1",
                render_plan_sha256=sha_one,
                render_plan=plan_one,
            )
        return BuildOutcome(
            status=BUILD_READY,
            candidate_path=fixed,
            renderer_build_id="build-x",
            run_id="run-plan",
            attempt_id="attempt-2",
            render_plan_sha256=sha_two,
            render_plan=plan_two,
        )

    out = tmp_path / "out"
    result = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=build,
        apply_repair=lambda _plan: None,
        output_dir=out,
        render_pages=False,
        require_render_plan=True,
    )
    return result, out, (sha_one, plan_one), (sha_two, plan_two)


def test_repair_attempt_uses_rebuilt_render_plan_hash(tmp_path: Path) -> None:
    """第二轮的证据身份必须写返修后那份计划的哈希。"""

    result, out, (sha_one, _), (sha_two, _) = _run_with_repair(tmp_path)
    assert result.status == STATUS_DELIVERED
    assert len(result.builds) == 2
    assert result.builds[0]["binding"]["render_plan_sha256"] == sha_one
    assert result.builds[1]["binding"]["render_plan_sha256"] == sha_two

    current = read_current_run(out)
    assert current is not None
    assert current.attempt_id == 2
    assert current.render_plan_sha256 == sha_two


def test_round2_contract_uses_rebuilt_render_plan(tmp_path: Path) -> None:
    """第二轮的结构合同必须拿返修后那份计划对账。"""

    result, out, _, (_, plan_two) = _run_with_repair(tmp_path)
    current = read_current_run(out)
    first = json.loads(
        (attempt_dir(out, current.run_id, 1) / "render-contract.json").read_text(
            encoding="utf-8"
        )
    )
    second = json.loads(
        (attempt_dir(out, current.run_id, 2) / "render-contract.json").read_text(
            encoding="utf-8"
        )
    )
    # 第一轮的计划漏了 e2，合同必须报出来
    assert first["planned_element_ids"] == ["e1"]
    assert first["passed"] is False
    # 第二轮用的是补齐后的计划，合同才过得去
    assert second["planned_element_ids"] == sorted(
        item["element_id"] for item in plan_two["elements"]
    )
    assert second["passed"] is True
    assert result.status == STATUS_DELIVERED


def test_each_attempt_keeps_its_render_plan_snapshot(tmp_path: Path) -> None:
    """每一轮都要在自己的 attempt 目录里留一份计划副本。"""

    _, out, (_, plan_one), (_, plan_two) = _run_with_repair(tmp_path)
    current = read_current_run(out)
    snapshot_one = attempt_dir(out, current.run_id, 1) / "render-plan.json"
    snapshot_two = attempt_dir(out, current.run_id, 2) / "render-plan.json"
    assert snapshot_one.is_file() and snapshot_two.is_file()
    assert json.loads(snapshot_one.read_text(encoding="utf-8")) == plan_one
    assert json.loads(snapshot_two.read_text(encoding="utf-8")) == plan_two
    assert plan_one != plan_two


def test_old_plan_binding_cannot_validate_repaired_candidate(
    tmp_path: Path,
) -> None:
    """拿第一轮的绑定去验第二轮候选，必须判 EVIDENCE_STALE。"""

    _, out, _, _ = _run_with_repair(tmp_path)
    current = read_current_run(out)
    old_build = json.loads(
        (
            attempt_dir(out, current.run_id, 1) / "round-1-build.json"
        ).read_text(encoding="utf-8")
    )
    problems = verify_binding(old_build["binding"], current)
    assert any("render_plan_sha256" in problem for problem in problems)
    assert any("attempt_id" in problem for problem in problems)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
