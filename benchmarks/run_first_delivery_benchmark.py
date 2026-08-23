"""真实论文的首版交付基准。

回答一个问题：**拿真实论文跑一遍，第一版能直接交付的有几篇？**

三条纪律写在代码里，不靠跑的人自觉：

1. 论文与作业目录受版权保护，不进仓库。这里只记哈希、页数和派生结论。
2. 译文来源必须如实标注。``benchmarks/corpus-real.json`` 里的几个作业用的是
   **确定性合成译文**，只为触发代码路径，不代表译文质量；只有真实模型翻译过的
   作业才算"译文已验证"。两者混在一张表里而不区分，就是在虚报。
3. 跑不动的写"未验证"，不填数。

用法::

    python3 benchmarks/run_first_delivery_benchmark.py --output <目录>
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

JOBS_ROOT = REPO_ROOT / "benchmarks" / "jobs-real"
SCHEMA_VERSION = "1.0"

#: 译文来源。混在一张表里而不区分，就是在虚报。
TRANSLATION_REAL = "real-model"
TRANSLATION_SYNTHETIC = "deterministic-synthetic"

STATUS_UNVERIFIED = "unverified"

#: 每篇论文最多跑这么久。超时算"未验证"，不算失败——分不清是慢还是坏。
PER_JOB_TIMEOUT_SECONDS = 1800


@dataclass
class CaseResult:
    """一篇论文的结果。"""

    case_id: str
    source_sha256: str
    source_pages: int
    translation_source: str
    status: str
    rebuilds: int = 0
    problem_count: int = 0
    manual_count: int = 0
    top_problems: list[str] = field(default_factory=list)
    elapsed_seconds: float | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _page_count(path: Path) -> int:
    import fitz

    with fitz.open(path) as document:
        return document.page_count


def discover_cases(real_translation_ids: set[str]) -> list[dict[str, str]]:
    """列出可跑的作业，并标出每个作业的译文来源。"""

    cases: list[dict[str, str]] = []
    for job_dir in sorted(JOBS_ROOT.glob("*")):
        if not (job_dir / "source.pdf").is_file():
            continue
        if not (job_dir / "translation.json").is_file():
            continue
        cases.append(
            {
                "case_id": job_dir.name,
                "job_dir": str(job_dir),
                "translation_source": (
                    TRANSLATION_REAL
                    if job_dir.name in real_translation_ids
                    else TRANSLATION_SYNTHETIC
                ),
            }
        )
    return cases


def _run(command: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return (result.returncode, (result.stdout + result.stderr)[-4000:])


#: 当前 translation.coverage 必须有的真实性字段。缺了才重算。
REQUIRED_COVERAGE_FIELDS = (
    "validated_translated_units",
    "validated_kept_source_units",
    "invalid_or_unverified_units",
)


def refresh_translation_coverage(work_dir: Path) -> str:
    """字段缺失时按当前真实性判定重算 translation.coverage。

    冻结的语料是在真实性字段加入之前做的，coverage 还是旧结构，输入就绪
    检查会直接拦下。这里**重算**，不是手填——数字全部来自实际单元。

    两条边界要守住：

    1. 字段已经齐了就别动。在一份已经算对的作业上重算一遍，反而可能算错。
    2. 必须把 retained_source 一起传进去。保留原文的单元算不算"已验证"，
       取决于保留区域；不传它，算出来的数会和校验器的独立重算对不上——
       这正是第一次跑出 65 != 68 的原因。
    """

    from translation_truthfulness import refresh_coverage

    path = work_dir / "translation.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    coverage = document.get("coverage") or {}
    if all(field in coverage for field in REQUIRED_COVERAGE_FIELDS):
        return ""

    retained_path = work_dir / "retained_source.json"
    retained = (
        json.loads(retained_path.read_text(encoding="utf-8"))
        if retained_path.is_file()
        else None
    )
    try:
        refresh_coverage(document, retained_source=retained)
    except Exception as exc:  # noqa: BLE001 - 算不出来要说清楚，不能默默跳过
        return f"重算 translation.coverage 失败: {exc}"
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return ""


def prepare_job(work_dir: Path, timeout: int) -> str:
    """把作业补到交付入口能用的状态。

    已经有的产物不重做——重跑绑定会在已完成的作业上得出不同结论，
    那测的就不是交付，是准备步骤本身。
    """

    problem = refresh_translation_coverage(work_dir)
    if problem:
        return problem

    steps = [
        ("analyze_source_elements.py", "source_elements.json"),
        ("bind_translation_units.py", "unit_bindings.json"),
    ]
    for script, produces in steps:
        if (work_dir / produces).is_file():
            continue
        code, output = _run(
            [sys.executable, str(REPO_ROOT / "scripts" / script), str(work_dir)],
            REPO_ROOT,
            timeout,
        )
        if code != 0:
            return f"{script} 退出码 {code}: {output[-400:]}"
    return ""


def run_case(
    case: dict[str, str],
    work_root: Path,
    *,
    timeout: int = PER_JOB_TIMEOUT_SECONDS,
) -> CaseResult:
    """跑一篇。作业目录先复制到临时位置，不动仓库里的原件。"""

    source = Path(case["job_dir"]) / "source.pdf"
    result = CaseResult(
        case_id=case["case_id"],
        source_sha256=_sha256(source),
        source_pages=_page_count(source),
        translation_source=case["translation_source"],
        status=STATUS_UNVERIFIED,
    )

    work_dir = work_root / case["case_id"]
    if work_dir.exists():
        shutil.rmtree(work_dir)
    shutil.copytree(case["job_dir"], work_dir)

    started = time.monotonic()
    try:
        problem = prepare_job(work_dir, timeout)
        if problem:
            result.note = f"准备阶段失败: {problem}"
            return result
        code, output = _run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "deliver_first_candidate.py"),
                str(work_dir),
            ],
            REPO_ROOT,
            timeout,
        )
    except subprocess.TimeoutExpired:
        result.note = f"超过 {timeout} 秒未完成，记为未验证"
        return result
    finally:
        result.elapsed_seconds = round(time.monotonic() - started, 2)

    # 读交付入口自己写出的 delivery.json，不解析 stdout——生成器沿途也会打印，
    # 从混合输出里捞 JSON 迟早捞错。
    delivery = work_dir / "delivery" / "delivery.json"
    if not delivery.is_file():
        result.note = f"交付入口退出码 {code}，没有写出 delivery.json: {output[-300:]}"
        return result
    payload = json.loads(delivery.read_text(encoding="utf-8"))

    result.status = str(payload.get("status") or STATUS_UNVERIFIED)
    result.rebuilds = int(payload.get("rebuilds") or 0)
    result.problem_count = int(payload.get("problem_count") or 0)
    result.manual_count = int(payload.get("manual_count") or 0)
    result.top_problems = [str(item) for item in payload.get("problems", [])[:5]]
    return result


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    """按译文来源分开统计。混在一起报，数字就没意义了。"""

    by_source: dict[str, dict[str, int]] = {}
    for item in results:
        bucket = by_source.setdefault(item.translation_source, {})
        bucket[item.status] = bucket.get(item.status, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "case_count": len(results),
        "by_translation_source": by_source,
        "unverified": [
            item.case_id for item in results if item.status == STATUS_UNVERIFIED
        ],
        "cases": [item.as_dict() for item in results],
    }


def format_report(summary: dict[str, Any]) -> str:
    lines = [f"真实论文首版交付基准：{summary['case_count']} 篇", ""]
    for source, counts in sorted(summary["by_translation_source"].items()):
        label = (
            "真实模型译文"
            if source == TRANSLATION_REAL
            else "确定性合成译文（只触发代码路径，不代表译文质量）"
        )
        total = sum(counts.values())
        lines.append(f"{label}: {total} 篇")
        for status, count in sorted(counts.items()):
            lines.append(f"    {status:12s} {count}")
    lines.append("")
    lines.append("逐篇:")
    for case in summary["cases"]:
        lines.append(
            f"  {case['case_id']:28s} {case['source_pages']:3d} 页  "
            f"{case['status']:10s} 重建 {case['rebuilds']}  "
            f"问题 {case['problem_count']:3d}  "
            f"待人工 {case['manual_count']:3d}  "
            f"{case['elapsed_seconds']}s"
        )
        if case["note"]:
            lines.append(f"      备注: {case['note']}")
        for problem in case["top_problems"][:2]:
            lines.append(f"      - {problem[:96]}")
    if summary["unverified"]:
        lines.append("")
        lines.append("未验证: " + "、".join(summary["unverified"]))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir",
        type=Path,
        required=True,
        help="作业副本与产物放在哪里（不进仓库）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "benchmarks" / "results" / "first-delivery.json",
    )
    parser.add_argument(
        "--real-translation",
        action="append",
        default=[],
        help="哪些作业用的是真实模型译文；其余一律标为合成译文",
    )
    parser.add_argument("--timeout", type=int, default=PER_JOB_TIMEOUT_SECONDS)
    parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args()

    cases = discover_cases(set(args.real_translation))
    if args.only:
        cases = [case for case in cases if case["case_id"] in set(args.only)]
    if not cases:
        print("没有可跑的作业")
        return 1

    work_root = args.work_dir.resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    results = [run_case(case, work_root, timeout=args.timeout) for case in cases]

    summary = summarize(results)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(format_report(summary))
    print(f"\n结果已写入: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
