"""质量档位合同：三档行为写成程序，且与排版路线彻底分开。

单独运行：
    python3 -m pytest -q tests/test_quality_mode.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from _fixtures import make_job  # noqa: E402
from academic_pdf_translation.contracts.enums import (  # noqa: E402
    QUALITY_MODE_TO_REVIEW_MODE,
    QualityMode,
    Route,
)
from academic_pdf_translation.contracts.migration import (  # noqa: E402
    MIGRATION_VERSION,
    derive_quality_mode,
    migrate_job,
    migrate_job_file,
    needs_migration,
)
from academic_pdf_translation.planning import mode_policy  # noqa: E402
from academic_pdf_translation.planning.mode_policy import (  # noqa: E402
    policy_for,
    policy_for_job,
)

from _common import load_json, write_json  # noqa: E402
from validate_job import validate_job  # noqa: E402


def test_legacy_none_maps_to_fast() -> None:
    assert QualityMode.parse("none") is QualityMode.FAST
    assert derive_quality_mode({"review": {"mode": "none"}}) is QualityMode.FAST


def test_legacy_independent_maps_to_balanced() -> None:
    assert QualityMode.parse("independent") is QualityMode.BALANCED
    assert (
        derive_quality_mode({"review": {"mode": "independent"}})
        is QualityMode.BALANCED
    )


def test_legacy_precise_maps_to_precise() -> None:
    assert derive_quality_mode({"review": {"mode": "precise"}}) is (
        QualityMode.PRECISE
    )


def test_quality_mode_and_route_are_independent() -> None:
    """档位和排版路线是两件事，任意组合都必须成立。"""

    for mode in QualityMode:
        for route in Route:
            job = {
                "quality_mode": mode.value,
                "route": {"selected": route.value},
                "review": {"mode": QUALITY_MODE_TO_REVIEW_MODE[mode]},
            }
            policy = policy_for_job(job)
            assert policy.quality_mode is mode
            assert job["route"]["selected"] == route.value


def test_fast_has_one_internal_repair() -> None:
    assert policy_for(QualityMode.FAST).max_internal_repairs == 1


def test_no_mode_allows_unlimited_internal_repair() -> None:
    for mode in QualityMode:
        assert policy_for(mode).max_internal_repairs == 1


def test_fast_has_no_full_independent_review() -> None:
    fast = policy_for(QualityMode.FAST)
    assert fast.full_independent_review is False
    assert fast.max_review_rounds == 0
    assert fast.max_independent_repairs == 0


def test_fast_still_does_targeted_visual_review() -> None:
    """快速档减的是重绘野心和复审范围，不是检查本身。"""

    assert policy_for(QualityMode.FAST).targeted_visual_review is True


def test_balanced_and_precise_do_full_independent_review() -> None:
    for mode in (QualityMode.BALANCED, QualityMode.PRECISE):
        policy = policy_for(mode)
        assert policy.full_independent_review is True
        assert policy.max_review_rounds == 1
        assert policy.max_independent_repairs == 1
    assert policy_for(QualityMode.PRECISE).deep_content_checks is True
    assert policy_for(QualityMode.BALANCED).deep_content_checks is False


def test_fast_forbids_rebuilding_complex_content() -> None:
    """快速档不许重画复杂矢量图，也不许重新输入复杂公式。"""

    fast = policy_for(QualityMode.FAST)
    assert mode_policy.VECTOR_FULL_REBUILD in fast.forbidden_strategies
    assert mode_policy.FORMULA_FULL_REBUILD in fast.forbidden_strategies
    assert fast.formula_strategy == mode_policy.FORMULA_PRESERVE_REGION
    assert fast.vector_figure_strategy == (
        mode_policy.VECTOR_PRESERVE_WITH_OVERLAY
    )


def test_no_mode_allows_flattening_a_table() -> None:
    """任何档位都不许把表格压成普通段落。"""

    for mode in QualityMode:
        policy = policy_for(mode)
        assert mode_policy.TABLE_FLATTEN_FORBIDDEN in policy.forbidden_strategies
        assert policy.table_strategy != mode_policy.TABLE_FLATTEN_FORBIDDEN
        assert policy.table_low_confidence_strategy != (
            mode_policy.TABLE_FLATTEN_FORBIDDEN
        )


def test_thresholds_live_in_one_place() -> None:
    """阈值集中在 mode_policy，不许散落。"""

    for mode in QualityMode:
        policy = policy_for(mode)
        assert 0.0 < policy.table_confidence_floor <= 1.0
        assert 0.0 < policy.label_mapping_confidence_floor <= 1.0
        assert 0.0 < policy.element_confidence_floor <= 1.0
        assert policy.preserved_region_min_dpi >= 300


def test_review_mode_is_derived_not_chosen() -> None:
    for mode in QualityMode:
        assert policy_for(mode).review_mode == QUALITY_MODE_TO_REVIEW_MODE[mode]


def test_invalid_quality_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        QualityMode.parse("turbo")


def test_new_job_records_quality_mode(tmp_path: Path) -> None:
    job_dir = make_job(tmp_path)
    job = load_json(job_dir / "job.json")
    assert job["quality_mode"] == QualityMode.BALANCED.value
    assert job["migration_version"] == MIGRATION_VERSION
    assert job["review"]["derived_from_quality_mode"] is True
    assert needs_migration(job) is False


def test_inconsistent_quality_mode_and_review_mode_is_rejected(
    tmp_path: Path,
) -> None:
    """两个字段对不上时必须报错，不能默默采信其中一个。"""

    job_dir = make_job(tmp_path)
    job = load_json(job_dir / "job.json")
    job["quality_mode"] = QualityMode.FAST.value
    write_json(job_dir / "job.json", job)
    report = validate_job(job_dir, "draft")
    assert report["valid"] is False
    assert any("不一致" in error for error in report["errors"])


def test_legacy_job_migrates_and_keeps_old_fields(tmp_path: Path) -> None:
    """旧作业能继续打开：只加字段，不删旧字段，并留快照。"""

    job_dir = make_job(tmp_path)
    job = load_json(job_dir / "job.json")
    job.pop("quality_mode")
    job.pop("migration_version")
    job["review"].pop("derived_from_quality_mode", None)
    job["review"]["mode"] = "independent"
    write_json(job_dir / "job.json", job)

    assert needs_migration(load_json(job_dir / "job.json")) is True
    report = migrate_job_file(job_dir)
    assert report["status"] == "migrated"
    assert report["quality_mode"] == QualityMode.BALANCED.value

    migrated = load_json(job_dir / "job.json")
    assert migrated["quality_mode"] == QualityMode.BALANCED.value
    assert migrated["migration_version"] == MIGRATION_VERSION
    # 旧字段一个都不能丢。
    assert migrated["review"]["mode"] == "independent"
    assert migrated["review"]["choice_recorded"] is True
    assert migrated["review"]["producer_id"] == job["review"]["producer_id"]

    snapshot = Path(report["snapshot"])
    assert snapshot.is_file()
    before = json.loads(snapshot.read_text(encoding="utf-8"))
    assert "quality_mode" not in before, "快照必须是迁移前的原样"

    # 幂等：再迁一次什么都不做。
    assert migrate_job_file(job_dir)["status"] == "unchanged"


def test_migration_does_not_touch_route(tmp_path: Path) -> None:
    """迁移只管档位，绝不改排版路线。"""

    job_dir = make_job(tmp_path)
    job = load_json(job_dir / "job.json")
    job.pop("quality_mode")
    job.pop("migration_version")
    job["route"]["selected"] = Route.CUSTOM_LAYOUT.value
    write_json(job_dir / "job.json", job)
    migrate_job_file(job_dir)
    assert load_json(job_dir / "job.json")["route"]["selected"] == (
        Route.CUSTOM_LAYOUT.value
    )


def test_migrate_job_is_pure_without_job_dir() -> None:
    job = {"review": {"mode": "none"}}
    migrated, changes = migrate_job(dict(job))
    assert migrated["quality_mode"] == QualityMode.FAST.value
    assert changes, "迁移必须留下可读的变更记录"
