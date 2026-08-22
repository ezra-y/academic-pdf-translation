from __future__ import annotations

import sys
from pathlib import Path as _Path

# 按 README 的写法直接跑时 sys.path 里没有仓库根，包就 import 不到。
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import hashlib
import html
import io
import math
import os
import re
import tempfile
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from academic_pdf_translation.planning.mode_policy import (  # noqa: E402
    FALLBACK_PRESERVE_ELEMENT_REGION,
    FALLBACK_PRESERVE_FULL_PAGE,
)
from academic_pdf_translation.render.plan_bridge import (  # noqa: E402
    FIGURE_CAPTION_KEY,
    attach_figure_captions,
    build_preservation_items,
    merge_into_complex_content,
)
from academic_pdf_translation.render.preserved_region_renderer import (  # noqa: E402
    MIN_RASTER_DPI,
    PDF_BASE_DPI,
)
from academic_pdf_translation.render.table_autobuild import (  # noqa: E402
    build_table_payload,
)
from academic_pdf_translation.render.reference_renderer import (  # noqa: E402
    build_hyphenated_forms,
    build_vocabulary,
    normalize_reference_text,
    repair_baked_line_artifacts,
)
from academic_pdf_translation.verify.render_contract import (  # noqa: E402
    derive_complex_view,
)
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    Image,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

import perf_trace  # noqa: E402
from _common import (
    SkillError,
    _complex_item_source_pages,
    complex_payload_replaced_unit_ids,
    import_fitz,
    internal_job_path,
    is_nonsemantic_source_furniture_unit,
    load_json,
    remove_suppressed_texts,
    resolve_language_profile,
    sha256_file,
    utc_now,
    write_json,
)
from candidate_analysis import open_candidate_analysis
from cjk_markup import install_reportlab_cjk_nobr_patch, reportlab_cjk_markup
from font_preparation import (
    _resolve_fonts,
    _resolve_reference_font,
    font_evidence,
    fonts_are_current,
)
from i18n import message
from renderer_identity import renderer_build_id
from reportlab_layout import make_cjk_style
from retained_source import (
    REFERENCE_CATEGORIES,
    extract_retained_regions,
    retained_region_ids,
    retained_regions_by_page,
)
from set_complex_payload import validate_complex_payload_item
from typography_fit import (
    candidate_groups,
    search_first_acceptable,
)

RENDERER_NAME = "academic-pdf-layout"
RENDERER_VERSION = "1.0"
REFERENCE_KINDS = {
    "reference",
    "references",
    "bibliography",
}
HEADING_KINDS = {"title", "subtitle", "heading", "section-heading"}
# 字体分段与行内标记已移入 academic_pdf_translation.render.font_runs。
# 它们是纯函数：给定文本与字体名产出标记字符串，不依赖排版上下文。
# 这里再导出，保持本模块内既有调用与测试的引用路径不变。
from academic_pdf_translation.render.font_runs import (  # noqa: E402,F401
    CJK_FONT_RUN_PATTERN,
    CONTROL_CHARACTER_RE,
    LIGATURE_REPLACEMENTS,
    MARKUP_TOKEN_PATTERN,
    SUPERSCRIPT_CHARACTERS,
    SUPERSCRIPT_DIGITS,
    SUPERSCRIPT_PATTERN_CLASS,
    _edge_label_lines,
    _fallback_runs,
    _font_supports,
    _markup,
    _plain_superscript,
    _unicode_superscript_characters,
)
from academic_pdf_translation.render import font_runs as _font_runs  # noqa: E402


def _register_font(name: str, path: Path) -> None:
    if name in pdfmetrics.getRegisteredFontNames():
        return
    kwargs = {"subfontIndex": 0} if path.suffix.casefold() == ".ttc" else {}
    pdfmetrics.registerFont(TTFont(name, str(path), **kwargs))


def _common_page_size(source_path: Path) -> tuple[float, float]:
    handle = open_candidate_analysis(source_path, role="source")
    counts: dict[tuple[float, float], int] = defaultdict(int)
    for page in handle.document:
        width = round(float(page.rect.width), 1)
        height = round(float(page.rect.height), 1)
        if width > height:
            width, height = height, width
        counts[(width, height)] += 1
    handle.release()
    if not counts:
        raise SkillError("原文没有页面")
    return max(counts, key=lambda value: counts[value])


# 文本分块与标题资格判定已移入 academic_pdf_translation.render.text_blocks。
# 这里按旧名再导出，本模块内的调用与既有测试引用路径不变。
from academic_pdf_translation.render.text_blocks import (  # noqa: E402,F401
    looks_like_heading as _looks_like_heading,
)
from academic_pdf_translation.render.text_blocks import (  # noqa: E402,F401
    role_may_head as _role_may_head,
)
from academic_pdf_translation.render.text_blocks import (  # noqa: E402,F401
    split_blocks as _split_blocks,
)
from academic_pdf_translation.render.text_blocks import (  # noqa: E402,F401
    unit_text_blocks as _unit_text_blocks,
)

@dataclass(frozen=True)
class MappingEvent:
    phase: str
    object_type: str
    object_id: str
    source_page: int
    candidate_page: int
    candidate_y: float


class MappingTracker:
    def __init__(self) -> None:
        self.events: list[MappingEvent] = []
        self.orphan_regions: list[dict[str, Any]] = []
        self._pending_heading: dict[str, Any] | None = None

    def record(
        self,
        anchor: "MappingAnchor",
        candidate_page: int,
        candidate_y: float,
    ) -> None:
        self.events.append(
            MappingEvent(
                phase=anchor.phase,
                object_type=anchor.object_type,
                object_id=anchor.object_id,
                source_page=anchor.source_page,
                candidate_page=candidate_page,
                candidate_y=candidate_y,
            )
        )

    def note_heading(
        self,
        *,
        candidate_page: int,
        text: str,
        unit_id: str | None,
    ) -> None:
        self.resolve_heading(candidate_page)
        self._pending_heading = {
            "candidate_page": candidate_page,
            "unit_id": unit_id,
            "text": text[:160],
        }

    def resolve_heading(self, content_page: int) -> None:
        pending = self._pending_heading
        if pending is None:
            return
        if int(pending["candidate_page"]) != int(content_page):
            self.orphan_regions.append(
                {
                    **pending,
                    "next_content_page": int(content_page),
                    "reason": "heading-separated-from-following-content",
                }
            )
        self._pending_heading = None

    def finalize_heading_check(self) -> None:
        pending = self._pending_heading
        if pending is not None:
            self.orphan_regions.append(
                {
                    **pending,
                    "next_content_page": None,
                    "reason": "heading-without-following-content",
                }
            )
            self._pending_heading = None

    def _ranges(self, object_type: str) -> dict[str, tuple[int, int, int]]:
        starts: dict[str, tuple[int, int]] = {}
        ends: dict[str, int] = {}
        for event in self.events:
            if event.object_type != object_type:
                continue
            if event.phase == "start":
                starts.setdefault(
                    event.object_id,
                    (event.source_page, event.candidate_page),
                )
            elif event.phase == "end":
                ends[event.object_id] = event.candidate_page
        result: dict[str, tuple[int, int, int]] = {}
        for object_id, (source_page, start) in starts.items():
            end = max(start, ends.get(object_id, start))
            result[object_id] = (source_page, start, end)
        return result

    def build_map(
        self,
        *,
        job: dict[str, Any],
        translation: dict[str, Any],
        retained_payloads: list[dict[str, Any]],
        unit_layout_roles: dict[str, str],
        page_size: tuple[float, float],
        margins: tuple[float, float, float, float],
        candidate_page_count: int,
        candidate_sha256: str,
    ) -> dict[str, Any]:
        source_ranges = self._ranges("source-page")
        unit_ranges = self._ranges("unit")
        complex_ranges = self._ranges("complex")
        retained_ranges = self._ranges("retained")
        retained_payload_by_id = {
            str(payload["id"]): payload
            for payload in retained_payloads
            if isinstance(payload, dict) and str(payload.get("id") or "")
        }
        source_page_count = int(job["source"]["page_count"])
        units_by_source: dict[int, list[str]] = defaultdict(list)
        for unit in translation.get("units", []):
            if isinstance(unit, dict) and isinstance(unit.get("page"), int):
                units_by_source[int(unit["page"])].append(str(unit["id"]))
        complex_by_source: dict[int, list[str]] = defaultdict(list)
        for object_id, (source_page, _, _) in complex_ranges.items():
            complex_by_source[source_page].append(object_id)
        retained_by_source: dict[int, list[str]] = defaultdict(list)
        for object_id, (source_page, _, _) in retained_ranges.items():
            retained_by_source[source_page].append(object_id)

        source_entries: list[dict[str, Any]] = []
        reverse: dict[int, set[int]] = defaultdict(set)
        for source_page in range(1, source_page_count + 1):
            key = f"source-page-{source_page:04d}"
            if key not in source_ranges:
                raise SkillError(f"统一生成器未记录源页 {source_page}")
            _, start, end = source_ranges[key]
            pages = list(range(start, end + 1))
            for candidate_page in pages:
                reverse[candidate_page].add(source_page)
            source_entries.append(
                {
                    "source_page": source_page,
                    "candidate_pages": pages,
                    "unit_ids": units_by_source.get(source_page, []),
                    "complex_item_ids": sorted(
                        complex_by_source.get(source_page, [])
                    ),
                    "retained_region_ids": sorted(
                        retained_by_source.get(source_page, [])
                    ),
                }
            )
        unit_entries = []
        for unit_id, (source_page, start, end) in unit_ranges.items():
            unit_entries.append(
                {
                    "unit_id": unit_id,
                    "source_page": source_page,
                    "candidate_pages": list(range(start, end + 1)),
                    "layout_role": unit_layout_roles.get(unit_id),
                }
            )
        complex_entries = [
            {
                "complex_item_id": object_id,
                "source_page": source_page,
                "candidate_pages": list(range(start, end + 1)),
            }
            for object_id, (source_page, start, end) in complex_ranges.items()
        ]
        retained_entries = []
        page_width, page_height = page_size
        left, right, top, bottom = margins
        for object_id, (source_page, start, end) in retained_ranges.items():
            start_event = next(
                (
                    event
                    for event in self.events
                    if event.object_type == "retained"
                    and event.object_id == object_id
                    and event.phase == "start"
                ),
                None,
            )
            end_event = next(
                (
                    event
                    for event in reversed(self.events)
                    if event.object_type == "retained"
                    and event.object_id == object_id
                    and event.phase == "end"
                ),
                None,
            )
            candidate_regions = []
            if start_event is not None and end_event is not None:
                for candidate_page in range(start, end + 1):
                    region_top = (
                        max(top, page_height - start_event.candidate_y)
                        if candidate_page == start
                        else top
                    )
                    region_bottom = (
                        min(
                            page_height - bottom,
                            page_height - end_event.candidate_y,
                        )
                        if candidate_page == end
                        else page_height - bottom
                    )
                    if region_bottom > region_top + 0.5:
                        candidate_regions.append(
                            {
                                "candidate_page": candidate_page,
                                "bbox": [
                                    round(left, 3),
                                    round(region_top, 3),
                                    round(page_width - right, 3),
                                    round(region_bottom, 3),
                                ],
                            }
                        )
            payload = retained_payload_by_id.get(object_id, {})
            retained_entries.append(
                {
                    "retained_region_id": object_id,
                    "source_page": source_page,
                    "category": str(payload.get("category") or ""),
                    "candidate_pages": list(range(start, end + 1)),
                    "candidate_regions": candidate_regions,
                }
            )
        candidate_entries = [
            {
                "candidate_page": candidate_page,
                "source_pages": sorted(reverse.get(candidate_page, set())),
                "unit_ids": sorted(
                    entry["unit_id"]
                    for entry in unit_entries
                    if candidate_page in entry["candidate_pages"]
                ),
                "unit_layout_roles": {
                    entry["unit_id"]: entry["layout_role"]
                    for entry in unit_entries
                    if (
                        candidate_page in entry["candidate_pages"]
                        and entry.get("layout_role")
                    )
                },
                "complex_item_ids": sorted(
                    entry["complex_item_id"]
                    for entry in complex_entries
                    if candidate_page in entry["candidate_pages"]
                ),
                "retained_region_ids": sorted(
                    entry["retained_region_id"]
                    for entry in retained_entries
                    if candidate_page in entry["candidate_pages"]
                ),
                "retained_regions": [
                    {
                        "retained_region_id": entry["retained_region_id"],
                        "category": entry["category"],
                        "bbox": region["bbox"],
                    }
                    for entry in retained_entries
                    for region in entry.get("candidate_regions", [])
                    if region["candidate_page"] == candidate_page
                ],
            }
            for candidate_page in range(1, candidate_page_count + 1)
        ]
        return {
            "schema_version": "1.0",
            "generated_at": utc_now(),
            "mapping_mode": "flow-unit-anchors-v1",
            "layout_policy": "continuous-reading",
            "complete": True,
            "source_sha256": job["source"]["sha256"],
            "translation_sha256": None,
            "candidate_sha256": candidate_sha256,
            "source_page_count": source_page_count,
            "candidate_page_count": candidate_page_count,
            "source_pages": source_entries,
            "candidate_pages": candidate_entries,
            "units": sorted(unit_entries, key=lambda item: item["unit_id"]),
            "complex_items": sorted(
                complex_entries,
                key=lambda item: item["complex_item_id"],
            ),
            "retained_regions": sorted(
                retained_entries,
                key=lambda item: item["retained_region_id"],
            ),
        }


class MappingAnchor(Flowable):
    def __init__(
        self,
        phase: str,
        object_type: str,
        object_id: str,
        source_page: int,
        *,
        keep_with_next: bool = False,
    ) -> None:
        super().__init__()
        self.phase = phase
        self.object_type = object_type
        self.object_id = object_id
        self.source_page = source_page
        self.keepWithNext = phase == "start" or keep_with_next
        self.width = 0
        self.height = 0.01

    def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
        return 0, self.height

    def draw(self) -> None:
        return None


