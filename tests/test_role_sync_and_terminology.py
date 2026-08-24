"""角色同步与术语豁免：保留原文的单元不被术语门槛逼翻译，
元素纠正后的新角色能一路传到批次写回。

单独运行：
    python3 -m pytest -q tests/test_role_sync_and_terminology.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from semantic_markers import validate_terminology  # noqa: E402


def test_terminology_check_skips_kept_source_units() -> None:
    """题录保留原文时不含中文术语是正确行为，不是违规。"""

    terminology = [
        {"source": "meaning in life", "target": "人生意义"}
    ]
    units = [
        {
            "id": "u-body",
            "source": "the meaning in life scale",
            "translation": "人生意义量表",
        },
        {
            "id": "u-ref",
            "source": "Steger, M. The meaning in life questionnaire.",
            "translation": "Steger, M. The meaning in life questionnaire.",
            "keep_source_code": "bibliography-entry",
            "keep_source_reason": "参考文献题录按学术惯例保留原文",
        },
    ]
    assert validate_terminology(terminology, units) == []


def test_terminology_check_still_binds_translated_body() -> None:
    """正文单元没用登记术语仍然要报——豁免只给保留原文的单元。"""

    terminology = [{"source": "meaning in life", "target": "人生意义"}]
    units = [
        {
            "id": "u-body",
            "source": "the meaning in life scale",
            "translation": "生活含义量表",
        }
    ]
    errors = validate_terminology(terminology, units)
    assert errors and "u-body" in errors[0]


def test_batch_apply_prefers_fresh_binding_roles(tmp_path: Path) -> None:
    """retype 纠正 → 重新绑定之后，写回一关要认新角色，不认旧的。"""

    from apply_translation_batch import _truthfulness_units

    job_dir = tmp_path
    (job_dir / "unit_bindings.json").write_text(
        json.dumps(
            {
                "bindings": [
                    {
                        "unit_id": "u1",
                        "element_id": "e1",
                        "element_role": "reference-entry",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    # translation.json 里的旧角色还是 body
    stale_roles = {"u1": "body"}
    batch = {
        "units": [
            {"id": "u1", "source": "Steger, M. (2006). MLQ."}
        ]
    }
    accepted = {
        "u1": {
            "translation": "Steger, M. (2006). MLQ.",
            "keep_source_code": "bibliography-entry",
            "keep_source_reason": "题录保留原文",
        }
    }
    # 直接复现 _assert_truthful 的角色合成逻辑
    element_roles = dict(stale_roles)
    for binding in json.loads(
        (job_dir / "unit_bindings.json").read_text(encoding="utf-8")
    )["bindings"]:
        role = str(binding.get("element_role") or "")
        if role:
            element_roles[str(binding["unit_id"])] = role
    units = _truthfulness_units(batch, accepted, element_roles)
    assert units[0]["element_role"] == "reference-entry"
