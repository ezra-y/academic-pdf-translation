"""文档里的数字必须来自实测，不能自己漂走。

README 写"0/6 可交付"，基准结果就得是 0/6。这类数字最容易在代码变好之后
留在原地，然后变成谎话——所以让测试盯着它。

单独运行：
    python3 -m pytest -q tests/test_documentation_claims.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESULT_FILE = ROOT / "benchmarks" / "results" / "first-delivery.json"
REPORT = ROOT / "benchmarks" / "results" / "first-delivery.md"
README = ROOT / "README.md"
README_EN = ROOT / "README_EN.md"
PIPELINE_DOC = ROOT / "references" / "element-pipeline.md"
VALIDATION = ROOT / "references" / "validation.md"
CHANGELOG = ROOT / "CHANGELOG.md"

#: 声称做得到某件事的说法。写下它们就得有实测撑着。
CLAIM_WORDS = ("首版成功率", "可交付", "deliverable")


def _summary() -> dict:
    if not RESULT_FILE.is_file():
        pytest.skip("还没有跑过首版交付基准")
    return json.loads(RESULT_FILE.read_text(encoding="utf-8"))


def _delivered_count(summary: dict) -> int:
    return sum(
        counts.get("delivered", 0)
        for counts in summary["by_translation_source"].values()
    )


# --- 数字对得上 -------------------------------------------------------------


def test_the_readme_states_the_measured_delivery_rate() -> None:
    """README 写的比例必须等于基准算出来的。"""

    summary = _summary()
    delivered = _delivered_count(summary)
    total = summary["case_count"]
    text = README.read_text(encoding="utf-8")
    assert f"{delivered}/{total}" in text, (
        f"README 应当写明实测的 {delivered}/{total}"
    )


def test_the_english_readme_agrees_with_the_chinese_one() -> None:
    summary = _summary()
    assert _delivered_count(summary) == 0, (
        "已经有可交付的样本了，两份 README 的措辞都要一起改"
    )
    english = README_EN.read_text(encoding="utf-8")
    assert "Zero of six" in english or "0/6" in english


def test_the_report_lists_every_case_it_measured() -> None:
    summary = _summary()
    report = REPORT.read_text(encoding="utf-8")
    for case in summary["cases"]:
        assert case["case_id"] in report, case["case_id"]


def test_the_report_counts_match_the_result_file() -> None:
    summary = _summary()
    report = REPORT.read_text(encoding="utf-8")
    for status in ("blocked", "handover", "delivered"):
        count = sum(
            counts.get(status, 0)
            for counts in summary["by_translation_source"].values()
        )
        if count:
            assert str(count) in report, f"{status} 应当是 {count}"


# --- 边界写清楚了 -----------------------------------------------------------


def test_the_readme_says_translation_cost_is_unmeasured() -> None:
    """禁止编造耗时与 Token。没测就得写没测。"""

    text = README.read_text(encoding="utf-8")
    assert "未测量" in text


def test_the_english_readme_says_the_same() -> None:
    assert "unmeasured" in README_EN.read_text(encoding="utf-8").lower()


def test_the_pipeline_doc_lists_its_limits() -> None:
    text = PIPELINE_DOC.read_text(encoding="utf-8")
    for limit in ("栅格", "xref", "未验证", "合成译文"):
        assert limit in text, limit


def test_the_validation_doc_separates_the_two_verdicts() -> None:
    """生成器自评和核查结论必须分开写，混为一谈就是虚报。"""

    text = VALIDATION.read_text(encoding="utf-8")
    assert "READY_TO_REGISTER" in text
    assert "delivered" in text
    assert "自评" in text


# --- 文档指向的东西真的存在 -------------------------------------------------


def _local_links(text: str, base: Path) -> list[Path]:
    targets = []
    for match in re.finditer(r"\]\(([^)#]+)\)", text):
        target = match.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        targets.append((base / target).resolve())
    return targets


@pytest.mark.parametrize(
    "document",
    [README, README_EN, PIPELINE_DOC, VALIDATION, REPORT, CHANGELOG],
    ids=lambda path: path.name,
)
def test_every_local_link_resolves(document: Path) -> None:
    if not document.is_file():
        pytest.skip(f"{document.name} 不存在")
    missing = [
        target
        for target in _local_links(
            document.read_text(encoding="utf-8"), document.parent
        )
        if not target.exists()
    ]
    assert missing == [], [str(path) for path in missing]


def test_the_pipeline_doc_names_only_modules_that_exist() -> None:
    """文档里点名的模块必须真的存在。

    文档同时点名包内模块（``verify/repair.py``）和仓库里的脚本
    （``scripts/build_candidate.py``），两处都要查。
    """

    text = PIPELINE_DOC.read_text(encoding="utf-8")
    package = ROOT / "academic_pdf_translation"
    named = set(re.findall(r"`([a-z_]+)/([a-z_]+)\.py`", text))
    assert named, "文档应当点名具体模块"
    for folder, module in sorted(named):
        candidates = [
            package / folder / f"{module}.py",
            ROOT / folder / f"{module}.py",
        ]
        assert any(path.is_file() for path in candidates), (
            f"{folder}/{module}.py 不存在"
        )


def test_the_pipeline_doc_names_only_real_repair_actions() -> None:
    from academic_pdf_translation.verify.repair import (
        ALLOWED_ACTIONS,
        FORBIDDEN_ACTIONS,
        MAX_REPAIR_ROUNDS,
    )

    text = PIPELINE_DOC.read_text(encoding="utf-8")
    assert f"MAX_REPAIR_ROUNDS = {MAX_REPAIR_ROUNDS}" in text
    for action in ALLOWED_ACTIONS | FORBIDDEN_ACTIONS:
        assert action in text, action


def test_the_pipeline_doc_exit_codes_match_the_cli() -> None:
    from academic_pdf_translation.delivery.first_delivery import (
        STATUS_BLOCKED,
        STATUS_DELIVERED,
        STATUS_HANDOVER,
    )

    text = PIPELINE_DOC.read_text(encoding="utf-8")
    for status in (STATUS_DELIVERED, STATUS_HANDOVER, STATUS_BLOCKED):
        assert f"`{status}`" in text, status


# --- 变更记录 ---------------------------------------------------------------


def test_the_changelog_version_matches_the_package() -> None:
    """版本号散在三个地方，改一处忘两处是迟早的事。"""

    import json as _json

    plugin = _json.loads(
        (ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    heading = re.search(
        r"^##\s+(\d+\.\d+\.\d+)\s*$",
        CHANGELOG.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert heading is not None, "CHANGELOG 里没有版本小节"
    assert heading.group(1) == plugin["version"]


def test_the_changelog_states_the_measured_delivery_rate() -> None:
    summary = _summary()
    delivered = _delivered_count(summary)
    text = CHANGELOG.read_text(encoding="utf-8")
    assert f"{delivered}/{summary['case_count']}" in text


def test_the_changelog_lists_the_known_limits() -> None:
    """新增能力要写，做不到的也要写。只写一半就是广告。"""

    text = CHANGELOG.read_text(encoding="utf-8")
    assert "已知限制" in text
    for limit in ("栅格", "xref", "未验证", "合成译文"):
        assert limit in text, limit
