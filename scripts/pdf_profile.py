from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from _common import (
    SkillError,
    character_counts,
    import_fitz,
    infer_source_language,
    sha256_file,
    utc_now,
    write_json,
)


def _image_area_ratio(page: Any) -> float:
    page_area = max(float(page.rect.width * page.rect.height), 1.0)
    total = 0.0
    for info in page.get_image_info(xrefs=True):
        bbox = info.get("bbox")
        if not bbox:
            continue
        x0, y0, x1, y1 = map(float, bbox)
        total += max(0.0, x1 - x0) * max(0.0, y1 - y0)
    return round(min(total / page_area, 1.0), 4)


def _page_profile(page: Any, index: int) -> dict[str, Any]:
    text_dict = page.get_text("dict")
    blocks = [block for block in text_dict["blocks"] if block.get("type") == 0]
    spans = [
        span
        for block in blocks
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        if span.get("text", "").strip()
    ]
    text = "\n".join(span["text"] for span in spans)
    counts = character_counts(text)
    image_ratio = _image_area_ratio(page)
    drawings = len(page.get_drawings())
    images = len(page.get_images(full=True))
    page_width = float(page.rect.width)

    left_chars = 0
    right_chars = 0
    wide_chars = 0
    for block in blocks:
        block_text = "".join(
            span.get("text", "")
            for line in block.get("lines", [])
            for span in line.get("spans", [])
        ).strip()
        if not block_text:
            continue
        x0, _, x1, _ = map(float, block["bbox"])
        width = x1 - x0
        chars = len(block_text)
        if width >= page_width * 0.62:
            wide_chars += chars
        elif (x0 + x1) / 2 < page_width / 2:
            left_chars += chars
        else:
            right_chars += chars
    total_column_chars = left_chars + right_chars + wide_chars
    double_column = (
        total_column_chars >= 200
        and left_chars >= total_column_chars * 0.2
        and right_chars >= total_column_chars * 0.2
        and wide_chars <= total_column_chars * 0.45
    )

    text_chars = len(text.strip())
    scan_risk = text_chars < 80 and image_ratio >= 0.35
    form_table_risk = drawings >= 60 and len(blocks) >= 15
    complex_page = (
        scan_risk
        or form_table_risk
        or images >= 4
        or drawings >= 120
        or len(blocks) >= 45
    )

    return {
        "page": index,
        "width": round(float(page.rect.width), 3),
        "height": round(float(page.rect.height), 3),
        "rotation": int(page.rotation),
        "text_chars": text_chars,
        "character_counts": counts,
        "text_blocks": len(blocks),
        "spans": len(spans),
        "images": images,
        "image_area_ratio": image_ratio,
        "drawings": drawings,
        "double_column_signal": double_column,
        "scan_risk": scan_risk,
        "form_table_risk": form_table_risk,
        "complex_page": complex_page,
    }


def profile_pdf(source: Path) -> dict[str, Any]:
    fitz = import_fitz()
    if not source.is_file():
        raise SkillError(f"PDF 不存在: {source}")
    try:
        document = fitz.open(source)
    except Exception as exc:
        raise SkillError(f"无法打开 PDF: {source}: {exc}") from exc
    if document.page_count < 1:
        raise SkillError(f"PDF 没有页面: {source}")

    pages = [_page_profile(page, index) for index, page in enumerate(document, 1)]
    all_text = "\n".join(page.get_text("text") for page in document)
    scan_pages = [page["page"] for page in pages if page["scan_risk"]]
    complex_pages = [page["page"] for page in pages if page["complex_page"]]
    form_pages = [page["page"] for page in pages if page["form_table_risk"]]
    page_sizes = {(page["width"], page["height"]) for page in pages}

    reasons: list[str] = []
    if len(scan_pages) / len(pages) >= 0.25:
        route = "scan-custom"
        reasons.append("至少四分之一页面缺少可靠文本层并以图像为主")
    elif form_pages or len(complex_pages) / len(pages) >= 0.4:
        route = "custom-layout"
        reasons.append("固定表格/表单或复杂页占比较高")
    elif complex_pages or len(page_sizes) > 1:
        route = "hybrid-complex-pages"
        reasons.append("存在局部复杂页或多种页面尺寸")
    else:
        route = "standard-auto"
        reasons.append("文本层和页面结构整体规则")

    return {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "source": str(source.resolve()),
        "sha256": sha256_file(source),
        "page_count": document.page_count,
        "source_language_estimate": infer_source_language(all_text),
        "page_size_variants": [
            {"width": width, "height": height}
            for width, height in sorted(page_sizes)
        ],
        "double_column_pages": [
            page["page"] for page in pages if page["double_column_signal"]
        ],
        "scan_risk_pages": scan_pages,
        "complex_pages": complex_pages,
        "form_table_risk_pages": form_pages,
        "route": {
            "recommended": route,
            "basis": "heuristic",
            "reasons": reasons,
            "requires_visual_confirmation": True,
        },
        "pages": pages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成学术 PDF 的版式与文本层画像")
    parser.add_argument("source_pdf", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = profile_pdf(args.source_pdf)
        output = args.output or args.source_pdf.with_suffix(".manifest.json")
        write_json(output, report)
        print(f"画像已写入: {output}")
        print(f"建议路线: {report['route']['recommended']}")
        return 0
    except SkillError as exc:
        print(f"错误: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
