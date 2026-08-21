"""字体准备顺序：全新作业不需要手工编辑 job.json。

单独运行：
    python3 -m pytest -q tests/test_font_preparation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixtures import (  # noqa: E402
    load_batch,
    make_job,
    plan,
    translated_results,
)

from _common import load_json, write_json  # noqa: E402
from apply_translation_batch import apply_translation_batch  # noqa: E402
from build_first_candidate import build_first_candidate  # noqa: E402
from font_preparation import (  # noqa: E402
    fonts_are_current,
    prepare_job_fonts,
)
from pre_render_audit import build_input_readiness_audit  # noqa: E402
from set_complex_content import set_complex_content  # noqa: E402
from validate_job import validate_job  # noqa: E402


def _finish_inputs(job_dir: Path) -> None:
    """把字体以外的其他输入补齐，让字体成为唯一变量。"""

    plan(job_dir)
    batch = load_batch(job_dir)
    apply_translation_batch(
        job_dir,
        batch["batch_id"],
        translated_results(batch),
    )
    set_complex_content(
        job_dir,
        [],
        confirmed_none=True,
        notes="合成测试论文，全部为规则正文。",
    )
    job = load_json(job_dir / "job.json")
    job["route"]["selected"] = job["route"]["recommended"]
    job["route"]["decision_reason"] = "合成测试论文，按推荐路线执行。"
    write_json(job_dir / "job.json", job)
    translation = load_json(job_dir / "translation.json")
    translation["terminology_reviewed"] = True
    write_json(job_dir / "translation.json", translation)
    inventory = load_json(job_dir / "figure_inventory.json")
    inventory["inventory_complete"] = True
    inventory["scope_note"] = "合成测试论文无图表。"
    write_json(job_dir / "figure_inventory.json", inventory)
    validate_job(job_dir, "translated", advance=True)


def test_initialize_job_freezes_real_font_files(tmp_path: Path) -> None:
    """初始化就把字体解析成磁盘上真实存在的文件，并记下哈希。"""

    job_dir = make_job(tmp_path)
    job = load_json(job_dir / "job.json")
    selected = job["quality"]["selected_fonts"]
    evidence = job["quality"]["selected_font_evidence"]
    assert selected, "初始化后 selected_fonts 不能为空"
    assert len(evidence) == len(selected)
    for path, record in zip(selected, evidence, strict=True):
        assert Path(path).is_file()
        assert record["path"] == path
        assert len(record["sha256"]) == 64
    assert fonts_are_current(job) is True


def test_fresh_job_resolves_fonts_before_readiness_audit(
    tmp_path: Path,
) -> None:
    """全新作业只调用统一入口就能通过输入就绪检查，不用手工改 JSON。"""

    job_dir = make_job(tmp_path)
    # 把字体清空，模拟“从未解析过”的全新作业。
    job = load_json(job_dir / "job.json")
    job["quality"]["selected_fonts"] = []
    job["quality"].pop("selected_font_evidence", None)
    write_json(job_dir / "job.json", job)

    blocked = build_input_readiness_audit(job_dir)
    assert any(
        issue["code"] == "SELECTED_FONTS_MISSING"
        for issue in blocked["issues"]
    ), "清空字体后输入就绪检查本应拦截，否则这个测试没有意义"

    _finish_inputs(job_dir)
    job = load_json(job_dir / "job.json")
    job["quality"]["selected_fonts"] = []
    job["quality"].pop("selected_font_evidence", None)
    write_json(job_dir / "job.json", job)

    report = build_first_candidate(job_dir)
    codes = [issue.get("code") for issue in report.get("issues", [])]
    assert "SELECTED_FONTS_MISSING" not in codes
    assert report["status"] != "BLOCKED_BEFORE_PREFLIGHT"
    assert load_json(job_dir / "job.json")["quality"]["selected_fonts"]


def test_changed_font_file_is_detected_and_reselected(tmp_path: Path) -> None:
    """字体记录被改动后能检测失效，并且能重新解析。"""

    job_dir = make_job(tmp_path)
    job = load_json(job_dir / "job.json")
    job["quality"]["selected_font_evidence"][0]["sha256"] = "0" * 64
    write_json(job_dir / "job.json", job)

    assert fonts_are_current(load_json(job_dir / "job.json")) is False
    audit = build_input_readiness_audit(job_dir)
    assert any(
        issue["code"] == "SELECTED_FONT_FILE_CHANGED"
        for issue in audit["issues"]
    )

    report = prepare_job_fonts(job_dir)
    assert report["status"] == "reselected"
    assert fonts_are_current(load_json(job_dir / "job.json")) is True
    healed = build_input_readiness_audit(job_dir)
    assert not any(
        issue["code"].startswith("SELECTED_FONT")
        for issue in healed["issues"]
    )


def test_prepare_is_idempotent(tmp_path: Path) -> None:
    """字体没变时不重复解析，避免每次生成都重扫字体目录。"""

    job_dir = make_job(tmp_path)
    assert prepare_job_fonts(job_dir)["status"] == "unchanged"
