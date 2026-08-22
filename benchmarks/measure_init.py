"""测量初始化阶段的原文扫描耗时与重复调用次数。

用法：指定一份 `scripts/` 目录，对语料逐篇运行
`profile_pdf()` 与 `extract_source_structure()`，输出耗时和 PyMuPDF
调用计数。基线版本与优化版本各跑一次，即可得到可比较的前后数据。

计数通过在 `fitz.Page` 上包裹方法完成，对新旧两版代码一视同仁。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any


COUNTERS: dict[str, int] = {}


def _bump(name: str) -> None:
    COUNTERS[name] = COUNTERS.get(name, 0) + 1


def _install_counters() -> None:
    import fitz

    original_open = fitz.open

    def counted_open(*args: Any, **kwargs: Any) -> Any:
        _bump("pdf_open")
        return original_open(*args, **kwargs)

    fitz.open = counted_open

    page_class = fitz.Page
    original_get_text = page_class.get_text
    original_get_drawings = page_class.get_drawings
    original_get_image_info = page_class.get_image_info
    original_get_images = page_class.get_images

    def counted_get_text(self: Any, option: str = "text", *args: Any, **kwargs: Any) -> Any:
        _bump(f"get_text_{option}")
        return original_get_text(self, option, *args, **kwargs)

    def counted_get_drawings(self: Any, *args: Any, **kwargs: Any) -> Any:
        _bump("get_drawings")
        return original_get_drawings(self, *args, **kwargs)

    def counted_get_image_info(self: Any, *args: Any, **kwargs: Any) -> Any:
        _bump("get_image_info")
        return original_get_image_info(self, *args, **kwargs)

    def counted_get_images(self: Any, *args: Any, **kwargs: Any) -> Any:
        _bump("get_images")
        return original_get_images(self, *args, **kwargs)

    page_class.get_text = counted_get_text
    page_class.get_drawings = counted_get_drawings
    page_class.get_image_info = counted_get_image_info
    page_class.get_images = counted_get_images


def _run_once(scripts_dir: Path, pdf: Path) -> dict[str, Any]:
    COUNTERS.clear()
    for module in list(sys.modules):
        if module in {
            "_common",
            "pdf_profile",
            "extract_source_structure",
            "source_analysis",
            "prepare_translation_units",
            "perf_trace",
        }:
            del sys.modules[module]

    sys.path.insert(0, str(scripts_dir))
    try:
        import extract_source_structure as structure_module
        import pdf_profile as profile_module

        single_pass = "source_analysis" in getattr(
            profile_module, "__dict__", {}
        ) or hasattr(profile_module, "analyze_source")

        started = time.perf_counter()
        if single_pass:
            from source_analysis import analyze_source

            analysis = analyze_source(pdf)
            manifest = profile_module.profile_pdf(pdf, analysis=analysis)
            structure = structure_module.extract_source_structure(
                pdf,
                analysis=analysis,
            )
        else:
            manifest = profile_module.profile_pdf(pdf)
            structure = structure_module.extract_source_structure(pdf)
        elapsed = time.perf_counter() - started
    finally:
        sys.path.remove(str(scripts_dir))

    return {
        "elapsed_seconds": round(elapsed, 4),
        "counters": dict(sorted(COUNTERS.items())),
        "page_count": manifest["page_count"],
        "route": manifest["route"]["recommended"],
        "complex_pages": list(manifest["complex_pages"]),
        "visual_pages": list(structure["visual_confirmation_pages"]),
        "manifest_digest": _digest(manifest),
        "structure_digest": _digest(structure),
    }


def _digest(payload: dict[str, Any]) -> str:
    import hashlib

    stripped = {
        key: value
        for key, value in payload.items()
        if key not in {"generated_at", "source"}
    }
    return hashlib.sha256(
        json.dumps(stripped, ensure_ascii=False, sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()[:16]


def measure(
    scripts_dir: Path,
    corpus: list[Path],
    repeats: int,
) -> dict[str, Any]:
    _install_counters()
    cases: list[dict[str, Any]] = []
    for pdf in corpus:
        runs = [_run_once(scripts_dir, pdf) for _ in range(repeats)]
        elapsed = [run["elapsed_seconds"] for run in runs]
        digests = {
            (run["manifest_digest"], run["structure_digest"]) for run in runs
        }
        cases.append(
            {
                "case": pdf.stem,
                "pdf": pdf.name,
                "size_bytes": pdf.stat().st_size,
                "page_count": runs[0]["page_count"],
                "route": runs[0]["route"],
                "complex_pages": runs[0]["complex_pages"],
                "visual_pages": runs[0]["visual_pages"],
                "median_seconds": round(statistics.median(elapsed), 4),
                "min_seconds": round(min(elapsed), 4),
                "counters_per_run": runs[0]["counters"],
                "stable_across_runs": len(digests) == 1,
                "manifest_digest": runs[0]["manifest_digest"],
                "structure_digest": runs[0]["structure_digest"],
            }
        )
    return {
        "scripts_dir": str(scripts_dir),
        "repeats": repeats,
        "total_median_seconds": round(
            sum(case["median_seconds"] for case in cases), 4
        ),
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scripts-dir", type=Path, required=True)
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "papers",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    corpus = sorted(args.corpus_dir.resolve().glob("*.pdf"))
    if not corpus:
        print(f"错误: 语料目录没有 PDF: {args.corpus_dir}")
        return 1
    report = measure(args.scripts_dir.resolve(), corpus, args.repeats)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"结果已写入: {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
