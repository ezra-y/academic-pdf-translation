"""首版交付基准：结果分开统计，跑不动的写"未验证"。

单独运行：
    python3 -m pytest -q tests/test_first_delivery_benchmark.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

import pytest  # noqa: E402

from run_first_delivery_benchmark import (  # noqa: E402
    REQUIRED_COVERAGE_FIELDS,
    STATUS_UNVERIFIED,
    TRANSLATION_REAL,
    TRANSLATION_SYNTHETIC,
    CaseResult,
    format_report,
    refresh_translation_coverage,
    summarize,
)

ROOT = Path(__file__).resolve().parent.parent
RESULT_FILE = ROOT / "benchmarks" / "results" / "first-delivery.json"


def _case(case_id: str, status: str, source: str) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        source_sha256="0" * 64,
        source_pages=10,
        translation_source=source,
        status=status,
    )


# --- 分开统计 ---------------------------------------------------------------


def test_real_and_synthetic_translations_are_counted_separately() -> None:
    """混在一张表里报，数字就没意义了。"""

    summary = summarize(
        [
            _case("a", "delivered", TRANSLATION_REAL),
            _case("b", "blocked", TRANSLATION_SYNTHETIC),
            _case("c", "blocked", TRANSLATION_SYNTHETIC),
        ]
    )
    assert summary["by_translation_source"][TRANSLATION_REAL] == {"delivered": 1}
    assert summary["by_translation_source"][TRANSLATION_SYNTHETIC] == {
        "blocked": 2
    }


def test_unverified_cases_are_listed_by_name() -> None:
    """跑不动的要点名，不能悄悄从分母里消失。"""

    summary = summarize(
        [
            _case("a", "delivered", TRANSLATION_REAL),
            _case("b", STATUS_UNVERIFIED, TRANSLATION_REAL),
        ]
    )
    assert summary["unverified"] == ["b"]
    assert summary["case_count"] == 2


def test_the_report_labels_synthetic_translations_as_such() -> None:
    report = format_report(
        summarize([_case("a", "blocked", TRANSLATION_SYNTHETIC)])
    )
    assert "不代表译文质量" in report


def test_the_report_prints_the_note_for_unverified_cases() -> None:
    case = _case("a", STATUS_UNVERIFIED, TRANSLATION_REAL)
    case.note = "超过 1800 秒未完成，记为未验证"
    report = format_report(summarize([case]))
    assert "未验证" in report
    assert "1800" in report


# --- 覆盖率重算 -------------------------------------------------------------


def test_a_complete_coverage_is_left_alone(tmp_path: Path) -> None:
    """在一份已经算对的作业上重算一遍，反而可能算错。"""

    coverage = dict.fromkeys(REQUIRED_COVERAGE_FIELDS, 7)
    coverage["source_units_total"] = 1
    payload = {"units": [], "coverage": coverage}
    path = tmp_path / "translation.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert refresh_translation_coverage(tmp_path) == ""
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["coverage"] == coverage


def test_a_stale_coverage_is_recomputed(tmp_path: Path) -> None:
    payload = {
        "target_language": "zh-Hans",
        "units": [
            {
                "id": "u1",
                "source": "Deep neural networks segment membranes.",
                "translation": "深度神经网络用于分割细胞膜。",
            }
        ],
        "coverage": {"source_units_total": 1},
    }
    path = tmp_path / "translation.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert refresh_translation_coverage(tmp_path) == ""
    coverage = json.loads(path.read_text(encoding="utf-8"))["coverage"]
    for field in REQUIRED_COVERAGE_FIELDS:
        assert field in coverage
    assert coverage["source_units_total"] == 1


def test_the_retained_source_is_fed_into_the_recomputation(
    tmp_path: Path,
) -> None:
    """保留原文的单元算不算"已验证"取决于保留区域。

    不把 retained_source.json 传进去，算出来的数会和校验器的独立重算对不上。
    """

    payload = {
        "target_language": "zh-Hans",
        "units": [
            {
                "id": "u1",
                "source": "Bioinformatics",
                "translation": "",
                "keep_source_code": "proper-noun",
            }
        ],
        "coverage": {"source_units_total": 1},
    }
    (tmp_path / "translation.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "retained_source.json").write_text(
        json.dumps({"schema_version": "1.0", "items": [], "regions": []}),
        encoding="utf-8",
    )
    assert refresh_translation_coverage(tmp_path) == ""


def test_a_broken_translation_file_reports_instead_of_crashing(
    tmp_path: Path,
) -> None:
    (tmp_path / "translation.json").write_text(
        json.dumps({"units": "不是列表", "coverage": {}}), encoding="utf-8"
    )
    problem = refresh_translation_coverage(tmp_path)
    assert problem
    assert "重算" in problem


# --- 已落盘的结果 -----------------------------------------------------------


def test_the_stored_result_keeps_no_copyrighted_text() -> None:
    """论文受版权保护，结果文件只许存哈希、页数和派生结论。"""

    if not RESULT_FILE.is_file():
        pytest.skip("还没有跑过首版交付基准")
    import re

    text = RESULT_FILE.read_text(encoding="utf-8")
    long_latin = re.findall(r"[A-Za-z][A-Za-z ,.'-]{60,}", text)
    assert long_latin == [], long_latin[:2]


def test_the_stored_result_records_a_hash_for_every_paper() -> None:
    if not RESULT_FILE.is_file():
        pytest.skip("还没有跑过首版交付基准")
    summary = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    assert summary["case_count"] == len(summary["cases"])
    for case in summary["cases"]:
        assert len(case["source_sha256"]) == 64
        assert case["source_pages"] > 0
        assert case["translation_source"] in {
            TRANSLATION_REAL,
            TRANSLATION_SYNTHETIC,
        }


def test_the_stored_result_does_not_claim_more_than_it_ran() -> None:
    """每一篇的状态都得是跑出来的，没有"默认通过"这一档。"""

    if not RESULT_FILE.is_file():
        pytest.skip("还没有跑过首版交付基准")
    summary = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    allowed = {"delivered", "handover", "blocked", STATUS_UNVERIFIED}
    for case in summary["cases"]:
        assert case["status"] in allowed, case
        if case["status"] == "delivered":
            assert case["problem_count"] == 0, case