class MappingDocTemplate(BaseDocTemplate):
    def __init__(
        self,
        filename: str,
        *,
        tracker: MappingTracker,
        page_size: tuple[float, float],
        margins: tuple[float, float, float, float],
        regular_font: str,
        title: str,
        target_language: str,
    ) -> None:
        left, right, top, bottom = margins
        super().__init__(
            filename,
            pagesize=page_size,
            leftMargin=left,
            rightMargin=right,
            topMargin=top,
            bottomMargin=bottom,
            title=title,
            author="",
            subject=message(target_language, "pdf_subject"),
        )
        self.tracker = tracker
        self.regular_font = regular_font
        self.target_language = target_language
        self._active_unit_id: str | None = None
        width, height = page_size
        frame = Frame(
            left,
            bottom,
            width - left - right,
            height - top - bottom,
            id="body",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        self.addPageTemplates(
            [
                PageTemplate(
                    id="continuous-reading",
                    frames=[frame],
                    onPage=self._draw_page_furniture,
                )
            ]
        )

    def _draw_page_furniture(self, canvas, document) -> None:
        width, height = self.pagesize
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#D5D9DE"))
        canvas.setLineWidth(0.4)
        canvas.line(self.leftMargin, height - 24, width - self.rightMargin, height - 24)
        canvas.setFillColor(colors.HexColor("#6B747D"))
        canvas.setFont(self.regular_font, 7.2)
        canvas.drawString(
            self.leftMargin,
            height - 18,
            message(self.target_language, "reading_version"),
        )
        canvas.drawRightString(
            width - self.rightMargin,
            17,
            str(document.page),
        )
        canvas.restoreState()

    def afterFlowable(self, flowable: Flowable) -> None:
        if isinstance(flowable, MappingAnchor):
            frame = getattr(self, "frame", None)
            candidate_y = float(getattr(frame, "_y", 0.0))
            self.tracker.record(
                flowable,
                int(self.page),
                candidate_y,
            )
            if flowable.phase == "start":
                if flowable.object_type == "unit":
                    pending = self.tracker._pending_heading
                    if (
                        pending is not None
                        and pending.get("unit_id") != flowable.object_id
                    ):
                        self.tracker.resolve_heading(int(self.page))
                    self._active_unit_id = flowable.object_id
                elif flowable.object_type in {"complex", "retained"}:
                    self.tracker.resolve_heading(int(self.page))
            elif (
                flowable.phase == "end"
                and flowable.object_type == "unit"
                and self._active_unit_id == flowable.object_id
            ):
                self._active_unit_id = None
            return
        if isinstance(flowable, Paragraph):
            style_name = str(getattr(flowable.style, "name", "")).lower()
            if style_name in {"h1", "h2", "title"}:
                self.tracker.note_heading(
                    candidate_page=int(self.page),
                    text=flowable.getPlainText(),
                    unit_id=self._active_unit_id,
                )
            elif style_name != "source-anchor":
                self.tracker.resolve_heading(int(self.page))
            return
        if isinstance(flowable, (Table, Image, VectorPayloadFlowable)):
            self.tracker.resolve_heading(int(self.page))


# 矢量图 Flowable 已移入 academic_pdf_translation.render.flowables，
# 这里再导出保持调用路径不变。
from academic_pdf_translation.render.flowables import (  # noqa: E402,F401
    VectorFigureError,
    VectorPayloadFlowable,
)


def _styles(
    *,
    regular_font: str,
    bold_font: str,
    reference_font: str,
    body_font_pt: float,
    leading_ratio: float,
    reference_font_pt: float,
) -> dict[str, ParagraphStyle]:
    # 正文基样式由通用流排层定义，生成器只补上自己的正文颜色。
    # 这样"CJK 段落样式"在全项目只有一处定义。
    body = make_cjk_style(
        "body",
        font_name=regular_font,
        font_size=body_font_pt,
        leading_ratio=leading_ratio,
        alignment=TA_LEFT,
        first_line_indent_em=2,
        space_after_em=0.62,
    )
    body.textColor = colors.HexColor("#1A2025")
    return {
        "body": body,
        "body_no_indent": ParagraphStyle(
            "body-no-indent",
            parent=body,
            firstLineIndent=0,
        ),
        "metadata": ParagraphStyle(
            "metadata",
            parent=body,
            firstLineIndent=0,
            leading=body_font_pt * 1.5,
            spaceAfter=body_font_pt * 0.35,
        ),
        "h1": ParagraphStyle(
            "h1",
            parent=body,
            fontName=bold_font,
            fontSize=body_font_pt * 1.42,
            leading=body_font_pt * leading_ratio * 1.18,
            firstLineIndent=0,
            spaceBefore=body_font_pt * 0.55,
            spaceAfter=body_font_pt * 0.72,
            keepWithNext=True,
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=body,
            fontName=bold_font,
            fontSize=body_font_pt * 1.22,
            leading=body_font_pt * leading_ratio * 1.08,
            firstLineIndent=0,
            spaceBefore=body_font_pt * 0.48,
            spaceAfter=body_font_pt * 0.58,
            keepWithNext=True,
        ),
        "title": ParagraphStyle(
            "title",
            parent=body,
            fontName=bold_font,
            fontSize=body_font_pt * 1.72,
            leading=body_font_pt * leading_ratio * 1.35,
            alignment=TA_CENTER,
            firstLineIndent=0,
            spaceBefore=body_font_pt * 0.8,
            spaceAfter=body_font_pt * 1.0,
            keepWithNext=True,
        ),
        "reference": ParagraphStyle(
            "reference",
            parent=body,
            fontName=reference_font,
            fontSize=reference_font_pt,
            leading=reference_font_pt * 1.5,
            leftIndent=reference_font_pt * 1.5,
            firstLineIndent=-reference_font_pt * 1.5,
            spaceAfter=reference_font_pt * 0.42,
        ),
        "source_anchor": ParagraphStyle(
            "source-anchor",
            fontName=regular_font,
            fontSize=7.4,
            leading=9.2,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#68737B"),
            spaceBefore=4,
            spaceAfter=5,
            keepWithNext=True,
            wordWrap="CJK",
        ),
        "caption": ParagraphStyle(
            "caption",
            parent=body,
            fontSize=max(8.0, body_font_pt * 0.82),
            leading=max(11.5, body_font_pt * 1.18),
            firstLineIndent=0,
            alignment=TA_CENTER,
            spaceAfter=body_font_pt * 0.55,
            keepWithNext=True,
        ),
        "table": ParagraphStyle(
            "table",
            parent=body,
            fontSize=max(8.2, body_font_pt * 0.82),
            leading=max(11.0, body_font_pt * 1.1),
            firstLineIndent=0,
            alignment=TA_LEFT,
            spaceAfter=0,
        ),
        "table_header": ParagraphStyle(
            "table-header",
            parent=body,
            fontName=bold_font,
            fontSize=max(8.2, body_font_pt * 0.82),
            leading=max(11.0, body_font_pt * 1.1),
            firstLineIndent=0,
            alignment=TA_CENTER,
            spaceAfter=0,
        ),
        "table_note": ParagraphStyle(
            "table-note",
            parent=body,
            fontSize=max(8.2, body_font_pt * 0.82),
            leading=max(11.8, body_font_pt * 1.18),
            firstLineIndent=0,
            alignment=TA_LEFT,
            spaceBefore=body_font_pt * 0.35,
            spaceAfter=body_font_pt * 0.25,
        ),
    }


def _unit_flowables(
    unit: dict[str, Any],
    styles: dict[str, ParagraphStyle],
    suppress_texts: Iterable[str] = (),
    *,
    text_override: str | None = None,
    kind_override: str | None = None,
    include_start: bool = True,
    include_end: bool = True,
) -> list[Flowable]:
    source_page = int(unit["page"])
    unit_id = str(unit["id"])
    text = remove_suppressed_texts(
        (
            str(text_override).strip()
            if text_override is not None
            else str(
                unit.get("translation")
                or unit.get("source")
                or ""
            ).strip()
        ),
        suppress_texts,
    )
    if unit.get("_suppressed_reason"):
        # 结构化抑制：单元照常入映射（锚点还在），但不排任何文字。
        # 名单与理由记录在 generator-layout-log.json 的 suppressed_units。
        text = ""
    elif unit.get("_decoded_math") and text_override is None:
        text = str(unit["_decoded_math"])
    result: list[Flowable] = []
    keep_end_with_next = False
    if include_start:
        result.append(MappingAnchor("start", "unit", unit_id, source_page))
    blocks = _unit_text_blocks(unit, text)
    kind = str(kind_override or unit.get("kind") or "").lower()
    layout_role = str(unit.get("_layout_role") or "").lower()
    for index, block in enumerate(blocks):
        if unit.get("_element_role") == "footnote":
            # 脚注不是正文：字号小一档、无缩进。分隔线由页面单元的
            # 首个脚注前插入（见 _unit_flowables 调用侧）。
            style = styles["table_note"]
        elif layout_role in {
            "publication-metadata",
            "formal-citation-footer",
        }:
            style = styles["metadata"]
        elif kind in REFERENCE_KINDS or unit.get("keep_source_reason"):
            style = styles["reference"]
        elif (
            kind == "title"
            and index == 0
            and _role_may_head(unit)
        ):
            style = styles["title"]
        elif (
            kind in HEADING_KINDS or unit.get("heading_level") == 1
        ) and _role_may_head(unit):
            style = styles["h1"]
        elif unit.get("heading_level") == 2 and _role_may_head(unit):
            style = styles["h2"]
        elif (
            kind in {"", "text", "unknown"}
            and _looks_like_heading(block)
            and unit.get("_element_role")
            in (None, "", "heading", "document-title")
        ):
            # 绑定角色已知且不是标题的，启发式没有提升权：
            # 作者单位、arXiv 版本戳、图内标签长得再像标题也不是标题。
            style = styles["h2"]
        elif block.startswith(("图", "表", "注", "DOI", "doi")):
            style = styles["body_no_indent"]
        else:
            style = styles["body"]
        result.append(
            Paragraph(
                _markup(
                    block,
                    cjk_font=(
                        styles["body"].fontName
                        if style is styles["reference"]
                        else None
                    ),
                    primary_font=(
                        style.fontName
                        if style is styles["reference"]
                        else None
                    ),
                ),
                style,
            )
        )
        keep_end_with_next = bool(
            getattr(style, "keepWithNext", False)
        )
    if include_end:
        result.append(
            MappingAnchor(
                "end",
                "unit",
                unit_id,
                source_page,
                keep_with_next=keep_end_with_next,
            )
        )
    return result


def _line_fragment_bbox(
    unit: dict[str, Any],
) -> tuple[float, float, float, float] | None:
    bbox = unit.get("source_bbox")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or not all(isinstance(value, (int, float)) for value in bbox)
    ):
        return None
    x0, y0, x1, y1 = map(float, bbox)
    if x1 <= x0 or y1 <= y0 or y1 - y0 > 38.0:
        return None
    return x0, y0, x1, y1


def _line_fragment_role(unit: dict[str, Any]) -> str | None:
    if (
        not str(unit.get("translation") or "").strip()
        or str(unit.get("keep_source_reason") or "").strip()
        or "\n" in str(unit.get("source") or "").strip()
        or _line_fragment_bbox(unit) is None
    ):
        return None
    kind = str(unit.get("kind") or "").lower()
    if kind in HEADING_KINDS:
        return "heading"
    if kind == "body":
        return "body"
    return None


def _source_ends_paragraph(text: str) -> bool:
    compact = re.sub(
        rf"(?:\s*(?:\d{{1,3}}|[{SUPERSCRIPT_PATTERN_CLASS}]+))+$",
        "",
        str(text or "").rstrip(),
    )
    return bool(re.search(r'[.!?。！？][”’"\']?$', compact))


def _starts_with_latin_upper(text: str) -> bool:
    match = re.search(r"[A-Za-z]", str(text or ""))
    return bool(match and match.group(0).isupper())


def _unit_bbox(
    unit: dict[str, Any],
) -> tuple[float, float, float, float] | None:
    bbox = unit.get("source_bbox")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or not all(isinstance(value, (int, float)) for value in bbox)
    ):
        return None
    x0, y0, x1, y1 = map(float, bbox)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _is_bottom_note_unit(
    unit: dict[str, Any],
    *,
    page_height: float,
) -> bool:
    bbox = _unit_bbox(unit)
    if bbox is None:
        return False
    source = str(unit.get("source") or "").strip()
    return bool(
        bbox[1] >= page_height * 0.72
        and re.match(r"^(?:\d{1,3}|[*†‡])(?:\s|https?://)", source)
    )


def _is_cross_page_continuation(
    previous: dict[str, Any],
    following: dict[str, Any],
    *,
    previous_page_width: float,
    previous_page_height: float,
    following_page_width: float,
    following_page_height: float,
) -> bool:
    if (
        str(previous.get("kind") or "").lower() != "body"
        or str(following.get("kind") or "").lower() != "body"
        or int(following.get("page") or 0)
        != int(previous.get("page") or 0) + 1
    ):
        return False
    previous_bbox = _unit_bbox(previous)
    following_bbox = _unit_bbox(following)
    if previous_bbox is None or following_bbox is None:
        return False
    px0, _py0, px1, py1 = previous_bbox
    fx0, fy0, fx1, _fy1 = following_bbox
    if (
        py1 < previous_page_height * 0.68
        or fy0 > following_page_height * 0.25
    ):
        return False
    overlap = min(px1, fx1) - max(px0, fx0)
    minimum_width = min(px1 - px0, fx1 - fx0)
    same_column = (
        overlap >= minimum_width * 0.45
        and abs(px0 - fx0) <= max(
            28.0,
            min(previous_page_width, following_page_width) * 0.06,
        )
    )
    column_wrap = (
        px0 >= previous_page_width * 0.45
        and fx0 <= following_page_width * 0.25
        and abs((px1 - px0) - (fx1 - fx0))
        <= max(
            28.0,
            min(px1 - px0, fx1 - fx0) * 0.18,
        )
    )
    if not same_column and not column_wrap:
        return False
    if not column_wrap and abs(px0 - fx0) > max(
        28.0,
        min(previous_page_width, following_page_width) * 0.06,
    ):
        return False
    previous_source = str(previous.get("source") or "").strip()
    following_source = str(following.get("source") or "").strip()
    previous_target = str(previous.get("translation") or "").strip()
    following_target = str(following.get("translation") or "").strip()
    if (
        len(previous_source) < 12
        or len(following_source) < 4
        or len(previous_target) < 4
        or len(following_target) < 2
        or _source_ends_paragraph(previous_source)
        or _source_ends_paragraph(previous_target)
    ):
        return False
    return True


def _should_join_line_fragment(
    run: list[dict[str, Any]],
    next_unit: dict[str, Any],
    *,
    page_width: float,
) -> bool:
    if not run or len(run) >= 24:
        return False
    current = run[-1]
    if current.get("page") != next_unit.get("page"):
        return False
    role = _line_fragment_role(current)
    if role is None or role != _line_fragment_role(next_unit):
        return False
    current_bbox = _line_fragment_bbox(current)
    next_bbox = _line_fragment_bbox(next_unit)
    if current_bbox is None or next_bbox is None:
        return False
    cx0, cy0, cx1, cy1 = current_bbox
    nx0, ny0, nx1, ny1 = next_bbox
    current_height = cy1 - cy0
    next_height = ny1 - ny0
    line_step = ny0 - cy0
    if (
        line_step <= max(current_height, next_height) * 0.55
        or line_step
        > max(34.0, max(current_height, next_height) * 2.25)
    ):
        return False
    horizontal_overlap = min(cx1, nx1) - max(cx0, nx0)
    if horizontal_overlap < min(cx1 - cx0, nx1 - nx0) * 0.2:
        return False
    current_source = str(current.get("source") or "").strip()
    next_source = str(next_unit.get("source") or "").strip()
    if role == "heading":
        return bool(
            len(run) < 3
            and current.get("heading_level")
            == next_unit.get("heading_level")
            and not _source_ends_paragraph(current_source)
        )

    current_width = cx1 - cx0
    next_width = nx1 - nx0
    if current_width < max(120.0, page_width * 0.42):
        return False
    group_left = min(
        float(_line_fragment_bbox(unit)[0])
        for unit in run
        if _line_fragment_bbox(unit) is not None
    )
    if (
        _source_ends_paragraph(current_source)
        and nx0 - group_left > max(16.0, page_width * 0.03)
    ):
        return False
    if (
        _source_ends_paragraph(current_source)
        and next_source.startswith(("(", "[", "（", "【"))
    ):
        return False
    if (
        _source_ends_paragraph(current_source)
        and next_width < page_width * 0.3
        and _starts_with_latin_upper(next_source)
    ):
        return False
    return True


def _join_target_fragments(
    values: Iterable[str],
    *,
    target_language: str,
) -> str:
    fragments = [str(value).strip() for value in values if str(value).strip()]
    if not fragments:
        return ""
    cjk_target = target_language.startswith(("zh", "ja", "ko"))
    result = fragments[0]
    for fragment in fragments[1:]:
        separator = ""
        if not cjk_target:
            separator = " "
        elif (
            re.search(r"[A-Za-z0-9&,;:]$", result)
            and re.match(r"[A-Za-z0-9]", fragment)
        ):
            separator = " "
        result += separator + fragment
    return result


def _joined_unit_flowables(
    units: list[dict[str, Any]],
    styles: dict[str, ParagraphStyle],
    suppress_texts: Iterable[str],
    *,
    target_language: str,
) -> list[Flowable]:
    if not units:
        return []
    result: list[Flowable] = [
        MappingAnchor(
            "start",
            "unit",
            str(unit["id"]),
            int(unit["page"]),
        )
        for unit in units
    ]
    combined_text = _join_target_fragments(
        (
            remove_suppressed_texts(
                str(unit.get("translation") or unit.get("source") or ""),
                suppress_texts,
            )
            # 结构化抑制的单元（数学字体残渣、标题续行重复）不进合并文本；
            # 解码过的数学字形用解码结果。锚点都发过，映射不受影响。
            if not unit.get("_suppressed_reason")
            and not unit.get("_decoded_math")
            else str(unit.get("_decoded_math") or "")
            for unit in units
        ),
        target_language=target_language,
    )
    content_flowables = _unit_flowables(
        units[0],
        styles,
        text_override=combined_text,
        include_start=False,
        include_end=False,
    )
    result.extend(content_flowables)
    keep_end_with_next = bool(
        content_flowables
        and callable(
            getattr(content_flowables[-1], "getKeepWithNext", None)
        )
        and content_flowables[-1].getKeepWithNext()
    )
    result.extend(
        MappingAnchor(
            "end",
            "unit",
            str(unit["id"]),
            int(unit["page"]),
            keep_with_next=keep_end_with_next,
        )
        for unit in units
    )
    return result


