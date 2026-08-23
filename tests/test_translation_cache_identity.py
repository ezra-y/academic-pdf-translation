"""缓存身份：换模型、换术语表、换目标语言都不得复用旧结果。

单独运行：
    python3 -m pytest -q tests/test_translation_cache_identity.py
"""

from __future__ import annotations

import json
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
from apply_translation_batch import (  # noqa: E402
    apply_cached_batches,
    apply_translation_batch,
)
from translation_cache import TranslationCache  # noqa: E402

MODEL_A = "model-a"
MODEL_B = "model-b"


def _reset_to_pending(job_dir: Path) -> None:
    translation = load_json(job_dir / "translation.json")
    for unit in translation["units"]:
        unit["translation"] = None
        unit["keep_source_code"] = None
        unit["keep_source_reason"] = None
    write_json(job_dir / "translation.json", translation)
    stored = load_json(job_dir / "translation-plan.json")
    for entry in stored["batches"]:
        entry["status"] = "pending"
        entry["applied_at"] = None
        entry["applied_unit_ids"] = []
    write_json(job_dir / "translation-plan.json", stored)


def _apply_once(job_dir: Path, model: str) -> None:
    batch = load_batch(job_dir)
    apply_translation_batch(
        job_dir,
        batch["batch_id"],
        translated_results(batch),
        model=model,
    )


def test_cache_is_bound_to_actual_model(tmp_path: Path) -> None:
    """模型 A 的结果不得被模型 B 的计划复用。"""

    job_dir = make_job(tmp_path)
    plan(job_dir, model=MODEL_A)
    _apply_once(job_dir, MODEL_A)
    entries = json.loads(
        (job_dir / "translation-cache.json").read_text(encoding="utf-8")
    )["entries"]
    assert entries, "记录了模型的批次应当进缓存"
    assert all(
        entry["metadata"]["model"] == MODEL_A for entry in entries.values()
    )

    _reset_to_pending(job_dir)
    plan(job_dir, model=MODEL_B)
    assert apply_cached_batches(job_dir) == [], (
        "换模型后不得命中旧缓存"
    )
    translation = load_json(job_dir / "translation.json")
    assert all(unit["translation"] is None for unit in translation["units"])


def test_no_model_recorded_means_no_reusable_cache(tmp_path: Path) -> None:
    """没有模型标识时不得生成可跨模型复用的正式缓存。"""

    job_dir = make_job(tmp_path)
    plan(job_dir)
    _apply_once(job_dir, "")
    assert not (job_dir / "translation-cache.json").exists()


def test_write_back_rejects_a_different_model(tmp_path: Path) -> None:
    """写回结果时验证实际模型与计划模型一致。"""

    job_dir = make_job(tmp_path)
    plan(job_dir, model=MODEL_A)
    batch = load_batch(job_dir)
    with pytest.raises(SkillError) as excinfo:
        apply_translation_batch(
            job_dir,
            batch["batch_id"],
            translated_results(batch),
            model=MODEL_B,
        )
    assert MODEL_B in str(excinfo.value)


def test_write_back_requires_declaring_the_model(tmp_path: Path) -> None:
    """计划记录了模型时，写回必须声明实际模型。"""

    job_dir = make_job(tmp_path)
    plan(job_dir, model=MODEL_A)
    batch = load_batch(job_dir)
    with pytest.raises(SkillError) as excinfo:
        apply_translation_batch(
            job_dir,
            batch["batch_id"],
            translated_results(batch),
        )
    assert "--model" in str(excinfo.value)


def test_cache_restore_rechecks_terminology_and_language(
    tmp_path: Path,
) -> None:
    """从缓存恢复时重新检查术语表与目标语言等身份字段。"""

    job_dir = make_job(tmp_path)
    plan(job_dir, model=MODEL_A)
    _apply_once(job_dir, MODEL_A)

    cache = TranslationCache(job_dir)
    stored = json.loads(cache.path.read_text(encoding="utf-8"))
    for entry in stored["entries"].values():
        entry["metadata"]["terminology_sha256"] = "0" * 64
    cache.path.write_text(
        json.dumps(stored, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    _reset_to_pending(job_dir)
    with pytest.raises(SkillError) as excinfo:
        apply_cached_batches(job_dir)
    assert "terminology_sha256" in str(excinfo.value)


def test_cache_restore_requires_terminology_review(tmp_path: Path) -> None:
    """术语表状态被改回未确认时，缓存写回也要停下来。"""

    job_dir = make_job(tmp_path)
    plan(job_dir, model=MODEL_A)
    _apply_once(job_dir, MODEL_A)
    _reset_to_pending(job_dir)

    stored = load_json(job_dir / "translation-plan.json")
    stored["terminology_reviewed"] = False
    write_json(job_dir / "translation-plan.json", stored)
    with pytest.raises(SkillError) as excinfo:
        apply_cached_batches(job_dir)
    assert "terminology_reviewed" in str(excinfo.value)
