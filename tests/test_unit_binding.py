"""翻译单元绑定：每段译文都得知道自己属于哪个元素。

单独运行：
    python3 -m pytest -q tests/test_unit_binding.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from academic_pdf_translation.analysis.source_elements import (  # noqa: E402
    build_inventory,
)
from academic_pdf_translation.analysis.unit_binding import (  # noqa: E402
    NON_BODY_FLOW_ROLES,
    ROLE_FIGURE_CAPTION,
    ROLE_FIGURE_LABEL,
    ROLE_FOOTNOTE,
    ROLE_TABLE_CELL,
    ROLE_TABLE_TITLE,
    bind_units,
    role_for,
    validate_payload_sources,
)
from academic_pdf_translation.contracts.enums import ElementType  # noqa: E402
from academic_pdf_translation.contracts.models import (  # noqa: E402
    SourceElement,
    SourceElementInventory,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _skip_without_real_job() -> tuple[dict, list[dict]]:
    structure = REAL_JOB / "source_structure.json"
    units = REAL_JOB / "source_units.json"
    if not structure.is_file() or not units.is_file():
        pytest.skip(
            "缺少真实论文作业 benchmarks/jobs-real/real-translation；"
            "真实论文受版权保护不入库"
        )
    return (
        json.loads(structure.read_text(encoding="utf-8")),
        json.loads(units.read_text(encoding="utf-8"))["units"],
    )


def _real_binding():
    structure, units = _skip_without_real_job()
    inventory = build_inventory(structure, pymupdf_version="1")
    return inventory, units, bind_units(units, inventory)


# --- 角色判定 ---------------------------------------------------------------


def test_role_for_each_element_type() -> None:
    cases = {
        ElementType.TABLE: ROLE_TABLE_CELL,
        ElementType.FOOTNOTE: ROLE_FOOTNOTE,
        ElementType.VECTOR_FIGURE: ROLE_FIGURE_LABEL,
    }
    for element_type, expected in cases.items():
        element = SourceElement(id="x", page=1, type=element_type)
        assert role_for(element) == expected


def test_caption_role_splits_figure_and_table() -> None:
    figure_caption = SourceElement(
        id="c1",
        page=1,
        type=ElementType.CAPTION,
        detail={"caption_kind": "figure"},
    )
    table_caption = SourceElement(
        id="c2",
        page=1,
        type=ElementType.CAPTION,
        detail={"caption_kind": "table"},
    )
    assert role_for(figure_caption) == ROLE_FIGURE_CAPTION
    assert role_for(table_caption) == ROLE_TABLE_TITLE


def test_embedded_label_role() -> None:
    label = SourceElement(
        id="l1",
        page=1,
        type=ElementType.UNKNOWN,
        detail={"role": "embedded-label"},
    )
    assert role_for(label) == ROLE_FIGURE_LABEL


# --- 绑定本身 ---------------------------------------------------------------


def test_no_orphan_translation_units() -> None:
    """真实论文里每段译文都要有归属。"""

    _, units, report = _real_binding()
    assert len(report.bindings) == len(units)
    assert report.orphan_units == []
    assert report.complete is True


def test_binding_prefers_exact_block_ids() -> None:
    """块 ID 是精确连接键，绝大多数单元应当走它。"""

    _, _, report = _real_binding()
    exact = [b for b in report.bindings if b.match == "source-block-id"]
    assert len(exact) >= len(report.bindings) * 0.9


def test_orphan_unit_is_reported_not_forced() -> None:
    """绑不上的单元如实报出来，不硬塞给某个元素。"""

    inventory = SourceElementInventory(
        source_sha256="a" * 64,
        page_count=1,
        elements=[
            SourceElement(
                id="p0001-body-001",
                page=1,
                type=ElementType.BODY,
                bbox=(50, 100, 500, 200),
                source_block_ids=[1],
            )
        ],
    )
    units = [
        {"id": "p0001-u0001", "page": 1, "source_block_ids": [1]},
        {
            "id": "p0009-u0001",
            "page": 9,
            "source_block_ids": [99],
            "source_bbox": [0, 0, 10, 10],
        },
    ]
    report = bind_units(units, inventory)
    assert report.orphan_units == ["p0009-u0001"]
    assert report.complete is False
    assert any("找不到归属元素" in problem for problem in report.problems)


def test_table_cells_have_translation_units() -> None:
    """表格里的文字必须绑到表格元素上。"""

    inventory, _, report = _real_binding()
    tables = [
        element
        for element in inventory.elements
        if element.type is ElementType.TABLE
    ]
    assert tables, "样本论文应当有表格"
    table_bindings = [
        binding
        for binding in report.bindings
        if binding.element_role == ROLE_TABLE_CELL
    ]
    assert table_bindings, "表格必须有绑定的翻译单元"
    for table in tables:
        assert table.translation_unit_ids, f"{table.id} 没有绑定任何单元"


def test_figure_labels_have_source_coordinates() -> None:
    """图内标签必须有原文坐标，否则没法一对一覆盖。"""

    inventory, units, report = _real_binding()
    by_id = {unit["id"]: unit for unit in units}
    labels = [
        binding
        for binding in report.bindings
        if binding.element_role == ROLE_FIGURE_LABEL
    ]
    assert labels, "密集矢量图应当有图内标签"
    for binding in labels:
        unit = by_id[binding.unit_id]
        assert unit.get("source_bbox"), f"{binding.unit_id} 缺少原文坐标"


def test_footnote_units_do_not_enter_body_flow() -> None:
    _, _, report = _real_binding()
    footnotes = [
        binding
        for binding in report.bindings
        if binding.element_role == ROLE_FOOTNOTE
    ]
    assert footnotes, "样本论文应当有脚注"
    for binding in footnotes:
        assert binding.element_role in NON_BODY_FLOW_ROLES


def test_existing_body_unit_ids_survive_migration() -> None:
    """绑定只做标注，不改任何已有单元 ID。"""

    inventory, units, report = _real_binding()
    before = [unit["id"] for unit in units]
    after = [binding.unit_id for binding in report.bindings]
    assert set(before) == set(after)
    # 再绑一次，ID 与归属都不变。
    second = bind_units(units, build_inventory(
        json.loads((REAL_JOB / "source_structure.json").read_text(encoding="utf-8")),
        pymupdf_version="1",
    ))
    assert [b.unit_id for b in second.bindings] == after
    assert [b.element_id for b in second.bindings] == [
        b.element_id for b in report.bindings
    ]


def test_every_bound_unit_belongs_to_exactly_one_element() -> None:
    _, _, report = _real_binding()
    seen: dict[str, str] = {}
    for binding in report.bindings:
        assert binding.unit_id not in seen, f"{binding.unit_id} 绑了两次"
        seen[binding.unit_id] = binding.element_id


# --- 载荷文字必须有来源 -----------------------------------------------------


def test_complex_payload_text_requires_unit_id() -> None:
    """载荷里的中文必须来自某个翻译单元，不能凭空写。"""

    problems = validate_payload_sources(
        [{"translation": "这是我自己编的图内说明。"}],
        {"p0001-u0001"},
    )
    assert problems
    assert "没有绑定 translation_unit_id" in problems[0]


def test_payload_text_with_unknown_unit_is_rejected() -> None:
    problems = validate_payload_sources(
        [{"translation": "有编号但对不上", "translation_unit_id": "p9999-u9999"}],
        {"p0001-u0001"},
    )
    assert problems
    assert "不存在的单元" in problems[0]


def test_payload_text_with_valid_unit_passes() -> None:
    assert (
        validate_payload_sources(
            [{"translation": "有来源的说明", "translation_unit_id": "p0001-u0001"}],
            {"p0001-u0001"},
        )
        == []
    )


def test_empty_payload_text_is_ignored() -> None:
    assert validate_payload_sources([{"translation": "   "}], set()) == []


def test_real_job_unsourced_overlay_text_is_detected() -> None:
    """真实作业里手写的图内浮层说明必须被查出来。"""

    complex_path = REAL_JOB / "complex_content.json"
    if not complex_path.is_file():
        pytest.skip("缺少真实论文作业的复杂载荷")
    _, _, report = _real_binding()
    regions = []
    for item in json.loads(complex_path.read_text(encoding="utf-8")).get(
        "items", []
    ):
        payload = item.get("payload")
        if isinstance(payload, dict):
            regions.extend(
                entry
                for entry in payload.get("regions", [])
                if isinstance(entry, dict)
            )
    if not regions:
        pytest.skip("该作业没有图像载荷区域")
    problems = validate_payload_sources(
        regions, {binding.unit_id for binding in report.bindings}
    )
    assert problems, "手写的图内说明没有来源单元，必须被查出来"
