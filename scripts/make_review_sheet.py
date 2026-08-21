from __future__ import annotations

import argparse
import io
from pathlib import Path

import perf_trace
from _common import (
    SkillError,
    import_fitz,
    internal_job_path,
    load_json,
    sha256_file,
    utc_now,
    write_json,
)
from candidate_page_map import (
    candidate_pages_for_source,
    load_candidate_page_map,
)


def _render_page(page, dpi: int):
    try:
        from PIL import Image
    except ImportError as exc:
        raise SkillError("缺少 Pillow，无法生成对照图。") from exc
    fitz = import_fitz()
    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
    return Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")


def _risk_map(job_dir: Path) -> tuple[dict[int, list[str]], str | None]:
    path = job_dir / "reviews" / "risk-report.json"
    if not path.is_file():
        return {}, None
    report = load_json(path)
    risks: dict[int, list[str]] = {}
    for item in report.get("pages", []):
        if not isinstance(item, dict):
            continue
        page = item.get("page")
        flags = item.get("flags", [])
        if isinstance(page, int) and isinstance(flags, list) and flags:
            risks[page] = [str(flag) for flag in flags]
    return risks, sha256_file(path)


def _labeled_pair(
    source_image,
    candidate_images: list,
    page_number: int,
    candidate_page_numbers: list[int],
    risk_flags: list[str],
):
    from PIL import Image, ImageDraw

    label_height = 42
    gap = 18
    candidate_label_height = 28
    candidate_width = max(
        (image.width for image in candidate_images),
        default=source_image.width,
    )
    candidate_height = sum(
        image.height + candidate_label_height
        for image in candidate_images
    ) + max(0, len(candidate_images) - 1) * 10
    width = source_image.width + candidate_width + gap
    height = max(source_image.height, candidate_height) + label_height
    canvas = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(canvas)
    draw.text((10, 8), f"PAGE {page_number} | SOURCE", fill="black")
    draw.text(
        (source_image.width + gap + 10, 8),
        "TRANSLATION | "
        + ",".join(map(str, candidate_page_numbers)),
        fill="black",
    )
    if risk_flags:
        risk_text = "CHECK: " + ", ".join(risk_flags)
        draw.text((10, 24), risk_text[:180], fill=(180, 0, 0))
    canvas.paste(source_image, (0, label_height))
    candidate_y = label_height
    for candidate_page, candidate_image in zip(
        candidate_page_numbers,
        candidate_images,
    ):
        draw.text(
            (source_image.width + gap + 10, candidate_y + 6),
            f"CANDIDATE PAGE {candidate_page}",
            fill=(70, 78, 84),
        )
        candidate_y += candidate_label_height
        canvas.paste(
            candidate_image,
            (source_image.width + gap, candidate_y),
        )
        candidate_y += candidate_image.height + 10
    return canvas


def _stack_pairs(pairs: list, gap: int = 20):
    from PIL import Image

    width = max(pair.width for pair in pairs)
    height = sum(pair.height for pair in pairs) + gap * (len(pairs) - 1)
    sheet = Image.new("RGB", (width, height), "white")
    y = 0
    for pair in pairs:
        x = (width - pair.width) // 2
        sheet.paste(pair, (x, y))
        y += pair.height + gap
    return sheet


def _parse_page_spec(value: str | None, page_count: int) -> list[int]:
    if not value:
        return []
    pages: set[int] = set()
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            try:
                start = int(start_text)
                end = int(end_text)
            except ValueError as exc:
                raise SkillError(f"高清页码范围无效: {token!r}") from exc
            if start > end:
                raise SkillError(f"高清页码范围起点大于终点: {token!r}")
            pages.update(range(start, end + 1))
        else:
            try:
                pages.add(int(token))
            except ValueError as exc:
                raise SkillError(f"高清页码无效: {token!r}") from exc
    invalid = sorted(page for page in pages if not 1 <= page <= page_count)
    if invalid:
        raise SkillError(f"高清页码超出范围: {invalid}")
    return sorted(pages)


