"""端到端基准：按阶段测量整条链路，并把结果绑定到当前代码构建哈希。

口径说明（先说清楚，避免读报告的人误会）：

- 模型翻译阶段**不在本报告的测量范围内**。这里用的是确定性的伪译文，
  只为触发同一批代码路径。真实模型的耗时和 Token 一律标记为未测量，
  绝不估算。
- PDF 生成与翻译分开记：`translation` 段只包含编排与写回，
  `pdf` 段包含试排、QA、完整性审查和预检。
- 每个案例分别跑冷启动与缓存两种状态，各至少 3 次，取中位数。
- 报告带上当前 `renderer_build_id` 与 git 提交，报告与代码对不上时，
  tests/test_benchmark_provenance.py 会失败。

用法::

    python3 benchmarks/run_benchmark.py --output benchmarks/results/optimized.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import perf_trace  # noqa: E402
from _common import SkillError, load_json, write_json  # noqa: E402
from build_first_candidate import build_first_candidate  # noqa: E402
from renderer_identity import renderer_build_id  # noqa: E402

COPY_IGNORES = ("history", "staging", "renders", "comparisons", "__pycache__")

#: 阶段归属：翻译链路与 PDF 生成链路必须分开报。
TRANSLATION_STAGES = (
    "initialize_job",
    "prepare_translation_units",
    "source_analysis",
    "source_structure",
    "source_profile",
    "plan_batches",
    "apply_batch",
)
PDF_STAGES = (
    "build_candidate",
    "retained_region_extract",
    "candidate_fingerprint",
    "register_candidate",
    "qa_candidate",
    "validate_job",
    "completeness_audit",
    "preflight",
    "review_sheet",
)

DEFAULT_REPEATS = 3


def _load_average() -> float:
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return -1.0


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except OSError:
        return ""


def _run_once(job_dir: Path, work_root: Path, tag: str) -> dict[str, Any]:
    destination = work_root / tag
    shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(
        job_dir,
        destination,
        ignore=shutil.ignore_patterns(*COPY_IGNORES),
    )
    started = time.perf_counter()
    try:
        report = build_first_candidate(destination, attempt_label="benchmark")
        status = str(report["status"])
    except SkillError as exc:
        status = f"ERROR: {str(exc)[:200]}"
    return {
        "seconds": round(time.perf_counter() - started, 4),
        "status": status,
    }


def _stage_seconds(snapshot: dict[str, Any], names: tuple[str, ...]) -> dict[str, float]:
    totals = snapshot.get("stage_totals", {})
    return {
        name: round(float(totals[name]["total_seconds"]), 4)
        for name in names
        if name in totals
    }


def _measure(
    job_dir: Path,
    work_root: Path,
    case_id: str,
    repeats: int,
) -> dict[str, Any]:
    cold_times: list[float] = []
    cold_snapshots: list[dict[str, Any]] = []
    status = "UNKNOWN"
    for index in range(repeats):
        # 冷启动：每次都清空进程内缓存，模拟新进程的第一次运行。
        perf_trace.reset()
        _reset_process_caches()
        result = _run_once(job_dir, work_root, f"{case_id}-cold-{index}")
        cold_times.append(result["seconds"])
        cold_snapshots.append(perf_trace.snapshot())
        status = result["status"]

    warm_times: list[float] = []
    perf_trace.reset()
    _run_once(job_dir, work_root, f"{case_id}-warm-priming")
    warm_snapshots: list[dict[str, Any]] = []
    for index in range(repeats):
        perf_trace.reset()
        result = _run_once(job_dir, work_root, f"{case_id}-warm-{index}")
        warm_times.append(result["seconds"])
        warm_snapshots.append(perf_trace.snapshot())

    median_cold = cold_snapshots[len(cold_snapshots) // 2]
    median_warm = warm_snapshots[len(warm_snapshots) // 2]
    return {
        "id": case_id,
        "status": status,
        "repeats": repeats,
        "cold": {
            "seconds": [round(value, 4) for value in cold_times],
            "median_seconds": round(statistics.median(cold_times), 4),
            "translation_stage_seconds": _stage_seconds(
                median_cold,
                TRANSLATION_STAGES,
            ),
            "pdf_stage_seconds": _stage_seconds(median_cold, PDF_STAGES),
            "counters": median_cold["counters"],
        },
        "warm": {
            "seconds": [round(value, 4) for value in warm_times],
            "median_seconds": round(statistics.median(warm_times), 4),
            "translation_stage_seconds": _stage_seconds(
                median_warm,
                TRANSLATION_STAGES,
            ),
            "pdf_stage_seconds": _stage_seconds(median_warm, PDF_STAGES),
            "counters": median_warm["counters"],
        },
    }


def _reset_process_caches() -> None:
    """清掉跨次运行会残留的进程内缓存，让冷启动真的是冷启动。"""

    from _common import FINGERPRINT_CACHE

    if hasattr(FINGERPRINT_CACHE, "clear"):
        FINGERPRINT_CACHE.clear()


def run_benchmark(
    manifest_path: Path,
    *,
    repeats: int = DEFAULT_REPEATS,
    label: str = "current",
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    manifest = load_json(manifest_path)
    cases: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="academic-benchmark-") as raw:
        work_root = Path(raw)
        for case in manifest["cases"]:
            job_dir = manifest_path.parent / case["job_dir"]
            measured = _measure(job_dir, work_root, str(case["id"]), repeats)
            measured.update(
                {
                    "page_count": case.get("page_count"),
                    "frozen_unit_count": case.get("frozen_unit_count"),
                    "tags": case.get("tags", []),
                }
            )
            cases.append(measured)

    cold_totals = [case["cold"]["median_seconds"] for case in cases]
    warm_totals = [case["warm"]["median_seconds"] for case in cases]
    return {
        "schema_version": "2.0",
        "label": label,
        "provenance": {
            "renderer_build_id": renderer_build_id(),
            "git_commit": _git("rev-parse", "HEAD"),
            "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "git_dirty": bool(_git("status", "--porcelain")),
            "python": sys.version.split()[0],
            # 机器负载直接决定这份耗时能不能当结论用；不记下来就没法判断。
            "load_average_1m": round(_load_average(), 2),
            "cpu_count": os.cpu_count(),
            "corpus": manifest.get("corpus"),
            "corpus_manifest_cases": len(manifest["cases"]),
        },
        "repeats": repeats,
        "model_translation": {
            "measured": False,
            "reason": (
                "本次运行没有调用真实翻译模型；译文由确定性伪译文替代，"
                "只用于触发同一批代码路径。"
            ),
            "model": None,
            "model_calls": None,
            "batches": None,
            "retries": None,
            "input_tokens": None,
            "output_tokens": None,
            "seconds": None,
        },
        "cases": cases,
        "totals": {
            "cold_median_seconds": round(sum(cold_totals), 4),
            "warm_median_seconds": round(sum(warm_totals), 4),
            "cold_case_median_seconds": round(
                statistics.median(cold_totals), 4
            )
            if cold_totals
            else None,
            "warm_case_median_seconds": round(
                statistics.median(warm_totals), 4
            )
            if warm_totals
            else None,
        },
        "note": (
            "合成语料只用于比较耗时与重复读取，不能替代真实论文的视觉抽查。"
            "模型翻译阶段未测量。"
        ),
    }


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        nargs="?",
        type=Path,
        default=here / "corpus.json",
    )
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--label", default="current")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 3:
        raise SystemExit("冷启动与缓存状态各需要至少 3 次")
    random.seed(0)
    report = run_benchmark(
        args.manifest,
        repeats=args.repeats,
        label=args.label,
    )
    if args.output:
        write_json(args.output.resolve(), report)
        print(f"报告已写入: {args.output.resolve()}")
    print(json.dumps(report["totals"], ensure_ascii=False, indent=2))
    print(f"renderer_build_id: {report['provenance']['renderer_build_id']}")
    print("模型翻译: 未测量")
    for case in report["cases"]:
        print(
            f"  {case['id']:<28}"
            f"冷 {case['cold']['median_seconds']:>7.3f}s"
            f"  缓存 {case['warm']['median_seconds']:>7.3f}s"
            f"  {case['status']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
