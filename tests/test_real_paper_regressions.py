"""真实论文暴露出来的六个缺陷，逐条钉死。

这些用例全部来自一次真实翻译：Ronneberger 等人的 U-Net
（arXiv:1505.04597）。合成语料跑不出这些情况，真实版式一上来就撞上了。

单独运行：
    python3 -m pytest -q tests/test_real_paper_regressions.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import load_json, write_json  # noqa: E402
from _fixtures import load_batch, make_job, plan  # noqa: E402
from audit_translation_completeness import (  # noqa: E402
    _unit_compression_flags,
)
from build_candidate import _markup  # noqa: E402
from pre_render_audit import _font_coverage_issues  # noqa: E402
from translation_truthfulness import (  # noqa: E402
    is_prose,
    residual_latin_letters,
    target_script_ratio,
)
from validate_job import _validate_source_text_coverage  # noqa: E402


def test_contact_line_is_not_treated_as_prose() -> None:
    """作者邮箱加主页那一行不是散文，不能按散文要求目标语言占比。"""

    contact = (
        "ronneber@informatik.uni-freiburg.de, WWW home page: "
        "http://lmb.informatik.uni-freiburg.de/"
    )
    assert is_prose(contact) is False
    translated = (
        "联系邮箱 ronneber@informatik.uni-freiburg.de，"
        "课题组主页：http://lmb.informatik.uni-freiburg.de/"
    )
    assert residual_latin_letters(translated) == 0
    assert target_script_ratio(translated, "han") == 1.0
    # 真正的散文仍然按散文处理，这条规则不能把正文也放过去。
    assert is_prose(
        "There is large consent that successful training of deep networks "
        "requires many thousand annotated training samples."
    )


def test_arxiv_stamp_is_an_identifier() -> None:
    """页边的 arXiv 编号是持久标识符，不算没翻的英文。"""

    stamp = "arXiv:1505.04597v1 [cs.CV] 2015 年 5 月 18 日"
    assert residual_latin_letters(stamp) == 0
    assert target_script_ratio(stamp, "han") == 1.0


def test_batch_payload_carries_source_bbox(tmp_path: Path) -> None:
    """批次必须带上坐标，否则参考文献保留区域的证据在写回时核不了。"""

    job_dir = make_job(tmp_path)
    plan(job_dir)
    batch = load_batch(job_dir)
    for unit in batch["units"]:
        assert "source_bbox" in unit, f"{unit['id']} 缺少 source_bbox"
        assert isinstance(unit["source_bbox"], list)
        assert len(unit["source_bbox"]) == 4


def test_symbol_only_unit_is_verified_by_coordinates(tmp_path: Path) -> None:
    """整段只有符号的公式片段，靠文本匹配定位不了，应改用坐标核对。"""

    job_dir = make_job(tmp_path)
    translation = load_json(job_dir / "translation.json")
    reference = translation["units"][0]
    translation["units"].append(
        {
            "id": "p0001-u9001",
            "source_ref": "p0001-u9001",
            "page": reference["page"],
            "kind": "body",
            "source": "!",
            "source_bbox": reference["source_bbox"],
            "translation": None,
            "keep_source_code": "formula-or-statistical-symbol",
            "keep_source_reason": "公式排版符号",
            "review_flags": [],
        }
    )
    write_json(job_dir / "translation.json", translation)

    errors: list[str] = []
    warnings: list[str] = []
    _validate_source_text_coverage(
        job_dir / "source.pdf",
        translation,
        load_json(job_dir / "retained_source.json"),
        errors,
        warnings,
    )
    assert not any("无法在对应原文页定位" in error for error in errors)
    assert any("只含符号" in warning for warning in warnings)

    # 坐标不合法时仍然要算没定位上，不能变成免检通道。
    translation["units"][-1]["source_bbox"] = [0, 0, 0, 0]
    write_json(job_dir / "translation.json", translation)
    errors = []
    warnings = []
    _validate_source_text_coverage(
        job_dir / "source.pdf",
        translation,
        load_json(job_dir / "retained_source.json"),
        errors,
        warnings,
    )
    assert any("无法在对应原文页定位" in error for error in errors)


def test_validated_keep_source_unit_is_not_a_compression_failure() -> None:
    """通过检查的保留原文单元没有译文，不该被判成"译文严重压缩"。"""

    args = ("body", 42, 0.0, 0.2, 0.25)
    assert _unit_compression_flags(*args) == [
        "SEVERE_TRANSLATION_COMPRESSION"
    ]
    assert _unit_compression_flags(*args, validated_keep_source=True) == []


def test_font_coverage_gap_is_reported_before_rendering(
    tmp_path: Path,
) -> None:
    """字体画不出的字符要在渲染前指出来，而不是事后报几个空字符。"""

    job_dir = make_job(tmp_path)
    fonts = [
        entry["path"]
        for entry in load_json(job_dir / "job.json")["quality"][
            "selected_font_evidence"
        ]
    ]
    # 私用区字符没有任何字体会去覆盖，这个判据在哪台机器上都成立。
    # 换成具体的数学符号（比如 ⊂）会看运气：macOS 的雅黑画不出，
    # Linux 的 uming 画得出，测试就会随环境飘。
    uncovered = "\ue0ff"
    document = {
        "units": [
            {
                "id": "p0001-u0001",
                "translation": f"样本包含 Ω{uncovered}Z2 的子集。",
            },
            {"id": "p0001-u0002", "translation": "这一段字体都画得出来。"},
        ]
    }
    issues = _font_coverage_issues(fonts, document)
    assert issues, "私用区字符没有任何字体覆盖，本应报出来"
    characters = {item["character"] for item in issues[0]["characters"]}
    assert uncovered in characters
    assert issues[0]["code"] == "FONT_CHARACTER_COVERAGE_GAP"
    assert "样" not in characters, "画得出来的字不该被报成缺口"

    # 排版器会自己处理掉的字符不重复报，否则检查和渲染互相矛盾。
    clean = {
        "units": [
            {"id": "p0001-u0003", "translation": "控制字符\x11与连字 Caﬀe"},
        ]
    }
    assert _font_coverage_issues(fonts, clean) == []


def test_renderer_drops_control_characters_and_ligatures() -> None:
    """控制字符与排版连字不得进入候选 PDF。"""

    rendered = _markup("Caﬀe \x11 \x10PK")
    assert "ﬀ" not in rendered
    assert "Caffe" in rendered
    assert "\x11" not in rendered
    assert "\x10" not in rendered


def test_element_roles_veto_frozen_heading_labels() -> None:
    """冻结单元的 kind/heading_level 是旧启发式打的标签。

    结构分析说这个单元是作者单位或正文，冻结标签就不能再把它排成标题。
    角色未知时不否决，维持原行为。
    """

    from build_candidate import _role_may_head

    assert _role_may_head({"_element_role": "heading"}) is True
    assert _role_may_head({"_element_role": "document-title"}) is True
    assert _role_may_head({}) is True
    for role in ("affiliation", "publication-metadata", "figure-label", "body"):
        assert _role_may_head({"_element_role": role}) is False, role
