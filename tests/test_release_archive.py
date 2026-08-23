"""发布包：允许名单打包，独立复核，装完能跑。

单独运行：
    python3 -m pytest -q tests/test_release_archive.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest  # noqa: E402

from check_release_archive import check_archive  # noqa: E402
from package_release import (  # noqa: E402
    ALLOWED_DIRS,
    ALLOWED_FILES,
    build_archive,
    collect_files,
    declared_versions,
)

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.2.0-rc.1"


@pytest.fixture(scope="module")
def archive(tmp_path_factory) -> Path:
    output = tmp_path_factory.mktemp("dist") / (
        f"academic-pdf-translation-{VERSION}.zip"
    )
    path, _ = build_archive(VERSION, output)
    return path


def _names(archive_path: Path) -> list[str]:
    with zipfile.ZipFile(archive_path) as handle:
        names = handle.namelist()
    prefix = f"academic-pdf-translation-{VERSION}/"
    return [name[len(prefix):] for name in names if name.startswith(prefix)]


def test_release_archive_uses_allowlist() -> None:
    """收集靠允许名单，不靠排除——新增目录默认不进包。"""

    files = collect_files()
    assert files
    allowed_roots = set(ALLOWED_DIRS)
    for relative in files:
        top = relative.parts[0]
        assert (
            relative.as_posix() in ALLOWED_FILES or top in allowed_roots
        ), relative


def test_release_archive_contains_no_git_directory(archive: Path) -> None:
    assert not any(name.startswith(".git/") for name in _names(archive))


def test_release_archive_contains_no_virtualenv(archive: Path) -> None:
    names = _names(archive)
    assert not any(
        name.startswith((".venv/", "venv/")) for name in names
    )


def test_release_archive_contains_no_cache_files(archive: Path) -> None:
    names = _names(archive)
    assert not any("__pycache__" in name for name in names)
    assert not any(name.endswith((".pyc", ".pyo")) for name in names)
    assert not any(".pytest_cache" in name for name in names)


def test_release_archive_contains_no_local_settings(archive: Path) -> None:
    names = _names(archive)
    assert not any("settings.local.json" in name for name in names)
    assert not any(name.startswith("local-dev/") for name in names)


def test_release_archive_excludes_tests_and_corpus(archive: Path) -> None:
    """测试留在源码仓库，但不进用户包；受版权语料一律不分发。"""

    names = _names(archive)
    assert not any(name.startswith("tests/") for name in names)
    assert not any(name.startswith("benchmarks/") for name in names)
    assert not any("jobs-real" in name or "papers-real" in name for name in names)


def test_release_archive_contains_required_entrypoints(archive: Path) -> None:
    names = set(_names(archive))
    for entry in (
        "SKILL.md",
        "README.md",
        "LICENSE",
        "requirements.txt",
        ".claude-plugin/plugin.json",
        "scripts/deliver_first_candidate.py",
        "academic_pdf_translation/__init__.py",
    ):
        assert entry in names, entry


def test_release_archive_version_matches_metadata(archive: Path) -> None:
    versions = declared_versions()
    assert versions["plugin.json"] == versions["pyproject.toml"]
    assert VERSION.startswith(versions["plugin.json"])
    with zipfile.ZipFile(archive) as handle:
        manifest = json.loads(
            handle.read(
                f"academic-pdf-translation-{VERSION}/"
                ".claude-plugin/plugin.json"
            ).decode("utf-8")
        )
    assert manifest["version"] == versions["plugin.json"]


def test_release_archive_can_run_bundle_check_after_extract(
    archive: Path,
) -> None:
    """完整复核：解压后真的跑一遍 check_bundle，零问题才算通过。"""

    assert check_archive(archive) == []


def test_checker_catches_a_polluted_archive(tmp_path: Path) -> None:
    """往包里塞一个禁止文件，检查必须失败——检查器本身也要能失效。"""

    polluted = tmp_path / f"academic-pdf-translation-{VERSION}.zip"
    build_archive(VERSION, polluted)
    with zipfile.ZipFile(polluted, "a") as handle:
        handle.writestr(
            f"academic-pdf-translation-{VERSION}/.claude/settings.local.json",
            "{}",
        )
    problems = check_archive(polluted)
    assert any("settings.local.json" in problem for problem in problems)


def test_checker_catches_bytecode_in_the_archive(tmp_path: Path) -> None:
    """包里混进 __pycache__ 字节码，检查必须失败。"""

    polluted = tmp_path / f"academic-pdf-translation-{VERSION}.zip"
    build_archive(VERSION, polluted)
    with zipfile.ZipFile(polluted, "a") as handle:
        handle.writestr(
            f"academic-pdf-translation-{VERSION}"
            "/scripts/__pycache__/_common.cpython-312.pyc",
            "\x00\x00",
        )
    problems = check_archive(polluted)
    assert any("__pycache__" in problem for problem in problems)
    assert any("解压后含缓存或字节码文件" in problem for problem in problems)


def test_bundle_check_passes_on_a_used_install(tmp_path: Path) -> None:
    """装好的包被用过之后，Python 自己写的 __pycache__ 不算打包问题。"""

    archive_path = tmp_path / f"academic-pdf-translation-{VERSION}.zip"
    build_archive(VERSION, archive_path)
    with zipfile.ZipFile(archive_path) as handle:
        handle.extractall(tmp_path / "install")
    install = tmp_path / "install" / f"academic-pdf-translation-{VERSION}"
    cache = install / "scripts" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "_common.cpython-312.pyc").write_bytes(b"\x00\x00")
    result = subprocess.run(
        [sys.executable, "scripts/check_bundle.py"],
        cwd=install,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
