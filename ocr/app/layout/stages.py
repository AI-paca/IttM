from collections.abc import Iterable
from dataclasses import dataclass, replace

import numpy as np
from PIL import Image

from app.chunking.vertical import (
    LayoutRegion,
    TableCell,
    TableLayout,
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
from app.layout.recursive_grid import (
    RecursiveGridConfig,
    RegionDecision,
    analyze_recursive_grid,
    group_recursive_leaves,
    prepare_recursive_region,
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


def _bool(
    parameters: dict[str, FeatureValue],
    name: str,
    default: bool,
) -> bool:
    value = parameters.get(name, default)
    return value if isinstance(value, bool) else default


def _has_visible_content(image: Image.Image) -> bool:
    gray = np.asarray(image.convert("L"))
    return bool(np.mean(gray < 245) > 0.0005)


def _bbox_area(bbox: tuple[int, int, int, int]) -> int:
    left, top, right, bottom = bbox
    return max(0, right - left) * max(0, bottom - top)


def _bbox_intersection_area(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int],
) -> int:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    return _bbox_area((left, top, right, bottom))


def _bbox_overlap_ratio(
    source: tuple[int, int, int, int],
    target: tuple[int, int, int, int],
) -> float:
    area = _bbox_area(source)
    if area <= 0:
        return 0.0
    return _bbox_intersection_area(source, target) / area


def _crop_region(
    image: Image.Image,
    bbox: tuple[int, int, int, int],
    *,
    metadata: dict[str, object] | None = None,
) -> LayoutRegion | None:
    crop = image.crop(bbox)
    if not _has_visible_content(crop):
        crop.close()
        return None
    return LayoutRegion(
        kind="image",
        image=crop,
        bbox=bbox,
        metadata=metadata,
    )


def _table_region_with_outer_bands(
    image: Image.Image,
    table_region: LayoutRegion,
    stage: LayoutStageSpec | None = None,
) -> list[LayoutRegion]:
    """Preserve visible content above and below a detected table bbox."""

    _, top, _, bottom = table_region.bbox
    regions: list[LayoutRegion] = []

    def append_band(
        bbox: tuple[int, int, int, int],
        position: str,
    ) -> None:
        band = _crop_region(
            image,
            bbox,
            metadata={"layout_kind": "recursive_grid_outer_band", "position": position},
        )
        if band is None:
            return
        if stage is None:
            regions.append(band)
            return
        children = _recursive_grid_image_regions(band.image, stage)
        if not children:
            regions.append(band)
            return
        offset_x, offset_y = bbox[0], bbox[1]
        for child in children:
            left, child_top, right, child_bottom = child.bbox
            regions.append(
                LayoutRegion(
                    kind=child.kind,
                    image=child.image,
                    bbox=(left + offset_x, child_top + offset_y, right + offset_x, child_bottom + offset_y),
                    table=child.table,
                    metadata=_shift_region_metadata(child.metadata, offset_x, offset_y),
                )
            )
        if band.image is not image:
            band.image.close()

    if top > 0:
        append_band((0, 0, image.width, top), "above")
    regions.append(table_region)
    if bottom < image.height:
        append_band((0, bottom, image.width, image.height), "below")
    return regions


def _foreground_mask_numpy(image: Image.Image) -> np.ndarray:
    gray = np.asarray(image.convert("L"))
    dark_limit = int(
        min(
            210,
            max(75, float(np.quantile(gray, 0.10)) + 35),
        )
    )
    light_limit = int(
        max(
            180,
            min(245, float(np.quantile(gray, 0.90)) - 20),
        )
    )
    candidates = (
        gray <= dark_limit,
        gray < 180,
        gray < 210,
        gray >= light_limit,
        gray > 245,
    )

    def score(mask: np.ndarray) -> tuple[int, float]:
        ratio = float(np.mean(mask))
        return int(0.0005 <= ratio <= 0.55), -abs(ratio - 0.10)

    return max(candidates, key=score)


def _group_true_indexes(values: np.ndarray) -> list[tuple[int, int]]:
    indexes = np.flatnonzero(values)
    if not indexes.size:
        return []
    groups: list[tuple[int, int]] = []
    start = int(indexes[0])
    previous = start
    for raw_index in indexes[1:]:
        index = int(raw_index)
        if index == previous + 1:
            previous = index
            continue
        groups.append((start, previous + 1))
        start = index
        previous = index
    groups.append((start, previous + 1))
    return groups


def _merge_short_intervals(
    intervals: list[tuple[int, int]],
    *,
    minimum: int,
) -> list[tuple[int, int]]:
    if not intervals:
        return []
    merged: list[list[int]] = [[intervals[0][0], intervals[0][1]]]
    for start, end in intervals[1:]:
        if merged[-1][1] - merged[-1][0] < minimum:
            merged[-1][1] = end
            continue
        merged.append([start, end])
    if len(merged) > 1 and merged[-1][1] - merged[-1][0] < minimum:
        merged[-2][1] = merged[-1][1]
        merged.pop()
    return [(start, end) for start, end in merged if end > start]


@dataclass(frozen=True)
class _SparseGridCell:
    row: int
    col: int
    bbox: tuple[int, int, int, int]
    occupied: bool = True
    soft_left_tracks: tuple[int, ...] = ()
    content_bbox: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class _SparseGrid:
    x_lines: tuple[int, ...]
    y_lines: tuple[int, ...]
    cells: tuple[_SparseGridCell, ...]

    def occupied_boxes(self) -> list[tuple[int, int, int, int]]:
        return [cell.bbox for cell in self.cells if cell.occupied]


def _blank_axis_cuts(
    mask: np.ndarray,
    *,
    axis: int,
    start: int,
    end: int,
    min_gap: int,
    min_span: int,
    line_density: float,
) -> list[int]:
    return [
        cut
        for cut, _, _ in _axis_cut_candidates(
            mask,
            axis=axis,
            start=start,
            end=end,
            min_gap=min_gap,
            min_span=min_span,
            line_density=line_density,
        )
    ]


def _axis_cut_candidates(
    mask: np.ndarray,
    *,
    axis: int,
    start: int,
    end: int,
    min_gap: int,
    min_span: int,
    line_density: float,
) -> list[tuple[int, str, int]]:
    if end - start < min_span * 2:
        return []
    sliced = mask[start:end, :] if axis == 1 else mask[:, start:end]
    density = np.mean(sliced, axis=axis)
    blank_threshold = max(0.001, min(0.01, float(np.quantile(density, 0.20))))
    blank_groups = _group_true_indexes(density <= blank_threshold)
    candidates = [
        (start + (group_start + group_end) // 2, "whitespace", group_end - group_start)
        for group_start, group_end in blank_groups
        if group_end - group_start >= min_gap and group_start >= min_span and (end - start) - group_end >= min_span
    ]

    # Table-like screenshots often expose dark rule rows/columns instead of
    # white gutters. Treat long dense rules as candidate boundaries too; OCR
    # will read the neighboring cells/blocks, not the rule pixels.
    line_groups = _group_true_indexes(density >= line_density)
    candidates.extend(
        (start + (group_start + group_end) // 2, "ink", group_end - group_start)
        for group_start, group_end in line_groups
        if group_end - group_start <= max(10, min_gap)
        and group_start >= min_span
        and (end - start) - group_end >= min_span
    )
    by_cut: dict[int, tuple[int, str, int]] = {}
    for cut, kind, span in candidates:
        previous = by_cut.get(cut)
        if previous is None or previous[1] != "ink":
            by_cut[cut] = (cut, kind, span)
    return [by_cut[cut] for cut in sorted(by_cut)]


def _has_content_on_both_sides(
    mask: np.ndarray,
    *,
    cut: int,
    axis: int,
    min_span: int,
) -> bool:
    if axis == 0:
        before = mask[:, :cut]
        after = mask[:, cut:]
    else:
        before = mask[:cut, :]
        after = mask[cut:, :]
    return (
        before.shape[axis] >= min_span
        and after.shape[axis] >= min_span
        and float(np.mean(before)) > 0.0005
        and float(np.mean(after)) > 0.0005
    )


def _logical_row_count(mask: np.ndarray) -> int:
    row_density = np.mean(mask, axis=1)
    threshold = max(0.001, min(0.02, float(np.quantile(row_density, 0.55))))
    groups = _group_true_indexes(row_density > threshold)
    merged: list[list[int]] = []
    for start, end in groups:
        if not merged or start - merged[-1][1] > 6:
            merged.append([start, end])
        else:
            merged[-1][1] = end
    return len([1 for start, end in merged if end - start >= 3])


def _recursive_grid_boxes(
    mask: np.ndarray,
    bbox: tuple[int, int, int, int],
    *,
    depth: int,
    max_depth: int,
    min_region_height: int,
    max_region_height: int,
    min_region_width: int,
    min_gap: int,
) -> list[tuple[int, int, int, int]]:
    left, top, right, bottom = bbox
    width = right - left
    height = bottom - top
    if depth >= max_depth or width <= min_region_width or height <= min_region_height:
        return [bbox]

    local = mask[top:bottom, left:right]
    if not bool(np.mean(local) > 0.0005):
        return []

    horizontal_cuts = _blank_axis_cuts(
        local,
        axis=1,
        start=0,
        end=height,
        min_gap=min_gap,
        min_span=min_region_height,
        line_density=0.35,
    )
    if height > max_region_height and not horizontal_cuts:
        horizontal_cuts = list(range(max_region_height, height, max_region_height))
    if horizontal_cuts:
        intervals = _merge_short_intervals(
            [
                (top + start, top + end)
                for start, end in zip(
                    (0, *horizontal_cuts),
                    (*horizontal_cuts, height),
                )
                if end > start
            ],
            minimum=min_region_height,
        )
        boxes: list[tuple[int, int, int, int]] = []
        for child_top, child_bottom in intervals:
            boxes.extend(
                _recursive_grid_boxes(
                    mask,
                    (left, child_top, right, child_bottom),
                    depth=depth + 1,
                    max_depth=max_depth,
                    min_region_height=min_region_height,
                    max_region_height=max_region_height,
                    min_region_width=min_region_width,
                    min_gap=min_gap,
                )
            )
        return boxes or [bbox]

    vertical_candidates = _axis_cut_candidates(
        local,
        axis=0,
        start=0,
        end=width,
        min_gap=max(min_gap, int(width * 0.012)),
        min_span=min_region_width,
        line_density=0.72,
    )
    vertical_cuts = [
        cut
        for cut, kind, span in vertical_candidates
        if _has_content_on_both_sides(
            local,
            cut=cut,
            axis=0,
            min_span=min_region_width,
        )
        and kind == "ink"
    ]
    if vertical_cuts:
        intervals = _merge_short_intervals(
            [
                (left + start, left + end)
                for start, end in zip(
                    (0, *vertical_cuts),
                    (*vertical_cuts, width),
                )
                if end > start
            ],
            minimum=min_region_width,
        )
        boxes = []
        for child_left, child_right in intervals:
            boxes.extend(
                _recursive_grid_boxes(
                    mask,
                    (child_left, top, child_right, bottom),
                    depth=depth + 1,
                    max_depth=max_depth,
                    min_region_height=min_region_height,
                    max_region_height=max_region_height,
                    min_region_width=min_region_width,
                    min_gap=min_gap,
                )
            )
        return boxes or [bbox]

    return [bbox]


def _recursive_grid_image_regions(
    image: Image.Image,
    stage: LayoutStageSpec,
) -> list[LayoutRegion]:
    config = recursive_grid_config(stage)
    analysis = analyze_recursive_grid(image, config)
    if not analysis.leaves:
        return []

    ordered = analysis.leaves
    shadow = analysis.projection
    shadow_profile = analysis.profile
    projection_by_leaf = {id(leaf): (anchor, codes) for leaf, anchor, codes in shadow.leaf_projection}
    y_lines = [0, image.height]
    for leaf in ordered:
        y_lines.extend((leaf.source_bbox[1], leaf.source_bbox[3]))
    grid_x_lines = shadow.x_tracks
    grid_y_lines = tuple(sorted(set(max(0, min(image.height, line)) for line in y_lines)))

    table_regions: list[tuple[set[int], LayoutRegion]] = []
    claimed_leaf_ids: set[int] = set()

    def detect_group_table(
        members,
        bbox,
        *,
        horizontal_coverage: float,
    ) -> None:
        member_ids = {id(leaf) for leaf in members}
        if not member_ids or member_ids & claimed_leaf_ids:
            return
        left, top, right, bottom = bbox
        group_image = image.crop(bbox)
        simple_table = _simple_track_table_layout(group_image)
        if simple_table is not None and (
            simple_table.cols < 3
            or not _simple_group_table_has_spanning_rules(
                group_image,
                simple_table,
            )
        ):
            simple_table = None
        gradient_table = _gradient_table_layout(
            group_image,
            horizontal_coverage=horizontal_coverage,
        )
        table = _prefer_recursive_table_layout(
            simple_table,
            gradient_table,
            group_image.size,
        )
        if table is None or _is_gutter_card_table(table):
            group_image.close()
            return
        segment_tables = _split_gradient_table_layout(table)
        appended = False
        for segment_table in segment_tables:
            x1, y1, x2, y2 = segment_table.bbox
            absolute_table_bbox = (
                left + x1,
                top + y1,
                left + x2,
                top + y2,
            )
            table_member_ids = {
                id(leaf)
                for leaf in members
                if id(leaf) not in claimed_leaf_ids
                and (
                    _bbox_overlap_ratio(
                        leaf.source_bbox,
                        absolute_table_bbox,
                    )
                    >= 0.20
                    or _bbox_overlap_ratio(
                        leaf.content_bbox or leaf.source_bbox,
                        absolute_table_bbox,
                    )
                    >= 0.35
                )
            }
            if not table_member_ids:
                continue
            table_image = group_image.crop(segment_table.bbox)
            (
                prepared_table,
                prepared_layout,
                table_decision,
            ) = _prepare_recursive_table_image(
                table_image,
                shift_table_layout(
                    segment_table,
                    -x1,
                    -y1,
                ),
                config,
            )
            if prepared_table is not table_image:
                table_image.close()
                table_image = prepared_table
            table_regions.append(
                (
                    table_member_ids,
                    LayoutRegion(
                        kind="table",
                        image=table_image,
                        bbox=absolute_table_bbox,
                        table=prepared_layout,
                        metadata={
                            "layout_kind": "recursive_grid_table",
                            "sparse_segment_kind": shadow_profile.kind,
                            "region_recursion": (table_decision.metadata(),),
                        },
                    ),
                )
            )
            claimed_leaf_ids.update(table_member_ids)
            appended = True
        group_image.close()
        if not appended:
            return

    groups = group_recursive_leaves(ordered)
    for group in groups:
        detect_group_table(
            group.leaves,
            group.bbox,
            horizontal_coverage=0.30 if shadow_profile.kind == "table" else 0.50,
        )

    if shadow_profile.kind == "table":
        rule_leaves = tuple(
            leaf for leaf in ordered if len(leaf.merge_left_tracks) >= 2 and id(leaf) not in claimed_leaf_ids
        )
        if rule_leaves:
            detect_group_table(
                rule_leaves,
                (
                    min(leaf.source_bbox[0] for leaf in rule_leaves),
                    min(leaf.source_bbox[1] for leaf in rule_leaves),
                    max(leaf.source_bbox[2] for leaf in rule_leaves),
                    max(leaf.source_bbox[3] for leaf in rule_leaves),
                ),
                horizontal_coverage=0.30,
            )

    regions = [region for _, region in table_regions]
    for leaf in ordered:
        if id(leaf) in claimed_leaf_ids:
            leaf.image.close()
            continue
        anchor, sparse_codes = projection_by_leaf[id(leaf)]
        regions.append(
            LayoutRegion(
                kind="image",
                image=leaf.image,
                bbox=leaf.source_bbox,
                metadata={
                    "layout_kind": "recursive_grid_cell",
                    "grid_row": anchor[0],
                    "grid_col": anchor[1],
                    "grid_x_left_tracks": grid_x_lines,
                    "grid_y_lines": grid_y_lines,
                    "soft_left_tracks": leaf.left_tracks,
                    "list_marker": leaf.dash_track is not None,
                    "content_bbox": leaf.content_bbox,
                    "source_bbox": leaf.source_bbox,
                    "aligned_size": leaf.image.size,
                    "structural_only": _is_structural_only_leaf(
                        leaf,
                        image.size,
                    ),
                    "sparse_codes": sparse_codes,
                    "sparse_shape": (shadow.rows, shadow.cols),
                    "region_recursion": tuple(decision.metadata() for decision in leaf.decisions),
                },
            )
        )
    return _normalize_recursive_output_regions(regions, image)


def _is_structural_only_leaf(
    leaf,
    page_size: tuple[int, int],
) -> bool:
    page_width, page_height = page_size
    left, top, right, bottom = leaf.content_bbox
    content_width = max(0, right - left)
    content_height = max(0, bottom - top)
    near_horizontal_edge = leaf.source_bbox[1] <= 0 or leaf.source_bbox[3] >= page_height
    thin_edge_ink = near_horizontal_edge and content_height <= max(4, round(page_height * 0.01))
    tiny_footer = (
        leaf.source_bbox[3] >= round(page_height * 0.90)
        and content_width <= max(12, round(page_width * 0.03))
        and (left <= round(page_width * 0.05) or right >= round(page_width * 0.95))
    )
    return thin_edge_ink or tiny_footer


def recursive_grid_config(
    stage: LayoutStageSpec,
) -> RecursiveGridConfig:
    parameters = dict(stage.parameters)
    min_cell_height = max(
        16,
        int(_number(parameters, "min_cell_height", 24)),
    )
    min_gap = max(
        4,
        int(_number(parameters, "min_separator_gap", 8)),
    )
    preprocess_steps = ("recursive_page_dewarp",) if _bool(parameters, "region_page_dewarp", True) else ()
    return RecursiveGridConfig(
        max_depth=max(
            1,
            int(_number(parameters, "max_depth", 32)),
        ),
        max_region_height=max(
            min_cell_height * 2,
            int(_number(parameters, "max_region_height", 1400)),
        ),
        min_cell_height=min_cell_height,
        min_separator_gap=min_gap,
        overlap=max(
            min_gap,
            int(_number(parameters, "chunk_overlap", 16)),
        ),
        deskew=_bool(parameters, "region_deskew", True),
        preprocess_steps=preprocess_steps,
    )


def _table_layout_after_recursive_preparation(
    image: Image.Image,
) -> TableLayout | None:
    simple = _simple_track_table_layout(image)
    gradient = _gradient_table_layout(
        image,
        horizontal_coverage=0.30,
    )
    return _prefer_recursive_table_layout(simple, gradient, image.size)


def _prefer_recursive_table_layout(
    simple: TableLayout | None,
    gradient: TableLayout | None,
    image_size: tuple[int, int],
) -> TableLayout | None:
    if simple is None:
        return gradient
    if gradient is None:
        return simple

    image_width, image_height = image_size
    gradient_width = gradient.bbox[2] - gradient.bbox[0]
    gradient_height = gradient.bbox[3] - gradient.bbox[1]
    gradient_coverage = gradient_width * gradient_height / max(1, image_width * image_height)
    simple_oversegments_rows = (
        simple.cols == gradient.cols
        and simple.cols >= 4
        and simple.rows >= gradient.rows + 4
        and simple.rows >= int(np.ceil(gradient.rows * 1.5))
    )
    if simple_oversegments_rows and gradient_coverage >= 0.35:
        return gradient
    return simple


def _is_gutter_card_table(table: TableLayout) -> bool:
    if table.cols != 3 or len(table.x_lines) != 4:
        return False
    width = table.bbox[2] - table.bbox[0]
    if width <= 0:
        return False
    column_widths = [right - left for left, right in zip(table.x_lines, table.x_lines[1:])]
    if len(column_widths) != 3:
        return False
    middle = column_widths[1]
    left, right = column_widths[0], column_widths[2]
    return middle <= max(64, round(width * 0.08)) and left >= width * 0.25 and right >= width * 0.25


def _full_page_table_lacks_early_vertical_support(
    image: Image.Image,
    table: TableLayout,
) -> bool:
    image_width, image_height = image.size
    left, top, right, bottom = table.bbox
    coverage = (right - left) * (bottom - top) / max(1, image_width * image_height)
    if coverage < 0.90 or table.rows < 12 or table.cols < 4 or len(table.x_lines) < 3:
        return False

    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    vertical_edges = (
        np.max(
            np.abs(rgb[:, 1:] - rgb[:, :-1]),
            axis=2,
        )
        >= 8
    )
    early_bottom = max(1, min(vertical_edges.shape[0], int(image_height * 0.20)))
    supported = 0
    for x in table.x_lines[1:-1]:
        edge_x = max(0, min(vertical_edges.shape[1] - 1, x - 1))
        strip = vertical_edges[
            :early_bottom,
            max(0, edge_x - 2) : min(vertical_edges.shape[1], edge_x + 3),
        ]
        if strip.size and float(np.mean(strip)) >= 0.12:
            supported += 1
    internal_count = len(table.x_lines) - 2
    return supported * 2 < internal_count


def _simple_group_table_has_spanning_rules(
    image: Image.Image,
    table: TableLayout,
) -> bool:
    internal_lines = table.x_lines[1:-1]
    if len(internal_lines) < 2:
        return False
    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    if rgb.shape[0] < 8 or rgb.shape[1] < 2:
        return False
    vertical_edges = (
        np.max(
            np.abs(rgb[:, 1:] - rgb[:, :-1]),
            axis=2,
        )
        >= 8
    )
    quarter = max(1, vertical_edges.shape[0] // 4)
    supported = 0
    for x in internal_lines:
        edge_x = max(0, min(vertical_edges.shape[1] - 1, x - 1))
        strip = vertical_edges[
            :,
            max(0, edge_x - 2) : min(
                vertical_edges.shape[1],
                edge_x + 3,
            ),
        ]
        if strip.size == 0:
            continue
        top = float(np.mean(strip[:quarter]))
        bottom = float(np.mean(strip[-quarter:]))
        if min(top, bottom) >= 0.36:
            supported += 1
    return supported * 2 >= len(internal_lines)


def _prepare_recursive_table_image(
    image: Image.Image,
    current_table: TableLayout,
    config: RecursiveGridConfig,
) -> tuple[Image.Image, TableLayout, RegionDecision]:
    prepared, decision = prepare_recursive_region(image, config)
    transformed = prepared.size != image.size or bool(decision.preprocess_steps) or abs(decision.deskew_angle) >= 0.05
    if not transformed:
        prepared.close()
        return image, current_table, decision
    prepared_layout = _table_layout_after_recursive_preparation(prepared)
    if prepared_layout is None:
        prepared.close()
        return (
            image,
            current_table,
            replace(
                decision,
                preprocess_steps=(),
                deskew_angle=0.0,
            ),
        )
    return prepared, prepared_layout, decision


def _normalize_recursive_output_regions(
    regions: list[LayoutRegion],
    image: Image.Image,
) -> list[LayoutRegion]:
    normalized_regions = [
        split_region
        for region in regions
        for split_region in _split_table_region_by_vertical_rules(
            _collapse_weak_table_region_columns(region),
        )
    ]
    return _merge_adjacent_recursive_tables(normalized_regions, image)


def _split_table_region_by_vertical_rules(
    region: LayoutRegion,
) -> tuple[LayoutRegion, ...]:
    if region.table is None or region.table.rows < 20 or region.table.cols < 3:
        return (region,)
    rgb = np.asarray(region.image.convert("RGB"), dtype=np.int16)
    vertical_mask = (
        np.max(
            np.abs(rgb[:, 1:] - rgb[:, :-1]),
            axis=2,
        )
        >= 8
    )
    full_x_lines = region.table.x_lines
    midpoint_x_lines = (
        full_x_lines[0],
        (full_x_lines[0] + full_x_lines[-1]) // 2,
        full_x_lines[-1],
    )

    def coverage(x: int, top: int, bottom: int) -> float:
        left = max(0, x - 2)
        right = min(vertical_mask.shape[1], x + 2)
        if right <= left or bottom <= top:
            return 0.0
        return float(np.mean(vertical_mask[top:bottom, left:right]))

    row_kinds: list[int] = []
    for top, bottom in zip(region.table.y_lines, region.table.y_lines[1:]):
        bounded_top = max(0, min(vertical_mask.shape[0], top))
        bounded_bottom = max(bounded_top + 1, min(vertical_mask.shape[0], bottom))
        full_coverages = [coverage(x, bounded_top, bounded_bottom) for x in full_x_lines[1:-1]]
        if full_coverages and min(full_coverages) >= 0.35:
            row_kinds.append(region.table.cols)
        elif coverage(midpoint_x_lines[1], bounded_top, bounded_bottom) >= 0.35:
            row_kinds.append(2)
        else:
            row_kinds.append(0)

    runs: list[tuple[int, int, int]] = []
    start = 0
    for index, kind in enumerate(row_kinds[1:], start=1):
        if kind == row_kinds[start]:
            continue
        runs.append((start, index, row_kinds[start]))
        start = index
    runs.append((start, len(row_kinds), row_kinds[start]))

    table_runs = [
        (start, end, kind) for start, end, kind in runs if kind >= 2 and end - start >= 3 and (end - start) * kind >= 6
    ]
    if len(table_runs) < 2:
        return (region,)

    result: list[LayoutRegion] = []
    for start, end, kind in runs:
        y_abs_top = region.bbox[1] + region.table.y_lines[start]
        y_abs_bottom = region.bbox[1] + region.table.y_lines[end]
        bbox = (
            region.bbox[0],
            y_abs_top,
            region.bbox[2],
            y_abs_bottom,
        )
        if kind < 2 or end - start < 3:
            result.append(
                LayoutRegion(
                    kind="image",
                    image=region.image.crop(
                        (
                            0,
                            region.table.y_lines[start],
                            region.image.width,
                            region.table.y_lines[end],
                        )
                    ),
                    bbox=bbox,
                    metadata={
                        **(region.metadata or {}),
                        "layout_split": "inactive_table_rows",
                    },
                )
            )
            continue
        x_lines = full_x_lines if kind == region.table.cols else midpoint_x_lines
        y_lines_abs = [region.bbox[1] + y for y in region.table.y_lines[start : end + 1]]
        local_y_lines = tuple(y - y_abs_top for y in y_lines_abs)
        cells = tuple(
            TableCell(
                row=row,
                col=col,
                bbox=(
                    x_lines[col],
                    local_y_lines[row],
                    x_lines[col + 1],
                    local_y_lines[row + 1],
                ),
            )
            for row in range(end - start)
            for col in range(kind)
        )
        result.append(
            LayoutRegion(
                kind="table",
                image=region.image.crop(
                    (
                        0,
                        region.table.y_lines[start],
                        region.image.width,
                        region.table.y_lines[end],
                    )
                ),
                bbox=bbox,
                table=TableLayout(
                    bbox=(0, 0, region.image.width, y_abs_bottom - y_abs_top),
                    rows=end - start,
                    cols=kind,
                    x_lines=tuple(x_lines),
                    y_lines=local_y_lines,
                    cells=cells,
                ),
                metadata={
                    **(region.metadata or {}),
                    "layout_split": "vertical_rule_runs",
                },
            )
        )
    if region.image is not None:
        region.image.close()
    return tuple(result)


def _collapse_weak_table_region_columns(
    region: LayoutRegion,
) -> LayoutRegion:
    if region.table is None or region.table.cols != 3 or region.table.rows > 4:
        return region
    x_lines = list(region.table.x_lines)
    if len(x_lines) != 4:
        return region
    widths = [right - left for left, right in zip(x_lines, x_lines[1:])]
    median_width = float(np.median(widths))
    if median_width <= 0:
        return region
    if max(abs(width - median_width) for width in widths) > median_width * 0.08:
        return region
    rgb = np.asarray(region.image.convert("RGB"), dtype=np.int16)
    vertical_mask = (
        np.max(
            np.abs(rgb[:, 1:] - rgb[:, :-1]),
            axis=2,
        )
        >= 8
    )

    def coverage(x: int) -> float:
        left = max(0, x - 2)
        right = min(vertical_mask.shape[1], x + 2)
        if right <= left:
            return 0.0
        return float(np.mean(vertical_mask[:, left:right]))

    internal_coverage = [coverage(x) for x in x_lines[1:-1]]
    midpoint = (x_lines[0] + x_lines[-1]) // 2
    if max(internal_coverage, default=0.0) >= 0.30 or coverage(midpoint) < 0.30:
        return region
    collapsed_x_lines = (x_lines[0], midpoint, x_lines[-1])
    cells = tuple(
        TableCell(
            row=row,
            col=col,
            bbox=(
                collapsed_x_lines[col],
                region.table.y_lines[row],
                collapsed_x_lines[col + 1],
                region.table.y_lines[row + 1],
            ),
        )
        for row in range(region.table.rows)
        for col in range(2)
    )
    return LayoutRegion(
        kind=region.kind,
        image=region.image,
        bbox=region.bbox,
        table=TableLayout(
            bbox=region.table.bbox,
            rows=region.table.rows,
            cols=2,
            x_lines=collapsed_x_lines,
            y_lines=region.table.y_lines,
            cells=cells,
        ),
        metadata={
            **(region.metadata or {}),
            "layout_column_collapse": "weak_equal_three_to_two",
        },
    )


def _merge_adjacent_recursive_tables(
    regions: list[LayoutRegion],
    image: Image.Image,
) -> list[LayoutRegion]:
    ordered = _coalesce_rule_supported_table_runs(
        _reading_order(regions),
        image,
    )
    result: list[LayoutRegion] = []
    index = 0
    while index < len(ordered):
        current = ordered[index]
        if current.table is None or index + 2 >= len(ordered):
            result.append(current)
            index += 1
            continue
        bridge = ordered[index + 1]
        following = ordered[index + 2]
        merged = _merge_table_pair_through_bridge(
            current,
            bridge,
            following,
            image,
        )
        if merged is None:
            result.append(current)
            index += 1
            continue
        for old_region in (current, bridge, following):
            if old_region.image is not image:
                old_region.image.close()
        result.append(merged)
        index += 3
    return result


def _coalesce_rule_supported_table_runs(
    regions: list[LayoutRegion],
    image: Image.Image,
) -> list[LayoutRegion]:
    result: list[LayoutRegion] = []
    index = 0
    while index < len(regions):
        match = _rule_supported_table_run_at(
            regions,
            index,
            image,
        )
        if match is None:
            result.append(regions[index])
            index += 1
            continue
        end, replacements = match
        for old_region in regions[index:end]:
            if old_region.image is not image:
                old_region.image.close()
        result.extend(replacements)
        index = end
    return result


def _rule_supported_table_run_at(
    regions: list[LayoutRegion],
    start: int,
    image: Image.Image,
) -> tuple[int, tuple[LayoutRegion, ...]] | None:
    seed_index = None
    seed = None
    for candidate_index in range(
        start,
        min(len(regions), start + 16),
    ):
        candidate = regions[candidate_index]
        if candidate.table is None or candidate.table.cols < 3:
            continue
        tracks = tuple(candidate.bbox[0] + x for x in candidate.table.x_lines)
        if all(
            _region_supports_vertical_tracks(
                region,
                tracks,
            )
            for region in regions[start:candidate_index]
        ):
            seed_index = candidate_index
            seed = candidate
            break
    if seed_index is None or seed is None or seed.table is None:
        return None

    tracks = tuple(seed.bbox[0] + x for x in seed.table.x_lines)
    end = seed_index + 1
    while end < len(regions) and _region_supports_vertical_tracks(
        regions[end],
        tracks,
    ):
        end += 1
    if start == seed_index and end == seed_index + 1:
        return None
    table_count = sum(region.table is not None and region.table.cols >= 3 for region in regions[start:end])
    if table_count < 2:
        return None

    run = regions[start:end]
    bbox = (
        min(region.bbox[0] for region in run),
        min(region.bbox[1] for region in run),
        max(region.bbox[2] for region in run),
        max(region.bbox[3] for region in run),
    )
    run_image = image.crop(bbox)
    table = _gradient_table_layout(
        run_image,
        horizontal_coverage=0.30,
    )
    if table is None or table.cols < 3:
        run_image.close()
        return None
    x1, y1, x2, y2 = table.bbox
    table_image = run_image.crop(table.bbox)
    run_image.close()
    absolute_bbox = (
        bbox[0] + x1,
        bbox[1] + y1,
        bbox[0] + x2,
        bbox[1] + y2,
    )
    replacements: list[LayoutRegion] = []
    prefix_bbox = (
        bbox[0],
        bbox[1],
        bbox[2],
        absolute_bbox[1],
    )
    prefix = _crop_region(
        image,
        prefix_bbox,
        metadata=_residual_rule_run_metadata(
            run[0],
            prefix_bbox,
        ),
    )
    if prefix is not None:
        replacements.append(prefix)
    replacements.append(
        LayoutRegion(
            kind="table",
            image=table_image,
            bbox=absolute_bbox,
            table=shift_table_layout(
                table,
                -x1,
                -y1,
            ),
            metadata={
                **(seed.metadata or {}),
                "layout_merge": "continuous_vertical_rule_run",
            },
        )
    )
    suffix_bbox = (
        bbox[0],
        absolute_bbox[3],
        bbox[2],
        bbox[3],
    )
    suffix = _crop_region(
        image,
        suffix_bbox,
        metadata=_residual_rule_run_metadata(
            run[-1],
            suffix_bbox,
        ),
    )
    if suffix is not None:
        replacements.append(suffix)
    return end, tuple(replacements)


def _residual_rule_run_metadata(
    source: LayoutRegion,
    bbox: tuple[int, int, int, int],
) -> dict[str, object]:
    metadata = {
        **(source.metadata or {}),
        "layout_kind": "recursive_grid_cell",
        "layout_merge_residual": True,
        "source_bbox": bbox,
        "content_bbox": bbox,
        "sparse_codes": (),
        "structural_only": False,
    }
    return metadata


def _region_supports_vertical_tracks(
    region: LayoutRegion,
    absolute_tracks: tuple[int, ...],
) -> bool:
    internal_tracks = absolute_tracks[1:-1]
    if len(internal_tracks) < 2:
        return False
    rgb = np.asarray(region.image.convert("RGB"), dtype=np.int16)
    if rgb.shape[1] < 2:
        return False
    vertical_edges = (
        np.max(
            np.abs(rgb[:, 1:] - rgb[:, :-1]),
            axis=2,
        )
        >= 8
    )
    supported = 0
    for absolute_x in internal_tracks:
        local_x = absolute_x - region.bbox[0] - 1
        if not 0 <= local_x < vertical_edges.shape[1]:
            continue
        strip = vertical_edges[
            :,
            max(0, local_x - 2) : min(
                vertical_edges.shape[1],
                local_x + 3,
            ),
        ]
        if strip.size and float(np.mean(strip)) >= 0.24:
            supported += 1
    return supported * 2 >= len(internal_tracks)


def _merge_table_pair_through_bridge(
    first: LayoutRegion,
    bridge: LayoutRegion,
    second: LayoutRegion,
    image: Image.Image,
) -> LayoutRegion | None:
    if first.table is None or second.table is None or bridge.table is not None:
        return None
    if first.table.cols != second.table.cols:
        return None
    if first.table.cols < 2:
        return None
    metadata = bridge.metadata or {}
    if metadata.get("layout_kind") != "recursive_grid_cell":
        return None
    if not _bridge_spans_table_width(first, bridge, second):
        return None
    if bridge.bbox[3] <= first.bbox[3] or bridge.bbox[1] >= second.bbox[1]:
        return None
    gap = second.bbox[1] - first.bbox[3]
    first_row_heights = [bottom - top for top, bottom in zip(first.table.y_lines, first.table.y_lines[1:])]
    median_row_height = float(np.median(first_row_heights)) if first_row_heights else 48.0
    if gap > max(120, int(round(median_row_height * 1.8))):
        return None
    if bridge.bbox[3] - bridge.bbox[1] > max(96, int(round(median_row_height * 1.5))):
        return None
    left_delta = abs(first.bbox[0] - second.bbox[0])
    right_delta = abs(first.bbox[2] - second.bbox[2])
    if max(left_delta, right_delta) > max(8, (first.bbox[2] - first.bbox[0]) // 120):
        return None

    first_x = tuple(first.bbox[0] + x for x in first.table.x_lines)
    second_x = tuple(second.bbox[0] + x for x in second.table.x_lines)
    if len(first_x) != len(second_x):
        return None
    tolerance = max(8, (first.bbox[2] - first.bbox[0]) // 120)
    if any(abs(left - right) > tolerance for left, right in zip(first_x, second_x)):
        return None

    y_abs = [first.bbox[1] + y for y in first.table.y_lines]
    bridge_bottom = second.bbox[1]
    if bridge_bottom <= y_abs[-1]:
        return None
    y_abs.append(bridge_bottom)
    y_abs.extend(second.bbox[1] + y for y in second.table.y_lines[1:])
    x_abs = first_x
    bbox = (
        x_abs[0],
        y_abs[0],
        x_abs[-1],
        y_abs[-1],
    )
    x_lines = tuple(x - bbox[0] for x in x_abs)
    y_lines = tuple(y - bbox[1] for y in y_abs)
    rows = len(y_lines) - 1
    cols = first.table.cols
    cells = tuple(
        TableCell(
            row=row,
            col=col,
            bbox=(
                x_lines[col],
                y_lines[row],
                x_lines[col + 1],
                y_lines[row + 1],
            ),
        )
        for row in range(rows)
        for col in range(cols)
    )
    return LayoutRegion(
        kind="table",
        image=image.crop(bbox),
        bbox=bbox,
        table=TableLayout(
            bbox=(0, 0, bbox[2] - bbox[0], bbox[3] - bbox[1]),
            rows=rows,
            cols=cols,
            x_lines=x_lines,
            y_lines=y_lines,
            cells=cells,
        ),
        metadata={
            **(first.metadata or {}),
            "layout_merge": "adjacent_tables_through_sparse_row",
        },
    )


def _bridge_spans_table_width(
    first: LayoutRegion,
    bridge: LayoutRegion,
    second: LayoutRegion,
) -> bool:
    metadata = bridge.metadata or {}
    content_bbox = metadata.get("content_bbox")
    if not (
        isinstance(content_bbox, tuple)
        and len(content_bbox) == 4
        and all(isinstance(value, int) for value in content_bbox)
    ):
        return True
    content_width = max(0, content_bbox[2] - content_bbox[0])
    table_width = min(
        first.bbox[2] - first.bbox[0],
        second.bbox[2] - second.bbox[0],
    )
    if table_width <= 0:
        return False
    return content_width >= max(160, int(round(table_width * 0.45)))


def _sparse_grid_from_rows(
    rows: list[tuple[int, int]],
    image: Image.Image,
    *,
    min_gap: int,
) -> _SparseGrid:
    width, height = image.size
    normalized_rows = _normalize_row_intervals(rows, height)
    boxes = _merge_y_boxes(
        [(0, top, width, bottom) for top, bottom in normalized_rows],
        image.size,
        min_gap=min_gap,
    )
    mask = _foreground_mask_numpy(image)
    y_lines = [0]
    for _, top, _, bottom in boxes:
        if top > y_lines[-1]:
            y_lines.append(top)
        if bottom > y_lines[-1]:
            y_lines.append(bottom)
    if y_lines[-1] < height:
        y_lines.append(height)
    y_lines = sorted(set(max(0, min(height, line)) for line in y_lines))
    cells: list[_SparseGridCell] = []
    all_left_tracks: list[int] = [0]
    for index, (_, top, _, bottom) in enumerate(boxes):
        content_bbox, left_tracks = _fake_cell_left_tracks(
            mask,
            (0, top, width, bottom),
            min_gap=min_gap,
        )
        all_left_tracks.extend(left_tracks)
        if bottom <= top:
            continue
        cells.append(
            _SparseGridCell(
                row=index,
                col=0,
                bbox=(0, top, width, bottom),
                occupied=True,
                soft_left_tracks=tuple(left_tracks),
                content_bbox=content_bbox,
            )
        )
    return _SparseGrid(
        x_lines=tuple(_merge_near_positions(all_left_tracks, tolerance=max(3, width // 300))),
        y_lines=tuple(y_lines),
        cells=tuple(cells),
    )


def _fake_cell_left_tracks(
    mask: np.ndarray,
    bbox: tuple[int, int, int, int],
    *,
    min_gap: int,
) -> tuple[tuple[int, int, int, int], list[int]]:
    left, top, right, bottom = bbox
    local = mask[top:bottom, left:right]
    if local.size == 0:
        return bbox, [left]

    column_ink = np.count_nonzero(local, axis=0)
    threshold = max(2, int(local.shape[0] * 0.03))
    groups = _group_true_indexes(column_ink >= threshold)
    if not groups:
        return bbox, [left]

    merge_gap = max(min_gap * 4, 12, (right - left) // 90)
    merged: list[list[int]] = []
    for start, end in groups:
        if not merged or start - merged[-1][1] > merge_gap:
            merged.append([start, end])
            continue
        merged[-1][1] = end

    wide_groups = [(left + start, left + end) for start, end in merged if end - start >= max(2, min_gap // 2)]
    if not wide_groups:
        return bbox, [left]

    content_left = max(left, min(start for start, _ in wide_groups) - min_gap)
    content_right = min(right, max(end for _, end in wide_groups) + min_gap)
    left_tracks = [start for start, _ in wide_groups]
    if content_left < left_tracks[0]:
        left_tracks.insert(0, content_left)
    return (content_left, top, content_right, bottom), _merge_near_positions(
        left_tracks,
        tolerance=max(2, min_gap),
    )


def _normalize_row_intervals(
    rows: list[tuple[int, int]],
    height: int,
) -> list[tuple[int, int]]:
    ordered = sorted((max(0, min(height, top)), max(0, min(height, bottom))) for top, bottom in rows if bottom > top)
    if not ordered:
        return []

    selected: list[tuple[int, int]] = []
    for top, bottom in ordered:
        if selected:
            previous_top, previous_bottom = selected[-1]
            overlap = min(previous_bottom, bottom) - max(previous_top, top)
            smaller = min(previous_bottom - previous_top, bottom - top)
            if overlap >= smaller * 0.80:
                selected[-1] = (
                    min(previous_top, top),
                    max(previous_bottom, bottom),
                )
                continue
            if top < previous_bottom:
                split = (top + previous_bottom) // 2
                selected[-1] = (previous_top, max(previous_top + 1, split))
                top = min(bottom - 1, max(split, top))
        if bottom > top:
            selected.append((top, bottom))
    return selected


def _overlapping_y_windows(
    height: int,
    chunk_height: int,
    overlap: int,
) -> list[tuple[int, int]]:
    if height <= chunk_height:
        return [(0, height)]
    step = max(1, chunk_height - min(overlap, chunk_height - 1))
    windows = []
    top = 0
    while top < height:
        bottom = min(height, top + chunk_height)
        windows.append((top, bottom))
        if bottom >= height:
            break
        top += step
    return windows


def _content_bands_from_mask(
    mask: np.ndarray,
    *,
    min_gap: int,
    min_height: int,
    overlap: int,
) -> list[tuple[int, int]]:
    height, width = mask.shape[:2]
    if height <= 0 or width <= 0:
        return []

    row_ink = np.count_nonzero(mask, axis=1)
    content_threshold = max(2, int(width * 0.002))
    content_groups = _group_true_indexes(row_ink >= content_threshold)
    if not content_groups:
        return []

    merge_gap = max(min_gap * 3, 24)
    merged: list[list[int]] = []
    for start, end in content_groups:
        if not merged or start - merged[-1][1] > merge_gap:
            merged.append([start, end])
            continue
        merged[-1][1] = end

    # OCR must not lose ascenders/descenders at artificial horizontal cuts.
    # The overlap belongs to fake cells, not to the source image itself.
    pad = max(2, min(overlap // 4, min_gap * 2, 12))
    return [(max(0, start - pad), min(height, end + pad)) for start, end in merged if end > start]


def _merge_y_boxes(
    boxes: list[tuple[int, int, int, int]],
    image_size: tuple[int, int],
    *,
    min_gap: int,
) -> list[tuple[int, int, int, int]]:
    if not boxes:
        return []
    width, height = image_size
    normalized = sorted(
        (
            max(0, min(width, left)),
            max(0, min(height, top)),
            max(0, min(width, right)),
            max(0, min(height, bottom)),
        )
        for left, top, right, bottom in boxes
        if right > left and bottom > top
    )
    merged: list[list[int]] = []
    for left, top, right, bottom in normalized:
        if not merged:
            merged.append([left, top, right, bottom])
            continue
        previous = merged[-1]
        overlap = min(previous[3], bottom) - max(previous[1], top)
        smaller = min(previous[3] - previous[1], bottom - top)
        gap = top - previous[3]
        if overlap >= smaller * 0.55 or (0 <= gap <= max(1, min_gap // 4)):
            previous[0] = min(previous[0], left)
            previous[1] = min(previous[1], top)
            previous[2] = max(previous[2], right)
            previous[3] = max(previous[3], bottom)
            continue
        merged.append([left, top, right, bottom])
    return [(left, top, right, bottom) for left, top, right, bottom in merged if bottom > top and right > left]


def _simple_track_table_layout(image: Image.Image) -> TableLayout | None:
    width, height = image.size
    if width < 80 or height < 80:
        return None

    x_lines = _simple_table_x_lines(image)
    if len(x_lines) < 3:
        return None
    if min(right - left for left, right in zip(x_lines, x_lines[1:])) < max(24, int(width * 0.07)):
        return None

    y_lines = _simple_table_y_lines(image)
    if len(y_lines) < 6:
        return None

    rows = len(y_lines) - 1
    cols = len(x_lines) - 1
    if rows * cols < 8:
        return None
    if cols <= 2 and height >= width * 3:
        return None
    if cols <= 2 and rows >= 20:
        return None
    row_heights = [bottom - top for top, bottom in zip(y_lines, y_lines[1:])]
    median_row_height = float(np.median(row_heights))
    if median_row_height > max(96, height * 0.16):
        return None

    cells = tuple(
        TableCell(row=row, col=col, bbox=(left, top, right, bottom))
        for row, (top, bottom) in enumerate(zip(y_lines, y_lines[1:]))
        for col, (left, right) in enumerate(zip(x_lines, x_lines[1:]))
        if right - left >= 8 and bottom - top >= 8
    )
    if len(cells) < rows * cols:
        return None
    return TableLayout(
        bbox=(x_lines[0], y_lines[0], x_lines[-1], y_lines[-1]),
        rows=rows,
        cols=cols,
        x_lines=tuple(x_lines),
        y_lines=tuple(y_lines),
        cells=cells,
    )


def _gradient_table_layout(
    image: Image.Image,
    *,
    horizontal_coverage: float = 0.50,
) -> TableLayout | None:
    width, height = image.size
    if width < 160 or height < 120:
        return None

    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    vertical_edge = np.max(
        np.abs(rgb[:, 1:] - rgb[:, :-1]),
        axis=2,
    )
    horizontal_edge = np.max(
        np.abs(rgb[1:] - rgb[:-1]),
        axis=2,
    )
    contrast = 8
    coverage = 0.50
    vertical_mask = vertical_edge >= contrast
    horizontal_mask = horizontal_edge >= contrast
    x_coverage = np.mean(vertical_mask, axis=0)
    y_coverage = np.mean(horizontal_mask, axis=1)
    x_positions = [(start + end) // 2 + 1 for start, end in _group_true_indexes(x_coverage >= coverage)]
    y_positions = [(start + end) // 2 + 1 for start, end in _group_true_indexes(y_coverage >= horizontal_coverage)]
    x_lines = _merge_near_positions(
        x_positions,
        tolerance=max(6, width // 1000),
    )
    y_lines = _merge_near_positions(
        y_positions,
        tolerance=max(6, height // 1000),
    )
    if len(x_lines) < 3 or len(y_lines) < 4:
        return None

    y_lines = _refine_gradient_table_lines(
        horizontal_mask,
        seed_lines=y_lines,
        span_start=x_lines[0],
        span_end=x_lines[-1],
        line_axis=1,
        tolerance=max(6, height // 1000),
    )
    x_lines = _refine_gradient_table_lines(
        vertical_mask,
        seed_lines=x_lines,
        span_start=y_lines[0],
        span_end=y_lines[-1],
        line_axis=0,
        tolerance=max(6, width // 1000),
        constrain_to_seed=True,
    )
    x_lines = _collapse_weak_equal_three_column_lines(
        vertical_mask,
        x_lines,
        y_lines,
    )

    min_col_width = max(8, width // 500)
    min_row_height = max(8, height // 500)
    if min(right - left for left, right in zip(x_lines, x_lines[1:])) < min_col_width:
        return None
    if min(bottom - top for top, bottom in zip(y_lines, y_lines[1:])) < min_row_height:
        return None

    left, right = x_lines[0], x_lines[-1]
    top, bottom = y_lines[0], y_lines[-1]
    table_coverage = (right - left) * (bottom - top) / max(1, width * height)
    rows = len(y_lines) - 1
    cols = len(x_lines) - 1
    if table_coverage < 0.12 or rows * cols < 8 or rows > 200 or cols > 80:
        return None

    cells = tuple(
        TableCell(
            row=row,
            col=col,
            bbox=(cell_left, cell_top, cell_right, cell_bottom),
        )
        for row, (cell_top, cell_bottom) in enumerate(zip(y_lines, y_lines[1:]))
        for col, (cell_left, cell_right) in enumerate(zip(x_lines, x_lines[1:]))
    )
    return TableLayout(
        bbox=(left, top, right, bottom),
        rows=rows,
        cols=cols,
        x_lines=tuple(x_lines),
        y_lines=tuple(y_lines),
        cells=cells,
    )


def _collapse_weak_equal_three_column_lines(
    vertical_mask: np.ndarray,
    x_lines: list[int],
    y_lines: list[int],
) -> list[int]:
    if len(x_lines) != 4 or len(y_lines) < 2:
        return x_lines
    widths = [right - left for left, right in zip(x_lines, x_lines[1:])]
    median_width = float(np.median(widths))
    if median_width <= 0 or max(abs(width - median_width) for width in widths) > median_width * 0.08:
        return x_lines
    top = max(0, min(vertical_mask.shape[0], y_lines[0]))
    bottom = max(top + 1, min(vertical_mask.shape[0], y_lines[-1]))

    def coverage(x: int) -> float:
        left = max(0, x - 2)
        right = min(vertical_mask.shape[1], x + 2)
        if right <= left:
            return 0.0
        return float(np.mean(vertical_mask[top:bottom, left:right]))

    internal_coverage = [coverage(x) for x in x_lines[1:-1]]
    midpoint = (x_lines[0] + x_lines[-1]) // 2
    midpoint_coverage = coverage(midpoint)
    if max(internal_coverage, default=0.0) >= 0.30:
        return x_lines
    if midpoint_coverage < 0.30:
        return x_lines
    return [x_lines[0], midpoint, x_lines[-1]]


def _split_gradient_table_layout(
    table: TableLayout,
) -> tuple[TableLayout, ...]:
    row_heights = [bottom - top for top, bottom in zip(table.y_lines, table.y_lines[1:]) if bottom > top]
    if len(row_heights) < 8:
        return (table,)
    median_height = float(np.median(row_heights))
    if median_height <= 0:
        return (table,)

    tall = [height > max(median_height * 2.5, median_height + 96) for height in row_heights]
    gap_runs = [(start, end) for start, end in _group_true_indexes(np.asarray(tall)) if end - start >= 2]
    if not gap_runs and table.cols >= 2 and len(row_heights) >= 10:
        single_tall = [height > max(median_height * 2.25, median_height + 96) for height in row_heights]
        gap_runs = [
            (index, index + 1)
            for index, is_tall in enumerate(single_tall)
            if is_tall
            and index >= 4
            and (
                len(row_heights) - index - 1 >= 4
                or (table.cols == 2 and index >= 8 and len(row_heights) - index - 1 >= 3)
            )
        ]
    if not gap_runs:
        return (table,)

    segments: list[tuple[int, int]] = []
    start_line = 0
    for gap_start, gap_end in gap_runs:
        if gap_start - start_line >= 2:
            segments.append((start_line, gap_start))
        start_line = gap_end
    if len(table.y_lines) - 1 - start_line >= 2:
        segments.append((start_line, len(table.y_lines) - 1))
    if len(segments) < 2:
        return (table,)

    result = []
    for row_start, row_end in segments:
        rows = row_end - row_start
        cols = table.cols
        if rows < 3 or cols < 2 or rows * cols < 6:
            continue
        y_lines = table.y_lines[row_start : row_end + 1]
        bbox = (
            table.x_lines[0],
            y_lines[0],
            table.x_lines[-1],
            y_lines[-1],
        )
        cells = tuple(
            TableCell(
                row=cell.row - row_start,
                col=cell.col,
                bbox=cell.bbox,
            )
            for cell in table.cells
            if row_start <= cell.row < row_end
        )
        if len(cells) != rows * cols:
            continue
        result.append(
            TableLayout(
                bbox=bbox,
                rows=rows,
                cols=cols,
                x_lines=table.x_lines,
                y_lines=tuple(y_lines),
                cells=tuple(
                    TableCell(
                        row=cell.row,
                        col=cell.col,
                        bbox=cell.bbox,
                    )
                    for cell in cells
                ),
            )
        )
    return tuple(result) if len(result) >= 2 else (table,)


def _refine_gradient_table_lines(
    edge_mask: np.ndarray,
    *,
    seed_lines: list[int],
    span_start: int,
    span_end: int,
    line_axis: int,
    tolerance: int,
    constrain_to_seed: bool = False,
) -> list[int]:
    if len(seed_lines) < 2 or span_end <= span_start:
        return seed_lines

    if line_axis == 1:
        local = edge_mask[:, span_start:span_end]
        projection = np.mean(local, axis=1)
    else:
        local = edge_mask[span_start:span_end, :]
        projection = np.mean(local, axis=0)
    positions = [(start + end) // 2 + 1 for start, end in _group_true_indexes(projection >= 0.40)]
    candidates = _merge_near_positions(
        positions,
        tolerance=tolerance,
    )
    candidates = [
        position
        for position in candidates
        if _has_long_gradient_run(
            edge_mask,
            position=position,
            span_start=span_start,
            span_end=span_end,
            line_axis=line_axis,
        )
    ]
    if constrain_to_seed:
        candidates = [position for position in candidates if seed_lines[0] <= position <= seed_lines[-1]]

    refined = sorted(
        set(seed_lines) | {position for position in candidates if seed_lines[0] <= position <= seed_lines[-1]}
    )
    if constrain_to_seed:
        return _merge_near_positions(refined, tolerance=tolerance)

    gaps = [right - left for left, right in zip(seed_lines, seed_lines[1:]) if right > left]
    maximum_step = max(
        tolerance * 3,
        int(round(float(np.median(gaps)) * 3.0)),
        int(round(max(gaps) * 1.6)),
    )
    last = refined[-1]
    for position in candidates:
        if position <= last:
            continue
        if position - last > maximum_step:
            break
        refined.append(position)
        last = position
    return _merge_near_positions(refined, tolerance=tolerance)


def _has_long_gradient_run(
    edge_mask: np.ndarray,
    *,
    position: int,
    span_start: int,
    span_end: int,
    line_axis: int,
) -> bool:
    index = position - 1
    if line_axis == 1:
        band = edge_mask[
            max(0, index - 2) : min(edge_mask.shape[0], index + 3),
            span_start:span_end,
        ]
        run_mask = np.any(band, axis=0)
    else:
        band = edge_mask[
            span_start:span_end,
            max(0, index - 2) : min(edge_mask.shape[1], index + 3),
        ]
        run_mask = np.any(band, axis=1)
    groups = _group_true_indexes(run_mask)
    longest = max((end - start for start, end in groups), default=0)
    return longest >= max(24, int((span_end - span_start) * 0.05))


def _simple_table_x_lines(image: Image.Image) -> list[int]:
    width, height = image.size
    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    if rgb.shape[1] < 2:
        return []

    edge = np.max(np.abs(rgb[:, 1:] - rgb[:, :-1]), axis=2)
    projection = np.mean(edge, axis=0)
    if not projection.size:
        return []

    threshold = max(
        12.0,
        float(np.quantile(projection, 0.98)),
        float(np.mean(projection) + np.std(projection) * 2.25),
    )
    positions = [int(index + 1) for index in np.flatnonzero(projection >= threshold) if 4 <= index + 1 <= width - 4]
    positions = _merge_near_positions(positions, tolerance=max(3, width // 80))
    edge_threshold = max(12.0, min(40.0, threshold * 0.45))
    high_coverage_positions = [
        (start + end) // 2 + 1
        for start, end in _group_true_indexes(np.mean(edge >= edge_threshold, axis=0) >= 0.55)
        if end - start <= max(12, width // 60) and 4 <= (start + end) // 2 + 1 <= width - 4
    ]
    positions = _merge_near_positions(
        [*positions, *high_coverage_positions],
        tolerance=max(2, width // 300),
    )
    positions = [
        position
        for position in positions
        if (
            _vertical_track_strength(projection, position) >= threshold
            and _vertical_track_coverage(
                edge,
                position,
                edge_threshold=edge_threshold,
            )
            >= 0.65
        )
        or _vertical_track_coverage(
            edge,
            position,
            edge_threshold=edge_threshold,
        )
        >= 0.55
    ]
    if not positions:
        return []

    lines = [0, *positions, width]
    return _drop_tiny_line_intervals(
        _merge_near_positions(lines, tolerance=max(2, width // 160)),
        minimum=max(8, int(width * 0.04)),
    )


def _vertical_track_strength(projection: np.ndarray, position: int) -> float:
    left = max(0, position - 2)
    right = min(projection.size, position + 2)
    return float(np.max(projection[left:right])) if right > left else 0.0


def _vertical_track_coverage(edge: np.ndarray, position: int, *, edge_threshold: float) -> float:
    left = max(0, position - 2)
    right = min(edge.shape[1], position + 2)
    if right <= left:
        return 0.0
    return float(np.max(np.mean(edge[:, left:right] >= edge_threshold, axis=0)))


def _simple_table_y_lines(image: Image.Image) -> list[int]:
    width, height = image.size
    mask = _foreground_mask_numpy(image)
    row_ink = np.count_nonzero(mask, axis=1)
    if not row_ink.size:
        return []

    threshold = max(5, int(width * 0.012))
    groups = _group_true_indexes(row_ink >= threshold)
    if not groups:
        return []

    merged: list[list[int]] = []
    max_text_gap = max(2, height // 160)
    for start, end in groups:
        if not merged or start - merged[-1][1] > max_text_gap:
            merged.append([start, end])
            continue
        merged[-1][1] = end

    bands = [(start, end) for start, end in merged if end - start >= 3]
    if len(bands) < 4:
        return []

    lines = [0]
    for (_, previous_bottom), (next_top, _) in zip(bands, bands[1:]):
        lines.append((previous_bottom + next_top) // 2)
    lines.append(height)
    return _drop_tiny_line_intervals(
        _merge_near_positions(lines, tolerance=max(1, height // 240)),
        minimum=max(8, height // 80),
    )


def _merge_near_positions(positions: list[int], *, tolerance: int) -> list[int]:
    if not positions:
        return []
    ordered = sorted(set(int(position) for position in positions))
    groups: list[list[int]] = [[ordered[0]]]
    for position in ordered[1:]:
        if position - groups[-1][-1] <= tolerance:
            groups[-1].append(position)
            continue
        groups.append([position])
    return [int(round(sum(group) / len(group))) for group in groups]


def _drop_tiny_line_intervals(lines: list[int], *, minimum: int) -> list[int]:
    if len(lines) < 2:
        return lines
    kept = list(lines)
    changed = True
    while changed and len(kept) >= 3:
        changed = False
        widths = [right - left for left, right in zip(kept, kept[1:])]
        for index, width in enumerate(widths):
            if width >= minimum:
                continue
            if index == 0:
                kept.pop(1)
            elif index == len(widths) - 1:
                kept.pop(index)
            else:
                kept.pop(index if widths[index - 1] >= widths[index + 1] else index + 1)
            changed = True
            break
    return kept


def _shift_region_metadata(
    metadata: dict[str, object] | None,
    dx: int,
    dy: int,
) -> dict[str, object] | None:
    if metadata is None:
        return None
    shifted = dict(metadata)
    for key in ("soft_left_tracks", "grid_x_left_tracks"):
        value = shifted.get(key)
        if isinstance(value, tuple) and all(isinstance(item, int) for item in value):
            shifted[key] = tuple(item + dx for item in value)
    value = shifted.get("grid_y_lines")
    if isinstance(value, tuple) and all(isinstance(item, int) for item in value):
        shifted["grid_y_lines"] = tuple(item + dy for item in value)
    value = shifted.get("content_bbox")
    if isinstance(value, tuple) and len(value) == 4 and all(isinstance(item, int) for item in value):
        left, top, right, bottom = value
        shifted["content_bbox"] = (left + dx, top + dy, right + dx, bottom + dy)
    return shifted


def _is_decorative_narrow_table(
    image: Image.Image,
    table: TableLayout,
) -> bool:
    left, top, right, bottom = table.bbox
    width_ratio = max(0, right - left) / max(1, image.width)
    height_ratio = max(0, bottom - top) / max(1, image.height)
    return table.cols <= 2 and table.rows >= 20 and width_ratio < 0.15 and height_ratio >= 0.70


def _recursive_grid_regions(
    image: Image.Image,
    stage: LayoutStageSpec,
    *,
    min_confirmed_cell_ratio: float,
) -> list[LayoutRegion]:
    config = recursive_grid_config(stage)
    simple_table = _simple_track_table_layout(image)
    if simple_table is not None:
        table = _prefer_recursive_table_layout(
            simple_table,
            _gradient_table_layout(
                image,
                horizontal_coverage=0.30,
            ),
            image.size,
        )
        assert table is not None
        if not _is_decorative_narrow_table(
            image,
            table,
        ) and not _full_page_table_lacks_early_vertical_support(
            image,
            table,
        ):
            source_table_bbox = table.bbox
            table_crop = image.crop(table.bbox)
            (
                prepared_table,
                prepared_layout,
                table_decision,
            ) = _prepare_recursive_table_image(
                table_crop,
                shift_table_layout(
                    table,
                    -table.bbox[0],
                    -table.bbox[1],
                ),
                config,
            )
            if prepared_table is not table_crop:
                table_crop.close()
                table_crop = prepared_table
            table_region = LayoutRegion(
                kind="table",
                image=table_crop,
                bbox=source_table_bbox,
                table=prepared_layout,
                metadata={
                    "layout_kind": "recursive_grid_table",
                    "region_recursion": (table_decision.metadata(),),
                },
            )
            return _normalize_recursive_output_regions(
                _table_region_with_outer_bands(image, table_region, stage),
                image,
            )

    table_regions = _partition_around_tables(
        image,
        min_confirmed_cell_ratio=min_confirmed_cell_ratio,
    )
    if not table_regions:
        return _recursive_grid_image_regions(image, stage)
    if any(_is_decorative_partition_table(region, image.size) for region in table_regions if region.kind == "table"):
        for region in table_regions:
            if region.image is not image:
                region.image.close()
        return _recursive_grid_image_regions(image, stage)

    regions: list[LayoutRegion] = []
    for region in table_regions:
        if region.kind == "table":
            if _is_unreliable_partition_table(region, page_size=image.size):
                child_regions = _recursive_grid_image_regions(
                    region.image,
                    stage,
                )
                offset_x, offset_y = region.bbox[0], region.bbox[1]
                for child in child_regions:
                    left, top, right, bottom = child.bbox
                    regions.append(
                        LayoutRegion(
                            kind=child.kind,
                            image=child.image,
                            bbox=(
                                left + offset_x,
                                top + offset_y,
                                right + offset_x,
                                bottom + offset_y,
                            ),
                            table=child.table,
                            metadata=_shift_region_metadata(
                                {
                                    **(child.metadata or {}),
                                    "layout_table_rejected": ("weak_partition_vertical_support"),
                                },
                                offset_x,
                                offset_y,
                            ),
                        )
                    )
                if region.image is not image:
                    region.image.close()
                if child_regions:
                    continue
            assert region.table is not None
            (
                prepared_table,
                prepared_layout,
                table_decision,
            ) = _prepare_recursive_table_image(
                region.image,
                region.table,
                config,
            )
            if prepared_table is not region.image:
                region.image.close()
            region = LayoutRegion(
                kind="table",
                image=prepared_table,
                bbox=region.bbox,
                table=prepared_layout,
                metadata={
                    **(region.metadata or {}),
                    "layout_kind": "recursive_grid_table",
                    "region_recursion": (table_decision.metadata(),),
                },
            )
            regions.append(region)
            continue
        child_regions = _recursive_grid_image_regions(region.image, stage)
        if not child_regions:
            continue
        offset_x, offset_y = region.bbox[0], region.bbox[1]
        for child in child_regions:
            left, top, right, bottom = child.bbox
            regions.append(
                LayoutRegion(
                    kind=child.kind,
                    image=child.image,
                    bbox=(left + offset_x, top + offset_y, right + offset_x, bottom + offset_y),
                    table=child.table,
                    metadata=_shift_region_metadata(child.metadata, offset_x, offset_y),
                )
            )
        if region.image is not image:
            region.image.close()
    return _normalize_recursive_output_regions(regions, image)


def _is_unreliable_partition_table(
    region: LayoutRegion,
    *,
    page_size: tuple[int, int] | None = None,
) -> bool:
    if region.table is None:
        return False
    table = region.table
    if page_size is not None and _is_decorative_partition_table(
        region,
        page_size,
    ):
        return True
    if table.rows < 12 or table.cols < 4:
        return False

    rgb = np.asarray(region.image.convert("RGB"), dtype=np.int16)
    if rgb.size == 0:
        return False
    vertical_mask = (
        np.max(
            np.abs(rgb[:, 1:] - rgb[:, :-1]),
            axis=2,
        )
        >= 8
    )
    internal_lines = table.x_lines[1:-1]
    if not internal_lines:
        return False

    strong_rows = 0
    row_ratios = []
    for top, bottom in zip(table.y_lines, table.y_lines[1:]):
        bounded_top = max(0, min(vertical_mask.shape[0], top))
        bounded_bottom = max(
            bounded_top + 1,
            min(vertical_mask.shape[0], bottom),
        )
        supported = 0
        for x in internal_lines:
            left = max(0, x - 2)
            right = min(vertical_mask.shape[1], x + 2)
            if right <= left:
                continue
            if (
                float(
                    np.mean(
                        vertical_mask[
                            bounded_top:bounded_bottom,
                            left:right,
                        ],
                    ),
                )
                >= 0.30
            ):
                supported += 1
        ratio = supported / max(1, len(internal_lines))
        row_ratios.append(ratio)
        if supported >= max(1, len(internal_lines) // 2):
            strong_rows += 1

    strong_ratio = strong_rows / max(1, table.rows)
    median_ratio = float(np.median(row_ratios)) if row_ratios else 0.0
    return strong_ratio < 0.45 and median_ratio < 0.50


def _is_decorative_partition_table(
    region: LayoutRegion,
    page_size: tuple[int, int],
) -> bool:
    if region.table is None:
        return False
    page_width, page_height = page_size
    left, top, right, bottom = region.bbox
    return (
        region.table.cols <= 2
        and region.table.rows >= 20
        and (right - left) / max(1, page_width) < 0.15
        and (bottom - top) / max(1, page_height) >= 0.70
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
        gradient_table = _gradient_table_layout(image)
        tables = [gradient_table] if gradient_table is not None else []
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


def _is_full_page_coarse_table(region: LayoutRegion, image: Image.Image) -> bool:
    if region.kind != "table" or region.table is None:
        return False
    image_width, image_height = image.size
    left, top, right, bottom = region.bbox
    coverage = ((right - left) * (bottom - top)) / max(1, image_width * image_height)
    return coverage >= 0.85 and region.table.rows <= 12 and region.table.cols <= 12


def _separator_center(separator: SeparatorCandidate) -> int:
    return int(round((separator.start + separator.end) / 2))


def _bounded_horizontal_bands(
    features: LayoutFeatures,
    *,
    min_height: int,
    max_height: int,
) -> list[tuple[int, int]]:
    height = features.height
    component_row_bands = _component_row_bands(features, min_height=min_height)
    if height <= max_height and len(component_row_bands) >= 3:
        return component_row_bands

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


def _component_row_bands(
    features: LayoutFeatures,
    *,
    min_height: int,
) -> list[tuple[int, int]]:
    if features.foreground_ratio < 0.045:
        return []

    max_component_height = max(24, int(features.height * 0.22))
    min_component_width = max(12, int(features.width * 0.025))
    intervals = []
    for component in features.components:
        left, top, right, bottom = component.bbox
        width = right - left
        height = bottom - top
        if width < min_component_width or height <= 0:
            continue
        if height > max_component_height:
            continue
        if component.fill_ratio < 0.03:
            continue
        intervals.append((top, bottom))
    if len(intervals) < 3:
        return []

    median_height = float(np.median([bottom - top for top, bottom in intervals]))
    merge_gap = max(6, int(round(median_height * 0.45)))
    clusters: list[list[int]] = []
    for top, bottom in sorted(intervals):
        if not clusters or top > clusters[-1][1] + merge_gap:
            clusters.append([top, bottom])
            continue
        clusters[-1][1] = max(clusters[-1][1], bottom)

    if len(clusters) < 3:
        return []

    if features.foreground_ratio < 0.08:
        min_band_height = max(24, int(round(median_height * 1.2)))
    else:
        min_band_height = max(32, int(round(min_height * 0.25)))
    boundaries = [0]
    for previous, current in zip(clusters, clusters[1:]):
        cut = int(round((previous[1] + current[0]) / 2))
        if cut - boundaries[-1] < min_band_height:
            continue
        if features.height - cut < min_band_height:
            continue
        boundaries.append(cut)
    boundaries.append(features.height)

    bands = [(top, bottom) for top, bottom in zip(boundaries, boundaries[1:]) if bottom > top]
    return bands if len(bands) >= 3 else []


def _medium_horizontal_bands(
    features: LayoutFeatures,
    *,
    min_height: int,
    max_height: int,
) -> list[tuple[int, int]]:
    if features.height > max_height:
        return _bounded_horizontal_bands(
            features,
            min_height=min_height,
            max_height=max_height,
        )

    min_medium_height = max(60, min(180, min_height))
    centers = []
    for separator in features.separators:
        if separator.axis != "y" or separator.kind != "whitespace" or separator.strength < 0.65:
            continue
        if separator.span_end - separator.span_start < features.width * 0.75:
            continue
        center = _separator_center(separator)
        if 0 < center < features.height:
            centers.append(center)

    boundaries = [0]
    for center in sorted(set(centers)):
        if center - boundaries[-1] < min_medium_height:
            continue
        if features.height - center < min_medium_height:
            continue
        boundaries.append(center)
    boundaries.append(features.height)

    bands = [(top, bottom) for top, bottom in zip(boundaries, boundaries[1:]) if bottom > top]
    return bands if len(bands) > 1 else [(0, features.height)]


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
    whitespace_candidates = [
        separator
        for separator in features.separators
        if separator.axis == "x"
        and separator.span_end > top
        and separator.span_start < bottom
        and separator.kind == "whitespace"
        and separator.strength >= 0.4
    ]
    ink_candidates = [
        separator
        for separator in features.separators
        if separator.axis == "x"
        and separator.span_end > top
        and separator.span_start < bottom
        and separator.kind == "ink"
        and separator.span_end - separator.span_start >= (bottom - top) * 0.8
    ]

    def summarize_cluster(cluster: list[SeparatorCandidate]) -> tuple[int, float, float, bool, bool] | None:
        coverage = _interval_coverage(
            ((separator.span_start, separator.span_end) for separator in cluster),
            start=top,
            end=bottom,
        )
        if coverage < min_coverage:
            return None
        center = int(round(sum(_separator_center(separator) for separator in cluster) / len(cluster)))
        whitespace_widths = [separator.end - separator.start for separator in cluster if separator.kind == "whitespace"]
        ink_count = sum(1 for separator in cluster if separator.kind == "ink")
        gap_width = float(
            np.median(whitespace_widths)
            if whitespace_widths
            else np.median([separator.end - separator.start for separator in cluster])
        )
        has_whitespace_cut = bool(whitespace_widths) and gap_width >= max(
            8,
            features.width * 0.01,
        )
        has_line_pair_cut = ink_count >= 2 and coverage >= max(0.8, min_coverage)
        if not has_whitespace_cut and not has_line_pair_cut:
            return None
        has_ink_support = ink_count > 0
        return center, max(1.0, gap_width), coverage, has_line_pair_cut, has_ink_support

    strict_tolerance = max(4, int(features.width * 0.015))
    loose_tolerance = max(strict_tolerance, int(features.width * 0.05))
    summaries = []
    for cluster in _cluster_x_separators(
        [*whitespace_candidates, *ink_candidates],
        tolerance=strict_tolerance,
    ):
        summary = summarize_cluster(cluster)
        if summary is not None:
            summaries.append(summary)

    strict_centers = [center for center, _, _, _, _ in summaries]
    for cluster in _cluster_x_separators(
        whitespace_candidates,
        tolerance=loose_tolerance,
    ):
        summary = summarize_cluster(cluster)
        if summary is None:
            continue
        center, _, _, _, _ = summary
        if any(abs(center - strict_center) <= loose_tolerance for strict_center in strict_centers):
            continue
        summaries.append(summary)

    if not summaries:
        return []

    def crosses_wide_component(cut: int, *, backed_by_line_pair: bool) -> bool:
        if backed_by_line_pair:
            return False
        for component in features.components:
            left, component_top, right, component_bottom = component.bbox
            overlap = min(bottom, component_bottom) - max(
                top,
                component_top,
            )
            if overlap < (bottom - top) * 0.1:
                continue
            if component.fill_ratio <= 0.08:
                continue
            if left < cut < right and right - left >= features.width * 0.35:
                return True
        return False

    horizontal_rule_count = sum(
        1
        for separator in features.separators
        if separator.axis == "y"
        and separator.span_end > 0
        and separator.span_start < features.width
        and separator.span_end - separator.span_start >= features.width * 0.5
    )
    table_row_evidence = bottom - top >= 160 and features.foreground_ratio >= 0.08 and horizontal_rule_count >= 1
    whitespace_summary_count = sum(1 for _, _, _, backed_by_line_pair, _ in summaries if not backed_by_line_pair)
    structural_cut_count = sum(1 for _, _, _, backed_by_line_pair, _ in summaries if backed_by_line_pair)

    def supports_vertical_split(
        *,
        backed_by_line_pair: bool,
        has_ink_support: bool,
    ) -> bool:
        if backed_by_line_pair:
            return bottom - top >= 160 and features.foreground_ratio >= 0.08
        if bottom - top < 160:
            return False
        if has_ink_support:
            return features.foreground_ratio >= 0.08
        if table_row_evidence:
            return True
        if structural_cut_count >= 2:
            return True
        return features.foreground_ratio >= 0.08 and whitespace_summary_count >= 2

    cuts = [
        center
        for center, gap_width, coverage, backed_by_line_pair, has_ink_support in summaries
        if supports_vertical_split(
            backed_by_line_pair=backed_by_line_pair,
            has_ink_support=has_ink_support,
        )
        if not crosses_wide_component(center, backed_by_line_pair=backed_by_line_pair)
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
    horizontal_bands = (
        _medium_horizontal_bands(
            features,
            min_height=min_region_height,
            max_height=max_region_height,
        )
        if _bool(parameters, "medium_page_segmentation", False)
        else _bounded_horizontal_bands(
            features,
            min_height=min_region_height,
            max_height=max_region_height,
        )
    )
    for top, bottom in horizontal_bands:
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


def _reading_order(regions: list[LayoutRegion]) -> list[LayoutRegion]:
    return sorted(
        regions,
        key=lambda region: (
            region.bbox[1],
            region.bbox[0],
            region.bbox[3],
            region.bbox[2],
        ),
    )


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
    if stage.name == "recursive_grid":
        regions = _recursive_grid_regions(
            image,
            stage,
            min_confirmed_cell_ratio=min_confirmed_cell_ratio,
        )
        return _reading_order(regions) or [
            LayoutRegion(
                kind="image",
                image=image,
                bbox=(0, 0, *image.size),
            )
        ]
    if stage.name == "table_regions":
        regions = _partition_around_tables(
            image,
            min_confirmed_cell_ratio=min_confirmed_cell_ratio,
        )
        return _reading_order(regions) or [
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
        table_only_regions = [region for region in table_regions if region.kind == "table"]
        if table_regions and not (
            table_only_regions and all(_is_full_page_coarse_table(region, image) for region in table_only_regions)
        ):
            return _reading_order(table_regions)
        regions = _spatial_image_regions(image, features, stage)
        return _reading_order(regions) or [
            LayoutRegion(
                kind="image",
                image=image,
                bbox=(0, 0, *image.size),
            )
        ]

    raise ValueError(f"Unknown layout stage '{stage.name}'")
