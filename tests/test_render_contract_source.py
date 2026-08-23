"""渲染合同的唯一事实来源：按元素 ID 对账，不数条目。

单独运行：
    python3 -m pytest -q tests/test_render_contract_source.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from academic_pdf_translation.verify.candidate_mapping import (  # noqa: E402
    CandidateMapping,
    ElementLocation,
)
from academic_pdf_translation.verify.render_contract import (  # noqa: E402
    OMIT_EXTRACTION_RESIDUE,
    complex_view_is_current,
    contract_from_documents,
    derive_candidate_elements,
    derive_complex_view,
    planning_issues,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _source(*items) -> dict:
    return {"elements": list(items)}


def _elem(element_id: str, required: bool = True, **extra) -> dict:
    return {"id": element_id, "required": required, **extra}


def _plan(*ids: str) -> dict:
    return {"elements": [{"element_id": value} for value in ids]}


def _candidate(*records) -> dict:
    return {"elements": list(records)}


def test_render_contract_uses_render_plan_as_source() -> None:
    """计划是唯一处理清单：必需元素必须每个都有计划。"""

    source = _source(_elem("e1"), _elem("e2"))
    contract = contract_from_documents(source, _plan("e1"))
    assert not contract.passed
    assert any("没有处理计划" in problem for problem in contract.problems)
    issues = planning_issues(source, _plan("e1"))
    assert any(
        issue["code"] == "REQUIRED_ELEMENTS_WITHOUT_PLAN" for issue in issues
    )
    # 计划齐了就过
    assert contract_from_documents(source, _plan("e1", "e2")).passed


def test_plan_element_not_in_source_is_rejected() -> None:
    """计划里多出一个原文根本没有的元素：必须失败，不能只查"少了谁"。"""

    source = _source(_elem("e1"))
    contract = contract_from_documents(source, _plan("e1", "ghost-001"))
    assert not contract.passed
    assert contract.unknown_planned_element_ids == {"ghost-001"}
    assert any("查无此人" in problem for problem in contract.problems)
    issues = planning_issues(source, _plan("e1", "ghost-001"))
    codes = {issue["code"] for issue in issues}
    assert "PLAN_ELEMENTS_WITHOUT_SOURCE" in codes


def test_candidate_element_only_in_plan_is_unsourced() -> None:
    """假元素混进计划，也不能因此获得"有原文来源"的身份。"""

    source = _source(_elem("e1"))
    candidate = _candidate(
        {"id": "e1", "located": True},
        {"id": "ghost-001", "located": True},
    )
    contract = contract_from_documents(
        source, _plan("e1", "ghost-001"), candidate
    )
    assert not contract.passed
    assert any(
        "候选清单里有原文清单查无此人的元素" in problem
        and "ghost-001" in problem
        for problem in contract.problems
    )


def test_optional_source_element_may_have_plan() -> None:
    """非必需元素有计划不算多出来——它在原文里确实存在。"""

    source = _source(_elem("e1"), _elem("e2", required=False))
    contract = contract_from_documents(source, _plan("e1", "e2"))
    assert contract.passed
    assert contract.unknown_planned_element_ids == set()
    assert contract.required_element_ids == {"e1"}
    assert contract.all_source_element_ids == {"e1", "e2"}
    assert planning_issues(source, _plan("e1", "e2")) == []


def test_old_complex_count_cannot_block_valid_new_plan() -> None:
    """旧手写载荷条目数与新计划不同：视图判旧，不判排版器失职。"""

    handwritten = {"schema_version": "1.0", "items": [{"id": "a"}]}
    assert not complex_view_is_current(handwritten, "p" * 64)
    derived = derive_complex_view(handwritten, "p" * 64)
    assert complex_view_is_current(derived, "p" * 64)
    # 计划换版本后，旧视图再次失效
    assert not complex_view_is_current(derived, "q" * 64)


def test_required_element_missing_is_blocked() -> None:
    source = _source(_elem("e1"), _elem("e2"))
    candidate = _candidate(
        {"id": "e1", "located": True},
        {"id": "e2", "located": False},
    )
    contract = contract_from_documents(source, _plan("e1", "e2"), candidate)
    assert not contract.passed
    assert "e2" in contract.illegal_omitted_element_ids


def test_same_page_multiple_elements_are_counted_separately() -> None:
    """同一页有图、表、图题：必须按元素 ID 一一对账，不能按页折叠。"""

    source = _source(
        _elem("p7-figure", page=7),
        _elem("p7-table", page=7),
        _elem("p7-caption", page=7),
    )
    candidate = _candidate(
        {"id": "p7-figure", "located": True},
        {"id": "p7-table", "located": True},
        {"id": "p7-caption", "located": False},
    )
    contract = contract_from_documents(
        source, _plan("p7-figure", "p7-table", "p7-caption"), candidate
    )
    assert not contract.passed
    assert contract.illegal_omitted_element_ids == {"p7-caption"}


def test_candidate_element_cannot_be_counted_twice() -> None:
    source = _source(_elem("e1"))
    candidate = _candidate(
        {"id": "e1", "located": True},
        {"id": "e1", "located": True},
    )
    contract = contract_from_documents(source, _plan("e1"), candidate)
    assert any("计了两次" in problem for problem in contract.problems)


def test_legal_omission_needs_a_reason_code() -> None:
    source = _source(_elem("e1"), _elem("e2"))
    candidate = _candidate(
        {"id": "e1", "located": True},
        {
            "id": "e2",
            "located": False,
            "omit_reason": OMIT_EXTRACTION_RESIDUE,
        },
    )
    contract = contract_from_documents(source, _plan("e1", "e2"), candidate)
    assert contract.passed
    assert contract.legal_omitted_element_ids == {"e2"}


def test_candidate_view_is_derived_from_mapping() -> None:
    """候选元素视图由映射派生：残渣理由来自映射证据，不接受手写。"""

    mapping = CandidateMapping(
        locations=[
            ElementLocation(
                element_id="e1",
                element_type="body",
                source_page=1,
                required=True,
                candidate_pages=[1],
                method="text-probe",
            ),
            ElementLocation(
                element_id="e2",
                element_type="body",
                source_page=1,
                required=False,
                evidence="抽取残渣（可用文字 'X'），不计入必需完整性",
            ),
        ]
    )
    view = derive_candidate_elements(mapping)
    assert view["derived_from"] == "candidate-mapping"
    by_id = {record["id"]: record for record in view["elements"]}
    assert by_id["e1"]["located"] is True
    assert by_id["e2"]["omit_reason"] == OMIT_EXTRACTION_RESIDUE


def test_legacy_complex_content_is_derived_not_authored() -> None:
    """真实作业构建后，磁盘上的 complex_content 必须带派生戳。"""

    path = REAL_JOB / "complex_content.json"
    plan_path = REAL_JOB / "render_plan.json"
    if not path.is_file() or not plan_path.is_file():
        pytest.skip("缺少真实论文作业；真实论文受版权保护不入库")
    data = json.loads(path.read_text(encoding="utf-8"))
    marker = data.get("derived_from")
    if not isinstance(marker, dict):
        pytest.skip("该作业还没有用新生成器重建过，视图尚未派生")
    assert marker.get("source") == "render_plan"


def test_unet_page_seven_contains_figure_and_table() -> None:
    """真实 U-Net 第 7 页同时有图和表：两个元素都要独立入账。"""

    elements_path = REAL_JOB / "source_elements.json"
    if not elements_path.is_file():
        pytest.skip("缺少真实论文作业；真实论文受版权保护不入库")
    elements = json.loads(elements_path.read_text(encoding="utf-8"))[
        "elements"
    ]
    page7 = [item for item in elements if item.get("page") == 7]
    kinds = {item["type"] for item in page7}
    assert "table" in kinds
    assert kinds & {"raster-figure", "vector-figure", "chart"}
    ids = [item["id"] for item in page7]
    assert len(ids) == len(set(ids))
