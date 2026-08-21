from __future__ import annotations

import argparse
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import perf_trace
from _common import (
    SkillError,
    center_in_bbox,
    character_counts,
    import_fitz,
    internal_job_path,
    load_json,
    resolve_language_profile,
    sha256_file,
    target_character_count,
    utc_now,
    write_json,
)
from candidate_page_map import (
    candidate_pages_for_unit,
    candidate_pages_for_source,
    load_candidate_page_map,
    source_pages_for_candidate,
)
from retained_source import extract_retained_regions


PLACEHOLDER_PATTERN = re.compile(
    r"\{\{[^{}]+\}\}|\{v\s*\d+\}|</?style\b[^>]*>|"
    r"<x\d+>|ZXQPH\d+QXZ|TODO_TRANSLATE|TRANSLATION_MISSING"
)
LATIN_PROSE_PATTERN = re.compile(
    r"\b(?:[A-Za-z][A-Za-z'-]*[ \t]+){3,}[A-Za-z][A-Za-z'-]*\b"
)
COMPATIBILITY_IDEOGRAPH_PATTERN = re.compile(r"[\uf900-\ufaff]")
HAN_CHARACTER_PATTERN = re.compile(r"[\u3400-\u9fff]")
ORPHAN_TRAILING_PUNCTUATION = "，。；：！？、）》】”’」』〉〕〗〙〛）,.;:!?]}”"
REFERENCE_KINDS = {
    "reference",
    "references",
    "bibliography",
}
SOURCE_MAPPING_LABEL_PATTERN = re.compile(
    r"^(?:"
    r"原文第\s*\d+\s*[页頁]|"
    r"source\s+page\s+\d+|page\s+source\s+\d+|"
    r"quellseite\s+\d+|página\s+original\s+\d+|"
    r"原文\s*\d+\s*ページ|원문\s*\d+\s*쪽"
    r")$",
    re.IGNORECASE,
)


def _page_selector_matches(item: dict, page_number: int) -> bool:
    if item.get("page") == page_number:
        return True
    pages = item.get("pages")
    return isinstance(pages, list) and page_number in pages


def _regions_for_page(items: list[dict], page_number: int) -> list[dict]:
    return [
        item
        for item in items
        if isinstance(item, dict) and _page_selector_matches(item, page_number)
    ]


def _region_covers_page(page: Any, region: dict) -> bool:
    bbox = region.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return False
    x0, y0, x1, y1 = map(float, bbox)
    area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    for page_rect in (page.rect, page.cropbox):
        page_area = max(float(page_rect.width * page_rect.height), 1.0)
        if area / page_area >= 0.8:
            return True
    return False


def _structured_table_page(
    page_number: int,
    page_overrides: list[dict],
    non_body_regions: list[dict],
) -> bool:
    if any(
        "table" in str(region.get("category", "")).lower()
        for region in non_body_regions
    ):
        return True
    for item in page_overrides:
        if not isinstance(item, dict) or not _page_selector_matches(
            item, page_number
        ):
            continue
        layout = str(item.get("layout", "")).lower()
        if (
            item.get("preserve_column_structure") is True
            or item.get("structured_table") is True
            or "table" in layout
        ):
            return True
    return False


def _structured_complex_candidate_pages(
    complex_content: dict,
    candidate_mapping: dict[str, Any] | None,
) -> set[int]:
    if not isinstance(candidate_mapping, dict):
        return set()
    structured_ids = {
        str(item.get("id") or "")
        for item in complex_content.get("items", [])
        if isinstance(item, dict)
        and item.get("status") == "ready"
        and item.get("method")
        in {"structured-table-rebuild", "semantic-grid-rebuild"}
        and str(item.get("id") or "")
    }
    pages: set[int] = set()
    for entry in candidate_mapping.get("complex_items", []):
        if (
            not isinstance(entry, dict)
            or str(entry.get("complex_item_id") or "") not in structured_ids
        ):
            continue
        pages.update(
            int(page)
            for page in entry.get("candidate_pages", [])
            if isinstance(page, int)
        )
    return pages


def _all_complex_candidate_pages(
    complex_content: dict[str, Any],
    candidate_mapping: dict[str, Any] | None,
) -> set[int]:
    if not isinstance(candidate_mapping, dict):
        return set()
    ready_ids = {
        str(item.get("id") or "")
        for item in complex_content.get("items", [])
        if isinstance(item, dict)
        and item.get("status") == "ready"
        and str(item.get("id") or "")
    }
    pages: set[int] = set()
    for entry in candidate_mapping.get("complex_items", []):
        if (
            not isinstance(entry, dict)
            or str(entry.get("complex_item_id") or "") not in ready_ids
        ):
            continue
        pages.update(
            int(page)
            for page in entry.get("candidate_pages", [])
            if isinstance(page, int)
        )
    return pages


def _pre_complex_break_pages(
    candidate_mapping: dict[str, Any] | None,
    structured_pages: set[int],
) -> set[int]:
    if not isinstance(candidate_mapping, dict):
        return set()
    source_pages_by_candidate = {
        int(entry["candidate_page"]): {
            int(page)
            for page in entry.get("source_pages", [])
            if isinstance(page, int)
        }
        for entry in candidate_mapping.get("candidate_pages", [])
        if isinstance(entry, dict)
        and isinstance(entry.get("candidate_page"), int)
    }
    result: set[int] = set()
    for page in structured_pages:
        if page <= 1:
            continue
        previous_sources = source_pages_by_candidate.get(page - 1, set())
        current_sources = source_pages_by_candidate.get(page, set())
        if previous_sources & current_sources:
            result.add(page - 1)
            continue
        if (
            previous_sources
            and current_sources
            and max(previous_sources) + 1 == min(current_sources)
        ):
            result.add(page - 1)
    return result


def _placeholder_token(text: str) -> str:
    return re.sub(
        r"\s+",
        "",
        unicodedata.normalize("NFKC", text or ""),
    )


def _expected_literal_placeholder_tokens(
    translation: dict[str, Any],
) -> set[str]:
    units_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for unit in translation.get("units", []):
        if isinstance(unit, dict) and isinstance(unit.get("page"), int):
            units_by_page[int(unit["page"])].append(unit)
    allowed: set[str] = set()
    for units in units_by_page.values():
        source = "\n".join(str(unit.get("source") or "") for unit in units)
        if "{" not in source or "}" not in source:
            continue
        target = "\n".join(
            str(unit.get("translation") or unit.get("source") or "")
            for unit in units
        )
        allowed.update(
            _placeholder_token(hit)
            for hit in PLACEHOLDER_PATTERN.findall(target)
            if hit.startswith("{{")
        )
    return allowed


def _allowed_latin_corpus(text: str) -> str:
    variants = {
        re.sub(r"[^a-z0-9]+", "", text.casefold()),
        re.sub(
            r"[^a-z0-9]+",
            "",
            re.sub(
                r"\b[A-Z][A-Z0-9]{1,4}\b",
                "",
                text,
            ).casefold(),
        ),
    }
    return "\n".join(value for value in variants if value)


def _complex_localized_source_labels(
    item: dict[str, Any],
) -> list[str]:
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return []
    sources: list[str] = []
    for region in payload.get("regions", []):
        if not isinstance(region, dict):
            continue
        for label in region.get("localized_labels", []):
            if not isinstance(label, dict):
                continue
            value = (
                label.get("source")
                or label.get("source_text")
                or label.get("label")
                or label.get("original")
                or ""
            )
            if isinstance(value, list):
                text = " ".join(
                    str(part).strip()
                    for part in value
                    if str(part).strip()
                )
            else:
                text = str(value).strip()
            if text:
                sources.append(text)
    for component in payload.get("components", []):
        if not isinstance(component, dict):
            continue
        sources.extend(
            _complex_localized_source_labels(
                {
                    "payload": component.get("payload") or component,
                }
            )
        )
    return sources


def _mapped_entry_has_visible_retained_content(entry: dict[str, Any]) -> bool:
    return any(
        str(region_id).strip()
        for region_id in entry.get("retained_region_ids", [])
    )


def _in_any_region(span_bbox: Any, regions: list[dict]) -> bool:
    return any(
        isinstance(region.get("bbox"), list)
        and len(region["bbox"]) == 4
        and center_in_bbox(span_bbox, region["bbox"])
        for region in regions
    )


def _region_union_area(regions: list[dict]) -> float:
    rectangles = []
    for region in regions:
        bbox = region.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        x0, y0, x1, y1 = map(float, bbox)
        if x1 > x0 and y1 > y0:
            rectangles.append((x0, y0, x1, y1))
    if not rectangles:
        return 0.0
    x_values = sorted({value for rect in rectangles for value in (rect[0], rect[2])})
    area = 0.0
    for left, right in zip(x_values, x_values[1:]):
        if right <= left:
            continue
        intervals = sorted(
            (y0, y1)
            for x0, y0, x1, y1 in rectangles
            if x0 < right and x1 > left
        )
        if not intervals:
            continue
        covered = 0.0
        start, end = intervals[0]
        for current_start, current_end in intervals[1:]:
            if current_start <= end:
                end = max(end, current_end)
            else:
                covered += end - start
                start, end = current_start, current_end
        covered += end - start
        area += (right - left) * covered
    return area


def _reference_area_ratio(page: Any, regions: list[dict]) -> float:
    reference_regions = [
        region
        for region in regions
        if region.get("category") in {"references", "bibliography"}
    ]
    page_area = max(float(page.rect.width * page.rect.height), 1.0)
    return min(1.0, _region_union_area(reference_regions) / page_area)


def _whole_page_reference(page: Any, regions: list[dict]) -> bool:
    return _reference_area_ratio(page, regions) >= 0.72


