"""批次执行链路：编排门槛、断点续跑、少翻一批必须失败。

单独运行：
    python3 -m pytest -q tests/test_translation_batch_resume.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from _fixtures import (  # noqa: E402
    fake_translate,
    make_job,
    plan,
    set_terminology_reviewed,
)

from _common import SkillError, load_json, write_json  # noqa: E402
from apply_translation_batch import verify_plan_execution  # noqa: E402
from plan_translation_batches import (  # noqa: E402
    plan_translation_batches,
)
from run_translation_batches import run_translation_batches  # noqa: E402

MODEL = "fake-batch-model-v1"
SMALL_BATCHES: dict[str, Any] = {
    "min_units": 2,
    "max_units": 3,
    "target_chars": 1000,
    "max_chars": 1200,
}


def _translator(
    fail_batches: set[str] | None = None,
    fail_times: int = 1,
):
    """确定性假翻译器；可以让指定批次先失败若干次再成功。"""

    attempts: dict[str, int] = {}
    failing = fail_batches or set()

    def translate(batch: dict[str, Any]) -> list[dict[str, Any]]:
        batch_id = str(batch["batch_id"])
        attempts[batch_id] = attempts.get(batch_id, 0) + 1
        if batch_id in failing and attempts[batch_id] <= fail_times:
            raise RuntimeError(f"模拟 {batch_id} 第 {attempts[batch_id]} 次失败")
        return [
            {"id": unit["id"], "translation": fake_translate(unit["source"])}
            for unit in batch["units"]
        ]

    translate.attempts = attempts  # type: ignore[attr-defined]
    return translate


def test_plan_requires_terminology_review(tmp_path: Path) -> None:
    """术语表没确认之前不得正式编排。"""

    job_dir = make_job(tmp_path)
    with pytest.raises(SkillError) as excinfo:
        plan_translation_batches(job_dir)
    assert "terminology_reviewed" in str(excinfo.value)
    assert not (job_dir / "translation-plan.json").exists()

    set_terminology_reviewed(job_dir)
    result = plan_translation_batches(job_dir, model=MODEL)
    assert result["terminology_reviewed"] is True


def test_plan_records_execution_identity(tmp_path: Path) -> None:
    """计划必须记录实际模型、提示版本、策略版本和术语表哈希。"""

    job_dir = make_job(tmp_path)
    result = plan(job_dir, model=MODEL)
    for key in (
        "model",
        "prompt_version",
        "strategy_version",
        "terminology_sha256",
        "target_language",
    ):
        assert result[key], f"计划缺少 {key}"
    assert result["model"] == MODEL


def test_executor_runs_every_batch_and_verifies(tmp_path: Path) -> None:
    """执行器跑完全部批次，账目必须对得上。"""

    job_dir = make_job(tmp_path)
    plan_data = plan(job_dir, model=MODEL, **SMALL_BATCHES)
    assert plan_data["batch_count"] >= 2, "夹具应当至少编排出两个批次"

    report = run_translation_batches(
        job_dir,
        _translator(),
        model=MODEL,
    )
    assert len(report["executed_batches"]) == plan_data["batch_count"]
    verification = report["verification"]
    assert verification["complete"] is True
    assert verification["applied_units"] == verification["document_units"]

    # 合并按冻结单元 ID，不按完成顺序。
    translation = load_json(job_dir / "translation.json")
    assert [unit["id"] for unit in translation["units"]] == sorted(
        unit["id"] for unit in translation["units"]
    )
    assert all(unit["translation"] for unit in translation["units"])


def test_single_batch_failure_only_retries_that_batch(tmp_path: Path) -> None:
    """单批失败只重试该批，其他批次不重复翻译。"""

    job_dir = make_job(tmp_path)
    plan_data = plan(job_dir, model=MODEL, **SMALL_BATCHES)
    target = plan_data["batches"][1]["batch_id"]
    translate = _translator(fail_batches={target}, fail_times=1)

    report = run_translation_batches(job_dir, translate, model=MODEL)
    assert report["verification"]["complete"] is True
    attempts = translate.attempts  # type: ignore[attr-defined]
    assert attempts[target] == 2
    for batch_id, count in attempts.items():
        if batch_id != target:
            assert count == 1, f"{batch_id} 不应当被重复翻译"


def test_missing_batch_makes_verification_fail(tmp_path: Path) -> None:
    """少执行一批时，程序必须明确失败。"""

    job_dir = make_job(tmp_path)
    plan_data = plan(job_dir, model=MODEL, **SMALL_BATCHES)
    translate = _translator()
    run_translation_batches(job_dir, translate, model=MODEL)

    # 抹掉最后一批的执行记录，模拟少翻一批。
    stored = load_json(job_dir / "translation-plan.json")
    last = stored["batches"][-1]
    last["status"] = "pending"
    last["applied_at"] = None
    last["applied_unit_ids"] = []
    write_json(job_dir / "translation-plan.json", stored)

    with pytest.raises(SkillError) as excinfo:
        verify_plan_execution(job_dir)
    message = str(excinfo.value)
    assert "还有 1 批未执行" in message
    assert plan_data["batch_count"] >= 2


def test_resume_continues_from_the_last_verified_batch(tmp_path: Path) -> None:
    """中断后再次运行，只翻没做完的批次。"""

    job_dir = make_job(tmp_path)
    plan(job_dir, model=MODEL, **SMALL_BATCHES)

    interrupting = _translator()
    original = interrupting

    def stop_after_first(batch: dict[str, Any]) -> list[dict[str, Any]]:
        if batch["index"] > 1:
            raise RuntimeError("模拟中断")
        return original(batch)

    with pytest.raises(SkillError):
        run_translation_batches(
            job_dir,
            stop_after_first,
            model=MODEL,
            max_retries=0,
        )
    stored = load_json(job_dir / "translation-plan.json")
    applied_first = [
        entry["batch_id"]
        for entry in stored["batches"]
        if entry["status"] == "applied"
    ]
    assert applied_first, "中断前完成的批次必须已经落盘"

    resumed = _translator()
    report = run_translation_batches(job_dir, resumed, model=MODEL)
    assert report["verification"]["complete"] is True
    for batch_id in applied_first:
        assert batch_id not in resumed.attempts  # type: ignore[attr-defined]


def test_replanning_preserves_applied_batch_evidence(tmp_path: Path) -> None:
    """重新编排保留已完成批次的时间、重试次数和证据。"""

    job_dir = make_job(tmp_path)
    plan(job_dir, model=MODEL, **SMALL_BATCHES)
    translate = _translator(
        fail_batches={"batch-0001"},
        fail_times=1,
    )
    run_translation_batches(job_dir, translate, model=MODEL)
    before = {
        entry["batch_id"]: dict(entry)
        for entry in load_json(job_dir / "translation-plan.json")["batches"]
    }

    replanned = plan_translation_batches(
        job_dir,
        model=MODEL,
        **SMALL_BATCHES,
    )
    for entry in replanned["batches"]:
        previous = before[entry["batch_id"]]
        assert entry["status"] == "applied"
        assert entry["applied_at"] == previous["applied_at"]
        assert entry["retries"] == previous["retries"]
        assert entry["applied_model"] == previous["applied_model"]
        assert entry["applied_unit_ids"] == previous["applied_unit_ids"]
    assert before["batch-0001"]["retries"] == 1


def test_replanning_does_not_infer_completion_from_translation_text(
    tmp_path: Path,
) -> None:
    """有译文不等于批次做过；状态只能来自记录，不能靠反推。"""

    job_dir = make_job(tmp_path)
    plan(job_dir, model=MODEL, **SMALL_BATCHES)
    translation = load_json(job_dir / "translation.json")
    for unit in translation["units"]:
        unit["translation"] = fake_translate(unit["source"])
    write_json(job_dir / "translation.json", translation)

    replanned = plan_translation_batches(
        job_dir,
        model=MODEL,
        **SMALL_BATCHES,
    )
    assert all(
        entry["status"] == "pending" for entry in replanned["batches"]
    )
