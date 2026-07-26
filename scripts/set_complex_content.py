from __future__ import annotations

import argparse
from pathlib import Path

from _common import (
    COMPLEX_CONTENT_KINDS,
    COMPLEX_CONTENT_METHODS,
    SkillError,
    load_json,
    write_json,
)


def _payload_template(method: str) -> dict[str, object]:
    if method in {"structured-table-rebuild", "semantic-grid-rebuild"}:
        return {"tables": []}
    if method == "vector-rebuild":
        return {"figures": []}
    if method in {"image-text-localization", "ocr-region-rebuild"}:
        return {"regions": []}
    if method in {"custom-page-reflow", "manual-reading-order-rebuild"}:
        return {"ordered_block_ids": []}
    return {}


def _parse_page_spec(value: str, page_count: int) -> dict[str, object]:
    parts = [part.strip() for part in value.split(",", 3)]
    if len(parts) != 4:
        raise SkillError(
            "--page 格式必须是 页码,类型,处理方式,理由"
        )
    page_text, kind, method, reason = parts
    try:
        page = int(page_text)
    except ValueError as exc:
        raise SkillError(f"复杂页页码无效: {page_text!r}") from exc
    if not 1 <= page <= page_count:
        raise SkillError(f"复杂页页码超出范围: {page}")
    if kind not in COMPLEX_CONTENT_KINDS:
        raise SkillError(f"不支持的复杂内容类型: {kind}")
    if method not in COMPLEX_CONTENT_METHODS:
        raise SkillError(f"不支持的复杂页处理方式: {method}")
    if not reason:
        raise SkillError(f"第 {page} 页必须写明采用专用处理的理由")
    return {
        "page": page,
        "kind": kind,
        "method": method,
        "reason": reason,
    }


def set_complex_content(
    job_dir: Path,
    page_specs: list[str],
    *,
    confirmed_none: bool = False,
    notes: str = "",
) -> list[dict[str, object]]:
    if confirmed_none and page_specs:
        raise SkillError("--none 不能与 --page 同时使用")
    if not confirmed_none and not page_specs:
        raise SkillError("必须使用 --none，或至少提供一个 --page")

    job_path = job_dir.resolve() / "job.json"
    job = load_json(job_path)
    page_count = int(job.get("source", {}).get("page_count") or 0)
    if page_count < 1:
        raise SkillError("job.json 缺少有效原文页数")

    confirmed_pages = [
        _parse_page_spec(spec, page_count) for spec in page_specs
    ]
    page_numbers = [int(item["page"]) for item in confirmed_pages]
    if len(page_numbers) != len(set(page_numbers)):
        raise SkillError("同一复杂页只能登记一次；混合内容使用 mixed-complex")

    route = job.setdefault("route", {})
    previous = route.get("complex_content", {})
    heuristic_pages = previous.get("heuristic_candidate_pages", [])
    if confirmed_none and heuristic_pages and not notes.strip():
        raise SkillError(
            "画像已提示候选复杂页；确认无复杂页时必须用 --notes 写明目视依据"
        )

    route["complex_content"] = {
        "classification_confirmed": True,
        "review_scope": "all-source-pages",
        "heuristic_candidate_pages": heuristic_pages,
        "confirmed_pages": sorted(
            confirmed_pages, key=lambda item: int(item["page"])
        ),
        "notes": notes.strip(),
    }
    write_json(job_path, job)
    payload_path = (
        job_dir.resolve()
        / job.get("files", {}).get(
            "complex_content_payload",
            "complex_content.json",
        )
    )
    existing_payload = (
        load_json(payload_path)
        if payload_path.is_file()
        else {"schema_version": "1.0", "items": []}
    )
    existing_by_page = {
        int(item["page"]): item
        for item in existing_payload.get("items", [])
        if isinstance(item, dict) and isinstance(item.get("page"), int)
    }
    payload_items = []
    for item in route["complex_content"]["confirmed_pages"]:
        page_number = int(item["page"])
        previous_item = existing_by_page.get(page_number, {})
        same_route = (
            previous_item.get("kind") == item["kind"]
            and previous_item.get("method") == item["method"]
        )
        payload_items.append(
            {
                "id": f"p{page_number:04d}-complex",
                "page": page_number,
                "kind": item["kind"],
                "method": item["method"],
                "status": (
                    previous_item.get("status", "pending")
                    if same_route
                    else "pending"
                ),
                "source_evidence": (
                    previous_item.get("source_evidence", [])
                    if same_route
                    else []
                ),
                "payload": (
                    previous_item.get(
                        "payload",
                        _payload_template(str(item["method"])),
                    )
                    if same_route
                    else _payload_template(str(item["method"]))
                ),
                "notes": (
                    previous_item.get("notes", "")
                    if same_route
                    else ""
                ),
            }
        )
    write_json(
        payload_path,
        {
            "schema_version": "1.0",
            "classification_complete": True,
            "items": payload_items,
        },
    )
    return route["complex_content"]["confirmed_pages"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="在翻译前登记复杂内容页及其第一次生成的专用处理方式"
    )
    parser.add_argument("job_dir", type=Path)
    parser.add_argument(
        "--page",
        action="append",
        default=[],
        metavar="页码,类型,处理方式,理由",
        help="可重复使用；同页多种复杂内容请登记为 mixed-complex",
    )
    parser.add_argument(
        "--none",
        action="store_true",
        help="目视检查全部原文页后，确认没有需要专用处理的复杂页",
    )
    parser.add_argument("--notes", default="")
    args = parser.parse_args()
    try:
        pages = set_complex_content(
            args.job_dir,
            args.page,
            confirmed_none=args.none,
            notes=args.notes,
        )
        if pages:
            print(
                "复杂页处理计划已记录: "
                + ", ".join(str(item["page"]) for item in pages)
            )
        else:
            print("复杂页处理计划已记录: 全文无复杂页")
        return 0
    except SkillError as exc:
        print(f"错误: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
