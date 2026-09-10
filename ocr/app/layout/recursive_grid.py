from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
from PIL import Image, ImageFilter

from app.layout import sparse_codes as _sparse_codes
from app.layout.sparse_codes import (
    EMPTY_SLOT_CODE,
    MERGE_LEFT_CODE,
    MERGE_LEFT_CODES,
    MERGE_UP_CODE,
    MERGE_UP_CODES,
)
from app.preprocessing import IMAGE_PREPROCESSING_STEPS

Box = tuple[int, int, int, int]
SPARSE_SHADOW_CODES = _sparse_codes.SPARSE_SHADOW_CODES
RECURSIVE_GRID_TRACE_VERSION = 1


@dataclass(frozen=True)
class RecursiveGridConfig:
    max_depth: int = 32
    max_region_height: int = 1400
    min_cell_height: int = 24
    min_separator_gap: int = 8
    overlap: int = 16
    deskew: bool = True
    preprocess_steps: tuple[str, ...] = ("recursive_page_dewarp",)


@dataclass(frozen=True)
class RegionDecision:
    depth: int
    preprocess_steps: tuple[str, ...]
    mask_mode: str
    contrast_delta: int
    deskew_angle: float
    split: str = "leaf"

    def metadata(self) -> dict[str, object]:
        return {
            "depth": self.depth,
            "preprocess_steps": self.preprocess_steps,
            "mask_mode": self.mask_mode,
            "contrast_delta": self.contrast_delta,
            "deskew_angle": self.deskew_angle,
            "split": self.split,
        }


@dataclass(frozen=True)
class RecursiveGridLeaf:
    source_bbox: Box
    image: Image.Image
    content_bbox: Box
    left_tracks: tuple[int, ...]
    dash_track: int | None
    merge_left_tracks: tuple[int, ...]
    decisions: tuple[RegionDecision, ...]


@dataclass(frozen=True)
class RecursiveGridGroup:
    index: int
    bbox: Box
    leaves: tuple[RecursiveGridLeaf, ...]


@dataclass(frozen=True)
class SparseShadowProjection:
    codes: frozenset[tuple[int, int, int]]
    leaf_projection: tuple[
        tuple[
            RecursiveGridLeaf,
            tuple[int, int],
            tuple[tuple[int, int, int], ...],
        ],
        ...,
    ]
    x_tracks: tuple[int, ...]
    rows: int
    cols: int


@dataclass(frozen=True)
class SparseShadowSignature:
    rows: int
    cols: int
    anchors: tuple[tuple[int, int], ...]
    codes: tuple[tuple[int, int, int], ...]
    x_tracks: tuple[int, ...]


@dataclass(frozen=True)
class SparseShadowProfile:
    kind: str
    occupied_rows: int
    merge_up_rows: int
    merge_left_rows: int
    dash_rows: int


@dataclass(frozen=True)
class RecursiveGridAnalysis:
    """Immutable hand-off between recursive layout phases.

    Keeping the leaves, sparse projection, stable signature and classification
    together prevents downstream code from recomputing only part of the
    pipeline with subtly different ordering or thresholds.
    """

    leaves: tuple[RecursiveGridLeaf, ...]
    projection: SparseShadowProjection
    signature: SparseShadowSignature
    profile: SparseShadowProfile


@dataclass(frozen=True)
class RecursiveGridLeafTrace:
    source_bbox: Box
    content_bbox: Box
    left_tracks: tuple[int, ...]
    dash_track: int | None
    merge_left_tracks: tuple[int, ...]
    decisions: tuple[RegionDecision, ...]


@dataclass(frozen=True)
class RecursiveGridProjectionTrace:
    leaf_index: int
    anchor: tuple[int, int]
    codes: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True)
class RecursiveGridTrace:
    """Image-free, deterministic ABI candidate for golden tests and WASM."""

    version: int
    rows: int
    cols: int
    x_tracks: tuple[int, ...]
    codes: tuple[tuple[int, int, int], ...]
    leaves: tuple[RecursiveGridLeafTrace, ...]
    projections: tuple[RecursiveGridProjectionTrace, ...]
    profile: SparseShadowProfile


def recursive_grid_trace(analysis: RecursiveGridAnalysis) -> RecursiveGridTrace:
    leaf_indexes = {id(leaf): index for index, leaf in enumerate(analysis.leaves)}
    return RecursiveGridTrace(
        version=RECURSIVE_GRID_TRACE_VERSION,
        rows=analysis.signature.rows,
        cols=analysis.signature.cols,
        x_tracks=analysis.signature.x_tracks,
        codes=analysis.signature.codes,
        leaves=tuple(
            RecursiveGridLeafTrace(
                source_bbox=leaf.source_bbox,
                content_bbox=leaf.content_bbox,
                left_tracks=leaf.left_tracks,
                dash_track=leaf.dash_track,
                merge_left_tracks=leaf.merge_left_tracks,
                decisions=leaf.decisions,
            )
            for leaf in analysis.leaves
        ),
        projections=tuple(
            RecursiveGridProjectionTrace(
                leaf_index=leaf_indexes[id(leaf)],
                anchor=anchor,
                codes=codes,
            )
            for leaf, anchor, codes in analysis.projection.leaf_projection
        ),
        profile=analysis.profile,
    )


