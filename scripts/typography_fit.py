from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, Iterable


class TypographyFitError(RuntimeError):
    pass


@dataclass(frozen=True)
class PageTextProfile:
    page: int
    translated_chars: int
    paragraph_count: int
    heading_count: int
    note_count: int
    available_width_pt: float
    available_height_pt: float

    @property
    def estimated_density(self) -> float:
        area = max(self.available_width_pt * self.available_height_pt, 1.0)
        return self.translated_chars / area


@dataclass(frozen=True)
class PageFitMeasurement:
    page: int
    fits: bool
    content_width_pt: float
    content_height_pt: float
    available_height_pt: float
    fill_ratio: float
    has_orphan_line: bool = False


MeasurePage = Callable[
    [PageTextProfile, float, float],
    PageFitMeasurement,
]


def descending_values(upper: float, lower: float, step: float) -> list[float]:
    if upper <= 0 or lower <= 0:
        raise TypographyFitError("字号和行距搜索边界必须大于 0")
    if upper < lower:
        raise TypographyFitError("搜索上界不得小于下界")
    if step <= 0:
        raise TypographyFitError("搜索步长必须大于 0")
    values: list[float] = []
    current = upper
    while current >= lower - 1e-9:
        values.append(round(current, 3))
        current -= step
    if values[-1] > lower + 1e-9:
        values.append(round(lower, 3))
    return values


def select_document_typography(
    page_profiles: Iterable[PageTextProfile],
    measure_page: MeasurePage,
    *,
    body_font_range_pt: tuple[float, float],
    body_font_step_pt: float,
    leading_range: tuple[float, float],
    leading_step: float,
    selection_priority: str = "leading-then-font",
    max_densest_fill_ratio: float = 1.0,
) -> dict:
    profiles = list(page_profiles)
    if not profiles:
        raise TypographyFitError("没有可用于文档级字号计算的普通正文页")
    if len({profile.page for profile in profiles}) != len(profiles):
        raise TypographyFitError("普通正文页画像存在重复页码")
    if not 0 < max_densest_fill_ratio <= 1:
        raise TypographyFitError("最密页舒适填充率必须在 0 到 1 之间")

    body_lower, body_upper = body_font_range_pt
    leading_lower, leading_upper = leading_range
    body_sizes = descending_values(body_upper, body_lower, body_font_step_pt)
    leading_ratios = descending_values(
        leading_upper,
        leading_lower,
        leading_step,
    )
    if selection_priority == "leading-then-font":
        candidates = [
            (body_size, leading_ratio)
            for leading_ratio in leading_ratios
            for body_size in body_sizes
        ]
    elif selection_priority == "font-then-leading":
        candidates = [
            (body_size, leading_ratio)
            for body_size in body_sizes
            for leading_ratio in leading_ratios
        ]
    else:
        raise TypographyFitError(
            "selection_priority 仅支持 leading-then-font 或 font-then-leading"
        )

    ordered_profiles = sorted(
        profiles,
        key=lambda profile: (
            profile.estimated_density,
            profile.translated_chars,
        ),
        reverse=True,
    )
    tested_candidates = 0
    failed_candidates: list[dict] = []
    for body_size, leading_ratio in candidates:
        tested_candidates += 1
        measurements: list[PageFitMeasurement] = []
        failed_measurement: PageFitMeasurement | None = None
        for profile in ordered_profiles:
            measurement = measure_page(profile, body_size, leading_ratio)
            if measurement.page != profile.page:
                raise TypographyFitError(
                    "测量回调返回的页码与页面画像不一致"
                )
            measurements.append(measurement)
            if not measurement.fits or measurement.has_orphan_line:
                failed_measurement = measurement
                break
        if failed_measurement is not None:
            if len(failed_candidates) < 24:
                failed_candidates.append(
                    {
                        "body_font_pt": body_size,
                        "leading_ratio": leading_ratio,
                        "first_failed_page": failed_measurement.page,
                        "fill_ratio": round(
                            failed_measurement.fill_ratio,
                            4,
                        ),
                        "has_orphan_line": (
                            failed_measurement.has_orphan_line
                        ),
                    }
                )
            continue

        densest = max(measurements, key=lambda item: item.fill_ratio)
        if densest.fill_ratio > max_densest_fill_ratio:
            if len(failed_candidates) < 24:
                failed_candidates.append(
                    {
                        "body_font_pt": body_size,
                        "leading_ratio": leading_ratio,
                        "first_failed_page": densest.page,
                        "fill_ratio": round(densest.fill_ratio, 4),
                        "has_orphan_line": False,
                        "reason": "densest-page-comfort-limit",
                    }
                )
            continue
        widest = max(measurements, key=lambda item: item.content_width_pt)
        page_profile_payload = [
            {
                **asdict(profile),
                "estimated_density": round(profile.estimated_density, 8),
            }
            for profile in sorted(profiles, key=lambda item: item.page)
        ]
        measurement_payload = [
            asdict(measurement)
            for measurement in sorted(
                measurements,
                key=lambda item: item.page,
            )
        ]
        return {
            "algorithm": "translated-page-fit-v1",
            "selection_method": "densest-page-fit",
            "selection_priority": selection_priority,
            "body_font_pt": round(body_size, 3),
            "leading_ratio": round(leading_ratio, 3),
            "body_font_range_pt": [
                round(body_lower, 3),
                round(body_upper, 3),
            ],
            "body_font_step_pt": round(body_font_step_pt, 3),
            "leading_range": [
                round(leading_lower, 3),
                round(leading_upper, 3),
            ],
            "leading_step": round(leading_step, 3),
            "max_densest_fill_ratio": round(
                max_densest_fill_ratio,
                4,
            ),
            "tested_candidate_count": tested_candidates,
            "selected_at_body_font_upper_bound": (
                abs(body_size - body_upper) < body_font_step_pt / 2
            ),
            "densest_page": densest.page,
            "densest_fill_ratio": round(densest.fill_ratio, 4),
            "widest_body_page": widest.page,
            "widest_body_width_pt": round(widest.content_width_pt, 3),
            "total_translated_chars": sum(
                profile.translated_chars for profile in profiles
            ),
            "page_profiles": page_profile_payload,
            "page_measurements": measurement_payload,
            "failed_candidate_sample": failed_candidates,
        }

    raise TypographyFitError(
        "全篇统一正文字号无法在批准版心内排下；应调整分页、版心或内容结构，"
        "不得逐页缩小字号。"
    )
