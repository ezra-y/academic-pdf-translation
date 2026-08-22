"""排版参数搜索：加速后的选择必须和逐一枚举的结果一致。

生产代码为了省时间不再穷举全部字号行距组合。这支用例把同一组页面
分别喂给加速搜索和线性扫描，要求两者选出同一套参数。
断言逐字保留原样，中文说明本身就是判据文档。

单独运行：
    python3 -m pytest -q tests/test_typography_search.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))




def test_typography_search_matches_linear_scan() -> None:
    """两级二分必须与线性扫描选出同一个组合，并在单调性失效时回退。"""

    from typography_fit import search_first_acceptable

    def make_evaluator(page_counts, limit, probes):
        def evaluate(group_index, item_index):
            probes.append((group_index, item_index))
            pages = page_counts[group_index][item_index]
            return {"fits": pages <= limit, "page_count": pages}

        return evaluate

    def linear_choice(page_counts, limit):
        for group_index, group in enumerate(page_counts):
            for item_index, pages in enumerate(group):
                if pages <= limit:
                    return (group_index, item_index)
        return None

    monotonic = [
        [24, 24, 21],
        [24, 24, 21],
        [24, 24, 20],
        [24, 22, 20],
        [24, 22, 19],
        [23, 21, 18],
    ]
    groups = [[(0.0, 0.0)] * len(row) for row in monotonic]
    for limit in range(17, 26):
        probes: list[tuple[int, int]] = []
        position, method, note = search_first_acceptable(
            groups=groups,
            evaluate=make_evaluator(monotonic, limit, probes),
        )
        expected = linear_choice(monotonic, limit)
        if position != expected:
            raise AssertionError(
                f"页数上限 {limit} 时二分结果 {position} 与线性扫描 "
                f"{expected} 不一致"
            )
        if expected is None:
            if method != "linear-fallback" or note != (
                "no-feasible-group-verify-exhaustively"
            ):
                raise AssertionError(
                    "断定无解前必须回退到完整线性扫描确认，不能只靠二分"
                )
            continue
        if method != "bounded-binary":
            raise AssertionError("单调页数下不应触发回退")
        unique_probes = len(set(probes))
        if unique_probes > 6:
            raise AssertionError(
                f"页数上限 {limit} 时用了 {unique_probes} 次试排，超出预期"
            )

    # 同组内字号更小却页数更多：这是能被实际试排观测到的单调性破坏。
    non_monotonic = [
        [30, 30, 25],
        [30, 20, 22],
    ]
    fallback_groups = [[(0.0, 0.0)] * len(row) for row in non_monotonic]
    position, method, note = search_first_acceptable(
        groups=fallback_groups,
        evaluate=make_evaluator(non_monotonic, 22, []),
    )
    if position is not None or method != "linear-fallback":
        raise AssertionError("观测到单调性失效时必须回退到完整线性扫描")
    if note != "page-count-not-monotonic-within-leading":
        raise AssertionError("回退必须记录原因，不能静默执行")

    # 行距更小的组反而更厚：跨组单调性破坏同样必须回退。
    across_groups = [
        [40, 35, 30],
        [40, 35, 29],
        [40, 35, 27],
        [40, 35, 28],
        [40, 35, 25],
    ]
    position, method, note = search_first_acceptable(
        groups=[[(0.0, 0.0)] * len(row) for row in across_groups],
        evaluate=make_evaluator(across_groups, 26, []),
    )
    if method != "linear-fallback":
        raise AssertionError("跨行距组的单调性破坏必须回退到完整线性扫描")
    if note != "page-count-not-monotonic-across-leading":
        raise AssertionError("回退必须记录原因，不能静默执行")

    def failing_evaluate(group_index, item_index):
        return None

    position, method, note = search_first_acceptable(
        groups=fallback_groups,
        evaluate=failing_evaluate,
    )
    if method != "linear-fallback" or note != "render-failed-during-search":
        raise AssertionError("试排失败时必须回退并记录原因")
