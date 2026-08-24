"""批次路径上的保留原文：题录与署名区按元素角色放行，正文门槛不动。

用户真正走的是 `plan_translation_batches.py` → `apply_translation_batch.py`
这条路。元素角色必须随批次文件一起下发，否则写回那一关看不到角色，
参考文献题录就只能被硬凑成中文句。

单独运行：
    python3 -m pytest -q tests/test_batch_source_form_units.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from _fixtures import load_batch, make_job, plan  # noqa: E402

from _common import SkillError, load_json  # noqa: E402
from apply_translation_batch import apply_translation_batch  # noqa: E402
from fake_translate import fake_translate  # noqa: E402
from plan_translation_batches import PLAN_FILE_NAME  # noqa: E402

KEEP_REASON = "题录按学术惯例保留原文"


def _batch_ids(job_dir: Path) -> list[str]:
    plan_data = load_json(job_dir / PLAN_FILE_NAME)
    return [str(entry["batch_id"]) for entry in plan_data["batches"]]


def _batch_with_role(job_dir: Path, role: str) -> dict[str, Any]:
    for batch_id in _batch_ids(job_dir):
        batch = load_batch(job_dir, batch_id)
        if any(unit.get("element_role") == role for unit in batch["units"]):
            return batch
    raise AssertionError(f"夹具论文应当含角色为 {role} 的单元")


def _results(
    batch: dict[str, Any],
    *,
    keep_role: str,
    keep_code: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for unit in batch["units"]:
        if unit.get("element_role") == keep_role:
            results.append(
                {
                    "id": unit["id"],
                    "translation": None,
                    "keep_source_code": keep_code,
                    "keep_source_reason": KEEP_REASON,
                }
            )
        else:
            results.append(
                {
                    "id": unit["id"],
                    "translation": fake_translate(unit["source"]),
                }
            )
    return results


def test_batch_units_carry_element_role(tmp_path: Path) -> None:
    """批次文件必须带上元素角色，写回那一关才查得到。"""

    job_dir = make_job(tmp_path)
    plan(job_dir)
    seen: set[str] = set()
    for batch_id in _batch_ids(job_dir):
        for unit in load_batch(job_dir, batch_id)["units"]:
            assert "element_role" in unit
            seen.add(str(unit["element_role"]))
    assert "reference-entry" in seen
    assert "publication-metadata" in seen


def test_bibliography_entry_passes_on_the_batch_path(tmp_path: Path) -> None:
    """按 SKILL.md 的写法标题录，写回命令一次通过。"""

    job_dir = make_job(tmp_path)
    plan(job_dir)
    batch = _batch_with_role(job_dir, "reference-entry")
    kept = [
        unit["id"]
        for unit in batch["units"]
        if unit["element_role"] == "reference-entry"
    ]
    report = apply_translation_batch(
        job_dir,
        batch["batch_id"],
        _results(
            batch,
            keep_role="reference-entry",
            keep_code="bibliography-entry",
        ),
    )
    assert report["applied_units"] == len(batch["units"])
    translation = load_json(job_dir / "translation.json")
    index = {unit["id"]: unit for unit in translation["units"]}
    for unit_id in kept:
        assert index[unit_id]["translation"] is None
        assert index[unit_id]["keep_source_code"] == "bibliography-entry"


def test_front_matter_passes_on_the_batch_path(tmp_path: Path) -> None:
    """作者、单位、出版元数据同样按角色放行，不必凑成中文句。"""

    job_dir = make_job(tmp_path)
    plan(job_dir)
    batch = _batch_with_role(job_dir, "publication-metadata")
    kept = [
        unit["id"]
        for unit in batch["units"]
        if unit["element_role"] == "publication-metadata"
    ]
    apply_translation_batch(
        job_dir,
        batch["batch_id"],
        _results(
            batch,
            keep_role="publication-metadata",
            keep_code="publication-front-matter",
        ),
    )
    translation = load_json(job_dir / "translation.json")
    index = {unit["id"]: unit for unit in translation["units"]}
    for unit_id in kept:
        assert index[unit_id]["keep_source_code"] == "publication-front-matter"


def test_body_units_still_fail_on_the_batch_path(tmp_path: Path) -> None:
    """正文的占比门槛一字不动：正文单元照抄原文仍整批拒绝。"""

    job_dir = make_job(tmp_path)
    plan(job_dir)
    batch = _batch_with_role(job_dir, "body")
    results = []
    for unit in batch["units"]:
        if unit["element_role"] == "body":
            results.append({"id": unit["id"], "translation": unit["source"]})
        else:
            results.append(
                {"id": unit["id"], "translation": fake_translate(unit["source"])}
            )
    with pytest.raises(SkillError) as excinfo:
        apply_translation_batch(job_dir, batch["batch_id"], results)
    assert "TRANSLATION_EQUALS_SOURCE" in str(excinfo.value)
    translation = load_json(job_dir / "translation.json")
    assert all(
        unit["translation"] is None for unit in translation["units"]
    )


def test_body_units_cannot_keep_source_on_the_batch_path(
    tmp_path: Path,
) -> None:
    """正文单元套用题录 code 仍然整批拒绝。"""

    job_dir = make_job(tmp_path)
    plan(job_dir)
    batch = _batch_with_role(job_dir, "body")
    with pytest.raises(SkillError) as excinfo:
        apply_translation_batch(
            job_dir,
            batch["batch_id"],
            _results(
                batch,
                keep_role="body",
                keep_code="bibliography-entry",
            ),
        )
    assert "KEEP_SOURCE_CODE_NOT_ALLOWED_FOR_UNIT" in str(excinfo.value)


def test_legacy_batch_without_role_falls_back_to_translation(
    tmp_path: Path,
) -> None:
    """早先编排、没有角色字段的批次文件，回 translation.json 查得到角色。"""

    job_dir = make_job(tmp_path)
    plan(job_dir)
    batch = _batch_with_role(job_dir, "reference-entry")
    plan_data = load_json(job_dir / PLAN_FILE_NAME)
    entry = next(
        item
        for item in plan_data["batches"]
        if item["batch_id"] == batch["batch_id"]
    )
    batch_path = job_dir / entry["file"]
    legacy = load_json(batch_path)
    results = _results(
        batch,
        keep_role="reference-entry",
        keep_code="bibliography-entry",
    )
    for unit in legacy["units"]:
        unit.pop("element_role", None)
    batch_path.write_text(
        __import__("json").dumps(legacy, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    apply_translation_batch(job_dir, batch["batch_id"], results)
    translation = load_json(job_dir / "translation.json")
    index = {unit["id"]: unit for unit in translation["units"]}
    kept = [
        unit["id"]
        for unit in batch["units"]
        if unit["element_role"] == "reference-entry"
    ]
    for unit_id in kept:
        assert index[unit_id]["keep_source_code"] == "bibliography-entry"
