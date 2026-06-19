from collections.abc import Iterable

import numpy as np
from PIL import Image

from app.chunking.vertical import (
    LayoutRegion,
    detect_table_layouts,
    shift_table_layout,
)
from app.layout.contracts import (
    FeatureValue,
    LayoutDecision,
    LayoutFeatures,
    LayoutStageSpec,
    SeparatorCandidate,
)


def _number(
    parameters: dict[str, FeatureValue],
    name: str,
    default: float,
) -> float:
    value = parameters.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return default
    return float(value)


def _has_visible_content(image: Image.Image) -> bool:
    gray = np.asarray(image.convert("L"))
    return bool(np.mean(gray < 245) > 0.0005)


def _crop_region(
    image: Image.Image,
    bbox: tuple[int, int, int, int],
) -> LayoutRegion | None:
    crop = image.crop(bbox)
    if not _has_visible_content(crop):
        crop.close()
        return None
    return LayoutRegion(
        kind="image",
        image=crop,
        bbox=bbox,
    )


def _partition_around_tables(
    image: Image.Image,
    *,
    min_confirmed_cell_ratio: float,
) -> list[LayoutRegion]:
    width, height = image.size
    tables = detect_table_layouts(
        image,
        min_confirmed_cell_ratio=min_confirmed_cell_ratio,
    )
    if not tables:
        return []

    y_boundaries = sorted(
        {
            0,
            height,
            *(coordinate for table in tables for coordinate in (table.bbox[1], table.bbox[3])),
        }
    )
    emitted_tables = set()
    regions: list[LayoutRegion] = []
    for top, bottom in zip(y_boundaries, y_boundaries[1:]):
        if bottom <= top:
            continue
        active = sorted(
            (table for table in tables if table.bbox[1] <= top and table.bbox[3] >= bottom),
            key=lambda table: table.bbox[0],
        )
        if not active:
            region = _crop_region(image, (0, top, width, bottom))
            if region is not None:
                regions.append(region)
            continue

        cursor_x = 0
        for table in active:
            x1, y1, x2, _ = table.bbox
            if x1 > cursor_x:
                region = _crop_region(
                    image,
                    (cursor_x, top, x1, bottom),
                )
                if region is not None:
                    regions.append(region)

            table_key = table.bbox
            if table_key not in emitted_tables and top == y1:
                table_crop = image.crop(table.bbox)
                regions.append(
                    LayoutRegion(
                        kind="table",
                        image=table_crop,
                        bbox=table.bbox,
                        table=shift_table_layout(table, -x1, -y1),
                    )
                )
                emitted_tables.add(table_key)
            cursor_x = max(cursor_x, x2)

        if cursor_x < width:
            region = _crop_region(
                image,
                (cursor_x, top, width, bottom),
            )
            if region is not None:
                regions.append(region)
    return regions


def _separator_center(separator: SeparatorCandidate) -> int:
    return int(round((separator.start + separator.end) / 2))