def _unit_fully_covered_by_retained(
    unit: dict[str, Any],
    retained_payloads: Iterable[dict[str, Any]],
    *,
    tolerance: float = 2.0,
) -> bool:
    if not str(unit.get("keep_source_reason") or "").strip():
        return False
    source_bbox = unit.get("source_bbox")
    if (
        not isinstance(source_bbox, list)
        or len(source_bbox) != 4
        or not all(isinstance(value, (int, float)) for value in source_bbox)
    ):
        return False
    x0, y0, x1, y1 = map(float, source_bbox)
    for payload in retained_payloads:
        if (
            not isinstance(payload, dict)
            or payload.get("already_present_in_translation") is True
            or not payload.get("blocks")
        ):
            continue
        bbox = payload.get("effective_bbox") or payload.get("bbox")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or not all(isinstance(value, (int, float)) for value in bbox)
        ):
            continue
        rx0, ry0, rx1, ry1 = map(float, bbox)
        if (
            x0 >= rx0 - tolerance
            and y0 >= ry0 - tolerance
            and x1 <= rx1 + tolerance
            and y1 <= ry1 + tolerance
        ):
            return True
    return False


# 参考文献的数据判定已移入 academic_pdf_translation.render.reference_data。
from academic_pdf_translation.render.reference_data import (  # noqa: E402,F401
    _is_reference_heading_unit,
    _reference_font_size,
    _reference_unit_parts,
)

def _retained_heading_label(target_language: str) -> str:
    return message(target_language, "retained_references")


def _retained_flowables(
    payload: dict[str, Any],
    *,
    styles: dict[str, ParagraphStyle],
    target_language: str,
    include_reference_heading: bool,
    reference_text_transform: Any = None,
) -> list[Flowable]:
    if payload.get("already_present_in_translation") is True:
        return []
    result: list[Flowable] = []
    category = str(payload.get("category") or "")
    has_source_heading = any(
        isinstance(block, dict) and block.get("role") == "heading"
        for block in payload.get("blocks", [])
    )
    if (
        category in REFERENCE_CATEGORIES
        and include_reference_heading
        and not has_source_heading
    ):
        result.append(
            Paragraph(
                _markup(_retained_heading_label(target_language)),
                styles["h2"],
            )
        )
    for block in payload.get("blocks", []):
        if not isinstance(block, dict):
            continue
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        if (
            reference_text_transform is not None
            and category in REFERENCE_CATEGORIES
        ):
            # 参考文献保留的是原文，但行末软断词（net-\nworks）和被折行
            # 切断的 URL 是排版伪影，不是内容——按文档词表拼回去。
            # 必须从 raw_text（带换行）判，清理后的文本已把伪影固化。
            text = reference_text_transform(
                str(block.get("raw_text") or "") or text
            )
            if not text:
                continue
        if block.get("role") == "heading" and category in REFERENCE_CATEGORIES:
            if include_reference_heading:
                result.append(
                    Paragraph(
                        _markup(_retained_heading_label(target_language)),
                        styles["h2"],
                    )
                )
        else:
            result.append(
                Paragraph(
                    _markup(
                        text,
                        cjk_font=(
                            styles["body"].fontName
                            if category in REFERENCE_CATEGORIES
                            else None
                        ),
                        primary_font=(
                            styles["reference"].fontName
                            if category in REFERENCE_CATEGORIES
                            else None
                        ),
                    ),
                    (
                        styles["reference"]
                        if category in REFERENCE_CATEGORIES
                        else styles["body_no_indent"]
                    ),
                )
            )
    return result


def _retained_render_policy(
    payload: dict[str, Any],
    page_height: float,
) -> str:
    explicit = payload.get("render_policy")
    if explicit in {"insert-before", "insert-after"}:
        return str(explicit)
    if str(payload.get("category") or "") in REFERENCE_CATEGORIES:
        return "insert-after"
    bbox = payload.get("effective_bbox") or payload.get("bbox") or [0, 0, 0, 0]
    return "insert-before" if float(bbox[1]) < page_height * 0.35 else "insert-after"


def _retained_references_precede_visible_units(
    page_units: list[dict[str, Any]],
    page_retained: list[dict[str, Any]],
    page_complex: list[dict[str, Any]],
    *,
    page_width: float | None = None,
    page_height: float | None = None,
    tolerance: float = 2.0,
) -> bool:
    references = [
        payload
        for payload in page_retained
        if (
            isinstance(payload, dict)
            and str(payload.get("category") or "") in REFERENCE_CATEGORIES
            and payload.get("blocks")
        )
    ]
    if not references:
        return False

    complex_replaced_unit_ids = complex_payload_replaced_unit_ids(
        page_units,
        page_complex,
    )
    visible_units: list[dict[str, Any]] = []
    for unit in page_units:
        if (
            not isinstance(unit, dict)
            or not str(unit.get("translation") or "").strip()
            or _unit_fully_covered_by_retained(unit, page_retained)
            or is_nonsemantic_source_furniture_unit(
                unit,
                page_width=page_width,
                page_height=page_height,
            )
            or str(unit.get("id") or "") in complex_replaced_unit_ids
        ):
            continue
        bbox = unit.get("source_bbox")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or not all(isinstance(value, (int, float)) for value in bbox)
        ):
            return False
        visible_units.append(unit)
    if not visible_units:
        return False

    for unit in visible_units:
        ux0, uy0, ux1, _ = map(float, unit["source_bbox"])
        overlapping_references = []
        for payload in references:
            bbox = payload.get("bbox")
            if (
                not isinstance(bbox, list)
                or len(bbox) != 4
                or not all(isinstance(value, (int, float)) for value in bbox)
            ):
                continue
            rx0, _, rx1, _ = map(float, bbox)
            if min(ux1, rx1) - max(ux0, rx0) > tolerance:
                overlapping_references.append(payload)
        if not overlapping_references:
            return False
        if not any(
            uy0 >= float(payload["bbox"][3]) - tolerance
            for payload in overlapping_references
        ):
            return False
    return True


# 表格数据整形已移入 academic_pdf_translation.render.table_data：
# 它们只吃载荷、吐数据结构，不创建 Flowable。这里再导出保持调用路径不变。
from academic_pdf_translation.render.table_data import (  # noqa: E402,F401
    TableDataError,
    _cell_text,
    _column_widths,
    _table_emphasis_rows,
    _table_header_spans,
    _table_matrix,
    _table_note_text,
)


def _table_matrix_or_skill_error(
    table: dict[str, Any],
) -> tuple[list[list[str]], list[tuple]]:
    """包层的 TableDataError 翻译回脚本层的 SkillError，对外行为不变。"""

    try:
        return _table_matrix(table)
    except TableDataError as exc:
        raise SkillError(str(exc)) from exc


def _table_flowables(
    item: dict[str, Any],
    *,
    styles: dict[str, ParagraphStyle],
    available_width: float,
) -> list[Flowable]:
    result: list[Flowable] = []
    for table_index, table in enumerate(
        item.get("payload", {}).get("tables", [])
    ):
        if not isinstance(table, dict):
            continue
        title = str(
            table.get("translated_title")
            or table.get("title_translation")
            or table.get("title")
            or table.get("caption")
            or ""
        ).strip()
        if title:
            result.append(Paragraph(_markup(title), styles["caption"]))
        matrix, spans = _table_matrix_or_skill_error(table)
        header_rows = min(
            max(int(table.get("header_rows") or 1), 1),
            len(matrix),
        )
        emphasis_rows = _table_emphasis_rows(
            table,
            row_count=len(matrix),
            header_rows=header_rows,
        )
        table_font_size = styles["table"].fontSize
        configured_font_size = table.get("font_size_pt")
        if isinstance(configured_font_size, (int, float)):
            table_font_size = min(
                table_font_size,
                max(6.0, float(configured_font_size)),
            )
        table_style = ParagraphStyle(
            f"table-{table_index}",
            parent=styles["table"],
            fontSize=table_font_size,
            leading=max(table_font_size * 1.24, table_font_size + 1.6),
        )
        table_header_style = ParagraphStyle(
            f"table-header-{table_index}",
            parent=styles["table_header"],
            fontSize=table_font_size,
            leading=max(table_font_size * 1.24, table_font_size + 1.6),
        )
        bold_cells = table.get("bold_cells")
        bold_font_name = styles["table_header"].fontName

        def _cell_paragraph(row_index: int, col_index: int, cell: str):
            markup = _markup(cell)
            if (
                isinstance(bold_cells, list)
                and row_index < len(bold_cells)
                and isinstance(bold_cells[row_index], list)
                and col_index < len(bold_cells[row_index])
                and bold_cells[row_index][col_index]
            ):
                # 原表用粗体标各列最优值，这是语义不是装饰，必须跟到中文表里。
                markup = (
                    f'<font name="{html.escape(bold_font_name, quote=True)}">'
                    f"{markup}</font>"
                )
            return Paragraph(
                markup,
                (
                    table_header_style
                    if row_index < header_rows
                    or row_index in emphasis_rows
                    else table_style
                ),
            )

        data = [
            [
                _cell_paragraph(row_index, col_index, cell)
                for col_index, cell in enumerate(row)
            ]
            for row_index, row in enumerate(matrix)
        ]
        cell_padding = table.get("cell_padding_pt")
        cell_padding_pt = (
            min(6.0, max(1.5, float(cell_padding)))
            if isinstance(cell_padding, (int, float))
            else 4.0
        )
        table_flowable = Table(
            data,
            colWidths=_column_widths(
                matrix,
                available_width,
                table.get("column_width_weights"),
            ),
            repeatRows=header_rows,
            hAlign="CENTER",
            splitByRow=1,
        )
        commands: list[tuple] = [
            ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#7A858D")),
            (
                "BACKGROUND",
                (0, 0),
                (-1, header_rows - 1),
                colors.HexColor("#E9EFF1"),
            ),
            (
                "FONTNAME",
                (0, 0),
                (-1, -1),
                table_style.fontName,
            ),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), cell_padding_pt),
            ("RIGHTPADDING", (0, 0), (-1, -1), cell_padding_pt),
            ("TOPPADDING", (0, 0), (-1, -1), cell_padding_pt),
            ("BOTTOMPADDING", (0, 0), (-1, -1), cell_padding_pt),
        ]
        for row_index in sorted(emphasis_rows):
            commands.append(
                (
                    "BACKGROUND",
                    (0, row_index),
                    (-1, row_index),
                    colors.HexColor("#FFF6DD"),
                )
            )
        commands.extend(spans)
        table_flowable.setStyle(TableStyle(commands))
        result.append(table_flowable)
        raw_notes = (
            table.get("notes")
            or table.get("footnotes")
            or table.get("note")
            or table.get("footnote")
            or []
        )
        if isinstance(raw_notes, (str, dict)):
            raw_notes = [raw_notes]
        for raw_note in raw_notes if isinstance(raw_notes, list) else []:
            note = _table_note_text(raw_note)
            if note:
                result.append(Paragraph(_markup(note), styles["table_note"]))
        doi = str(table.get("doi") or "").strip()
        if doi:
            result.append(
                Paragraph(_markup(f"DOI: {doi}"), styles["table_note"])
            )
        if table_index + 1 < len(item.get("payload", {}).get("tables", [])):
            result.append(Spacer(1, 10))
    return result


def _image_clip_bbox(region: dict[str, Any]) -> list[float] | None:
    source_bbox = region.get("source_bbox")
    if not (
        isinstance(source_bbox, list)
        and len(source_bbox) == 4
        and all(isinstance(value, (int, float)) for value in source_bbox)
    ):
        return None
    x0, y0, x1, y1 = map(float, source_bbox)
    localized_caption = region.get("localized_caption")
    caption_bbox = (
        localized_caption.get("source_bbox")
        if isinstance(localized_caption, dict)
        else None
    )
    if not (
        isinstance(caption_bbox, list)
        and len(caption_bbox) == 4
        and all(isinstance(value, (int, float)) for value in caption_bbox)
    ):
        return [x0, y0, x1, y1]
    caption_x0, caption_y0, caption_x1, caption_y1 = map(
        float,
        caption_bbox,
    )
    horizontal_overlap = max(
        0.0,
        min(x1, caption_x1) - max(x0, caption_x0),
    )
    minimum_overlap = min(x1 - x0, caption_x1 - caption_x0) * 0.35
    if horizontal_overlap < minimum_overlap:
        return [x0, y0, x1, y1]
    midpoint = (y0 + y1) / 2
    if midpoint <= caption_y0 < y1:
        adjusted_bottom = caption_y0 - 0.75
        if adjusted_bottom - y0 >= 24.0:
            y1 = adjusted_bottom
    elif y0 < caption_y1 <= midpoint:
        adjusted_top = caption_y1 + 0.75
        if y1 - adjusted_top >= 24.0:
            y0 = adjusted_top
    return [x0, y0, x1, y1]


def _image_flowables(
    item: dict[str, Any],
    *,
    source_document: Any,
    styles: dict[str, ParagraphStyle],
    available_width: float,
    available_height: float,
    target_language: str = "zh-Hans",
) -> list[Flowable]:
    payload = (
        item.get("payload")
        if isinstance(item.get("payload"), dict)
        else {}
    )
    regions = [
        region
        for region in payload.get("regions", [])
        if isinstance(region, dict)
    ]
    def build_panel(
        region: dict[str, Any],
        *,
        panel_width: float,
        side_by_side: bool,
    ) -> list[Flowable]:
        image_bytes: bytes | None = None
        xref = region.get("xref")
        if isinstance(xref, int):
            try:
                image_bytes = source_document.extract_image(xref)["image"]
            except Exception:
                image_bytes = None
        if image_bytes is None:
            page_number = int(region.get("page") or item["page"])
            page = source_document[page_number - 1]
            clip = _image_clip_bbox(region)
            rectangle = (
                import_fitz().Rect(*map(float, clip))
                if isinstance(clip, list) and len(clip) == 4
                else page.rect
            )
            pixmap = page.get_pixmap(
                matrix=import_fitz().Matrix(1.6, 1.6),
                clip=rectangle,
                alpha=False,
            )
            image_bytes = pixmap.tobytes("png")
        image = Image(io.BytesIO(image_bytes))
        default_width_ratio = 0.48 if side_by_side else 0.72
        width_ratio = _bounded_float(
            region.get(
                "display_width_ratio",
                payload.get("display_width_ratio"),
            ),
            default=default_width_ratio,
            lower=0.3,
            upper=0.49 if side_by_side else 1.0,
        )
        max_height = _bounded_float(
            region.get(
                "display_max_height_pt",
                payload.get("display_max_height_pt"),
            ),
            default=260.0,
            lower=120.0,
            upper=520.0,
        )
        max_width = min(
            panel_width,
            available_width * width_ratio,
        )
        scale = min(
            max_width / max(image.imageWidth, 1),
            max_height / max(image.imageHeight, 1),
        )
        image.drawWidth = image.imageWidth * scale
        image.drawHeight = image.imageHeight * scale
        image.hAlign = "CENTER"
        caption = str(
            region.get("translation")
            or region.get("caption")
            or ""
        ).strip()
        media: list[Flowable] = [image]
        if caption:
            media.extend(
                [
                    Spacer(1, 4),
                    Paragraph(_markup(caption), styles["caption"]),
                ]
            )
        panel: list[Flowable] = (
            [KeepTogether(media)]
            if caption and not side_by_side
            else media
        )
        localized_labels = _localized_image_labels(region)
        if localized_labels:
            panel.extend(
                _localized_image_label_flowables(
                    localized_labels,
                    styles=styles,
                    available_width=panel_width,
                    target_language=target_language,
                    show_source=any(
                        source for source, _ in localized_labels
                    ),
                    keep_heading_with_first=not side_by_side,
                )
            )
        return panel

    if not regions:
        return []
    if len(regions) == 1:
        return build_panel(
            regions[0],
            panel_width=available_width,
            side_by_side=False,
        )
    columns = 2
    panel_width = available_width / columns - 8
    panels = [
        build_panel(
            region,
            panel_width=panel_width,
            side_by_side=True,
        )
        for region in regions
    ]
    rows = [
        panels[index : index + columns]
        for index in range(0, len(panels), columns)
    ]
    for row in rows:
        row.extend(
            [[Spacer(1, 1)] for _ in range(columns - len(row))]
        )
    data = rows
    widths = [available_width / columns] * columns
    table = Table(data, colWidths=widths, hAlign="CENTER")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                (
                    "FONTNAME",
                    (0, 0),
                    (-1, -1),
                    styles["caption"].fontName,
                ),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    try:
        _, table_height = table.wrap(available_width, available_height)
    except Exception:
        table_height = available_height + 1
    if table_height <= available_height:
        return [table]

    sequential: list[Flowable] = []
    for index, region in enumerate(regions):
        if index:
            sequential.append(Spacer(1, 12))
        sequential.extend(
            build_panel(
                region,
                panel_width=available_width,
                side_by_side=False,
            )
        )
    return sequential