def _body_spans(
    page: Any,
    page_number: int,
    spans: list[dict],
    overrides: dict,
    retained_regions: list[dict],
) -> tuple[list[dict], str]:
    spans = [
        span
        for span in spans
        if not SOURCE_MAPPING_LABEL_PATTERN.fullmatch(
            str(span.get("text") or "").strip()
        )
    ]
    body_regions = _regions_for_page(
        overrides.get("body_regions", []), page_number
    )
    non_body_regions = _regions_for_page(
        overrides.get("non_body_regions", []), page_number
    )
    reference_regions = [
        region
        for region in retained_regions
        if region.get("category") in {"references", "bibliography"}
    ]
    if any(_region_covers_page(page, region) for region in non_body_regions):
        return [], "explicit-non-body"
    if body_regions:
        return [
            span for span in spans if _in_any_region(span["bbox"], body_regions)
        ], "explicit"

    top = float(page.rect.height) * 0.06
    bottom = float(page.rect.height) * 0.93
    result = []
    for span in spans:
        bbox = span["bbox"]
        if float(bbox[1]) < top or float(bbox[3]) > bottom:
            continue
        if _in_any_region(bbox, non_body_regions):
            continue
        if _in_any_region(bbox, reference_regions):
            continue
        result.append(span)
    return result, "heuristic"


def _weighted_font_mode(spans: list[dict]) -> float | None:
    weights: Counter[float] = Counter()
    for span in spans:
        text = span.get("text", "").strip()
        if not text:
            continue
        weights[round(float(span["size"]), 1)] += max(len(text), 1)
    if not weights:
        return None
    return float(weights.most_common(1)[0][0])


def _low_table_spans(
    spans: list[dict],
    table_regions: list[dict],
    minimum_font_pt: float,
) -> list[dict]:
    hits: list[dict] = []
    for span in spans:
        text = span.get("text", "").strip()
        size = float(span.get("size", 0))
        if (
            len(text) >= 2
            and size < minimum_font_pt
            and not int(span.get("flags", 0)) & 1
            and _in_any_region(span["bbox"], table_regions)
        ):
            hits.append(
                {
                    "text": text[:120],
                    "size": round(size, 3),
                    "bbox": [
                        round(float(value), 2) for value in span["bbox"]
                    ],
                }
            )
    return hits


def _leading_ratios(
    text_dict: dict,
    body_mode: float | None,
    page_number: int,
    overrides: dict,
    retained_regions: list[dict],
) -> list[float]:
    if body_mode is None:
        return []
    non_body_regions = _regions_for_page(
        overrides.get("non_body_regions", []), page_number
    )
    reference_regions = [
        region
        for region in retained_regions
        if region.get("category") in {"references", "bibliography"}
    ]
    ratios: list[float] = []
    for block in text_dict["blocks"]:
        if block.get("type") != 0:
            continue
        lines = block.get("lines", [])
        for previous, current in zip(lines, lines[1:]):
            if _in_any_region(current["bbox"], non_body_regions):
                continue
            if _in_any_region(current["bbox"], reference_regions):
                continue
            previous_spans = [
                span for span in previous.get("spans", []) if span.get("text", "").strip()
            ]
            current_spans = [
                span for span in current.get("spans", []) if span.get("text", "").strip()
            ]
            if not previous_spans or not current_spans:
                continue
            size = statistics.median(
                [float(span["size"]) for span in previous_spans + current_spans]
            )
            if abs(size - body_mode) > 1.2 or size <= 0:
                continue
            baseline_gap = float(current["bbox"][3]) - float(previous["bbox"][3])
            ratio = baseline_gap / size
            if 0.8 <= ratio <= 3.0:
                ratios.append(ratio)
    return ratios


def _column_blank_ratio(page: Any, body_spans: list[dict]) -> float:
    page_rect = page.rect
    page_width = float(page_rect.width)
    page_center = float(page_rect.x0) + page_width / 2
    center_margin = page_width * 0.06
    text_total = sum(len(span.get("text", "").strip()) for span in body_spans)
    center_crossing = [
        span
        for span in body_spans
        if (
            len(span.get("text", "").strip()) >= 12
            and float(span["bbox"][0]) <= page_center - center_margin
            and float(span["bbox"][2]) >= page_center + center_margin
        )
    ]
    center_crossing_chars = sum(
        len(span.get("text", "").strip()) for span in center_crossing
    )
    single_column = (
        len(center_crossing) >= 5
        and text_total > 0
        and center_crossing_chars / text_total >= 0.25
    )
    usable_bottom = float(page_rect.height) * 0.9
    usable_height = float(page_rect.height) * 0.84
    if single_column:
        content_bottom = max(
            (float(span["bbox"][3]) for span in body_spans),
            default=usable_bottom,
        )
        return round(
            max(0.0, usable_bottom - content_bottom) / usable_height,
            3,
        )

    ratios: list[float] = []
    for left, right in (
        (float(page_rect.x0), float(page_rect.x0 + page_rect.width / 2)),
        (float(page_rect.x0 + page_rect.width / 2), float(page_rect.x1)),
    ):
        column = [
            span
            for span in body_spans
            if left
            <= (float(span["bbox"][0]) + float(span["bbox"][2])) / 2
            < right
        ]
        if sum(len(span["text"].strip()) for span in column) < 80:
            continue
        content_bottom = max(float(span["bbox"][3]) for span in column)
        ratios.append(max(0.0, usable_bottom - content_bottom) / usable_height)
    return round(max(ratios, default=0.0), 3)


def _top_blank_ratio(page: Any, body_spans: list[dict]) -> float:
    if sum(len(span["text"].strip()) for span in body_spans) < 80:
        return 0.0
    page_height = float(page.rect.height)
    usable_top = page_height * 0.06
    usable_height = page_height * 0.84
    content_top = min(float(span["bbox"][1]) for span in body_spans)
    return round(max(0.0, content_top - usable_top) / usable_height, 3)


def _body_line_width_ratio(
    page: Any,
    text_dict: dict,
    body_spans: list[dict],
) -> tuple[float | None, int]:
    if not body_spans:
        return None, 0
    body_keys = {
        (
            tuple(round(float(value), 3) for value in span["bbox"]),
            str(span.get("text", "")),
        )
        for span in body_spans
    }
    ratios: list[float] = []
    page_width = max(float(page.rect.width), 1.0)
    for block in text_dict["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            selected = [
                span
                for span in line.get("spans", [])
                if (
                    tuple(round(float(value), 3) for value in span["bbox"]),
                    str(span.get("text", "")),
                )
                in body_keys
            ]
            text = "".join(span.get("text", "") for span in selected).strip()
            if len(text) < 12:
                continue
            x0 = min(float(span["bbox"][0]) for span in selected)
            x1 = max(float(span["bbox"][2]) for span in selected)
            ratio = max(0.0, x1 - x0) / page_width
            if ratio >= 0.08:
                ratios.append(ratio)
    if not ratios:
        return None, 0
    return round(statistics.median(ratios), 3), len(ratios)


def _orphan_single_han_lines(
    text_dict: dict,
    body_spans: list[dict],
) -> list[dict]:
    if not body_spans:
        return []
    body_keys = {
        (
            tuple(round(float(value), 3) for value in span["bbox"]),
            str(span.get("text", "")),
        )
        for span in body_spans
    }
    lines: list[dict] = []
    for block in text_dict["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            selected = [
                span
                for span in line.get("spans", [])
                if (
                    tuple(round(float(value), 3) for value in span["bbox"]),
                    str(span.get("text", "")),
                )
                in body_keys
            ]
            text = "".join(span.get("text", "") for span in selected).strip()
            if not text:
                continue
            lines.append(
                {
                    "text": text,
                    "bbox": [
                        min(float(span["bbox"][0]) for span in selected),
                        min(float(span["bbox"][1]) for span in selected),
                        max(float(span["bbox"][2]) for span in selected),
                        max(float(span["bbox"][3]) for span in selected),
                    ],
                    "size": statistics.median(
                        float(span["size"]) for span in selected
                    ),
                }
            )
    lines.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
    hits: list[dict] = []
    for previous, current in zip(lines, lines[1:]):
        orphan_core = current["text"].rstrip(
            ORPHAN_TRAILING_PUNCTUATION
        )
        if (
            not HAN_CHARACTER_PATTERN.fullmatch(orphan_core)
            or len(previous["text"]) < 8
            or abs(float(previous["size"]) - float(current["size"])) > 0.4
        ):
            continue
        vertical_gap = float(current["bbox"][1]) - float(previous["bbox"][3])
        if -0.5 <= vertical_gap <= float(current["size"]) * 1.5:
            hits.append(
                {
                    "text": current["text"],
                    "previous_text": previous["text"][-80:],
                    "bbox": [round(value, 2) for value in current["bbox"]],
                }
            )
    return hits


