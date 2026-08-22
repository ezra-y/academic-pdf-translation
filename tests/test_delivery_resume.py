"""--resume 只把没做完的门槛做完，绝不回头改历史。

出问题的路是这一条：生成 → 自动返修 → 当前候选是 attempt-2 → 等视觉
结果 → 录入 → --resume。恢复流程如果照旧按"第一轮"绑定，就会把
attempt-2 的候选重新复制进 attempt-1、把指针指回 attempt-1、覆盖第一轮
原有证据——"历史证据不可修改"当场破功。

规矩只有一条：**新建候选才绑定新 attempt，复用已有候选绝不绑定。**

单独运行：
    python3 -m pytest -q tests/test_delivery_resume.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402
import pytest  # noqa: E402
from academic_pdf_translation.delivery.evidence import (  # noqa: E402
    attempt_dir,
    read_current_run,
)
from academic_pdf_translation.delivery.first_delivery import (  # noqa: E402
    run_first_delivery,
)
from academic_pdf_translation.delivery.models import (  # noqa: E402
    BUILD_READY,
    BuildOutcome,
    file_sha256,
)

from deliver_first_candidate import make_resume_builder  # noqa: E402

PRESENT = "AlphaBetaGammaDelta"
ABSENT = "EpsilonZetaEtaTheta"


def _make_pdf(path: Path, texts: list[str]) -> Path:
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    cursor = 100.0
    for text in texts:
        page.insert_text((60, cursor), text, fontsize=12)
        cursor += 40
    document.save(path)
    document.close()
    return path


def _tiny_job(tmp_path: Path):
    source = _make_pdf(tmp_path / "source.pdf", [PRESENT, ABSENT])
    elements = [
        {
            "id": "e1",
            "type": "body",
            "page": 1,
            "bbox": [60, 90, 400, 110],
            "required": True,
        },
        {
            "id": "e2",
            "type": "body",
            "page": 1,
            "bbox": [60, 130, 400, 150],
            "required": True,
        },
    ]
    units = [
        {"id": "u1", "translation": PRESENT},
        {"id": "u2", "translation": ABSENT},
    ]
    bindings = [
        {"unit_id": "u1", "element_id": "e1"},
        {"unit_id": "u2", "element_id": "e2"},
    ]
    return source, elements, units, bindings


def _plan(element_ids: list[str]) -> dict:
    return {
        "schema_version": "2.0",
        "elements": [
            {
                "element_id": element_id,
                "element_type": "body",
                "page": 1,
                "strategy": "translate-and-reflow",
                "renderer": "text",
                "status": "ready",
            }
            for element_id in element_ids
        ],
    }


def _hash_tree(root: Path) -> dict[str, str]:
    """目录里每个文件的哈希。用来证明"一个字节都没动"。"""

    return {
        str(path.relative_to(root)): file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _first_run(tmp_path: Path):
    """跑出一个停在 attempt-2 的运行：首版不合格，返修后合格。"""

    source, elements, units, bindings = _tiny_job(tmp_path)
    bad = _make_pdf(tmp_path / "bad.pdf", [PRESENT])
    fixed = _make_pdf(tmp_path / "fixed.pdf", [PRESENT, ABSENT])

    def build(round_index: int) -> BuildOutcome:
        candidate = bad if round_index == 0 else fixed
        return BuildOutcome(
            status=BUILD_READY,
            candidate_path=candidate,
            candidate_sha256=file_sha256(candidate),
            renderer_build_id="build-r",
            run_id="run-resume",
            attempt_id=f"attempt-{round_index + 1}",
            render_plan_sha256=f"{round_index}" * 64,
            render_plan=_plan(["e1"] if round_index == 0 else ["e1", "e2"]),
        )

    out = tmp_path / "out"
    result = run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=build,
        apply_repair=lambda _plan: None,
        output_dir=out,
        render_pages=False,
    )
    assert result.attempt_id == 2, "前提没成立：这一跑必须停在 attempt-2"
    return (source, elements, units, bindings, out)


def _resume(source, elements, units, bindings, out: Path):
    return run_first_delivery(
        source,
        elements,
        units,
        bindings,
        build=make_resume_builder(out),
        apply_repair=None,
        output_dir=out,
        render_pages=False,
    )


def test_resume_from_attempt2_keeps_attempt2_current(tmp_path: Path) -> None:
    """从第二轮恢复，"现在"还得是第二轮。"""

    source, elements, units, bindings, out = _first_run(tmp_path)
    before = read_current_run(out)
    result = _resume(source, elements, units, bindings, out)
    after = read_current_run(out)
    assert result.attempt_id == 2
    assert result.run_id == "run-resume"
    assert after == before, "恢复不许改动 current-run.json"
    assert after.attempt_id == 2


def test_resume_does_not_overwrite_attempt1(tmp_path: Path) -> None:
    """attempt-1 目录里的每个文件，恢复前后必须逐字节相同。"""

    source, elements, units, bindings, out = _first_run(tmp_path)
    old_dir = attempt_dir(out, "run-resume", 1)
    before = _hash_tree(old_dir)
    assert before, "前提没成立：attempt-1 里应当有证据"
    _resume(source, elements, units, bindings, out)
    assert _hash_tree(old_dir) == before


def test_resume_preserves_attempt1_candidate_hash(tmp_path: Path) -> None:
    """第二轮的候选不许被复制回第一轮。"""

    source, elements, units, bindings, out = _first_run(tmp_path)
    old_candidate = attempt_dir(out, "run-resume", 1) / "candidate.pdf"
    new_candidate = attempt_dir(out, "run-resume", 2) / "candidate.pdf"
    before = file_sha256(old_candidate)
    assert before != file_sha256(new_candidate)
    _resume(source, elements, units, bindings, out)
    assert file_sha256(old_candidate) == before
    assert file_sha256(old_candidate) != file_sha256(new_candidate)


def test_resume_preserves_attempt1_evidence_hashes(tmp_path: Path) -> None:
    """第一轮的证据哈希不变，且它的绑定仍然写着 attempt-1。"""

    source, elements, units, bindings, out = _first_run(tmp_path)
    old_dir = attempt_dir(out, "run-resume", 1)
    evidence = sorted(old_dir.glob("round-1-*.json"))
    assert evidence, "前提没成立：attempt-1 里应当有 round-1 证据"
    before = {path.name: file_sha256(path) for path in evidence}
    _resume(source, elements, units, bindings, out)
    after = {path.name: file_sha256(path) for path in evidence}
    assert after == before
    build_record = json.loads(
        (old_dir / "round-1-build.json").read_text(encoding="utf-8")
    )
    assert build_record["binding"]["attempt_id"] == 1


def test_resume_only_writes_result_into_current_attempt(
    tmp_path: Path,
) -> None:
    """恢复只往当前 attempt 里写；别处一个文件都不许变。"""

    source, elements, units, bindings, out = _first_run(tmp_path)
    before = _hash_tree(out)
    result = _resume(source, elements, units, bindings, out)
    after = _hash_tree(out)

    current_prefix = str(
        attempt_dir(out, "run-resume", 2).relative_to(out)
    )
    changed = {
        name
        for name in set(before) | set(after)
        if before.get(name) != after.get(name)
    }
    assert changed, "恢复总得写点东西，否则这条用例没在测东西"
    for name in changed:
        assert name.startswith(current_prefix), name
    for path in result.evidence.values():
        assert str(attempt_dir(out, "run-resume", 2)) in path, path


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
