"""复杂内容策略校验：未确认、标准、混合三种状态各自该报什么错。

这一段原本内联在 scripts/self_test.py 的 run() 里，三次调用同一个校验函数、
比对三份错误列表。切成一个命名用例后，失败信息能直接指到策略本身。
断言逐字保留原样，中文说明本身就是判据文档。

单独运行：
    python3 -m pytest -q tests/test_complex_content_policy.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from validate_job import _validate_complex_content_policy  # noqa: E402


def test_validate_complex_content_policy() -> None:
    """未确认的复杂内容要报错，标准页要报错，混合页在满足条件时必须放行。"""

    unconfirmed_complex_errors: list[str] = []
    _validate_complex_content_policy(
        {
            "selected": "standard-auto",
            "complex_content": {
                "classification_confirmed": False,
                "review_scope": "all-source-pages",
                "heuristic_candidate_pages": [],
                "confirmed_pages": [],
                "notes": "",
            },
        },
        page_count=3,
        stage="translated",
        errors=unconfirmed_complex_errors,
    )
    if not any(
        "目视确认全部原文页" in error
        for error in unconfirmed_complex_errors
    ):
        raise AssertionError("未完成全篇复杂内容预检时必须阻断 translated 阶段")

    complex_page = {
        "page": 2,
        "kind": "other-complex",
        "method": "custom-page-reflow",
        "reason": "该页结构不适合普通正文生成器，需按语义区域重建。",
    }
    standard_complex_errors: list[str] = []
    _validate_complex_content_policy(
        {
            "selected": "standard-auto",
            "complex_content": {
                "classification_confirmed": True,
                "review_scope": "all-source-pages",
                "heuristic_candidate_pages": [],
                "confirmed_pages": [complex_page],
                "notes": "已按原尺寸检查全部原文页。",
            },
        },
        page_count=3,
        stage="translated",
        errors=standard_complex_errors,
    )
    if not any(
        "不得选择 standard-auto" in error
        for error in standard_complex_errors
    ):
        raise AssertionError("任一复杂内容页首次使用普通自动路线时必须被阻断")

    hybrid_complex_errors: list[str] = []
    _validate_complex_content_policy(
        {
            "selected": "hybrid-complex-pages",
            "complex_content": {
                "classification_confirmed": True,
                "review_scope": "all-source-pages",
                "heuristic_candidate_pages": [],
                "confirmed_pages": [complex_page],
                "notes": "已按原尺寸检查全部原文页。",
            },
        },
        page_count=3,
        stage="translated",
        errors=hybrid_complex_errors,
    )
    if hybrid_complex_errors:
        raise AssertionError(
            f"复杂页采用专用重建路线后不应被误拦截: {hybrid_complex_errors}"
        )
