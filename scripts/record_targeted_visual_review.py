"""把一次真正做过的视觉检查录成结果文件。

评审（人或审查代理）逐页逐项看完之后，把答案写成一个简单的 JSON：

    {
      "reviewer_type": "targeted-agent",
      "items": [
        {"candidate_page": 2, "check_code": "element-missing",
         "decision": "PASS", "detail": "图 1 在，位置正确"}
      ]
    }

本脚本负责三件事，评审自己不用管：

1. 用**当前候选文件**算出 candidate_sha256 盖进结果——结果从此只对
   这一份文件有效，候选一换自动作废；
2. 对照交付目录里的检查计划，核对每个选中页的每个检查码是否都有答案；
3. 写出规范的 visual-review-result.json，供交付入口 --visual-result 使用。

退出码：0 = 已录入且覆盖完整；1 = 输入不合法或覆盖不完整。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402

from academic_pdf_translation.delivery.evidence import (  # noqa: E402
    attempt_dir,
    read_current_run,
)
from academic_pdf_translation.delivery.models import file_sha256  # noqa: E402
from academic_pdf_translation.verify.visual_result import (  # noqa: E402
    VisualResultError,
    result_from_dict,
)

from _common import SkillError, load_json, write_json  # noqa: E402


def _current_attempt_dir(delivery_dir: Path) -> Path | None:
    """按 current-run.json 指针找"现在算数"的那一轮证据目录。

    禁止按固定文件名摸旧报告——旧证据还在，但它属于历史。
    """

    identity = read_current_run(delivery_dir)
    if identity is None:
        return None
    directory = attempt_dir(delivery_dir, identity.run_id, identity.attempt_id)
    return directory if directory.is_dir() else None


def _latest_build_candidate(delivery_dir: Path) -> Path | None:
    directory = _current_attempt_dir(delivery_dir)
    if directory is None:
        return None
    bundled = directory / "candidate.pdf"
    if bundled.is_file():
        return bundled
    for path in sorted(directory.glob("round-*-build.json"), reverse=True):
        candidate = load_json(path).get("candidate_path")
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def _latest_plan(delivery_dir: Path) -> dict | None:
    directory = _current_attempt_dir(delivery_dir)
    if directory is None:
        return None
    for path in sorted(directory.glob("round-*-review.json"), reverse=True):
        return load_json(path)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path)
    parser.add_argument(
        "--result",
        type=Path,
        required=True,
        help="评审写的答案 JSON（reviewer_type + items）",
    )
    parser.add_argument(
        "--candidate",
        type=Path,
        default=None,
        help="被检查的候选 PDF；默认取交付证据里最后一轮的候选",
    )
    parser.add_argument(
        "--delivery-dir",
        type=Path,
        default=None,
        help="交付证据目录，默认 <job_dir>/delivery",
    )
    args = parser.parse_args()

    job_dir = args.job_dir.resolve()
    delivery_dir = (args.delivery_dir or job_dir / "delivery").resolve()

    try:
        raw = load_json(args.result)
        candidate = args.candidate or _latest_build_candidate(delivery_dir)
        if candidate is None or not Path(candidate).is_file():
            raise SkillError(
                "找不到被检查的候选 PDF；用 --candidate 指明，"
                "或先跑一次 deliver_first_candidate 生成构建证据"
            )
        # 哈希由脚本按当前文件计算，不接受评审手填。
        raw["candidate_sha256"] = file_sha256(Path(candidate))
        result = result_from_dict(raw)
    except (SkillError, VisualResultError, OSError, ValueError) as exc:
        print(f"错误: {exc}")
        return 1

    problems: list[str] = []
    plan = _latest_plan(delivery_dir)
    if plan is None:
        problems.append(
            "交付目录里没有检查计划（round-*-review.json）；"
            "结果照录，但无法核对覆盖是否完整"
        )
    else:
        for page in plan.get("selected", []):
            page_no = page.get("candidate_page")
            codes = {
                signal.get("code")
                for signal in page.get("signals", [])
                if signal.get("code")
            }
            for code in sorted(codes):
                if not result.answered(page_no, code):
                    problems.append(f"第 {page_no} 页的 {code} 没有答案")

    output = delivery_dir / "visual-review-result.json"
    write_json(output, result.as_dict())
    print(f"已录入: {output}")
    print(f"候选哈希: {result.candidate_sha256}")
    print(f"条目 {len(result.items)} 条，总结论 {result.decision}")
    if problems:
        print()
        print("覆盖不完整:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