def _interline_gap_outliers(
    text_dict: dict,
    body_spans: list[dict],
    body_mode: float | None,
    minimum_ratio: float = 4.0,
) -> list[dict]:
    if not body_spans or body_mode is None or body_mode <= 0:
        return []
    body_keys = {
        (
            tuple(round(float(value), 3) for value in span["bbox"]),
            str(span.get("text", "")),
        )
        for span in body_spans
    }
    all_lines: list[dict] = []
    for block in text_dict["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [
                span
                for span in line.get("spans", [])
                if str(span.get("text") or "").strip()
            ]
            if not spans:
                continue
            all_lines.append(
                {
                    "text": "".join(
                        str(span.get("text") or "") for span in spans
                    ).strip(),
                    "bbox": [
                        min(float(span["bbox"][0]) for span in spans),
                        min(float(span["bbox"][1]) for span in spans),
                        max(float(span["bbox"][2]) for span in spans),
                        max(float(span["bbox"][3]) for span in spans),
                    ],
                }
            )
    lines: list[dict] = []
    for block in text_dict["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            selected = [
                span
                for span in line.get("spans", [])
                if (
                    tuple(round(float(value), 3) for value in span["bbox"]),
                    str(span.get("text", "")),
                )
                in body_keys
            ]
            text = "".join(span.get("text", "") for span in selected).strip()
            if not text:
                continue
            lines.append(
                {
                    "text": text,
                    "bbox": [
                        min(float(span["bbox"][0]) for span in selected),
                        min(float(span["bbox"][1]) for span in selected),
                        max(float(span["bbox"][2]) for span in selected),
                        max(float(span["bbox"][3]) for span in selected),
                    ],
                    "size": statistics.median(
                        float(span["size"]) for span in selected
                    ),
                }
            )
    lines.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
    hits: list[dict] = []
    for previous, current in zip(lines, lines[1:]):
        gap = float(current["bbox"][1]) - float(previous["bbox"][3])
        size = statistics.median(
            [float(previous["size"]), float(current["size"])]
        )
        if gap <= 0 or size <= 0:
            continue
        ratio = gap / size
        if ratio >= minimum_ratio:
            gap_left = min(
                float(previous["bbox"][0]),
                float(current["bbox"][0]),
            )
            gap_right = max(
                float(previous["bbox"][2]),
                float(current["bbox"][2]),
            )
            occupied = False
            for line in all_lines:
                center_y = (
                    float(line["bbox"][1]) + float(line["bbox"][3])
                ) / 2
                if not (
                    float(previous["bbox"][3]) < center_y
                    < float(current["bbox"][1])
                ):
                    continue
                horizontal_overlap = max(
                    0.0,
                    min(gap_right, float(line["bbox"][2]))
                    - max(gap_left, float(line["bbox"][0])),
                )
                if horizontal_overlap >= 4.0:
                    occupied = True
                    break
            if occupied:
                continue
            hits.append(
                {
                    "gap_pt": round(gap, 2),
                    "gap_to_font_ratio": round(ratio, 2),
                    "previous_text": previous["text"][-80:],
                    "next_text": current["text"][:80],
                }
            )
    return hits


def _paragraph_gap_inflation_justified(
    overrides: dict, page_number: int
) -> bool:
    for item in overrides.get("page_overrides", []):
        if not isinstance(item, dict) or not _page_selector_matches(
            item, page_number
        ):
            continue
        if (
            item.get("paragraph_gap_inflation_justified") is True
            and isinstance(item.get("reason"), str)
            and item["reason"].strip()
        ):
            return True
    return False


def _document_typography_locked(overrides: dict) -> bool:
    typography = overrides.get("document_typography")
    if not isinstance(typography, dict):
        return False
    leading_value = typography.get(
        "leading_ratio",
        typography.get(
            "leading",
            typography.get("body_leading"),
        ),
    )
    natural_spacing = (
        typography.get("paragraph_spacing_policy") == "natural"
        or typography.get("natural_paragraph_spacing") is True
        or isinstance(typography.get("paragraph_space_em"), (int, float))
    )
    return (
        typography.get("selection_method")
        in {"densest-page-fit", "actual-render-page-budget"}
        and (
            typography.get("all_body_pages_locked") is True
            or typography.get("font_locked_across_document") is True
        )
        and isinstance(typography.get("body_font_pt"), (int, float))
        and isinstance(leading_value, (int, float))
        and natural_spacing
        and isinstance(typography.get("reason"), str)
        and typography["reason"].strip()
    )


def _registered_generator_typography(
    job_dir: Path,
    job: dict,
    candidate_path: Path,
    candidate_mapping: dict[str, Any] | None,
) -> dict[str, float] | None:
    files = job.get("files", {})
    provenance_path = internal_job_path(
        job_dir,
        files.get("candidate_provenance", "candidate_provenance.json"),
    )
    layout_log_path = job_dir / "generator-layout-log.json"
    if not provenance_path.is_file() or not layout_log_path.is_file():
        return None
    provenance = load_json(provenance_path)
    layout_log = load_json(layout_log_path)
    candidate_hash = sha256_file(candidate_path)
    if provenance.get("candidate_sha256") != candidate_hash:
        return None
    if (
        isinstance(candidate_mapping, dict)
        and candidate_mapping.get("candidate_sha256") != candidate_hash
    ):
        return None
    if (
        provenance.get("renderer") != layout_log.get("renderer")
        or str(provenance.get("renderer_version") or "")
        != str(layout_log.get("renderer_version") or "")
        or str(provenance.get("renderer_build_id") or "")
        != str(layout_log.get("renderer_build_id") or "")
    ):
        return None
    body_font = layout_log.get("body_font_pt")
    reference_font = layout_log.get("reference_font_pt")
    if not isinstance(body_font, (int, float)) or not 5 <= body_font <= 30:
        return None
    result = {"body_font_pt": float(body_font)}
    if (
        isinstance(reference_font, (int, float))
        and 5 <= reference_font <= 30
    ):
        result["reference_font_pt"] = float(reference_font)
    return result


def _candidate_retained_source(
    retained: dict,
    candidate_mapping: dict[str, Any] | None,
) -> dict:
    if candidate_mapping is None:
        return retained
    candidate_regions: list[dict] = []
    for entry in candidate_mapping.get("retained_regions", []):
        if not isinstance(entry, dict):
            continue
        for region in entry.get("candidate_regions", []):
            if not isinstance(region, dict):
                continue
            candidate_page = region.get("candidate_page")
            bbox = region.get("bbox")
            if (
                not isinstance(candidate_page, int)
                or not isinstance(bbox, list)
                or len(bbox) != 4
            ):
                continue
            candidate_regions.append(
                {
                    "page": candidate_page,
                    "bbox": bbox,
                    "category": entry.get("category"),
                    "retained_region_id": entry.get("retained_region_id"),
                    "reason": "由候选页映射生成的保留原文区域。",
                }
            )
    candidate_items: list[dict] = []
    for item in retained.get("items", []):
        if not isinstance(item, dict):
            continue
        source_page = item.get("page")
        if not isinstance(source_page, int):
            candidate_items.append(dict(item))
            continue
        for candidate_page in candidate_pages_for_source(
            candidate_mapping,
            source_page,
        ):
            candidate_items.append({**item, "page": candidate_page})
    return {
        "schema_version": retained.get("schema_version", "1.0"),
        "items": candidate_items,
        "regions": candidate_regions,
    }


def _sparse_layout_justified(
    overrides: dict, page_number: int
) -> bool:
    for item in overrides.get("page_overrides", []):
        pages = item.get("pages", [])
        applies = item.get("page") == page_number or (
            isinstance(pages, list) and page_number in pages
        )
        if (
            applies
            and item.get("sparse_layout_justified") is True
            and isinstance(item.get("reason"), str)
            and item["reason"].strip()
        ):
            return True
    return False


def _horizontal_width_change_justified(
    overrides: dict, page_number: int
) -> bool:
    for item in overrides.get("page_overrides", []):
        if not isinstance(item, dict) or not _page_selector_matches(
            item, page_number
        ):
            continue
        if (
            item.get("horizontal_width_change_justified") is True
            and isinstance(item.get("reason"), str)
            and item["reason"].strip()
        ):
            return True
    return False


def _body_width_collapsed(
    source_ratio: float | None,
    candidate_ratio: float | None,
    retention_min: float,
    loss_trigger: float,
) -> bool:
    if (
        source_ratio is None
        or candidate_ratio is None
        or source_ratio <= 0
    ):
        return False
    retention = candidate_ratio / source_ratio
    loss = max(0.0, source_ratio - candidate_ratio)
    return retention < retention_min and loss >= loss_trigger


def _unit_is_substantive_body_prose(unit: dict | None) -> bool:
    if not isinstance(unit, dict):
        return False
    if str(unit.get("kind") or "").lower() not in {
        "body",
        "list-item",
        "paragraph",
    }:
        return False
    text = re.sub(r"\s+", " ", str(unit.get("source") or "")).strip()
    if not text:
        return False
    latin_words = re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)*", text)
    cjk_chars = re.findall(r"[\u3400-\u9fff]", text)
    if len(cjk_chars) >= 80 or len(latin_words) >= 28:
        return True
    return (
        len(latin_words) >= 18
        and bool(re.search(r"[.!?。！？](?:[\"'”’)\]]*)$", text))
    )


def _bottom_whitespace_is_unbalanced(
    excess_bottom_ratio: float,
    bottom_blank_ratio: float,
    top_blank_ratio: float,
    excess_trigger: float = 0.25,
    imbalance_trigger: float = 0.20,
) -> bool:
    return (
        excess_bottom_ratio >= excess_trigger
        and bottom_blank_ratio - top_blank_ratio >= imbalance_trigger
    )


def _excessive_unused_space_unjustified(
    page: dict,
    overrides: dict,
    pre_complex_break_pages: set[int],
) -> bool:
    page_number = int(page["page"])
    return (
        page["target_chars"] >= 120
        and page.get("mapped_has_body_prose", True)
        and not page.get("mapped_has_retained_regions", False)
        and not page["whole_page_reference_exception"]
        and not page["complex_visual_page"]
        and not page.get("is_final_candidate_page", False)
        and page_number not in pre_complex_break_pages
        and not _sparse_layout_justified(overrides, page_number)
        and _bottom_whitespace_is_unbalanced(
            page["excess_bottom_blank_ratio"],
            page["largest_column_bottom_blank_ratio"],
            page["top_blank_ratio"],
        )
    )