def analyze_recursive_grid(
    image: Image.Image,
    config: RecursiveGridConfig,
) -> RecursiveGridAnalysis:
    """Run segmentation -> sparse projection -> structural classification."""

    leaves = tuple(
        sorted(
            segment_recursive_grid(image, config),
            key=lambda leaf: (
                leaf.source_bbox[1],
                leaf.source_bbox[0],
                leaf.source_bbox[3],
            ),
        )
    )
    projection = project_sparse_shadow(list(leaves))
    signature = sparse_shadow_signature(projection)
    return RecursiveGridAnalysis(
        leaves=leaves,
        projection=projection,
        signature=signature,
        profile=classify_sparse_shadow(signature),
    )


def segment_recursive_grid(
    image: Image.Image,
    config: RecursiveGridConfig,
) -> list[RecursiveGridLeaf]:
    source = image.convert("RGB").copy()
    leaves = _segment_node(
        source,
        (0, 0, image.width, image.height),
        config=config,
        depth=0,
        history=(),
        edge_insets=(0.0, 0.0),
    )
    return _deduplicate_leaves(leaves)


def prepare_recursive_region(
    image: Image.Image,
    config: RecursiveGridConfig,
    *,
    depth: int = 0,
) -> tuple[Image.Image, RegionDecision]:
    aligned, applied_steps = _apply_region_preprocessing(
        image.convert("RGB").copy(),
        config.preprocess_steps,
    )
    mask, raw_mask, mask_mode, contrast_delta = _adaptive_foreground_mask(
        aligned,
    )
    if config.deskew:
        aligned, _, _, deskew_angle = _deskew_region(
            aligned,
            mask,
            raw_mask,
            mask_mode=mask_mode,
            contrast_delta=contrast_delta,
        )
    else:
        deskew_angle = 0.0
    return (
        aligned,
        RegionDecision(
            depth=depth,
            preprocess_steps=applied_steps,
            mask_mode=mask_mode,
            contrast_delta=contrast_delta,
            deskew_angle=deskew_angle,
        ),
    )


