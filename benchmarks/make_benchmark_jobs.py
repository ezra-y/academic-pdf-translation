"""从合成语料构建可直接跑基准的作业，并写出 `benchmarks/corpus.json`。

`scripts/benchmark_corpus.py` 需要的是**已经翻译完、可以直接生成候选**的作业
目录。本脚本负责把合成 PDF 变成这样的作业：初始化、填入合成译文、冻结字体、
完成图表清单与复杂页登记。

合成译文按单元内容确定性生成，同一份语料每次构建结果一致，因此基准可复算。
它不是真实译文，只用来触发同一批代码路径。

`corpus.json` 里的 `job_dir` 使用相对路径，可以直接入库，换机器不用改。
"""

from __future__ import annotations

import argparse
import random
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _common import load_json, sha256_file, write_json  # noqa: E402
from init_job import initialize_job  # noqa: E402
from make_synthetic_corpus import BUILDERS, build_corpus  # noqa: E402
from set_complex_content import set_complex_content  # noqa: E402
from translation_truthfulness import refresh_coverage  # noqa: E402

#: 中文译文相对英文原文的字数比例。合成语料按类型给不同默认值，
#: 让每个案例都能真正走完流水线；参考文献密集页天然更挤，比例更低。
# 合成译文的字数比例（译文字符数 / 原文字符数）。
# 0.55 与 benchmarks/make_real_jobs.py 的真实论文默认值一致：中文比英文密，
# 同样内容的字符数通常只有原文的一半左右。此前用的 0.75 偏长，
# 在真实中文字体下会把合成语料顶出页数扩张保护上限，测出来的不是
# 排版性能，而是"语料写得太长"。
CASE_TRANSLATION_RATIO = {
    "single-column-body": 0.55,
    "two-column-body": 0.55,
    "structured-table-and-model": 0.55,
    "image-heavy": 0.55,
    "reference-heavy": 0.45,
}

CASE_TAGS = {
    "single-column-body": ["body", "single-column"],
    "two-column-body": ["body", "two-column"],
    "structured-table-and-model": ["table", "diagram"],
    "image-heavy": ["figure", "image"],
    "reference-heavy": ["references"],
}

VOCAB = ["缓存", "失效", "策略", "分布式", "系统", "评估", "讨论", "样本", "方差", "区间", "回归", "效度", "信度", "参与者", "流程", "测量", "结果", "处理", "对照", "基线", "显著", "估计", "队列", "站点", "负载", "延迟", "陈旧", "读取", "协调", "部署", "月度", "生成", "封套", "局限", "未来", "工作", "如下", "指标", "阈值"]