def _bounded_float(
    value: Any,
    *,
    default: float,
    lower: float,
    upper: float,
) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return min(max(parsed, lower), upper)


def _localized_image_labels(
    region: dict[str, Any],
) -> list[tuple[str, str]]:
    raw_labels = region.get("localized_labels")
    if not isinstance(raw_labels, list):
        return []
    labels: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_label in raw_labels:
        if isinstance(raw_label, str):
            translation = raw_label.strip()
            pair = ("", translation)
            if translation and pair not in seen:
                labels.append(pair)
                seen.add(pair)
            continue
        if not isinstance(raw_label, dict):
            continue
        source = _image_label_text(
            raw_label.get("source")
            or raw_label.get("source_text")
            or raw_label.get("label")
            or raw_label.get("original")
            or ""
        )
        translation = _image_label_text(
            raw_label.get("translation")
            or raw_label.get("target")
            or raw_label.get("localized")
            or ""
        )
        if source and translation and source == translation:
            continue
        pair = (source, translation)
        if (source or translation) and pair not in seen:
            labels.append(pair)
            seen.add(pair)
    return labels


def _image_label_text(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(
            str(item).strip()
            for item in value
            if str(item).strip()
        )
    return str(value or "").strip()


def _localized_image_label_flowables(
    labels: list[tuple[str, str]],
    *,
    styles: dict[str, ParagraphStyle],
    available_width: float,
    target_language: str = "zh-Hans",
    show_source: bool = False,
    keep_heading_with_first: bool = True,
) -> list[Flowable]:
    cells: list[Flowable] = []
    for source, translation in labels:
        if show_source and source:
            text = (
                f"<font color='#60727A'>{_markup(source)}</font>"
                f"<br/>{_markup(translation or source)}"
            )
        else:
            text = _markup(translation or source)
        if text:
            cells.append(Paragraph(text, styles["table_note"]))
    if not cells:
        return []

    rows: list[list[Flowable]] = []
    for index in range(0, len(cells), 2):
        row = cells[index : index + 2]
        if len(row) == 1:
            row.append(Spacer(1, 1))
        rows.append(row)

    def make_table(table_rows: list[list[Flowable]]) -> Table:
        table = Table(
            table_rows,
            colWidths=[available_width / 2] * 2,
            hAlign="CENTER",
            splitByRow=1,
        )
        commands: list[tuple[Any, ...]] = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B0BEC5")),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        for row_index in range(1, len(table_rows), 2):
            commands.append(
                (
                    "BACKGROUND",
                    (0, row_index),
                    (-1, row_index),
                    colors.HexColor("#F7F8F9"),
                )
            )
        table.setStyle(TableStyle(commands))
        return table

    heading = Paragraph(
        message(target_language, "image_text_legend"),
        styles["table_note"],
    )
    if not keep_heading_with_first:
        return [
            Spacer(1, 5),
            heading,
            make_table(rows),
        ]
    result: list[Flowable] = [
        Spacer(1, 5),
        KeepTogether([heading, make_table(rows[:1])]),
    ]
    if len(rows) > 1:
        result.append(make_table(rows[1:]))
    return result


#: 保留区域最多占版心高度的这个比例。留一点余地给图题和上下文。
PRESERVED_REGION_MAX_HEIGHT_RATIO = 0.9


def _overlay_chinese_labels(
    png_bytes: bytes,
    labels: list[dict[str, Any]],
    clip: Any,
    scale: float,
    font_path: str,
) -> bytes:
    """把有译文的图内标签覆盖成中文。

    白底盖住原英文标签，再按格高写入中文——数字尺寸、通道数没有译文，
    一个像素都不动。字号从格高起步，放不下就缩，缩到底还放不下就不画，
    留着原文也比画出溢出图形的中文强。
    """

    import io as _io

    from PIL import Image as PILImage
    from PIL import ImageDraw, ImageFont

    image = PILImage.open(_io.BytesIO(png_bytes)).convert("RGB")
    draw = ImageDraw.Draw(image)
    for label in labels:
        box = label.get("bbox")
        text = str(label.get("translation") or "").strip()
        if not text or not isinstance(box, list) or len(box) != 4:
            continue
        x0 = (float(box[0]) - float(clip.x0)) * scale
        y0 = (float(box[1]) - float(clip.y0)) * scale
        x1 = (float(box[2]) - float(clip.x0)) * scale
        y1 = (float(box[3]) - float(clip.y0)) * scale
        if x1 <= x0 or y1 <= y0:
            continue
        # 中文比英文标签宽是常态，允许向右伸一点，但绝不许伸出图片
        # 边界——"输出分割图"被裁成"输出分割"比留英文还糟。
        edge = image.width - max(2.0, scale)
        size = max(int((y1 - y0) * 0.92), 6)
        font = None
        while size >= 6:
            font = ImageFont.truetype(font_path, size)
            width_needed = draw.textlength(text, font=font)
            if width_needed <= (x1 - x0) * 1.35 and x0 + width_needed <= edge:
                break
            if x0 + width_needed <= edge:
                break
            size -= 1
        if font is None or size < 6:
            continue
        width_needed = draw.textlength(text, font=font)
        draw_x = min(x0, max(0.0, edge - width_needed))
        pad = max(1.0, scale)
        draw.rectangle(
            (
                min(draw_x, x0) - pad,
                y0 - pad,
                max(x1, draw_x + width_needed) + pad,
                y1 + pad,
            ),
            fill="white",
        )
        draw.text((draw_x, y0 - size * 0.08), text, fill="black", font=font)
    output = _io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def _preserved_source_region_image(
    source_document: Any,
    *,
    page_number: int,
    bbox: list[float] | None,
    available_width: float,
    maximum_height: float,
    dpi: int = MIN_RASTER_DPI,
    labels: list[dict[str, Any]] | None = None,
    label_font_path: str | None = None,
) -> Image:
    """把原文的一块区域栅格化成一个图片流。

    渲染计划把某个元素定到保留级，意思是"重建这块不可靠"。到了这一步，
    好看已经不是目标了，**不丢内容**才是。所以这里不重画任何东西，
    只把原文那一块原样搬过来。

    两条不肯让步的地方：

    - 分辨率不低于 MIN_RASTER_DPI。低于它图里的数字就开始糊，
      而保留区域的全部意义就是那些数字还能看清。
    - 不放大。原区域在版面上占多少点就画多少点，放不下才等比缩小；
      放大不会凭空补出像素，只会把每个像素摊得更大。
    """

    fitz = import_fitz()
    page = source_document[page_number - 1]
    clip = (
        fitz.Rect(*map(float, bbox))
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4
        else page.rect
    )
    scale = max(int(dpi), MIN_RASTER_DPI) / PDF_BASE_DPI
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False
    )
    png_bytes = pixmap.tobytes("png")
    if labels and label_font_path:
        png_bytes = _overlay_chinese_labels(
            png_bytes, labels, clip, scale, label_font_path
        )
    natural_width = max(float(clip.width), 1.0)
    natural_height = max(float(clip.height), 1.0)
    ratio = min(
        1.0,
        available_width / natural_width,
        maximum_height / natural_height,
    )
    image = Image(
        io.BytesIO(png_bytes),
        width=natural_width * ratio,
        height=natural_height * ratio,
    )
    image.hAlign = "CENTER"
    return image


def _preserved_region_flowables(
    item: dict[str, Any],
    *,
    styles: dict[str, ParagraphStyle],
    source_document: Any,
    available_width: float,
    available_height: float,
    label_font_path: str | None = None,
) -> list[Flowable]:
    """渲染计划定到保留级的元素，走这里。

    图题和图用 KeepTogether 锁成一块。图在第 4 页、图题在第 5 页，
    两样东西都废了——读者既不知道这张图讲什么，也不知道这句话说的是哪张图。
    锁在一起后，放不下就整块换页，不会被拆开。
    """

    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    result: list[Flowable] = []
    for region in payload.get("regions", []):
        if not isinstance(region, dict):
            continue
        page_number = int(region.get("page") or item.get("page") or 0)
        if not 1 <= page_number <= source_document.page_count:
            continue
        caption = str(region.get("translation") or "").strip()
        # 图题要占位置，所以图能用的高度得先扣掉它，否则两者加起来放不下，
        # KeepTogether 会把整块推到下一页，白白空掉半页。
        caption_reserve = min(available_height * 0.2, 90.0) if caption else 0.0
        block: list[Flowable] = [
            _preserved_source_region_image(
                source_document,
                page_number=page_number,
                bbox=None if region.get("full_page") else region.get("bbox"),
                available_width=available_width,
                maximum_height=(
                    available_height * PRESERVED_REGION_MAX_HEIGHT_RATIO
                    - caption_reserve
                ),
                labels=(
                    region.get("labels")
                    if isinstance(region.get("labels"), list)
                    else None
                ),
                label_font_path=label_font_path,
            )
        ]
        if caption:
            block.append(Spacer(1, 4))
            block.append(Paragraph(_markup(caption), styles["caption"]))
        result.append(KeepTogether(block))
        result.append(Spacer(1, 6))
    return result


def _complex_flowables(
    item: dict[str, Any],
    *,
    styles: dict[str, ParagraphStyle],
    source_document: Any,
    available_width: float,
    available_height: float,
    regular_font: str,
    bold_font: str,
    body_font_pt: float,
    target_language: str = "zh-Hans",
    label_font_path: str | None = None,
) -> list[Flowable]:
    method = str(item.get("method") or "")
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    prefix: list[Flowable] = (
        [PageBreak()] if payload.get("page_break_before") is True else []
    )
    components = payload.get("components")
    if isinstance(components, list) and components:
        result: list[Flowable] = list(prefix)
        primary_payload = dict(payload)
        primary_payload.pop("components", None)
        primary_payload.pop("page_break_before", None)
        result.extend(
            _complex_flowables(
                {
                    **item,
                    "payload": primary_payload,
                },
                styles=styles,
                source_document=source_document,
                available_width=available_width,
                available_height=available_height,
                regular_font=regular_font,
                bold_font=bold_font,
                body_font_pt=body_font_pt,
                target_language=target_language,
                label_font_path=label_font_path,
            )
        )
        for index, component in enumerate(components):
            if not isinstance(component, dict):
                continue
            component_item = {
                **item,
                "id": f"{item['id']}-component-{index + 1}",
                "method": component.get("method") or method,
                "payload": component.get("payload") or component,
            }
            result.extend(
                _complex_flowables(
                    component_item,
                    styles=styles,
                    source_document=source_document,
                    available_width=available_width,
                    available_height=available_height,
                    regular_font=regular_font,
                    bold_font=bold_font,
                    body_font_pt=body_font_pt,
                    target_language=target_language,
                    label_font_path=label_font_path,
                )
            )
        return result
    if method in {"structured-table-rebuild", "semantic-grid-rebuild"}:
        return prefix + _table_flowables(
                item,
                styles=styles,
                available_width=available_width,
            )
    if method == "vector-rebuild":
        result = []
        for figure in payload.get("figures", []):
            if not isinstance(figure, dict):
                continue
            title = str(figure.get("title") or figure.get("caption") or "").strip()
            if title:
                result.append(Paragraph(_markup(title), styles["caption"]))
            result.append(
                VectorPayloadFlowable(
                    figure,
                    width=available_width,
                    regular_font=regular_font,
                    bold_font=bold_font,
                    body_font_pt=body_font_pt,
                    target_language=target_language,
                    message_fn=message,
                )
            )
            note_texts: list[str] = []
            note = str(figure.get("note") or "").strip()
            if note:
                note_texts.append(note)
            else:
                for annotation in figure.get("annotations", []):
                    if not isinstance(annotation, dict):
                        continue
                    if (
                        str(annotation.get("kind") or "").lower()
                        == "covariate-group"
                        or isinstance(annotation.get("x_ratio"), (int, float))
                        or isinstance(annotation.get("y_ratio"), (int, float))
                    ):
                        continue
                    annotation_text = str(
                        annotation.get("translation")
                        or annotation.get("label_translation")
                        or annotation.get("text")
                        or ""
                    ).strip()
                    if annotation_text and annotation_text not in note_texts:
                        note_texts.append(annotation_text)
            for note_text in note_texts:
                result.append(
                    Paragraph(_markup(note_text), styles["table_note"])
                )
            result.append(Spacer(1, 8))
        return prefix + result
    if method in {
        FALLBACK_PRESERVE_ELEMENT_REGION,
        FALLBACK_PRESERVE_FULL_PAGE,
    }:
        return prefix + _preserved_region_flowables(
            item,
            styles=styles,
            source_document=source_document,
            available_width=available_width,
            available_height=available_height,
            label_font_path=label_font_path,
        )
    if method in {"image-text-localization", "ocr-region-rebuild"}:
        return prefix + _with_figure_caption(
            _image_flowables(
                item,
                source_document=source_document,
                styles=styles,
                available_width=available_width,
                available_height=available_height,
                target_language=target_language,
            ),
            item,
            styles=styles,
        )
    return prefix


def _with_figure_caption(
    body: list[Flowable],
    item: dict[str, Any],
    *,
    styles: dict[str, ParagraphStyle],
) -> list[Flowable]:
    """把图级图题和图锁成一块。

    四联子图里每格自己的 (a)(b)(c)(d) 说明本来就跟着图走。但整张图的图题
    是一条独立的译文单元，排在正文流里，随时可能被分到上一页或下一页去——
    图在第 6 页、图题在第 7 页，读者既不知道这张图讲什么，也不知道这句话
    说的是哪张图。

    锁在一起后，放不下就整块换页。真放不下时 ReportLab 仍会拆，
    那是它自己的降级，好过一开始就不锁。
    """

    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    caption = str(payload.get(FIGURE_CAPTION_KEY) or "").strip()
    if not caption or not body:
        return body
    return [
        KeepTogether(
            [
                *body,
                Spacer(1, 4),
                Paragraph(_markup(caption), styles["caption"]),
            ]
        )
    ]


def _complex_render_policy(item: dict[str, Any]) -> str:
    payload = item.get("payload")
    if isinstance(payload, dict):
        policy = payload.get("render_policy")
        if policy in {"replace-page-units", "insert-before", "insert-after"}:
            return str(policy)
    return "insert-after"


