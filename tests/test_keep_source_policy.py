"""恶意输入测试：保留原文必须有结构化理由和证据。

单独运行：
    python3 -m pytest -q tests/test_keep_source_policy.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from _fixtures import load_batch, make_job, plan  # noqa: E402

from _common import SkillError, load_json, write_json  # noqa: E402
from apply_translation_batch import apply_translation_batch  # noqa: E402
from audit_translation_completeness import (  # noqa: E402
    build_completeness_audit,
)
from translation_truthfulness import (  # noqa: E402
    KEEP_SOURCE_CODES,
    evaluate_translation,
    refresh_coverage,
)
from validate_job import _validate_translation  # noqa: E402

ARBITRARY_REASON = "按学术规范保留原文"


def _reference_region(job_dir: Path) -> dict:
    """按真实坐标登记一块参考文献保留区域。"""

    translation = load_json(job_dir / "translation.json")
    reference_units = [
        unit
        for unit in translation["units"]
        if unit["source"].lstrip().startswith("[")
        or unit["source"].strip() == "References"
    ]
    assert reference_units, "夹具论文应当含参考文献单元"
    page = reference_units[0]["page"]
    boxes = [unit["source_bbox"] for unit in reference_units]
    return {
        "id": f"p{page:04d}-retained-001",
        "page": page,
        "category": "references",
        "reason": "参考文献题录按学术惯例保留原文",
        "bbox": [
            min(box[0] for box in boxes) - 2,
            min(box[1] for box in boxes) - 2,
            max(box[2] for box in boxes) + 2,
            max(box[3] for box in boxes) + 2,
        ],
    }


def test_reject_free_text_keep_reason_for_body(tmp_path: Path) -> None:
    """普通正文只给自由文本理由，必须被拒绝。"""

    job_dir = make_job(tmp_path)
    plan(job_dir)
    batch = load_batch(job_dir)
    results = [
        {
            "id": unit["id"],
            "translation": None,
            "keep_source_reason": ARBITRARY_REASON,
        }
        for unit in batch["units"]
    ]
    with pytest.raises(SkillError) as excinfo:
        apply_translation_batch(job_dir, batch["batch_id"], results)
    assert "keep_source_code" in str(excinfo.value)
    translation = load_json(job_dir / "translation.json")
    assert translation["coverage"]["complete"] is False


def test_reject_whole_document_keep_source(tmp_path: Path) -> None:
    """全篇都声称是参考文献题录，必须被拒绝。"""

    job_dir = make_job(tmp_path)
    plan(job_dir)
    batch = load_batch(job_dir)
    results = [
        {
            "id": unit["id"],
            "translation": None,
            "keep_source_code": "bibliography-entry",
            "keep_source_reason": ARBITRARY_REASON,
        }
        for unit in batch["units"]
    ]
    with pytest.raises(SkillError) as excinfo:
        apply_translation_batch(job_dir, batch["batch_id"], results)
    assert "KEEP_SOURCE_CODE_NOT_ALLOWED_FOR_UNIT" in str(excinfo.value)


def test_reject_every_code_used_on_ordinary_body() -> None:
    """任何一个 code 用在整段普通正文上都不成立。"""

    body = (
        "Automated document pipelines increasingly report their own completion "
        "status, and reviewers rarely verify the reported value."
    )
    for code in KEEP_SOURCE_CODES:
        document = {
            "source_language": "und-Latn",
            "target_language": "zh-Hans",
            "terminology": [],
            "units": [
                {
                    "id": "p0001-u0001",
                    "page": 1,
                    "kind": "body",
                    "source": body,
                    "source_bbox": [50, 50, 500, 90],
                    "translation": None,
                    "keep_source_code": code,
                    "keep_source_reason": ARBITRARY_REASON,
                }
            ],
        }
        report = evaluate_translation(document)
        assert report["invalid_or_unverified_units"] == 1, (
            f"{code} 不应当能豁免整段普通正文"
        )


def test_allow_bibliography_with_structured_evidence(tmp_path: Path) -> None:
    """有坐标和类别证据的参考文献题录，可以合法保留原文。"""

    job_dir = make_job(tmp_path)
    region = _reference_region(job_dir)
    retained = load_json(job_dir / "retained_source.json")
    retained["regions"] = [region]
    write_json(job_dir / "retained_source.json", retained)

    translation = load_json(job_dir / "translation.json")
    kept_ids = []
    for unit in translation["units"]:
        if unit["page"] == region["page"] and unit["source"].lstrip().startswith(
            "["
        ):
            unit["translation"] = None
            unit["keep_source_code"] = "bibliography-entry"
            unit["keep_source_reason"] = "题录按学术惯例保留原文"
            kept_ids.append(unit["id"])
        else:
            unit["translation"] = "已翻译的中文段落。"
    assert kept_ids, "夹具论文应当含可保留的题录单元"

    report = evaluate_translation(
        translation,
        retained_source=retained,
    )
    kept_states = {
        verdict["unit_id"]: verdict["state"] for verdict in report["units"]
    }
    for unit_id in kept_ids:
        assert kept_states[unit_id] == "kept-source"
    assert report["validated_kept_source_units"] == len(kept_ids)
    for verdict in report["units"]:
        if verdict["unit_id"] in kept_ids:
            # 单元类型和保留区域都算结构化证据，取到哪一个都行。
            assert verdict["evidence"]["basis"] in {
                "retained-source-region",
                "reference-unit-kind",
            }


def test_bibliography_code_needs_reference_evidence(tmp_path: Path) -> None:
    """既不是题录单元、区域又不覆盖它时，同一个 code 不能豁免。"""

    job_dir = make_job(tmp_path)
    region = _reference_region(job_dir)
    region["bbox"] = [0.0, 0.0, 5.0, 5.0]
    retained = load_json(job_dir / "retained_source.json")
    retained["regions"] = [region]

    translation = load_json(job_dir / "translation.json")
    marked = 0
    for unit in translation["units"]:
        if not unit["source"].lstrip().startswith("[") and marked == 0:
            unit["translation"] = None
            unit["keep_source_code"] = "bibliography-entry"
            marked += 1
        else:
            unit["translation"] = "已翻译的中文段落。"
    assert marked == 1
    report = evaluate_translation(translation, retained_source=retained)
    assert report["invalid_or_unverified_units"] > 0


def test_residual_source_not_whitelisted_by_arbitrary_reason(
    tmp_path: Path,
) -> None:
    """自由文本理由不得把普通页面变成参考文献页。"""

    job_dir = make_job(tmp_path)
    translation = load_json(job_dir / "translation.json")
    for unit in translation["units"]:
        unit["translation"] = None
        unit["keep_source_reason"] = ARBITRARY_REASON
    translation["coverage"]["complete"] = True
    write_json(job_dir / "translation.json", translation)

    report = build_completeness_audit(job_dir)
    assert report["decision"] == "NEEDS_REPAIR"
    # 参考文献页只能由原文结构认定：单元绑定到题录元素的那一页。
    # 自由文本理由一页也不许多标出来。
    reference_role_pages = {
        unit["page"]
        for unit in translation["units"]
        if unit.get("element_role") == "reference-entry"
    }
    assert not any(
        page["reference_page"] and page["page"] not in reference_role_pages
        for page in report["pages"]
    )
    assert report["translation_truthfulness"]["validated_kept_source_units"] == 0


def test_legacy_keep_reason_reports_a_migration_error(tmp_path: Path) -> None:
    """旧作业只有自由文本理由时，必须报出可理解的迁移错误，不能静默放行。"""

    job_dir = make_job(tmp_path)
    translation = load_json(job_dir / "translation.json")
    for unit in translation["units"]:
        unit.pop("keep_source_code", None)
        unit["translation"] = None
        unit["keep_source_reason"] = "题录保留原文"
    refresh_coverage(translation)

    errors: list[str] = []
    _validate_translation(
        translation,
        page_count=max(unit["page"] for unit in translation["units"]),
        target_language="zh-Hans",
        errors=errors,
    )
    assert any("keep_source_code" in error for error in errors)
    assert any("旧作业" in error for error in errors)


def test_document_keep_source_ratio_has_a_ceiling() -> None:
    """即使每个单元都合法，全篇保留原文的比例也有上限。"""

    units = [
        {
            "id": f"p0001-u{index:04d}",
            "page": 1,
            "kind": "references",
            "source": f"[{index}] Author, A. ({2000 + index}). Title. Journal.",
            "source_bbox": [50, 50 + index, 500, 70 + index],
            "translation": None,
            "keep_source_code": "bibliography-entry",
        }
        for index in range(1, 6)
    ]
    document = {
        "source_language": "und-Latn",
        "target_language": "zh-Hans",
        "terminology": [],
        "units": units,
    }
    report = evaluate_translation(document)
    assert report["invalid_or_unverified_units"] == 0
    assert report["complete"] is False
    assert any(
        problem["code"] == "DOCUMENT_KEEP_SOURCE_RATIO_HIGH"
        for problem in report["problems"]
    )
