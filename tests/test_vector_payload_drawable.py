"""vector-rebuild 载荷必须画得出东西，画不出来就停下。

真实样本上出过这样一件事：手写的直方图载荷用了渲染器不认识的键，
候选里一根柱子都没画，构建、QA、对账却全绿。这里把那条静默路径钉住。

单独运行：
    python3 -m pytest -q tests/test_vector_payload_drawable.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from academic_pdf_translation.render.flowables import (  # noqa: E402
    figure_payload_is_drawable,
)

from pre_render_audit import _vector_payload_issues  # noqa: E402

CODE = "VECTOR_REBUILD_PAYLOAD_NOT_DRAWABLE"


def _complex(payload: dict) -> dict:
    return {
        "items": [
            {
                "page": 6,
                "kind": "figure-with-text",
                "method": "vector-rebuild",
                "status": "ready",
                "payload": payload,
            }
        ]
    }


def test_a_payload_the_renderer_cannot_draw_is_rejected() -> None:
    """渲染器不认识的键只会被排成几行标签，这不是一张图。"""

    issues = _vector_payload_issues(
        _complex(
            {
                "figures": [
                    {
                        "title": "报告 PIL 量表均分的研究篇数",
                        "bar-panels": [{"value": 45}],
                        "labels": ["纵轴刻度为 10", "纵轴刻度为 20"],
                    }
                ]
            }
        )
    )
    assert [issue["code"] for issue in issues] == [CODE]
    assert issues[0]["figure_indexes"] == [1]


def test_a_payload_with_real_structure_passes() -> None:
    issues = _vector_payload_issues(
        _complex(
            {
                "figures": [
                    {
                        "title": "模型结构",
                        "nodes": [{"id": "a", "label": "输入"}],
                        "edges": [{"from": "a", "to": "a"}],
                    }
                ]
            }
        )
    )
    assert issues == []


def test_an_empty_figure_list_is_rejected() -> None:
    assert [
        issue["code"] for issue in _vector_payload_issues(_complex({"figures": []}))
    ] == [CODE]


def test_other_methods_are_not_judged_by_this_rule() -> None:
    """保留策略本来就不画矢量结构，别拿这条规则去挡它。"""

    content = _complex({"figures": []})
    content["items"][0]["method"] = "preserve-element-region"
    assert _vector_payload_issues(content) == []


def test_drawable_keys_are_what_the_flowable_actually_uses() -> None:
    assert figure_payload_is_drawable({"series": [1]})
    assert not figure_payload_is_drawable({"labels": ["纵轴刻度为 10"]})
    assert not figure_payload_is_drawable("not a dict")
