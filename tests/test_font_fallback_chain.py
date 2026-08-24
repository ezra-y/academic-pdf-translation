"""字体回退链：匹配失败继续找，符号与中文分开检查。

单独运行：
    python3 -m pytest -q tests/test_font_fallback_chain.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest  # noqa: E402
from academic_pdf_translation.contracts.fonts import (  # noqa: E402
    ROLE_MATH,
    ROLE_REGULAR,
    _score_fallbacks,
    discover_font_files,
    font_covers,
    probe_reportlab_font,
    resolve_fonts,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = ROOT / "benchmarks" / "jobs-real" / "real-translation"


def _loadable_cjk_font() -> Path | None:
    for path in discover_font_files():
        probe = probe_reportlab_font(path)
        if probe.loadable and font_covers(path, "中文样本", probe.subfont_index):
            return path
    return None


def test_incompatible_preferred_font_falls_through() -> None:
    """偏好名字匹配上但全部加载失败时，必须继续试剩下的字体。"""

    real = _loadable_cjk_font()
    if real is None:
        pytest.skip("本机没有可加载的中文字体")
    broken = ROOT / "does-not-exist" / "PreferredFake-Regular.ttf"
    resolution = resolve_fonts(
        ["PreferredFake"],
        required_characters="中文样本",
        available=[broken, real],
    )
    assert ROLE_REGULAR in resolution.selections
    assert Path(resolution.selections[ROLE_REGULAR].path) == real


def test_unmatched_but_loadable_cjk_font_can_be_selected() -> None:
    """名字完全不匹配的字体也要进第二阶段队列。"""

    real = _loadable_cjk_font()
    if real is None:
        pytest.skip("本机没有可加载的中文字体")
    resolution = resolve_fonts(
        ["NoSuchFamilyAtAll"],
        required_characters="中文样本",
        available=[real],
    )
    assert ROLE_REGULAR in resolution.selections


def test_rejected_font_reason_is_recorded() -> None:
    """被拒的候选必须留下原因，不许静默跳过。"""

    broken = ROOT / "does-not-exist" / "Broken.ttf"
    resolution = resolve_fonts(
        ["Broken"],
        required_characters="中文样本",
        available=[broken],
    )
    assert ROLE_REGULAR not in resolution.selections
    assert resolution.rejected
    assert all(probe.reason for probe in resolution.rejected)


def test_font_selection_is_deterministic() -> None:
    """同样的输入必须选出同样的字体：第二阶段排序是确定的。"""

    files = discover_font_files()[:40]
    if not files:
        pytest.skip("本机没有可发现的字体")
    first = _score_fallbacks(list(files))
    second = _score_fallbacks(list(reversed(files)))
    assert first == second


def test_cjk_and_math_can_use_different_fonts() -> None:
    """核心中文与数学符号分开检查：正文覆盖不了的符号交给后备角色。"""

    real = _loadable_cjk_font()
    if real is None:
        pytest.skip("本机没有可加载的中文字体")
    symbol_font = None
    for path in discover_font_files():
        probe = probe_reportlab_font(path)
        if probe.loadable and font_covers(path, "∈Ω", probe.subfont_index):
            symbol_font = path
            break
    if symbol_font is None:
        pytest.skip("本机没有覆盖 ∈Ω 的字体")
    resolution = resolve_fonts(
        [],
        required_characters="中文样本",
        math_characters="∈Ω",
    )
    if not font_covers(
        Path(resolution.selections[ROLE_REGULAR].path), "∈Ω"
    ):
        assert ROLE_MATH in resolution.selections or resolution.warnings


def test_stale_macos_path_is_reselected_on_linux(tmp_path: Path) -> None:
    """冻结的绝对字体路径在当前系统不存在时，冻结作废并重新解析。"""

    if not (REAL_JOB / "job.json").is_file():
        pytest.skip("缺少真实论文作业；真实论文受版权保护不入库")
    job_dir = tmp_path / "job"
    shutil.copytree(REAL_JOB, job_dir)
    import json

    from font_preparation import prepare_job_fonts

    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    job.setdefault("quality", {})["selected_fonts"] = [
        "/Users/example/Library/Fonts/gone.ttc",
        "/Users/example/Library/Fonts/gone-bd.ttc",
        "/Users/example/Library/Fonts/gone.ttc",
    ]
    (job_dir / "job.json").write_text(
        json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = prepare_job_fonts(job_dir)
    assert report["status"] == "reselected"
    assert "stale_evidence" in report
    for path in report["selected_fonts"]:
        assert Path(path).is_file()


def test_real_job_gets_a_math_fallback_and_builds(tmp_path: Path) -> None:
    """真实论文：符号后备选出来后，构建通过且字体合同一致。"""

    if not (REAL_JOB / "source.pdf").is_file():
        pytest.skip("缺少真实论文作业；真实论文受版权保护不入库")
    job_dir = tmp_path / "job"
    shutil.copytree(REAL_JOB, job_dir)
    from build_first_candidate import build_first_candidate

    report = build_first_candidate(job_dir, None)
    assert report["status"] == "READY_TO_REGISTER", report.get("issues")
    import json

    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    fonts = job["quality"]["selected_fonts"]
    # 渲染合同里声明的字体必须与冻结字体一致（含后备）
    log = json.loads(
        (job_dir / "generator-layout-log.json").read_text(encoding="utf-8")
    )
    declared = log["render_contract"]["font_paths"]
    assert sorted(
        str(Path(path).resolve()) for path in declared
    ) == sorted(str(Path(path).resolve()) for path in fonts)
