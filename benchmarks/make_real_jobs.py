"""用真实开放获取论文构建基准作业。

论文来自 arXiv，只在本地使用，不随仓库分发。译文仍是确定性合成文本，
用于触发同一批代码路径；它不是真实翻译，也不能用于评价译文质量。

本脚本回答的问题是：在真实论文的版式上，优化前后的机器行为是否一致、
是否更快。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _common import load_json, sha256_file, write_json  # noqa: E402
from make_benchmark_jobs import build_jobs  # noqa: E402


#: 五类代表样本，按项目自带的 pdf_profile.py 画像结果选定。
REAL_CASES = {
    "real-single-column-body": ("arxiv-1912.01703.pdf", ["body", "single-column"]),
    "real-two-column-body": ("arxiv-1512.03385.pdf", ["body", "two-column"]),
    "real-table-and-model": ("arxiv-1706.03762.pdf", ["table", "diagram"]),
    "real-image-heavy": ("arxiv-1703.06870.pdf", ["figure", "image"]),
    "real-reference-heavy": ("arxiv-2005.14165.pdf", ["references"]),
}


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--papers-dir", type=Path, default=here / "papers-real")
    parser.add_argument("--jobs-root", type=Path, default=here / "jobs-real")
    parser.add_argument("--output", type=Path, default=here / "corpus-real.json")
    parser.add_argument("--translation-ratio", type=float, default=0.55)
    parser.add_argument(
        "--identity",
        action="store_true",
        help="用原文本身作为译文，目标语言设为 en；用于在真实版式上跑通机器链路",
    )
    args = parser.parse_args()

    papers = args.papers_dir.resolve()
    sources = {
        name: papers / filename
        for name, (filename, _) in REAL_CASES.items()
    }
    missing = [str(path) for path in sources.values() if not path.is_file()]
    if missing:
        raise SystemExit("缺少论文，请先下载: " + ", ".join(missing))

    cases = build_jobs(
        args.jobs_root.resolve(),
        papers,
        target_language="en" if args.identity else "zh-Hans",
        translation_ratio=args.translation_ratio,
        sources=sources,
        tags={name: tags for name, (_, tags) in REAL_CASES.items()},
        identity=args.identity,
    )
    write_json(
        args.output.resolve(),
        {
            "schema_version": "1.0",
            "corpus": "real-open-access-v1",
            "note": (
                "真实开放获取论文（arXiv），原文不随仓库分发。"
                "译文为确定性合成文本，只用于触发代码路径，不代表译文质量。"
            ),
            "translation_ratio": args.translation_ratio,
            "cases": cases,
        },
    )
    print(f"真实语料清单已写入: {args.output.resolve()}")
    print(f"{'案例':<26}{'页':>5}{'冻结单元':>10}{'复杂页':>8}{'双栏页':>8}{'路线':>24}")
    for case in cases:
        print(
            f"{case['id']:<26}{case['page_count']:>5}{case['frozen_unit_count']:>10}"
            f"{case['complex_page_count']:>8}{case['double_column_page_count']:>8}"
            f"{case['recommended_route']:>24}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
