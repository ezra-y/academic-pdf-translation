from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from reportlab.pdfbase.ttfonts import TTFont

from _common import (
    SkillError,
    internal_job_path,
    load_json,
    resolve_language_profile,
    sha256_file,
    utc_now,
    write_json,
)
from audit_translation_completeness import build_completeness_audit
from validate_job import validate_job
from retained_source import retained_region_ids
from renderer_identity import renderer_build_id


VISIBLE_MARKUP_RE = re.compile(
    r"<\s*/?\s*(?:br|p|div|span|font|table|tr|td|th)\b",
    re.IGNORECASE,
)
PLACEHOLDER_RE = re.compile(
    r"(?:\bTODO\b|\bTBD\b|\bFIXME\b|\bPLACEHOLDER\b|待翻译|未翻译|翻译中)",
    re.IGNORECASE,
)
def _ids_sha256(ids: list[str]) -> str:
    payload = "\n".join(ids).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source_units_hash(
    source_units_path: Path,
    translation_path: Path,
) -> str:
    if source_units_path.is_file():
        return sha256_file(source_units_path)
    payload = (
        "legacy-manual\n" + sha256_file(translation_path)
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _walk_strings(value: Any, path: str = "$") -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    if isinstance(value, str):
        hits.append((path, value))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            hits.extend(_walk_strings(item, f"{path}[{index}]"))
    elif isinstance(value, dict):
        for key, item in value.items():
            hits.extend(_walk_strings(item, f"{path}.{key}"))
    return hits


def _text_input_issues(
    translation: dict[str, Any],
    complex_content: dict[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    samples = []
    target_text = {
        "units": [
            {
                "id": unit.get("id"),
                "translation": unit.get("translation"),
                "keep_source_reason": unit.get("keep_source_reason"),
            }
            for unit in translation.get("units", [])
            if isinstance(unit, dict)
        ],
        "terminology": translation.get("terminology", []),
    }
    for path, text in _walk_strings(
        {
            "translation": target_text,
            "complex_content": complex_content,
        }
    ):
        codes = []
        if "\x00" in text:
            codes.append("NULL_CHARACTER")
        if "\ufffd" in text:
            codes.append("REPLACEMENT_CHARACTER")
        if VISIBLE_MARKUP_RE.search(text):
            codes.append("VISIBLE_MARKUP")
        if PLACEHOLDER_RE.search(text):
            codes.append("PLACEHOLDER")
        if codes:
            samples.append(
                {
                    "path": path,
                    "codes": codes,
                    "sample": text[:180],
                }
            )
    if samples:
        issues.append(
            {
                "code": "TEXT_INPUT_NOT_CLEAN",
                "message": "译文或复杂页数据仍含空字符、替换字符、可见标记或占位文字。",
                "samples": samples[:40],
            }
        )
    return issues


def _font_issues(
    selected_fonts: Any,
    contract: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    issues: list[dict[str, Any]] = []
    evidence: list[dict[str, str]] = []
    if not isinstance(selected_fonts, list) or not selected_fonts:
        return (
            [
                {
                    "code": "SELECTED_FONTS_MISSING",
                    "message": "导出前必须冻结实际使用的字体文件。",
                }
            ],
            evidence,
        )

    normalized: list[str] = []
    for index, value in enumerate(selected_fonts):
        if not isinstance(value, str) or not value.strip():
            issues.append(
                {
                    "code": "FONT_PATH_INVALID",
                    "message": f"selected_fonts[{index}] 不是有效路径。",
                }
            )
            continue
        path = Path(value).expanduser().resolve()
        normalized.append(str(path))
        if not path.is_file():
            issues.append(
                {
                    "code": "FONT_FILE_MISSING",
                    "path": str(path),
                }
            )
            continue
        try:
            TTFont(f"PreRenderAuditFont{index}", str(path))
        except Exception as exc:
            issues.append(
                {
                    "code": "FONT_FILE_UNREADABLE",
                    "path": str(path),
                    "message": str(exc),
                }
            )
            continue
        evidence.append(
            {
                "path": str(path),
                "sha256": sha256_file(path),
            }
        )

    contract_fonts = contract.get("font_paths")
    if not isinstance(contract_fonts, list) or sorted(
        str(Path(path).expanduser().resolve())
        for path in contract_fonts
        if isinstance(path, str)
    ) != sorted(normalized):
        issues.append(
            {
                "code": "RENDERER_FONT_CONTRACT_MISMATCH",
                "message": "排版器声明的字体与 job.json 冻结字体不一致。",
            }
        )
    return issues, evidence


def _layout_contract_issues(
    layout_log: dict[str, Any],
    translation: dict[str, Any],
    complex_content: dict[str, Any],
    retained: dict[str, Any],
    target_language: str,
    selected_fonts: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if layout_log.get("renderer") == "academic-pdf-unified-flow":
        recorded_build = str(
            layout_log.get("renderer_build_id") or ""
        )
        if recorded_build != renderer_build_id():
            issues.append(
                {
                    "code": "RENDERER_BUILD_ID_MISMATCH",
                    "message": "排版日志与当前统一生成器代码不一致。",
                }
            )
    contract = layout_log.get("render_contract")
    if not isinstance(contract, dict):
        return (
            [
                {
                    "code": "RENDER_CONTRACT_MISSING",
                    "message": (
                        "排版器没有提交导出前合同，无法证明全部翻译单元和文字区域"
                        "已经参与试排。"
                    ),
                }
            ],
            {},
        )

    units = [
        unit
        for unit in translation.get("units", [])
        if isinstance(unit, dict) and isinstance(unit.get("id"), str)
    ]
    unit_ids = [str(unit["id"]) for unit in units]
    expected_ids_hash = _ids_sha256(unit_ids)
    expected_count = len(unit_ids)
    if (
        contract.get("all_units_consumed") is not True
        or contract.get("unit_count") != expected_count
        or contract.get("unit_ids_sha256") != expected_ids_hash
    ):
        issues.append(
            {
                "code": "RENDER_UNIT_COVERAGE_INCOMPLETE",
                "message": "排版器没有证明所有冻结译文单元都已进入版式。",
                "expected_unit_count": expected_count,
                "actual_unit_count": contract.get("unit_count"),
            }
        )
    retained_ids = retained_region_ids(retained)
    expected_retained_hash = _ids_sha256(retained_ids)
    if (
        contract.get("all_retained_regions_consumed") is not True
        or contract.get("retained_region_count") != len(retained_ids)
        or contract.get("retained_region_ids_sha256") != expected_retained_hash
    ):
        issues.append(
            {
                "code": "RENDER_RETAINED_SOURCE_NOT_CONSUMED",
                "message": (
                    "排版器没有证明全部保留原文区域已真正进入候选版式。"
                ),
                "expected_retained_region_count": len(retained_ids),
                "actual_retained_region_count": contract.get(
                    "retained_region_count"
                ),
            }
        )
    if contract.get("all_text_regions_measured") is not True:
        issues.append(
            {
                "code": "UNMEASURED_TEXT_REGIONS",
                "regions": contract.get("unmeasured_text_regions") or [],
                "message": "仍有标题、脚注、图注、声明或其他文字区域未做导出前试排。",
            }
        )
    complex_ids = [
        str(item.get("id"))
        for item in complex_content.get("items", [])
        if isinstance(item, dict) and str(item.get("id") or "")
    ]
    expected_complex_hash = _ids_sha256(complex_ids)
    if (
        contract.get("all_complex_items_consumed") is not True
        or contract.get("complex_item_count") != len(complex_ids)
        or contract.get("complex_item_ids_sha256") != expected_complex_hash
    ):
        issues.append(
            {
                "code": "RENDER_COMPLEX_CONTENT_NOT_CONSUMED",
                "message": (
                    "排版器没有证明全部复杂页载荷已真正进入候选版式。"
                ),
                "expected_complex_item_count": len(complex_ids),
                "actual_complex_item_count": contract.get(
                    "complex_item_count"
                ),
            }
        )
    if contract.get("heading_checks_performed") is not True:
        issues.append(
            {
                "code": "HEADING_PLACEMENT_NOT_CHECKED",
                "message": "排版器没有提交标题与首段同页检查结果。",
            }
        )
    for field, code in (
        ("overflow_regions", "PRE_RENDER_OVERFLOW"),
        ("orphan_regions", "PRE_RENDER_ORPHAN_LINE"),
    ):
        values = contract.get(field)
        if not isinstance(values, list):
            issues.append(
                {
                    "code": "RENDER_CONTRACT_FIELD_MISSING",
                    "field": field,
                }
            )
        elif values:
            issues.append({"code": code, "regions": values})

    _, profile = resolve_language_profile(target_language)
    if (
        profile.get("writing_system") in {"han", "japanese"}
        and contract.get("cjk_kinsoku_enabled") is not True
    ):
        issues.append(
            {
                "code": "CJK_KINSOKU_NOT_ENABLED",
                "message": "目标语言排版器尚未启用 CJK 行首行末禁则。",
            }
        )
    font_issues, font_evidence = _font_issues(selected_fonts, contract)
    issues.extend(font_issues)
    return issues, {
        "contract": contract,
        "font_evidence": font_evidence,
        "expected_unit_count": expected_count,
        "expected_unit_ids_sha256": expected_ids_hash,
        "expected_complex_item_count": len(complex_ids),
        "expected_complex_item_ids_sha256": expected_complex_hash,
        "expected_retained_region_count": len(retained_ids),
        "expected_retained_region_ids_sha256": expected_retained_hash,
    }


def _translated_validation(job_dir: Path) -> dict[str, Any]:
    return validate_job(
        job_dir,
        "translated",
        status_override="translated",
    )


def _content_audit_without_candidate(job_dir: Path) -> dict[str, Any]:
    return build_completeness_audit(
        job_dir,
        include_candidate=False,
    )


def build_pre_render_audit(job_dir: Path) -> dict[str, Any]:
    job_dir = job_dir.resolve()
    job = load_json(job_dir / "job.json")
    files = job.get("files", {})
    source_path = internal_job_path(job_dir, job["source"]["job_path"])
    source_units_path = internal_job_path(
        job_dir,
        files.get("source_units", "source_units.json"),
    )
    translation_path = internal_job_path(job_dir, files["translation"])
    complex_path = internal_job_path(
        job_dir,
        files.get("complex_content_payload", "complex_content.json"),
    )
    layout_path = internal_job_path(job_dir, files["layout_overrides"])
    retained_path = internal_job_path(job_dir, files["retained_source"])
    layout_log_path = job_dir / "generator-layout-log.json"

    translation = load_json(translation_path)
    complex_content = load_json(complex_path)
    retained = load_json(retained_path)
    layout_log = load_json(layout_log_path)
    validation = _translated_validation(job_dir)
    completeness = _content_audit_without_candidate(job_dir)

    issues: list[dict[str, Any]] = [
        {
            "code": "JOB_DATA_INVALID",
            "message": error,
        }
        for error in validation.get("errors", [])
    ]
    issues.extend(_text_input_issues(translation, complex_content))
    contract_issues, contract_evidence = _layout_contract_issues(
        layout_log,
        translation,
        complex_content,
        retained,
        str(job["translation"]["target_language"]),
        job.get("quality", {}).get("selected_fonts"),
    )
    issues.extend(contract_issues)

    inventory = load_json(
        internal_job_path(job_dir, files["figure_inventory"])
    )
    unresolved = [
        item.get("id") or item.get("page")
        for item in inventory.get("items", [])
        if isinstance(item, dict)
        and str(item.get("status") or "").lower() == "unresolved"
    ]
    if (
        inventory.get("inventory_complete") is not True
        or not str(inventory.get("scope_note") or "").strip()
        or unresolved
    ):
        issues.append(
            {
                "code": "FIGURE_INVENTORY_NOT_READY",
                "unresolved": unresolved,
                "message": "图表、截图和复杂视觉内容尚未在导出前清点完。",
            }
        )
    if completeness.get("decision") == "NEEDS_REPAIR":
        issues.append(
            {
                "code": "TRANSLATION_DATA_NEEDS_REPAIR",
                "pages": completeness.get("repair_pages", []),
                "tasks": completeness.get("repair_plan", {}).get("tasks", []),
            }
        )

    report = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "status": "READY_TO_RENDER" if not issues else "BLOCKED",
        "issue_count": len(issues),
        "issues": issues,
        "warnings": [
            {
                "code": "CONTENT_REVIEW_SIGNAL",
                "pages": completeness.get("review_pages", []),
                "flag_counts": completeness.get("flag_counts", {}),
            }
        ]
        if completeness.get("review_pages")
        else [],
        "input_hashes": {
            "source_sha256": sha256_file(source_path),
            "source_units_sha256": _source_units_hash(
                source_units_path,
                translation_path,
            ),
            "translation_sha256": sha256_file(translation_path),
            "complex_content_sha256": sha256_file(complex_path),
            "retained_source_sha256": sha256_file(retained_path),
            "layout_overrides_sha256": sha256_file(layout_path),
            "generator_layout_log_sha256": sha256_file(layout_log_path),
        },
        "contract_evidence": contract_evidence,
        "validation_warnings": validation.get("warnings", []),
        "completeness_decision": completeness.get("decision"),
        "completeness_repair_pages": completeness.get("repair_pages", []),
        "completeness_review_pages": completeness.get("review_pages", []),
    }
    output_path = internal_job_path(
        job_dir,
        files.get("render_readiness", "staging/render-readiness.json"),
    )
    write_json(output_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="在生成 PDF 文件前一次性检查全部输入数据和排版器试排合同"
    )
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = build_pre_render_audit(args.job_dir)
        if args.output:
            write_json(args.output.resolve(), report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "READY_TO_RENDER" else 2
    except SkillError as exc:
        print(f"错误: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
