"""把翻译单元绑定到原文元素，并检查有没有孤立译文。"""

from __future__ import annotations

import argparse
from pathlib import Path

from academic_pdf_translation.analysis.element_overrides import (
    apply_all,
    load_overrides,
)
from academic_pdf_translation.analysis.source_elements import (
    analyze_job_elements,
)
from academic_pdf_translation.analysis.unit_binding import (
    bind_units,
    validate_payload_sources,
)

from _common import SkillError, import_fitz, load_json, write_json

BINDING_FILE_NAME = "unit_bindings.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path)
    args = parser.parse_args()
    job_dir = args.job_dir.resolve()
    try:
        job = load_json(job_dir / "job.json")
        source_units = load_json(
            job_dir
            / job.get("files", {}).get("source_units", "source_units.json")
        )
        fitz = import_fitz()
        inventory = analyze_job_elements(
            job_dir,
            pymupdf_version=getattr(fitz, "VersionBind", "0"),
        )
        apply_all(inventory, load_overrides(job_dir))
        report = bind_units(source_units.get("units", []), inventory)

        complex_path = job_dir / job.get("files", {}).get(
            "complex_content_payload", "complex_content.json"
        )
        if complex_path.is_file():
            payload_texts = []
            for item in load_json(complex_path).get("items", []):
                payload = item.get("payload")
                if isinstance(payload, dict):
                    payload_texts.extend(
                        entry
                        for entry in payload.get("regions", [])
                        if isinstance(entry, dict)
                    )
            report.problems.extend(
                validate_payload_sources(
                    payload_texts,
                    {binding.unit_id for binding in report.bindings},
                )
            )
        write_json(job_dir / BINDING_FILE_NAME, report.as_dict())
    except (SkillError, OSError, ValueError) as exc:
        print(f"错误: {exc}")
        return 1

    print(f"绑定结果已写入: {job_dir / BINDING_FILE_NAME}")
    print(f"翻译单元: {len(source_units.get('units', []))}")
    print(f"已绑定: {len(report.bindings)}")
    print(f"孤立译文: {len(report.orphan_units)}")
    print(f"没有单元的元素: {len(report.elements_without_units)}")
    counts: dict[str, int] = {}
    for binding in report.bindings:
        counts[binding.element_role] = counts.get(binding.element_role, 0) + 1
    print("角色分布:")
    for name, count in sorted(counts.items()):
        print(f"  {name:<24}{count:>4}")
    if report.problems:
        print("问题:")
        for problem in report.problems:
            print(f"  - {problem}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
