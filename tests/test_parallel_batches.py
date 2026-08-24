"""并行翻译：批次连续分配（上限 5）与 --results-dir 串行写回。

单独运行：
    python3 -m pytest -q tests/test_parallel_batches.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _fixtures import (  # noqa: E402
    fake_translate,
    make_job,
    set_terminology_reviewed,
)

from _common import load_json  # noqa: E402
from plan_translation_batches import (  # noqa: E402
    MAX_TRANSLATORS,
    assign_batches,
    plan_translation_batches,
)

MODEL = "fake-batch-model-v1"
SMALL = {"min_units": 2, "max_units": 3, "target_chars": 1000, "max_chars": 1200}


def test_assignments_are_contiguous_and_cover_everything() -> None:
    ids = [f"batch-{i:04d}" for i in range(1, 18)]
    assignments = assign_batches(ids, 4)
    assert len(assignments) == 4
    flat = [b for item in assignments for b in item["batch_ids"]]
    # 连续分配：拼回去就是原顺序，一个不多一个不少
    assert flat == ids
    sizes = [len(item["batch_ids"]) for item in assignments]
    assert max(sizes) - min(sizes) <= 1


def test_translator_count_is_capped_at_five() -> None:
    ids = [f"batch-{i:04d}" for i in range(1, 18)]
    assert MAX_TRANSLATORS == 5
    assert len(assign_batches(ids, 9)) == 5
    assert len(assign_batches(ids, 0)) == 1
    # 批比人少时，人数收缩到批数
    assert len(assign_batches(ids[:2], 5)) == 2


def _script(name: str) -> str:
    return str(Path(__file__).resolve().parent.parent / "scripts" / name)


def test_results_dir_applies_all_batches_serially(tmp_path: Path) -> None:
    """并行代理各写各的结果文件，唯一写回者一口气串行吃完。"""

    job_dir = make_job(tmp_path)
    set_terminology_reviewed(job_dir)
    plan = plan_translation_batches(job_dir, model=MODEL, **SMALL)
    assert plan["batch_count"] >= 2

    results_dir = tmp_path / "results"
    results_dir.mkdir()
    for batch_meta in plan["batches"]:
        batch: dict[str, Any] = load_json(
            job_dir / "translation-batches" / f"{batch_meta['batch_id']}.json"
        )
        payload = [
            {"id": unit["id"], "translation": fake_translate(unit["source"])}
            for unit in batch["units"]
        ]
        (results_dir / f"{batch_meta['batch_id']}.result.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    proc = subprocess.run(
        [
            sys.executable,
            _script("apply_translation_batch.py"),
            str(job_dir),
            "--results-dir",
            str(results_dir),
            "--model",
            MODEL,
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"串行写回 {plan['batch_count']} 批" in proc.stdout

    refreshed = load_json(job_dir / "translation-plan.json")
    assert all(
        batch["status"] == "applied" for batch in refreshed["batches"]
    )
    translation = load_json(job_dir / "translation.json")
    assert translation["coverage"]["complete"] is True


def test_results_dir_reports_missing_batches(tmp_path: Path) -> None:
    """缺谁的结果文件就点谁的名，已有的照常写回，不算失败。"""

    job_dir = make_job(tmp_path)
    set_terminology_reviewed(job_dir)
    plan = plan_translation_batches(job_dir, model=MODEL, **SMALL)
    first = plan["batches"][0]
    batch = load_json(
        job_dir / "translation-batches" / f"{first['batch_id']}.json"
    )
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    (results_dir / f"{first['batch_id']}.result.json").write_text(
        json.dumps(
            [
                {
                    "id": unit["id"],
                    "translation": fake_translate(unit["source"]),
                }
                for unit in batch["units"]
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            _script("apply_translation_batch.py"),
            str(job_dir),
            "--results-dir",
            str(results_dir),
            "--model",
            MODEL,
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "串行写回 1 批" in proc.stdout
    assert "还缺" in proc.stdout