def _text_block_overlaps(text_dict: dict) -> list[dict]:
    blocks = []
    for block in text_dict["blocks"]:
        if block.get("type") != 0:
            continue
        text = "".join(
            span.get("text", "")
            for line in block.get("lines", [])
            for span in line.get("spans", [])
        ).strip()
        if len(text) < 12:
            continue
        bbox = [float(value) for value in block["bbox"]]
        area = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
        if area <= 0:
            continue
        blocks.append({"text": text, "bbox": bbox, "area": area})

    overlaps = []
    for index, first in enumerate(blocks):
        for second in blocks[index + 1 :]:
            x0 = max(first["bbox"][0], second["bbox"][0])
            y0 = max(first["bbox"][1], second["bbox"][1])
            x1 = min(first["bbox"][2], second["bbox"][2])
            y1 = min(first["bbox"][3], second["bbox"][3])
            intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
            if intersection / min(first["area"], second["area"]) < 0.35:
                continue
            overlaps.append(
                {
                    "first": first["text"][:100],
                    "second": second["text"][:100],
                    "intersection_ratio": round(
                        intersection / min(first["area"], second["area"]), 3
                    ),
                }
            )
    return overlaps


def _text_span_overlaps(spans: list[dict]) -> list[dict]:
    prepared = []
    for span in spans:
        text = span.get("text", "").strip()
        if len(text) < 2:
            continue
        bbox = [float(value) for value in span["bbox"]]
        area = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
        if area <= 0:
            continue
        prepared.append({"text": text, "bbox": bbox, "area": area})
    overlaps = []
    for index, first in enumerate(prepared):
        for second in prepared[index + 1 :]:
            x0 = max(first["bbox"][0], second["bbox"][0])
            y0 = max(first["bbox"][1], second["bbox"][1])
            x1 = min(first["bbox"][2], second["bbox"][2])
            y1 = min(first["bbox"][3], second["bbox"][3])
            intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
            ratio = intersection / min(first["area"], second["area"])
            if ratio < 0.45:
                continue
            overlaps.append(
                {
                    "first": first["text"][:100],
                    "second": second["text"][:100],
                    "intersection_ratio": round(ratio, 3),
                }
            )
    return overlaps


def _allowed_patterns(retained: dict) -> list[tuple[int | None, re.Pattern[str]]]:
    patterns: list[tuple[int | None, re.Pattern[str]]] = []
    for item in retained.get("items", []):
        value = item.get("pattern") or item.get("text")
        if not value:
            continue
        try:
            patterns.append(
                (
                    int(item["page"]) if item.get("page") is not None else None,
                    re.compile(value if item.get("is_regex") else re.escape(value)),
                )
            )
        except re.error as exc:
            raise SkillError(f"retained_source.json 中正则无效: {value}: {exc}") from exc
    return patterns


