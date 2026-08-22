"""每个脚本都要能按 README 的写法直接跑起来。

`python3 scripts/X.py` 运行时，sys.path 里只有 scripts/，没有仓库根。
引用 academic_pdf_translation 包的脚本如果不先把根加进去，就会 import 失败——
而这一类失败在 pytest 里看不见，因为 conftest 已经把路径铺好了。

所以这条测试用**子进程**跑，不借 pytest 的环境。

单独运行：
    python3 -m pytest -q tests/test_scripts_runnable.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
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


# --- 检查工具不许弄脏它正在检查的目录 ---------------------------------------


def _clean_copy(target: Path) -> None:
    """按 git 跟踪清单复制一份干净副本，模拟用户拿到的交付物。"""

    # 已跟踪的加上"还没提交但不被忽略的"——后者也会随下一次提交一起发出去，
    # 只看已跟踪清单，新加的文件在这条测试里永远是缺的。
    listing = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if listing.returncode != 0:
        pytest.skip("不在 git 仓库里，无法取交付清单")
    for name in listing.stdout.splitlines():
        source = ROOT / name
        if not source.is_file():
            continue
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


@pytest.mark.parametrize("script", ["check_bundle.py", "self_test.py"])
def test_the_audit_tools_leave_no_bytecode_behind(script: str) -> None:
    """干净安装后按 README 跑一遍，两项都得 PASS。

    这两个工具要审计"交付物里有没有字节码缓存"，可它们自己一导入模块就会
    生成 __pycache__——真在干净环境里跑过一次才发现，新用户照 README 走
    必然看到 SELF TEST FAIL。
    """

    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "bundle"
        target.mkdir()
        _clean_copy(target)
        result = subprocess.run(
            [sys.executable, str(target / "scripts" / script)],
            cwd=target,
            capture_output=True,
            text=True,
            timeout=600,
        )
        leftovers = sorted(
            path.relative_to(target)
            for path in target.rglob("__pycache__")
        )
        assert result.returncode == 0, result.stdout[-800:] + result.stderr[-800:]
        assert leftovers == [], f"{script} 留下了字节码缓存: {leftovers}"
