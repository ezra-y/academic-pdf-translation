"""性能报告必须与当前源码对得上。

单独运行：
    python3 -m pytest -q tests/test_benchmark_provenance.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from renderer_identity import RENDERER_INPUTS, renderer_build_id  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "benchmarks" / "results"
OPTIMIZED = RESULTS / "optimized.json"
BASELINE = RESULTS / "baseline.json"
COMPARISON = RESULTS / "comparison.md"

REQUIRED_TOP_LEVEL = (
    "schema_version",
    "label",
    "provenance",
    "repeats",
    "model_translation",
    "cases",
    "totals",
)


def _load(path: Path) -> dict:
    assert path.is_file(), f"缺少基准报告: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_benchmark_build_id_matches_current_code() -> None:
    """当前性能报告的构建哈希必须等于当前源码的构建哈希。"""

    report = _load(OPTIMIZED)
    assert report["provenance"]["renderer_build_id"] == renderer_build_id(), (
        "benchmarks/results/optimized.json 是用别的代码跑出来的；"
        "请重新运行 benchmarks/run_benchmark.py"
    )


def test_baseline_is_a_different_build() -> None:
    """基线必须是另一份构建，否则对比没有意义。"""

    baseline = _load(BASELINE)
    optimized = _load(OPTIMIZED)
    assert (
        baseline["provenance"]["renderer_build_id"]
        != optimized["provenance"]["renderer_build_id"]
    )
    assert baseline["provenance"]["git_commit"]
    assert optimized["provenance"]["git_commit"]


@pytest.mark.parametrize("path", [BASELINE, OPTIMIZED])
def test_report_structure_matches_the_generator(path: Path) -> None:
    """两份报告的结构必须一致，且与生成脚本的输出结构相同。"""

    report = _load(path)
    for key in REQUIRED_TOP_LEVEL:
        assert key in report, f"{path.name} 缺少字段 {key!r}"
    assert report["repeats"] >= 3, "冷启动与缓存状态各需要至少 3 次"
    assert report["cases"], f"{path.name} 没有案例"
    for case in report["cases"]:
        for state in ("cold", "warm"):
            assert len(case[state]["seconds"]) == report["repeats"]
            assert case[state]["median_seconds"] > 0
            assert "translation_stage_seconds" in case[state]
            assert "pdf_stage_seconds" in case[state]
            assert "counters" in case[state]


@pytest.mark.parametrize("path", [BASELINE, OPTIMIZED])
def test_model_translation_is_marked_unmeasured(path: Path) -> None:
    """没有真实模型数据时，模型阶段必须显式标记未测量，不能填估算值。"""

    report = _load(path)
    model = report["model_translation"]
    assert model["measured"] is False
    assert model["reason"]
    for key in (
        "model",
        "model_calls",
        "batches",
        "retries",
        "input_tokens",
        "output_tokens",
        "seconds",
    ):
        assert model[key] is None, f"{key} 未测量却填了值"


def test_corpus_covers_the_five_required_layout_classes() -> None:
    """语料至少覆盖普通正文、双栏、复杂图表、图片较多、参考文献较多。"""

    corpus = json.loads(
        (ROOT / "benchmarks" / "corpus.json").read_text(encoding="utf-8")
    )
    tags = {tag for case in corpus["cases"] for tag in case.get("tags", [])}
    for required in (
        "body",
        "two-column",
        "table",
        "image",
        "references",
    ):
        assert required in tags, f"语料缺少 {required} 类版式"


def test_comparison_separates_translation_and_pdf_time() -> None:
    """对比文档必须明确区分翻译时间与 PDF 生成时间，并标注模型未测量。"""

    text = COMPARISON.read_text(encoding="utf-8")
    assert "未测量" in text
    assert "translation_stage_seconds" in text
    assert "pdf_stage_seconds" in text
    optimized = _load(OPTIMIZED)
    assert optimized["provenance"]["renderer_build_id"] in text


def test_renderer_inputs_cover_the_modules_that_change_output() -> None:
    """影响排版输出的模块都要计入构建哈希，否则报告绑不住代码。"""

    for relative in (
        "scripts/build_candidate.py",
        "scripts/font_preparation.py",
        "scripts/candidate_analysis.py",
    ):
        assert relative in RENDERER_INPUTS, f"{relative} 未计入 renderer_build_id"
