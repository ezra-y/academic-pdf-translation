"""没有渲染计划不等于合同通过。

原来 _apply_render_contract 看见 render_plan is None 就 return True。
于是删掉 render_plan.json 反而成了最省事的"过关"办法——新架构给自己
留了一条退回旧链路的逃生通道。

现在 v2 作业只有两条路：自动生成一份计划，或者 BLOCKED_RENDER_PLAN_MISSING。
旧作业要跳过这道合同，必须显式写 --legacy-no-render-plan。

单独运行：
    python3 -m pytest -q tests/test_render_plan_required.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
import pytest  # noqa: E402
from _fixtures import make_job  # noqa: E402
from academic_pdf_translation.delivery.first_delivery import (  # noqa: E402
    BLOCKED_RENDER_PLAN_MISSING,
    STATUS_BLOCKED,
    STATUS_DELIVERED,
    DeliveryResult,
    run_first_delivery,
)
from academic_pdf_translation.planning.render_plan import (  # noqa: E402
    PLAN_FILE_NAME,
)

import deliver_first_candidate as cli  # noqa: E402
from _common import SkillError, write_json  # noqa: E402

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


def test_v2_delivery_blocks_without_render_plan(tmp_path: Path) -> None:
    """候选再干净，没有计划也不许说交付。"""

    source, elements, units, bindings = _tiny_job(tmp_path)
    good = _make_pdf(tmp_path / "good.pdf", [PRESENT, ABSENT])
    blocked = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=lambda _round: good,
        output_dir=tmp_path / "out",
        render_pages=False,
        require_render_plan=True,
    )
    assert blocked.status == STATUS_BLOCKED
    assert any(
        BLOCKED_RENDER_PLAN_MISSING in problem for problem in blocked.problems
    )
    # 同一份候选在旧作业模式下仍然能交付——差别只在有没有那句显式声明
    legacy = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=lambda _round: good,
        output_dir=tmp_path / "out-legacy",
        render_pages=False,
        require_render_plan=False,
    )
    assert legacy.status == STATUS_DELIVERED


def test_delivery_can_auto_generate_missing_render_plan(
    tmp_path: Path,
) -> None:
    """作业缺计划时，交付入口自己补一份，而不是当作没这回事。"""

    job_dir = make_job(tmp_path)
    plan_path = job_dir / PLAN_FILE_NAME
    if plan_path.is_file():
        plan_path.unlink()
    assert not plan_path.is_file()

    result = cli.ensure_render_plan(job_dir)
    assert result == plan_path
    assert plan_path.is_file()
    sha, plan = cli.read_render_plan(job_dir)
    assert sha
    assert plan is not None
    assert plan["elements"], "自动生成的计划里得真有元素"


def test_legacy_mode_requires_explicit_flag(tmp_path: Path) -> None:
    """旧链路只留给显式声明的作业，不给静默兼容。"""

    # 作业有元素清单和绑定，但没有源 PDF——计划算不出来，正是要考的那一步
    job_dir = tmp_path / "legacy-job"
    job_dir.mkdir()
    write_json(
        job_dir / "source_elements.json",
        {"elements": [{"id": "e1", "type": "body", "page": 1}]},
    )
    write_json(job_dir / "translation.json", {"units": [{"id": "u1"}]})
    write_json(
        job_dir / "unit_bindings.json",
        {"bindings": [{"unit_id": "u1", "element_id": "e1"}]},
    )
    assert not (job_dir / PLAN_FILE_NAME).is_file()

    captured: dict = {}

    def _fake_run(*_args, **kwargs):
        captured.update(kwargs)
        return DeliveryResult(status=STATUS_DELIVERED)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(cli, "run_first_delivery", _fake_run)
    try:
        # 没有显式声明：自动生成失败就必须停，不许悄悄退回旧链路
        monkey.setattr(
            sys, "argv", ["deliver_first_candidate.py", str(job_dir)]
        )
        assert cli.main() == 1
        assert not captured, "没有计划时根本不该走到交付流程"

        # 显式声明之后才允许跳过合同
        monkey.setattr(
            sys,
            "argv",
            [
                "deliver_first_candidate.py",
                str(job_dir),
                "--legacy-no-render-plan",
            ],
        )
        assert cli.main() == 0
        assert captured["require_render_plan"] is False
    finally:
        monkey.undo()


def test_missing_plan_that_cannot_be_generated_is_blocked(
    tmp_path: Path,
) -> None:
    """自动生成失败时，报的是 BLOCKED_RENDER_PLAN_MISSING，不是"通过"。"""

    job_dir = tmp_path / "empty-job"
    job_dir.mkdir()
    with pytest.raises(SkillError) as excinfo:
        cli.ensure_render_plan(job_dir)
    assert BLOCKED_RENDER_PLAN_MISSING in str(excinfo.value)
    assert "--legacy-no-render-plan" in str(excinfo.value)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