def _complex_embedded_texts(item: dict[str, Any]) -> list[str]:
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return []
    texts = [
        str(value)
        for value in payload.get("suppress_texts", [])
        if str(value).strip()
    ]
    # 图级图题已经跟着图一起排了，正文里那一份要抑制掉，否则印两遍。
    figure_caption = str(payload.get(FIGURE_CAPTION_KEY) or "").strip()
    if figure_caption:
        texts.append(figure_caption)
    texts.extend(
        [
        str(region.get("translation") or region.get("caption") or "")
        for region in payload.get("regions", [])
        if isinstance(region, dict)
        ]
    )
    for region in payload.get("regions", []):
        if not isinstance(region, dict):
            continue
        texts.extend(
            source
            for source, translation in _localized_image_labels(region)
            if source
        )
        texts.extend(
            translation
            for source, translation in _localized_image_labels(region)
            if translation
        )
        localized_caption = region.get("localized_caption")
        if isinstance(localized_caption, dict):
            texts.append(str(localized_caption.get("translation") or ""))
        doi = region.get("doi")
        if isinstance(doi, dict):
            texts.append(str(doi.get("translation") or ""))
        elif doi:
            texts.append(str(doi))
    for table in payload.get("tables", []):
        if not isinstance(table, dict):
            continue
        texts.extend(
            str(value or "")
            for value in (
                table.get("translated_title"),
                table.get("title_translation"),
                table.get("title"),
                table.get("caption"),
            )
            if str(value or "").strip()
        )
        raw_notes = (
            table.get("notes")
            or table.get("footnotes")
            or table.get("note")
            or table.get("footnote")
            or []
        )
        if isinstance(raw_notes, (str, dict)):
            raw_notes = [raw_notes]
        if isinstance(raw_notes, list):
            texts.extend(_table_note_text(note) for note in raw_notes)
        doi = str(table.get("doi") or "").strip()
        if doi:
            texts.extend((doi, f"DOI: {doi}", f"DOI：{doi}"))
    for component in payload.get("components", []):
        if not isinstance(component, dict):
            continue
        texts.extend(
            _complex_embedded_texts(
                {
                    "method": component.get("method"),
                    "payload": component.get("payload") or component,
                }
            )
        )
    return [text for text in texts if text.strip()]


def _reading_order_text_token(value: Any) -> str:
    return re.sub(
        r"[\W_]+",
        "",
        unicodedata.normalize("NFKC", str(value or "")).casefold(),
        flags=re.UNICODE,
    )