def _font_path() -> Path:
    candidates = [
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit("基准作业需要一份可嵌入的字体文件")


def _synthetic_translation(unit: dict[str, Any], ratio: float) -> str:
    """按单元 ID 与原文长度确定性生成合成译文。

    真实译文必须保留数字、统计量、引文编号、缩写、DOI 和 URL，因此合成译文
    也把 `required_anchors` 原样带上。否则内容完整性审计会正确地把语料判为
    需要返修，基准就跑不到排版和预检。
    """

    anchors = unit.get("required_anchors") or {}
    pieces: list[str] = []
    for category, values in anchors.items():
        if not isinstance(values, list):
            continue
        for value in values:
            # 引文编号必须以方括号形式出现才算保留，和真实译文一致。
            pieces.append(
                f"[{value}]" if category == "citations" else str(value)
            )
    carried = " ".join(pieces).strip()
    generator = random.Random(str(unit.get("id") or ""))
    length = max(6, int(len(str(unit.get("source") or "")) * ratio))
    words: list[str] = []
    while sum(len(word) for word in words) < max(length - len(carried), 6):
        words.append(generator.choice(VOCAB))
    body = "".join(words)[: max(length - len(carried), 6)]
    return f"{body}{carried}" if carried else body


def build_jobs(
    jobs_root: Path,
    papers_dir: Path,
    *,
    target_language: str = "zh-Hans",
    translation_ratio: float | None = None,
    sources: dict[str, Path] | None = None,
    tags: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """把一批 PDF 变成可直接跑基准的作业。

    默认使用合成语料；传入 `sources` 时可以换成任意 PDF，例如真实开放获取
    论文。译文仍是合成的，只用来触发同一批代码路径，不是真实翻译。
    """

    jobs_root.mkdir(parents=True, exist_ok=True)
    font = str(_font_path())
    cases: list[dict[str, Any]] = []
    case_tags = tags if tags is not None else CASE_TAGS
    plan = (
        sources
        if sources is not None
        else {name: papers_dir / f"{name}.pdf" for name in BUILDERS}
    )

    for name, source in plan.items():
        if not source.is_file():
            raise SystemExit(f"缺少语料: {source}")
        job_dir = jobs_root / name
        shutil.rmtree(job_dir, ignore_errors=True)
        initialize_job(
            source,
            job_dir,
            target_language,
            "en",
            False,
            producer_id="benchmark-producer",
        )

        # 走真实入口登记"已按原尺寸检查、无复杂页"，与人工流程一致。
        set_complex_content(
            job_dir,
            [],
            confirmed_none=True,
            notes=(
                "合成语料由脚本生成，页面只含普通正文、矩形网格和灰底图块，"
                "无需按复杂页重建；仅用于性能基准，不用于质量验收。"
            ),
        )
        # 字体已由 initialize_job 解析并冻结；这里只在解析结果不可用时兜底。
        job = load_json(job_dir / "job.json")
        if not job["quality"].get("selected_fonts"):
            job["quality"]["selected_fonts"] = [font]
        job["route"]["selected"] = job["route"]["recommended"]
        job["route"]["decision_reason"] = "基准语料：合成样本"
        write_json(job_dir / "job.json", job)

        ratio = (
            translation_ratio
            if translation_ratio is not None
            else CASE_TRANSLATION_RATIO.get(name, 0.55)
        )
        translation = load_json(job_dir / "translation.json")
        for unit in translation["units"]:
            # 合成译文是目标语言的伪词加原样锚点。恒等译文（原文照抄）
            # 已经被译文真实性检查正确拒绝，因此这里不再提供那条捷径。
            unit["translation"] = _synthetic_translation(unit, ratio)
            unit["keep_source_code"] = None
            unit["keep_source_reason"] = None
        translation["terminology_reviewed"] = True
        # 覆盖率按真实性判定重算，不手写。
        refresh_coverage(translation)
        write_json(job_dir / "translation.json", translation)
        if translation["coverage"]["complete"] is not True:
            raise SystemExit(
                f"基准作业 {name} 的合成译文没有通过译文真实性检查: "
                + ", ".join(
                    translation["coverage"]["truthfulness_problem_codes"]
                )
            )

        inventory = load_json(job_dir / "figure_inventory.json")
        inventory["inventory_complete"] = True
        inventory["scope_note"] = "合成语料，无需本地化的图内文字"
        inventory["items"] = []
        write_json(job_dir / "figure_inventory.json", inventory)

        complex_content = load_json(job_dir / "complex_content.json")
        complex_content["classification_complete"] = True
        write_json(job_dir / "complex_content.json", complex_content)

        manifest = load_json(job_dir / "source_manifest.json")
        cases.append(
            {
                "id": name,
                "job_dir": f"{jobs_root.name}/{name}",
                "tags": case_tags.get(name, []),
                "source_pdf": source.name,
                "source_bytes": source.stat().st_size,
                "source_sha256": sha256_file(source),
                "page_count": manifest["page_count"],
                "frozen_unit_count": len(translation["units"]),
                "complex_page_count": len(manifest.get("complex_pages", [])),
                "double_column_page_count": len(
                    manifest.get("double_column_pages", [])
                ),
                "recommended_route": manifest["route"]["recommended"],
                "translation_ratio": ratio,
                "translation_mode": "synthetic",
            }
        )
    return cases


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--papers-dir", type=Path, default=here / "papers")
    parser.add_argument("--jobs-root", type=Path, default=here / "jobs")
    parser.add_argument("--output", type=Path, default=here / "corpus.json")
    parser.add_argument(
        "--translation-ratio",
        type=float,
        default=None,
        help="覆盖每个案例的默认译文字数比例",
    )
    args = parser.parse_args()

    papers = args.papers_dir.resolve()
    if not any(papers.glob("*.pdf")):
        build_corpus(papers)
    cases = build_jobs(
        args.jobs_root.resolve(),
        papers,
        translation_ratio=args.translation_ratio,
    )
    manifest = {
        "schema_version": "1.0",
        "corpus": "synthetic-representative-v1",
        "note": (
            "合成语料，不是真实论文。用于比较重复读取与耗时，"
            "不能替代真实论文的视觉抽查。"
        ),
        "translation_ratio_override": args.translation_ratio,
        "cases": cases,
    }
    write_json(args.output.resolve(), manifest)
    print(f"基准清单已写入: {args.output.resolve()}")
    print(
        f"{'案例':<30}{'页':>4}{'冻结单元':>9}{'复杂页':>8}{'路线':>24}"
    )
    for case in cases:
        print(
            f"{case['id']:<30}{case['page_count']:>4}"
            f"{case['frozen_unit_count']:>9}{case['complex_page_count']:>8}"
            f"{case['recommended_route']:>24}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
