"""元素纠正：能改错，但不能借着"改"把元素弄没。

单独运行：
    python3 -m pytest -q tests/test_element_overrides.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from academic_pdf_translation.analysis.element_overrides import (  # noqa: E402
    ACTION_LINK,
    ACTION_MERGE,
    ACTION_MOVE_BBOX,
    ACTION_OMIT,
    ACTION_RETYPE,
    ACTION_SPLIT,
    ACTION_UNLINK,
    OMIT_CODES,
    ElementOverride,
    OverrideError,
    apply_override,
    load_overrides,
    save_overrides,
)
from academic_pdf_translation.contracts.enums import ElementType  # noqa: E402
from academic_pdf_translation.contracts.models import (  # noqa: E402
    SourceElement,
    SourceElementInventory,
)


def _inventory() -> SourceElementInventory:
    return SourceElementInventory(
        source_sha256="a" * 64,
        page_count=1,
        elements=[
            SourceElement(
                id="p0001-table-001",
                page=1,
                type=ElementType.TABLE,
                bbox=(50, 100, 500, 300),
            ),
            SourceElement(
                id="p0001-body-001",
                page=1,
                type=ElementType.BODY,
                bbox=(50, 320, 500, 400),
                source_block_ids=[7],
            ),
            SourceElement(
                id="p0001-body-002",
                page=1,
                type=ElementType.BODY,
                bbox=(50, 410, 500, 480),
                source_block_ids=[8],
            ),
            SourceElement(
                id="p0001-furniture-001",
                page=1,
                type=ElementType.PAGE_NUMBER,
                bbox=(290, 760, 310, 775),
            ),
            SourceElement(
                id="p0001-figure-001",
                page=1,
                type=ElementType.VECTOR_FIGURE,
                bbox=(50, 500, 500, 700),
            ),
        ],
    )


def _override(**kwargs) -> ElementOverride:
    base = {
        "reason": "自动识别把它认错了",
        "author": "tester",
    }
    base.update(kwargs)
    return ElementOverride(**base)


def test_required_element_cannot_be_deleted() -> None:
    """必需元素不能被省略。"""

    inventory = _inventory()
    with pytest.raises(OverrideError) as excinfo:
        apply_override(
            _override(
                action=ACTION_OMIT,
                element_id="p0001-table-001",
                omit_code="decorative-rule",
            ),
            inventory,
        )
    assert "必需元素" in str(excinfo.value)
    assert inventory.by_id("p0001-table-001") is not None


@pytest.mark.parametrize(
    "element_id",
    ["p0001-table-001", "p0001-body-001", "p0001-figure-001"],
)
def test_no_required_type_can_be_omitted(element_id: str) -> None:
    inventory = _inventory()
    for code in OMIT_CODES:
        with pytest.raises(OverrideError):
            apply_override(
                _override(
                    action=ACTION_OMIT,
                    element_id=element_id,
                    omit_code=code,
                ),
                _inventory(),
            )
    assert inventory.by_id(element_id) is not None


def test_nonsemantic_element_requires_structured_code() -> None:
    """省略必须给固定代码；自由文字理由单独无效。"""

    inventory = _inventory()
    with pytest.raises(OverrideError) as excinfo:
        apply_override(
            _override(
                action=ACTION_OMIT,
                element_id="p0001-furniture-001",
                reason="这个不重要，跳过就行",
            ),
            inventory,
        )
    assert "omit_code" in str(excinfo.value)

    apply_override(
        _override(
            action=ACTION_OMIT,
            element_id="p0001-furniture-001",
            omit_code="page-number",
        ),
        inventory,
    )
    element = inventory.by_id("p0001-furniture-001")
    assert element is not None
    assert element.detail["omitted"] is True
    assert element.detail["omit_code"] == "page-number"


def test_retype_cannot_smuggle_a_required_element_into_furniture() -> None:
    """不能把必需元素改判成可省略类型来绕过清单。"""

    with pytest.raises(OverrideError) as excinfo:
        apply_override(
            _override(
                action=ACTION_RETYPE,
                element_id="p0001-figure-001",
                new_type=ElementType.WATERMARK.value,
            ),
            _inventory(),
        )
    assert "绕过结构对账" in str(excinfo.value)


def test_retype_between_content_types_is_allowed() -> None:
    inventory = _inventory()
    apply_override(
        _override(
            action=ACTION_RETYPE,
            element_id="p0001-body-001",
            new_type=ElementType.CAPTION.value,
        ),
        inventory,
    )
    element = inventory.by_id("p0001-body-001")
    assert element is not None
    assert element.type is ElementType.CAPTION
    assert element.detail["original_type"] == ElementType.BODY.value


def test_split_preserves_parent_history() -> None:
    inventory = _inventory()
    apply_override(
        _override(
            action=ACTION_SPLIT,
            element_id="p0001-body-001",
            parts=[
                {"type": ElementType.BODY.value, "bbox": [50, 320, 500, 360]},
                {"type": ElementType.CAPTION.value, "bbox": [50, 362, 500, 400]},
            ],
        ),
        inventory,
    )
    assert inventory.by_id("p0001-body-001") is None
    children = [
        element
        for element in inventory.elements
        if element.detail.get("parent_element_id") == "p0001-body-001"
    ]
    assert len(children) == 2
    for child in children:
        assert child.id.startswith("p0001-body-001-part")
        assert child.detail["override_history"], "子元素必须带着纠正历史"


def test_split_needs_at_least_two_parts() -> None:
    with pytest.raises(OverrideError):
        apply_override(
            _override(
                action=ACTION_SPLIT,
                element_id="p0001-body-001",
                parts=[{"type": ElementType.BODY.value, "bbox": [1, 2, 3, 4]}],
            ),
            _inventory(),
        )


def test_merge_preserves_all_source_blocks() -> None:
    inventory = _inventory()
    apply_override(
        _override(
            action=ACTION_MERGE,
            element_id="p0001-body-001",
            merge_with=["p0001-body-002"],
        ),
        inventory,
    )
    merged = inventory.by_id("p0001-body-001")
    assert merged is not None
    assert merged.source_block_ids == [7, 8]
    # 原 ID 必须留在历史里，随时能追回去。
    assert "p0001-body-002" in merged.detail["merged_element_ids"]
    assert inventory.by_id("p0001-body-002") is None
    assert merged.bbox == (50, 320, 500, 480)


def test_merge_across_pages_is_rejected() -> None:
    inventory = _inventory()
    inventory.elements.append(
        SourceElement(
            id="p0002-body-001",
            page=2,
            type=ElementType.BODY,
            bbox=(50, 100, 500, 200),
        )
    )
    with pytest.raises(OverrideError) as excinfo:
        apply_override(
            _override(
                action=ACTION_MERGE,
                element_id="p0001-body-001",
                merge_with=["p0002-body-001"],
            ),
            inventory,
        )
    assert "同一页" in str(excinfo.value)


def test_move_bbox_keeps_the_original() -> None:
    inventory = _inventory()
    apply_override(
        _override(
            action=ACTION_MOVE_BBOX,
            element_id="p0001-figure-001",
            bbox=[40, 490, 520, 710],
        ),
        inventory,
    )
    element = inventory.by_id("p0001-figure-001")
    assert element is not None
    assert element.bbox == (40, 490, 520, 710)
    assert element.detail["original_bbox"] == [50, 500, 500, 700]


def test_link_and_unlink_a_caption() -> None:
    inventory = _inventory()
    apply_override(
        _override(
            action=ACTION_LINK,
            element_id="p0001-body-001",
            relation="captions-for",
            target_id="p0001-figure-001",
        ),
        inventory,
    )
    element = inventory.by_id("p0001-body-001")
    assert element is not None
    assert element.relations["captions-for"] == ["p0001-figure-001"]

    apply_override(
        _override(
            action=ACTION_UNLINK,
            element_id="p0001-body-001",
            relation="captions-for",
            target_id="p0001-figure-001",
        ),
        inventory,
    )
    assert element.relations["captions-for"] == []


def test_every_override_is_traceable() -> None:
    """每次纠正都要能追溯：谁、什么时候、为什么、依据是什么。"""

    inventory = _inventory()
    override = _override(
        action=ACTION_MOVE_BBOX,
        element_id="p0001-figure-001",
        bbox=[40, 490, 520, 710],
        author="claude-opus-5",
        reason="聚类漏掉了右侧的箭头",
        evidence={"page": "1", "measured_bbox": "40,490,520,710"},
    )
    apply_override(override, inventory)
    history = inventory.by_id("p0001-figure-001").detail["override_history"]
    assert len(history) == 1
    record = history[0]
    assert record["author"] == "claude-opus-5"
    assert record["reason"]
    assert record["recorded_at"]
    assert record["evidence"]["page"] == "1"


def test_missing_reason_or_author_is_rejected() -> None:
    for kwargs in ({"reason": ""}, {"author": ""}):
        override = _override(
            action=ACTION_MOVE_BBOX,
            element_id="p0001-figure-001",
            bbox=[1, 2, 3, 4],
            **kwargs,
        )
        with pytest.raises(OverrideError):
            apply_override(override, _inventory())


def test_unknown_action_is_rejected() -> None:
    with pytest.raises(OverrideError):
        apply_override(
            _override(action="delete", element_id="p0001-table-001"),
            _inventory(),
        )


def test_overrides_round_trip_on_disk(tmp_path: Path) -> None:
    overrides = [
        _override(
            action=ACTION_OMIT,
            element_id="p0001-furniture-001",
            omit_code="page-number",
        )
    ]
    save_overrides(tmp_path, overrides)
    loaded = load_overrides(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].omit_code == "page-number"
    assert loaded[0].element_id == "p0001-furniture-001"
