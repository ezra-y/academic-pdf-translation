"""真实论文跑整条链路：生成 → 返修 → 等视觉 → 录入 → resume。

合成候选能证明分支走对了，证不了真实论文上的证据链。这个文件把真实作业
复制到临时目录跑完整条路，逐项核对：当前运行还是不是那一轮、第一轮的
文件有没有被动过、第二轮用的是不是返修后重算的那份渲染计划。

真实论文受版权保护不入库，缺语料时自动跳过。

单独运行：
    python3 -m pytest -q tests/test_real_delivery_chain.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from academic_pdf_translation.delivery.evidence import (  # noqa: E402
    attempt_dir,
    read_current_run,
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
from academic_pdf_translation.verify.visual_gate import (  # noqa: E402
    required_answers_from_document,
)
from academic_pdf_translation.verify.visual_result import (  # noqa: E402
    DECISION_PASS,
    ReviewItem,
    VisualReviewResult,
)

import deliver_first_candidate as cli  # noqa: E402
from _common import load_json  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _real_job_copy(tmp_path: Path) -> Path:
    """把真实作业复制一份到临时目录。仓库原件一个字节都不许动。"""

    needed = [
        REAL_JOB / "source.pdf",
        REAL_JOB / "candidate.pdf",
        REAL_JOB / "source_elements.json",
        REAL_JOB / "translation.json",
        REAL_JOB / "unit_bindings.json",
        REAL_JOB / "job.json",
    ]
    if not all(path.is_file() for path in needed):
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    job_dir = tmp_path / "job"
    shutil.copytree(REAL_JOB, job_dir)
    shutil.rmtree(job_dir / "delivery", ignore_errors=True)
    return job_dir


def _hash_tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _answers_for(review_path: Path, identity) -> VisualReviewResult:
    """按计划自己列出的清单，逐元素逐项作答。

    问题清单从落盘的计划里读，不写死——计划变了，答案跟着变。
    """

    answers = required_answers_from_document(load_json(review_path))
    assert answers, "前提没成立：这一轮应当有要人细看的条目"
    return VisualReviewResult(
        binding=identity,
        reviewer_type="pytest-real-chain",
        items=[
            ReviewItem(
                candidate_page=page,
                element_id=element_id,
                check_code=code,
                decision=DECISION_PASS,
                detail="端到端链路核对",
            )
            for (page, element_id, code) in sorted(answers)
        ],
    )


def test_real_paper_repair_chain_keeps_attempt_history(
    tmp_path: Path,
) -> None:
    """返修到 attempt-2 之后再 resume：历史不动，身份不换，计划是新的。

    第一轮故意用语料里那份人工判为不合格的候选，好让自动返修真的触发；
    第二轮走真实生成器，它会先重算渲染计划再画。
    """

    job_dir = _real_job_copy(tmp_path)
    elements, units, bindings = cli._job_inputs(job_dir)
    cli.ensure_render_plan(job_dir)
    delivery = job_dir / "delivery"
    bad = job_dir / "candidate.pdf"
    real_build = cli.make_builder(job_dir, None)

    def build(round_index: int) -> BuildOutcome:
        if round_index == 0:
            sha, plan = cli.read_render_plan(job_dir)
            return BuildOutcome(
                status=BUILD_READY,
                candidate_path=bad,
                candidate_sha256=file_sha256(bad),
                renderer_build_id="corpus-candidate",
                run_id="run-real-chain",
                attempt_id="attempt-1",
                render_plan_sha256=sha,
                render_plan=plan,
            )
        outcome = real_build(1)
        outcome.run_id = "run-real-chain"
        return outcome

    first = run_first_delivery(
        job_dir / "source.pdf",
        elements,
        units,
        bindings,
        build=build,
        apply_repair=cli.make_repair_applier(job_dir),
        output_dir=delivery,
        require_render_plan=True,
    )
    assert first.rebuilds == 1, "前提没成立：这一跑必须真的返修过一轮"
    current = read_current_run(delivery)
    assert current.attempt_id == 2

    one = attempt_dir(delivery, current.run_id, 1)
    two = attempt_dir(delivery, current.run_id, 2)
    sha_one = json.loads(
        (one / "round-1-build.json").read_text(encoding="utf-8")
    )["binding"]["render_plan_sha256"]
    sha_two = json.loads(
        (two / "round-2-build.json").read_text(encoding="utf-8")
    )["binding"]["render_plan_sha256"]
    rebuilt = cli.read_render_plan(job_dir)[0]
    # 第二轮的身份必须是返修后重算的那份计划，不是开跑时那份
    assert sha_one != sha_two
    assert sha_two == rebuilt == current.render_plan_sha256
    # 两轮各留各的计划快照
    assert (one / "render-plan.json").is_file()
    assert (two / "render-plan.json").is_file()
    assert (one / "render-plan.json").read_bytes() != (
        two / "render-plan.json"
    ).read_bytes()

    # 录入视觉结果之后 --resume：只把门槛做完，不碰历史
    before = _hash_tree(one)
    resumed = run_first_delivery(
        job_dir / "source.pdf",
        elements,
        units,
        bindings,
        build=cli.make_resume_builder(delivery, job_dir),
        apply_repair=None,
        output_dir=delivery,
        visual_result=_answers_for(two / "round-2-review.json", current),
        require_render_plan=True,
    )
    assert resumed.attempt_id == 2
    assert resumed.run_id == current.run_id
    assert read_current_run(delivery) == current
    assert _hash_tree(one) == before, "attempt-1 的文件必须一个字节都没变"


def test_real_paper_visual_result_resume_delivers(tmp_path: Path) -> None:
    """真实作业跑通「等视觉 → 录入 → resume → 可以交付」。"""

    job_dir = _real_job_copy(tmp_path)
    elements, units, bindings = cli._job_inputs(job_dir)
    cli.ensure_render_plan(job_dir)
    delivery = job_dir / "delivery"

    first = run_first_delivery(
        job_dir / "source.pdf",
        elements,
        units,
        bindings,
        build=cli.make_builder(job_dir, None),
        apply_repair=cli.make_repair_applier(job_dir),
        output_dir=delivery,
        require_render_plan=True,
        page_budget=12,
    )
    assert first.status != STATUS_DELIVERED, (
        "前提没成立：没有真实视觉结果时不该说可以交付"
    )
    assert any(
        "WAITING_FOR_VISUAL_REVIEW" in problem for problem in first.problems
    )

    current = read_current_run(delivery)
    directory = attempt_dir(delivery, current.run_id, current.attempt_id)
    review = directory / f"round-{current.attempt_id}-review.json"

    resumed = run_first_delivery(
        job_dir / "source.pdf",
        elements,
        units,
        bindings,
        build=cli.make_resume_builder(delivery, job_dir),
        apply_repair=None,
        output_dir=delivery,
        visual_result=_answers_for(review, current),
        require_render_plan=True,
        page_budget=12,
    )
    assert resumed.status == STATUS_DELIVERED, resumed.problems
    assert resumed.attempt_id == current.attempt_id
    assert read_current_run(delivery) == current


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