def _bounded_horizontal_bands(
    features: LayoutFeatures,
    *,
    min_height: int,
    max_height: int,
) -> list[tuple[int, int]]:
    height = features.height
    separators = sorted(
        (
            separator
            for separator in features.separators
            if separator.axis == "y" and separator.kind == "whitespace" and separator.strength >= 0.5
        ),
        key=_separator_center,
    )
    centers = {_separator_center(separator) for separator in separators if 0 < _separator_center(separator) < height}
    structural_centers = set()
    for component in features.components:
        left, _, right, bottom = component.bbox
        if right - left < features.width * 0.55:
            continue
        if 0 < bottom < height:
            centers.add(bottom)
            structural_centers.add(bottom)
    centers = sorted(centers)
    if height <= max_height:
        return [(0, height)]

    bands: list[list[int]] = []
    cursor = 0
    while cursor < height:
        if height - cursor <= max_height:
            if bands and height - cursor < min_height:
                bands[-1][1] = height
            else:
                bands.append([cursor, height])
            break

        lower = cursor + min_height
        upper = min(height, cursor + max_height)
        structural = [center for center in structural_centers if cursor + max(80, min_height // 3) <= center <= upper]
        candidates = [center for center in centers if lower <= center <= upper]
        cut = min(structural) if structural else (max(candidates) if candidates else upper)
        if cut <= cursor:
            cut = min(height, cursor + max_height)
        bands.append([cursor, cut])
        cursor = cut

    return [(start, end) for start, end in bands if end > start]


def _cluster_x_separators(
    separators: Iterable[SeparatorCandidate],
    *,
    tolerance: int,
) -> list[list[SeparatorCandidate]]:
    clusters: list[list[SeparatorCandidate]] = []
    for separator in sorted(separators, key=_separator_center):
        if not clusters:
            clusters.append([separator])
            continue
        cluster_center = sum(_separator_center(value) for value in clusters[-1]) / len(clusters[-1])
        if abs(_separator_center(separator) - cluster_center) <= tolerance:
            clusters[-1].append(separator)
        else:
            clusters.append([separator])
    return clusters


def _interval_coverage(
    intervals: Iterable[tuple[int, int]],
    *,
    start: int,
    end: int,
) -> float:
    clipped = sorted(
        (
            max(start, left),
            min(end, right),
        )
        for left, right in intervals
        if right > start and left < end
    )
    if not clipped:
        return 0.0

    total = 0
    current_start, current_end = clipped[0]
    for left, right in clipped[1:]:
        if left <= current_end:
            current_end = max(current_end, right)
            continue
        total += max(0, current_end - current_start)
        current_start, current_end = left, right
    total += max(0, current_end - current_start)
    return total / max(1, end - start)


def _vertical_cuts_for_band(
    features: LayoutFeatures,
    *,
    top: int,
    bottom: int,
    min_cell_width: int,
    min_coverage: float,
) -> list[int]:
    candidates = [
        separator
        for separator in features.separators
        if separator.axis == "x"
        and separator.kind == "whitespace"
        and separator.strength >= 0.5
        and separator.span_end > top
        and separator.span_start < bottom
    ]
    clusters = _cluster_x_separators(
        candidates,
        tolerance=max(4, int(features.width * 0.015)),
    )
    summaries = []
    for cluster in clusters:
        coverage = _interval_coverage(
            ((separator.span_start, separator.span_end) for separator in cluster),
            start=top,
            end=bottom,
        )
        if coverage < min_coverage:
            continue
        center = int(round(sum(_separator_center(separator) for separator in cluster) / len(cluster)))
        gap_width = float(np.median([separator.end - separator.start for separator in cluster]))
        if gap_width < max(8, features.width * 0.01):
            continue
        summaries.append((center, gap_width, coverage))

    if not summaries:
        return []

    def crosses_wide_component(cut: int) -> bool:
        for component in features.components:
            left, component_top, right, component_bottom = component.bbox
            overlap = min(bottom, component_bottom) - max(
                top,
                component_top,
            )
            if overlap <= 0:
                continue
            if left < cut < right and right - left >= features.width * 0.35:
                return True
        return False

    strongest = max(gap_width * coverage for _, gap_width, coverage in summaries)
    cuts = [
        center
        for center, gap_width, coverage in summaries
        if gap_width * coverage >= strongest * 0.65 and not crosses_wide_component(center)
    ]
    selected: list[int] = []
    previous = 0
    for cut in sorted(cuts):
        if cut - previous < min_cell_width:
            continue
        if features.width - cut < min_cell_width:
            continue
        selected.append(cut)
        previous = cut
    return selected


def _spatial_image_regions(
    image: Image.Image,
    features: LayoutFeatures,
    stage: LayoutStageSpec,
) -> list[LayoutRegion]:
    parameters = dict(stage.parameters)
    max_region_height = max(
        200,
        int(_number(parameters, "max_region_height", 1400)),
    )
    min_region_height = max(
        80,
        min(
            max_region_height,
            int(_number(parameters, "min_region_height", 300)),
        ),
    )
    min_cell_width = max(
        40,
        int(
            _number(
                parameters,
                "min_region_width",
                max(80, features.width * 0.08),
            )
        ),
    )
    min_separator_coverage = min(
        1.0,
        max(
            0.05,
            _number(parameters, "min_separator_coverage", 0.55),
        ),
    )

    regions: list[LayoutRegion] = []
    for top, bottom in _bounded_horizontal_bands(
        features,
        min_height=min_region_height,
        max_height=max_region_height,
    ):
        cuts = _vertical_cuts_for_band(
            features,
            top=top,
            bottom=bottom,
            min_cell_width=min_cell_width,
            min_coverage=min_separator_coverage,
        )
        x_boundaries = [0, *cuts, features.width]
        for left, right in zip(x_boundaries, x_boundaries[1:]):
            region = _crop_region(
                image,
                (left, top, right, bottom),
            )
            if region is not None:
                regions.append(region)
    return regions


def execute_layout_decision(
    image: Image.Image,
    features: LayoutFeatures,
    decision: LayoutDecision,
    *,
    min_confirmed_cell_ratio: float,
) -> list[LayoutRegion]:
    if not decision.stages:
        return [
            LayoutRegion(
                kind="image",
                image=image,
                bbox=(0, 0, *image.size),
            )
        ]

    stage = decision.stages[0]
    if stage.name == "table_regions":
        regions = _partition_around_tables(
            image,
            min_confirmed_cell_ratio=min_confirmed_cell_ratio,
        )
        return regions or [
            LayoutRegion(
                kind="image",
                image=image,
                bbox=(0, 0, *image.size),
            )
        ]
    if stage.name == "spatial_regions":
        table_regions = _partition_around_tables(
            image,
            min_confirmed_cell_ratio=min_confirmed_cell_ratio,
        )
        if table_regions:
            return table_regions
        regions = _spatial_image_regions(image, features, stage)
        return regions or [
            LayoutRegion(
                kind="image",
                image=image,
                bbox=(0, 0, *image.size),
            )
        ]

    raise ValueError(f"Unknown layout stage '{stage.name}'")
