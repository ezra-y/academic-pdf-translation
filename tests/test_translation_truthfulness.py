"""恶意输入测试：译文必须真的是目标语言。

每个用例都能单独运行：
    python3 -m pytest -q tests/test_translation_truthfulness.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from _fixtures import (  # noqa: E402
    load_batch,
    make_job,
    plan,
    translated_results,
)

from _common import SkillError, load_json, write_json  # noqa: E402
from apply_translation_batch import apply_translation_batch  # noqa: E402
from audit_translation_completeness import (  # noqa: E402
    build_completeness_audit,
)
from translation_truthfulness import (  # noqa: E402
    evaluate_translation,
    refresh_coverage,
)


def test_reject_source_text_as_cross_language_translation(tmp_path: Path) -> None:
    """英文原文原样写进 zh-Hans 作业的 translation 字段，必须整批拒绝。"""

    job_dir = make_job(tmp_path)
    plan(job_dir)
    batch = load_batch(job_dir)
    results = [
        {"id": unit["id"], "translation": unit["source"]}
        for unit in batch["units"]
    ]
    with pytest.raises(SkillError) as excinfo:
        apply_translation_batch(job_dir, batch["batch_id"], results)
    assert "TRANSLATION_EQUALS_SOURCE" in str(excinfo.value)

    # 整批拒绝意味着一个字都没写进去。
    translation = load_json(job_dir / "translation.json")
    assert all(unit["translation"] is None for unit in translation["units"])
    assert translation["coverage"]["complete"] is False


def test_reject_translation_without_target_language_characters(
    tmp_path: Path,
) -> None:
    """稍作改写但仍然是英文的“译文”，也必须被拒绝。"""

    job_dir = make_job(tmp_path)
    plan(job_dir)
    batch = load_batch(job_dir)
    results = [
        {
            "id": unit["id"],
            "translation": "Translated: " + unit["source"],
        }
        for unit in batch["units"]
    ]
    with pytest.raises(SkillError) as excinfo:
        apply_translation_batch(job_dir, batch["batch_id"], results)
    message = str(excinfo.value)
    assert (
        "TARGET_LANGUAGE_ABSENT" in message
        or "TARGET_LANGUAGE_RATIO_LOW" in message
    )


def test_accept_real_target_language_translation(tmp_path: Path) -> None:
    """语言正确、锚点齐全的译文必须能正常写入，检查不能过严。"""

    job_dir = make_job(tmp_path)
    plan(job_dir)
    batch = load_batch(job_dir)
    report = apply_translation_batch(
        job_dir,
        batch["batch_id"],
        translated_results(batch),
    )
    assert report["applied_units"] == len(batch["units"])
    coverage = load_json(job_dir / "translation.json")["coverage"]
    assert coverage["complete"] is True
    assert coverage["invalid_or_unverified_units"] == 0
    assert coverage["validated_translated_units"] == coverage[
        "source_units_total"
    ]


def test_coverage_not_complete_when_translation_invalid(tmp_path: Path) -> None:
    """直接改写 translation.json 冒充完成时，覆盖率重算必须推翻它。"""

    job_dir = make_job(tmp_path)
    translation = load_json(job_dir / "translation.json")
    for unit in translation["units"]:
        unit["translation"] = unit["source"]
    translation["coverage"].update(
        complete=True,
        translated_units=len(translation["units"]),
        validated_translated_units=len(translation["units"]),
        invalid_or_unverified_units=0,
    )
    write_json(job_dir / "translation.json", translation)

    coverage = refresh_coverage(translation)
    assert coverage["complete"] is False
    assert coverage["invalid_or_unverified_units"] == len(translation["units"])
    assert coverage["validated_translated_units"] == 0
    assert "等待翻译" not in coverage["scope_note"]


def test_completeness_audit_does_not_trust_reported_complete(
    tmp_path: Path,
) -> None:
    """完整性审查不看自报的 complete 字段，自己重算。"""

    job_dir = make_job(tmp_path)
    translation = load_json(job_dir / "translation.json")
    for unit in translation["units"]:
        unit["translation"] = unit["source"]
    translation["coverage"]["complete"] = True
    write_json(job_dir / "translation.json", translation)

    report = build_completeness_audit(job_dir)
    assert report["decision"] == "NEEDS_REPAIR"
    assert report["translation_truthfulness"]["complete"] is False
    assert (
        report["translation_truthfulness"]["problem_counts"][
            "TRANSLATION_EQUALS_SOURCE"
        ]
        == len(translation["units"])
    )


def test_three_levels_are_checked_not_only_a_document_ratio() -> None:
    """单元、批次、文档三层都要有结论，不能只看全篇一个比例。"""

    document = {
        "source_language": "und-Latn",
        "target_language": "zh-Hans",
        "terminology": [],
        "units": [
            {
                "id": "p0001-u0001",
                "page": 1,
                "kind": "body",
                "source": "The sample included one hundred participants.",
                "translation": "样本包含一百名参与者。",
            },
            {
                "id": "p0001-u0002",
                "page": 1,
                "kind": "body",
                "source": "Latency stayed within the agreed envelope.",
                "translation": "Latency stayed inside the agreed envelope.",
            },
        ],
    }
    report = evaluate_translation(
        document,
        batches=[
            {
                "batch_id": "batch-0001",
                "unit_ids": ["p0001-u0001", "p0001-u0002"],
            }
        ],
    )
    assert report["invalid_or_unverified_units"] == 1
    assert report["batches"][0]["batch_id"] == "batch-0001"
    assert report["document_target_script_ratio"] is not None
    assert report["complete"] is False


def test_cache_replay_runs_the_same_checks(tmp_path: Path) -> None:
    """缓存命中后仍然走同一套检查，不能靠缓存绕过。"""

    job_dir = make_job(tmp_path)
    plan(job_dir)
    batch = load_batch(job_dir)
    apply_translation_batch(
        job_dir,
        batch["batch_id"],
        translated_results(batch),
    )
    cache_path = job_dir / "translation-cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    # 只把中文换成英文，锚点原样保留：这样能确保拦住它的是语言检查，
    # 而不是先一步的锚点检查。
    for entry in cache["entries"].values():
        for item in entry["results"]:
            item["translation"] = re.sub(
                r"[\u3400-\u9fff]+",
                " untranslated ",
                item["translation"],
            )
    cache_path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    translation = load_json(job_dir / "translation.json")
    for unit in translation["units"]:
        unit["translation"] = None
    write_json(job_dir / "translation.json", translation)
    plan_data = load_json(job_dir / "translation-plan.json")
    for entry in plan_data["batches"]:
        entry["status"] = "pending"
    write_json(job_dir / "translation-plan.json", plan_data)

    from apply_translation_batch import apply_cached_batches

    with pytest.raises(SkillError) as excinfo:
        apply_cached_batches(job_dir)
    assert "真实性检查" in str(excinfo.value)
