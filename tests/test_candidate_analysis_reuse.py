"""一次预检里候选 PDF 只完整解析一次。

单独运行：
    python3 -m pytest -q tests/test_candidate_analysis_reuse.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixtures import (  # noqa: E402
    load_batch,
    make_job,
    plan,
    translated_results,
)

import perf_trace  # noqa: E402
from _common import load_json, write_json  # noqa: E402
from apply_translation_batch import apply_translation_batch  # noqa: E402
from build_candidate import (  # noqa: E402
    RENDERER_NAME,
    RENDERER_VERSION,
    build_candidate,
)
from candidate_analysis import (  # noqa: E402
    active_paths,
    candidate_analysis,
    open_candidate_analysis,
)
from preflight_candidate import preflight_candidate  # noqa: E402
from renderer_identity import renderer_build_id  # noqa: E402
from set_complex_content import set_complex_content  # noqa: E402
from validate_job import validate_job  # noqa: E402

MODEL = "fake-batch-model-v1"


def _ready_job(tmp_path: Path) -> Path:
    job_dir = make_job(tmp_path)
    plan(job_dir, model=MODEL)
    batch = load_batch(job_dir)
    apply_translation_batch(
        job_dir,
        batch["batch_id"],
        translated_results(batch),
        model=MODEL,
    )
    set_complex_content(
        job_dir,
        [],
        confirmed_none=True,
        notes="合成测试论文，全部为规则正文。",
    )
    job = load_json(job_dir / "job.json")
    job["route"]["selected"] = job["route"]["recommended"]
    job["route"]["decision_reason"] = "合成测试论文，按推荐路线执行。"
    write_json(job_dir / "job.json", job)
    translation = load_json(job_dir / "translation.json")
    translation["terminology_reviewed"] = True
    write_json(job_dir / "translation.json", translation)
    inventory = load_json(job_dir / "figure_inventory.json")
    inventory["inventory_complete"] = True
    inventory["scope_note"] = "合成测试论文无图表。"
    write_json(job_dir / "figure_inventory.json", inventory)
    validate_job(job_dir, "translated", advance=True)
    return job_dir


def test_repeated_open_reuses_one_document(tmp_path: Path) -> None:
    """同一路径重复打开只解析一次，引用计数归零才真的关闭。"""

    job_dir = make_job(tmp_path)
    source = job_dir / "source.pdf"
    perf_trace.reset()
    first = open_candidate_analysis(source, role="source")
    second = open_candidate_analysis(source, role="source")
    assert first is second
    assert perf_trace.counter(perf_trace.COUNTER_PDF_OPEN) == 1
    first.release()
    assert active_paths(), "还有使用者时不得关闭"
    second.release()
    assert active_paths() == []


def test_page_text_is_extracted_once_per_page(tmp_path: Path) -> None:
    """同一页的文字只抽一次，之后命中缓存。"""

    job_dir = make_job(tmp_path)
    perf_trace.reset()
    with candidate_analysis(job_dir / "source.pdf", role="source") as analysis:
        analysis.document_text()
        analysis.document_text()
        extracted = perf_trace.counter("candidate_text_extract")
        assert extracted == analysis.page_count
        assert perf_trace.counter("candidate_analysis_reuse") >= (
            analysis.page_count
        )


def test_candidate_pdf_analysis_is_reused(tmp_path: Path) -> None:
    """一次注册前预检里，影子候选只被完整打开一次。"""

    job_dir = _ready_job(tmp_path)
    candidate_pdf = job_dir / "staging" / "candidate-test.pdf"
    candidate_pdf.parent.mkdir(parents=True, exist_ok=True)
    build_candidate(job_dir, candidate_pdf)
    from pre_render_audit import build_pre_render_audit

    build_pre_render_audit(job_dir)

    perf_trace.reset()
    preflight_candidate(
        job_dir,
        candidate_pdf,
        RENDERER_NAME,
        RENDERER_VERSION,
        renderer_build_id(),
    )
    opens = perf_trace.counter(perf_trace.COUNTER_CANDIDATE_PDF_OPEN)
    reuse = perf_trace.counter("candidate_analysis_reuse")
    assert opens <= 2, (
        f"预检期间候选 PDF 被打开 {opens} 次；"
        "QA、作业校验和完整性审查应当共用同一次分析"
    )
    assert reuse >= 2, (
        "共享分析应当至少被复用两次（QA、作业校验、完整性审查）"
    )
    assert active_paths() == [], "预检结束后不得留下未关闭的候选分析"


def test_every_production_pdf_open_is_counted() -> None:
    """生产脚本里不得再出现未计数的 fitz.open。"""

    import ast

    scripts = Path(__file__).resolve().parent.parent / "scripts"
    offenders: list[str] = []
    for path in sorted(scripts.glob("*.py")):
        if path.name in {"self_test.py", "_common.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "open":
                continue
            base = func.value
            named_fitz = (
                isinstance(base, ast.Name) and base.id == "fitz"
            ) or (
                isinstance(base, ast.Call)
                and isinstance(base.func, ast.Name)
                and base.func.id == "import_fitz"
            )
            if named_fitz and node.args:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "以下位置直接调用 fitz.open，没有进入性能计数: "
        + ", ".join(offenders)
    )
