"""翻译批次仍然保留单元级检查：批处理不能把单条译文的门槛抹平。

这支自己造作业、造伪译文，走一遍批次落盘与回读，确认每个翻译单元
的语言、锚点、编号都还各自受检，而不是只看整批是否返回。
断言逐字保留原样，中文说明本身就是判据文档。

单独运行：
    python3 -m pytest -q tests/test_translation_batch_units.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from _self_test_helpers import _batch_unit_ids, _make_pdf, _zh_stub  # noqa: E402

from _common import (  # noqa: E402
    SkillError,
    load_json,
    write_json,
)
from init_job import initialize_job  # noqa: E402


def test_translation_batches_keep_unit_level_checks() -> None:
    """按批次翻译，但校验仍然逐单元；整批不合格时不写入任何译文。"""

    from apply_translation_batch import (
        _validate_against_batch,
        apply_cached_batches,
        apply_translation_batch,
    )
    from content_anchors import required_anchors
    from plan_translation_batches import (
        group_units,
        plan_translation_batches,
    )

    anchored_source = (
        "Participants (N = 412) reported a 27.3% reduction (p < .001) [14]."
    )
    probe_batch = {
        "batch_id": "batch-probe",
        "units": [
            {
                "id": "probe-unit",
                "source": anchored_source,
                "required_anchors": required_anchors(anchored_source),
            }
        ],
    }
    kept = _validate_against_batch(
        probe_batch,
        [
            {
                "id": "probe-unit",
                "translation": (
                    "参与者（N = 412）报告下降 27.3%（p < .001）[14]。"
                ),
                "keep_source_reason": None,
            }
        ],
    )
    if set(kept) != {"probe-unit"}:
        raise AssertionError("锚点齐全的译文必须被接受")
    for broken in (
        "参与者报告了明显下降。",
        "参与者（N = 412）报告下降 27.3%（p < .001）。",
    ):
        try:
            _validate_against_batch(
                probe_batch,
                [
                    {
                        "id": "probe-unit",
                        "translation": broken,
                        "keep_source_reason": None,
                    }
                ],
            )
        except SkillError:
            continue
        raise AssertionError("丢失必填锚点的译文必须被拦截")

    heading_units = [
        {"id": "u-1", "page": 1, "kind": "heading", "source": "Methods"},
        {"id": "u-2", "page": 1, "kind": "body", "source": "A" * 400},
        {"id": "u-3", "page": 1, "kind": "body", "source": "B" * 400},
    ]
    groups = group_units(
        heading_units,
        min_units=1,
        max_units=1,
        target_chars=1000,
        max_chars=1000,
    )
    if any(
        len(group) == 1 and heading_units[group[0]]["kind"] == "heading"
        for group in groups
    ):
        raise AssertionError("标题不得与它后面的第一段拆到不同批次")

    continuation_units = [
        {"id": "u-1", "page": 1, "kind": "body", "source": "C" * 400},
        {
            "id": "u-2",
            "page": 1,
            "kind": "body",
            "source": "the sentence continues without a final stop",
        },
        {"id": "u-3", "page": 2, "kind": "body", "source": "onto the next page."},
    ]
    continuation_groups = group_units(
        continuation_units,
        min_units=1,
        max_units=1,
        target_chars=100,
        max_chars=100,
    )
    for group in continuation_groups:
        ids = {continuation_units[index]["id"] for index in group}
        if "u-2" in ids and "u-3" not in ids:
            raise AssertionError("跨页续句必须留在同一批次")

    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        source = root / "batching-source.pdf"
        _make_pdf(
            source,
            [
                [
                    "Adaptive cache invalidation in distributed systems.",
                    "We evaluate three deployment sites over twelve months.",
                    "Stale reads drop once invalidation is coordinated.",
                ],
                [
                    "Method. Each site ran the same workload generator.",
                    "Results. Latency stayed within the agreed envelope.",
                    "Discussion. Limitations and future work follow.",
                ],
            ],
        )
        job_dir = root / "job"
        initialize_job(
            source,
            job_dir,
            "zh-Hans",
            "en",
            False,
            producer_id="self-test-producer",
        )
        try:
            plan_translation_batches(job_dir, min_units=2, max_units=4)
        except SkillError:
            pass
        else:
            raise AssertionError("术语表未确认时不得正式编排批次")
        reviewed = load_json(job_dir / "translation.json")
        reviewed["terminology_reviewed"] = True
        write_json(job_dir / "translation.json", reviewed)
        plan = plan_translation_batches(
            job_dir,
            min_units=2,
            max_units=4,
            target_chars=1000,
            max_chars=2000,
            model="self-test-model",
        )
        if plan["batch_count"] < 2:
            raise AssertionError("自测样本应至少编排出两个批次")
        planned_ids = [
            unit_id
            for entry in plan["batches"]
            for unit_id in _batch_unit_ids(job_dir, entry)
        ]
        translation = load_json(job_dir / "translation.json")
        frozen_ids = [str(unit["id"]) for unit in translation["units"]]
        if planned_ids != frozen_ids:
            raise AssertionError(
                "批次必须恰好覆盖全部冻结单元一次，且保持原有顺序"
            )

        first = plan["batches"][0]
        batch = load_json(job_dir / first["file"])
        good = [
            {
                "id": unit["id"],
                "translation": _zh_stub(unit["source"]),
                "keep_source_reason": None,
                "review_flags": [],
            }
            for unit in batch["units"]
        ]
        try:
            apply_translation_batch(job_dir, first["batch_id"], good[:-1])
        except SkillError:
            pass
        else:
            raise AssertionError("批次结果数量不足时必须整批拒绝")
        if any(
            unit.get("translation")
            for unit in load_json(job_dir / "translation.json")["units"]
        ):
            raise AssertionError("整批拒绝后不得写入任何译文")

        try:
            apply_translation_batch(
                job_dir,
                first["batch_id"],
                good,
                model="another-model",
            )
        except SkillError:
            pass
        else:
            raise AssertionError("实际模型与计划模型不一致时必须拒绝写回")

        report = apply_translation_batch(
            job_dir,
            first["batch_id"],
            good,
            model="self-test-model",
        )
        if report["applied_units"] != len(good):
            raise AssertionError("合格批次必须整批写入")

        replanned = plan_translation_batches(
            job_dir,
            min_units=2,
            max_units=4,
            target_chars=1000,
            max_chars=2000,
            model="self-test-model",
        )
        if replanned["batches"][0]["status"] != "applied":
            raise AssertionError("重新编排必须保留已完成批次，支持断点续跑")
        if any(
            entry["status"] == "applied"
            for entry in replanned["batches"][1:]
        ):
            raise AssertionError("未翻译的批次不得被标记为已完成")

        cleared = load_json(job_dir / "translation.json")
        for unit in cleared["units"]:
            unit["translation"] = None
        write_json(job_dir / "translation.json", cleared)
        restored_plan = load_json(job_dir / "translation-plan.json")
        for entry in restored_plan["batches"]:
            entry["status"] = "pending"
        write_json(job_dir / "translation-plan.json", restored_plan)
        write_json(
            job_dir / "translation.json",
            cleared,
        )
        if apply_cached_batches(job_dir) != [first["batch_id"]]:
            raise AssertionError("缓存必须能直接写回已完成批次")
        recovered = load_json(job_dir / "translation.json")
        if sum(
            1 for unit in recovered["units"] if unit.get("translation")
        ) != len(good):
            raise AssertionError("缓存写回后译文数量必须还原")
