"""跨平台字体发现、ReportLab 兼容性探测与角色选择。

单独运行：
    python3 -m pytest -q tests/test_fonts.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from _fixtures import make_job  # noqa: E402
from academic_pdf_translation.contracts import fonts  # noqa: E402

from _common import load_json, write_json  # noqa: E402
from font_preparation import (  # noqa: E402
    fonts_are_current,
    prepare_job_fonts,
    resolve_job_fonts,
)
from pre_render_audit import build_input_readiness_audit  # noqa: E402


def test_linux_font_directories_are_searched() -> None:
    """Linux 上必须搜 Linux 的目录，而且不搜 macOS 的空路径。"""

    found = [str(path) for path in fonts.font_search_dirs("Linux")]
    assert any(value.endswith("/usr/share/fonts") for value in found)
    assert any(value.endswith("/usr/local/share/fonts") for value in found)
    assert any(value.endswith("/.fonts") for value in found)
    assert not any("/System/Library/Fonts" in value for value in found)


def test_windows_font_directories_are_searched() -> None:
    """Windows 上必须搜 Windows 的目录，而且不搜 Linux 的空路径。"""

    found = [str(path) for path in fonts.font_search_dirs("Windows")]
    assert any("Fonts" in value for value in found)
    assert not any(value.startswith("/usr/share/fonts") for value in found)
    assert not any("/System/Library/Fonts" in value for value in found)


def test_macos_font_directories_are_searched() -> None:
    found = [str(path) for path in fonts.font_search_dirs("Darwin")]
    assert any("/System/Library/Fonts" in value for value in found)
    assert not any(value.startswith("/usr/share/fonts") for value in found)


def test_reportlab_incompatible_font_is_rejected(tmp_path: Path) -> None:
    """装不进 ReportLab 的字体必须被拒绝，并且说清楚原因。"""

    broken = tmp_path / "NotAFont-Regular.ttf"
    broken.write_bytes(b"OTTO" + struct.pack(">HHHH", 1, 0, 0, 0) + b"\x00" * 64)
    probe = fonts.probe_reportlab_font(broken)
    assert probe.loadable is False
    assert probe.reason, "被拒绝时必须给出原因"


def test_font_probe_failure_has_clear_reason(tmp_path: Path) -> None:
    """文件根本不存在时也要给出可读的原因，而不是抛异常。"""

    probe = fonts.probe_reportlab_font(tmp_path / "missing.ttf")
    assert probe.loadable is False
    assert "不存在" in probe.reason


def test_extension_alone_does_not_prove_compatibility(tmp_path: Path) -> None:
    """后缀是 .ttf 不代表能用，必须真的装一次。"""

    fake = tmp_path / "Pretend-Regular.ttf"
    fake.write_bytes(b"\x00" * 256)
    assert fonts.probe_reportlab_font(fake).loadable is False


def test_abbreviated_bold_suffix_is_recognized() -> None:
    """msyhbd.ttc 就是 Microsoft YaHei Bold，只认全拼会漏掉真正的粗体。"""

    assert fonts._is_bold_file(Path("msyhbd.ttc")) is True
    assert fonts._is_bold_file(Path("msyh.ttc")) is False
    assert fonts._family_token(Path("msyhbd.ttc")) == fonts._family_token(
        Path("msyh.ttc")
    )


def test_medium_weight_is_not_treated_as_bold() -> None:
    """中等字重不是粗体，不能拿它冒充粗体。"""

    assert fonts._is_bold_file(Path("STHeiti Medium.ttc")) is False


def test_regular_and_bold_fonts_are_distinct_when_available(
    tmp_path: Path,
) -> None:
    """同族粗体存在时必须选它；不存在时必须明确警告，不能静默顶替。"""

    available = [Path("/fake/Fakefamily.ttf"), Path("/fake/Fakefamilybd.ttf")]
    monkey = {
        "/fake/Fakefamily.ttf": fonts.FontProbe(
            path="/fake/Fakefamily.ttf", loadable=True, covered_codepoints=100
        ),
        "/fake/Fakefamilybd.ttf": fonts.FontProbe(
            path="/fake/Fakefamilybd.ttf", loadable=True, covered_codepoints=100
        ),
    }
    original_probe = fonts.probe_reportlab_font
    original_sha = fonts.sha256_file
    fonts.probe_reportlab_font = lambda path, **kwargs: monkey[str(path)]
    fonts.sha256_file = lambda path: "0" * 64
    try:
        resolution = fonts.resolve_fonts(
            ["Fakefamily"],
            available=available,
        )
        assert (
            resolution.selections[fonts.ROLE_REGULAR].path
            != resolution.selections[fonts.ROLE_BOLD].path
        )
        assert resolution.warnings == []

        single = fonts.resolve_fonts(
            ["Fakefamily"],
            available=[Path("/fake/Fakefamily.ttf")],
        )
        assert (
            single.selections[fonts.ROLE_BOLD].source == "regular-fallback"
        )
        assert any(
            "FONT_BOLD_FALLS_BACK_TO_REGULAR" in warning
            for warning in single.warnings
        )
    finally:
        fonts.probe_reportlab_font = original_probe
        fonts.sha256_file = original_sha


def test_font_coverage_uses_a_controlled_table(tmp_path: Path) -> None:
    """字符覆盖测试不能假设某个字符一定不在系统字体里。"""

    available = [Path("/fake/Coverme.ttf")]
    original_probe = fonts.probe_reportlab_font
    original_covers = fonts.font_covers
    original_sha = fonts.sha256_file
    fonts.probe_reportlab_font = lambda path, **kwargs: fonts.FontProbe(
        path=str(path), loadable=True, covered_codepoints=3
    )
    fonts.font_covers = lambda path, characters, subfont_index=None: set(
        characters
    ) <= {"甲", "乙"}
    fonts.sha256_file = lambda path: "0" * 64
    try:
        good = fonts.resolve_fonts(
            ["Coverme"], available=available, required_characters="甲乙"
        )
        assert fonts.ROLE_REGULAR in good.selections
        bad = fonts.resolve_fonts(
            ["Coverme"], available=available, required_characters="丙"
        )
        assert fonts.ROLE_REGULAR not in bad.selections
        assert any(
            "无法显示目标语言" in probe.reason for probe in bad.rejected
        )
    finally:
        fonts.probe_reportlab_font = original_probe
        fonts.font_covers = original_covers
        fonts.sha256_file = original_sha


def test_fresh_job_can_prepare_fonts_before_rendering(tmp_path: Path) -> None:
    """全新作业在排版之前就能把字体准备好。"""

    job_dir = make_job(tmp_path)
    job = load_json(job_dir / "job.json")
    assert job["quality"]["selected_fonts"], "初始化后必须已经冻结字体"
    resolution = resolve_job_fonts(job, job_dir)
    assert fonts.ROLE_REGULAR in resolution.selections
    assert fonts.ROLE_BOLD in resolution.selections
    assert fonts.ROLE_REFERENCE in resolution.selections
    for selection in resolution.selections.values():
        assert Path(selection.path).is_file()
        assert selection.probe.loadable is True


def test_selected_fonts_record_role_and_probe(tmp_path: Path) -> None:
    """字体证据必须带角色、哈希和兼容性探测结果。"""

    job_dir = make_job(tmp_path)
    evidence = load_json(job_dir / "job.json")["quality"][
        "selected_font_evidence"
    ]
    roles = [entry["role"] for entry in evidence]
    assert roles == [
        fonts.ROLE_REGULAR,
        fonts.ROLE_BOLD,
        fonts.ROLE_REFERENCE,
    ]
    for entry in evidence:
        assert len(entry["sha256"]) == 64
        assert entry["probe"]["loadable"] is True


def test_changed_font_evidence_is_reported_before_reselection(
    tmp_path: Path,
) -> None:
    """字体证据失效时先报告，不能静默覆盖。"""

    job_dir = make_job(tmp_path)
    job = load_json(job_dir / "job.json")
    job["quality"]["selected_font_evidence"][0]["sha256"] = "0" * 64
    write_json(job_dir / "job.json", job)

    assert fonts_are_current(load_json(job_dir / "job.json")) is False
    audit = build_input_readiness_audit(job_dir)
    assert any(
        issue["code"] == "SELECTED_FONT_FILE_CHANGED"
        for issue in audit["issues"]
    ), "必须先报告旧证据失效"

    report = prepare_job_fonts(job_dir)
    assert report["status"] == "reselected"
    assert fonts_are_current(load_json(job_dir / "job.json")) is True


def test_prepare_is_idempotent(tmp_path: Path) -> None:
    job_dir = make_job(tmp_path)
    assert prepare_job_fonts(job_dir)["status"] == "unchanged"


@pytest.mark.parametrize(
    "requested,stem,expected_nonzero",
    [
        ("Noto Sans", "NotoSansOriya", False),
        ("Noto Sans", "NotoSans-Regular", True),
        ("Microsoft YaHei", "msyh", True),
        ("Arial", "Arial", True),
    ],
)
def test_family_name_matching(
    requested: str, stem: str, expected_nonzero: bool
) -> None:
    """字体家族名匹配：别名要认，但不能误配到别的文字系统。"""

    score = fonts._match_score(requested, Path(f"{stem}.ttf"))
    assert bool(score) is expected_nonzero


def test_regular_weight_outranks_bold_for_the_regular_role() -> None:
    assert fonts._match_score("Arial", Path("Arial.ttf")) > fonts._match_score(
        "Arial", Path("Arial Bold.ttf")
    )