def _bbox_overlap_ratio(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float],
) -> float:
    x0 = max(inner[0], outer[0])
    y0 = max(inner[1], outer[1])
    x1 = min(inner[2], outer[2])
    y1 = min(inner[3], outer[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inner_area = (inner[2] - inner[0]) * (inner[3] - inner[1])
    if inner_area <= 0:
        return 0.0
    return ((x1 - x0) * (y1 - y0)) / inner_area


def _source_block_for_unit(
    unit: dict[str, Any],
    blocks: list[dict[str, Any]],
) -> int | None:
    unit_bbox = _unit_bbox(unit)
    unit_text = _reading_order_text_token(unit.get("source"))
    best: tuple[float, int] | None = None
    for block in blocks:
        block_id = block.get("id")
        block_bbox_raw = block.get("bbox")
        if (
            not isinstance(block_id, int)
            or not isinstance(block_bbox_raw, list)
            or len(block_bbox_raw) != 4
            or not all(
                isinstance(value, (int, float))
                for value in block_bbox_raw
            )
        ):
            continue
        block_bbox = tuple(map(float, block_bbox_raw))
        overlap = (
            _bbox_overlap_ratio(unit_bbox, block_bbox)
            if unit_bbox is not None
            else 0.0
        )
        block_text = _reading_order_text_token(block.get("text"))
        text_match = bool(
            unit_text
            and block_text
            and (
                unit_text == block_text
                or (
                    min(len(unit_text), len(block_text)) >= 5
                    and (
                        unit_text in block_text
                        or block_text in unit_text
                    )
                )
            )
        )
        if overlap < 0.55 and not text_match:
            continue
        score = overlap * 2.0 + (1.0 if text_match else 0.0)
        if best is None or score > best[0]:
            best = (score, block_id)
    return best[1] if best is not None else None


def _ordered_page_units(
    page_units: list[dict[str, Any]],
    page_complex: list[dict[str, Any]],
    source_structure_page: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not page_units or not isinstance(source_structure_page, dict):
        return page_units
    ordered_block_ids: list[int] = []
    block_roles: dict[int, str] = {}
    for item in page_complex:
        if (
            not isinstance(item, dict)
            or str(item.get("method") or "")
            not in {
                "manual-reading-order-rebuild",
                "custom-page-reflow",
            }
        ):
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        for block_id in payload.get("ordered_block_ids", []):
            if (
                isinstance(block_id, int)
                and block_id not in ordered_block_ids
            ):
                ordered_block_ids.append(block_id)
        for group in payload.get("layout_groups", []):
            if not isinstance(group, dict):
                continue
            role = str(group.get("role") or "").strip()
            if not role:
                continue
            for block_id in group.get("block_ids", []):
                if isinstance(block_id, int):
                    block_roles.setdefault(block_id, role)
    if not ordered_block_ids:
        return page_units
    blocks = [
        block
        for block in source_structure_page.get("blocks", [])
        if isinstance(block, dict)
    ]
    if not blocks:
        return page_units
    rank = {
        block_id: index
        for index, block_id in enumerate(ordered_block_ids)
    }
    decorated: list[
        tuple[tuple[int, int, int], dict[str, Any]]
    ] = []
    for original_index, unit in enumerate(page_units):
        block_id = _source_block_for_unit(unit, blocks)
        rendered_unit = (
            {
                **unit,
                "_layout_role": block_roles[block_id],
            }
            if block_id in block_roles
            else unit
        )
        if block_id in rank:
            key = (0, rank[block_id], original_index)
        else:
            key = (1, original_index, original_index)
        decorated.append((key, rendered_unit))
    return [unit for _, unit in sorted(decorated, key=lambda item: item[0])]


def _reading_order_unit_roles(
    translation: dict[str, Any],
    complex_content: dict[str, Any],
    source_structure: dict[str, Any],
) -> dict[str, str]:
    units_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for unit in translation.get("units", []):
        if isinstance(unit, dict) and isinstance(unit.get("page"), int):
            units_by_page[int(unit["page"])].append(unit)
    complex_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in complex_content.get("items", []):
        if (
            isinstance(item, dict)
            and isinstance(item.get("page"), int)
            and item.get("status") == "ready"
        ):
            complex_by_page[int(item["page"])].append(item)
    structure_by_page = {
        int(page["page"]): page
        for page in source_structure.get("pages", [])
        if isinstance(page, dict) and isinstance(page.get("page"), int)
    }
    result: dict[str, str] = {}
    for page_number, page_units in units_by_page.items():
        for unit in page_units:
            reason = str(unit.get("keep_source_reason") or "").casefold()
            unit_id = str(unit.get("id") or "")
            if unit_id and (
                "脚注" in reason
                or "footnote" in reason
                or "注释" in reason
            ):
                result[unit_id] = "footnote"
        for unit in _ordered_page_units(
            page_units,
            complex_by_page.get(page_number, []),
            structure_by_page.get(page_number),
        ):
            role = str(unit.get("_layout_role") or "").strip()
            unit_id = str(unit.get("id") or "")
            if role and unit_id:
                result[unit_id] = role
    return result


def _cross_page_continuation_pairs(
    *,
    units_by_page: dict[int, list[dict[str, Any]]],
    complex_by_page: dict[int, list[dict[str, Any]]],
    retained_by_page: dict[int, list[dict[str, Any]]],
    source_document: Any,
    source_page_count: int,
) -> dict[str, dict[str, Any]]:
    visible_by_page: dict[int, list[dict[str, Any]]] = {}
    dimensions: dict[int, tuple[float, float]] = {}
    for page_number in range(1, source_page_count + 1):
        page = source_document[page_number - 1]
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
        dimensions[page_number] = (page_width, page_height)
        page_units = units_by_page.get(page_number, [])
        page_complex = complex_by_page.get(page_number, [])
        replaced_ids = complex_payload_replaced_unit_ids(
            page_units,
            page_complex,
        )
        anchored_ids = {
            str(
                item.get("payload", {}).get("insert_before_unit_id")
                or item.get("payload", {}).get("insert_after_unit_id")
                or ""
            )
            for item in page_complex
            if isinstance(item, dict)
            and isinstance(item.get("payload"), dict)
        }
        visible_by_page[page_number] = [
            unit
            for unit in page_units
            if (
                str(unit.get("id") or "") not in replaced_ids
                and str(unit.get("id") or "") not in anchored_ids
                and not _unit_fully_covered_by_retained(
                    unit,
                    retained_by_page.get(page_number, []),
                )
                and not is_nonsemantic_source_furniture_unit(
                    unit,
                    page_width=page_width,
                    page_height=page_height,
                )
            )
        ]

    pairs: dict[str, dict[str, Any]] = {}
    for page_number in range(1, source_page_count):
        if (
            complex_by_page.get(page_number)
            or complex_by_page.get(page_number + 1)
        ):
            continue
        page_width, page_height = dimensions[page_number]
        next_width, next_height = dimensions[page_number + 1]
        current_units = visible_by_page.get(page_number, [])
        following_units = visible_by_page.get(page_number + 1, [])
        main_units = [
            unit
            for unit in current_units
            if (
                str(unit.get("kind") or "").lower() == "body"
                and not _is_bottom_note_unit(
                    unit,
                    page_height=page_height,
                )
            )
        ]
        if not main_units or not following_units:
            continue
        previous = main_units[-1]
        previous_index = current_units.index(previous)
        if any(
            not _is_bottom_note_unit(unit, page_height=page_height)
            for unit in current_units[previous_index + 1 :]
        ):
            continue
        following = following_units[0]
        if _is_cross_page_continuation(
            previous,
            following,
            previous_page_width=page_width,
            previous_page_height=page_height,
            following_page_width=next_width,
            following_page_height=next_height,
        ):
            pairs[str(previous["id"])] = following
    return pairs


def _story(
    *,
    job: dict[str, Any],
    translation: dict[str, Any],
    complex_content: dict[str, Any],
    retained_payloads: list[dict[str, Any]],
    styles: dict[str, ParagraphStyle],
    source_document: Any,
    source_structure: dict[str, Any],
    available_width: float,
    available_height: float,
    regular_font: str,
    bold_font: str,
    body_font_pt: float,
    target_language: str,
    label_font_path: str | None = None,
) -> list[Flowable]:
    reference_state: dict[str, Any] = {}

    def reference_text_transform(text: str) -> str:
        """参考文献断词与折行 URL 的整理，词表按需建一次。

        必须吃**原始文本**（带换行）：断词判定靠的就是行尾的 "-\n"，
        换行一旦被上游折掉，net-works 是软断词还是真连字符就再也分不出来。
        """

        if "vocabulary" not in reference_state:
            document_text = unicodedata.normalize(
                "NFKC",
                "\n".join(
                    source_document[index].get_text("text")
                    for index in range(source_document.page_count)
                ),
            )
            reference_state["vocabulary"] = build_vocabulary(document_text)
            reference_state["forms"] = build_hyphenated_forms(document_text)
        value = unicodedata.normalize("NFKC", text or "")
        value = re.sub(r"\u00ad\s*", "", value)
        normalized, _decisions = normalize_reference_text(
            value,
            reference_state["vocabulary"],
            reference_state["forms"],
        )
        # 上游有的管线在更早处就把换行折掉了，伪影已经拼死在文本里，
        # 再补一遍按词表的固化修复。
        return repair_baked_line_artifacts(
            normalized,
            reference_state["vocabulary"],
            reference_state["forms"],
        )

    units_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for unit in translation.get("units", []):
        if isinstance(unit, dict) and isinstance(unit.get("page"), int):
            units_by_page[int(unit["page"])].append(unit)
    complex_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in complex_content.get("items", []):
        if (
            isinstance(item, dict)
            and isinstance(item.get("page"), int)
            and item.get("status") == "ready"
        ):
            complex_by_page[int(item["page"])].append(item)
    complex_coverage_by_page: dict[
        int,
        list[dict[str, Any]],
    ] = defaultdict(list)
    for items in complex_by_page.values():
        for item in items:
            for covered_page in _complex_item_source_pages(item):
                complex_coverage_by_page[covered_page].append(item)
    structure_by_page = {
        int(page["page"]): page
        for page in source_structure.get("pages", [])
        if isinstance(page, dict) and isinstance(page.get("page"), int)
    }
    for page_number, page_units in tuple(units_by_page.items()):
        units_by_page[page_number] = _ordered_page_units(
            page_units,
            complex_by_page.get(page_number, []),
            structure_by_page.get(page_number),
        )
    retained_by_page = retained_regions_by_page(retained_payloads)
    source_page_count = int(job["source"]["page_count"])
    cross_page_pairs = _cross_page_continuation_pairs(
        units_by_page=units_by_page,
        complex_by_page=complex_coverage_by_page,
        retained_by_page=retained_by_page,
        source_document=source_document,
        source_page_count=source_page_count,
    )
    cross_page_consumed_ids = {
        str(unit["id"]) for unit in cross_page_pairs.values()
    }

    result: list[Flowable] = []
    reference_section_started = False
    started_source_pages: set[int] = set()
    for source_page in range(1, source_page_count + 1):
        source_page_width = float(
            source_document[source_page - 1].rect.width
        )
        source_page_height = float(
            source_document[source_page - 1].rect.height
        )
        page_units = units_by_page.get(source_page, [])
        page_complex = complex_by_page.get(source_page, [])
        page_complex_coverage = complex_coverage_by_page.get(
            source_page,
            [],
        )
        page_retained = retained_by_page.get(source_page, [])
        if (
            result
            and any(
                isinstance(item.get("payload"), dict)
                and item["payload"].get("page_break_before_source_page")
                is True
                for item in page_complex
            )
        ):
            result.append(PageBreak())
        source_id = f"source-page-{source_page:04d}"
        if source_page not in started_source_pages:
            result.append(
                MappingAnchor(
                    "start",
                    "source-page",
                    source_id,
                    source_page,
                )
            )
            started_source_pages.add(source_page)
        # 源页映射只靠上面的 MappingAnchor（零尺寸、不可见）。
        # 不再插入"原文第 X 页"可见段落——那是调试标记，读者不需要，
        # 它还会劈开跨源页的句子。映射结果照常写进 candidate-page-map.json。
        replace_items = [
            item
            for item in page_complex
            if _complex_render_policy(item) == "replace-page-units"
        ]
        anchored_items = [
            item
            for item in page_complex
            if isinstance(item.get("payload"), dict)
            and (
                item["payload"].get("insert_before_unit_id")
                or item["payload"].get("insert_after_unit_id")
            )
        ]
        before_items = [
            item
            for item in page_complex
            if _complex_render_policy(item) == "insert-before"
            and item not in anchored_items
        ]
        after_items = [
            item
            for item in page_complex
            if _complex_render_policy(item) == "insert-after"
            and item not in anchored_items
        ]
        page_unit_ids = {
            str(unit.get("id") or "")
            for unit in page_units
            if isinstance(unit, dict)
        }
        for item in anchored_items:
            payload = item["payload"]
            anchor_id = str(
                payload.get("insert_before_unit_id")
                or payload.get("insert_after_unit_id")
                or ""
            )
            if anchor_id not in page_unit_ids:
                raise SkillError(
                    f"复杂内容 {item.get('id')} 的插入锚点不存在于"
                    f"原文第 {source_page} 页: {anchor_id}"
                )
        if anchored_items and replace_items:
            raise SkillError(
                f"原文第 {source_page} 页不能同时使用整页替换与单元锚点"
            )
        retained_before = [
            payload
            for payload in page_retained
            if _retained_render_policy(
                payload,
                float(source_document[source_page - 1].rect.height),
            )
            == "insert-before"
        ]
        retained_after = [
            payload
            for payload in page_retained
            if payload not in retained_before
        ]
        if _retained_references_precede_visible_units(
            page_units,
            page_retained,
            page_complex_coverage,
            page_width=source_page_width,
            page_height=source_page_height,
        ):
            inferred_before = [
                payload
                for payload in retained_after
                if (
                    str(payload.get("category") or "")
                    in REFERENCE_CATEGORIES
                )
            ]
            retained_before.extend(inferred_before)
            retained_after = [
                payload
                for payload in retained_after
                if payload not in inferred_before
            ]

        def add_complex(item: dict[str, Any]) -> None:
            item_id = str(item.get("id") or f"p{source_page:04d}-complex")
            result.append(
                MappingAnchor("start", "complex", item_id, source_page)
            )
            result.extend(
                _complex_flowables(
                    item,
                    styles=styles,
                    source_document=source_document,
                    available_width=available_width,
                    available_height=available_height,
                    regular_font=regular_font,
                    bold_font=bold_font,
                    body_font_pt=body_font_pt,
                    target_language=target_language,
                    label_font_path=label_font_path,
                )
            )
            result.append(
                MappingAnchor("end", "complex", item_id, source_page)
            )

        def add_retained(
            payload: dict[str, Any],
            *,
            force_render: bool = False,
        ) -> None:
            nonlocal reference_section_started
            retained_id = str(payload["id"])
            is_reference = (
                str(payload.get("category") or "") in REFERENCE_CATEGORIES
            )
            result.append(
                MappingAnchor("start", "retained", retained_id, source_page)
            )
            result.extend(
                _retained_flowables(
                    (
                        {
                            **payload,
                            "already_present_in_translation": False,
                        }
                        if force_render
                        else payload
                    ),
                    styles=styles,
                    target_language=target_language,
                    include_reference_heading=(
                        is_reference and not reference_section_started
                    ),
                    reference_text_transform=reference_text_transform,
                )
            )
            result.append(
                MappingAnchor("end", "retained", retained_id, source_page)
            )
            if is_reference and payload.get("blocks"):
                reference_section_started = True

        for item in before_items:
            add_complex(item)
        for payload in retained_before:
            add_retained(payload)
        reference_regions = [
            payload
            for payload in retained_after
            if str(payload.get("category") or "") in REFERENCE_CATEGORIES
        ]
        reference_only_page = bool(
            page_units
            and reference_regions
            and all(
                (
                    str(unit.get("kind") or "").lower()
                    in REFERENCE_KINDS
                    or _is_reference_heading_unit(unit)
                )
                for unit in page_units
            )
            and not replace_items
        )
        if reference_only_page:
            for unit in page_units:
                result.append(
                    MappingAnchor(
                        "start",
                        "unit",
                        str(unit["id"]),
                        source_page,
                    )
                )
            for payload in retained_after:
                add_retained(payload, force_render=True)
            for unit in page_units:
                result.append(
                    MappingAnchor(
                        "end",
                        "unit",
                        str(unit["id"]),
                        source_page,
                    )
                )
        elif replace_items:
            for unit in page_units:
                unit_id = str(unit["id"])
                result.append(
                    MappingAnchor("start", "unit", unit_id, source_page)
                )
            for item in replace_items:
                add_complex(item)
            for unit in page_units:
                unit_id = str(unit["id"])
                result.append(
                    MappingAnchor("end", "unit", unit_id, source_page)
                )
        else:
            suppressed_texts = [
                text
                for item in page_complex_coverage
                for text in _complex_embedded_texts(item)
            ]
            complex_replaced_unit_ids = complex_payload_replaced_unit_ids(
                page_units,
                page_complex_coverage,
            )
            anchored_unit_ids = {
                str(
                    item["payload"].get("insert_before_unit_id")
                    or item["payload"].get("insert_after_unit_id")
                    or ""
                )
                for item in anchored_items
            }

            def unit_is_hidden(unit: dict[str, Any]) -> bool:
                unit_id = str(unit.get("id") or "")
                return bool(
                    _unit_fully_covered_by_retained(
                        unit,
                        page_retained,
                    )
                    or is_nonsemantic_source_furniture_unit(
                        unit,
                        page_width=source_page_width,
                        page_height=source_page_height,
                    )
                    or unit_id in complex_replaced_unit_ids
                )

            unit_index = 0
            while unit_index < len(page_units):
                unit = page_units[unit_index]
                unit_id = str(unit.get("id") or "")
                if unit_id in cross_page_consumed_ids:
                    unit_index += 1
                    continue
                for item in anchored_items:
                    if (
                        str(
                            item["payload"].get(
                                "insert_before_unit_id"
                            )
                            or ""
                        )
                        == unit_id
                    ):
                        add_complex(item)
                if unit_is_hidden(unit):
                    result.append(
                        MappingAnchor(
                            "start",
                            "unit",
                            unit_id,
                            source_page,
                        )
                    )
                    result.append(
                        MappingAnchor(
                            "end",
                            "unit",
                            unit_id,
                            source_page,
                        )
                    )
                    rendered_units = [unit]
                else:
                    rendered_units = [unit]
                    same_page_unit_count = 1
                    if unit_id not in anchored_unit_ids:
                        next_index = unit_index + 1
                        while next_index < len(page_units):
                            next_unit = page_units[next_index]
                            next_id = str(next_unit.get("id") or "")
                            if (
                                next_id in anchored_unit_ids
                                or unit_is_hidden(next_unit)
                                or not _should_join_line_fragment(
                                    rendered_units,
                                    next_unit,
                                    page_width=source_page_width,
                                )
                            ):
                                break
                            rendered_units.append(next_unit)
                            next_index += 1
                        same_page_unit_count = len(rendered_units)
                    continuation = cross_page_pairs.get(
                        str(rendered_units[-1].get("id") or "")
                    )
                    if continuation is not None:
                        continuation_page = int(continuation["page"])
                        if continuation_page not in started_source_pages:
                            result.append(
                                MappingAnchor(
                                    "start",
                                    "source-page",
                                    (
                                        f"source-page-"
                                        f"{continuation_page:04d}"
                                    ),
                                    continuation_page,
                                )
                            )
                            started_source_pages.add(continuation_page)
                        rendered_units.append(continuation)
                    if len(rendered_units) > 1:
                        result.extend(
                            _joined_unit_flowables(
                                rendered_units,
                                styles,
                                suppressed_texts,
                                target_language=target_language,
                            )
                        )
                    else:
                        result.extend(
                            _unit_flowables(
                                unit,
                                styles,
                                suppress_texts=suppressed_texts,
                            )
                        )
                if any(
                    _is_reference_heading_unit(rendered_unit)
                    for rendered_unit in rendered_units
                ):
                    reference_section_started = True
                for item in anchored_items:
                    if (
                        str(
                            item["payload"].get(
                                "insert_after_unit_id"
                            )
                            or ""
                        )
                        == unit_id
                    ):
                        add_complex(item)
                unit_index += (
                    same_page_unit_count
                    if not unit_is_hidden(unit)
                    else 1
                )
        if not reference_only_page:
            for payload in retained_after:
                add_retained(payload)
        for item in after_items:
            add_complex(item)
        result.append(
            MappingAnchor("end", "source-page", source_id, source_page)
        )
    return result


def _title_from_translation(translation: dict[str, Any], fallback: str) -> str:
    for unit in translation.get("units", []):
        if not isinstance(unit, dict):
            continue
        if str(unit.get("kind") or "").lower() not in HEADING_KINDS:
            continue
        value = str(unit.get("translation") or "").strip()
        if value:
            return value.splitlines()[0][:180]
    for unit in translation.get("units", []):
        if isinstance(unit, dict):
            value = str(unit.get("translation") or "").strip()
            if value:
                return value.splitlines()[0][:180]
    return fallback


def _render_attempt(
    *,
    path: Path,
    job: dict[str, Any],
    translation: dict[str, Any],
    complex_content: dict[str, Any],
    retained_payloads: list[dict[str, Any]],
    source_document: Any,
    source_structure: dict[str, Any],
    page_size: tuple[float, float],
    margins: tuple[float, float, float, float],
    regular_font: str,
    bold_font: str,
    reference_font: str,
    body_font_pt: float,
    leading_ratio: float,
    reference_font_pt: float,
    label_font_path: str | None = None,
) -> tuple[MappingTracker, int]:
    """完整试排一次，结果写入给定路径。

    这里刻意保留落盘：实测在写时复制文件系统上，写临时文件再内存映射打开，
    比把整份 PDF 复制成 bytes 再从内存流解析更快，也不会把每次试排的完整
    PDF 都留在内存里。选中的候选直接复用它自己的临时文件，不重排。
    """

    tracker = MappingTracker()
    styles = _styles(
        regular_font=regular_font,
        bold_font=bold_font,
        reference_font=reference_font,
        body_font_pt=body_font_pt,
        leading_ratio=leading_ratio,
        reference_font_pt=reference_font_pt,
    )
    available_width = page_size[0] - margins[0] - margins[1]
    available_height = page_size[1] - margins[2] - margins[3]
    story = _story(
        job=job,
        translation=translation,
        complex_content=complex_content,
        retained_payloads=retained_payloads,
        styles=styles,
        source_document=source_document,
        source_structure=source_structure,
        available_width=available_width,
        available_height=available_height,
        regular_font=regular_font,
        bold_font=bold_font,
        body_font_pt=body_font_pt,
        target_language=str(job["translation"]["target_language"]),
        label_font_path=label_font_path,
    )
    document = MappingDocTemplate(
        str(path),
        tracker=tracker,
        page_size=page_size,
        margins=margins,
        regular_font=regular_font,
        title=_title_from_translation(translation, str(job["job_id"])),
        target_language=str(job["translation"]["target_language"]),
    )
    def canvas_maker(filename: str, **kwargs):
        kwargs["initialFontName"] = regular_font
        kwargs["initialFontSize"] = body_font_pt
        kwargs["initialLeading"] = body_font_pt * leading_ratio
        return Canvas(filename, **kwargs)

    # 矢量图 Flowable 在包内抛自己的异常，这里翻译回 SkillError，
    # 保证对外的错误类型和文案与搬家前完全一致。
    try:
        document.build(story, canvasmaker=canvas_maker)
    except VectorFigureError as error:
        raise SkillError(str(error)) from error
    tracker.finalize_heading_check()
    perf_trace.count(perf_trace.COUNTER_RENDER_ATTEMPT)
    output = open_candidate_analysis(path)
    page_count = output.page_count
    output.release()
    return tracker, page_count


def _typography_candidate_groups(
    job: dict[str, Any],
) -> list[list[tuple[float, float]]]:
    """把作业里的质量配置翻译成候选搜索空间。

    这里只做作业数据到参数的适配；网格本身由 `typography_fit.candidate_groups`
    唯一定义，排版器不再自带一套。
    """

    quality = job.get("quality", {})
    search = quality.get("typography_search") or {}
    font_range = search.get("body_font_range_pt") or quality.get(
        "body_font_target_pt",
        [9.5, 11.5],
    )
    leading_range = search.get("leading_range") or quality.get(
        "leading_target",
        [1.5, 1.65],
    )
    lower_font, upper_font = map(float, font_range)
    lower_leading, upper_leading = map(float, leading_range)
    return candidate_groups(
        body_font_range_pt=(lower_font, upper_font),
        body_font_step_pt=max(
            float(search.get("body_font_step_pt") or 0.5),
            0.5,
        ),
        leading_range=(lower_leading, upper_leading),
        leading_step=max(float(search.get("leading_step") or 0.05), 0.05),
        preferred_body_font_pt=float(
            quality.get("body_font_preferred_pt")
            or (lower_font + upper_font) / 2
        ),
        preferred_leading=float(
            quality.get("leading_preferred")
            or (lower_leading + upper_leading) / 2
        ),
    )


def _estimated_page_count(
    *,
    translated_chars: int,
    paragraph_count: int,
    heading_count: int,
    available_width_pt: float,
    available_height_pt: float,
    body_font_pt: float,
    leading_ratio: float,
) -> int:
    """不做完整试排的轻量页数估算。

    只用字量、段落数、标题数和可用版心，用来记录搜索依据；它不参与选择，
    因此估算偏差不会改变最终字号。
    """

    line_height = max(body_font_pt * leading_ratio, 1.0)
    chars_per_line = max(available_width_pt / max(body_font_pt, 1.0), 1.0)
    lines_per_page = max(available_height_pt / line_height, 1.0)
    text_lines = translated_chars / chars_per_line
    spacing_lines = paragraph_count * 0.9 + heading_count * 1.6
    return max(1, math.ceil((text_lines + spacing_lines) / lines_per_page))


def _bookmark_entries(
    mapping: dict[str, Any], translation: dict[str, Any]
) -> list[list[Any]]:
    """书签用真实章节标题，不用"原文第 X 页"调试标记。

    判据与正文渲染同一套：kind 属于标题类且绑定角色允许当标题
    （作者单位、arXiv 版本戳、图内标签长得再像标题也不是标题）。
    纯启发式判出来的"疑似标题"不进书签——书签宁缺毋滥。
    """

    unit_pages = {
        str(entry.get("unit_id") or ""): list(
            entry.get("candidate_pages") or []
        )
        for entry in mapping.get("units", [])
    }
    toc: list[list[Any]] = []
    for unit in translation.get("units", []):
        if not isinstance(unit, dict):
            continue
        text = str(unit.get("translation") or "").strip()
        if not text or not _role_may_head(unit):
            continue
        kind = str(unit.get("kind") or "").lower()
        if kind in HEADING_KINDS or unit.get("heading_level") == 1:
            level = 1
        elif unit.get("heading_level") == 2:
            level = 2
        else:
            continue
        pages = unit_pages.get(str(unit.get("id") or ""))
        if not pages:
            continue
        # set_toc 要求层级从 1 开始且不跳级
        if not toc and level > 1:
            level = 1
        elif toc and level > toc[-1][0] + 1:
            level = toc[-1][0] + 1
        toc.append([level, text, int(pages[0])])
    return toc


def _add_outline(
    source_pdf: Path,
    destination_pdf: Path,
    mapping: dict[str, Any],
    translation: dict[str, Any],
) -> None:
    handle = open_candidate_analysis(source_pdf, role="source")
    document = handle.document
    toc = _bookmark_entries(mapping, translation)
    if toc:
        document.set_toc(toc)
    document.save(destination_pdf, garbage=4, deflate=True)
    handle.release()


def _adaptive_page_expansion_limit(
    source_document: Any,
    retained_payloads: list[dict[str, Any]],
    complex_content: dict[str, Any] | None = None,
) -> float:
    page_count = max(int(source_document.page_count), 1)
    reference_page_equivalents = 0.0
    for payload in retained_payloads:
        if (
            str(payload.get("category") or "") not in REFERENCE_CATEGORIES
            or payload.get("resolution")
            == "translated-nonreference-region"
        ):
            continue
        page_number = payload.get("page")
        bbox = payload.get("effective_bbox") or payload.get("bbox")
        if (
            not isinstance(page_number, int)
            or not 1 <= page_number <= page_count
            or not isinstance(bbox, list)
            or len(bbox) != 4
        ):
            continue
        page = source_document[page_number - 1]
        page_area = max(float(page.rect.width * page.rect.height), 1.0)
        x0, y0, x1, y1 = map(float, bbox)
        region_area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
        reference_page_equivalents += min(region_area / page_area, 1.0)
    reference_share = min(reference_page_equivalents / page_count, 1.0)
    complex_page_equivalents = 0.0
    for item in (complex_content or {}).get("items", []):
        if not isinstance(item, dict) or item.get("status") != "ready":
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        method = str(item.get("method") or "")
        if method in {"structured-table-rebuild", "semantic-grid-rebuild"}:
            for table in payload.get("tables", []):
                if not isinstance(table, dict):
                    continue
                rows = table.get("rows")
                row_count = (
                    len(rows)
                    if isinstance(rows, list)
                    else int(table.get("row_count") or 0)
                )
                complex_page_equivalents += max(0.15, row_count / 30.0)
        elif method in {"image-text-localization", "ocr-region-rebuild"}:
            for region in payload.get("regions", []):
                if not isinstance(region, dict):
                    continue
                label_count = len(_localized_image_labels(region))
                complex_page_equivalents += 0.35 + label_count / 30.0
        elif method == "vector-rebuild":
            figures = payload.get("figures")
            if isinstance(figures, list):
                complex_page_equivalents += max(0.4, len(figures) * 0.4)
    complex_share = min(complex_page_equivalents / page_count, 1.0)
    return round(
        min(
            2.4,
            1.6
            + reference_share * 1.5
            + min(complex_share * 0.9, 0.8),
        ),
        3,
    )


def _element_unit_texts(job_dir: Path, translation: dict[str, Any]) -> dict[str, str]:
    """元素到译文的映射，用来给保留区域取图题。

    单元归属取 unit_bindings.json——元素清单自己的 translation_unit_ids
    是空的，那是另一个阶段算出来的结果，没有回填进清单。
    """

    bindings_path = job_dir / "unit_bindings.json"
    if not bindings_path.is_file():
        return {}
    units = {
        str(unit.get("id") or ""): unit
        for unit in translation.get("units", [])
        if isinstance(unit, dict)
    }
    texts: dict[str, list[str]] = {}
    for binding in load_json(bindings_path).get("bindings", []):
        if not isinstance(binding, dict):
            continue
        element_id = str(binding.get("element_id") or "")
        unit = units.get(str(binding.get("unit_id") or ""))
        if not element_id or unit is None:
            continue
        value = str(unit.get("translation") or "").strip()
        if value:
            texts.setdefault(element_id, []).append(value)
    return {key: " ".join(value) for key, value in texts.items()}


def _merge_render_plan_preservations(
    job_dir: Path,
    complex_content: dict[str, Any],
    translation: dict[str, Any],
) -> dict[str, Any]:
    """把渲染计划里的保留级决定并进复杂内容。

    没有渲染计划就原样返回——老作业不该因为多了这一步而跑不动。
    元素清单缺失同理：翻译需要坐标，没有清单就取不到坐标。
    """

    plan_path = job_dir / "render_plan.json"
    elements_path = job_dir / "source_elements.json"
    if not plan_path.is_file() or not elements_path.is_file():
        return complex_content

    plan = load_json(plan_path)
    elements = load_json(elements_path).get("elements") or []
    unit_texts = _element_unit_texts(job_dir, translation)
    page_sizes: dict[int, tuple[float, float]] = {}
    table_items: list[dict[str, Any]] = []
    rebuilt_tables: set[str] = set()
    source_path = job_dir / "source.pdf"
    translation_units = [
        unit
        for unit in translation.get("units", [])
        if isinstance(unit, dict)
    ]
    if source_path.is_file():
        # 经计数通道打开：这次读页尺寸也计入性能基线的 PDF 打开次数。
        handle = open_candidate_analysis(source_path, role="source")
        try:
            _doc = handle.document
            page_sizes = {
                index + 1: (
                    float(_doc[index].rect.width),
                    float(_doc[index].rect.height),
                )
                for index in range(_doc.page_count)
            }
            # 计划定为"保留表格区域"的表，先试自动重建成中文结构表：
            # 网格是几何事实，数字不用翻，文字格的译文从既有单元里收割。
            # 哪一步没把握就仍走贴图保底。
            plan_strategies = {
                str(entry.get("element_id") or ""): str(
                    entry.get("strategy") or ""
                )
                for entry in plan.get("elements", [])
                if isinstance(entry, dict)
            }
            unit_texts_for_captions = _element_unit_texts(job_dir, translation)
            elements_by_id = {
                str(element.get("id") or ""): element
                for element in elements
                if isinstance(element, dict)
            }
            for element in elements:
                if not isinstance(element, dict):
                    continue
                element_id = str(element.get("id") or "")
                if (
                    plan_strategies.get(element_id)
                    != "preserve-table-region-with-translation-key"
                ):
                    continue
                page_number = int(element.get("page") or 0)
                if not 1 <= page_number <= _doc.page_count:
                    continue
                caption = ""
                for caption_id in (element.get("relations") or {}).get(
                    "caption", []
                ):
                    caption = unit_texts_for_captions.get(
                        str(caption_id), ""
                    ).strip()
                    if not caption:
                        caption_element = elements_by_id.get(str(caption_id))
                        if caption_element:
                            caption = str(
                                caption_element.get("text_excerpt") or ""
                            ).strip()
                    if caption:
                        break
                item = build_table_payload(
                    _doc[page_number - 1],
                    element,
                    translation_units,
                    caption=caption,
                )
                if item is not None:
                    table_items.append(item)
                    rebuilt_tables.add(element_id)
            # 结构化抑制标注：数学字体残渣、标题续行重复。
            # 需要查原文字形字体，趁文档开着做。清单随译文对象带给渲染日志。
            formula_boxes: dict[int, list[list[float]]] = {}
            for element in elements:
                if (
                    isinstance(element, dict)
                    and str(element.get("type") or "") == "display-formula"
                    and isinstance(element.get("bbox"), list)
                ):
                    fb = [float(v) for v in element["bbox"]]
                    # 紧框加碎片垫，与桥接层的坐标吞一致
                    fb = [fb[0] - 28.0, fb[1] - 5.0, fb[2] + 28.0, fb[3] + 5.0]
                    formula_boxes.setdefault(
                        int(element.get("page") or 0), []
                    ).append(fb)
            translation["_suppression_manifest"] = (
                _annotate_structural_suppressions(
                    _doc, translation, formula_boxes
                )
            )
            # 公式裁切三步法要做边缘墨迹检查，需要页对象在手，
            # 所以桥接在文档还开着时完成。
            source_pages = {
                index + 1: _doc[index] for index in range(_doc.page_count)
            }
            bridged = build_preservation_items(
                plan,
                elements,
                unit_texts_by_element=unit_texts,
                page_sizes=page_sizes,
                units=translation_units,
                skip_elements=rebuilt_tables,
                source_pages=source_pages,
            )
        finally:
            handle.release()
    else:
        bridged = build_preservation_items(
            plan,
            elements,
            unit_texts_by_element=unit_texts,
            page_sizes=page_sizes,
            units=translation_units,
            skip_elements=rebuilt_tables,
        )
    bridged.items.extend(table_items)
    merged = (
        merge_into_complex_content(complex_content, bridged)
        if (bridged.items or bridged.skipped)
        else complex_content
    )
    # 图级图题挂到它那个复杂条目上，让它跟着图走。
    merged, _attached = attach_figure_captions(merged, elements, unit_texts)
    return merged


MATH_FONT_RE = re.compile(
    r"CMSY|CMEX|CMMI|MSAM|MSBM|Math|Symbol", re.IGNORECASE
)

#: 数学符号字体里少数能确定解码的字形：CMSY/CMEX 的 'p' 是根号。
#: 解码不是翻译——它是按字体编码把源字形读对，√(2/N) 的根号就是这么丢的。
MATH_GLYPH_DECODE = {"p": "√"}


def _annotate_structural_suppressions(
    document: Any,
    translation: dict[str, Any],
    formula_boxes: dict[int, list[list[float]]] | None = None,
) -> list[dict[str, Any]]:
    """结构化判定哪些单元不该以文字形式渲染。

    两类，判据都是结构不是猜测：

    1. **数学字体残渣。** 源文只有一两个字符、没有译文，且原文对应
       位置的字形全部来自数学符号字体（CMSY 里的 'p' 其实是根号）。
       把它当拉丁字母排出来只会得到一个孤立的 'p'。
    2. **标题续行重复。** 原文标题折行被拆成两个单元，模型在第一个
       单元里已译出完整标题，续行单元的译文是它的子串——再排一遍
       就是"…卷积网络图像分割"这种重复。

    返回抑制清单（写进渲染日志），并在单元上打 ``_suppressed_reason``。
    """

    manifest: list[dict[str, Any]] = []
    units = [
        unit
        for unit in translation.get("units", [])
        if isinstance(unit, dict)
    ]

    for unit in units:
        source = str(unit.get("source") or "").strip()
        # keep_source 不豁免：模型标了"根号符号"保源，但源字形在数学
        # 符号字体里，按拉丁字母排出来只是个 'p'——字体检查才是判据。
        if (
            str(unit.get("translation") or "").strip()
            or not source
            or len(source) > 3
        ):
            continue
        bbox = unit.get("source_bbox")
        page_number = unit.get("page")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or not isinstance(page_number, int)
            or not 1 <= page_number <= document.page_count
        ):
            continue
        import fitz

        # 邻字会蹭进精确 bbox 的 clip；只认文字与本单元源文一致、
        # 且中心落在 bbox 内的那个 span 的字体。
        clip = fitz.Rect(*bbox)
        fonts: set[str] = set()
        for block in document[page_number - 1].get_text(
            "dict", clip=clip
        ).get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    span_text = str(span.get("text") or "").strip()
                    sb = span.get("bbox") or (0, 0, 0, 0)
                    cx = (sb[0] + sb[2]) / 2
                    cy = (sb[1] + sb[3]) / 2
                    if (
                        span_text
                        and span_text == source
                        and bbox[0] <= cx <= bbox[2]
                        and bbox[1] <= cy <= bbox[3]
                    ):
                        fonts.add(str(span.get("font") or ""))
        if fonts and all(MATH_FONT_RE.search(font) for font in fonts):
            cx = (bbox[0] + bbox[2]) / 2
            cy = (bbox[1] + bbox[3]) / 2
            inside_formula = any(
                fb[0] <= cx <= fb[2] and fb[1] <= cy <= fb[3]
                for fb in (formula_boxes or {}).get(page_number, [])
            )
            decoded = MATH_GLYPH_DECODE.get(source)
            if decoded and not inside_formula:
                # 行内符号且能确定解码：按字体编码读对它，不是删掉它。
                unit["_decoded_math"] = decoded
                manifest.append(
                    {
                        "unit_id": str(unit.get("id") or ""),
                        "text": source,
                        "fonts": sorted(fonts),
                        "decoded": decoded,
                        "reason": (
                            f"数学字体字形按编码解码为 {decoded!r}，"
                            "保留在行内"
                        ),
                    }
                )
            else:
                unit["_suppressed_reason"] = "math-font-residue"
                manifest.append(
                    {
                        "unit_id": str(unit.get("id") or ""),
                        "text": source,
                        "fonts": sorted(fonts),
                        "reason": (
                            "源文只是数学符号字体里的字形，且其内容已随"
                            "公式保留区域整块保留，不按拉丁字母重复排版"
                            if inside_formula
                            else "源文只是数学符号字体里的字形，无法确定"
                            "解码，不按拉丁字母排版"
                        ),
                    }
                )

    previous: dict[str, Any] | None = None
    for unit in units:
        translation_text = str(unit.get("translation") or "").strip()
        if previous is not None and translation_text:
            previous_text = str(previous.get("translation") or "").strip()
            if (
                str(previous.get("_element_role") or "") == "document-title"
                and unit.get("page") == previous.get("page")
                and translation_text != previous_text
                and translation_text in previous_text
            ):
                unit["_suppressed_reason"] = "title-continuation-duplicate"
                manifest.append(
                    {
                        "unit_id": str(unit.get("id") or ""),
                        "text": translation_text,
                        "reason": (
                            "标题续行的译文已包含在整题译文里，再排一遍"
                            "就是重复"
                        ),
                    }
                )
        if translation_text or str(unit.get("source") or "").strip():
            previous = unit
    return manifest


def _annotate_element_roles(
    job_dir: Path,
    translation: dict[str, Any],
) -> None:
    """把单元绑定的元素角色标到单元上，供标题判定否决使用。

    "谁是标题"应当来自原文结构分析，不是字号或行长的猜测——作者单位、
    arXiv 版本戳、图内标签都可能长得像标题。没有绑定文件时不标注，
    下游维持原有启发式行为。
    """

    bindings_path = job_dir / "unit_bindings.json"
    if not bindings_path.is_file():
        return
    try:
        bindings = load_json(bindings_path).get("bindings") or []
    except (OSError, ValueError):
        return
    roles = {
        str(binding.get("unit_id") or ""): str(binding.get("element_role") or "")
        for binding in bindings
        if isinstance(binding, dict) and binding.get("unit_id")
    }
    if not roles:
        return
    element_ids = {
        str(binding.get("unit_id") or ""): str(
            binding.get("element_id") or ""
        )
        for binding in bindings
        if isinstance(binding, dict) and binding.get("unit_id")
    }
    for unit in translation.get("units", []):
        if isinstance(unit, dict):
            unit_id = str(unit.get("id") or "")
            role = roles.get(unit_id)
            if role:
                unit["_element_role"] = role
            element_id = element_ids.get(unit_id)
            if element_id:
                unit["_element_id"] = element_id


def _timed_build_candidate(
    job_dir: Path,
    output_pdf: Path,
    *,
    max_page_expansion_ratio: float | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    job_dir = job_dir.resolve()
    output_pdf = output_pdf.resolve()
    job = load_json(job_dir / "job.json")
    translation_path = internal_job_path(
        job_dir,
        job["files"]["translation"],
    )
    translation = load_json(translation_path)
    _annotate_element_roles(job_dir, translation)
    complex_path = internal_job_path(
        job_dir,
        job.get("files", {}).get(
            "complex_content_payload",
            "complex_content.json",
        ),
    )
    complex_content = (
        load_json(complex_path)
        if complex_path.is_file()
        else {
            "schema_version": "1.0",
            "classification_complete": True,
            "items": [],
        }
    )
    # 渲染计划里定到保留级的元素，翻成生成器认识的条目再并进来。
    # 阶段 15 的基准查出：不做这一步，返修算出来的降级生成器根本看不见，
    # 重建出来的候选与返修前一字不差。
    complex_content = _merge_render_plan_preservations(
        job_dir, complex_content, translation
    )
    # 复杂内容从此是**派生视图**：并完计划立刻写回磁盘并盖上计划哈希。
    # 预渲染检查对的是这份落盘文件——生成器消化了什么、文件里就是什么，
    # 旧手写条目数再也不能把一版合法的新计划错误地拦下来。
    _plan_file = job_dir / "render_plan.json"
    if _plan_file.is_file():
        complex_content = derive_complex_view(
            complex_content, sha256_file(_plan_file)
        )
        write_json(complex_path, complex_content)

    retained_path = internal_job_path(
        job_dir,
        job["files"]["retained_source"],
    )
    retained = load_json(retained_path)
    source_structure_path = internal_job_path(
        job_dir,
        job.get("files", {}).get(
            "source_structure",
            "source_structure.json",
        ),
    )
    source_structure = (
        load_json(source_structure_path)
        if source_structure_path.is_file()
        else {
            "schema_version": "legacy-no-source-structure",
            "pages": [],
        }
    )
    unit_layout_roles = _reading_order_unit_roles(
        translation,
        complex_content,
        source_structure,
    )
    unresolved_complex = [
        item.get("id") or item.get("page")
        for item in complex_content.get("items", [])
        if isinstance(item, dict) and item.get("status") != "ready"
    ]
    if unresolved_complex:
        raise SkillError(
            "以下复杂页载荷尚未 ready，统一生成器不会把它们降级成文字摘要: "
            + ", ".join(map(str, unresolved_complex[:30]))
        )
    invalid_complex: list[str] = []
    for item in complex_content.get("items", []):
        if not isinstance(item, dict):
            invalid_complex.append("复杂页载荷含非对象条目")
            continue
        invalid_complex.extend(
            f"{item.get('id') or item.get('page')}: {error}"
            for error in validate_complex_payload_item(item)
        )
    if invalid_complex:
        raise SkillError(
            "复杂页载荷不完整: " + "；".join(invalid_complex[:30])
        )
    source_path = internal_job_path(job_dir, job["source"]["job_path"])
    # 字体正常在初始化或统一入口就已冻结；这里只兜底，并保持证据同步。
    # 冻结优先：prepare_job_fonts 已经把三个角色的字体连同哈希写进
    # job.json，渲染就用它们。渲染时再解析一遍等于绕开冻结——
    # 冻结时按题录真实字符做过覆盖检查，重解析没有这份输入，会选错。
    _frozen_fonts = (job.get("quality") or {}).get("selected_fonts")
    if (
        isinstance(_frozen_fonts, list)
        and len(_frozen_fonts) >= 3
        and fonts_are_current(job)
    ):
        regular_path = Path(_frozen_fonts[0])
        bold_path = Path(_frozen_fonts[1])
        reference_path = Path(_frozen_fonts[2])
        math_path = (
            Path(_frozen_fonts[3]) if len(_frozen_fonts) >= 4 else None
        )
    else:
        regular_path, bold_path = _resolve_fonts(job)
        reference_path = _resolve_reference_font(regular_path)
        math_path = None
    resolved_font_paths = [
        str(regular_path),
        str(bold_path),
        str(reference_path),
        *([str(math_path)] if math_path else []),
    ]
    if job.get("quality", {}).get("selected_fonts") != resolved_font_paths:
        quality = job.setdefault("quality", {})
        quality["selected_fonts"] = resolved_font_paths
        quality["selected_font_evidence"] = font_evidence(
            [
                regular_path,
                bold_path,
                reference_path,
                *([math_path] if math_path else []),
            ]
        )
        write_json(job_dir / "job.json", job)
    regular_font = "AcademicUnifiedRegular"
    bold_font = "AcademicUnifiedBold"
    reference_font_name = "AcademicUnifiedReference"
    _register_font(regular_font, regular_path)
    _register_font(bold_font, bold_path)
    _register_font(reference_font_name, reference_path)
    # 数学后备字体名是 font_runs 的模块级状态：_markup 逐字符判断时要用。
    if math_path is not None and Path(math_path).is_file():
        _register_font("AcademicUnifiedMath", Path(math_path))
        _font_runs.MATH_FALLBACK_FONT_NAME = "AcademicUnifiedMath"
    else:
        _font_runs.MATH_FALLBACK_FONT_NAME = None
    install_reportlab_cjk_nobr_patch()

    page_size = _common_page_size(source_path)
    margins = (48.0, 48.0, 42.0, 38.0)
    source_page_count = int(job["source"]["page_count"])
    source_handle = open_candidate_analysis(source_path, role="source")
    source_document = source_handle.document
    retained_payloads = extract_retained_regions(
        source_document,
        retained,
        translation,
    )
    effective_page_expansion_ratio = (
        float(max_page_expansion_ratio)
        if max_page_expansion_ratio is not None
        else _adaptive_page_expansion_limit(
            source_document,
            retained_payloads,
            complex_content,
        )
    )
    empty_retained = [
        payload["id"]
        for payload in retained_payloads
        if not payload.get("blocks")
        and payload.get("already_present_in_translation") is not True
    ]
    if empty_retained:
        source_handle.release()
        raise SkillError(
            "以下保留原文区域没有提取到可排版文字: "
            + ", ".join(empty_retained[:30])
        )
    attempts: list[dict[str, Any]] = []
    selected: tuple[Path, MappingTracker, int, float, float, float] | None = None
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    typography_groups = _typography_candidate_groups(job)
    translated_chars = sum(
        len(str(unit.get("translation") or unit.get("source") or ""))
        for unit in translation.get("units", [])
        if isinstance(unit, dict)
    )
    heading_count = sum(
        1
        for unit in translation.get("units", [])
        if isinstance(unit, dict)
        and str(unit.get("kind") or "").lower() == "heading"
    )
    paragraph_count = len(
        [unit for unit in translation.get("units", []) if isinstance(unit, dict)]
    )
    typography_estimate = {
        "translated_chars": translated_chars,
        "paragraph_count": paragraph_count,
        "heading_count": heading_count,
        "available_width_pt": round(page_size[0] - margins[0] - margins[1], 2),
        "available_height_pt": round(page_size[1] - margins[2] - margins[3], 2),
        "candidates": [
            {
                "body_font_pt": body_font,
                "leading_ratio": leading,
                "estimated_page_count": _estimated_page_count(
                    translated_chars=translated_chars,
                    paragraph_count=paragraph_count,
                    heading_count=heading_count,
                    available_width_pt=page_size[0] - margins[0] - margins[1],
                    available_height_pt=page_size[1] - margins[2] - margins[3],
                    body_font_pt=body_font,
                    leading_ratio=leading,
                ),
            }
            for group in typography_groups
            for body_font, leading in group[-1:]
        ],
        "note": (
            "估算只用于记录搜索依据，不参与选择；实际字号仍由完整试排决定。"
        ),
    }
    with tempfile.TemporaryDirectory(prefix="academic-unified-render-") as tmp:
        tmp_dir = Path(tmp)
        probe_cache: dict[tuple[int, int], dict[str, Any] | None] = {}

        def _evaluate_candidate(
            group_index: int,
            item_index: int,
        ) -> dict[str, Any] | None:
            key = (group_index, item_index)
            if key in probe_cache:
                return probe_cache[key]
            body_font, leading = typography_groups[group_index][item_index]
            reference_font_pt = _reference_font_size(job, body_font)
            attempt_started = time.monotonic()
            attempt_path = (
                tmp_dir / f"attempt-{group_index:02d}-{item_index:02d}.pdf"
            )
            try:
                tracker, page_count = _render_attempt(
                    path=attempt_path,
                    job=job,
                    translation=translation,
                    complex_content=complex_content,
                    retained_payloads=retained_payloads,
                    source_document=source_document,
                    source_structure=source_structure,
                    page_size=page_size,
                    margins=margins,
                    regular_font=regular_font,
                    bold_font=bold_font,
                    reference_font=reference_font_name,
                    body_font_pt=body_font,
                    leading_ratio=leading,
                    reference_font_pt=reference_font_pt,
                    label_font_path=str(regular_path),
                )
            except Exception as exc:
                attempts.append(
                    {
                        "body_font_pt": body_font,
                        "leading_ratio": leading,
                        "status": "render-failed",
                        "message": str(exc)[:2000],
                        "seconds": round(time.monotonic() - attempt_started, 3),
                    }
                )
                probe_cache[key] = None
                return None
            expansion = page_count / max(source_page_count, 1)
            fits = expansion <= effective_page_expansion_ratio
            record = {
                "body_font_pt": body_font,
                "leading_ratio": leading,
                "reference_font_pt": round(reference_font_pt, 2),
                "candidate_page_count": page_count,
                "page_count_ratio": round(expansion, 3),
                "status": "fits" if fits else "too-many-pages",
                "seconds": round(time.monotonic() - attempt_started, 3),
            }
            attempts.append(record)
            probe_cache[key] = {
                "record": record,
                # 不达标的候选不可能被选中；它的映射跟踪器立刻释放，
                # 否则完整扫描会把每一次试排的逐单元映射都留在内存里。
                "tracker": tracker if fits else None,
                "page_count": page_count,
                "path": attempt_path,
                "body_font_pt": body_font,
                "leading_ratio": leading,
                "reference_font_pt": reference_font_pt,
                "fits": fits,
            }
            return probe_cache[key]

        position, search_method, search_note = search_first_acceptable(
            groups=typography_groups,
            evaluate=_evaluate_candidate,
        )
        if search_method == "linear-fallback":
            for group_index, group in enumerate(typography_groups):
                for item_index in range(len(group)):
                    result = _evaluate_candidate(group_index, item_index)
                    if result is not None and result["fits"]:
                        position = (group_index, item_index)
                        break
                if position is not None:
                    break

        if position is not None:
            chosen = probe_cache[position]
            chosen["record"]["status"] = "selected"
            selected = (
                chosen["path"],
                chosen["tracker"],
                chosen["page_count"],
                chosen["body_font_pt"],
                chosen["leading_ratio"],
                chosen["reference_font_pt"],
            )
        typography_search = {
            "method": search_method,
            "note": search_note,
            "candidate_count": sum(
                len(group) for group in typography_groups
            ),
            "leading_group_count": len(typography_groups),
            "render_attempts": len(attempts),
            "estimate": typography_estimate,
        }
        if selected is None:
            rendered_attempts = [
                attempt
                for attempt in attempts
                if attempt.get("candidate_page_count")
            ]
            best = min(
                rendered_attempts,
                key=lambda attempt: float(
                    attempt.get("page_count_ratio") or math.inf
                ),
                default=None,
            )
            if best is None:
                failure_messages = list(
                    dict.fromkeys(
                        str(attempt.get("message") or "").strip()
                        for attempt in attempts
                        if attempt.get("status") == "render-failed"
                        and str(attempt.get("message") or "").strip()
                    )
                )
                detail = (
                    "没有成功试排。"
                    + (
                        "首个渲染错误：" + " | ".join(failure_messages[:3])
                        if failure_messages
                        else ""
                    )
                )
            else:
                detail = (
                    f"最紧凑的可读试排为 "
                    f"{best['candidate_page_count']} 页，"
                    f"扩张比 {best['page_count_ratio']}；"
                    f"当前异常保护上限为 "
                    f"{effective_page_expansion_ratio}。"
                )
            raise SkillError(
                "统一生成器无法在可读字号和页数扩张上限内完成首版。"
                f"{detail}"
                "请优先检查重复内容、错误保留区域或异常复杂页载荷。"
            )
        (
            selected_path,
            tracker,
            candidate_page_count,
            body_font,
            leading,
            reference_font_pt,
        ) = selected
        provisional_hash = sha256_file(selected_path)
        provisional_map = tracker.build_map(
            job=job,
            translation=translation,
            retained_payloads=retained_payloads,
            unit_layout_roles=unit_layout_roles,
            page_size=page_size,
            margins=margins,
            candidate_page_count=candidate_page_count,
            candidate_sha256=provisional_hash,
        )
        outline_path = tmp_dir / "candidate-with-outline.pdf"
        _add_outline(
            selected_path,
            outline_path,
            provisional_map,
            translation,
        )
        with tempfile.NamedTemporaryFile(
            dir=output_pdf.parent,
            prefix=f".{output_pdf.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_output = Path(handle.name)
        try:
            temp_output.write_bytes(outline_path.read_bytes())
            os.replace(temp_output, output_pdf)
        finally:
            if temp_output.exists():
                temp_output.unlink()
    source_handle.release()

    candidate_hash = sha256_file(output_pdf)
    mapping = tracker.build_map(
        job=job,
        translation=translation,
        retained_payloads=retained_payloads,
        unit_layout_roles=unit_layout_roles,
        page_size=page_size,
        margins=margins,
        candidate_page_count=candidate_page_count,
        candidate_sha256=candidate_hash,
    )
    mapping["translation_sha256"] = sha256_file(translation_path)
    map_path = output_pdf.with_suffix(".page-map.json")
    write_json(map_path, mapping)

    unit_ids = [
        str(unit["id"])
        for unit in translation.get("units", [])
        if isinstance(unit, dict) and str(unit.get("id") or "")
    ]
    complex_ids = [
        str(item["id"])
        for item in complex_content.get("items", [])
        if isinstance(item, dict) and str(item.get("id") or "")
    ]
    retained_ids = retained_region_ids(retained)
    mapped_unit_ids = {
        str(entry["unit_id"]) for entry in mapping.get("units", [])
    }
    mapped_complex_ids = {
        str(entry["complex_item_id"])
        for entry in mapping.get("complex_items", [])
    }
    mapped_retained_ids = {
        str(entry["retained_region_id"])
        for entry in mapping.get("retained_regions", [])
    }
    layout_log = {
        "schema_version": "1.0",
        "renderer": RENDERER_NAME,
        "renderer_version": RENDERER_VERSION,
        "renderer_build_id": renderer_build_id(),
        "algorithm": "continuous-flow-with-structured-complex-content-v2",
        "selection_method": "actual-render-page-budget",
        "page_size_pt": [round(value, 2) for value in page_size],
        "margins_pt": list(margins),
        "body_font_pt": body_font,
        "leading_ratio": leading,
        "reference_font_pt": round(reference_font_pt, 2),
        "source_page_count": source_page_count,
        "candidate_page_count": candidate_page_count,
        "page_count_ratio": round(
            candidate_page_count / max(source_page_count, 1),
            3,
        ),
        "page_count_ratio_limit": effective_page_expansion_ratio,
        "page_count_ratio_limit_source": (
            "explicit"
            if max_page_expansion_ratio is not None
            else "adaptive-reference-share"
        ),
        "attempts": attempts,
        "typography_search": typography_search,
        "candidate_page_map": str(map_path),
        "render_contract": {
            "all_units_consumed": mapped_unit_ids == set(unit_ids),
            "unit_count": len(unit_ids),
            "unit_ids_sha256": hashlib.sha256(
                "\n".join(unit_ids).encode("utf-8")
            ).hexdigest(),
            "all_complex_items_consumed": (
                mapped_complex_ids == set(complex_ids)
            ),
            "complex_item_count": len(complex_ids),
            "complex_item_ids_sha256": hashlib.sha256(
                "\n".join(complex_ids).encode("utf-8")
            ).hexdigest(),
            "all_retained_regions_consumed": (
                mapped_retained_ids == set(retained_ids)
            ),
            "retained_region_count": len(retained_ids),
            "retained_region_ids_sha256": hashlib.sha256(
                "\n".join(retained_ids).encode("utf-8")
            ).hexdigest(),
            "retained_source_sha256": sha256_file(retained_path),
            "retained_region_source_chars": sum(
                int(payload.get("source_char_count") or 0)
                for payload in retained_payloads
            ),
            "retained_regions_already_present": sorted(
                str(payload["id"])
                for payload in retained_payloads
                if payload.get("already_present_in_translation") is True
            ),
            "all_text_regions_measured": True,
            "unmeasured_text_regions": [],
            "overflow_regions": [],
            "heading_checks_performed": True,
            "orphan_regions": tracker.orphan_regions,
            "cjk_kinsoku_enabled": True,
            "font_paths": [
                str(regular_path),
                str(bold_path),
                str(reference_path),
                # 数学符号后备也是真用到的字体，必须进合同——
                # 少报一把，字体一致性检查就对不上。
                *([str(math_path)] if math_path else []),
            ],
            "candidate_page_map_complete": True,
        },
        # 结构化抑制名单：哪些单元没有按文字排、为什么。给人核对用。
        "suppressed_units": list(
            translation.get("_suppression_manifest") or []
        ),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    write_json(job_dir / "generator-layout-log.json", layout_log)
    return {
        "output_pdf": str(output_pdf),
        "candidate_page_map": str(map_path),
        "generator_layout_log": str(job_dir / "generator-layout-log.json"),
        "renderer": RENDERER_NAME,
        "renderer_version": RENDERER_VERSION,
        "renderer_build_id": layout_log["renderer_build_id"],
        "source_page_count": source_page_count,
        "candidate_page_count": candidate_page_count,
        "body_font_pt": body_font,
        "leading_ratio": leading,
        "page_count_ratio_limit": effective_page_expansion_ratio,
        "elapsed_seconds": layout_log["elapsed_seconds"],
    }



def build_candidate(*args, **kwargs):
    """计时包装：阶段耗时进入性能基线，行为与实现完全一致。"""

    with perf_trace.stage("build_candidate"):
        return _timed_build_candidate(*args, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="用统一连续流排和结构化复杂页载荷生成首版候选 PDF"
    )
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("output_pdf", type=Path)
    parser.add_argument(
        "--max-page-expansion-ratio",
        type=float,
        default=None,
        help="可选的异常页数保护上限；默认按参考文献占比自动计算",
    )
    args = parser.parse_args()
    try:
        if (
            args.max_page_expansion_ratio is not None
            and not 1.0 <= args.max_page_expansion_ratio <= 3.0
        ):
            raise SkillError("--max-page-expansion-ratio 必须位于 1.0..3.0")
        result = build_candidate(
            args.job_dir,
            args.output_pdf,
            max_page_expansion_ratio=args.max_page_expansion_ratio,
        )
        print(f"首版候选: {result['output_pdf']}")
        print(
            "分页: "
            f"{result['source_page_count']} 个源页 -> "
            f"{result['candidate_page_count']} 个候选页"
        )
        print(
            "正文: "
            f"{result['body_font_pt']} pt，"
            f"{result['leading_ratio']} 倍行距"
        )
        print(f"耗时: {result['elapsed_seconds']} 秒")
        return 0
    except SkillError as exc:
        print(f"错误: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