def project_sparse_shadow(
    leaves: list[RecursiveGridLeaf],
) -> SparseShadowProjection:
    ordered = sorted(
        leaves,
        key=lambda leaf: (
            leaf.source_bbox[1],
            leaf.source_bbox[0],
        ),
    )
    if not ordered:
        return SparseShadowProjection(
            codes=frozenset(),
            leaf_projection=(),
            x_tracks=(),
            rows=0,
            cols=0,
        )

    page_width = max(leaf.source_bbox[2] for leaf in ordered)
    groups = group_recursive_leaves(ordered)
    anchor_track_by_leaf: dict[int, int] = {}
    projection_tracks_by_leaf: dict[int, tuple[int, ...]] = {}
    for group in groups:
        group_anchor = min(_leaf_content_track(leaf) for leaf in group.leaves)
        for leaf in group.leaves:
            anchor_track_by_leaf[id(leaf)] = group_anchor
            projection_tracks_by_leaf[id(leaf)] = _merge_positions(
                (
                    group_anchor,
                    *leaf.merge_left_tracks,
                ),
                tolerance=max(2, page_width // 300),
            )
    x_tracks = _merge_positions(
        [track for leaf in ordered for track in projection_tracks_by_leaf[id(leaf)]],
        tolerance=max(4, page_width // 250),
    )
    occupied: set[tuple[int, int]] = set()
    projected_cells = []
    merge_left_cells: set[tuple[int, int]] = set()
    row = 0
    for group_index, group in enumerate(groups):
        if group_index:
            row += 1
        for leaf in group.leaves:
            tracks = projection_tracks_by_leaf[id(leaf)]
            columns = sorted(
                {
                    min(
                        range(len(x_tracks)),
                        key=lambda index: abs(x_tracks[index] - track),
                    )
                    for track in tracks
                }
            )
            cells = tuple((row, column) for column in columns)
            occupied.update(cells)
            for track in leaf.merge_left_tracks:
                merge_left_cells.add(
                    (
                        row,
                        min(
                            range(len(x_tracks)),
                            key=lambda index: abs(x_tracks[index] - track),
                        ),
                    )
                )
            anchor_column = min(
                range(len(x_tracks)),
                key=lambda index: abs(x_tracks[index] - anchor_track_by_leaf[id(leaf)]),
            )
            anchor = (row, anchor_column)
            projected_cells.append((leaf, anchor, cells))
            row += 1
    codes = set()
    payload_cells = {anchor for _, anchor, _ in projected_cells}
    cells_by_row: dict[int, list[tuple[int, int]]] = {}
    for cell in occupied:
        cells_by_row.setdefault(cell[0], []).append(cell)
    for row_cells in cells_by_row.values():
        for cell in sorted(row_cells):
            code = 0
            if (cell[0] - 1, cell[1]) in occupied:
                code += MERGE_UP_CODE
            if cell in merge_left_cells:
                code += MERGE_LEFT_CODE
            if cell not in payload_cells:
                code += EMPTY_SLOT_CODE
            if code:
                codes.add((cell[0], cell[1], code))
    projection = []
    for leaf, anchor, cells in projected_cells:
        leaf_codes = tuple(sorted(code for code in codes if (code[0], code[1]) in cells))
        projection.append((leaf, anchor, leaf_codes))
    return SparseShadowProjection(
        codes=frozenset(codes),
        leaf_projection=tuple(projection),
        x_tracks=x_tracks,
        rows=row,
        cols=len(x_tracks),
    )


def sparse_shadow_signature(
    projection: SparseShadowProjection,
) -> SparseShadowSignature:
    return SparseShadowSignature(
        rows=projection.rows,
        cols=projection.cols,
        anchors=tuple(sorted({anchor for _, anchor, _ in projection.leaf_projection})),
        codes=tuple(sorted(projection.codes)),
        x_tracks=projection.x_tracks,
    )


def classify_sparse_shadow(
    signature: SparseShadowSignature,
) -> SparseShadowProfile:
    occupied_rows = len({row for row, _ in signature.anchors})
    merge_up_rows = len({row for row, _, code in signature.codes if code in MERGE_UP_CODES})
    merge_left_rows = len({row for row, _, code in signature.codes if code in MERGE_LEFT_CODES})
    indented_rows = len({row for row, column in signature.anchors if column > 0})
    dash_rows = 0

    if (
        signature.cols >= 2
        and merge_left_rows == 0
        and indented_rows > 0
        and indented_rows * 2 >= max(1, occupied_rows)
    ):
        kind = "list"
    elif signature.cols >= 3 and merge_left_rows >= 2 and merge_left_rows * 2 >= max(1, occupied_rows):
        kind = "table"
    elif signature.cols <= 2 or merge_left_rows == 0:
        kind = "text"
    else:
        kind = "mixed"
    return SparseShadowProfile(
        kind=kind,
        occupied_rows=occupied_rows,
        merge_up_rows=merge_up_rows,
        merge_left_rows=merge_left_rows,
        dash_rows=dash_rows,
    )


def group_recursive_leaves(
    leaves: list[RecursiveGridLeaf],
) -> list[RecursiveGridGroup]:
    ordered = sorted(
        leaves,
        key=lambda leaf: (
            (leaf.source_bbox[1] + leaf.source_bbox[3]) / 2,
            leaf.source_bbox[0],
        ),
    )
    if not ordered:
        return []

    centers = [(leaf.content_bbox[1] + leaf.content_bbox[3]) / 2 for leaf in ordered]
    distances = [current - previous for previous, current in zip(centers, centers[1:]) if current > previous]
    normal_pitch = float(np.quantile(distances, 0.65)) if distances else 0.0
    content_gaps = [
        current.content_bbox[1] - previous.content_bbox[3]
        for previous, current in zip(ordered, ordered[1:])
        if current.content_bbox[1] > previous.content_bbox[3]
    ]
    normal_gap = float(np.median(content_gaps)) if content_gaps else 0.0
    gap_threshold = max(18.0, normal_gap * 2.2)
    groups: list[list[RecursiveGridLeaf]] = []
    for leaf_index, leaf in enumerate(ordered):
        starts_marker = _starts_with_marker(leaf)
        content_gap = leaf.content_bbox[1] - ordered[leaf_index - 1].content_bbox[3] if leaf_index > 0 else 0
        large_gap = leaf_index > 0 and (
            content_gap > gap_threshold
            or (normal_pitch > 0 and centers[leaf_index] - centers[leaf_index - 1] > normal_pitch * 1.55)
        )
        if not groups or starts_marker or large_gap:
            groups.append([leaf])
        else:
            groups[-1].append(leaf)

    result = []
    for index, members in enumerate(groups):
        result.append(
            RecursiveGridGroup(
                index=index,
                bbox=(
                    min(leaf.source_bbox[0] for leaf in members),
                    min(leaf.source_bbox[1] for leaf in members),
                    max(leaf.source_bbox[2] for leaf in members),
                    max(leaf.source_bbox[3] for leaf in members),
                ),
                leaves=tuple(members),
            )
        )
    return result


def _starts_with_marker(leaf: RecursiveGridLeaf) -> bool:
    if len(leaf.left_tracks) < 2:
        return False
    width = leaf.source_bbox[2] - leaf.source_bbox[0]
    if leaf.left_tracks[0] <= leaf.source_bbox[0] + max(8, width * 0.02):
        return False
    gap = leaf.left_tracks[1] - leaf.left_tracks[0]
    return gap >= max(12, width * 0.015) and gap <= max(120, width * 0.12)


def _leaf_content_track(leaf: RecursiveGridLeaf) -> int:
    excluded = {
        *leaf.merge_left_tracks,
        *((leaf.dash_track,) if leaf.dash_track is not None else ()),
    }
    return next(
        (track for track in leaf.left_tracks if track not in excluded),
        leaf.content_bbox[0],
    )


def _segment_node(
    image: Image.Image,
    source_bbox: Box,
    *,
    config: RecursiveGridConfig,
    depth: int,
    history: tuple[RegionDecision, ...],
    edge_insets: tuple[float, float],
) -> list[RecursiveGridLeaf]:
    if depth < config.max_depth and image.height > config.max_region_height:
        children = []
        parent_decision = RegionDecision(
            depth=depth,
            preprocess_steps=(),
            mask_mode="deferred",
            contrast_delta=0,
            deskew_angle=0.0,
            split="overlapping_chunks",
        )
        for top, bottom in _overlapping_windows(
            image.height,
            config.max_region_height,
            config.overlap,
        ):
            child = image.crop((0, top, image.width, bottom))
            child_bbox = _map_local_box(
                (0, top, image.width, bottom),
                image.size,
                source_bbox,
            )
            children.extend(
                _segment_node(
                    child,
                    child_bbox,
                    config=config,
                    depth=depth + 1,
                    history=history + (parent_decision,),
                    edge_insets=edge_insets,
                )
            )
        image.close()
        return children

    aligned, applied_steps = _apply_region_preprocessing(
        image,
        config.preprocess_steps,
    )
    (
        mask,
        raw_mask,
        mask_mode,
        contrast_delta,
    ) = _adaptive_foreground_mask(aligned)
    if config.deskew:
        aligned, mask, raw_mask, deskew_angle = _deskew_region(
            aligned,
            mask,
            raw_mask,
            mask_mode=mask_mode,
            contrast_delta=contrast_delta,
        )
    else:
        deskew_angle = 0.0
    decision = RegionDecision(
        depth=depth,
        preprocess_steps=applied_steps,
        mask_mode=mask_mode,
        contrast_delta=contrast_delta,
        deskew_angle=deskew_angle,
    )
    structural_mask, child_edge_insets = _horizontal_projection(
        mask,
        min_gap=config.min_separator_gap,
        inherited_insets=edge_insets,
    )

    if depth < config.max_depth:
        separator = _horizontal_separator(
            structural_mask,
            min_cell_height=config.min_cell_height,
            min_separator_gap=config.min_separator_gap,
        )
        if separator is not None:
            cut, seam_cost = separator
            overlap = 0
            upper_bottom = min(aligned.height, cut + overlap)
            lower_top = max(0, cut - overlap)
            if (
                upper_bottom >= config.min_cell_height
                and aligned.height - lower_top >= config.min_cell_height
                and lower_top > 0
                and upper_bottom < aligned.height
            ):
                split_decision = replace(
                    decision,
                    split=f"horizontal_seam:{seam_cost:.4f}",
                )
                upper = aligned.crop((0, 0, aligned.width, upper_bottom))
                lower = aligned.crop((0, lower_top, aligned.width, aligned.height))
                upper_bbox = _map_local_box(
                    (0, 0, aligned.width, upper_bottom),
                    aligned.size,
                    source_bbox,
                )
                lower_bbox = _map_local_box(
                    (0, lower_top, aligned.width, aligned.height),
                    aligned.size,
                    source_bbox,
                )
                aligned.close()
                return [
                    *_segment_node(
                        upper,
                        upper_bbox,
                        config=config,
                        depth=depth + 1,
                        history=history + (split_decision,),
                        edge_insets=child_edge_insets,
                    ),
                    *_segment_node(
                        lower,
                        lower_bbox,
                        config=config,
                        depth=depth + 1,
                        history=history + (split_decision,),
                        edge_insets=child_edge_insets,
                    ),
                ]

        vertical_separator = _vertical_lane_separator(
            structural_mask,
            min_cell_height=config.min_cell_height,
            min_gap=config.min_separator_gap,
        )
        if vertical_separator is not None:
            cut, gutter_width = vertical_separator
            split_decision = replace(
                decision,
                split=f"vertical_gutter:{gutter_width}",
            )
            left = aligned.crop((0, 0, cut, aligned.height))
            right = aligned.crop((cut, 0, aligned.width, aligned.height))
            left_bbox = _map_local_box(
                (0, 0, cut, aligned.height),
                aligned.size,
                source_bbox,
            )
            right_bbox = _map_local_box(
                (cut, 0, aligned.width, aligned.height),
                aligned.size,
                source_bbox,
            )
            boundary = _map_x(cut, aligned.width, source_bbox)
            aligned.close()
            left_leaves = _segment_node(
                left,
                left_bbox,
                config=config,
                depth=depth + 1,
                history=history + (split_decision,),
                edge_insets=(child_edge_insets[0], 0.0),
            )
            right_leaves = _segment_node(
                right,
                right_bbox,
                config=config,
                depth=depth + 1,
                history=history + (split_decision,),
                edge_insets=(0.0, child_edge_insets[1]),
            )
            boundary_tolerance = max(
                2,
                (source_bbox[2] - source_bbox[0]) // 300,
            )
            return [
                *left_leaves,
                *(
                    replace(
                        leaf,
                        merge_left_tracks=_merge_positions(
                            (boundary, *leaf.merge_left_tracks),
                            tolerance=boundary_tolerance,
                        ),
                    )
                    for leaf in right_leaves
                ),
            ]

    (
        content_bbox,
        left_tracks,
        dash_track,
        merge_left_tracks,
    ) = _left_tracks(
        structural_mask,
        min_gap=config.min_separator_gap,
    )
    rule_tracks = _dominant_vertical_rule_tracks(
        raw_mask,
        min_gap=config.min_separator_gap,
    )
    if not left_tracks or float(np.mean(structural_mask)) < 0.0005:
        aligned.close()
        return []

    source_content_bbox = _map_local_box(
        content_bbox,
        aligned.size,
        source_bbox,
    )
    source_tracks = tuple(_map_x(track, aligned.width, source_bbox) for track in left_tracks)
    source_merge_left_tracks = tuple(
        _map_x(track, aligned.width, source_bbox) for track in (*merge_left_tracks, *rule_tracks)
    )
    return [
        RecursiveGridLeaf(
            source_bbox=source_bbox,
            image=aligned,
            content_bbox=source_content_bbox,
            left_tracks=_merge_positions(
                source_tracks,
                tolerance=max(2, (source_bbox[2] - source_bbox[0]) // 300),
            ),
            dash_track=(
                _map_x(
                    dash_track,
                    aligned.width,
                    source_bbox,
                )
                if dash_track is not None
                else None
            ),
            merge_left_tracks=_merge_positions(
                source_merge_left_tracks,
                tolerance=max(
                    2,
                    (source_bbox[2] - source_bbox[0]) // 300,
                ),
            ),
            decisions=history + (decision,),
        )
    ]


def _apply_region_preprocessing(
    image: Image.Image,
    step_names: tuple[str, ...],
) -> tuple[Image.Image, tuple[str, ...]]:
    processed = image
    applied = []
    for name in step_names:
        step_type = IMAGE_PREPROCESSING_STEPS.get(name)
        if step_type is None:
            continue
        candidate = step_type().apply(processed)
        if candidate is processed:
            continue
        processed.close()
        processed = candidate
        applied.append(name)
    return processed, tuple(applied)


def _adaptive_foreground_mask(
    image: Image.Image,
) -> tuple[np.ndarray, np.ndarray, str, int]:
    rgb_image = image.convert("RGB")
    rgb = np.asarray(rgb_image, dtype=np.int16)
    gray_image = rgb_image.convert("L")
    gray = np.asarray(gray_image, dtype=np.int16)
    overlay_mask = _bright_saturated_overlay_mask(rgb, gray)
    radius = max(3, min(12, min(image.size) // 220))
    blurred = np.asarray(
        gray_image.filter(ImageFilter.GaussianBlur(radius=radius)),
        dtype=np.int16,
    )
    median = float(np.median(gray))
    if median <= 105:
        modes = ("local_light",)
    elif median >= 150:
        modes = ("local_dark",)
    else:
        modes = ("local_dark", "local_light")

    candidates: list[tuple[float, str, int, np.ndarray]] = []
    for mode in modes:
        for delta in (10, 14, 18, 22, 28):
            if mode == "local_light":
                mask = gray - delta > blurred
            else:
                mask = gray + delta < blurred
            mask = mask & ~overlay_mask
            ratio = float(np.mean(mask))
            valid = 0.002 <= ratio <= 0.28
            score = (0.0 if valid else 1.0) + abs(ratio - 0.065)
            candidates.append((score, mode, delta, mask))

    _, mode, delta, selected = min(candidates, key=lambda item: item[0])
    return (
        _remove_dominant_rules(selected),
        selected,
        mode,
        delta,
    )


def _bright_saturated_overlay_mask(rgb: np.ndarray, gray: np.ndarray) -> np.ndarray:
    """Ignore bright annotation fills/guides while segmenting OCR rows.

    Debug overlays and UI annotations often use saturated pastel fills and
    guide lines that are much brighter than text. They should not split or join
    recursive OCR blocks, but the original pixels are still passed to OCR.
    """
    high = np.max(rgb, axis=2)
    low = np.min(rgb, axis=2)
    chroma = high - low
    return (chroma >= 38) & (high >= 145) & (gray >= 115)


def _dominant_vertical_rule_tracks(
    mask: np.ndarray,
    *,
    min_gap: int,
) -> tuple[int, ...]:
    if not mask.size or not np.any(mask):
        return ()
    active_rows = np.flatnonzero(np.any(mask, axis=1))
    active_columns = np.flatnonzero(np.any(mask, axis=0))
    if not active_rows.size or not active_columns.size:
        return ()

    top = int(active_rows[0])
    bottom = int(active_rows[-1]) + 1
    left = int(active_columns[0])
    right = int(active_columns[-1]) + 1
    column_coverage = np.mean(mask[top:bottom, :], axis=0)
    maximum_rule_width = max(4, mask.shape[1] // 180)
    vertical_tracks = tuple(
        (start + end) // 2
        for start, end in _true_groups(column_coverage >= 0.55)
        if end - start <= maximum_rule_width and start > left + min_gap and end < right - min_gap
    )
    row_coverage = np.mean(mask[:, left:right], axis=1)
    maximum_rule_height = max(4, mask.shape[0] // 100)
    horizontal_rules = tuple(
        (start + end) // 2
        for start, end in _true_groups(row_coverage >= 0.30)
        if end - start <= maximum_rule_height and start > top + min_gap and end < bottom - min_gap
    )
    if len(vertical_tracks) < 2 or len(horizontal_rules) < 5:
        return ()
    return vertical_tracks


def _remove_dominant_rules(mask: np.ndarray) -> np.ndarray:
    cleaned = mask.copy()
    if not cleaned.size or not np.any(cleaned):
        return cleaned

    active_rows = np.flatnonzero(np.any(cleaned, axis=1))
    active_columns = np.flatnonzero(np.any(cleaned, axis=0))
    if not active_rows.size or not active_columns.size:
        return cleaned

    top = int(active_rows[0])
    bottom = int(active_rows[-1]) + 1
    left = int(active_columns[0])
    right = int(active_columns[-1]) + 1
    column_coverage = np.mean(cleaned[top:bottom, :], axis=0)
    row_coverage = np.mean(cleaned[:, left:right], axis=1)
    cleaned[:, column_coverage >= 0.88] = False
    cleaned[row_coverage >= 0.88, :] = False
    return cleaned


def _deskew_region(
    image: Image.Image,
    mask: np.ndarray,
    raw_mask: np.ndarray,
    *,
    mask_mode: str,
    contrast_delta: int,
) -> tuple[Image.Image, np.ndarray, np.ndarray, float]:
    if image.width < 160 or image.height < 80:
        return image, mask, raw_mask, 0.0

    scale = min(1.0, 520 / max(1, image.width))
    mask_image = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
    if scale < 1:
        mask_image = mask_image.resize(
            (
                max(1, round(mask_image.width * scale)),
                max(1, round(mask_image.height * scale)),
            ),
            getattr(Image, "Resampling", Image).BILINEAR,
        )

    coarse_angles = (-6.0, -4.0, -2.0, 0.0, 2.0, 4.0, 6.0)
    scores = {angle: _row_concentration(mask_image, angle) for angle in coarse_angles}
    best = max(scores, key=scores.get)
    refined_angles = (best - 1.0, best - 0.5, best, best + 0.5, best + 1.0)
    refined = {angle: _row_concentration(mask_image, angle) for angle in refined_angles if -7.0 <= angle <= 7.0}
    best = max(refined, key=refined.get)
    baseline = max(1e-6, scores[0.0])
    if abs(best) < 0.5 or refined[best] < baseline * 1.04:
        return image, mask, raw_mask, 0.0

    rgb = np.asarray(image.convert("RGB"))
    fill = tuple(int(value) for value in np.median(rgb, axis=(0, 1)))
    rotated = image.rotate(
        best,
        resample=getattr(Image, "Resampling", Image).BICUBIC,
        expand=False,
        fillcolor=fill,
    )
    image.close()
    (
        rotated_mask,
        rotated_raw_mask,
        _,
        _,
    ) = _adaptive_foreground_mask(rotated)
    return rotated, rotated_mask, rotated_raw_mask, float(best)


def _row_concentration(mask: Image.Image, angle: float) -> float:
    rotated = mask.rotate(
        angle,
        resample=getattr(Image, "Resampling", Image).BILINEAR,
        expand=False,
        fillcolor=0,
    )
    rows = np.asarray(rotated, dtype=np.float32).sum(axis=1)
    mean = float(np.mean(rows))
    if mean <= 0:
        return 0.0
    return float(np.var(rows) / mean)


def _vertical_lane_separator(
    mask: np.ndarray,
    *,
    min_cell_height: int,
    min_gap: int,
) -> tuple[int, int] | None:
    height, width = mask.shape
    if width < max(160, min_cell_height * 4) or height < min_cell_height or not np.any(mask):
        return None

    active_groups = _true_groups(np.any(mask, axis=0))
    if len(active_groups) < 2:
        return None

    merge_gap = max(min_gap * 4, width // 30, 12)
    merged: list[list[int]] = []
    for start, end in active_groups:
        if not merged or start - merged[-1][1] > merge_gap:
            merged.append([start, end])
        else:
            merged[-1][1] = end
    if len(merged) < 2:
        return None

    minimum_lane_width = max(80, round(width * 0.12))
    minimum_gutter = max(min_gap * 8, round(width * 0.12))
    candidates = []
    for left_group, right_group in zip(merged, merged[1:]):
        gutter_left = left_group[1]
        gutter_right = right_group[0]
        gutter_width = gutter_right - gutter_left
        cut = (gutter_left + gutter_right) // 2
        if gutter_width < minimum_gutter or cut < minimum_lane_width or width - cut < minimum_lane_width:
            continue

        left_rows = np.flatnonzero(np.any(mask[:, :gutter_left], axis=1))
        right_rows = np.flatnonzero(np.any(mask[:, gutter_right:], axis=1))
        if not left_rows.size or not right_rows.size:
            continue

        def logical_row_count(lane: np.ndarray) -> int:
            lane_width = lane.shape[1]
            row_ink = np.count_nonzero(lane, axis=1)
            row_groups = _true_groups(row_ink >= max(1, round(lane_width * 0.001)))
            return sum(end - start >= 2 for start, end in row_groups)

        if logical_row_count(mask[:, :gutter_left]) < 2 or logical_row_count(mask[:, gutter_right:]) < 2:
            continue

        overlap = min(int(left_rows[-1]), int(right_rows[-1])) - max(int(left_rows[0]), int(right_rows[0])) + 1
        smaller_span = min(
            int(left_rows[-1] - left_rows[0] + 1),
            int(right_rows[-1] - right_rows[0] + 1),
        )
        if overlap < max(
            min_cell_height,
            round(smaller_span * 0.20),
        ):
            continue
        candidates.append((gutter_width, cut))

    if not candidates:
        return None
    gutter_width, cut = max(candidates)
    return cut, gutter_width


def _horizontal_separator(
    mask: np.ndarray,
    *,
    min_cell_height: int,
    min_separator_gap: int,
) -> tuple[int, float] | None:
    height, width = mask.shape
    if height < min_cell_height * 2 or width < 16:
        return None

    row_ink = np.count_nonzero(mask, axis=1)
    blank_threshold = max(1, round(width * 0.001))
    gaps = [
        (top, bottom, (top + bottom) // 2)
        for top, bottom in _true_groups(row_ink <= blank_threshold)
        if bottom - top >= min_separator_gap
        and (top + bottom) // 2 >= min_cell_height
        and height - (top + bottom) // 2 >= min_cell_height
    ]
    if not gaps:
        return None

    top, bottom, cut = max(
        gaps,
        key=lambda gap: (
            gap[1] - gap[0],
            -abs((gap[0] + gap[1]) / 2 - height / 2),
        ),
    )
    return cut, 0.0


def _horizontal_projection_mask(
    mask: np.ndarray,
    *,
    min_gap: int,
) -> np.ndarray:
    projected, _ = _horizontal_projection(
        mask,
        min_gap=min_gap,
        inherited_insets=(0.0, 0.0),
    )
    return projected


def _horizontal_projection(
    mask: np.ndarray,
    *,
    min_gap: int,
    inherited_insets: tuple[float, float],
) -> tuple[np.ndarray, tuple[float, float]]:
    height, width = mask.shape
    if height < 32 or width < 64 or not np.any(mask):
        return mask, inherited_insets

    column_ink = np.count_nonzero(mask, axis=0)
    groups = _true_groups(column_ink >= max(2, round(height * 0.01)))
    merge_gap = max(min_gap * 2, width // 200, 4)
    merged: list[list[int]] = []
    for start, end in groups:
        if not merged or start - merged[-1][1] > merge_gap:
            merged.append([start, end])
        else:
            merged[-1][1] = end

    edge_band = max(min_gap * 3, round(width * 0.08))
    max_clutter_width = max(min_gap * 6, round(width * 0.12))
    ignored = np.zeros(width, dtype=bool)
    inherited_left = min(
        width,
        max(0, round(width * inherited_insets[0])),
    )
    inherited_right = min(
        width,
        max(0, round(width * inherited_insets[1])),
    )
    if inherited_left:
        ignored[:inherited_left] = True
    if inherited_right:
        ignored[width - inherited_right :] = True
    detected_left = inherited_left
    detected_right = inherited_right
    can_detect_edge = height >= max(240, round(width * 0.20))
    if can_detect_edge:
        for start, end in merged:
            near_left = start <= edge_band
            near_right = width - end <= edge_band
            if not (near_left or near_right):
                continue
            if end - start > max_clutter_width:
                continue

            active_rows = np.flatnonzero(np.any(mask[:, start:end], axis=1))
            if not active_rows.size:
                continue
            row_span = int(active_rows[-1] - active_rows[0] + 1) / height
            row_coverage = active_rows.size / height
            if row_span < 0.65 or row_coverage < 0.05:
                continue

            if near_left:
                ignored[:end] = True
                detected_left = max(detected_left, end)
            if near_right:
                ignored[start:] = True
                detected_right = max(detected_right, width - start)

    if not np.any(ignored):
        return mask, inherited_insets

    projected = mask.copy()
    projected[:, ignored] = False
    return projected, (
        detected_left / width,
        detected_right / width,
    )


def _left_tracks(
    mask: np.ndarray,
    *,
    min_gap: int,
) -> tuple[Box, tuple[int, ...], int | None, tuple[int, ...]]:
    height, width = mask.shape
    column_ink = np.count_nonzero(mask, axis=0)
    threshold = max(2, int(height * 0.03))
    groups = _true_groups(column_ink >= threshold)
    if not groups:
        return (0, 0, width, height), (), None, ()

    merge_gap = max(min_gap * 4, 12, width // 90)
    merged: list[list[int]] = []
    for start, end in groups:
        if not merged or start - merged[-1][1] > merge_gap:
            merged.append([start, end])
        else:
            merged[-1][1] = end
    wide = [(start, end) for start, end in merged if end - start >= max(2, min_gap // 2)]
    if not wide:
        return (0, 0, width, height), (), None, ()

    active_groups = _true_groups(np.any(mask, axis=0))
    if (
        len(active_groups) >= 2
        and active_groups[0][0] <= min_gap
        and active_groups[0][1] - active_groups[0][0] <= min_gap * 2
        and active_groups[1][0] - active_groups[0][1] >= min_gap * 2
    ):
        active_groups = active_groups[1:]
    if (
        len(active_groups) >= 2
        and width - active_groups[-1][1] <= min_gap
        and active_groups[-1][1] - active_groups[-1][0] <= min_gap * 2
        and active_groups[-1][0] - active_groups[-2][1] >= min_gap * 2
    ):
        active_groups = active_groups[:-1]
    content_left = max(0, active_groups[0][0] - min_gap)
    content_right = min(width, active_groups[-1][1] + min_gap)
    row_ink = np.count_nonzero(mask, axis=1)
    row_groups = _true_groups(row_ink >= max(2, round(width * 0.008)))
    if row_groups:
        content_top = max(0, row_groups[0][0] - min_gap)
        content_bottom = min(height, row_groups[-1][1] + min_gap)
    else:
        content_top = 0
        content_bottom = height
    tracks = [content_left]
    dash_track = None
    marker_content_track = None
    if len(groups) >= 2:
        first_start, first_end = groups[0]
        second_start, second_end = groups[1]
        marker_width = first_end - first_start
        marker_limit = max(24, round(width * 0.06))
        if (
            marker_width <= marker_limit
            and second_start - first_end >= min_gap
            and second_start - first_end <= max(120, round(width * 0.12))
        ):
            marker_rows = np.flatnonzero(np.any(mask[:, first_start:first_end], axis=1))
            marker_height = int(marker_rows[-1] - marker_rows[0] + 1) if marker_rows.size else height
            marker_area = (
                mask[
                    marker_rows[0] : marker_rows[-1] + 1,
                    first_start:first_end,
                ]
                if marker_rows.size
                else mask[:, first_start:first_end]
            )
            marker_fill = float(np.mean(marker_area))
            left_contact = float(np.mean(marker_area[:, 0]))
            right_contact = float(np.mean(marker_area[:, -1]))
            marker_row_fill = float(np.max(np.mean(marker_area, axis=1)))
            marker_column_fill = float(np.max(np.mean(marker_area, axis=0)))
            following_rows = np.flatnonzero(np.any(mask[:, second_start:second_end], axis=1))
            following_height = int(following_rows[-1] - following_rows[0] + 1) if following_rows.size else height
            is_dash = (
                marker_height >= 3
                and marker_width >= max(5, round(marker_height * 1.4))
                and marker_height <= max(4, round(following_height * 0.45))
            )
            is_bullet = (
                marker_height >= 5
                and marker_width >= 5
                and marker_height <= max(10, round(height * 0.22))
                and marker_width >= max(3, round(marker_height * 0.40))
                and marker_width <= max(10, round(marker_height * 1.60))
                and marker_fill >= 0.20
                and abs(left_contact - right_contact) <= 0.35
                and max(left_contact, right_contact) < 0.95
            )
            is_plus = (
                marker_width >= 5
                and marker_height >= 5
                and marker_width <= max(24, round(height * 0.75))
                and marker_height <= max(24, round(height * 0.75))
                and marker_width >= round(marker_height * 0.60)
                and marker_width <= round(marker_height * 1.60)
                and 0.12 <= marker_fill <= 0.40
                and marker_row_fill >= 0.80
                and marker_column_fill >= 0.80
            )
            if is_dash or is_bullet or is_plus:
                tracks.append(second_start)
                marker_content_track = second_start
                dash_track = content_left
    subtable_gap = max(min_gap * 8, round(width * 0.08))
    merge_left_tracks = []
    for previous, current in zip(wide, wide[1:]):
        if (
            current[0] - previous[1] >= subtable_gap
            and current[0] != marker_content_track
            and _column_pair_cooccurs(
                mask,
                previous,
                current,
                min_gap=min_gap,
            )
        ):
            tracks.append(current[0])
            merge_left_tracks.append(current[0])
    return (
        (
            content_left,
            content_top,
            content_right,
            content_bottom,
        ),
        _merge_positions(
            tracks,
            tolerance=max(2, min_gap),
        ),
        dash_track,
        _merge_positions(
            merge_left_tracks,
            tolerance=max(2, min_gap),
        ),
    )


def _column_pair_cooccurs(
    mask: np.ndarray,
    previous: tuple[int, int],
    current: tuple[int, int],
    *,
    min_gap: int,
) -> bool:
    width = mask.shape[1]
    minimum_width = max(min_gap, round(width * 0.01))
    if previous[1] - previous[0] < minimum_width or current[1] - current[0] < minimum_width:
        return False

    previous_rows = np.any(
        mask[:, previous[0] : previous[1]],
        axis=1,
    )
    current_rows = np.any(
        mask[:, current[0] : current[1]],
        axis=1,
    )
    overlap = int(np.count_nonzero(previous_rows & current_rows))
    smaller_span = min(
        int(np.count_nonzero(previous_rows)),
        int(np.count_nonzero(current_rows)),
    )
    return overlap >= 4 and overlap / max(1, smaller_span) >= 0.25


def _true_groups(values: np.ndarray) -> list[tuple[int, int]]:
    indexes = np.flatnonzero(values)
    if not indexes.size:
        return []
    groups = []
    start = int(indexes[0])
    previous = start
    for raw in indexes[1:]:
        index = int(raw)
        if index == previous + 1:
            previous = index
            continue
        groups.append((start, previous + 1))
        start = index
        previous = index
    groups.append((start, previous + 1))
    return groups


def _overlapping_windows(
    height: int,
    window: int,
    overlap: int,
) -> list[tuple[int, int]]:
    if height <= window:
        return [(0, height)]
    step = max(1, window - min(overlap, window - 1))
    result = []
    top = 0
    while top < height:
        bottom = min(height, top + window)
        result.append((top, bottom))
        if bottom == height:
            break
        top += step
    return result


def _map_local_box(
    local: Box,
    local_size: tuple[int, int],
    source: Box,
) -> Box:
    width, height = local_size
    source_left, source_top, source_right, source_bottom = source
    source_width = source_right - source_left
    source_height = source_bottom - source_top
    left, top, right, bottom = local
    return (
        source_left + round(left * source_width / max(1, width)),
        source_top + round(top * source_height / max(1, height)),
        source_left + round(right * source_width / max(1, width)),
        source_top + round(bottom * source_height / max(1, height)),
    )


def _map_x(value: int, local_width: int, source: Box) -> int:
    left, _, right, _ = source
    return left + round(value * (right - left) / max(1, local_width))


def _merge_positions(
    values: tuple[int, ...] | list[int],
    *,
    tolerance: int,
) -> tuple[int, ...]:
    ordered = sorted(set(values))
    if not ordered:
        return ()
    groups: list[list[int]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return tuple(round(sum(group) / len(group)) for group in groups)


def _deduplicate_leaves(
    leaves: list[RecursiveGridLeaf],
) -> list[RecursiveGridLeaf]:
    selected: list[RecursiveGridLeaf] = []
    for leaf in sorted(
        leaves,
        key=lambda item: (
            item.source_bbox[1],
            item.source_bbox[0],
            item.source_bbox[3],
        ),
    ):
        duplicate = next(
            (
                existing
                for existing in selected
                if _axis_overlap_ratio(
                    existing.content_bbox,
                    leaf.content_bbox,
                    axis=0,
                )
                >= 0.65
                and _axis_overlap_ratio(
                    existing.content_bbox,
                    leaf.content_bbox,
                    axis=1,
                )
                >= 0.65
            ),
            None,
        )
        if duplicate is None:
            selected.append(leaf)
            continue
        if _box_area(leaf.content_bbox) > _box_area(duplicate.content_bbox):
            duplicate.image.close()
            selected[selected.index(duplicate)] = leaf
        else:
            leaf.image.close()
    return selected


def _axis_overlap_ratio(
    first: Box,
    second: Box,
    *,
    axis: int,
) -> float:
    start_index = axis
    end_index = axis + 2
    overlap = min(first[end_index], second[end_index]) - max(first[start_index], second[start_index])
    if overlap <= 0:
        return 0.0
    smaller = min(
        first[end_index] - first[start_index],
        second[end_index] - second[start_index],
    )
    return overlap / max(1, smaller)


def _box_area(box: Box) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])
