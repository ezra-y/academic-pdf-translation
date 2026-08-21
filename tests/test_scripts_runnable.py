"""每个脚本都要能按 README 的写法直接跑起来。

`python3 scripts/X.py` 运行时，sys.path 里只有 scripts/，没有仓库根。
引用 academic_pdf_translation 包的脚本如果不先把根加进去，就会 import 失败——
而这一类失败在 pytest 里看不见，因为 conftest 已经把路径铺好了。

所以这条测试用**子进程**跑，不借 pytest 的环境。

单独运行：
    python3 -m pytest -q tests/test_scripts_runnable.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

#: 这些不是命令行入口，是被别的脚本 import 的共用模块。
LIBRARY_ONLY = {
    "_common.py",
    "i18n.py",
    "cjk_markup.py",
    "reportlab_layout.py",
    "typography_fit.py",
    "content_anchors.py",
    "semantic_markers.py",
    "review_policy.py",
    "renderer_identity.py",
    "translation_cache.py",
    "run_metrics.py",
    "pdf_profile.py",
}


def _entry_points() -> list[Path]:
    return sorted(
        path
        for path in SCRIPTS.glob("*.py")
        if path.name not in LIBRARY_ONLY
        and "argparse" in path.read_text(encoding="utf-8")
    )


def _package_users() -> list[Path]:
    return sorted(
        path
        for path in SCRIPTS.glob("*.py")
        if "academic_pdf_translation" in path.read_text(encoding="utf-8")
    )


def test_there_are_scripts_to_check() -> None:
    assert _entry_points()
    assert _package_users()


@pytest.mark.parametrize(
    "script", _entry_points(), ids=lambda path: path.name
)
def test_every_entry_point_answers_help(script: Path) -> None:
    """--help 跑通就说明所有模块级 import 都成立。"""

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"{script.name} 无法直接运行:\n{result.stderr[-1200:]}"
    )


@pytest.mark.parametrize(
    "script", _package_users(), ids=lambda path: path.name
)
def test_package_users_bootstrap_the_repo_root(script: Path) -> None:
    """引用包的脚本必须自己把仓库根加进 sys.path。

    少了这一句，README 里写的 `python3 scripts/X.py` 就跑不起来，
    而在 pytest 里完全看不出来。
    """

    text = script.read_text(encoding="utf-8")
    assert "sys.path.insert" in text, (
        f"{script.name} 引用了 academic_pdf_translation，"
        "但没有把仓库根加进 sys.path"
    )