def _cache_matches(
    manifest: dict,
    *,
    source_hash: str,
    candidate_hash: str,
    risk_report_hash: str | None,
    dpi: int,
    pages_per_sheet: int,
    comparison_dir: Path,
    candidate_page_map_hash: str | None,
) -> bool:
    expected = {
        "source_sha256": source_hash,
        "candidate_sha256": candidate_hash,
        "risk_report_sha256": risk_report_hash,
        "dpi": dpi,
        "pages_per_sheet": pages_per_sheet,
        "candidate_page_map_sha256": candidate_page_map_hash,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        return False
    sheet_files = manifest.get("sheet_files")
    sheet_hashes = manifest.get("sheet_sha256")
    if not isinstance(sheet_files, list) or not sheet_files:
        return False
    if not isinstance(sheet_hashes, dict):
        return False
    for value in sheet_files:
        path = comparison_dir / value
        if (
            not path.is_file()
            or sheet_hashes.get(value) != sha256_file(path)
        ):
            return False
    review_pdf = manifest.get("review_pdf")
    review_pdf_path = (
        comparison_dir / review_pdf
        if isinstance(review_pdf, str)
        else comparison_dir / "__missing__"
    )
    return (
        review_pdf_path.is_file()
        and manifest.get("review_pdf_sha256") == sha256_file(review_pdf_path)
    )


def _timed_make_review_sheet(
    job_dir: Path,
    dpi: int = 110,
    pages_per_sheet: int = 2,
    detail_page_spec: str | None = None,
    detail_dpi: int = 180,
    force: bool = False,
) -> dict:
    if not 72 <= dpi <= 220:
        raise SkillError("审查图 DPI 必须在 72 到 220 之间")
    if not 1 <= pages_per_sheet <= 4:
        raise SkillError("每张审查图只能放 1 到 4 组源译页")
    if not 120 <= detail_dpi <= 300:
        raise SkillError("高清页 DPI 必须在 120 到 300 之间")

    job_dir = job_dir.resolve()
    job = load_json(job_dir / "job.json")
    files = job["files"]
    source_path = internal_job_path(job_dir, job["source"]["job_path"])
    candidate_path = internal_job_path(job_dir, files["candidate"])
    if not source_path.is_file() or not candidate_path.is_file():
        raise SkillError("生成对照图前必须同时存在 source.pdf 和 candidate.pdf")

    fitz = import_fitz()
    source = fitz.open(source_path)
    candidate = fitz.open(candidate_path)
    translation = load_json(
        internal_job_path(job_dir, files["translation"])
    )
    candidate_mapping = (
        load_candidate_page_map(
            job_dir,
            job,
            required=("candidate_page_map" in files),
            candidate_path=candidate_path,
            translation=translation,
        )
        if (
            "candidate_page_map" in files
            or (job_dir / "candidate-page-map.json").is_file()
        )
        else None
    )
    if candidate_mapping is None and source.page_count != candidate.page_count:
        source.close()
        candidate.close()
        raise SkillError(
            f"页数不一致，无法生成逐页对照: {source.page_count} vs "
            f"{candidate.page_count}"
        )

    detail_pages = _parse_page_spec(detail_page_spec, source.page_count)
    source_hash = sha256_file(source_path)
    candidate_hash = sha256_file(candidate_path)
    candidate_page_map_path = (
        internal_job_path(job_dir, files["candidate_page_map"])
        if "candidate_page_map" in files
        else job_dir / "candidate-page-map.json"
    )
    candidate_page_map_hash = (
        sha256_file(candidate_page_map_path)
        if candidate_mapping is not None
        and candidate_page_map_path.is_file()
        else None
    )
    risks, risk_report_hash = _risk_map(job_dir)
    comparison_dir = job_dir / "comparisons"
    sheet_dir = comparison_dir / "sheets"
    detail_dir = comparison_dir / "details"
    for directory in (comparison_dir, sheet_dir, detail_dir):
        directory.mkdir(parents=True, exist_ok=True)

    manifest_path = comparison_dir / "manifest.json"
    review_pdf_path = comparison_dir / "source-vs-candidate.pdf"
    cache_hit = False
    manifest: dict = {}
    if manifest_path.is_file() and not force:
        manifest = load_json(manifest_path)
        cache_hit = _cache_matches(
            manifest,
            source_hash=source_hash,
            candidate_hash=candidate_hash,
            risk_report_hash=risk_report_hash,
            dpi=dpi,
            pages_per_sheet=pages_per_sheet,
            comparison_dir=comparison_dir,
            candidate_page_map_hash=candidate_page_map_hash,
        )

    if not cache_hit:
        for old_path in sheet_dir.iterdir():
            if old_path.is_file() or old_path.is_symlink():
                old_path.unlink()

        sheet_paths: list[Path] = []
        sheet_index: list[dict] = []
        pending_pairs: list = []
        pending_pages: list[int] = []
        for index in range(source.page_count):
            page_number = index + 1
            source_image = _render_page(source[index], dpi)
            candidate_page_numbers = candidate_pages_for_source(
                candidate_mapping,
                page_number,
            )
            candidate_images = [
                _render_page(candidate[candidate_page - 1], dpi)
                for candidate_page in candidate_page_numbers
                if 1 <= candidate_page <= candidate.page_count
            ]
            if not candidate_images:
                source_image.close()
                raise SkillError(f"源页 {page_number} 没有可渲染的候选页")
            pair = _labeled_pair(
                source_image,
                candidate_images,
                page_number,
                candidate_page_numbers,
                risks.get(page_number, []),
            )
            source_image.close()
            for candidate_image in candidate_images:
                candidate_image.close()
            pending_pairs.append(pair)
            pending_pages.append(page_number)
            if (
                len(pending_pairs) == pages_per_sheet
                or page_number == source.page_count
            ):
                sheet_number = len(sheet_paths) + 1
                sheet_path = sheet_dir / f"sheet-{sheet_number:04d}.png"
                sheet = _stack_pairs(pending_pairs)
                sheet.save(sheet_path, "PNG", compress_level=3)
                sheet.close()
                for pending_pair in pending_pairs:
                    pending_pair.close()
                sheet_paths.append(sheet_path)
                sheet_index.append(
                    {
                        "sheet": sheet_number,
                        "file": str(sheet_path.relative_to(comparison_dir)),
                        "pages": pending_pages,
                        "risk_pages": [
                            page for page in pending_pages if page in risks
                        ],
                    }
                )
                pending_pairs = []
                pending_pages = []

        review_pdf = fitz.open()
        for sheet_path in sheet_paths:
            image = fitz.Pixmap(str(sheet_path))
            page_width = 842.0
            page_height = page_width * image.height / image.width
            page = review_pdf.new_page(width=page_width, height=page_height)
            page.insert_image(page.rect, filename=str(sheet_path))
        review_pdf.save(review_pdf_path, deflate=True)
        review_pdf.close()

        manifest = {
            "schema_version": "2.0",
            "generated_at": utc_now(),
            "source_sha256": source_hash,
            "candidate_sha256": candidate_hash,
            "candidate_page_map_sha256": candidate_page_map_hash,
            "risk_report_sha256": risk_report_hash,
            "page_count": source.page_count,
            "candidate_page_count": candidate.page_count,
            "dpi": dpi,
            "pages_per_sheet": pages_per_sheet,
            "sheet_count": len(sheet_paths),
            "sheet_files": [
                str(path.relative_to(comparison_dir)) for path in sheet_paths
            ],
            "sheet_sha256": {
                str(path.relative_to(comparison_dir)): sha256_file(path)
                for path in sheet_paths
            },
            "sheet_index": sheet_index,
            "review_pdf": review_pdf_path.name,
            "review_pdf_sha256": sha256_file(review_pdf_path),
        }
        write_json(manifest_path, manifest)

    for old_path in detail_dir.iterdir():
        if old_path.is_file() or old_path.is_symlink():
            old_path.unlink()
    detail_paths: list[str] = []
    for page_number in detail_pages:
        source_image = _render_page(source[page_number - 1], detail_dpi)
        candidate_page_numbers = candidate_pages_for_source(
            candidate_mapping,
            page_number,
        )
        candidate_images = [
            _render_page(candidate[candidate_page - 1], detail_dpi)
            for candidate_page in candidate_page_numbers
            if 1 <= candidate_page <= candidate.page_count
        ]
        if not candidate_images:
            source_image.close()
            raise SkillError(f"源页 {page_number} 没有可渲染的候选页")
        pair = _labeled_pair(
            source_image,
            candidate_images,
            page_number,
            candidate_page_numbers,
            risks.get(page_number, []),
        )
        source_image.close()
        for candidate_image in candidate_images:
            candidate_image.close()
        detail_path = detail_dir / f"page-{page_number:04d}.png"
        pair.save(detail_path, "PNG", compress_level=3)
        pair.close()
        detail_paths.append(str(detail_path))

    source.close()
    candidate.close()
    return {
        "pages": int(manifest["page_count"]),
        "page_count": int(manifest["page_count"]),
        "dpi": int(manifest["dpi"]),
        "pages_per_sheet": int(manifest["pages_per_sheet"]),
        "sheet_count": int(manifest["sheet_count"]),
        "sheets": [
            str(comparison_dir / value) for value in manifest["sheet_files"]
        ],
        "review_pdf": str(review_pdf_path),
        "manifest": str(manifest_path),
        "detail_pairs": detail_paths,
        "cache_hit": cache_hit,
    }



def make_review_sheet(*args, **kwargs):
    """计时包装：阶段耗时进入性能基线，行为与实现完全一致。"""

    with perf_trace.stage("review_sheet"):
        return _timed_make_review_sheet(*args, **kwargs)

def main() -> int:
    parser = argparse.ArgumentParser(
        description="一次生成供复审智能体读取的原文与译文对照图包"
    )
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--dpi", type=int, default=110)
    parser.add_argument("--pages-per-sheet", type=int, default=2)
    parser.add_argument(
        "--detail-pages",
        help="只为疑点页额外生成高清对照，例如 3,7-9",
    )
    parser.add_argument("--detail-dpi", type=int, default=180)
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使原文和候选哈希未变化，也重新生成审查图包",
    )
    args = parser.parse_args()
    try:
        report = make_review_sheet(
            args.job_dir,
            dpi=args.dpi,
            pages_per_sheet=args.pages_per_sheet,
            detail_page_spec=args.detail_pages,
            detail_dpi=args.detail_dpi,
            force=args.force,
        )
        state = "复用缓存" if report["cache_hit"] else "新生成"
        print(
            f"{state} {report['sheet_count']} 张审查图，"
            f"覆盖 {report['pages']} 页"
        )
        print(f"审查 PDF: {report['review_pdf']}")
        if report["detail_pairs"]:
            print(f"高清疑点页: {len(report['detail_pairs'])} 张")
        return 0
    except SkillError as exc:
        print(f"错误: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