def _looks_like_proper_name(sample: str) -> bool:
    tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ’'`.-]*", sample)
    if not 2 <= len(tokens) <= 7:
        return False
    particles = {"de", "del", "der", "di", "du", "la", "le", "van", "von"}
    return all(
        token.casefold() in particles
        or bool(re.fullmatch(r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ’'`.-]*", token))
        for token in tokens
    )


def _compressed_page_requires_repair(page: dict) -> bool:
    return bool(
        page.get("compressed_despite_blank_space")
        and not page.get("whole_page_reference_exception")
        and not page.get("structured_table_visual_check")
        and not page.get("complex_visual_page")
        and not page.get("is_final_candidate_page", False)
    )


def _inventory_accounts_for_missing_image(item: dict) -> bool:
    policy = str(item.get("translation_policy") or "").lower()
    if policy == "omit-nonsemantic":
        return bool(
            item.get("text_status") == "not-applicable"
            and str(item.get("translation_policy_reason") or "").strip()
        )
    method = str(item.get("method") or "").lower()
    status = str(item.get("status") or "").lower()
    payload_ready = bool(
        status == "payload-ready"
        and str(item.get("payload_status") or "").lower() == "ready"
        and str(item.get("text_status") or "").lower() == "translated"
        and str(item.get("complex_payload_id") or "").strip()
    )
    return bool(
        method in {"vector-rebuild", "structured-table-rebuild"}
        and (
            status in {"translated", "resolved", "pass"}
            or payload_ready
        )
    )


def _meaningful_image_bbox(
    bbox: Any,
    *,
    page_width: float,
    page_height: float,
) -> bool:
    if (
        not isinstance(bbox, (list, tuple))
        or len(bbox) != 4
        or not all(isinstance(value, (int, float)) for value in bbox)
    ):
        return False
    x0, y0, x1, y1 = map(float, bbox)
    width = abs(x1 - x0)
    height = abs(y1 - y0)
    page_area = max(float(page_width) * float(page_height), 1.0)
    return bool(
        width >= 8.0
        and height >= 8.0
        and width * height >= max(100.0, page_area * 0.00025)
    )


def _meaningful_page_image_count(page: Any) -> int:
    page_width = float(page.rect.width)
    page_height = float(page.rect.height)
    try:
        image_info = page.get_image_info(xrefs=True)
    except Exception:
        image_info = []
    if not image_info:
        image_info = [
            block
            for block in page.get_text("dict").get("blocks", [])
            if block.get("type") == 1
        ]
    seen: set[tuple[Any, tuple[float, ...]]] = set()
    for item in image_info:
        bbox = item.get("bbox") if isinstance(item, dict) else None
        if not _meaningful_image_bbox(
            bbox,
            page_width=page_width,
            page_height=page_height,
        ):
            continue
        key = (
            item.get("xref") if isinstance(item, dict) else None,
            tuple(round(float(value), 2) for value in bbox),
        )
        seen.add(key)
    return len(seen)


def _residual_source_prose(
    spans: list[dict],
    page_number: int,
    retained: dict,
    allowed_patterns: list[tuple[int | None, re.Pattern[str]]],
    allowed_corpus: str = "",
) -> list[dict]:
    regions = _regions_for_page(retained.get("regions", []), page_number)
    hits: list[dict] = []
    for span in spans:
        if _in_any_region(span["bbox"], regions):
            continue
        text = span["text"]
        text = re.sub(r"https?://\S+|doi:\s*\S+|10\.\d{4,9}/\S+", "", text, flags=re.I)
        for pattern_page, pattern in allowed_patterns:
            if pattern_page is None or pattern_page == page_number:
                text = pattern.sub("", text)
        for match in LATIN_PROSE_PATTERN.finditer(text):
            sample = match.group(0).strip()
            if len(sample) < 18:
                continue
            if _looks_like_proper_name(sample):
                continue
            sample_key = re.sub(r"[^a-z0-9]+", "", sample.casefold())
            if sample_key and sample_key in allowed_corpus:
                continue
            hits.append(
                {
                    "page": page_number,
                    "text": sample[:220],
                    "bbox": [round(float(value), 2) for value in span["bbox"]],
                }
            )
    return hits


def _font_name_token(value: str) -> str:
    name = re.sub(r"^[A-Z]{6}\+", "", str(value or ""))
    name = re.sub(r"-\d+$", "", name)
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def _font_embedding_issues(document: Any) -> list[dict]:
    issues: list[dict] = []
    seen: set[int] = set()
    used_font_tokens = {
        token
        for page_number in range(document.page_count)
        for block in document[page_number].get_text("dict").get("blocks", [])
        if block.get("type") == 0
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        if (token := _font_name_token(str(span.get("font") or "")))
    }
    for page_number in range(document.page_count):
        for font in document.get_page_fonts(page_number, full=True):
            xref = int(font[0])
            if xref in seen:
                continue
            seen.add(xref)
            basefont = str(font[3])
            basefont_token = _font_name_token(basefont)
            if basefont_token and not any(
                basefont_token in used or used in basefont_token
                for used in used_font_tokens
            ):
                continue
            if xref <= 0:
                issues.append({"xref": xref, "font": basefont, "reason": "no-xref"})
                continue
            try:
                extracted = document.extract_font(xref)
                font_bytes = extracted[-1] if extracted else b""
            except Exception:
                font_bytes = b""
            if not font_bytes:
                issues.append(
                    {"xref": xref, "font": basefont, "reason": "not-embedded"}
                )
    return issues


def _page_metrics(
    page: Any,
    page_number: int,
    profile: dict,
    quality: dict,
    overrides: dict,
    retained: dict,
    allowed_patterns: list[tuple[int | None, re.Pattern[str]]],
    allowed_retained_corpus: str = "",
) -> dict:
    text_dict = page.get_text("dict")
    spans = [
        span
        for block in text_dict["blocks"]
        if block.get("type") == 0
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        if span.get("text", "").strip()
    ]
    text = "\n".join(span["text"] for span in spans)
    retained_regions = _regions_for_page(retained.get("regions", []), page_number)
    non_body_regions = _regions_for_page(
        overrides.get("non_body_regions", []), page_number
    )
    table_regions = [
        region
        for region in non_body_regions
        if "table" in str(region.get("category", "")).lower()
    ]
    body_spans, body_detection = _body_spans(
        page, page_number, spans, overrides, retained_regions
    )
    body_mode = _weighted_font_mode(body_spans)
    table_font_min_pt = float(quality.get("table_font_min_pt", 7.0))
    low_table_spans = _low_table_spans(
        spans, table_regions, table_font_min_pt
    )
    body_font_size_weights: Counter[float] = Counter()
    for span in body_spans:
        span_text = span.get("text", "").strip()
        if span_text:
            body_font_size_weights[round(float(span["size"]), 1)] += len(
                span_text
            )
    leading = _leading_ratios(
        text_dict, body_mode, page_number, overrides, retained_regions
    )
    median_leading = round(statistics.median(leading), 3) if leading else None
    median_body_line_width, body_line_width_sample_count = (
        _body_line_width_ratio(page, text_dict, body_spans)
    )
    orphan_single_han_lines = _orphan_single_han_lines(
        text_dict, spans
    )
    interline_gap_outliers = _interline_gap_outliers(
        text_dict, body_spans, body_mode
    )
    low_body_spans = []
    if body_mode is not None:
        for span in body_spans:
            text_value = span["text"].strip()
            size = float(span["size"])
            if (
                len(text_value) >= 24
                and size < float(quality["body_font_min_pt"])
                and abs(size - body_mode) <= 1.2
            ):
                low_body_spans.append(
                    {
                        "text": text_value[:120],
                        "size": round(size, 3),
                        "bbox": [round(float(value), 2) for value in span["bbox"]],
                    }
                )

    out_of_bounds = []
    coordinate_rect = (
        page.cropbox
        if int(getattr(page, "rotation", 0)) in {90, 270}
        else page.rect
    )
    bounds_tolerance = (
        3.0 if int(getattr(page, "rotation", 0)) in {90, 270} else 0.5
    )
    for span in spans:
        rect = span["bbox"]
        if (
            float(rect[0]) < float(coordinate_rect.x0) - bounds_tolerance
            or float(rect[1]) < float(coordinate_rect.y0) - bounds_tolerance
            or float(rect[2]) > float(coordinate_rect.x1) + bounds_tolerance
            or float(rect[3]) > float(coordinate_rect.y1) + bounds_tolerance
        ):
            out_of_bounds.append(
                {
                    "text": span["text"][:120],
                    "bbox": [round(float(value), 2) for value in rect],
                }
            )

    target_chars = target_character_count(text, profile["writing_system"])
    blank_ratio = _column_blank_ratio(page, body_spans)
    top_blank_ratio = _top_blank_ratio(page, body_spans)
    compressed = (
        (
            body_mode is not None
            and body_mode < float(
                quality.get(
                    "body_font_preferred_pt",
                    quality["body_font_target_pt"][0],
                )
            )
            - 0.3
        )
        or (
            median_leading is not None
            and median_leading < float(
                quality.get(
                    "leading_preferred",
                    quality["leading_target"][0],
                )
            )
        )
    )
    sparse_unjustified = (
        target_chars >= 120
        and blank_ratio >= 0.25
        and not _sparse_layout_justified(overrides, page_number)
    )
    source_residuals = []
    if profile["residual_source_scan"] == "latin-prose":
        source_residuals = _residual_source_prose(
            spans,
            page_number,
            retained,
            allowed_patterns,
            allowed_retained_corpus,
        )
    structured_table = _structured_table_page(
        page_number,
        overrides.get("page_overrides", []),
        non_body_regions,
    )

    return {
        "page": page_number,
        "width": round(float(page.rect.width), 3),
        "height": round(float(page.rect.height), 3),
        "text_chars": len(text),
        "target_chars": target_chars,
        "images": _meaningful_page_image_count(page),
        "drawings": len(page.get_drawings()),
        "body_detection": body_detection,
        "body_font_mode_pt": body_mode,
        "body_font_size_weights": {
            str(size): weight
            for size, weight in sorted(body_font_size_weights.items())
        },
        "low_body_spans": low_body_spans,
        "table_font_min_pt": table_font_min_pt,
        "low_table_spans": low_table_spans,
        "median_leading_ratio": median_leading,
        "leading_sample_count": len(leading),
        "median_body_line_width_ratio": median_body_line_width,
        "body_line_width_sample_count": body_line_width_sample_count,
        "orphan_single_han_lines": orphan_single_han_lines,
        "interline_gap_outliers": interline_gap_outliers,
        "largest_column_bottom_blank_ratio": blank_ratio,
        "top_blank_ratio": top_blank_ratio,
        "vertical_blank_imbalance_ratio": round(
            blank_ratio - top_blank_ratio, 3
        ),
        "compressed_despite_blank_space": compressed and blank_ratio >= 0.18,
        "sparse_layout_unjustified": sparse_unjustified,
        "out_of_bounds_spans": out_of_bounds,
        "replacement_chars": text.count("\ufffd"),
        "compatibility_ideographs": COMPATIBILITY_IDEOGRAPH_PATTERN.findall(text),
        "placeholder_hits": PLACEHOLDER_PATTERN.findall(text),
        "source_residuals": source_residuals,
        "text_block_overlaps": (
            [] if structured_table else _text_block_overlaps(text_dict)
        ),
        "text_span_overlaps": _text_span_overlaps(spans),
        "structured_table_visual_check": structured_table,
        "whole_page_reference_exception": _whole_page_reference(
            page, retained_regions
        ),
        "reference_region_area_ratio": round(
            _reference_area_ratio(page, retained_regions),
            3,
        ),
        "null_characters": text.count("\x00"),
    }


def _timed_run_qa(job_dir: Path) -> dict:
    job_dir = job_dir.resolve()
    job = load_json(job_dir / "job.json")
    files = job["files"]
    source_path = internal_job_path(job_dir, job["source"]["job_path"])
    candidate_path = internal_job_path(job_dir, files["candidate"])
    if not source_path.is_file():
        raise SkillError(f"缺少原文: {source_path}")
    if not candidate_path.is_file():
        raise SkillError(f"缺少候选 PDF: {candidate_path}")

    _, profile = resolve_language_profile(job["translation"]["target_language"])
    quality = job["quality"]
    overrides = load_json(internal_job_path(job_dir, files["layout_overrides"]))
    retained = load_json(internal_job_path(job_dir, files["retained_source"]))
    figure_inventory = load_json(
        internal_job_path(job_dir, files["figure_inventory"])
    )
    translation = load_json(
        internal_job_path(job_dir, files["translation"])
    )
    complex_path = internal_job_path(
        job_dir,
        files.get("complex_content_payload", "complex_content.json"),
    )
    complex_content = (
        load_json(complex_path)
        if complex_path.is_file()
        else {"items": []}
    )
    fitz = import_fitz()
    source = fitz.open(source_path)
    candidate = fitz.open(candidate_path)
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
    generator_typography = _registered_generator_typography(
        job_dir,
        job,
        candidate_path,
        candidate_mapping,
    )
    structured_candidate_pages = _structured_complex_candidate_pages(
        complex_content,
        candidate_mapping,
    )
    pre_complex_break_pages = _pre_complex_break_pages(
        candidate_mapping,
        _all_complex_candidate_pages(
            complex_content,
            candidate_mapping,
        ),
    )
    candidate_retained = _candidate_retained_source(
        retained,
        candidate_mapping,
    )
    allowed_patterns = _allowed_patterns(candidate_retained)
    allowed_corpus_by_candidate: dict[int, str] = defaultdict(str)
    retained_payloads = extract_retained_regions(
        source,
        retained,
        translation,
    )
    retained_payload_by_id = {
        str(payload["id"]): payload
        for payload in retained_payloads
        if isinstance(payload, dict) and str(payload.get("id") or "")
    }
    if candidate_mapping is not None:
        for entry in candidate_mapping.get("retained_regions", []):
            if not isinstance(entry, dict):
                continue
            payload = retained_payload_by_id.get(
                str(entry.get("retained_region_id") or "")
            )
            if not payload:
                continue
            retained_key = _allowed_latin_corpus(
                str(payload.get("text") or "")
            )
            mapped_pages = {
                page
                for page in entry.get("candidate_pages", [])
                if isinstance(page, int)
            }
            if isinstance(payload.get("page"), int):
                mapped_pages.update(
                    candidate_pages_for_source(
                        candidate_mapping,
                        int(payload["page"]),
                    )
                )
            for candidate_page in mapped_pages:
                allowed_corpus_by_candidate[candidate_page] += (
                    retained_key + "\n"
                )
        complex_by_id = {
            str(item.get("id") or ""): item
            for item in complex_content.get("items", [])
            if isinstance(item, dict) and str(item.get("id") or "")
        }
        for entry in candidate_mapping.get("complex_items", []):
            if not isinstance(entry, dict):
                continue
            item = complex_by_id.get(
                str(entry.get("complex_item_id") or "")
            )
            if not item:
                continue
            source_labels = _complex_localized_source_labels(item)
            if not source_labels:
                continue
            localized_key = _allowed_latin_corpus(
                "\n".join(source_labels)
            )
            for candidate_page in entry.get("candidate_pages", []):
                if isinstance(candidate_page, int):
                    allowed_corpus_by_candidate[candidate_page] += (
                        localized_key + "\n"
                    )
    for unit in translation.get("units", []):
        if not isinstance(unit, dict) or not isinstance(unit.get("page"), int):
            continue
        text = str(unit.get("translation") or unit.get("source") or "")
        mapped_candidate_pages = candidate_pages_for_unit(
            candidate_mapping,
            str(unit.get("id") or ""),
            int(unit["page"]),
        )
        if (
            profile["writing_system"] != "latin"
            and target_character_count(text, profile["writing_system"]) >= 12
        ):
            translated_key = _allowed_latin_corpus(text)
            for candidate_page in mapped_candidate_pages:
                allowed_corpus_by_candidate[candidate_page] += (
                    translated_key + "\n"
                )
        kind = str(unit.get("kind") or "").lower()
        markers = (
            "参考文献题录（保留原文）",
            "参考文献（题录保留原文）",
            "參考文獻（題錄保留原文）",
        )
        marker = next((value for value in markers if value in text), None)
        if marker:
            retained_text = text.split(marker, 1)[1]
        elif unit.get("keep_source_reason") or kind in REFERENCE_KINDS:
            retained_text = text
        else:
            continue
        retained_key = _allowed_latin_corpus(retained_text)
        for candidate_page in mapped_candidate_pages:
            allowed_corpus_by_candidate[candidate_page] += (
                retained_key + "\n"
            )

    source_pages = []
    for index, page in enumerate(source, 1):
        text_dict = page.get_text("dict")
        spans = [
            span
            for block in text_dict["blocks"]
            if block.get("type") == 0
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if span.get("text", "").strip()
        ]
        retained_regions = _regions_for_page(
            retained.get("regions", []), index
        )
        body_spans, _ = _body_spans(
            page,
            index,
            spans,
            {
                "body_regions": [],
                "non_body_regions": overrides.get("non_body_regions", []),
            },
            retained_regions,
        )
        median_body_line_width, body_line_width_sample_count = (
            _body_line_width_ratio(page, text_dict, body_spans)
        )
        source_pages.append(
            {
                "page": index,
                "width": round(float(page.rect.width), 3),
                "height": round(float(page.rect.height), 3),
                "images": _meaningful_page_image_count(page),
                "median_body_line_width_ratio": median_body_line_width,
                "body_line_width_sample_count": body_line_width_sample_count,
                "largest_column_bottom_blank_ratio": _column_blank_ratio(
                    page, body_spans
                ),
            }
        )
    candidate_overrides = overrides
    if candidate_mapping is not None:
        remapped_page_overrides = []
        remapped_non_body_regions = []
        for item in overrides.get("page_overrides", []):
            if not isinstance(item, dict):
                continue
            source_numbers = []
            if isinstance(item.get("page"), int):
                source_numbers.append(int(item["page"]))
            if isinstance(item.get("pages"), list):
                source_numbers.extend(
                    int(page)
                    for page in item["pages"]
                    if isinstance(page, int)
                )
            candidate_numbers = sorted(
                {
                    candidate_page
                    for source_page in source_numbers
                    for candidate_page in candidate_pages_for_source(
                        candidate_mapping,
                        source_page,
                    )
                }
            )
            if not candidate_numbers:
                continue
            remapped = {
                key: value
                for key, value in item.items()
                if key not in {"page", "pages"}
            }
            remapped["pages"] = candidate_numbers
            remapped_page_overrides.append(remapped)
        for candidate_page in sorted(structured_candidate_pages):
            remapped_page_overrides.append(
                {
                    "page": candidate_page,
                    "layout": "structured-table",
                    "structured_table": True,
                    "reason": "就绪的复杂页载荷以结构化表格方式进入候选。",
                }
            )
            page_rect = candidate[candidate_page - 1].rect
            remapped_non_body_regions.append(
                {
                    "page": candidate_page,
                    "bbox": [
                        float(page_rect.x0),
                        float(page_rect.y0),
                        float(page_rect.x1),
                        float(page_rect.y1),
                    ],
                    "category": "structured-table",
                }
            )
        candidate_overrides = {
            **overrides,
            "body_regions": [],
            "non_body_regions": remapped_non_body_regions,
            "page_overrides": remapped_page_overrides,
        }
    candidate_pages = [
        _page_metrics(
            page,
            index,
            profile,
            quality,
            candidate_overrides,
            candidate_retained,
            allowed_patterns,
            allowed_corpus_by_candidate.get(index, ""),
        )
        for index, page in enumerate(candidate, 1)
    ]
    if candidate_pages:
        candidate_pages[-1]["is_final_candidate_page"] = True
    candidate_text = "\n".join(page.get_text("text") for page in candidate)
    expected_literal_placeholder_tokens = (
        _expected_literal_placeholder_tokens(translation)
    )
    unit_by_id = {
        str(unit.get("id") or ""): unit
        for unit in translation.get("units", [])
        if isinstance(unit, dict)
    }

    hard_failures: list[dict] = []
    review_flags: list[dict] = []
    if pre_complex_break_pages:
        review_flags.append(
            {
                "code": "NATURAL_BREAK_BEFORE_COMPLEX_CONTENT",
                "pages": sorted(pre_complex_break_pages),
                "message": (
                    "下一候选页承载同一或紧邻原文页的大型结构化内容，"
                    "本页页底留白按自然分页处理并保留目视复核。"
                ),
            }
        )
    if candidate_mapping is None and source.page_count != candidate.page_count:
        hard_failures.append(
            {
                "code": "PAGE_COUNT_MISMATCH",
                "source": source.page_count,
                "candidate": candidate.page_count,
            }
        )
    elif candidate_mapping is not None and source.page_count != candidate.page_count:
        review_flags.append(
            {
                "code": "PAGINATION_REFLOWED_WITH_SOURCE_MAP",
                "source_pages": source.page_count,
                "candidate_pages": candidate.page_count,
                "message": "正文按连续阅读重新分页，逐页核对使用候选页映射。",
            }
        )
    size_mismatches = []
    for candidate_page in candidate_pages:
        mapped_source_numbers = source_pages_for_candidate(
            candidate_mapping,
            int(candidate_page["page"]),
        )
        mapped_source_metrics = [
            source_pages[source_page - 1]
            for source_page in mapped_source_numbers
            if 1 <= source_page <= len(source_pages)
        ]
        if not mapped_source_metrics:
            continue
        source_blank_values = [
            float(item["largest_column_bottom_blank_ratio"])
            for item in mapped_source_metrics
        ]
        source_blank = statistics.median(source_blank_values)
        candidate_page["mapped_source_pages"] = mapped_source_numbers
        mapped_complex_ids = []
        mapped_unit_ids: list[str] = []
        mapped_entry: dict[str, Any] = {}
        if candidate_mapping is not None:
            mapped_entry = next(
                (
                    entry
                    for entry in candidate_mapping.get(
                        "candidate_pages",
                        [],
                    )
                    if isinstance(entry, dict)
                    and entry.get("candidate_page")
                    == candidate_page["page"]
                ),
                {},
            )
            mapped_complex_ids = [
                str(value)
                for value in mapped_entry.get("complex_item_ids", [])
            ]
            mapped_unit_ids = [
                str(value)
                for value in mapped_entry.get("unit_ids", [])
            ]
        candidate_page["mapped_complex_item_ids"] = mapped_complex_ids
        candidate_page["mapped_has_retained_regions"] = (
            _mapped_entry_has_visible_retained_content(mapped_entry)
        )
        candidate_page["mapped_has_body_prose"] = (
            any(
                (
                    _unit_is_substantive_body_prose(
                        unit_by_id.get(unit_id)
                    )
                    and str(
                        mapped_entry.get(
                            "unit_layout_roles",
                            {},
                        ).get(unit_id)
                        or ""
                    ).lower()
                    not in {
                        "publication-metadata",
                        "formal-citation-footer",
                        "footnote",
                    }
                )
                for unit_id in mapped_unit_ids
            )
            if candidate_mapping is not None
            else True
        )
        candidate_page["complex_visual_page"] = bool(mapped_complex_ids)
        candidate_page["source_bottom_blank_ratio"] = round(source_blank, 3)
        candidate_page["excess_bottom_blank_ratio"] = round(
            max(
                0.0,
                candidate_page["largest_column_bottom_blank_ratio"]
                - source_blank,
            ),
            3,
        )
        source_width_values = [
            float(item["median_body_line_width_ratio"])
            for item in mapped_source_metrics
            if item["median_body_line_width_ratio"] is not None
        ]
        source_width_ratio = (
            statistics.median(source_width_values)
            if source_width_values
            else None
        )
        candidate_width_ratio = candidate_page[
            "median_body_line_width_ratio"
        ]
        candidate_page["source_body_line_width_ratio"] = source_width_ratio
        candidate_page["body_width_retention_ratio"] = (
            round(candidate_width_ratio / source_width_ratio, 3)
            if source_width_ratio
            and candidate_width_ratio is not None
            else None
        )
        candidate_page["body_width_loss_ratio"] = (
            round(max(0.0, source_width_ratio - candidate_width_ratio), 3)
            if source_width_ratio is not None
            and candidate_width_ratio is not None
            else None
        )
        if candidate_mapping is None and not any(
            abs(source_page["width"] - candidate_page["width"]) <= 0.1
            and abs(source_page["height"] - candidate_page["height"]) <= 0.1
            for source_page in mapped_source_metrics
        ):
            size_mismatches.append(candidate_page["page"])
    if size_mismatches:
        hard_failures.append(
            {"code": "PAGE_SIZE_MISMATCH", "pages": size_mismatches}
        )

    empty_pages = [
        page["page"]
        for page in candidate_pages
        if page["text_chars"] == 0 and page["images"] == 0
    ]
    if empty_pages:
        hard_failures.append({"code": "BLANK_PAGES", "pages": empty_pages})

    target_text_missing = [
        page["page"]
        for page in candidate_pages
        if not page["whole_page_reference_exception"]
        and page["target_chars"]
        < int(profile["minimum_target_chars_per_nonreference_page"])
    ]
    if target_text_missing:
        hard_failures.append(
            {"code": "TARGET_TEXT_MISSING", "pages": target_text_missing}
        )

    font_floor_pages = [
        page["page"]
        for page in candidate_pages
        if not page["whole_page_reference_exception"]
        and not page.get("complex_visual_page")
        and (
            (
                page["body_font_mode_pt"] is not None
                and page["body_font_mode_pt"] < float(quality["body_font_min_pt"])
            )
            or page["low_body_spans"]
        )
    ]
    if font_floor_pages:
        hard_failures.append(
            {"code": "BODY_FONT_BELOW_MINIMUM", "pages": font_floor_pages}
        )

    low_table_pages = []
    for page in candidate_pages:
        hits = page["low_table_spans"]
        low_character_count = sum(len(hit["text"]) for hit in hits)
        if hits and (
            low_character_count >= 24
            or any(len(hit["text"]) >= 12 for hit in hits)
        ):
            low_table_pages.append(
                {
                    "page": page["page"],
                    "minimum_font_pt": page["table_font_min_pt"],
                    "low_character_count": low_character_count,
                    "samples": hits[:12],
                }
            )
    if low_table_pages:
        hard_failures.append(
            {
                "code": "TABLE_FONT_BELOW_MINIMUM",
                "pages": low_table_pages,
                "message": (
                    "结构化表格使用独立字号层级，但仍须原尺寸可读；"
                    "不得把表格标成非正文后用极小字号绕过正文门槛。"
                ),
            }
        )

    leading_exception_pages = {
        int(item["page"])
        for item in overrides.get("leading_exceptions", [])
        if item.get("page") is not None and item.get("reason")
    }
    leading_fail_pages = []
    undocumented_tight_pages = []
    missing_leading_samples = []
    for page in candidate_pages:
        ratio = page["median_leading_ratio"]
        if ratio is None:
            if (
                page["text_chars"] >= 300
                and not page["whole_page_reference_exception"]
                and not page["structured_table_visual_check"]
            ):
                missing_leading_samples.append(page["page"])
            continue
        if (
            ratio < float(quality["leading_exception_min"])
            and not page["whole_page_reference_exception"]
            and not page["structured_table_visual_check"]
            and not page.get("complex_visual_page")
        ):
            leading_fail_pages.append(page["page"])
        elif (
            ratio < float(quality["leading_target"][0])
            and page["page"] not in leading_exception_pages
            and not page["whole_page_reference_exception"]
            and not page["structured_table_visual_check"]
            and not page.get("complex_visual_page")
        ):
            undocumented_tight_pages.append(page["page"])
    if leading_fail_pages:
        hard_failures.append(
            {"code": "LEADING_BELOW_HARD_MINIMUM", "pages": leading_fail_pages}
        )
    if undocumented_tight_pages:
        hard_failures.append(
            {
                "code": "LEADING_EXCEPTION_NOT_DOCUMENTED",
                "pages": undocumented_tight_pages,
            }
        )
    if missing_leading_samples:
        review_flags.append(
            {
                "code": "LEADING_REQUIRES_VISUAL_CHECK",
                "pages": missing_leading_samples,
            }
        )

    ordinary_body_pages = [
        page
        for page in candidate_pages
        if (
            page["body_font_mode_pt"] is not None
            and page["target_chars"] >= 120
            and page.get("mapped_has_body_prose", True)
            and not page["whole_page_reference_exception"]
            and not page["structured_table_visual_check"]
            and not page.get("complex_visual_page")
        )
    ]
    if ordinary_body_pages:
        if generator_typography is not None:
            document_body_mode = generator_typography["body_font_pt"]
            reference_font_mode = generator_typography.get(
                "reference_font_pt"
            )
            mode_source = "registered-generator"
        else:
            document_weights: Counter[float] = Counter()
            for page in ordinary_body_pages:
                for size, weight in page[
                    "body_font_size_weights"
                ].items():
                    document_weights[float(size)] += int(weight)
            document_body_mode = float(
                document_weights.most_common(1)[0][0]
            )
            reference_font_mode = None
            mode_source = "document-character-mode"

        inconsistent_body_fonts = []
        for page in ordinary_body_pages:
            weights = {
                float(size): int(weight)
                for size, weight in page[
                    "body_font_size_weights"
                ].items()
            }
            evaluated_weights = weights
            if (
                reference_font_mode is not None
                and abs(reference_font_mode - document_body_mode) > 0.25
            ):
                without_reference = {
                    size: weight
                    for size, weight in weights.items()
                    if abs(size - reference_font_mode) > 0.25
                }
                if without_reference:
                    evaluated_weights = without_reference
            expected_weight = sum(
                weight
                for size, weight in evaluated_weights.items()
                if abs(size - document_body_mode) <= 0.25
            )
            required_weight = max(
                24,
                int(sum(evaluated_weights.values()) * 0.20),
            )
            if expected_weight >= required_weight:
                continue
            inconsistent_body_fonts.append(
                {
                    "page": page["page"],
                    "body_font_mode_pt": page["body_font_mode_pt"],
                    "document_body_mode_pt": round(
                        document_body_mode,
                        2,
                    ),
                    "document_mode_character_weight": expected_weight,
                    "minimum_character_weight": required_weight,
                    "mode_source": mode_source,
                }
            )
        if inconsistent_body_fonts:
            hard_failures.append(
                {
                    "code": "BODY_FONT_INCONSISTENT_ACROSS_DOCUMENT",
                    "pages": inconsistent_body_fonts,
                    "message": (
                        "普通正文应先按全篇最密页计算统一字号，再冻结到全篇；"
                        "不得逐页缩放正文。"
                    ),
                }
            )

    inflated_paragraph_gaps = []
    for page in candidate_pages:
        gaps = page["interline_gap_outliers"]
        if (
            not gaps
            or page["whole_page_reference_exception"]
            or page["structured_table_visual_check"]
            or page.get("complex_visual_page")
            or _paragraph_gap_inflation_justified(overrides, page["page"])
        ):
            continue
        if (
            len(gaps) >= 2
            or max(gap["gap_to_font_ratio"] for gap in gaps) >= 8.0
        ):
            inflated_paragraph_gaps.append(
                {
                    "page": page["page"],
                    "gaps": gaps[:8],
                }
            )
    if inflated_paragraph_gaps:
        hard_failures.append(
            {
                "code": "PARAGRAPH_GAP_INFLATION",
                "pages": inflated_paragraph_gaps,
                "message": (
                    "检测到以超大段间距追赶页面高度。应保持自然段落流，"
                    "优先按全篇统一字号放大正文；不得用机械分块和极端段距填页。"
                ),
            }
        )

    compressed_pages = [
        page["page"]
        for page in candidate_pages
        if _compressed_page_requires_repair(page)
    ]
    if compressed_pages:
        hard_failures.append(
            {"code": "COMPRESSED_WITH_UNUSED_SPACE", "pages": compressed_pages}
        )
    sparse_pages = [
        {
            "page": page["page"],
            "largest_column_bottom_blank_ratio": page[
                "largest_column_bottom_blank_ratio"
            ],
            "top_blank_ratio": page["top_blank_ratio"],
            "vertical_blank_imbalance_ratio": page[
                "vertical_blank_imbalance_ratio"
            ],
        }
        for page in candidate_pages
        if (
            page["sparse_layout_unjustified"]
            and not page.get("mapped_has_retained_regions", False)
            and page["page"] not in pre_complex_break_pages
        )
    ]
    if sparse_pages:
        review_flags.append(
            {
                "code": "SPARSE_PAGE_REQUIRES_JUSTIFICATION",
                "pages": sparse_pages,
                "message": (
                    "页面仍有较大可用空间。优先增加字号、行距、题项间距或"
                    "重平衡版心；确需保留时在 page_overrides 中记录理由。"
                ),
            }
        )
    excessive_unused_space = [
        {
            "page": page["page"],
            "source_bottom_blank_ratio": page["source_bottom_blank_ratio"],
            "candidate_bottom_blank_ratio": page[
                "largest_column_bottom_blank_ratio"
            ],
            "excess_bottom_blank_ratio": page[
                "excess_bottom_blank_ratio"
            ],
        }
        for page in candidate_pages
        if _excessive_unused_space_unjustified(
            page,
            overrides,
            pre_complex_break_pages,
        )
    ]
    if excessive_unused_space:
        if _document_typography_locked(overrides):
            review_flags.append(
                {
                    "code": "NATURAL_SHORT_PAGE_AFTER_DOCUMENT_TYPOGRAPHY",
                    "pages": excessive_unused_space,
                    "message": (
                        "全篇普通正文已按最密页试排锁定统一字号与行距，且段距膨胀、"
                        "正文缩字和版心坍缩均由其他门禁独立检查。剩余短页留白必须"
                        "逐页原尺寸人工复核，不能由自动QA直接判定PASS。"
                    ),
                }
            )
        else:
            hard_failures.append(
                {
                    "code": "EXCESSIVE_UNUSED_SPACE_VS_SOURCE",
                    "pages": excessive_unused_space,
                }
            )

    width_retention_min = float(
        quality.get("body_width_retention_min", 0.72)
    )
    width_loss_trigger = float(
        quality.get("body_width_loss_trigger", 0.12)
    )
    collapsed_body_width_pages = [
        {
            "page": page["page"],
            "source_body_line_width_ratio": page[
                "source_body_line_width_ratio"
            ],
            "candidate_body_line_width_ratio": page[
                "median_body_line_width_ratio"
            ],
            "body_width_retention_ratio": page[
                "body_width_retention_ratio"
            ],
            "body_width_loss_ratio": page["body_width_loss_ratio"],
        }
        for page in candidate_pages
        if (
            page["target_chars"] >= 120
            and page.get("mapped_has_body_prose", True)
            and not page["whole_page_reference_exception"]
            and not page["structured_table_visual_check"]
            and not page.get("complex_visual_page")
            and _body_width_collapsed(
                page["source_body_line_width_ratio"],
                page["median_body_line_width_ratio"],
                width_retention_min,
                width_loss_trigger,
            )
            and not _horizontal_width_change_justified(
                overrides, page["page"]
            )
        )
    ]
    if collapsed_body_width_pages:
        hard_failures.append(
            {
                "code": "BODY_WIDTH_COLLAPSE_VS_SOURCE",
                "pages": collapsed_body_width_pages,
                "message": (
                    "普通正文的横向版心相对原文被显著压窄。"
                    "应优先保持原文正文宽度，并通过自然段流排、字号、"
                    "行距和段距处理纵向空间；只有任务明确批准新版式时，"
                    "才可记录 horizontal_width_change_justified。"
                ),
            }
        )
    complex_visual_pages = [
        page["page"]
        for page in candidate_pages
        if page.get("complex_visual_page")
    ]
    if complex_visual_pages:
        review_flags.append(
            {
                "code": "COMPLEX_VISUAL_LAYOUT_REQUIRES_VISUAL_CHECK",
                "pages": complex_visual_pages,
                "message": (
                    "图片、图表或矢量模型页不使用普通正文段距和行宽门槛，"
                    "必须按原尺寸核对结构与可读性。"
                ),
            }
        )

    orphan_han_pages = [
        {
            "page": page["page"],
            "hits": page["orphan_single_han_lines"],
        }
        for page in candidate_pages
        if (
            page["orphan_single_han_lines"]
            and not page["whole_page_reference_exception"]
            and not page["structured_table_visual_check"]
        )
    ]
    if orphan_han_pages:
        hard_failures.append(
            {
                "code": "ORPHAN_SINGLE_HAN_LINE",
                "pages": orphan_han_pages,
                "message": (
                    "检测到紧跟长行的单个汉字续行。"
                    "应通过平衡断行、调整标题字号或改写合法换行修复；"
                    "不得把单字孤行作为正常排版交付。"
                ),
            }
        )

    out_of_bounds_pages = [
        page["page"] for page in candidate_pages if page["out_of_bounds_spans"]
    ]
    if out_of_bounds_pages:
        hard_failures.append(
            {"code": "TEXT_OUT_OF_BOUNDS", "pages": out_of_bounds_pages}
        )
    overlap_pages = [
        page["page"]
        for page in candidate_pages
        if page["text_block_overlaps"] or page["text_span_overlaps"]
    ]
    if overlap_pages:
        hard_failures.append(
            {"code": "TEXT_BLOCK_OVERLAP", "pages": overlap_pages}
        )
    structured_table_pages = [
        page["page"]
        for page in candidate_pages
        if page["structured_table_visual_check"]
    ]
    if structured_table_pages:
        review_flags.append(
            {
                "code": "STRUCTURED_TABLE_REQUIRES_VISUAL_CHECK",
                "pages": structured_table_pages,
                "message": (
                    "结构化表页已跳过易误报的整块文本框相交检查；"
                    "仍执行字符 span 重叠检查，并必须逐页原尺寸目视复核。"
                ),
            }
        )

    replacement_count = sum(page["replacement_chars"] for page in candidate_pages)
    if replacement_count:
        hard_failures.append(
            {"code": "REPLACEMENT_CHARACTERS", "count": replacement_count}
        )
    null_character_count = sum(
        page["null_characters"] for page in candidate_pages
    )
    if null_character_count:
        hard_failures.append(
            {"code": "NULL_CHARACTERS", "count": null_character_count}
        )
    compatibility_count = sum(
        len(page["compatibility_ideographs"]) for page in candidate_pages
    )
    if profile["disallow_compatibility_ideographs"] and compatibility_count:
        hard_failures.append(
            {"code": "COMPATIBILITY_IDEOGRAPHS", "count": compatibility_count}
        )
    placeholder_hits = [
        hit
        for page in candidate_pages
        for hit in page["placeholder_hits"]
        if (
            not hit.startswith("{{")
            or _placeholder_token(hit)
            not in expected_literal_placeholder_tokens
        )
    ]
    if placeholder_hits:
        hard_failures.append(
            {"code": "PLACEHOLDER_TEXT", "samples": placeholder_hits[:20]}
        )
    source_residuals = [
        hit for page in candidate_pages for hit in page["source_residuals"]
    ]
    if source_residuals:
        hard_failures.append(
            {
                "code": "UNACCOUNTED_SOURCE_PROSE",
                "count": len(source_residuals),
                "samples": source_residuals[:30],
            }
        )

    script_counts = character_counts(candidate_text)
    primary_script = profile.get("primary_script")
    primary_count = int(script_counts.get(primary_script, 0))
    primary_minimum = int(
        profile.get("minimum_primary_script_chars_per_document", 1)
    )
    if primary_count < primary_minimum:
        hard_failures.append(
            {
                "code": "TARGET_PRIMARY_SCRIPT_MISSING",
                "target_language": job["translation"]["target_language"],
                "primary_script": primary_script,
                "count": primary_count,
                "minimum": primary_minimum,
            }
        )
    if profile["writing_system"] == "han" and script_counts["han"] >= 100:
        target_markers = set(profile.get("variant_marker_chars", ""))
        opposite_markers = set(profile.get("opposite_variant_marker_chars", ""))
        target_variant_hits = sum(
            1 for character in candidate_text if character in target_markers
        )
        opposite_variant_hits = sum(
            1 for character in candidate_text if character in opposite_markers
        )
        minimum_variant_hits = max(3, round(script_counts["han"] * 0.002))
        if (
            target_variant_hits < minimum_variant_hits
            and opposite_variant_hits > target_variant_hits
        ):
            hard_failures.append(
                {
                    "code": "TARGET_HAN_VARIANT_MISMATCH",
                    "target_language": job["translation"]["target_language"],
                    "target_marker_hits": target_variant_hits,
                    "opposite_marker_hits": opposite_variant_hits,
                    "minimum_target_hits": minimum_variant_hits,
                }
            )

    if profile["writing_system"] == "latin":
        words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", candidate_text.lower())
        markers = set(profile.get("language_marker_words", []))
        marker_hits = sum(1 for word in words if word in markers)
        minimum_hits = max(3, round(len(words) * 0.02))
        exception_reason = quality.get("language_marker_exception_reason")
        if (
            len(words) >= 25
            and marker_hits < minimum_hits
            and not (
                isinstance(exception_reason, str) and exception_reason.strip()
            )
        ):
            hard_failures.append(
                {
                    "code": "TARGET_LANGUAGE_MARKERS_MISSING",
                    "target_language": job["translation"]["target_language"],
                    "word_count": len(words),
                    "marker_hits": marker_hits,
                    "minimum_hits": minimum_hits,
                }
            )

    font_embedding_issues = _font_embedding_issues(candidate)
    if font_embedding_issues:
        hard_failures.append(
            {"code": "FONT_NOT_EMBEDDED", "fonts": font_embedding_issues}
        )

    image_count_risk = []
    for source_page in source_pages:
        mapped_candidate_pages = candidate_pages_for_source(
            candidate_mapping,
            int(source_page["page"]),
        )
        mapped_metrics = [
            candidate_pages[candidate_page - 1]
            for candidate_page in mapped_candidate_pages
            if 1 <= candidate_page <= len(candidate_pages)
        ]
        if (
            source_page["images"] >= 1
            and sum(item["images"] for item in mapped_metrics) == 0
        ):
            image_count_risk.append(source_page["page"])
    image_rebuild_pages: set[int] = set()
    for page_number in range(1, source.page_count + 1):
        for item in overrides.get("page_overrides", []):
            if (
                isinstance(item, dict)
                and _page_selector_matches(item, page_number)
                and (
                    item.get("image_preserved_without_pdf_image") is True
                    or item.get("vector_rebuild") is True
                )
                and isinstance(item.get("reason"), str)
                and item["reason"].strip()
            ):
                image_rebuild_pages.add(page_number)
    for item in figure_inventory.get("items", []):
        if not isinstance(item, dict):
            continue
        if not _inventory_accounts_for_missing_image(item):
            continue
        page = item.get("page")
        if isinstance(page, int):
            image_rebuild_pages.add(page)
        pages = item.get("pages")
        if isinstance(pages, list):
            image_rebuild_pages.update(
                value for value in pages if isinstance(value, int)
            )
    unexplained_image_loss = [
        page for page in image_count_risk if page not in image_rebuild_pages
    ]
    if unexplained_image_loss:
        hard_failures.append(
            {"code": "SOURCE_IMAGE_MISSING", "pages": unexplained_image_loss}
        )
    if any(page["images"] for page in source_pages) or figure_inventory.get(
        "items"
    ):
        review_flags.append(
            {
                "code": "IMAGE_TEXT_REQUIRES_INVENTORY_AND_VISUAL_REVIEW",
                "message": "PDF 文本层无法证明位图内部文字已完成翻译。",
            }
        )

    decision = "BLOCKED" if hard_failures else "READY_FOR_HUMAN_REVIEW"
    report = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "automatic_decision": decision,
        "source": str(source_path),
        "source_sha256": sha256_file(source_path),
        "candidate": str(candidate_path),
        "candidate_sha256": sha256_file(candidate_path),
        "target_language": job["translation"]["target_language"],
        "quality": quality,
        "generator_typography": generator_typography,
        "hard_failures": hard_failures,
        "review_flags": review_flags,
        "source_pages": source_pages,
        "candidate_pages": candidate_pages,
    }
    output = internal_job_path(job_dir, files["qa"])
    write_json(output, report)
    return report



def run_qa(*args, **kwargs):
    """计时包装：阶段耗时进入性能基线，行为与实现完全一致。"""

    with perf_trace.stage("qa_candidate"):
        return _timed_run_qa(*args, **kwargs)

def main() -> int:
    parser = argparse.ArgumentParser(description="对学术 PDF 译制候选执行确定性 QA")
    parser.add_argument("job_dir", type=Path)
    args = parser.parse_args()
    try:
        report = run_qa(args.job_dir)
        print(f"自动结论: {report['automatic_decision']}")
        print(f"硬失败: {len(report['hard_failures'])}")
        print(f"人工风险: {len(report['review_flags'])}")
        return 0 if report["automatic_decision"] != "BLOCKED" else 2
    except SkillError as exc:
        print(f"错误: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
