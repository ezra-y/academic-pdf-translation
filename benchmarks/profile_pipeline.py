"""按阶段剖析候选流水线，并区分冷启动与缓存命中两种状态。

冷启动 = 新进程里对该作业的第一次运行，进程内缓存全空。
缓存状态 = 同一进程里的后续运行，哈希缓存与原文扫描结果已经建立。

输出按阶段耗时降序排列，并附上 PyMuPDF 调用计数与缓存命中数，
用来回答"时间主要花在哪一段"。
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import perf_trace  # noqa: E402
from _common import SkillError, load_json  # noqa: E402
from build_first_candidate import build_first_candidate  # noqa: E402


COPY_IGNORES = ("history", "staging", "renders", "comparisons", "__pycache__")


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
        report = build_first_candidate(destination, attempt_label="profile")
        status = report["status"]
    except SkillError as exc:
        status = f"ERROR: {str(exc)[:60]}"
    return {
        "seconds": time.perf_counter() - started,
        "status": status,
    }


def profile_corpus(
    manifest_path: Path,
    *,
    repeats: int = 3,
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    manifest = load_json(manifest_path)
    cases: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="academic-profile-") as raw:
        work_root = Path(raw)
        for case in manifest["cases"]:
            job_dir = manifest_path.parent / case["job_dir"]
            perf_trace.reset()
            cold = _run_once(job_dir, work_root, f"{case['id']}-cold")
            cold_stages = perf_trace.snapshot()

            warm_times: list[float] = []
            perf_trace.reset()
            for index in range(max(repeats - 1, 1)):
                warm_times.append(
                    _run_once(
                        job_dir,
                        work_root,
                        f"{case['id']}-warm-{index}",
                    )["seconds"]
                )
            warm_stages = perf_trace.snapshot()

            cases.append(
                {
                    "id": case["id"],
                    "page_count": case.get("page_count"),
                    "frozen_unit_count": case.get("frozen_unit_count"),
                    "status": cold["status"],
                    "cold_seconds": round(cold["seconds"], 3),
                    "warm_min_seconds": round(min(warm_times), 3),
                    "warm_median_seconds": round(
                        statistics.median(warm_times), 3
                    ),
                    "cold_stage_totals": cold_stages["stage_totals"],
                    "cold_counters": cold_stages["counters"],
                    "warm_counters": warm_stages["counters"],
                }
            )

    aggregate: dict[str, dict[str, Any]] = {}
    for case in cases:
        for stage, totals in case["cold_stage_totals"].items():
            entry = aggregate.setdefault(
                stage,
                {"calls": 0, "total_seconds": 0.0},
            )
            entry["calls"] += totals["calls"]
            entry["total_seconds"] = round(
                entry["total_seconds"] + totals["total_seconds"], 6
            )
    ranked = dict(
        sorted(
            aggregate.items(),
            key=lambda item: item[1]["total_seconds"],
            reverse=True,
        )
    )
    measured = sum(entry["total_seconds"] for entry in ranked.values())
    for entry in ranked.values():
        entry["share"] = (
            round(entry["total_seconds"] / measured, 4) if measured else None
        )
    return {
        "schema_version": "1.0",
        "corpus": manifest.get("corpus"),
        "repeats": repeats,
        "cases": cases,
        "stage_totals_cold": ranked,
        "measured_stage_seconds": round(measured, 3),
        "note": (
            "合成语料只用于比较耗时与重复读取，不能替代真实论文的视觉抽查。"
            "首版通过率不在本报告口径内。"
        ),
    }


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", type=Path, default=here / "corpus.json")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = profile_corpus(args.manifest, repeats=args.repeats)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"报告已写入: {args.output}")

    print()
    print(f"{'案例':<30}{'冷启动':>9}{'缓存后':>9}{'差异':>8}   结论")
    print("-" * 78)
    for case in report["cases"]:
        gain = (
            (case["cold_seconds"] - case["warm_min_seconds"])
            / case["cold_seconds"]
            * 100
            if case["cold_seconds"]
            else 0.0
        )
        print(
            f"{case['id']:<30}{case['cold_seconds']:>8.3f}s"
            f"{case['warm_min_seconds']:>8.3f}s{gain:>7.1f}%   {case['status']}"
        )
    print()
    print("按阶段排序（冷启动，全部案例合计）:")
    print(f"{'阶段':<28}{'次数':>6}{'耗时s':>10}{'占比':>8}")
    print("-" * 52)
    for stage, totals in report["stage_totals_cold"].items():
        share = totals["share"]
        print(
            f"{stage:<28}{totals['calls']:>6}{totals['total_seconds']:>10.3f}"
            f"{(share * 100 if share else 0):>7.1f}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
