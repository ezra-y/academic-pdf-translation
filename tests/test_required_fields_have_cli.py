"""两个必填字段必须有命令行入口，用户不该手改 JSON。

`terminology_reviewed` 是编排批次的前置，`route.selected` 与
`route.decision_reason` 是进入 translated 阶段的前置。

单独运行：
    python3 -m pytest -q tests/test_required_fields_have_cli.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from _fixtures import make_job  # noqa: E402

from _common import SkillError, load_json  # noqa: E402
from plan_translation_batches import plan_translation_batches  # noqa: E402
from set_complex_content import select_route, set_complex_content  # noqa: E402
from set_terminology import set_terminology  # noqa: E402


def test_terminology_reviewed_unlocks_batch_planning(tmp_path: Path) -> None:
    """确认术语表之后，编排批次的命令就不再拒绝。"""

    job_dir = make_job(tmp_path)
    with pytest.raises(SkillError) as excinfo:
        plan_translation_batches(job_dir)
    assert "terminology_reviewed" in str(excinfo.value)

    result = set_terminology(
        job_dir,
        ["meaning in life=人生意义"],
        reviewed=True,
    )
    assert result["terminology_reviewed"] is True
    assert result["terminology"] == [
        {"source": "meaning in life", "target": "人生意义"}
    ]
    plan = plan_translation_batches(job_dir)
    assert plan["batches"]


def test_terminology_refuses_to_change_after_planning(tmp_path: Path) -> None:
    """批次编排之后改术语必须被拒，除非显式 --force。"""

    job_dir = make_job(tmp_path)
    set_terminology(job_dir, [], reviewed=True)
    plan_translation_batches(job_dir)
    with pytest.raises(SkillError):
        set_terminology(job_dir, ["cohort=队列"])
    result = set_terminology(job_dir, ["cohort=队列"], force=True)
    assert result["terminology"] == [{"source": "cohort", "target": "队列"}]


def test_select_route_writes_both_required_fields(tmp_path: Path) -> None:
    """一条命令把路线和理由都写进 job.json。"""

    job_dir = make_job(tmp_path)
    job = load_json(job_dir / "job.json")
    assert not job["route"].get("selected")

    select_route(job_dir, "standard-auto", "全文为标准正文，普通路线可重建。")
    job = load_json(job_dir / "job.json")
    assert job["route"]["selected"] == "standard-auto"
    assert job["route"]["decision_reason"]


def test_select_route_rejects_empty_reason(tmp_path: Path) -> None:
    job_dir = make_job(tmp_path)
    with pytest.raises(SkillError):
        select_route(job_dir, "standard-auto", "   ")


def test_select_route_rejects_standard_auto_with_complex_pages(
    tmp_path: Path,
) -> None:
    """登记过复杂页就不能再选普通正文路线。"""

    job_dir = make_job(tmp_path)
    set_complex_content(
        job_dir,
        ["1,structured-table,structured-table-rebuild,统计表需保持行列关系"],
    )
    with pytest.raises(SkillError) as excinfo:
        select_route(job_dir, "standard-auto", "想省事")
    assert "hybrid-complex-pages" in str(excinfo.value)
    select_route(job_dir, "hybrid-complex-pages", "第 1 页含统计表。")
    assert (
        load_json(job_dir / "job.json")["route"]["selected"]
        == "hybrid-complex-pages"
    )
