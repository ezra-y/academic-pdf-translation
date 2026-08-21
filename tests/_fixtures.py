"""测试夹具：在临时目录里造一份最小但真实的作业。

夹具走的是生产脚本本身（init_job / plan_translation_batches），
不手工拼 job.json，避免测试和产品分叉。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "audit" / "tools"))

from fake_translate import fake_translate  # noqa: E402
from make_repro_paper import build as build_repro_paper  # noqa: E402

from _common import load_json, write_json  # noqa: E402
from init_job import initialize_job  # noqa: E402
from plan_translation_batches import (  # noqa: E402
    PLAN_FILE_NAME,
    plan_translation_batches,
)


def make_source_pdf(tmp_path: Path) -> Path:
    return build_repro_paper(tmp_path / "paper.pdf")


def make_job(
    tmp_path: Path,
    *,
    target_language: str = "zh-Hans",
    source_pdf: Path | None = None,
) -> Path:
    source = source_pdf or make_source_pdf(tmp_path)
    job_dir = tmp_path / "job"
    initialize_job(
        source,
        job_dir,
        target_language,
        "auto",
        False,
        "balanced",
        None,
        "pytest",
        None,
    )
    return job_dir.resolve()


def plan(
    job_dir: Path,
    *,
    reviewed: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """正式编排前先确认术语表；只有专门测这条门槛时才传 reviewed=False。"""

    if reviewed:
        set_terminology_reviewed(job_dir)
    return plan_translation_batches(job_dir, **kwargs)


def load_batch(job_dir: Path, batch_id: str = "batch-0001") -> dict[str, Any]:
    plan_data = load_json(job_dir / PLAN_FILE_NAME)
    entry = next(
        item
        for item in plan_data["batches"]
        if item["batch_id"] == batch_id
    )
    return load_json(job_dir / entry["file"])


def translated_results(batch: dict[str, Any]) -> list[dict[str, Any]]:
    """一批可信的目标语言译文：语言正确、锚点齐全。"""

    return [
        {"id": unit["id"], "translation": fake_translate(unit["source"])}
        for unit in batch["units"]
    ]


def set_terminology_reviewed(job_dir: Path, value: bool = True) -> None:
    path = job_dir / "translation.json"
    translation = load_json(path)
    translation["terminology_reviewed"] = value
    write_json(path, translation)
