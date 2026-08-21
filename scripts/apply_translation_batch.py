"""把一个翻译批次的结果写回 translation.json。

批次负责执行，校验仍然逐单元进行：ID 必须存在、不得重复、不得修改原文、
数量必须与批次一致、必填锚点不能丢失。任何一项不满足就整批拒绝，
translation.json 保持原样。

每批成功后原子写入并记录状态，因此中断可以从最后一个成功批次继续，
修改第 8 批也不会重新翻译前 7 批。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import perf_trace
from _common import SkillError, load_json, sha256_file, utc_now, write_json
from content_anchors import anchors_present
from plan_translation_batches import PLAN_FILE_NAME, load_plan
from translation_cache import TranslationCache


ALLOWED_RESULT_KEYS = {
    "id",
    "translation",
    "keep_source_reason",
    "review_flags",
}


def _normalize_results(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("results", "units", "translations"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise SkillError("批次结果必须是数组，或含 results/units 数组的对象")
    results: list[dict[str, Any]] = []
    for index, item in enumerate(payload, 1):
        if not isinstance(item, dict):
            raise SkillError(f"批次结果第 {index} 项不是对象")
        unknown = set(item) - ALLOWED_RESULT_KEYS
        if unknown:
            raise SkillError(
                f"批次结果第 {index} 项含未批准字段: "
                + ", ".join(sorted(unknown))
            )
        results.append(item)
    return results


def _validate_against_batch(
    batch: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    expected = {
        str(unit["id"]): unit
        for unit in batch.get("units", [])
        if isinstance(unit, dict) and str(unit.get("id") or "")
    }
    if len(results) != len(expected):
        raise SkillError(
            f"批次 {batch['batch_id']} 需要 {len(expected)} 条结果，"
            f"实际收到 {len(results)} 条"
        )

    seen: set[str] = set()
    problems: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for item in results:
        unit_id = str(item.get("id") or "")
        if unit_id not in expected:
            problems.append(f"{unit_id or '<空 id>'}: 不属于本批次")
            continue
        if unit_id in seen:
            problems.append(f"{unit_id}: 重复出现")
            continue
        seen.add(unit_id)

        translation = item.get("translation")
        keep_reason = item.get("keep_source_reason")
        has_translation = isinstance(translation, str) and translation.strip()
        has_keep_reason = isinstance(keep_reason, str) and keep_reason.strip()
        if not has_translation and not has_keep_reason:
            problems.append(
                f"{unit_id}: 既没有译文，也没有写明保留原文的理由"
            )
            continue
        if has_translation and has_keep_reason:
            problems.append(
                f"{unit_id}: 不能同时给出译文和保留原文理由"
            )
            continue

        flags = item.get("review_flags", [])
        if flags is not None and not isinstance(flags, list):
            problems.append(f"{unit_id}: review_flags 必须是数组")
            continue

        if has_translation:
            missing = anchors_present(
                expected[unit_id].get("required_anchors") or {},
                str(translation),
            )
            lost = {
                category: values
                for category, values in missing.items()
                if values
            }
            if lost:
                detail = "; ".join(
                    f"{category}: {', '.join(map(str, values))}"
                    for category, values in sorted(lost.items())
                )
                problems.append(f"{unit_id}: 译文丢失必填锚点 [{detail}]")
                continue
        by_id[unit_id] = item

    missing_ids = sorted(set(expected) - seen)
    if missing_ids:
        problems.append("缺少结果: " + ", ".join(missing_ids))
    if problems:
        raise SkillError(
            f"批次 {batch['batch_id']} 校验失败，未写入任何译文:\n"
            + "\n".join(f"- {problem}" for problem in problems)
        )
    return by_id


def _refresh_coverage(translation: dict[str, Any]) -> dict[str, Any]:
    units = [
        unit for unit in translation.get("units", []) if isinstance(unit, dict)
    ]
    translated = sum(
        1
        for unit in units
        if str(unit.get("translation") or "").strip()
    )
    kept = sum(
        1
        for unit in units
        if str(unit.get("keep_source_reason") or "").strip()
    )
    coverage = translation.setdefault("coverage", {})
    coverage["source_units_total"] = len(units)
    coverage["translated_units"] = translated
    coverage["kept_source_units"] = kept
    coverage["complete"] = translated + kept == len(units)
    return coverage


def apply_translation_batch(
    job_dir: Path,
    batch_id: str,
    results: list[dict[str, Any]],
    *,
    elapsed_seconds: float | None = None,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    retries: int = 0,
) -> dict[str, Any]:
    job_dir = Path(job_dir).resolve()
    job = load_json(job_dir / "job.json")
    translation_path = job_dir / job["files"]["translation"]
    plan = load_plan(job_dir)

    entry = next(
        (
            item
            for item in plan.get("batches", [])
            if str(item.get("batch_id")) == batch_id
        ),
        None,
    )
    if entry is None:
        raise SkillError(f"翻译计划中没有批次 {batch_id}")
    batch = load_json(job_dir / entry["file"])

    perf_trace.count(perf_trace.COUNTER_TRANSLATION_BATCH)
    normalized = _normalize_results(results)
    accepted = _validate_against_batch(batch, normalized)

    translation = load_json(translation_path)
    index = {
        str(unit.get("id")): unit
        for unit in translation.get("units", [])
        if isinstance(unit, dict)
    }
    for unit_id, item in accepted.items():
        unit = index.get(unit_id)
        if unit is None:
            raise SkillError(
                f"translation.json 中缺少批次单元 {unit_id}，请重新编排批次"
            )
        source_unit = next(
            source
            for source in batch["units"]
            if str(source["id"]) == unit_id
        )
        if str(unit.get("source") or "") != str(source_unit.get("source") or ""):
            raise SkillError(
                f"{unit_id}: 原文与批次不一致，批次已过期，请重新编排"
            )
        translated = item.get("translation")
        keep_reason = item.get("keep_source_reason")
        unit["translation"] = (
            str(translated).strip()
            if isinstance(translated, str) and translated.strip()
            else None
        )
        unit["keep_source_reason"] = (
            str(keep_reason).strip()
            if isinstance(keep_reason, str) and keep_reason.strip()
            else None
        )
        flags = item.get("review_flags")
        if isinstance(flags, list):
            unit["review_flags"] = flags

    coverage = _refresh_coverage(translation)
    write_json(translation_path, translation)

    cache = TranslationCache(job_dir)
    cache.put(
        str(entry["cache_key"]),
        [accepted[unit_id] for unit_id in sorted(accepted)],
        metadata={
            "batch_id": batch_id,
            "applied_at": utc_now(),
            "model": model,
        },
    )

    entry["status"] = "applied"
    entry["applied_at"] = utc_now()
    entry["retries"] = int(retries)
    plan["translation_sha256"] = sha256_file(translation_path)
    write_json(job_dir / PLAN_FILE_NAME, plan)

    from run_metrics import record_run_metric

    record_run_metric(
        job_dir,
        stage="translation-batch",
        status="complete",
        elapsed_seconds=elapsed_seconds,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        metadata={
            "batch_id": batch_id,
            "unit_count": entry["unit_count"],
            "source_chars": entry["source_chars"],
            "retries": int(retries),
            "cache_key": entry["cache_key"],
        },
    )

    pending = [
        item
        for item in plan["batches"]
        if item.get("status") != "applied"
    ]
    return {
        "batch_id": batch_id,
        "applied_units": len(accepted),
        "coverage": coverage,
        "pending_batches": [item["batch_id"] for item in pending],
        "translation_sha256": plan["translation_sha256"],
    }


def apply_cached_batches(job_dir: Path) -> list[str]:
    """把缓存中已有结果的待处理批次直接写回，不重新翻译。"""

    job_dir = Path(job_dir).resolve()
    plan = load_plan(job_dir)
    cache = TranslationCache(job_dir)
    restored: list[str] = []
    for entry in plan.get("batches", []):
        if entry.get("status") == "applied":
            continue
        cached = cache.get(str(entry.get("cache_key") or ""))
        if not cached:
            continue
        apply_translation_batch(
            job_dir,
            str(entry["batch_id"]),
            cached,
        )
        restored.append(str(entry["batch_id"]))
    return restored


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--batch", help="批次 ID，例如 batch-0001")
    parser.add_argument(
        "--result",
        type=Path,
        help="批次结果 JSON；使用 - 表示从标准输入读取",
    )
    parser.add_argument(
        "--from-cache",
        action="store_true",
        help="把缓存中已有结果的待处理批次直接写回",
    )
    parser.add_argument("--elapsed-seconds", type=float)
    parser.add_argument("--model")
    parser.add_argument("--input-tokens", type=int)
    parser.add_argument("--output-tokens", type=int)
    parser.add_argument("--retries", type=int, default=0)
    args = parser.parse_args()
    try:
        if args.from_cache:
            restored = apply_cached_batches(args.job_dir)
            print(
                "从缓存写回批次: " + (", ".join(restored) or "无")
            )
            return 0
        if not args.batch or args.result is None:
            raise SkillError("请提供 --batch 和 --result，或使用 --from-cache")
        started = time.perf_counter()
        if str(args.result) == "-":
            payload = json.loads(sys.stdin.read())
        else:
            payload = json.loads(
                args.result.resolve().read_text(encoding="utf-8")
            )
        report = apply_translation_batch(
            args.job_dir,
            args.batch,
            payload,
            elapsed_seconds=(
                args.elapsed_seconds
                if args.elapsed_seconds is not None
                else round(time.perf_counter() - started, 3)
            ),
            model=args.model,
            input_tokens=args.input_tokens,
            output_tokens=args.output_tokens,
            retries=args.retries,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (SkillError, json.JSONDecodeError) as exc:
        print(f"错误: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
