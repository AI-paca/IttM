from __future__ import annotations

import hashlib
import math
from bisect import bisect_left
from dataclasses import dataclass
from typing import Iterable

import numpy as np
from PIL import Image, ImageFilter

from app.preprocessing import (
    RasterTransform,
    confidence_gated_region_deskew,
)
from app.sparse_pipeline.contracts import (
    AffineTransform,
    AlignmentTrace,
    AxisInterval,
    Box,
    GeometryResult,
    GeometryStatus,
    RecursiveNode,
    Rule,
    RuleAxis,
    Segment,
    SegmentationResult,
    SegmentKind,
    SegmentSpan,
    SparseCell,
    SparseSegmentMatrix,
    SplitAxis,
    StopReason,
)
from app.sparse_pipeline.local_structure import (
    LocalLine,
    LocalNetwork,
    LocalStructureConfig,
    LocalStructureResult,
    detect_local_structures,
)


@dataclass(frozen=True)
class GeometryConfig:
    """Deterministic, geometry-only limits for stage 1."""

    foreground_threshold: int | None = None
    min_rule_length: int = 24
    max_rule_thickness: int = 4
    min_rule_page_fraction: float = 0.6
    min_rule_aspect_ratio: float = 12.0
    min_rule_contrast: int = 32
    min_safe_gap: int = 3
    max_depth: int = 64
    max_nodes: int = 32_767
    max_runs: int = 500_000
    # A connected component cannot exist without at least one run.  Keep both
    # limits equal by default so a legitimate multi-page raster cannot fail
    # merely because it contains more than 100k disconnected glyph strokes.
    # Explicitly smaller component limits remain available to callers/tests.
    max_components: int = 500_000
    max_input_pixels: int = 80_000_000
    max_aligned_pixels: int = 100_000_000
    deskew_max_degrees: float = 5.0
    deskew_coarse_step: float = 0.5
    deskew_fine_step: float = 0.1
    deskew_min_foreground_pixels: int = 128
    deskew_min_gain: float = 0.02
    deskew_max_sample_dimension: int = 1200
    region_deskew_enabled: bool = True
    region_deskew_min_confidence: float = 0.82
    region_deskew_min_degrees: float = 1.0
    alpha_background_rgb: tuple[int, int, int] = (255, 255, 255)

    def __post_init__(self) -> None:
        integer_fields = (
            self.min_rule_length,
            self.max_rule_thickness,
            self.min_rule_contrast,
            self.min_safe_gap,
            self.max_depth,
            self.max_nodes,
            self.max_runs,
            self.max_components,
            self.max_input_pixels,
            self.max_aligned_pixels,
            self.deskew_min_foreground_pixels,
            self.deskew_max_sample_dimension,
        )
        if any(type(value) is not int for value in integer_fields):
            raise ValueError("geometry integer limits must be integers")
        float_fields = (
            self.min_rule_page_fraction,
            self.min_rule_aspect_ratio,
            self.deskew_max_degrees,
            self.deskew_coarse_step,
            self.deskew_fine_step,
            self.deskew_min_gain,
            self.region_deskew_min_confidence,
            self.region_deskew_min_degrees,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
            for value in float_fields
        ):
            raise ValueError("geometry ratios and angles must be finite numbers")
        if self.foreground_threshold is not None and (
            type(self.foreground_threshold) is not int or not 0 <= self.foreground_threshold <= 255
        ):
            raise ValueError("foreground_threshold must be an integer between zero and 255")
        if not 0 <= self.min_rule_contrast <= 255:
            raise ValueError("min_rule_contrast must be between zero and 255")
        if (
            min(
                self.min_rule_length,
                self.max_rule_thickness,
                self.min_safe_gap,
                self.max_nodes,
                self.max_runs,
                self.max_components,
                self.max_input_pixels,
                self.max_aligned_pixels,
                self.deskew_min_foreground_pixels,
                self.deskew_max_sample_dimension,
            )
            < 1
        ):
            raise ValueError("geometry size and count limits must be positive")
        if self.max_depth < 0:
            raise ValueError("max_depth must be non-negative")
        if not 0.0 < self.min_rule_page_fraction <= 1.0:
            raise ValueError("min_rule_page_fraction must be between zero and one")
        if self.min_rule_aspect_ratio <= 1.0:
            raise ValueError("min_rule_aspect_ratio must be greater than one")
        if not 0.0 <= self.deskew_max_degrees <= 15.0:
            raise ValueError("deskew_max_degrees must be between zero and 15")
        if self.deskew_coarse_step <= 0.0 or self.deskew_fine_step <= 0.0:
            raise ValueError("deskew steps must be positive")
        if self.deskew_min_gain < 0.0:
            raise ValueError("deskew_min_gain must be non-negative")
        if type(self.region_deskew_enabled) is not bool:
            raise ValueError("region_deskew_enabled must be a boolean")
        if not 0.0 <= self.region_deskew_min_confidence <= 1.0:
            raise ValueError(
                "region_deskew_min_confidence must be between zero and one"
            )
        if not 0.0 <= self.region_deskew_min_degrees <= 15.0:
            raise ValueError(
                "region_deskew_min_degrees must be between zero and 15"
            )
        if len(self.alpha_background_rgb) != 3 or any(
            type(value) is not int or not 0 <= value <= 255 for value in self.alpha_background_rgb
        ):
            raise ValueError("alpha_background_rgb must contain three bytes")


@dataclass(frozen=True)
class GeometryBundle:
    """A result plus immutable raster evidence used by debug writers."""

    result: GeometryResult
    source_rgb: np.ndarray
    aligned_rgb: np.ndarray
    foreground_mask: np.ndarray
    rule_mask: np.ndarray
    ownership: np.ndarray


class GeometryLimitError(RuntimeError):
    """Raised before a raster allocation would exceed a configured bound."""


@dataclass(frozen=True)
class _Run:
    row: int
    start: int
    stop: int
    label: int


@dataclass(frozen=True)
class _Component:
    component_id: int
    bbox: Box
    pixels: int
    runs: tuple[_Run, ...]


@dataclass
class _NodeDraft:
    node_id: str
    bbox: Box
    depth: int
    parent_id: str | None
    path: tuple[str, ...]
    axis: SplitAxis | None = None
    child_ids: tuple[str, ...] = ()
    separator_boxes: tuple[Box, ...] = ()
    split_coordinate: int | None = None
    stop_reason: StopReason | None = None
    rule_partition: bool = False
    row_rule_top: bool = False
    row_rule_bottom: bool = False


@dataclass(frozen=True)
class _RuleDraft:
    axis: RuleAxis
    bbox: Box
    candidate_mask: np.ndarray
    claim_full_bbox: bool = False
    partition_evidence: bool = True


@dataclass(frozen=True)
class _RowGrid:
    pitch: int
    phase: int
    row_count: int
    score: float


class GeometryAnalyzer:
    """Analyze document pixels without invoking enhancement, OCR, or grammar."""

    def __init__(self, config: GeometryConfig | None = None) -> None:
        self.config = config or GeometryConfig()
        self._last_bundle: GeometryBundle | None = None

    @property
    def last_bundle(self) -> GeometryBundle | None:
        return self._last_bundle

    def analyze(self, image: Image.Image) -> GeometryResult:
        bundle = self.analyze_bundle(image)
        self._last_bundle = bundle
        return bundle.result

    def analyze_bundle(self, image: Image.Image) -> GeometryBundle:
        if not isinstance(image, Image.Image):
            raise TypeError("image must be a Pillow Image")
        if image.width < 1 or image.height < 1:
            raise ValueError("image dimensions must be positive")
        source_pixels = image.width * image.height
        if source_pixels > self.config.max_input_pixels:
            raise GeometryLimitError(f"input pixel limit exceeded: {source_pixels} > {self.config.max_input_pixels}")
        if source_pixels > self.config.max_aligned_pixels:
            raise GeometryLimitError(
                f"aligned pixel limit exceeded before allocation: "
                f"{source_pixels} > {self.config.max_aligned_pixels}"
            )

        source_rgb = _source_rgb(image, self.config.alpha_background_rgb)
        preprocessed = confidence_gated_region_deskew(
            image,
            enabled=self.config.region_deskew_enabled,
            minimum_confidence=self.config.region_deskew_min_confidence,
            minimum_degrees=self.config.region_deskew_min_degrees,
            maximum_pixels=min(
                self.config.max_input_pixels,
                self.config.max_aligned_pixels,
            ),
        )
        if preprocessed.applied:
            try:
                geometry_source_rgb = _source_rgb(
                    preprocessed.image,
                    self.config.alpha_background_rgb,
                )
            finally:
                preprocessed.image.close()
        else:
            geometry_source_rgb = source_rgb
        geometry_source_pixels = (
            geometry_source_rgb.shape[0] * geometry_source_rgb.shape[1]
        )
        if geometry_source_pixels > self.config.max_aligned_pixels:
            raise GeometryLimitError(
                "region-aligned pixel limit exceeded: "
                f"{geometry_source_pixels} > {self.config.max_aligned_pixels}"
            )
        background = _estimate_background(geometry_source_rgb)
        source_foreground = _detect_foreground(
            geometry_source_rgb,
            background,
            self.config.foreground_threshold,
        )
        correction = _estimate_correction(source_foreground, self.config)
        aligned_rgb, warped_source_mask, geometry_transform = _align(
            geometry_source_rgb,
            source_foreground,
            background,
            correction,
            max_aligned_pixels=self.config.max_aligned_pixels,
        )
        transform = RasterTransform.compose(
            preprocessed.transform,
            geometry_transform,
            alignment_degrees=correction,
        )
        aligned_foreground = np.logical_or(
            _detect_foreground(aligned_rgb, background, self.config.foreground_threshold),
            warped_source_mask,
        )
        partition_foreground, foreground_mode = _select_layout_foreground(
            aligned_rgb,
            aligned_foreground,
        )
        adaptive_partition = foreground_mode.startswith("adaptive-")

        rule_evidence = _rule_evidence_mask(
            aligned_rgb,
            background,
            aligned_foreground,
            self.config.min_rule_contrast,
        )
        rule_drafts = _detect_rules(rule_evidence, self.config, aligned_rgb)
        suppressed_rule_pixels = int(aligned_foreground.sum()) - int(rule_evidence.sum())
        if suppressed_rule_pixels > int(rule_evidence.sum()):
            rule_drafts = _expand_rule_bands(
                rule_drafts,
                aligned_foreground,
                self.config,
            )
        rules, rule_mask = _materialize_rules(rule_drafts, aligned_foreground, transform)
        ownership_foreground = (
            np.logical_or(
                partition_foreground,
                np.logical_and(aligned_foreground, rule_mask),
            )
            if adaptive_partition
            else aligned_foreground
        )
        non_rule_foreground = np.logical_and(
            ownership_foreground,
            np.logical_not(rule_mask),
        )
        connected_components = _connected_components(
            non_rule_foreground,
            max_runs=self.config.max_runs,
            max_components=self.config.max_components,
        )
        partition_mask = np.logical_and(
            partition_foreground,
            np.logical_not(rule_mask),
        )
        partition_components = _connected_components(
            partition_mask,
            max_runs=self.config.max_runs,
            max_components=self.config.max_components,
        )
        seam_guard_mask = _guard_diacritic_gaps(
            partition_mask,
            partition_components,
        )
        diacritic_guard = np.logical_and(
            seam_guard_mask,
            np.logical_not(partition_mask),
        )
        diacritic_guard_stale = False
        row_grid = _estimate_regular_row_grid(
            partition_mask,
            partition_components,
        )
        drafts, refined_partition_mask = _recursive_partition(
            partition_mask,
            partition_components,
            self.config,
            rules=rules,
            diacritic_guard=diacritic_guard,
            row_grid=row_grid,
            cover_separators=adaptive_partition,
            partition_rgb=(
                aligned_rgb
                if adaptive_partition
                else None
            ),
            physical_fallback=non_rule_foreground,
        )
        if adaptive_partition:
            non_rule_foreground = np.logical_and(
                refined_partition_mask,
                np.logical_not(rule_mask),
            )
            connected_components = _connected_components(
                non_rule_foreground,
                max_runs=self.config.max_runs,
                max_components=self.config.max_components,
            )
            ownership_foreground = np.logical_or(
                non_rule_foreground,
                np.logical_and(aligned_foreground, rule_mask),
            )
        components = _fragment_components_for_leaves(connected_components, drafts)
        residual_rule_drafts = _recover_leaf_rule_networks(
            rule_evidence,
            non_rule_foreground,
            rule_mask,
            rules,
            drafts,
            components,
            self.config,
        )
        recursive_residual_rule_drafts = tuple(
            draft for draft in residual_rule_drafts if draft.partition_evidence
        )
        deferred_ownership_rule_drafts = tuple(
            draft for draft in residual_rule_drafts if not draft.partition_evidence
        )
        if recursive_residual_rule_drafts:
            ownership_only_horizontal_boxes = frozenset(
                draft.bbox
                for draft in recursive_residual_rule_drafts
                if draft.axis is RuleAxis.HORIZONTAL
            )
            rule_drafts = tuple(
                sorted(
                    (*rule_drafts, *recursive_residual_rule_drafts),
                    key=lambda draft: (
                        0 if draft.axis is RuleAxis.HORIZONTAL else 1,
                        draft.bbox.top,
                        draft.bbox.left,
                        draft.bbox.bottom,
                        draft.bbox.right,
                    ),
                )
            )
            rules, rule_mask = _materialize_rules(
                rule_drafts,
                aligned_foreground,
                transform,
            )
            ownership_foreground = (
                np.logical_or(
                    partition_foreground,
                    np.logical_and(aligned_foreground, rule_mask),
                )
                if adaptive_partition
                else aligned_foreground
            )
            non_rule_foreground = np.logical_and(
                ownership_foreground,
                np.logical_not(rule_mask),
            )
            connected_components = _connected_components(
                non_rule_foreground,
                max_runs=self.config.max_runs,
                max_components=self.config.max_components,
            )
            partition_mask = np.logical_and(
                partition_foreground,
                np.logical_not(rule_mask),
            )
            partition_components = _connected_components(
                partition_mask,
                max_runs=self.config.max_runs,
                max_components=self.config.max_components,
            )
            seam_guard_mask = _guard_diacritic_gaps(
                partition_mask,
                partition_components,
            )
            diacritic_guard = np.logical_and(
                seam_guard_mask,
                np.logical_not(partition_mask),
            )
            diacritic_guard_stale = False
            row_grid = _estimate_regular_row_grid(
                partition_mask,
                partition_components,
            )
            drafts, refined_partition_mask = _recursive_partition(
                partition_mask,
                partition_components,
                self.config,
                rules=rules,
                diacritic_guard=diacritic_guard,
                row_grid=row_grid,
                nonstructural_rule_boxes=ownership_only_horizontal_boxes,
                cover_separators=adaptive_partition,
                partition_rgb=(
                    aligned_rgb
                    if adaptive_partition
                    else None
                ),
                physical_fallback=non_rule_foreground,
            )
            if adaptive_partition:
                non_rule_foreground = np.logical_and(
                    refined_partition_mask,
                    np.logical_not(rule_mask),
                )
                connected_components = _connected_components(
                    non_rule_foreground,
                    max_runs=self.config.max_runs,
                    max_components=self.config.max_components,
                )
                ownership_foreground = np.logical_or(
                    non_rule_foreground,
                    np.logical_and(aligned_foreground, rule_mask),
                )
            components = _fragment_components_for_leaves(
                connected_components,
                drafts,
            )
        if deferred_ownership_rule_drafts:
            # Unsupported horizontal strokes are removed from OCR ownership,
            # but they are not separator evidence.  Materialize them only
            # after the second recursive pass so an underline cannot invent a
            # row boundary or turn the surrounding region into a rule-locked
            # partition.  Component-only refinement below may then use the
            # whitespace exposed by their removal.
            rule_drafts = tuple(
                sorted(
                    (*rule_drafts, *deferred_ownership_rule_drafts),
                    key=lambda draft: (
                        0 if draft.axis is RuleAxis.HORIZONTAL else 1,
                        draft.bbox.top,
                        draft.bbox.left,
                        draft.bbox.bottom,
                        draft.bbox.right,
                    ),
                )
            )
            rules, rule_mask = _materialize_rules(
                rule_drafts,
                aligned_foreground,
                transform,
            )
            ownership_foreground = (
                np.logical_or(
                    refined_partition_mask,
                    np.logical_and(aligned_foreground, rule_mask),
                )
                if adaptive_partition
                else aligned_foreground
            )
            non_rule_foreground = np.logical_and(
                ownership_foreground,
                np.logical_not(rule_mask),
            )
            connected_components = _connected_components(
                non_rule_foreground,
                max_runs=self.config.max_runs,
                max_components=self.config.max_components,
            )
            diacritic_guard_stale = True
            components = _fragment_components_for_leaves(
                connected_components,
                drafts,
            )
        final_line_draft_values: list[_RuleDraft] = []
        # Promoting one pure line can expose one adjacent halo as its own
        # component.  Three bounded ownership-only cleanup passes absorb a
        # dark core plus two raster-halo edges without rerunning recursion.
        for _ in range(3):
            pass_drafts = _recover_residual_line_drafts(
                non_rule_foreground,
                rule_evidence,
                rules,
                components,
                self.config,
            )
            if not pass_drafts:
                break
            final_line_draft_values.extend(pass_drafts)
            rule_drafts = tuple(
                sorted(
                    (*rule_drafts, *pass_drafts),
                    key=lambda draft: (
                        0 if draft.axis is RuleAxis.HORIZONTAL else 1,
                        draft.bbox.top,
                        draft.bbox.left,
                        draft.bbox.bottom,
                        draft.bbox.right,
                    ),
                )
            )
            rules, rule_mask = _materialize_rules(
                rule_drafts,
                aligned_foreground,
                transform,
            )
            ownership_foreground = (
                np.logical_or(
                    refined_partition_mask,
                    np.logical_and(aligned_foreground, rule_mask),
                )
                if adaptive_partition
                else aligned_foreground
            )
            non_rule_foreground = np.logical_and(
                ownership_foreground,
                np.logical_not(rule_mask),
            )
            connected_components = _connected_components(
                non_rule_foreground,
                max_runs=self.config.max_runs,
                max_components=self.config.max_components,
            )
            diacritic_guard_stale = True
            components = _fragment_components_for_leaves(
                connected_components,
                drafts,
            )
        final_line_drafts = tuple(final_line_draft_values)
        if diacritic_guard_stale:
            partition_mask = np.logical_and(
                partition_foreground,
                np.logical_not(rule_mask),
            )
            partition_components = _connected_components(
                partition_mask,
                max_runs=self.config.max_runs,
                max_components=self.config.max_components,
            )
            seam_guard_mask = _guard_diacritic_gaps(
                partition_mask,
                partition_components,
            )
            diacritic_guard = np.logical_and(
                seam_guard_mask,
                np.logical_not(partition_mask),
            )
        drafts = _refine_component_boundaries(
            drafts,
            components,
            diacritic_guard,
            self.config,
            rules,
        )
        segments, ownership, leaf_segment_ids = _materialize_segments(
            components,
            drafts,
            transform,
            aligned_foreground.shape,
        )
        nodes = _materialize_nodes(drafts, leaf_segment_ids, segments)
        matrix = _project_sparse_matrix(
            aligned_foreground.shape,
            ownership,
            segments,
            rules,
            nodes,
        )
        structural_bands = tuple((rule.axis, rule.bbox) for rule in rules)
        thin_line_segment_count = sum(
            _thin_line_axis(
                segment.bbox,
                segment.ink_pixels,
                structural_bands,
                self.config,
            )
            is not None
            for segment in segments
        )

        foreground_pixels = int(ownership_foreground.sum())
        owned_pixels = sum(segment.ink_pixels for segment in segments) + sum(rule.foreground_pixels for rule in rules)
        if owned_pixels != foreground_pixels:
            raise RuntimeError(f"foreground ownership is not exact: {owned_pixels} != {foreground_pixels}")

        content_bbox = _mask_bbox(ownership_foreground)
        alignment = AlignmentTrace(
            transform=transform,
            correction_degrees=correction,
            background_rgb=background,
            content_bbox=content_bbox,
            foreground_pixels=foreground_pixels,
        )
        segmentation = SegmentationResult(
            segments=segments,
            rules=rules,
            nodes=nodes,
            root_node_id="geo-root",
            aligned_size=transform.aligned_size,
            foreground_pixels=foreground_pixels,
        )
        limit_leaf_count = sum(node.stop_reason is StopReason.LIMIT for node in nodes)
        status = GeometryStatus.DEGRADED if limit_leaf_count else GeometryStatus.COMPLETE
        result = GeometryResult(
            alignment=alignment,
            segmentation=segmentation,
            matrix=matrix,
            aligned_rgb_sha256=hashlib.sha256(
                memoryview(np.ascontiguousarray(aligned_rgb))
            ).hexdigest(),
            status=status,
            diagnostics=(
                f"region_transform={transform.operation}",
                f"region_gate={preprocessed.gate_reason}",
                f"region_confidence={transform.confidence:.6f}",
                f"region_source_angle={transform.source_angle_degrees:.6f}",
                f"region_residual_angle={transform.residual_angle_degrees:.6f}",
                f"connected_components={len(connected_components)}",
                f"foreground_mode={foreground_mode}",
                f"component_fragments={len(components)}",
                f"segments={len(segments)}",
                f"rules={len(rules)}",
                f"rule_evidence_pixels={int(rule_evidence.sum())}",
                "recovered_rule_bands="
                f"{len(residual_rule_drafts) + len(final_line_drafts)}",
                f"thin_line_segment_count={thin_line_segment_count}",
                f"nodes={len(nodes)}",
                f"limit_leaves={limit_leaf_count}",
                (
                    "row_grid=none"
                    if row_grid is None
                    else f"row_grid={row_grid.pitch}:{row_grid.phase}:{row_grid.row_count}"
                ),
            ),
        )
        bundle = GeometryBundle(
            result=result,
            source_rgb=_readonly(source_rgb),
            aligned_rgb=_readonly(aligned_rgb),
            foreground_mask=_readonly(ownership_foreground),
            rule_mask=_readonly(rule_mask),
            ownership=_readonly(ownership),
        )
        self._last_bundle = bundle
        return bundle


def _readonly(array: np.ndarray) -> np.ndarray:
    value = np.array(array, copy=True)
    value.setflags(write=False)
    return value


def _source_rgb(image: Image.Image, alpha_background: tuple[int, int, int]) -> np.ndarray:
    has_alpha = "A" in image.getbands() or "transparency" in image.info
    if has_alpha:
        foreground = image.convert("RGBA")
        background = Image.new("RGBA", image.size, (*alpha_background, 255))
        converted = Image.alpha_composite(background, foreground).convert("RGB")
    else:
        converted = image.convert("RGB")
    return np.array(converted, dtype=np.uint8, copy=True)


def _estimate_background(rgb: np.ndarray) -> tuple[int, int, int]:
    height, width, _ = rgb.shape
    edge_color = rgb[0, 0]
    if all(np.array_equal(rgb[y, x], edge_color) for y, x in ((0, -1), (-1, 0), (-1, -1))):
        same_edge = np.all(rgb == edge_color, axis=2)

        def uniform_prefix(lines: np.ndarray) -> int:
            count = 0
            for uniform in lines:
                if not bool(uniform):
                    break
                count += 1
            return count

        row_uniform = same_edge.all(axis=1)
        column_uniform = same_edge.all(axis=0)
        top = uniform_prefix(row_uniform)
        bottom = uniform_prefix(row_uniform[::-1])
        left = uniform_prefix(column_uniform)
        right = uniform_prefix(column_uniform[::-1])
        thicknesses = (top, bottom, left, right)
        mean_thickness = sum(thicknesses) / 4.0
        symmetric_frame = max(thicknesses) - min(thicknesses) <= max(1, round(mean_thickness * 0.1))
        narrow_frame = mean_thickness <= max(width, height) * 0.1
        edge_ratio = float(same_edge.mean())
        if (
            min(thicknesses) >= 1
            and top + bottom < height
            and left + right < width
            and symmetric_frame
            and narrow_frame
            and edge_ratio >= 0.5
        ):
            inner_bottom = height - bottom
            inner_right = width - right
            boundary_edge_ratios = (
                float(same_edge[top, left:inner_right].mean()),
                float(same_edge[inner_bottom - 1, left:inner_right].mean()),
                float(same_edge[top:inner_bottom, left].mean()),
                float(same_edge[top:inner_bottom, inner_right - 1].mean()),
            )
            if max(boundary_edge_ratios) <= 0.5:
                inner = rgb[top:inner_bottom, left:inner_right].reshape(-1, 3)
                edge_key = int((edge_color.astype(np.uint16) // 8) @ np.asarray((1024, 32, 1), dtype=np.uint16))
                quantized = inner.astype(np.uint16) // 8
                keys = quantized[:, 0] * 1024 + quantized[:, 1] * 32 + quantized[:, 2]
                non_frame = inner[keys != edge_key]
                if non_frame.size:
                    return _modal_rgb(non_frame)

    inset_y = min(max(1, round(height * 0.2)), max(0, (height - 1) // 2))
    inset_x = min(max(1, round(width * 0.2)), max(0, (width - 1) // 2))
    interior = rgb[
        inset_y : height - inset_y if inset_y else height,
        inset_x : width - inset_x if inset_x else width,
    ]
    return _modal_rgb(interior.reshape(-1, 3))


def _modal_rgb(flat: np.ndarray) -> tuple[int, int, int]:
    quantized = flat.astype(np.uint16) // 8
    keys = quantized[:, 0] * 1024 + quantized[:, 1] * 32 + quantized[:, 2]
    modal_key = int(np.bincount(keys, minlength=32768).argmax())
    candidates = flat[keys == modal_key]
    median = np.median(candidates, axis=0).astype(np.uint8)
    return (int(median[0]), int(median[1]), int(median[2]))


def _detect_foreground(
    rgb: np.ndarray,
    background: tuple[int, int, int],
    configured_threshold: int | None,
) -> np.ndarray:
    difference = np.max(
        np.abs(rgb.astype(np.int16) - np.asarray(background, dtype=np.int16)),
        axis=2,
    )
    if configured_threshold is None:
        exact_background_ratio = float(np.mean(difference == 0))
        if exact_background_ratio >= 0.15:
            threshold = 0
        else:
            border = np.concatenate((difference[0], difference[-1], difference[:, 0], difference[:, -1]))
            median = float(np.median(border))
            mad = float(np.median(np.abs(border.astype(np.float64) - median)))
            threshold = int(max(2.0, min(32.0, median + 6.0 * mad + 2.0)))
    else:
        threshold = configured_threshold
    return difference > threshold


def _select_layout_foreground(
    rgb: np.ndarray,
    physical_foreground: np.ndarray,
    *,
    force_adaptive: bool = False,
) -> tuple[np.ndarray, str]:
    """Return the pre-v19 fill-suppressed recursion mask.

    A single page-background colour is exact for ordinary scans, but it makes
    every differently coloured card or table cell one solid foreground
    component.  Once that physical mask covers most of the canvas, recursive
    whitespace can no longer exist.  In that case an illumination-relative
    mask retains the local light/dark strokes and finite colour boundaries
    while treating the flat interiors as explicit empty matrix regions.

    The physical mask remains fallback/rule evidence.  On a fill-dominated
    raster the flat fill is deliberately excluded from segment ownership so
    a coloured empty cell stays an explicit empty region; ordinary rasters
    retain their exact physical ownership.  The layout mask is recomputed on
    recursive crops, while the full-page result is the root decision.
    """

    if not force_adaptive:
        # A thick, closed page frame can dominate a tiny raster just like a
        # flat coloured fill.  The interior, however, remains ordinary sparse
        # text.  Measure beyond four observed dense edge bands before enabling
        # fill suppression; this keeps frame/text pixels exact while a genuinely
        # fill-dominated table interior (the coloured rubric) stays adaptive.
        row_coverage = physical_foreground.mean(axis=1)
        column_coverage = physical_foreground.mean(axis=0)

        def edge_runs(values: np.ndarray) -> tuple[int, int]:
            start = 0
            while start < len(values) and values[start] >= 0.80:
                start += 1
            stop = len(values)
            while stop > start and values[stop - 1] >= 0.80:
                stop -= 1
            return start, len(values) - stop

        top, bottom = edge_runs(row_coverage)
        left, right = edge_runs(column_coverage)
        if min(top, bottom, left, right) > 0:
            interior = physical_foreground[
                top : physical_foreground.shape[0] - bottom,
                left : physical_foreground.shape[1] - right,
            ]
            if not interior.size or float(interior.mean()) <= 0.30:
                return np.array(physical_foreground, copy=True), "physical-frame-interior"

    gray_image = Image.fromarray(rgb, mode="RGB").convert("L")
    gray = np.asarray(gray_image, dtype=np.int16)
    radius = max(3, min(12, min(gray.shape) // 220))
    blurred = np.asarray(
        gray_image.filter(ImageFilter.GaussianBlur(radius=radius)),
        dtype=np.int16,
    )
    high = np.max(rgb, axis=2).astype(np.int16)
    low = np.min(rgb, axis=2).astype(np.int16)
    overlay = ((high - low) >= 38) & (high >= 145) & (gray >= 115)

    def clean(mask: np.ndarray) -> np.ndarray:
        """Remove only axis-dominant bands within one ink polarity."""

        cleaned = np.array(mask, copy=True)
        active_rows = np.flatnonzero(cleaned.any(axis=1))
        active_columns = np.flatnonzero(cleaned.any(axis=0))
        if active_rows.size and active_columns.size:
            top = int(active_rows[0])
            bottom = int(active_rows[-1]) + 1
            left = int(active_columns[0])
            right = int(active_columns[-1]) + 1
            column_coverage = cleaned[top:bottom].mean(axis=0)
            row_coverage = cleaned[:, left:right].mean(axis=1)
            cleaned[:, column_coverage >= 0.88] = False
            cleaned[row_coverage >= 0.88] = False
        return cleaned

    selected_by_mode: list[tuple[str, int, np.ndarray]] = []
    for polarity in ("local_dark", "local_light"):
        candidates: list[tuple[float, int, np.ndarray]] = []
        for delta in (10, 14, 18, 22, 28):
            if polarity == "local_light":
                mask = gray - delta > blurred
            else:
                mask = gray + delta < blurred
            mask = np.logical_and(mask, np.logical_not(overlay))
            ratio = float(np.mean(mask))
            valid = 0.002 <= ratio <= 0.28
            score = (0.0 if valid else 1.0) + abs(ratio - 0.065)
            candidates.append((score, delta, mask))
        _, delta, mask = min(candidates, key=lambda value: value[0])
        selected_by_mode.append((polarity, delta, clean(mask)))
    selected = np.logical_or.reduce(
        tuple(mask for _, _, mask in selected_by_mode)
    )
    mode = "adaptive-dual:" + "+".join(
        f"{polarity}:{delta}"
        for polarity, delta, _ in selected_by_mode
    )
    if not selected.any():
        if force_adaptive:
            return np.zeros_like(physical_foreground, dtype=bool), "adaptive-empty"
        return np.array(physical_foreground, copy=True), "physical-fallback"
    if not force_adaptive:
        physical_occupancy = float(np.mean(physical_foreground))
        adaptive_occupancy = float(np.mean(selected))
        suppressed = np.logical_and(
            physical_foreground,
            np.logical_not(selected),
        )
        selected_pixels = int(selected.sum())
        suppressed_pixels = int(suppressed.sum())
        anchored_fraction = int(
            np.logical_and(selected, physical_foreground).sum()
        ) / max(1, selected_pixels)
        selected_delta = min(delta for _, delta, _ in selected_by_mode)
        signed_rgb = rgb.astype(np.int16, copy=False)
        local_variation = np.zeros(rgb.shape[:2], dtype=np.int16)
        horizontal_variation = np.max(
            np.abs(signed_rgb[:, 1:] - signed_rgb[:, :-1]),
            axis=2,
        )
        vertical_variation = np.max(
            np.abs(signed_rgb[1:] - signed_rgb[:-1]),
            axis=2,
        )
        local_variation[:, 1:] = np.maximum(
            local_variation[:, 1:], horizontal_variation
        )
        local_variation[:, :-1] = np.maximum(
            local_variation[:, :-1], horizontal_variation
        )
        local_variation[1:] = np.maximum(
            local_variation[1:], vertical_variation
        )
        local_variation[:-1] = np.maximum(
            local_variation[:-1], vertical_variation
        )
        flat_suppressed_fraction = int(
            np.logical_and(
                suppressed,
                local_variation <= selected_delta,
            ).sum()
        ) / max(1, suppressed_pixels)
        fill_dominates = (
            physical_occupancy - adaptive_occupancy >= 0.15
            and physical_occupancy >= 4.0 * adaptive_occupancy
            and anchored_fraction >= 0.80
            and flat_suppressed_fraction >= 2.0 / 3.0
        )
        quantized_rgb = rgb.astype(np.uint16, copy=False) // 8
        quantized_keys = (
            quantized_rgb[:, :, 0] * 1024
            + quantized_rgb[:, :, 1] * 32
            + quantized_rgb[:, :, 2]
        )
        dominant_quantized_fraction = float(
            np.bincount(
                quantized_keys.ravel(),
                minlength=32768,
            ).max()
        ) / max(1, quantized_keys.size)
        unstable_paper_background = (
            physical_occupancy >= 0.30
            and physical_occupancy - adaptive_occupancy >= 0.15
            and adaptive_occupancy <= 0.20
            and dominant_quantized_fraction < 0.15
        )
        if not fill_dominates and not unstable_paper_background:
            return np.array(physical_foreground, copy=True), "physical"
        if unstable_paper_background and not fill_dominates:
            mode += ":unstable-paper-background"
    return selected, mode


def _rule_evidence_mask(
    rgb: np.ndarray,
    background: tuple[int, int, int],
    foreground: np.ndarray,
    minimum_contrast: int,
) -> np.ndarray:
    """Suppress flat, low-contrast fills only while detecting rules.

    The lossless ``foreground`` mask remains the source of pixel ownership.
    A neutral table fill can cover millions of pixels and connect every line
    into one component when rule projections are measured directly on that
    mask.  Structural rules need the darker/high-contrast subset instead.  A
    zero floor explicitly restores the original evidence mask.
    """

    if minimum_contrast == 0:
        return foreground
    difference = np.max(
        np.abs(rgb.astype(np.int16) - np.asarray(background, dtype=np.int16)),
        axis=2,
    )
    return np.logical_and(foreground, difference > minimum_contrast)


def _projection_score(mask: np.ndarray, angle: float) -> float:
    if abs(angle) < 1e-12:
        candidate = mask
    else:
        rotated = Image.fromarray(mask.astype(np.uint8) * 255, mode="L").rotate(
            angle,
            resample=Image.Resampling.NEAREST,
            expand=True,
            fillcolor=0,
        )
        candidate = np.asarray(rotated, dtype=np.uint8) > 0
    counts = candidate.sum(axis=1, dtype=np.float64)
    total = float(counts.sum())
    return 0.0 if total == 0.0 else float(np.square(counts).sum() / total)


def _float_range(start: float, stop: float, step: float) -> tuple[float, ...]:
    count = int(math.floor((stop - start) / step + 1e-9))
    return tuple(round(start + index * step, 10) for index in range(count + 1))


def _estimate_correction(mask: np.ndarray, config: GeometryConfig) -> float:
    foreground_pixels = int(mask.sum())
    if foreground_pixels < config.deskew_min_foreground_pixels or config.deskew_max_degrees == 0.0:
        return 0.0
    sample = _max_pool_mask(mask, config.deskew_max_sample_dimension)
    baseline = _projection_score(sample, 0.0)
    coarse_angles = _float_range(
        -config.deskew_max_degrees,
        config.deskew_max_degrees,
        config.deskew_coarse_step,
    )
    coarse_scores = tuple((_projection_score(sample, angle), -abs(angle), -angle, angle) for angle in coarse_angles)
    coarse_best = max(coarse_scores)[3]
    fine_start = max(-config.deskew_max_degrees, coarse_best - config.deskew_coarse_step)
    fine_stop = min(config.deskew_max_degrees, coarse_best + config.deskew_coarse_step)
    fine_angles = _float_range(fine_start, fine_stop, config.deskew_fine_step)
    best_score, _, _, best_angle = max(
        (_projection_score(sample, angle), -abs(angle), -angle, angle) for angle in fine_angles
    )
    required = baseline * (1.0 + config.deskew_min_gain)
    if best_score <= required or abs(best_angle) < config.deskew_fine_step / 2.0:
        return 0.0
    return float(best_angle)


def _max_pool_mask(mask: np.ndarray, maximum_dimension: int) -> np.ndarray:
    factor = max(1, math.ceil(max(mask.shape) / maximum_dimension))
    if factor == 1:
        return mask
    height, width = mask.shape
    padded_height = math.ceil(height / factor) * factor
    padded_width = math.ceil(width / factor) * factor
    padded = np.zeros((padded_height, padded_width), dtype=bool)
    padded[:height, :width] = mask
    return padded.reshape(padded_height // factor, factor, padded_width // factor, factor).max(axis=(1, 3))


def _align(
    source_rgb: np.ndarray,
    source_mask: np.ndarray,
    background: tuple[int, int, int],
    correction_degrees: float,
    *,
    max_aligned_pixels: int | None = None,
) -> tuple[np.ndarray, np.ndarray, AffineTransform]:
    height, width = source_mask.shape
    if abs(correction_degrees) < 1e-12:
        if max_aligned_pixels is not None and width * height > max_aligned_pixels:
            raise GeometryLimitError(f"aligned pixel limit exceeded: {width * height} > {max_aligned_pixels}")
        return (
            np.array(source_rgb, copy=True),
            np.array(source_mask, copy=True),
            AffineTransform.identity((width, height)),
        )

    # Pillow's positive rotation is counter-clockwise in a y-down raster.
    # The forward matrix below uses ordinary Cartesian coefficients over
    # pixel coordinates, so its mathematical angle has the opposite sign.
    radians = math.radians(-correction_degrees)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    center_x = width / 2.0
    center_y = height / 2.0
    rotation = np.asarray(
        (
            (cosine, -sine, center_x - cosine * center_x + sine * center_y),
            (sine, cosine, center_y - sine * center_x - cosine * center_y),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )
    corners = np.asarray(((0.0, 0.0, 1.0), (width, 0.0, 1.0), (0.0, height, 1.0), (width, height, 1.0)))
    mapped = (rotation @ corners.T).T
    minimum_x = math.floor(float(mapped[:, 0].min()))
    minimum_y = math.floor(float(mapped[:, 1].min()))
    maximum_x = math.ceil(float(mapped[:, 0].max()))
    maximum_y = math.ceil(float(mapped[:, 1].max()))
    aligned_width = maximum_x - minimum_x
    aligned_height = maximum_y - minimum_y
    aligned_pixels = aligned_width * aligned_height
    if max_aligned_pixels is not None and aligned_pixels > max_aligned_pixels:
        raise GeometryLimitError(f"aligned pixel limit exceeded: {aligned_pixels} > {max_aligned_pixels}")
    translation = np.asarray(((1.0, 0.0, -minimum_x), (0.0, 1.0, -minimum_y), (0.0, 0.0, 1.0)))
    forward_array = translation @ rotation
    inverse_array = np.linalg.inv(forward_array)
    forward = tuple(float(value) for value in forward_array.reshape(-1))
    inverse = tuple(float(value) for value in inverse_array.reshape(-1))
    transform = AffineTransform(
        original_size=(width, height),
        aligned_size=(aligned_width, aligned_height),
        forward=forward,
        inverse=inverse,
    )
    coefficients = tuple(float(value) for value in inverse_array[:2].reshape(-1))
    aligned_image = Image.fromarray(source_rgb, mode="RGB").transform(
        (aligned_width, aligned_height),
        Image.Transform.AFFINE,
        coefficients,
        resample=Image.Resampling.BICUBIC,
        fillcolor=background,
    )
    warped_mask_image = Image.fromarray(source_mask.astype(np.uint8) * 255, mode="L").transform(
        (aligned_width, aligned_height),
        Image.Transform.AFFINE,
        coefficients,
        resample=Image.Resampling.NEAREST,
        fillcolor=0,
    )
    return (
        np.asarray(aligned_image, dtype=np.uint8).copy(),
        np.asarray(warped_mask_image, dtype=np.uint8) > 0,
        transform,
    )


def _true_runs(values: np.ndarray) -> tuple[tuple[int, int], ...]:
    padded = np.pad(values.astype(np.int8), (1, 1))
    changes = np.diff(padded)
    starts = np.flatnonzero(changes == 1)
    stops = np.flatnonzero(changes == -1)
    return tuple((int(start), int(stop)) for start, stop in zip(starts, stops))


def _connected_components(
    mask: np.ndarray,
    *,
    max_runs: int | None = None,
    max_components: int | None = None,
) -> tuple[_Component, ...]:
    height, _ = mask.shape
    parents: list[int] = []
    runs: list[_Run] = []
    previous: list[_Run] = []

    def find(label: int) -> int:
        while parents[label] != label:
            parents[label] = parents[parents[label]]
            label = parents[label]
        return label

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            lower, higher = sorted((first_root, second_root))
            parents[higher] = lower

    for row in range(height):
        current: list[_Run] = []
        previous_index = 0
        row_runs = _true_runs(mask[row])
        if max_runs is not None and len(runs) + len(row_runs) > max_runs:
            raise GeometryLimitError(
                f"connected-component run limit exceeded: " f"{len(runs) + len(row_runs)} > {max_runs}"
            )
        for start, stop in row_runs:
            label = len(parents)
            parents.append(label)
            run = _Run(row, start, stop, label)
            while previous_index < len(previous) and previous[previous_index].stop < start:
                previous_index += 1
            overlap_index = previous_index
            while overlap_index < len(previous) and previous[overlap_index].start <= stop:
                union(label, previous[overlap_index].label)
                overlap_index += 1
            current.append(run)
            runs.append(run)
        previous = current

    grouped: dict[int, list[_Run]] = {}
    for run in runs:
        root = find(run.label)
        if root not in grouped:
            if max_components is not None and len(grouped) >= max_components:
                raise GeometryLimitError(f"connected-component count limit exceeded: " f"more than {max_components}")
            grouped[root] = []
        grouped[root].append(run)
    unordered: list[tuple[Box, int, tuple[_Run, ...]]] = []
    for component_runs in grouped.values():
        bbox = Box(
            min(run.start for run in component_runs),
            min(run.row for run in component_runs),
            max(run.stop for run in component_runs),
            max(run.row for run in component_runs) + 1,
        )
        pixels = sum(run.stop - run.start for run in component_runs)
        unordered.append((bbox, pixels, tuple(component_runs)))
    unordered.sort(key=lambda value: (value[0].top, value[0].left, value[0].bottom, value[0].right))
    return tuple(
        _Component(component_id=index, bbox=bbox, pixels=pixels, runs=component_runs)
        for index, (bbox, pixels, component_runs) in enumerate(unordered)
    )


def _body_component_height(components: tuple[_Component, ...], percentile: float) -> float:
    if not components:
        return 0.0
    body_candidates = tuple(
        component.bbox.height for component in components if component.pixels >= 4 and component.bbox.height >= 3
    )
    heights = body_candidates or tuple(component.bbox.height for component in components)
    median = float(np.percentile(heights, 50))
    estimate = float(np.percentile(heights, percentile))
    return min(estimate, 2.0 * median)


def _guard_diacritic_gaps(mask: np.ndarray, components: tuple[_Component, ...]) -> np.ndarray:
    guarded = np.array(mask, copy=True)
    if len(components) < 2:
        return guarded
    qualified_heights = tuple(
        component.bbox.height for component in components if component.pixels >= 4 and component.bbox.height >= 3
    )
    search_heights = qualified_heights or tuple(component.bbox.height for component in components)
    # Real font metrics can leave almost half an x-height between a detached
    # accent and its body (DejaVu Serif ``ё`` reaches 4/9).  Shape, overlap and
    # pixel-ratio checks below keep this wider search from joining text rows.
    maximum_gap = max(1, round(float(np.percentile(search_heights, 95)) * 0.5))
    # A page row can contain thousands of components.  The old vertical-only
    # sweep compared every accent candidate with every component in the same
    # narrow y band even though almost all pairs were horizontally disjoint.
    # Fixed-width x bins are an exact overlap index: overlapping half-open
    # boxes necessarily share at least one bin, and the original predicates
    # below remain the final authority.
    bin_width = 64
    x_bins: dict[int, list[_Component]] = {}
    for component in components:
        first_bin = component.bbox.left // bin_width
        last_bin = (component.bbox.right - 1) // bin_width
        for bin_index in range(first_bin, last_bin + 1):
            x_bins.setdefault(bin_index, []).append(component)
    x_bin_tops: dict[int, tuple[int, ...]] = {}
    for bin_index, candidates in x_bins.items():
        candidates.sort(
            key=lambda component: (component.bbox.top, component.bbox.left)
        )
        x_bin_tops[bin_index] = tuple(
            component.bbox.top for component in candidates
        )
    for upper in components:
        candidates: dict[int, _Component] = {}
        first_bin = upper.bbox.left // bin_width
        last_bin = (upper.bbox.right - 1) // bin_width
        for bin_index in range(first_bin, last_bin + 1):
            bin_candidates = x_bins.get(bin_index, ())
            tops = x_bin_tops.get(bin_index, ())
            start = bisect_left(tops, upper.bbox.bottom)
            stop = bisect_left(
                tops,
                upper.bbox.bottom + maximum_gap + 1,
            )
            for candidate in bin_candidates[start:stop]:
                candidates[candidate.component_id] = candidate
        for lower in candidates.values():
            gap = lower.bbox.top - upper.bbox.bottom
            if gap > maximum_gap:
                continue
            if gap <= 0:
                continue
            if gap > max(1, round(lower.bbox.height * 0.5)):
                continue
            overlap_left = max(upper.bbox.left, lower.bbox.left)
            overlap_right = min(upper.bbox.right, lower.bbox.right)
            if overlap_right <= overlap_left:
                continue
            overlap_ratio = (overlap_right - overlap_left) / min(upper.bbox.width, lower.bbox.width)
            upper_height_ratio = upper.bbox.height / lower.bbox.height
            upper_pixel_ratio = upper.pixels / lower.pixels
            if overlap_ratio >= 0.5 and upper_height_ratio <= 0.5 and upper_pixel_ratio <= 0.55:
                guarded[upper.bbox.bottom : lower.bbox.top, overlap_left:overlap_right] = True
    return guarded


def _orientation_candidates(mask: np.ndarray, *, horizontal: bool, minimum_length: int) -> np.ndarray:
    candidate = np.zeros_like(mask, dtype=bool)
    lines = mask if horizontal else mask.T
    output = candidate if horizontal else candidate.T
    for index, line in enumerate(lines):
        for start, stop in _true_runs(line):
            if stop - start >= minimum_length:
                output[index, start:stop] = True
    return candidate


def _has_lateral_clearance(mask: np.ndarray, bbox: Box, *, horizontal: bool) -> bool:
    band = 4
    if horizontal:
        first = mask[max(0, bbox.top - band) : bbox.top, bbox.left : bbox.right]
        second = mask[bbox.bottom : min(mask.shape[0], bbox.bottom + band), bbox.left : bbox.right]
    else:
        first = mask[bbox.top : bbox.bottom, max(0, bbox.left - band) : bbox.left]
        second = mask[bbox.top : bbox.bottom, bbox.right : min(mask.shape[1], bbox.right + band)]
    if first.size == 0 or second.size == 0:
        return False
    return max(float(first.mean()), float(second.mean())) <= 0.25


def _fill_short_gaps(mask: np.ndarray, *, horizontal: bool, maximum_gap: int) -> np.ndarray:
    filled = np.array(mask, copy=True)
    lines = filled if horizontal else filled.T
    for line in lines:
        for start, stop in _true_runs(np.logical_not(line)):
            if start > 0 and stop < len(line) and stop - start <= maximum_gap:
                line[start:stop] = True
    return filled


def _color_rule_drafts(rgb: np.ndarray, config: GeometryConfig) -> tuple[_RuleDraft, ...]:
    height, width, _ = rgb.shape
    sample_step = max(1, math.ceil(max(height, width) / 512))
    color_sample = rgb[::sample_step, ::sample_step]
    chroma = np.max(color_sample, axis=2) - np.min(color_sample, axis=2)
    if float(np.percentile(chroma, 99)) < 12.0:
        return ()
    values = rgb.astype(np.int16)
    horizontal_evidence = np.zeros((height, width), dtype=bool)
    maximum_offset = min(config.max_rule_thickness + 1, max(1, (height - 1) // 2))
    for offset in range(1, maximum_offset + 1):
        if height <= 2 * offset:
            break
        center = values[offset : height - offset]
        above = values[: height - 2 * offset]
        below = values[2 * offset :]
        differs_above = np.max(np.abs(center - above), axis=2) >= 12
        differs_below = np.max(np.abs(center - below), axis=2) >= 12
        stable_flanks = np.max(np.abs(above - below), axis=2) <= 8
        horizontal_evidence[offset : height - offset] |= differs_above & differs_below & stable_flanks
    horizontal_evidence = _fill_short_gaps(
        horizontal_evidence,
        horizontal=True,
        maximum_gap=config.max_rule_thickness,
    )

    vertical_evidence = np.zeros((height, width), dtype=bool)
    if width > 1:
        vertical_evidence[:, 1:] = np.max(np.abs(values[:, 1:] - values[:, :-1]), axis=2) >= 12
    vertical_evidence = _fill_short_gaps(
        vertical_evidence,
        horizontal=False,
        maximum_gap=config.max_rule_thickness,
    )

    horizontal_candidates: list[_RuleDraft] = []
    minimum_horizontal = min(width, config.min_rule_length)
    if minimum_horizontal >= 2:
        candidates = _orientation_candidates(
            horizontal_evidence,
            horizontal=True,
            minimum_length=minimum_horizontal,
        )
        for component in _connected_components(
            candidates,
            max_runs=config.max_runs,
            max_components=config.max_components,
        ):
            if (
                component.bbox.height <= max(config.max_rule_thickness, 1)
                and component.bbox.width >= round(width * max(0.8, config.min_rule_page_fraction))
                and component.bbox.width / component.bbox.height >= config.min_rule_aspect_ratio
            ):
                horizontal_candidates.append(_RuleDraft(RuleAxis.HORIZONTAL, component.bbox, candidates))

    if len(horizontal_candidates) < 3:
        return ()

    vertical_candidates: list[_RuleDraft] = []
    minimum_vertical = min(height, config.min_rule_length)
    if minimum_vertical >= 2 and horizontal_candidates:
        candidates = _orientation_candidates(
            vertical_evidence,
            horizontal=False,
            minimum_length=minimum_vertical,
        )
        for component in _connected_components(
            candidates,
            max_runs=config.max_runs,
            max_components=config.max_components,
        ):
            crossings = sum(
                horizontal.bbox.intersection(component.bbox) is not None for horizontal in horizontal_candidates
            )
            if (
                crossings >= 2
                and component.bbox.width <= max(config.max_rule_thickness, 1)
                and component.bbox.height / component.bbox.width >= config.min_rule_aspect_ratio
            ):
                vertical_candidates.append(_RuleDraft(RuleAxis.VERTICAL, component.bbox, candidates))
    return tuple(horizontal_candidates + vertical_candidates)


def _local_line_bands(
    network: LocalNetwork,
    *,
    horizontal: bool,
    tolerance: int,
) -> tuple[tuple[int, float, tuple[LocalLine, ...]], ...]:
    """Aggregate collinear finite fragments without extending their bboxes."""

    source = network.horizontal_lines if horizontal else network.vertical_lines
    ordered = sorted(
        source,
        key=lambda line: (
            (line.bbox[1] + line.bbox[3]) / 2.0
            if horizontal
            else (line.bbox[0] + line.bbox[2]) / 2.0
        ),
    )
    groups: list[list[LocalLine]] = []
    centers: list[float] = []
    for line in ordered:
        center = (
            (line.bbox[1] + line.bbox[3]) / 2.0
            if horizontal
            else (line.bbox[0] + line.bbox[2]) / 2.0
        )
        if groups and abs(center - centers[-1]) <= tolerance:
            groups[-1].append(line)
            centers[-1] = sum(
                (
                    (member.bbox[1] + member.bbox[3]) / 2.0
                    if horizontal
                    else (member.bbox[0] + member.bbox[2]) / 2.0
                )
                for member in groups[-1]
            ) / len(groups[-1])
        else:
            groups.append([line])
            centers.append(center)

    span_start = network.bbox[0] if horizontal else network.bbox[1]
    span_stop = network.bbox[2] if horizontal else network.bbox[3]
    span = max(1, span_stop - span_start)
    result: list[tuple[int, float, tuple[LocalLine, ...]]] = []
    for center, members in zip(centers, groups, strict=True):
        intervals = sorted(
            (
                (max(span_start, line.bbox[0]), min(span_stop, line.bbox[2]))
                if horizontal
                else (max(span_start, line.bbox[1]), min(span_stop, line.bbox[3]))
            )
            for line in members
        )
        covered = 0
        current_start, current_stop = intervals[0]
        for start, stop in intervals[1:]:
            if start <= current_stop + tolerance:
                current_stop = max(current_stop, stop)
            else:
                covered += max(0, current_stop - current_start)
                current_start, current_stop = start, stop
        covered += max(0, current_stop - current_start)
        result.append((round(center), covered / span, tuple(members)))
    return tuple(result)


@dataclass(frozen=True)
class _EdgeOpenCell:
    """Three observed lines proving one cell truncated by a raster edge."""

    edge: str
    cross_coordinate: int
    span_start: int
    span_stop: int
    lines: tuple[LocalLine, LocalLine, LocalLine]


def _edge_open_grid_lines(
    lines: tuple[LocalLine, ...],
    *,
    width: int,
    height: int,
    config: GeometryConfig,
    tolerance: int,
) -> tuple[LocalLine, ...]:
    """Return exact finite lines from repeated cells cut by one image edge.

    A screenshot or camera crop can remove the closing border of a card row.
    Such a row has no ordinary three-by-three lattice witness, but every
    visible cell still has one crossbar and two rails which start at that
    crossbar and continue to the same raster edge.  Three adjacent witnesses
    prove a repeated structure.  The returned lines keep their detector
    bboxes; this function never projects a rail, fills a corner, or draws the
    missing edge.
    """

    minimum_crossbar = 4 * config.min_rule_length
    maximum_thickness = max(2 * tolerance + 1, 3 * config.max_rule_thickness)

    def interval_gap(point: int, interval: tuple[int, int]) -> int:
        return max(interval[0] - point, point - interval[1], 0)

    def cell_candidates(edge: str) -> tuple[_EdgeOpenCell, ...]:
        horizontal_crossbar = edge in ("top", "bottom")
        crossbar_axis = "horizontal" if horizontal_crossbar else "vertical"
        rail_axis = "vertical" if horizontal_crossbar else "horizontal"
        crossbars = tuple(
            line
            for line in lines
            if line.axis == crossbar_axis
            and line.length >= minimum_crossbar
            and line.thickness <= maximum_thickness
        )
        rails = tuple(
            line
            for line in lines
            if line.axis == rail_axis and line.thickness <= maximum_thickness
        )
        values: list[_EdgeOpenCell] = []
        for crossbar in crossbars:
            if horizontal_crossbar:
                coordinate = (crossbar.bbox[1] + crossbar.bbox[3]) // 2
                span = (crossbar.bbox[0], crossbar.bbox[2])
            else:
                coordinate = (crossbar.bbox[0] + crossbar.bbox[2]) // 2
                span = (crossbar.bbox[1], crossbar.bbox[3])
            minimum_rail = max(
                2 * config.min_rule_length,
                round(0.25 * crossbar.length),
            )

            eligible: list[LocalLine] = []
            for rail in rails:
                if rail.length < minimum_rail:
                    continue
                if edge == "bottom":
                    touches_edge = rail.bbox[3] >= height - tolerance
                    starts_at_crossbar = abs(rail.bbox[1] - coordinate) <= 2 * tolerance
                elif edge == "top":
                    touches_edge = rail.bbox[1] <= tolerance
                    starts_at_crossbar = abs(rail.bbox[3] - coordinate) <= 2 * tolerance
                elif edge == "right":
                    touches_edge = rail.bbox[2] >= width - tolerance
                    starts_at_crossbar = abs(rail.bbox[0] - coordinate) <= 2 * tolerance
                else:
                    touches_edge = rail.bbox[0] <= tolerance
                    starts_at_crossbar = abs(rail.bbox[2] - coordinate) <= 2 * tolerance
                if touches_edge and starts_at_crossbar:
                    eligible.append(rail)

            def nearest(endpoint: int) -> tuple[LocalLine, ...]:
                return tuple(
                    sorted(
                        (
                            rail
                            for rail in eligible
                            if interval_gap(
                                endpoint,
                                (
                                    (rail.bbox[0], rail.bbox[2])
                                    if horizontal_crossbar
                                    else (rail.bbox[1], rail.bbox[3])
                                ),
                            )
                            <= 2 * tolerance
                        ),
                        key=lambda rail: (
                            interval_gap(
                                endpoint,
                                (
                                    (rail.bbox[0], rail.bbox[2])
                                    if horizontal_crossbar
                                    else (rail.bbox[1], rail.bbox[3])
                                ),
                            ),
                            -rail.length,
                            rail.bbox[1],
                            rail.bbox[0],
                            rail.bbox[3],
                            rail.bbox[2],
                        ),
                    )
                )

            first = nearest(span[0])
            second = nearest(span[1])
            if not first or not second or first[0] == second[0]:
                continue
            values.append(
                _EdgeOpenCell(
                    edge=edge,
                    cross_coordinate=coordinate,
                    span_start=span[0],
                    span_stop=span[1],
                    lines=(crossbar, first[0], second[0]),
                )
            )
        return tuple(values)

    # Gradient halos may produce two nearly identical crossbars.  They are
    # one spatial witness and must not satisfy the repetition requirement by
    # themselves.
    distinct: list[_EdgeOpenCell] = []
    candidates = tuple(
        cell
        for edge in ("top", "bottom", "left", "right")
        for cell in cell_candidates(edge)
    )
    for cell in sorted(
        candidates,
        key=lambda value: (
            value.edge,
            value.cross_coordinate,
            value.span_start,
            value.span_stop,
        ),
    ):
        duplicate = False
        for accepted in distinct:
            if (
                accepted.edge != cell.edge
                or abs(accepted.cross_coordinate - cell.cross_coordinate) > 2 * tolerance
            ):
                continue
            overlap = min(accepted.span_stop, cell.span_stop) - max(
                accepted.span_start,
                cell.span_start,
            )
            shorter = min(
                accepted.span_stop - accepted.span_start,
                cell.span_stop - cell.span_start,
            )
            if overlap >= 0.75 * shorter:
                duplicate = True
                break
        if not duplicate:
            distinct.append(cell)

    parents = list(range(len(distinct)))

    def find(value: int) -> int:
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parents[second_root] = first_root

    for first, left in enumerate(distinct):
        for second in range(first + 1, len(distinct)):
            right = distinct[second]
            if (
                left.edge == right.edge
                and abs(left.cross_coordinate - right.cross_coordinate) <= 2 * tolerance
                and max(
                    left.span_start - right.span_stop,
                    right.span_start - left.span_stop,
                    0,
                )
                <= config.min_rule_length
            ):
                union(first, second)

    families: dict[int, list[_EdgeOpenCell]] = {}
    for index, cell in enumerate(distinct):
        families.setdefault(find(index), []).append(cell)
    selected = {
        line
        for family in families.values()
        if len(family) >= 3
        for cell in family
        for line in cell.lines
    }
    return tuple(
        sorted(
            selected,
            key=lambda line: (
                0 if line.axis == "horizontal" else 1,
                line.bbox[1],
                line.bbox[0],
                line.bbox[3],
                line.bbox[2],
            ),
        )
    )


def _local_structure_rule_drafts(
    rgb: np.ndarray,
    config: GeometryConfig,
) -> tuple[_RuleDraft, ...]:
    """Promote primary lattices and bounded low-contrast continuations.

    Contrast three is intentionally not an independent rule detector.  It may
    only extend one already proven primary core along the core's observed
    vertical tracks.  This recovers a dark continuation below a rubric while
    preventing low-contrast text from joining adjacent card networks.
    """

    tolerance = max(3, config.max_rule_thickness)
    minimum_extent = 4 * config.min_rule_length

    def detect(contrast: int) -> LocalStructureResult:
        return detect_local_structures(
            rgb,
            LocalStructureConfig(
                minimum_contrast=contrast,
                minimum_length=config.min_rule_length,
                maximum_gap=1,
                maximum_line_thickness=max(1, config.max_rule_thickness),
                junction_tolerance=2,
            ),
        )

    def describe(
        networks: tuple[LocalNetwork, ...],
    ) -> tuple[
        tuple[
            LocalNetwork,
            tuple[tuple[int, float, tuple[LocalLine, ...]], ...],
            tuple[tuple[int, float, tuple[LocalLine, ...]], ...],
        ],
        ...,
    ]:
        values = []
        for network in networks:
            if network.bbox[2] - network.bbox[0] < minimum_extent or network.bbox[3] - network.bbox[1] < minimum_extent:
                continue
            if _is_photographic_rule_region(rgb, network.bbox):
                continue
            values.append(
                (
                    network,
                    _local_line_bands(
                        network,
                        horizontal=True,
                        tolerance=tolerance,
                    ),
                    _local_line_bands(
                        network,
                        horizontal=False,
                        tolerance=tolerance,
                    ),
                )
            )
        return tuple(values)

    def bands_cross(
        horizontal: tuple[int, float, tuple[LocalLine, ...]],
        vertical: tuple[int, float, tuple[LocalLine, ...]],
    ) -> bool:
        """Return whether finite source lines form an observed junction."""

        horizontal_coordinate = horizontal[0]
        vertical_coordinate = vertical[0]
        return any(
            horizontal_line.bbox[0] - tolerance
            <= vertical_coordinate
            <= horizontal_line.bbox[2] + tolerance
            and vertical_line.bbox[1] - tolerance
            <= horizontal_coordinate
            <= vertical_line.bbox[3] + tolerance
            for horizontal_line in horizontal[2]
            for vertical_line in vertical[2]
        )

    primary_contrast = max(1, min(config.min_rule_contrast, 8))
    primary_result = detect(primary_contrast)
    descriptors = describe(primary_result.networks)

    selected: set[LocalLine] = set(
        _edge_open_grid_lines(
            primary_result.lines,
            width=rgb.shape[1],
            height=rgb.shape[0],
            config=config,
            tolerance=tolerance,
        )
    )
    primary_cores: list[
        tuple[
            LocalNetwork,
            tuple[tuple[int, float, tuple[LocalLine, ...]], ...],
            tuple[tuple[int, float, tuple[LocalLine, ...]], ...],
        ]
    ] = []
    for core, horizontal_bands, vertical_bands in descriptors:
        horizontal_candidates = tuple(
            band for band in horizontal_bands if band[1] >= 0.55
        )
        vertical_candidates = tuple(
            band for band in vertical_bands if band[1] >= 0.55
        )
        # Coverage alone is not structural proof: an underline or the flat
        # edge of a glyph may span most of a small connected network after it
        # touches one table rail.  A finite grid line needs two independently
        # observed perpendicular junctions.  Apply the condition in both
        # directions so rejected text cannot in turn prove a vertical stem.
        strong_horizontal = tuple(
            band
            for band in horizontal_candidates
            if sum(bands_cross(band, vertical) for vertical in vertical_candidates)
            >= 2
        )
        strong_vertical = tuple(
            band
            for band in vertical_candidates
            if sum(bands_cross(horizontal, band) for horizontal in strong_horizontal)
            >= 2
        )
        strong_horizontal = tuple(
            band
            for band in strong_horizontal
            if sum(bands_cross(band, vertical) for vertical in strong_vertical)
            >= 2
        )
        if len(strong_horizontal) < 3 or len(strong_vertical) < 3:
            continue
        primary_cores.append((core, strong_horizontal, strong_vertical))
        for _, _, lines in (*strong_horizontal, *strong_vertical):
            selected.update(lines)

        core_height = core.bbox[3] - core.bbox[1]
        core_coordinates = tuple(coordinate for coordinate, _, _ in strong_horizontal)
        for flank, flank_horizontal, flank_vertical in descriptors:
            if flank is core:
                continue
            overlap = min(core.bbox[3], flank.bbox[3]) - max(core.bbox[1], flank.bbox[1])
            if overlap <= 0:
                continue
            flank_height = flank.bbox[3] - flank.bbox[1]
            if overlap / max(1, min(core_height, flank_height)) < 0.75:
                continue
            horizontal_gap = max(
                core.bbox[0] - flank.bbox[2],
                flank.bbox[0] - core.bbox[2],
                0,
            )
            if horizontal_gap > max(
                config.min_rule_length,
                round(0.08 * max(core_height, flank_height)),
            ):
                continue
            repeated_horizontal = tuple(
                band
                for band in flank_horizontal
                if band[1] >= 0.55
                and any(abs(band[0] - coordinate) <= 2 * tolerance for coordinate in core_coordinates)
            )
            flank_vertical_support = tuple(band for band in flank_vertical if band[1] >= 0.45)
            repeated_horizontal = tuple(
                band
                for band in repeated_horizontal
                if sum(
                    bands_cross(band, vertical)
                    for vertical in flank_vertical_support
                )
                >= 2
            )
            flank_vertical_support = tuple(
                band
                for band in flank_vertical_support
                if sum(
                    bands_cross(horizontal, band)
                    for horizontal in repeated_horizontal
                )
                >= 2
            )
            if len(repeated_horizontal) < 2 or len(flank_vertical_support) < 2:
                continue
            for _, _, lines in (*repeated_horizontal, *flank_vertical_support):
                selected.update(lines)

    low_contrast = max(1, min(config.min_rule_contrast, 3))
    if primary_cores and low_contrast < primary_contrast:
        low_descriptors = describe(detect(low_contrast).networks)

        for low_network, low_horizontal, low_vertical in low_descriptors:
            low_width = low_network.bbox[2] - low_network.bbox[0]
            low_vertical_support = tuple(band for band in low_vertical if band[1] >= 0.45)
            matches: list[
                tuple[
                    LocalNetwork,
                    tuple[tuple[int, float, tuple[LocalLine, ...]], ...],
                    tuple[tuple[int, float, tuple[LocalLine, ...]], ...],
                ]
            ] = []
            for core, core_horizontal, core_vertical in primary_cores:
                overlap_width = max(
                    0,
                    min(core.bbox[2], low_network.bbox[2]) - max(core.bbox[0], low_network.bbox[0]),
                )
                shared_primary_coordinates = tuple(
                    coordinate
                    for coordinate, _, _ in core_vertical
                    if any(
                        abs(coordinate - low_coordinate) <= 2 * tolerance
                        for low_coordinate, _, _ in low_vertical_support
                    )
                )
                left_overhang = max(
                    0,
                    core.bbox[0] - low_network.bbox[0],
                )
                right_overhang = max(
                    0,
                    low_network.bbox[2] - core.bbox[2],
                )
                vertical_extension = (
                    low_network.bbox[1] < core.bbox[1] - tolerance or low_network.bbox[3] > core.bbox[3] + tolerance
                )
                if (
                    overlap_width / max(1, low_width) >= 0.50
                    and len(shared_primary_coordinates) >= 3
                    and max(left_overhang, right_overhang) <= 2 * tolerance
                    and vertical_extension
                ):
                    matches.append((core, core_horizontal, core_vertical))

            # A sensitive network spanning two independently proven cards is
            # not an extension of either card.  Object grouping may relate
            # those cards later without inventing one wide rule network here.
            if len(matches) != 1:
                continue
            matched_core, core_horizontal, core_vertical = matches[0]
            shared_vertical = tuple(
                band
                for band in low_vertical_support
                if any(abs(band[0] - coordinate) <= 2 * tolerance for coordinate, _, _ in core_vertical)
            )
            if len(shared_vertical) < 3:
                continue

            core_horizontal_coordinates = tuple(coordinate for coordinate, _, _ in core_horizontal)
            strong_low_horizontal = tuple(
                band
                for band in low_horizontal
                if band[1] >= 0.55
                and (
                    band[0] < matched_core.bbox[1] - tolerance
                    or band[0] > matched_core.bbox[3] + tolerance
                    or any(abs(band[0] - coordinate) <= 2 * tolerance for coordinate in core_horizontal_coordinates)
                )
                and sum(bands_cross(band, vertical) for vertical in shared_vertical) >= 3
            )
            supported_vertical = tuple(
                band
                for band in shared_vertical
                if sum(bands_cross(horizontal, band) for horizontal in strong_low_horizontal) >= 3
            )
            supported_horizontal = tuple(
                band
                for band in strong_low_horizontal
                if sum(bands_cross(band, vertical) for vertical in supported_vertical) >= 3
            )
            if len(supported_horizontal) < 3 or len(supported_vertical) < 3:
                continue

            # Low-contrast observations contain the primary edge too.  Replace
            # that core's shorter lines rather than retaining overlapping rule
            # drafts, then add the original finite low-detector bboxes.
            primary_lines = {line for _, _, lines in (*core_horizontal, *core_vertical) for line in lines}
            selected.difference_update(primary_lines)
            for _, _, lines in (*supported_horizontal, *supported_vertical):
                selected.update(lines)

    if not selected:
        return ()
    candidate_mask = np.zeros(rgb.shape[:2], dtype=bool)
    for line in selected:
        left, top, right, bottom = line.bbox
        candidate_mask[top:bottom, left:right] = True
    return tuple(
        _RuleDraft(
            axis=(
                RuleAxis.HORIZONTAL
                if line.axis == "horizontal"
                else RuleAxis.VERTICAL
            ),
            bbox=Box(*line.bbox),
            candidate_mask=candidate_mask,
            claim_full_bbox=True,
        )
        for line in sorted(
            selected,
            key=lambda value: (
                0 if value.axis == "horizontal" else 1,
                value.bbox[1],
                value.bbox[0],
                value.bbox[3],
                value.bbox[2],
            ),
        )
    )


def _is_photographic_rule_region(
    rgb: np.ndarray,
    bbox: tuple[int, int, int, int],
) -> bool:
    """Reject line lattices explained by photographic texture.

    Building facades, bridges, book covers, and product photographs contain
    many real perpendicular edges.  Junction count alone therefore cannot
    prove a document table.  A photograph differs from a printed or coloured
    table by combining high quantized colour entropy with dense local
    gradients.  Sampling keeps this gate bounded and independent of page or
    fixture dimensions; flat coloured cells and low-contrast graph paper stay
    eligible as physical rule evidence.
    """

    left, top, right, bottom = bbox
    crop = rgb[top:bottom, left:right]
    if crop.size == 0:
        return False
    step = max(1, math.ceil(max(crop.shape[:2]) / 256))
    sample = crop[::step, ::step].astype(np.int16, copy=False)
    if min(sample.shape[:2]) < 8:
        return False

    horizontal = np.max(
        np.abs(sample[:, 1:] - sample[:, :-1]),
        axis=2,
    )
    vertical = np.max(
        np.abs(sample[1:] - sample[:-1]),
        axis=2,
    )
    gradients = np.zeros(sample.shape[:2], dtype=np.int16)
    gradients[:, 1:] = np.maximum(gradients[:, 1:], horizontal)
    gradients[1:] = np.maximum(gradients[1:], vertical)
    dense_gradient_fraction = float(np.mean(gradients >= 32))

    quantized = sample.astype(np.uint16, copy=False) // 16
    keys = (
        quantized[:, :, 0] * 256
        + quantized[:, :, 1] * 16
        + quantized[:, :, 2]
    )
    counts = np.bincount(keys.reshape(-1), minlength=4096)
    occupied = counts[counts > 0].astype(np.float64)
    probabilities = occupied / max(1.0, float(occupied.sum()))
    entropy = float(
        -np.sum(probabilities * np.log2(probabilities))
    )
    return (
        len(occupied) >= 256
        and entropy >= 5.0
        and dense_gradient_fraction >= 0.12
    )


def _page_edge_band(
    candidate: np.ndarray,
    bbox: Box,
    *,
    horizontal: bool,
) -> Box | None:
    """Clamp a frame component to its dense edge-connected raster band.

    Text placed at a zero-width margin can be 8-connected to a thick frame.
    Connected-component bounds then include the adjacent glyph row even though
    that row is not page-wide.  Only consecutive edge rows/columns with at
    least 80 percent axial coverage belong to the frame.
    """

    perpendicular_size = candidate.shape[0] if horizontal else candidate.shape[1]
    # A perspective-corrected border can remain physical for only part of the
    # page height/width.  Measure density along the observed finite component,
    # not against a fictitious page-long continuation.
    if horizontal:
        projection = candidate[:, bbox.left : bbox.right].sum(
            axis=1,
            dtype=np.int64,
        )
        axial_span = bbox.width
    else:
        projection = candidate[bbox.top : bbox.bottom, :].sum(
            axis=0,
            dtype=np.int64,
        )
        axial_span = bbox.height
    dense = projection >= math.ceil(axial_span * 0.8)
    perpendicular_start = bbox.top if horizontal else bbox.left
    perpendicular_stop = bbox.bottom if horizontal else bbox.right
    if perpendicular_start == 0:
        stop = 0
        while stop < perpendicular_size and dense[stop]:
            stop += 1
        if stop == 0:
            return None
        return Box(bbox.left, 0, bbox.right, stop) if horizontal else Box(0, bbox.top, stop, bbox.bottom)
    if perpendicular_stop == perpendicular_size:
        start = perpendicular_size
        while start > 0 and dense[start - 1]:
            start -= 1
        if start == perpendicular_size:
            return None
        return (
            Box(bbox.left, start, bbox.right, perpendicular_size)
            if horizontal
            else Box(start, bbox.top, perpendicular_size, bbox.bottom)
        )
    return None


def _closed_page_frame(
    drafts: list[_RuleDraft],
    *,
    width: int,
    height: int,
) -> list[_RuleDraft]:
    """Accept a complete frame or an observed finite page-edge corner.

    Perspective correction can leave only two or three sides inside the
    aligned raster.  Requiring a fictitious fourth side turns the retained
    border into text ownership and expands every nearby object crop to the
    page edge.  One horizontal plus one vertical finite edge is sufficient
    physical evidence; a lone edge is still left untouched so an edge-aligned
    banner cannot disappear as a guessed frame.
    """

    has_top = any(draft.axis is RuleAxis.HORIZONTAL and draft.bbox.top == 0 for draft in drafts)
    has_bottom = any(draft.axis is RuleAxis.HORIZONTAL and draft.bbox.bottom == height for draft in drafts)
    has_left = any(draft.axis is RuleAxis.VERTICAL and draft.bbox.left == 0 for draft in drafts)
    has_right = any(draft.axis is RuleAxis.VERTICAL and draft.bbox.right == width for draft in drafts)
    has_horizontal = has_top or has_bottom
    has_vertical = has_left or has_right
    has_corner = has_horizontal and has_vertical
    # Two horizontal crop limits are common after projector dewarp.  Two
    # vertical edge strokes alone are not enough: a single oversized display
    # word can legitimately touch both sides of a tight raster.
    has_opposite_pair = has_top and has_bottom
    return drafts if has_corner or has_opposite_pair else []


def _detect_rules(
    mask: np.ndarray,
    config: GeometryConfig,
    rgb: np.ndarray | None = None,
) -> tuple[_RuleDraft, ...]:
    height, width = mask.shape
    horizontal_minimum = min(width, config.min_rule_length)
    vertical_minimum = min(height, config.min_rule_length)
    effective_thickness = max(config.max_rule_thickness, round(min(width, height) * 0.003))

    def collect(source: np.ndarray, *, frame_edges: bool) -> list[_RuleDraft]:
        values: list[_RuleDraft] = []
        for axis, horizontal, minimum in (
            (RuleAxis.HORIZONTAL, True, horizontal_minimum),
            (RuleAxis.VERTICAL, False, vertical_minimum),
        ):
            if minimum < 2:
                continue
            candidate = _orientation_candidates(source, horizontal=horizontal, minimum_length=minimum)
            for component in _connected_components(
                candidate,
                max_runs=config.max_runs,
                max_components=config.max_components,
            ):
                thickness = component.bbox.height if horizontal else component.bbox.width
                length = component.bbox.width if horizontal else component.bbox.height
                aspect_ratio = length / max(1, thickness)
                perpendicular_size = height if horizontal else width
                frame_thickness_limit = max(16, 4 * effective_thickness)
                frame_bbox = _page_edge_band(
                    candidate,
                    component.bbox,
                    horizontal=horizontal,
                )
                frame_thickness = (
                    0
                    if frame_bbox is None
                    else frame_bbox.height if horizontal else frame_bbox.width
                )
                is_frame_edge = (
                    length
                    >= max(
                        config.min_rule_length,
                        round((width if horizontal else height) * 0.20),
                    )
                    and frame_bbox is not None
                    and frame_thickness <= frame_thickness_limit
                )
                is_structural_rule = (
                    thickness <= effective_thickness
                    and length >= minimum
                    and aspect_ratio >= config.min_rule_aspect_ratio
                    and _has_lateral_clearance(source, component.bbox, horizontal=horizontal)
                )
                if frame_edges and is_frame_edge:
                    assert frame_bbox is not None
                    # The dense edge core proves the band.  Claim the complete
                    # finite foreground component so an antialiased/tapered
                    # halo is not left behind as a narrow text segment.  A
                    # component joined to a real glyph exceeds the guarded
                    # thickness above and is therefore never accepted here.
                    component_thickness = (
                        component.bbox.height
                        if horizontal
                        else component.bbox.width
                    )
                    complete_component_is_bounded = (
                        component_thickness <= frame_thickness_limit
                    )
                    axis_size = width if horizontal else height
                    partial_axis = length < round(axis_size * 0.80)
                    values.append(
                        _RuleDraft(
                            axis,
                            (
                                component.bbox
                                if complete_component_is_bounded and partial_axis
                                else frame_bbox
                            ),
                            source,
                            claim_full_bbox=(
                                complete_component_is_bounded and partial_axis
                            ),
                        )
                    )
                elif not frame_edges and is_structural_rule:
                    values.append(_RuleDraft(axis, component.bbox, candidate))
        return values

    # Page frames are claimed first.  Structural candidates are then measured
    # on the interior mask so adjacent glyph strokes cannot inherit the frame's
    # length and turn the whole page into a false grid network.
    frame_drafts = _closed_page_frame(
        collect(mask, frame_edges=True),
        width=width,
        height=height,
    )
    frame_mask = np.zeros_like(mask, dtype=bool)
    for draft in frame_drafts:
        box = draft.bbox
        frame_mask[box.top : box.bottom, box.left : box.right] |= draft.candidate_mask[
            box.top : box.bottom,
            box.left : box.right,
        ]
    candidates = collect(np.logical_and(mask, np.logical_not(frame_mask)), frame_edges=False)
    accepted_indexes: set[int] = set()
    for index, candidate in enumerate(candidates):
        axis_length = width if candidate.axis is RuleAxis.HORIZONTAL else height
        length = candidate.bbox.width if candidate.axis is RuleAxis.HORIZONTAL else candidate.bbox.height
        if length >= max(
            2 * config.min_rule_length,
            round(axis_length * max(0.8, config.min_rule_page_fraction)),
        ):
            accepted_indexes.add(index)

    unseen = set(range(len(candidates)))
    while unseen:
        seed = unseen.pop()
        network = {seed}
        frontier = [seed]
        while frontier:
            current = frontier.pop()
            current_candidate = candidates[current]
            neighbors = {
                index
                for index in unseen
                if candidates[index].axis is not current_candidate.axis
                and candidates[index].bbox.intersection(current_candidate.bbox) is not None
            }
            unseen.difference_update(neighbors)
            network.update(neighbors)
            frontier.extend(neighbors)
        horizontal_count = sum(candidates[index].axis is RuleAxis.HORIZONTAL for index in network)
        vertical_count = len(network) - horizontal_count
        network_bbox = Box.union(candidates[index].bbox for index in network)
        if (
            horizontal_count >= 2
            and vertical_count >= 2
            and network_bbox.width >= 2 * config.min_rule_length
            and network_bbox.height >= 2 * config.min_rule_length
        ):
            accepted_indexes.update(network)
    # A genuine long divider can prove a shorter perpendicular divider at
    # their crossing.  Propagate only from already accepted structural rules;
    # isolated full-height glyph strokes therefore keep the absolute guard.
    while True:
        promoted = {
            index
            for index, candidate in enumerate(candidates)
            if index not in accepted_indexes
            and any(
                candidate.axis is not candidates[accepted].axis
                and candidate.bbox.intersection(candidates[accepted].bbox) is not None
                for accepted in accepted_indexes
            )
        }
        if not promoted:
            break
        accepted_indexes.update(promoted)
    drafts = frame_drafts + [candidates[index] for index in sorted(accepted_indexes)]
    if rgb is not None:
        drafts.extend(_color_rule_drafts(rgb, config))
        local_drafts = _local_structure_rule_drafts(rgb, config)

        def covered_by_proven_rule(local: _RuleDraft) -> bool:
            """Reject a gradient edge already contained by a darker rule.

            RGB gradients observe both sides of a rasterized border.  A dark
            two-pixel table rule can therefore yield another line at the
            transition from that rule into a filled cell.  Local evidence is
            retained when its finite span extends the proven rule; only a
            physically contained duplicate is suppressed here.
            """

            tolerance = max(1, config.max_rule_thickness)
            for proven in drafts:
                if proven.axis is not local.axis:
                    continue
                if local.axis is RuleAxis.HORIZONTAL:
                    perpendicular_gap = max(
                        proven.bbox.top - local.bbox.bottom,
                        local.bbox.top - proven.bbox.bottom,
                        0,
                    )
                    contained = (
                        proven.bbox.left - tolerance <= local.bbox.left
                        and local.bbox.right <= proven.bbox.right + tolerance
                    )
                else:
                    perpendicular_gap = max(
                        proven.bbox.left - local.bbox.right,
                        local.bbox.left - proven.bbox.right,
                        0,
                    )
                    contained = (
                        proven.bbox.top - tolerance <= local.bbox.top
                        and local.bbox.bottom <= proven.bbox.bottom + tolerance
                    )
                if perpendicular_gap <= tolerance and contained:
                    return True
            return False

        drafts.extend(
            draft
            for draft in local_drafts
            if not covered_by_proven_rule(draft)
        )
    return tuple(
        sorted(
            drafts,
            key=lambda draft: (
                0 if draft.axis is RuleAxis.HORIZONTAL else 1,
                draft.bbox.top,
                draft.bbox.left,
                draft.bbox.bottom,
                draft.bbox.right,
            ),
        )
    )


def _expand_rule_bands(
    drafts: tuple[_RuleDraft, ...],
    foreground: np.ndarray,
    config: GeometryConfig,
) -> tuple[_RuleDraft, ...]:
    """Recover a rasterized rule network's low-contrast halo.

    PDF antialiasing can render one physical border as a dark core plus a
    second, nearly-background column band.  Leaving that dense halo in the
    lossless ownership mask reconnects every filled row around the extracted
    core.  Expansion is perpendicular-only, projection-proved (80% dense),
    and bounded by the same scale used for accepted rule thickness.
    """

    height, width = foreground.shape
    maximum_halo = max(
        config.max_rule_thickness,
        round(min(width, height) * 0.003),
    )
    values: list[_RuleDraft] = []
    for draft in drafts:
        if draft.axis is not RuleAxis.VERTICAL:
            values.append(draft)
            continue
        search_left = max(0, draft.bbox.left - maximum_halo)
        search_right = min(width, draft.bbox.right + maximum_halo)
        local = foreground[
            draft.bbox.top : draft.bbox.bottom,
            search_left:search_right,
        ]
        minimum_pixels = math.ceil(local.shape[0] * 0.8)
        dense_columns = np.flatnonzero(
            local.sum(axis=0, dtype=np.int64) >= minimum_pixels
        )
        if dense_columns.size:
            # A rasterized border can leave a one-pixel, low-contrast corner
            # immediately beside the projection-dense band.  Claim exactly
            # that bounded adjacency as rule evidence too; eligible pixels
            # remain losslessly counted by ``_materialize_rules``.
            left = max(
                search_left,
                search_left + int(dense_columns[0]) - 1,
            )
            right = min(
                search_right,
                search_left + int(dense_columns[-1]) + 2,
            )
            bbox = Box(left, draft.bbox.top, right, draft.bbox.bottom)
        else:  # pragma: no cover - accepted core is itself normally dense
            bbox = draft.bbox
        values.append(
            _RuleDraft(
                axis=draft.axis,
                bbox=bbox,
                candidate_mask=draft.candidate_mask,
                claim_full_bbox=True,
            )
        )
    vertical = tuple(
        draft for draft in values if draft.axis is RuleAxis.VERTICAL
    )
    completed: list[_RuleDraft] = []
    for draft in values:
        if draft.axis is RuleAxis.VERTICAL:
            completed.append(draft)
            continue
        crossings = tuple(
            candidate
            for candidate in vertical
            if candidate.bbox.top < draft.bbox.bottom
            and draft.bbox.top < candidate.bbox.bottom
            and candidate.bbox.left <= draft.bbox.right
            and draft.bbox.left <= candidate.bbox.right
        )
        bbox = (
            draft.bbox
            if not crossings
            else Box(
                min(draft.bbox.left, *(item.bbox.left for item in crossings)),
                draft.bbox.top,
                max(draft.bbox.right, *(item.bbox.right for item in crossings)),
                draft.bbox.bottom,
            )
        )
        completed.append(
            _RuleDraft(
                axis=draft.axis,
                bbox=bbox,
                candidate_mask=draft.candidate_mask,
                claim_full_bbox=True,
            )
        )
    return tuple(completed)


def _materialize_rules(
    drafts: tuple[_RuleDraft, ...],
    foreground: np.ndarray,
    transform: AffineTransform,
) -> tuple[tuple[Rule, ...], np.ndarray]:
    claimed = np.zeros_like(foreground, dtype=bool)
    values: list[Rule] = []
    axis_counts = {RuleAxis.HORIZONTAL: 0, RuleAxis.VERTICAL: 0}
    for draft in drafts:
        box = draft.bbox
        foreground_band = foreground[
            box.top : box.bottom,
            box.left : box.right,
        ]
        eligible = (
            foreground_band
            if draft.claim_full_bbox
            else np.logical_and(
                draft.candidate_mask[
                    box.top : box.bottom,
                    box.left : box.right,
                ],
                foreground_band,
            )
        )
        available = np.logical_and(
            eligible,
            np.logical_not(claimed[box.top : box.bottom, box.left : box.right]),
        )
        pixels = int(available.sum())
        if pixels == 0:
            continue
        claimed[box.top : box.bottom, box.left : box.right] |= eligible
        index = axis_counts[draft.axis]
        axis_counts[draft.axis] += 1
        prefix = "h" if draft.axis is RuleAxis.HORIZONTAL else "v"
        values.append(
            Rule(
                rule_id=f"rule-{prefix}-{index:06d}",
                bbox=box,
                source_bbox=transform.box_to_source(box),
                axis=draft.axis,
                foreground_pixels=pixels,
                strength=float(eligible.sum() / box.area),
            )
        )
    return tuple(values), claimed


def _near_collinear_rule(
    axis: RuleAxis,
    bbox: Box,
    structural_bands: tuple[tuple[RuleAxis, Box], ...],
    config: GeometryConfig,
) -> bool:
    perpendicular_tolerance = 0
    aligned: list[Box] = []
    for candidate_axis, candidate in structural_bands:
        if candidate_axis is not axis:
            continue
        if axis is RuleAxis.HORIZONTAL:
            perpendicular_distance = max(
                candidate.top - bbox.bottom,
                bbox.top - candidate.bottom,
                0,
            )
            axial_distance = max(
                candidate.left - bbox.right,
                bbox.left - candidate.right,
                0,
            )
        else:
            perpendicular_distance = max(
                candidate.left - bbox.right,
                bbox.left - candidate.right,
                0,
            )
            axial_distance = max(
                candidate.top - bbox.bottom,
                bbox.top - candidate.bottom,
                0,
            )
        if perpendicular_distance > perpendicular_tolerance:
            continue
        candidate_length = (
            candidate.width
            if axis is RuleAxis.HORIZONTAL
            else candidate.height
        )
        bbox_length = bbox.width if axis is RuleAxis.HORIZONTAL else bbox.height
        if min(candidate_length, bbox_length) / max(
            candidate_length,
            bbox_length,
        ) < 0.25:
            continue
        aligned.append(candidate)
        axial_length = bbox.width if axis is RuleAxis.HORIZONTAL else bbox.height
        axial_tolerance = max(
            2 * config.min_rule_length,
            4 * axial_length,
        )
        if axial_distance <= axial_tolerance:
            return True
    # Repeated collinear bands are document-level grid evidence even when the
    # current fragment is separated from each tier by a larger blank region.
    return len(aligned) >= 2


def _thin_line_axis(
    bbox: Box,
    pixels: int,
    structural_bands: tuple[tuple[RuleAxis, Box], ...],
    config: GeometryConfig,
) -> RuleAxis | None:
    """Classify rule leakage while retaining short punctuation strokes.

    Every candidate here requires a nearby collinear structural band.  The
    recovery caller separately recognizes unsupported horizontal underlines
    from raster clearance; keeping that proof out of this bbox-only helper
    prevents detached display-font strokes from becoming page structure.
    """

    if pixels <= 0 or pixels / bbox.area < 0.7:
        return None
    maximum_thickness = config.max_rule_thickness
    for axis, thickness, length in (
        (RuleAxis.HORIZONTAL, bbox.height, bbox.width),
        (RuleAxis.VERTICAL, bbox.width, bbox.height),
    ):
        if thickness > maximum_thickness:
            continue
        aspect_ratio = length / max(1, thickness)
        if (
            length >= max(8, 3 * thickness)
            and aspect_ratio >= 4.0
            and _near_collinear_rule(axis, bbox, structural_bands, config)
        ):
            return axis
    return None


def _unsupported_horizontal_line_component(
    component: _Component,
    non_rule_foreground: np.ndarray,
    components: tuple[_Component, ...],
    body_height: float,
    config: GeometryConfig,
) -> bool:
    bbox = component.bbox
    if not (
        bbox.height <= config.max_rule_thickness
        and bbox.height <= max(2, math.floor(0.35 * body_height))
        and bbox.width >= config.min_rule_length
        and bbox.width / bbox.height >= config.min_rule_aspect_ratio
        and component.pixels / bbox.area >= 0.7
    ):
        return False
    # This fallback is for a detached ownership-only underline.  A thin
    # bottom stroke inside a small rendered word can become its own component
    # after recursive fragmentation and otherwise satisfy the same aspect
    # ratio.  Crossed structural table rows are proved by the network paths
    # and do not depend on this fallback.
    if bbox.top > 0 and bool(
        non_rule_foreground[
            bbox.top - 1,
            bbox.left : bbox.right,
        ].any()
    ):
        return False
    lookaround = max(8, round(2.0 * body_height))
    if any(
        candidate.component_id != component.component_id
        and candidate.bbox.top < bbox.top - lookaround
        and candidate.bbox.bottom > bbox.bottom + lookaround
        and candidate.bbox.left < bbox.right
        and bbox.left < candidate.bbox.right
        for candidate in components
    ):
        return False
    above = non_rule_foreground[
        max(0, bbox.top - lookaround) : bbox.top,
        bbox.left : bbox.right,
    ]
    above_pixels = int(above.sum())
    upper_components = sum(
        candidate.component_id != component.component_id
        and bbox.top - lookaround <= candidate.bbox.bottom <= bbox.top
        and candidate.bbox.left < bbox.right
        and bbox.left < candidate.bbox.right
        for candidate in components
    )
    return (
        upper_components >= 2
        and above_pixels >= max(4, round(0.05 * bbox.width))
    )


def _component_has_rule_evidence(
    component: _Component,
    line_evidence: np.ndarray,
) -> bool:
    bbox = component.bbox
    pixels = int(
        line_evidence[
            bbox.top : bbox.bottom,
            bbox.left : bbox.right,
        ].sum()
    )
    return pixels / component.pixels >= 0.7


def _pure_vertical_line_component(
    component: _Component,
    config: GeometryConfig,
) -> bool:
    bbox = component.bbox
    return (
        bbox.width <= 2
        and bbox.height >= max(8, 3 * bbox.width)
        and bbox.height / bbox.width >= 4.0
        and component.pixels / bbox.area >= 0.7
    )


def _horizontal_network_supported(
    component: _Component,
    structural_bands: tuple[tuple[RuleAxis, Box], ...],
    config: GeometryConfig,
) -> bool:
    bbox = component.bbox
    if not (
        bbox.height <= config.max_rule_thickness
        and bbox.width >= max(8, 3 * bbox.height)
        and bbox.width / bbox.height >= 4.0
        and component.pixels / bbox.area >= 0.7
    ):
        return False
    tolerance = config.max_rule_thickness + 1
    crossings = sum(
        axis is RuleAxis.VERTICAL
        and candidate.top - tolerance <= bbox.top
        and bbox.bottom <= candidate.bottom + tolerance
        and bbox.left - tolerance < candidate.right
        and candidate.left < bbox.right + tolerance
        for axis, candidate in structural_bands
    )
    return crossings >= 2


def _vertical_network_supported(
    component: _Component,
    components: tuple[_Component, ...],
    structural_bands: tuple[tuple[RuleAxis, Box], ...],
    config: GeometryConfig,
) -> bool:
    """Require grid context before promoting a pure vertical raster stem."""

    bbox = component.bbox
    if not _pure_vertical_line_component(component, config):
        return False
    if _near_collinear_rule(
        RuleAxis.VERTICAL,
        bbox,
        structural_bands,
        config,
    ):
        return True

    tolerance = config.max_rule_thickness + 1
    horizontal = tuple(
        candidate
        for axis, candidate in structural_bands
        if axis is RuleAxis.HORIZONTAL
        and candidate.left < bbox.right
        and bbox.left < candidate.right
    )

    def touches(endpoint: int) -> bool:
        return any(
            max(
                candidate.top - endpoint,
                endpoint - candidate.bottom,
                0,
            )
            <= tolerance
            for candidate in horizontal
        )

    endpoint_crossings = int(touches(bbox.top)) + int(touches(bbox.bottom))
    if endpoint_crossings >= 2:
        return True
    if endpoint_crossings == 0:
        return False

    peers = tuple(
        candidate
        for candidate in components
        if candidate.component_id != component.component_id
        and _pure_vertical_line_component(candidate, config)
    )
    repeated_column = sum(
        max(
            candidate.bbox.left - bbox.right,
            bbox.left - candidate.bbox.right,
            0,
        )
        <= tolerance
        for candidate in peers
    ) >= 1
    row_peers = tuple(
        candidate
        for candidate in peers
        if abs(candidate.bbox.top - bbox.top) <= tolerance
        and abs(candidate.bbox.bottom - bbox.bottom) <= tolerance
    )
    row_boxes = (bbox, *(candidate.bbox for candidate in row_peers))
    row_span = max(box.right for box in row_boxes) - min(
        box.left for box in row_boxes
    )
    repeated_row = (
        len(row_boxes) >= 3
        and row_span >= max(2 * config.min_rule_length, 4 * bbox.height)
    )
    return repeated_column or repeated_row


def _recover_residual_line_drafts(
    non_rule_foreground: np.ndarray,
    line_evidence: np.ndarray,
    rules: tuple[Rule, ...],
    components: tuple[_Component, ...],
    config: GeometryConfig,
) -> tuple[_RuleDraft, ...]:
    height, width = non_rule_foreground.shape
    structural_bands = tuple(
        (rule.axis, rule.bbox)
        for rule in rules
        if not (
            rule.axis is RuleAxis.HORIZONTAL
            and (rule.bbox.top == 0 or rule.bbox.bottom == height)
        )
        and not (
            rule.axis is RuleAxis.VERTICAL
            and (rule.bbox.left == 0 or rule.bbox.right == width)
        )
    )
    body_height = _body_component_height(components, 50)
    values: list[_RuleDraft] = []
    seen: set[tuple[RuleAxis, Box]] = set()
    for component in components:
        axis = _thin_line_axis(
            component.bbox,
            component.pixels,
            structural_bands,
            config,
        )
        if axis is not None and not _component_has_rule_evidence(
            component,
            line_evidence,
        ):
            axis = None
        if (
            axis is None
            and _component_has_rule_evidence(component, line_evidence)
            and _horizontal_network_supported(
                component,
                structural_bands,
                config,
            )
        ):
            axis = RuleAxis.HORIZONTAL
        if axis is None and _unsupported_horizontal_line_component(
            component,
            non_rule_foreground,
            components,
            body_height,
            config,
        ):
            axis = RuleAxis.HORIZONTAL
        if axis is None and _vertical_network_supported(
            component,
            components,
            structural_bands,
            config,
        ):
            axis = RuleAxis.VERTICAL
        if axis is None:
            continue
        key = (axis, component.bbox)
        if key in seen:
            continue
        seen.add(key)
        values.append(
            _RuleDraft(
                axis,
                component.bbox,
                non_rule_foreground,
            )
        )
    return tuple(values)


def _recover_leaf_rule_networks(
    rule_evidence: np.ndarray,
    unclaimed_foreground: np.ndarray,
    claimed_rules: np.ndarray,
    rules: tuple[Rule, ...],
    drafts: tuple[_NodeDraft, ...],
    components: tuple[_Component, ...],
    config: GeometryConfig,
) -> tuple[_RuleDraft, ...]:
    """Recover broken grid bands after the first recursive row alignment.

    Rasterized table borders often form one connected comb: several short
    collinear horizontal pieces meet verticals with a one-pixel endpoint gap.
    Page-wide rule detection intentionally rejects each piece in isolation,
    leaving an entire header row as one OCR segment.  Inside an already
    isolated recursive leaf we can require a much stronger local proof: a
    near-full-width thin row band crossed by at least two thin, tall column
    bands.  Merged cells have no crossing band and therefore stay merged.
    """

    residual = np.logical_and(rule_evidence, np.logical_not(claimed_rules))
    long_horizontal_evidence = _orientation_candidates(
        residual,
        horizontal=True,
        minimum_length=config.min_rule_length,
    )
    leaves = tuple(node for node in drafts if not node.child_ids)
    drafts_by_id = {node.node_id: node for node in drafts}
    components_by_leaf: dict[str, list[_Component]] = {
        leaf.node_id: [] for leaf in leaves
    }
    for component in components:
        leaf = _leaf_for_run(component.runs[0], drafts_by_id)
        components_by_leaf[leaf.node_id].append(component)

    maximum_thickness = max(
        config.max_rule_thickness,
        round(min(residual.shape) * 0.003),
    )
    recovered: list[_RuleDraft] = []
    seen: set[tuple[RuleAxis, Box]] = set()
    leaf_contents: dict[str, Box] = {}
    networks: list[tuple[str, Box, tuple[tuple[int, int], ...]]] = []
    for leaf in leaves:
        local_components = components_by_leaf[leaf.node_id]
        if not local_components:
            continue
        content = Box.union(component.bbox for component in local_components)
        leaf_contents[leaf.node_id] = content
        if (
            content.width < 2 * config.min_rule_length
            or content.height < max(8, config.min_rule_length // 3)
        ):
            continue
        local = residual[
            content.top : content.bottom,
            content.left : content.right,
        ]
        long_horizontal = long_horizontal_evidence[
            content.top : content.bottom,
            content.left : content.right,
        ]
        dense_rows = long_horizontal.sum(axis=1, dtype=np.int64) >= max(
            config.min_rule_length,
            math.ceil(content.width * 0.75),
        )
        dense_columns = local.sum(axis=0, dtype=np.int64) >= max(
            4,
            math.ceil(content.height * 0.55),
        )
        row_bands = tuple(
            (start, stop)
            for start, stop in _true_runs(dense_rows)
            if stop - start <= maximum_thickness
        )
        column_bands = tuple(
            (start, stop)
            for start, stop in _true_runs(dense_columns)
            if stop - start <= maximum_thickness
            and start > 0
            and stop < content.width
        )
        if len(column_bands) < 3:
            continue
        crossed_rows = tuple(
            band
            for band in row_bands
            if sum(
                bool(local[band[0] : band[1], left:right].any())
                for left, right in column_bands
            )
            >= 3
        )
        if not crossed_rows:
            guided_bands = tuple(
                (start, stop)
                for start, stop in column_bands
                if int(local[:, start:stop].any(axis=1).sum())
                >= math.ceil(content.height * 0.8)
                if _supports_projected_column_gap(
                    rules,
                    content,
                    content.left + start,
                    content.left + stop,
                    float(content.height),
                )
            )
            if len(guided_bands) < 16:
                continue
            for start, stop in guided_bands:
                box = Box(
                    content.left + start,
                    content.top,
                    content.left + stop,
                    content.bottom,
                )
                key = (RuleAxis.VERTICAL, box)
                if key not in seen:
                    seen.add(key)
                    recovered.append(
                        _RuleDraft(RuleAxis.VERTICAL, box, residual)
                    )
            continue
        global_column_bands = tuple(
            (content.left + start, content.left + stop)
            for start, stop in column_bands
        )
        networks.append((leaf.node_id, content, global_column_bands))

        # A dense row crossed by several independent column traces is itself
        # structural evidence.  Materialize its collinear long pieces as one
        # horizontal rule so the lossless remainder cannot expose table
        # borders as thin high-aspect OCR segments.  Once three independent
        # crossings prove the row, claim its complete residual band: leaving
        # even one short edge piece would keep the row projection occupied
        # and prevent the second pass from using it as a safe row seam.
        for start, stop in crossed_rows:
            box = Box(
                content.left,
                content.top + start,
                content.right,
                content.top + stop,
            )
            key = (RuleAxis.HORIZONTAL, box)
            if key not in seen:
                seen.add(key)
                recovered.append(
                    _RuleDraft(
                        RuleAxis.HORIZONTAL,
                        box,
                        residual,
                        claim_full_bbox=True,
                    )
                )

        for start, stop in column_bands:
            if not any(
                local[row_start:row_stop, start:stop].any()
                for row_start, row_stop in crossed_rows
            ):
                continue
            box = Box(
                content.left + start,
                content.top,
                content.left + stop,
                content.bottom,
            )
            key = (RuleAxis.VERTICAL, box)
            if key not in seen:
                seen.add(key)
                recovered.append(_RuleDraft(RuleAxis.VERTICAL, box, residual))

    # A following header tier can carry the same vertical borders without a
    # second dense horizontal crossbar.  Project only into an immediately
    # adjacent leaf, and require at least three of the source bands to contain
    # a locally continuous vertical trace.  Text that spans a merged cell has
    # no such trace and remains one segment.
    for leaf in leaves:
        content = leaf_contents.get(leaf.node_id)
        if content is None or any(source_id == leaf.node_id for source_id, _, _ in networks):
            continue
        candidates: list[tuple[int, Box, tuple[tuple[int, int], ...]]] = []
        for source_id, source, bands in networks:
            if source_id == leaf.node_id:
                continue
            vertical_distance = max(
                source.top - content.bottom,
                content.top - source.bottom,
                0,
            )
            if vertical_distance > max(
                2 * config.min_rule_length,
                2 * content.height,
            ):
                continue
            overlapping_bands = tuple(
                (left, right)
                for left, right in bands
                if content.left < left and right < content.right
            )
            if len(overlapping_bands) >= 3:
                candidates.append((vertical_distance, source, overlapping_bands))
        if not candidates:
            continue
        _, _, projected_bands = min(
            candidates,
            key=lambda value: (value[0], -len(value[2]), value[1].top),
        )
        supported: list[tuple[int, int]] = []
        for left, right in projected_bands:
            trace = residual[content.top : content.bottom, left:right].any(axis=1)
            longest_run = max(
                (stop - start for start, stop in _true_runs(trace)),
                default=0,
            )
            if longest_run >= max(4, math.ceil(content.height * 0.7)):
                supported.append((left, right))
        if len(supported) < 8:
            continue
        for left, right in supported:
            box = Box(left, content.top, right, content.bottom)
            key = (RuleAxis.VERTICAL, box)
            if key not in seen:
                seen.add(key)
                recovered.append(_RuleDraft(RuleAxis.VERTICAL, box, residual))

    # Accepted table bands can retain a dense one-pixel raster halo or a
    # short continuation just outside the leaf that proved the network.  The
    # fragmented component list is the exact ownership unit used by the
    # second pass, so promote only line-shaped units supported by those bands.
    height, width = residual.shape
    structural_bands = tuple(
        (rule.axis, rule.bbox)
        for rule in rules
        if not (
            rule.axis is RuleAxis.HORIZONTAL
            and (rule.bbox.top == 0 or rule.bbox.bottom == height)
        )
        and not (
            rule.axis is RuleAxis.VERTICAL
            and (rule.bbox.left == 0 or rule.bbox.right == width)
        )
    ) + tuple((draft.axis, draft.bbox) for draft in recovered)
    body_height = _body_component_height(components, 50)
    for component in components:
        axis = _thin_line_axis(
            component.bbox,
            component.pixels,
            structural_bands,
            config,
        )
        if axis is not None and not _component_has_rule_evidence(
            component,
            residual,
        ):
            axis = None
        if (
            axis is None
            and _component_has_rule_evidence(component, residual)
            and _horizontal_network_supported(
                component,
                structural_bands,
                config,
            )
        ):
            axis = RuleAxis.HORIZONTAL
        if axis is None and _unsupported_horizontal_line_component(
            component,
            unclaimed_foreground,
            components,
            body_height,
            config,
        ):
            key = (RuleAxis.HORIZONTAL, component.bbox)
            if key not in seen:
                seen.add(key)
                recovered.append(
                    _RuleDraft(
                        RuleAxis.HORIZONTAL,
                        component.bbox,
                        unclaimed_foreground,
                        partition_evidence=False,
                    )
                )
            continue
        if axis is None and _vertical_network_supported(
            component,
            components,
            structural_bands,
            config,
        ):
            axis = RuleAxis.VERTICAL
        if axis is None:
            continue
        key = (axis, component.bbox)
        if key in seen:
            continue
        seen.add(key)
        recovered.append(_RuleDraft(axis, component.bbox, residual))
    return tuple(recovered)


def _zero_bands(projection: np.ndarray, offset: int) -> Iterable[tuple[int, int]]:
    for start, stop in _true_runs(projection == 0):
        yield offset + start, offset + stop


def _bridge_supported_upper_columns(
    upper_window: np.ndarray,
    bridge_columns: np.ndarray,
) -> np.ndarray:
    """Return nearby non-dust ink columns relevant to a guarded seam."""

    supported = np.zeros_like(upper_window, dtype=bool)
    if upper_window.shape[0] > 1:
        supported[1:, :] |= upper_window[:-1, :]
        supported[:-1, :] |= upper_window[1:, :]
    if upper_window.shape[1] > 1:
        supported[:, 1:] |= upper_window[:, :-1]
        supported[:, :-1] |= upper_window[:, 1:]
    if upper_window.shape[0] > 1 and upper_window.shape[1] > 1:
        supported[1:, 1:] |= upper_window[:-1, :-1]
        supported[1:, :-1] |= upper_window[:-1, 1:]
        supported[:-1, 1:] |= upper_window[1:, :-1]
        supported[:-1, :-1] |= upper_window[1:, 1:]
    supported &= upper_window
    upper_ink_columns = upper_window.any(axis=0)
    return np.logical_or(
        supported.any(axis=0),
        np.logical_and(upper_ink_columns, bridge_columns),
    )


def _protected_upper_boundary(
    mask: np.ndarray,
    bbox: Box,
    guard_start: int,
    guard_stop: int,
    diacritic_guard: np.ndarray | None,
) -> bool:
    if diacritic_guard is None:
        return False
    # Measure only evidence close enough to participate in this boundary.
    # Distant dust must not dilute a real accent bridge, while a full text row
    # immediately above a coincident lower glyph must still be splittable.
    bridge_columns = diacritic_guard[
        guard_start:guard_stop,
        bbox.left : bbox.right,
    ].any(axis=0)
    if not bridge_columns.any():
        return False

    # A page-wide zero band can expose only the final row of a longer guarded
    # accent-to-body bridge.  Measuring lookback from that one-row tail misses
    # the detached accent above it, especially when unrelated dust occupies
    # earlier rows.  Follow the already-proven bridge backwards in the same
    # columns and anchor the evidence window at its true upper edge.
    bridge_start = guard_start
    while bridge_start > bbox.top and np.logical_and(
        diacritic_guard[
            bridge_start - 1,
            bbox.left : bbox.right,
        ],
        bridge_columns,
    ).any():
        bridge_start -= 1
    lookback = max(3, 2 * (guard_stop - bridge_start))
    upper_window = mask[
        max(bbox.top, bridge_start - lookback) : bridge_start,
        bbox.left : bbox.right,
    ]
    # Nearby one-pixel dust must not dilute a genuine accent bridge.  Keep
    # columns supported by 8-connected ink; also retain a tiny mark that is
    # itself directly covered by the bridge so one-pixel diacritics survive.
    upper_columns = _bridge_supported_upper_columns(upper_window, bridge_columns)
    guarded_upper_columns = int(np.logical_and(bridge_columns, upper_columns).sum())
    return guarded_upper_columns / max(1, int(upper_columns.sum())) >= 0.6


def _has_body_evidence_on_both_sides(
    components: tuple[_Component, ...],
    bbox: Box,
    coordinate: int,
    body_height: float,
) -> bool:
    minimum_height = max(2, round(body_height * 0.5))
    above = False
    below = False
    for component in components:
        intersection = component.bbox.intersection(bbox)
        if intersection is None or intersection.height < minimum_height:
            continue
        if intersection.center[1] < coordinate:
            above = True
        else:
            below = True
        if above and below:
            return True
    return False


def _supports_imbalanced_row_gap(
    components: tuple[_Component, ...],
    bbox: Box,
    gap_start: int,
    gap_stop: int,
    before_pixels: int,
    after_pixels: int,
) -> bool:
    """Accept a large safe margin only when its small side is structured.

    A page logo can outweigh a short calendar-number row by several orders of
    magnitude.  Pixel balance alone then glues both across hundreds of blank
    rows.  Requiring multiple non-dust components on one aligned row keeps the
    override unavailable to isolated margin noise and detached diacritics.
    """

    if gap_stop <= gap_start:
        return False
    minority_before = before_pixels < after_pixels
    minority = tuple(
        component
        for component in components
        if component.bbox.intersection(bbox) is not None
        and (
            component.bbox.bottom <= gap_start
            if minority_before
            else component.bbox.top >= gap_stop
        )
        and component.pixels >= 4
        and component.bbox.height >= 2
    )
    if len(minority) < 2:
        return False
    heights = tuple(component.bbox.height for component in minority)
    local_body_height = float(np.percentile(heights, 50))
    minority_bbox = Box.union(component.bbox for component in minority)
    minority_pixels = sum(component.pixels for component in minority)
    return (
        gap_stop - gap_start >= max(4, round(1.5 * local_body_height))
        and minority_bbox.height <= max(4, round(2.5 * local_body_height))
        and minority_bbox.width >= max(8, round(1.5 * local_body_height))
        and minority_pixels >= max(24, round(4.0 * local_body_height))
    )


def _supports_projected_column_gap(
    rules: tuple[Rule, ...],
    bbox: Box,
    gap_start: int,
    gap_stop: int,
    body_height: float,
) -> bool:
    matching = tuple(
        rule
        for rule in rules
        if rule.axis is RuleAxis.VERTICAL
        and gap_start
        <= (rule.bbox.left + rule.bbox.right) / 2.0
        <= gap_stop
    )
    if not matching:
        return False
    maximum_local_distance = max(4, round(2.0 * body_height), bbox.height)
    if any(
        max(
            bbox.top - rule.bbox.bottom,
            rule.bbox.top - bbox.bottom,
            0,
        )
        <= maximum_local_distance
        for rule in matching
    ):
        return True
    # Repeated distant rules at the same page columns are document-level
    # sparse-matrix evidence (common in forms whose label/value row omits its
    # own border).  One unrelated stroke elsewhere on the page is not enough.
    return len(matching) >= 2


def _best_separator(
    mask: np.ndarray,
    bbox: Box,
    row_minimum_gap: int,
    column_minimum_gap: int,
    components: tuple[_Component, ...] = (),
    body_height: float = 0.0,
    rules: tuple[Rule, ...] = (),
    diacritic_guard: np.ndarray | None = None,
    allow_non_rule_columns: bool = True,
    projected_column_rules: tuple[Rule, ...] = (),
    prefer_rows: bool = False,
) -> tuple[SplitAxis, Box, bool] | None:
    local = mask[bbox.top : bbox.bottom, bbox.left : bbox.right]
    row_projection = local.sum(axis=1)
    column_projection = local.sum(axis=0)
    occupied_rows = np.flatnonzero(row_projection)
    occupied_columns = np.flatnonzero(column_projection)
    content_height = (
        bbox.height
        if occupied_rows.size == 0
        else int(occupied_rows[-1] - occupied_rows[0] + 1)
    )
    content_width = (
        bbox.width
        if occupied_columns.size == 0
        else int(occupied_columns[-1] - occupied_columns[0] + 1)
    )
    candidates: list[
        tuple[int, int, float, int, int, SplitAxis, Box, bool]
    ] = []
    total_ink = int(local.sum())
    if total_ink == 0:
        return None

    for axis, projection, offset in (
        (SplitAxis.ROWS, row_projection, bbox.top),
        (SplitAxis.COLUMNS, column_projection, bbox.left),
    ):
        minimum_gap = row_minimum_gap if axis is SplitAxis.ROWS else column_minimum_gap
        cumulative = np.cumsum(projection, dtype=np.int64)
        for start, stop in _zero_bands(projection, offset):
            is_rule_seam = any(
                (
                    axis is SplitAxis.ROWS
                    and rule.axis is RuleAxis.HORIZONTAL
                    and rule.bbox.top < stop
                    and start < rule.bbox.bottom
                    and rule.bbox.intersection(bbox) is not None
                    and rule.bbox.intersection(bbox).width
                    >= round(content_width * 0.8)
                )
                or (
                    axis is SplitAxis.COLUMNS
                    and rule.axis is RuleAxis.VERTICAL
                    and rule.bbox.left < stop
                    and start < rule.bbox.right
                    and rule.bbox.intersection(bbox) is not None
                    and rule.bbox.intersection(bbox).height
                    >= round(content_height * 0.8)
                )
                for rule in rules
            )
            if stop - start < minimum_gap and not is_rule_seam:
                continue
            if (
                axis is SplitAxis.COLUMNS
                and not is_rule_seam
                and not allow_non_rule_columns
                and not _supports_projected_column_gap(
                    projected_column_rules,
                    bbox,
                    start,
                    stop,
                    body_height,
                )
            ):
                continue
            local_start = start - offset
            local_stop = stop - offset
            before = int(cumulative[local_start - 1]) if local_start > 0 else 0
            after = total_ink - (int(cumulative[local_stop - 1]) if local_stop > 0 else 0)
            if before == 0 or after == 0:
                continue
            balance = min(before, after) / max(before, after)
            if (
                axis is SplitAxis.ROWS
                and not is_rule_seam
                and balance < 0.2
                and not _has_body_evidence_on_both_sides(
                    components,
                    bbox,
                    start,
                    body_height,
                )
                and not _supports_imbalanced_row_gap(
                    components,
                    bbox,
                    start,
                    stop,
                    before,
                    after,
                )
            ):
                continue
            if (
                axis is SplitAxis.ROWS
                and not is_rule_seam
                and _protected_upper_boundary(
                    mask,
                    bbox,
                    start,
                    stop,
                    diacritic_guard,
                )
            ):
                continue
            span = bbox.height if axis is SplitAxis.ROWS else bbox.width
            score = (stop - start) / max(1, span) + 0.25 * balance
            separator = (
                Box(bbox.left, start, bbox.right, stop)
                if axis is SplitAxis.ROWS
                else Box(start, bbox.top, stop, bbox.bottom)
            )
            axis_priority = 0 if axis is SplitAxis.ROWS else 1
            candidates.append(
                (
                    1 if is_rule_seam else 0,
                    1 if prefer_rows and axis is SplitAxis.ROWS else 0,
                    score,
                    -axis_priority,
                    -start,
                    axis,
                    separator,
                    is_rule_seam,
                )
            )
    if not candidates:
        return None
    if prefer_rows:
        row_candidates = tuple(
            candidate
            for candidate in candidates
            if candidate[5] is SplitAxis.ROWS
        )
        if row_candidates:
            # Restore the v16 guillotine order: finish the horizontal page
            # bands before looking for layout columns.  Balance is useful for
            # deciding whether a gap is safe (the checks above retain that
            # guard), but it must not make a narrow inter-line gap outrank a
            # wider object-scale separator.  A row-first crop also prevents a
            # page-spanning vertical whitespace track from slicing a header
            # before the header has been isolated from the columnar body.
            selected = max(
                row_candidates,
                key=lambda value: (
                    value[6].height,
                    -abs(
                        (value[6].top + value[6].bottom) / 2.0
                        - (bbox.top + bbox.bottom) / 2.0
                    ),
                    value[0],
                    value[2],
                    value[4],
                ),
            )
            return selected[5], selected[6], selected[7]
    selected = max(candidates, key=lambda value: value[:5])
    return selected[5], selected[6], selected[7]


def _estimate_regular_row_grid(
    mask: np.ndarray,
    components: tuple[_Component, ...],
) -> _RowGrid | None:
    """Find a page-wide row lattice only when raster geometry proves it.

    Touching rows have no blank separator, and a local projection minimum can
    sit inside either neighboring glyph row.  A regular page supplies stronger
    evidence: a repeated projection, a symmetric page model, and a pitch that
    is compatible with the observed component body height.  Requiring all
    three keeps the lattice out of ordinary asymmetric/single-line images.
    """

    projection = mask.sum(axis=1, dtype=np.int64)
    occupied = np.flatnonzero(projection)
    if occupied.size < 6:
        return None
    content_top = int(occupied[0])
    content_bottom = int(occupied[-1]) + 1
    content = projection[content_top:content_bottom].astype(np.float64)
    maximum_pitch = min(128, len(content) // 2)
    if maximum_pitch < 3:
        return None

    top_inset = content_top
    bottom_inset = mask.shape[0] - content_bottom
    symmetric_phases = range(
        max(0, top_inset - 2, bottom_inset - 2),
        min(top_inset, bottom_inset) + 1,
    )
    page_fits: dict[int, tuple[int, int]] = {}
    for pitch in range(3, maximum_pitch + 1):
        fits: list[tuple[int, int]] = []
        for phase in symmetric_phases:
            inner_height = mask.shape[0] - 2 * phase
            if inner_height <= 0 or inner_height % pitch:
                continue
            row_count = inner_height // pitch
            if row_count >= 3:
                fits.append((row_count, phase))
        if len(fits) == 1:
            page_fits[pitch] = fits[0]
    if not page_fits:
        return None

    internal_zero_stops = tuple(
        stop for start, stop in _true_runs(projection == 0) if start > 0 and stop < len(projection)
    )
    symmetric_span = mask.shape[0] - 2 * min(top_inset, bottom_inset)
    body_height = _body_component_height(components, 50)
    minimum_pitch = max(3, round(body_height * 0.8))
    candidates: list[tuple[float, int, int, int]] = []
    for pitch, (row_count, phase) in page_fits.items():
        first = content[:-pitch]
        second = content[pitch:]
        first_std = float(first.std())
        second_std = float(second.std())
        if first_std == 0.0 or second_std == 0.0:
            continue
        correlation = float(np.corrcoef(first, second)[0, 1])
        normalized_difference = float(np.mean(np.abs(first - second)) / max(1.0, float(content.mean())))
        repeat_fraction = abs(symmetric_span / pitch - round(symmetric_span / pitch))
        zero_periodicity = 0.0
        if len(internal_zero_stops) >= 2:
            angles = tuple(2.0 * math.pi * coordinate / pitch for coordinate in internal_zero_stops)
            zero_periodicity = math.hypot(
                sum(math.cos(angle) for angle in angles),
                sum(math.sin(angle) for angle in angles),
            ) / len(angles)
        score = correlation - normalized_difference - 3.0 * repeat_fraction + 0.1 * zero_periodicity
        candidates.append((score, pitch, row_count, phase))
    if not candidates:
        return None
    body_candidates = tuple(candidate for candidate in candidates if candidate[1] >= minimum_pitch)
    pool = body_candidates or tuple(candidates)
    score, pitch, row_count, phase = max(pool, key=lambda value: (value[0], value[1]))
    reset_ratios: list[float] = []
    for index in range(1, row_count):
        coordinate = phase + index * pitch
        if not 0 < coordinate < len(projection):
            continue
        left_peak = int(projection[max(0, coordinate - pitch) : coordinate].max(initial=0))
        right_peak = int(projection[coordinate : min(len(projection), coordinate + pitch)].max(initial=0))
        smaller_peak = min(left_peak, right_peak)
        if smaller_peak == 0:
            continue
        boundary_ink = min(int(projection[coordinate - 1]), int(projection[coordinate]))
        reset_ratios.append(boundary_ink / smaller_peak)
    good_resets = sum(ratio <= 0.55 for ratio in reset_ratios)
    reset_supported = (
        good_resets >= 2 and len(reset_ratios) == row_count - 1 and good_resets / len(reset_ratios) >= 0.75
    )
    sparse_three_row_supported = row_count == 3 and pitch >= minimum_pitch and len(components) / row_count <= 3.0
    if score <= -1.0 or not (reset_supported or sparse_three_row_supported):
        return None
    return _RowGrid(pitch=pitch, phase=phase, row_count=row_count, score=score)


def _best_row_grid_boundary(
    mask: np.ndarray,
    bbox: Box,
    row_grid: _RowGrid | None,
) -> int | None:
    if row_grid is None:
        return None
    local_projection = mask[
        bbox.top : bbox.bottom,
        bbox.left : bbox.right,
    ].any(axis=1)
    occupied_rows = np.flatnonzero(local_projection)
    if (
        occupied_rows.size
        and int(occupied_rows[-1]) - int(occupied_rows[0]) + 1
        <= row_grid.pitch
    ):
        # The estimated grid describes inter-line cadence.  Once a recursive
        # leaf contains no more than one cadence of actual ink, another grid
        # cut can only pass through a glyph or its underline.  This is the
        # scale-based recursion stop; it does not depend on fixture identity,
        # OCR text, or an expected object count.
        return None
    first_index = max(1, math.floor((bbox.top - row_grid.phase) / row_grid.pitch) + 1)
    last_index = min(
        row_grid.row_count - 1,
        math.ceil((bbox.bottom - row_grid.phase) / row_grid.pitch) - 1,
    )
    candidates: list[tuple[float, float, int]] = []
    for index in range(first_index, last_index + 1):
        coordinate = row_grid.phase + index * row_grid.pitch
        if not bbox.top < coordinate < bbox.bottom:
            continue
        before = int(mask[bbox.top : coordinate, bbox.left : bbox.right].sum())
        after = int(mask[coordinate : bbox.bottom, bbox.left : bbox.right].sum())
        if before == 0 or after == 0:
            continue
        balance = min(before, after) / max(before, after)
        center_distance = abs((bbox.top + bbox.bottom) / 2.0 - coordinate)
        candidates.append((balance, -center_distance, coordinate))
    return None if not candidates else max(candidates)[2]


def _best_row_valley(
    mask: np.ndarray,
    bbox: Box,
    body_height: float,
    body_evidence_height: float,
    components: tuple[_Component, ...],
    diacritic_guard: np.ndarray | None,
) -> int | None:
    projection = mask[bbox.top : bbox.bottom, bbox.left : bbox.right].sum(axis=1)
    occupied_rows = np.flatnonzero(projection)
    if occupied_rows.size == 0:
        return None
    content_top = bbox.top + int(occupied_rows[0])
    content_bottom = bbox.top + int(occupied_rows[-1]) + 1
    minimum_span = max(3, round(body_height * 0.55))
    radius = max(3, round(body_height))
    local_components = tuple(component for component in components if component.bbox.intersection(bbox) is not None)
    candidates: list[tuple[float, float, int]] = []
    for coordinate in range(content_top + minimum_span, content_bottom - minimum_span + 1):
        if not any(
            (component.bbox.top + component.bbox.bottom) / 2.0 < coordinate for component in local_components
        ) or not any(
            (component.bbox.top + component.bbox.bottom) / 2.0 >= coordinate for component in local_components
        ):
            continue
        local_coordinate = coordinate - bbox.top
        left_start = max(0, local_coordinate - radius)
        right_stop = min(len(projection), local_coordinate + radius)
        left_peak = int(projection[left_start:local_coordinate].max(initial=0))
        right_peak = int(projection[local_coordinate:right_stop].max(initial=0))
        smaller_peak = min(left_peak, right_peak)
        if smaller_peak == 0:
            continue
        valley = min(int(projection[local_coordinate - 1]), int(projection[local_coordinate]))
        valley_ratio = valley / smaller_peak
        ending_components = tuple(component for component in local_components if component.bbox.bottom == coordinate)
        starting_components = tuple(component for component in local_components if component.bbox.top == coordinate)
        endpoint_width_ratio = min(
            sum(component.bbox.width for component in ending_components),
            sum(component.bbox.width for component in starting_components),
        ) / max(1, bbox.width)
        endpoint_supported = bool(ending_components) and bool(starting_components) and endpoint_width_ratio >= 0.03
        before = int(projection[:local_coordinate].sum())
        after = int(projection[local_coordinate:].sum())
        if before == 0 or after == 0:
            continue
        balance = min(before, after) / max(before, after)
        if balance < 0.08:
            continue
        if endpoint_supported:
            if valley_ratio > 0.6:
                continue
        elif valley_ratio > 0.3 and (
            valley_ratio > 0.4
            or balance < 0.2
            or not _has_body_evidence_on_both_sides(
                components,
                bbox,
                coordinate,
                body_evidence_height,
            )
        ):
            continue
        if balance < 0.2 and not _has_body_evidence_on_both_sides(
            components,
            bbox,
            coordinate,
            body_evidence_height,
        ):
            continue
        upper_columns = np.zeros(bbox.width, dtype=bool)
        lower_columns = np.zeros(bbox.width, dtype=bool)
        for component in local_components:
            if component.pixels < 4 or component.bbox.height < 3:
                continue
            left = max(bbox.left, component.bbox.left) - bbox.left
            right = min(bbox.right, component.bbox.right) - bbox.left
            if right <= left:
                continue
            component_center = (component.bbox.top + component.bbox.bottom) / 2.0
            # A component crossing the candidate is one piece of evidence,
            # not an entire row on both sides.  Counting it twice lets an
            # internal glyph valley masquerade as a line boundary.
            if component_center < coordinate:
                upper_columns[left:right] = True
            else:
                lower_columns[left:right] = True
        upper_width = int(upper_columns.sum())
        lower_width = int(lower_columns.sum())
        minimum_row_width = max(
            8,
            round(body_evidence_height * 1.5),
            round(bbox.width * 0.08),
        )
        if min(upper_width, lower_width) < minimum_row_width:
            continue
        if _protected_upper_boundary(
            mask,
            bbox,
            max(bbox.top, coordinate - 1),
            min(bbox.bottom, coordinate + 1),
            diacritic_guard,
        ):
            continue
        center_distance = abs((content_top + content_bottom) / 2.0 - coordinate)
        score = 1.0 - valley_ratio + 0.2 * balance
        if endpoint_supported:
            score += 1.0 + endpoint_width_ratio
        candidates.append((score, -center_distance, coordinate))
    if not candidates:
        return None
    return max(candidates)[2]


def _offset_components(
    components: tuple[_Component, ...],
    *,
    left: int,
    top: int,
) -> tuple[_Component, ...]:
    return tuple(
        _Component(
            component_id=index,
            bbox=Box(
                component.bbox.left + left,
                component.bbox.top + top,
                component.bbox.right + left,
                component.bbox.bottom + top,
            ),
            pixels=component.pixels,
            runs=tuple(
                _Run(
                    row=run.row + top,
                    start=run.start + left,
                    stop=run.stop + left,
                    label=run.label,
                )
                for run in component.runs
            ),
        )
        for index, component in enumerate(components)
    )


def _layout_partition_evidence(
    mask: np.ndarray,
    components: tuple[_Component, ...],
    *,
    maximum_evidence_components: int,
) -> tuple[np.ndarray, tuple[_Component, ...]]:
    """Keep dust lossless without allowing it to create recursive leaves."""

    if len(components) <= maximum_evidence_components:
        return np.array(mask, copy=True), components
    meaningful = tuple(
        component
        for component in components
        if component.pixels >= 4 and component.bbox.height >= 3
    )
    if not meaningful:
        return np.array(mask, copy=True), components
    evidence = np.zeros_like(mask, dtype=bool)
    for component in meaningful:
        for run in component.runs:
            evidence[run.row, run.start : run.stop] = True
    return evidence, meaningful


def _recursive_partition(
    mask: np.ndarray,
    components: tuple[_Component, ...],
    config: GeometryConfig,
    *,
    rules: tuple[Rule, ...] = (),
    diacritic_guard: np.ndarray | None = None,
    row_grid: _RowGrid | None = None,
    nonstructural_rule_boxes: frozenset[Box] = frozenset(),
    cover_separators: bool = False,
    partition_rgb: np.ndarray | None = None,
    physical_fallback: np.ndarray | None = None,
) -> tuple[tuple[_NodeDraft, ...], np.ndarray]:
    height, width = mask.shape
    if partition_rgb is not None and partition_rgb.shape[:2] != mask.shape:
        raise ValueError("partition RGB and mask shapes disagree")
    if physical_fallback is not None and physical_fallback.shape != mask.shape:
        raise ValueError("partition fallback and mask shapes disagree")
    original_component_count = len(components)
    working_mask, components = _layout_partition_evidence(
        mask,
        components,
        maximum_evidence_components=config.max_nodes,
    )
    cover_separators = (
        cover_separators or len(components) != original_component_count
    )
    working_guard = (
        np.array(diacritic_guard, copy=True)
        if diacritic_guard is not None
        else np.zeros_like(mask, dtype=bool)
    )
    root_body_component_height = _body_component_height(components, 75)
    root_valley_body_height = _body_component_height(components, 50)
    root = _NodeDraft("geo-root", Box(0, 0, width, height), 0, None, ("geo-root",))
    nodes: list[_NodeDraft] = [root]
    stack: list[tuple[_NodeDraft, tuple[_Component, ...], tuple[Rule, ...]]] = [(root, components, rules)]
    while stack:
        node, local_components, local_rules = stack.pop()
        if partition_rgb is not None:
            crop = partition_rgb[
                node.bbox.top : node.bbox.bottom,
                node.bbox.left : node.bbox.right,
            ]
            # Adaptive polarity is local: the full page can prove light ink on
            # a coloured panel while the panel crop proves dark ink on that
            # same fill.  Recomputing the crop and replacing its parent mask
            # discarded the first polarity (the yellow-cell regression).
            # Carry every already-proven parent pixel into the child, then add
            # the crop-local evidence.  Flat fill is still absent because only
            # adaptive evidence is inherited; the physical fallback is never
            # unioned here.
            inherited_mask = np.array(
                working_mask[
                    node.bbox.top : node.bbox.bottom,
                    node.bbox.left : node.bbox.right,
                ],
                copy=True,
            )
            fallback = (
                inherited_mask
                if physical_fallback is None
                else physical_fallback[
                    node.bbox.top : node.bbox.bottom,
                    node.bbox.left : node.bbox.right,
                ]
            )
            local_mask, _ = _select_layout_foreground(
                crop,
                fallback,
                force_adaptive=True,
            )
            local_mask = np.logical_or(local_mask, inherited_mask)
            for rule in local_rules:
                intersection = rule.bbox.intersection(node.bbox)
                if intersection is None:
                    continue
                local_mask[
                    intersection.top - node.bbox.top : intersection.bottom - node.bbox.top,
                    intersection.left - node.bbox.left : intersection.right - node.bbox.left,
                ] = False
            crop_components = _connected_components(
                local_mask,
                max_runs=config.max_runs,
                max_components=config.max_components,
            )
            local_mask, crop_components = _layout_partition_evidence(
                local_mask,
                crop_components,
                maximum_evidence_components=config.max_nodes,
            )
            working_mask[
                node.bbox.top : node.bbox.bottom,
                node.bbox.left : node.bbox.right,
            ] = local_mask
            local_components = _offset_components(
                crop_components,
                left=node.bbox.left,
                top=node.bbox.top,
            )
            local_guard_mask = _guard_diacritic_gaps(
                local_mask,
                crop_components,
            )
            working_guard[
                node.bbox.top : node.bbox.bottom,
                node.bbox.left : node.bbox.right,
            ] = np.logical_and(local_guard_mask, np.logical_not(local_mask))
        body_component_height = (
            _body_component_height(local_components, 75)
            if partition_rgb is not None
            else root_body_component_height
        )
        valley_body_height = (
            _body_component_height(local_components, 50)
            if partition_rgb is not None
            else root_valley_body_height
        )
        effective_column_gap = max(
            config.min_safe_gap,
            round(body_component_height * 0.8),
        )
        if node.bbox.left != 0 or node.bbox.right != width:
            # A first column crop isolates a layout lane, not necessarily an
            # atomic text object.  Continue the v16-style recursive search
            # inside it, but require a real lane-sized gutter before making
            # another column cut.  This keeps ordinary word spacing out of
            # the sparse matrix while still allowing three-or-more columns.
            effective_column_gap = max(
                effective_column_gap,
                round(node.bbox.width * 0.12),
            )
        local_pixels = int(
            working_mask[
                node.bbox.top : node.bbox.bottom,
                node.bbox.left : node.bbox.right,
            ].sum()
        )
        if local_pixels == 0:
            node.stop_reason = StopReason.EMPTY if node.parent_id is None else StopReason.ATOMIC
            continue
        decision = _best_separator(
            working_mask,
            node.bbox,
            row_minimum_gap=1,
            column_minimum_gap=effective_column_gap,
            components=local_components,
            body_height=body_component_height,
            rules=local_rules,
            diacritic_guard=working_guard,
            allow_non_rule_columns=not node.rule_partition,
            projected_column_rules=rules if node.rule_partition else (),
            # V16 first exhausted safe horizontal separators and only then
            # searched for a vertical lane.  Keeping that order at every
            # locality preserves full-width headers above columnar bodies.
            prefer_rows=True,
        )
        split_coordinate = _best_row_grid_boundary(
            working_mask,
            node.bbox,
            None if partition_rgb is not None else row_grid,
        )
        if decision is not None and decision[2]:
            split_coordinate = None
            axis, separator, _ = decision
        elif decision is not None and decision[0] is SplitAxis.ROWS:
            split_coordinate = None
            axis, separator, _ = decision
        elif split_coordinate is not None:
            axis = SplitAxis.ROWS
            separator = None
        elif node.rule_partition:
            # A structural rule proves the boundary that introduced this
            # region, but it does not prove that every row on either side is
            # one atomic OCR segment.  Keep column gaps inside a rule-bounded
            # row merged (important for spanning table cells), while still
            # accepting recursively proven row seams and guarded projection
            # valleys above and below it.
            split_coordinate = _best_row_valley(
                working_mask,
                node.bbox,
                valley_body_height,
                body_component_height,
                local_components,
                working_guard,
            )
            if split_coordinate is None:
                if decision is None:
                    node.stop_reason = StopReason.ATOMIC
                    continue
                axis, separator, _ = decision
            else:
                axis = SplitAxis.ROWS
                separator = None
        elif decision is None:
            # A vertical crop can still contain several touching text rows.
            # Keep the recursive row-valley search active at every locality;
            # the lane-sized column threshold above prevents word gaps from
            # being mistaken for further layout columns.
            split_coordinate = _best_row_valley(
                working_mask,
                node.bbox,
                valley_body_height,
                body_component_height,
                local_components,
                working_guard,
            )
            if split_coordinate is None:
                node.stop_reason = StopReason.ATOMIC
                continue
            axis = SplitAxis.ROWS
            separator = None
        else:
            axis, separator, _ = decision
        if node.depth >= config.max_depth or len(nodes) + 2 > config.max_nodes:
            # Reaching a safety bound is only degradation when another split
            # is actually required.  Tiny terminal regions at the bound are
            # valid atomic leaves, not unfinished recursion.
            node.stop_reason = StopReason.LIMIT
            continue
        if split_coordinate is not None:
            child_boxes = (
                Box(node.bbox.left, node.bbox.top, node.bbox.right, split_coordinate),
                Box(node.bbox.left, split_coordinate, node.bbox.right, node.bbox.bottom),
            )
        elif axis is SplitAxis.ROWS:
            assert separator is not None
            if not cover_separators:
                child_boxes = (
                    Box(node.bbox.left, node.bbox.top, node.bbox.right, separator.top),
                    Box(node.bbox.left, separator.bottom, node.bbox.right, node.bbox.bottom),
                )
            else:
                midpoint = (separator.top + separator.bottom) // 2
                child_boxes = (
                    Box(node.bbox.left, node.bbox.top, node.bbox.right, midpoint),
                    Box(node.bbox.left, midpoint, node.bbox.right, node.bbox.bottom),
                )
        else:
            assert separator is not None
            if not cover_separators:
                child_boxes = (
                    Box(node.bbox.left, node.bbox.top, separator.left, node.bbox.bottom),
                    Box(separator.right, node.bbox.top, node.bbox.right, node.bbox.bottom),
                )
            else:
                midpoint = (separator.left + separator.right) // 2
                child_boxes = (
                    Box(node.bbox.left, node.bbox.top, midpoint, node.bbox.bottom),
                    Box(midpoint, node.bbox.top, node.bbox.right, node.bbox.bottom),
                )
        horizontal_rule_split = (
            decision is not None
            and decision[2]
            and axis is SplitAxis.ROWS
            and separator is not None
            and not any(
                box.top < separator.bottom
                and separator.top < box.bottom
                for box in nonstructural_rule_boxes
            )
        )
        if horizontal_rule_split:
            child_row_boundaries = (
                (node.row_rule_top, True),
                (True, node.row_rule_bottom),
            )
        elif axis is SplitAxis.ROWS:
            # A whitespace/valley split is not a structural rule.  Only the
            # outer child retains an already-proven structural boundary.
            child_row_boundaries = (
                (node.row_rule_top, False),
                (False, node.row_rule_bottom),
            )
        else:
            child_row_boundaries = (
                (node.row_rule_top, node.row_rule_bottom),
                (node.row_rule_top, node.row_rule_bottom),
            )
        children = tuple(
            _NodeDraft(
                node_id=f"{node.node_id}.{index}",
                bbox=child_box,
                depth=node.depth + 1,
                parent_id=node.node_id,
                path=node.path + (f"{node.node_id}.{index}",),
                rule_partition=node.rule_partition
                or (row_rule_top and row_rule_bottom),
                row_rule_top=row_rule_top,
                row_rule_bottom=row_rule_bottom,
            )
            for index, (child_box, (row_rule_top, row_rule_bottom)) in enumerate(
                zip(child_boxes, child_row_boundaries, strict=True)
            )
        )
        node.axis = axis
        node.child_ids = tuple(child.node_id for child in children)
        node.separator_boxes = () if separator is None else (separator,)
        node.split_coordinate = split_coordinate
        nodes.extend(children)
        child_work = tuple(
            (
                child,
                tuple(
                    component for component in local_components if component.bbox.intersection(child.bbox) is not None
                ),
                tuple(rule for rule in local_rules if rule.bbox.intersection(child.bbox) is not None),
            )
            for child in children
        )
        stack.extend(reversed(child_work))
    return tuple(nodes), working_mask


def _leaf_for_run(run: _Run, drafts_by_id: dict[str, _NodeDraft]) -> _NodeDraft:
    node = drafts_by_id["geo-root"]
    while node.child_ids:
        matching = tuple(
            drafts_by_id[child_id]
            for child_id in node.child_ids
            if drafts_by_id[child_id].bbox.contains_point(run.start, run.row)
            and drafts_by_id[child_id].bbox.contains_point(run.stop - 1, run.row)
        )
        if len(matching) != 1:
            raise RuntimeError(
                f"foreground run at row {run.row}, x={run.start}:{run.stop} " f"has {len(matching)} recursive owners"
            )
        node = matching[0]
    return node


def _fragment_components_for_leaves(
    components: tuple[_Component, ...],
    drafts: tuple[_NodeDraft, ...],
) -> tuple[_Component, ...]:
    drafts_by_id = {draft.node_id: draft for draft in drafts}
    root = drafts_by_id["geo-root"]
    grouped: dict[tuple[int, str], list[_Run]] = {}
    for component in components:
        for run in component.runs:
            # Follow only branches that intersect this run.  The former scan
            # compared every run with every leaf, turning long multi-page
            # documents into O(runs * leaves) work.  The recursive tree is
            # already the spatial index we need, including the rare case in
            # which one physical run is split across two child boxes.
            row_leaves: list[_NodeDraft] = []
            stack = [root]
            while stack:
                node = stack.pop()
                if (
                    not node.bbox.top <= run.row < node.bbox.bottom
                    or node.bbox.left >= run.stop
                    or run.start >= node.bbox.right
                ):
                    continue
                if node.child_ids:
                    stack.extend(
                        reversed(
                            tuple(
                                drafts_by_id[child_id]
                                for child_id in node.child_ids
                            )
                        )
                    )
                else:
                    row_leaves.append(node)
            owned = 0
            for leaf in row_leaves:
                start = max(run.start, leaf.bbox.left)
                stop = min(run.stop, leaf.bbox.right)
                if start >= stop:
                    continue
                grouped.setdefault(
                    (component.component_id, leaf.node_id),
                    [],
                ).append(_Run(run.row, start, stop, run.label))
                owned += stop - start
            if owned != run.stop - run.start:
                raise RuntimeError(
                    f"foreground run at row {run.row}, x={run.start}:{run.stop} "
                    f"has {owned} recursively owned pixels"
                )
    unordered: list[tuple[Box, int, tuple[_Run, ...]]] = []
    for runs in grouped.values():
        bbox = Box(
            min(run.start for run in runs),
            min(run.row for run in runs),
            max(run.stop for run in runs),
            max(run.row for run in runs) + 1,
        )
        pixels = sum(run.stop - run.start for run in runs)
        unordered.append((bbox, pixels, tuple(runs)))
    unordered.sort(key=lambda value: (value[0].top, value[0].left, value[0].bottom, value[0].right))
    return tuple(
        _Component(component_id=index, bbox=bbox, pixels=pixels, runs=runs)
        for index, (bbox, pixels, runs) in enumerate(unordered)
    )


def _best_component_boundary(
    components: tuple[_Component, ...],
    bbox: Box,
    diacritic_guard: np.ndarray,
) -> tuple[int, tuple[_Component, ...], tuple[_Component, ...]] | None:
    if len(components) < 2:
        return None
    # Single-pixel scan dust must retain an owner, but it is not evidence for
    # creating another OCR segment.  Let meaningful glyph bodies choose the
    # seam; all components are assigned to the resulting children below.
    decision_components = tuple(
        component
        for component in components
        if component.pixels >= 4 and component.bbox.height >= 3
    )
    if len(decision_components) < 2:
        return None
    ordered = tuple(
        sorted(
            decision_components,
            key=lambda component: (
                component.bbox.top,
                component.bbox.bottom,
                component.bbox.left,
                component.bbox.right,
                component.component_id,
            ),
        )
    )
    count = len(ordered)
    total_pixels = sum(component.pixels for component in ordered)
    content_box = Box.union(component.bbox for component in ordered)

    prefix_pixels: list[int] = []
    prefix_bottoms: list[int] = []
    prefix_lefts: list[int] = []
    prefix_rights: list[int] = []
    pixels = 0
    bottom = bbox.top
    left = bbox.right
    right = bbox.left
    for component in ordered:
        pixels += component.pixels
        bottom = max(bottom, component.bbox.bottom)
        left = min(left, component.bbox.left)
        right = max(right, component.bbox.right)
        prefix_pixels.append(pixels)
        prefix_bottoms.append(bottom)
        prefix_lefts.append(left)
        prefix_rights.append(right)

    suffix_lefts = [bbox.right] * count
    suffix_rights = [bbox.left] * count
    left = bbox.right
    right = bbox.left
    for index in range(count - 1, -1, -1):
        component = ordered[index]
        left = min(left, component.bbox.left)
        right = max(right, component.bbox.right)
        suffix_lefts[index] = left
        suffix_rights[index] = right

    candidates: list[tuple[float, int, int, int, int]] = []
    required_width = max(1, round(content_box.width * 0.15))
    for index in range(1, count):
        coordinate = prefix_bottoms[index - 1]
        if (
            not bbox.top < coordinate < bbox.bottom
            or coordinate > ordered[index].bbox.top
        ):
            continue
        upper_pixels = prefix_pixels[index - 1]
        lower_pixels = total_pixels - upper_pixels
        pixel_balance = min(upper_pixels, lower_pixels) / max(upper_pixels, lower_pixels)
        if pixel_balance < 0.12:
            continue
        upper_width = prefix_rights[index - 1] - prefix_lefts[index - 1]
        lower_width = suffix_rights[index] - suffix_lefts[index]
        if upper_width < required_width or lower_width < required_width:
            continue
        component_balance = min(index, count - index) / max(index, count - index)
        score = pixel_balance + 0.25 * component_balance
        candidates.append(
            (score, -coordinate, index, upper_pixels, lower_pixels)
        )
    if not candidates:
        return None
    for _, negative_coordinate, index, upper_pixels, lower_pixels in sorted(
        candidates,
        key=lambda value: value[:2],
        reverse=True,
    ):
        coordinate = -negative_coordinate
        upper = tuple(
            component
            for component in components
            if component.bbox.bottom <= coordinate
        )
        lower = tuple(
            component
            for component in components
            if component.bbox.top >= coordinate
        )
        if (
            not upper
            or not lower
            or len(upper) + len(lower) != len(components)
        ):
            continue
        if upper_pixels < lower_pixels:
            guard_top = max(bbox.top, coordinate - 1)
            guard_bottom = min(bbox.bottom, coordinate + 1)
            bridge_columns = diacritic_guard[
                guard_top:guard_bottom,
                bbox.left : bbox.right,
            ].any(axis=0)
            lookback = max(3, 2 * (guard_bottom - guard_top))
            nearest_upper_bottom = prefix_bottoms[index - 1]
            window_top = max(bbox.top, nearest_upper_bottom - lookback)
            upper_window = np.zeros(
                (coordinate - window_top, bbox.width),
                dtype=bool,
            )
            for component in upper:
                for run in component.runs:
                    if run.row < window_top or run.row >= coordinate:
                        continue
                    local_left = max(bbox.left, run.start) - bbox.left
                    local_right = min(bbox.right, run.stop) - bbox.left
                    if local_right > local_left:
                        upper_window[
                            run.row - window_top,
                            local_left:local_right,
                        ] = True
            upper_columns = _bridge_supported_upper_columns(
                upper_window,
                bridge_columns,
            )
            guard_coverage = int(
                np.logical_and(bridge_columns, upper_columns).sum()
            ) / max(1, int(upper_columns.sum()))
            if guard_coverage >= 0.6:
                continue
        return coordinate, upper, lower
    return None


def _best_component_rule_boundary(
    components: tuple[_Component, ...],
    bbox: Box,
    rules: tuple[Rule, ...],
) -> tuple[Box, tuple[_Component, ...], tuple[_Component, ...]] | None:
    """Find a vertical rule that separates every local component exactly."""

    if len(components) < 2:
        return None
    content = Box.union(component.bbox for component in components)
    candidates: list[
        tuple[float, float, int, Box, tuple[_Component, ...], tuple[_Component, ...]]
    ] = []
    for rule in rules:
        if rule.axis is not RuleAxis.VERTICAL:
            continue
        intersection = rule.bbox.intersection(content)
        if (
            intersection is None
            or intersection.height < math.ceil(0.8 * content.height)
            or rule.bbox.left <= bbox.left
            or rule.bbox.right >= bbox.right
        ):
            continue
        left = tuple(
            component
            for component in components
            if component.bbox.right <= rule.bbox.left
        )
        right = tuple(
            component
            for component in components
            if component.bbox.left >= rule.bbox.right
        )
        if not left or not right or len(left) + len(right) != len(components):
            continue
        left_pixels = sum(component.pixels for component in left)
        right_pixels = sum(component.pixels for component in right)
        pixel_balance = min(left_pixels, right_pixels) / max(
            left_pixels,
            right_pixels,
        )
        component_balance = min(len(left), len(right)) / max(
            len(left),
            len(right),
        )
        coverage = intersection.height / content.height
        separator = Box(
            rule.bbox.left,
            bbox.top,
            rule.bbox.right,
            bbox.bottom,
        )
        candidates.append(
            (
                coverage,
                pixel_balance + 0.25 * component_balance,
                -rule.bbox.left,
                separator,
                left,
                right,
            )
        )
    if not candidates:
        return None
    _, _, _, separator, left, right = max(
        candidates,
        key=lambda value: value[:3],
    )
    return separator, left, right


def _refine_component_boundaries(
    drafts: tuple[_NodeDraft, ...],
    components: tuple[_Component, ...],
    diacritic_guard: np.ndarray,
    config: GeometryConfig,
    rules: tuple[Rule, ...] = (),
) -> tuple[_NodeDraft, ...]:
    nodes = list(drafts)
    leaves = tuple(node for node in nodes if not node.child_ids)
    drafts_by_id = {node.node_id: node for node in nodes}
    components_by_leaf: dict[str, list[_Component]] = {
        leaf.node_id: [] for leaf in leaves
    }
    for component in components:
        # ``components`` were already fragmented by the same recursive tree.
        # Re-scanning every leaf for every component is O(C * leaves); descend
        # the tree once from a component's first physical run instead.
        leaf = _leaf_for_run(component.runs[0], drafts_by_id)
        components_by_leaf[leaf.node_id].append(component)
    stack = [
        (leaf, tuple(components_by_leaf[leaf.node_id]))
        for leaf in reversed(leaves)
    ]
    while stack:
        node, local_components = stack.pop()
        row_decision = _best_component_boundary(
            local_components,
            node.bbox,
            diacritic_guard,
        )
        column_decision = (
            None
            if row_decision is not None
            else _best_component_rule_boundary(
                local_components,
                node.bbox,
                rules,
            )
        )
        if row_decision is None and column_decision is None:
            continue
        if node.depth >= config.max_depth or len(nodes) + 2 > config.max_nodes:
            node.stop_reason = StopReason.LIMIT
            continue
        if row_decision is not None:
            coordinate, first_components, second_components = row_decision
            axis = SplitAxis.ROWS
            separator = None
            child_boxes = (
                Box(node.bbox.left, node.bbox.top, node.bbox.right, coordinate),
                Box(node.bbox.left, coordinate, node.bbox.right, node.bbox.bottom),
            )
        else:
            assert column_decision is not None
            separator, first_components, second_components = column_decision
            coordinate = None
            axis = SplitAxis.COLUMNS
            child_boxes = (
                Box(node.bbox.left, node.bbox.top, separator.left, node.bbox.bottom),
                Box(separator.right, node.bbox.top, node.bbox.right, node.bbox.bottom),
            )
        children = tuple(
            _NodeDraft(
                node_id=f"{node.node_id}.c{index}",
                bbox=child_box,
                depth=node.depth + 1,
                parent_id=node.node_id,
                path=node.path + (f"{node.node_id}.c{index}",),
                stop_reason=StopReason.ATOMIC,
            )
            for index, child_box in enumerate(child_boxes)
        )
        node.axis = axis
        node.child_ids = tuple(child.node_id for child in children)
        node.separator_boxes = () if separator is None else (separator,)
        node.split_coordinate = coordinate
        node.stop_reason = None
        nodes.extend(children)
        stack.append((children[1], second_components))
        stack.append((children[0], first_components))
    return tuple(nodes)


def _leaf_for_component(component: _Component, leaves: tuple[_NodeDraft, ...]) -> _NodeDraft:
    first_run = component.runs[0]
    point = (first_run.start, first_run.row)
    matching = tuple(leaf for leaf in leaves if leaf.bbox.contains_point(*point))
    if len(matching) != 1:
        raise RuntimeError(f"component {component.component_id} has {len(matching)} recursive owners")
    leaf = matching[0]
    for run in component.runs:
        if not leaf.bbox.contains_point(run.start, run.row) or not leaf.bbox.contains_point(run.stop - 1, run.row):
            raise RuntimeError(f"recursive separator cuts component {component.component_id}")
    return leaf


def _materialize_segments(
    components: tuple[_Component, ...],
    drafts: tuple[_NodeDraft, ...],
    transform: AffineTransform,
    shape: tuple[int, int],
) -> tuple[tuple[Segment, ...], np.ndarray, dict[str, tuple[str, ...]]]:
    leaves = tuple(node for node in drafts if not node.child_ids)
    drafts_by_id = {node.node_id: node for node in drafts}
    grouped: dict[str, list[_Component]] = {leaf.node_id: [] for leaf in leaves}
    leaf_by_id = {leaf.node_id: leaf for leaf in leaves}
    for component in components:
        # Components are already fragmented against this exact tree.  Use its
        # spatial index instead of another O(C * leaves) ownership scan.
        leaf = _leaf_for_run(component.runs[0], drafts_by_id)
        grouped[leaf.node_id].append(component)

    segment_drafts: list[tuple[Box, _NodeDraft, tuple[_Component, ...]]] = []
    for leaf_id, values in grouped.items():
        if values:
            segment_drafts.append(
                (
                    Box.union(component.bbox for component in values),
                    leaf_by_id[leaf_id],
                    tuple(values),
                )
            )
    segment_drafts.sort(key=lambda value: (value[0].top, value[0].left, value[0].bottom, value[0].right))

    row_tops = {top: index for index, top in enumerate(sorted({box.top for box, _, _ in segment_drafts}))}
    ownership = np.full(shape, -1, dtype=np.int32)
    segments: list[Segment] = []
    leaf_segment_ids: dict[str, tuple[str, ...]] = {leaf.node_id: () for leaf in leaves}
    for index, (bbox, leaf, values) in enumerate(segment_drafts):
        segment_id = f"segment-{index:06d}"
        component_ids = tuple(component.component_id for component in values)
        ink_pixels = sum(component.pixels for component in values)
        segment = Segment(
            segment_id=segment_id,
            bbox=bbox,
            source_bbox=transform.box_to_source(bbox),
            kind=SegmentKind.TEXT,
            ink_pixels=ink_pixels,
            row_index=row_tops[bbox.top],
            order_key=(bbox.top, bbox.left),
            parent_path=leaf.path,
            component_ids=component_ids,
        )
        segments.append(segment)
        leaf_segment_ids[leaf.node_id] = (segment_id,)
        for component in values:
            for run in component.runs:
                current = ownership[run.row, run.start : run.stop]
                if np.any(current != -1):
                    raise RuntimeError("non-rule foreground has multiple segment owners")
                ownership[run.row, run.start : run.stop] = index
    return tuple(segments), ownership, leaf_segment_ids


def _materialize_nodes(
    drafts: tuple[_NodeDraft, ...],
    leaf_segment_ids: dict[str, tuple[str, ...]],
    segments: tuple[Segment, ...],
) -> tuple[RecursiveNode, ...]:
    segment_order = {segment.segment_id: segment.order_key for segment in segments}
    descendants: dict[str, tuple[str, ...]] = {}
    for draft in reversed(drafts):
        if draft.child_ids:
            descendants[draft.node_id] = tuple(
                sorted(
                    (segment_id for child_id in draft.child_ids for segment_id in descendants[child_id]),
                    key=segment_order.__getitem__,
                )
            )
        else:
            descendants[draft.node_id] = leaf_segment_ids[draft.node_id]

    drafts_by_id = {draft.node_id: draft for draft in drafts}
    preorder: list[_NodeDraft] = []
    stack = [drafts_by_id["geo-root"]]
    while stack:
        draft = stack.pop()
        preorder.append(draft)
        stack.extend(reversed(tuple(drafts_by_id[child_id] for child_id in draft.child_ids)))
    return tuple(
        RecursiveNode(
            node_id=draft.node_id,
            bbox=draft.bbox,
            depth=draft.depth,
            parent_id=draft.parent_id,
            axis=draft.axis,
            child_ids=draft.child_ids,
            segment_ids=descendants[draft.node_id],
            separator_boxes=draft.separator_boxes,
            split_coordinate=draft.split_coordinate,
            stop_reason=draft.stop_reason,
        )
        for draft in preorder
    )


def _intervals(cuts: set[int]) -> tuple[AxisInterval, ...]:
    ordered = tuple(sorted(cuts))
    return tuple(
        AxisInterval(index=index, start=start, end=stop)
        for index, (start, stop) in enumerate(zip(ordered, ordered[1:]))
        if stop > start
    )


def _overlapping_interval_indices(
    intervals: tuple[AxisInterval, ...], box_start: int, box_stop: int
) -> tuple[int, ...]:
    return tuple(interval.index for interval in intervals if interval.start < box_stop and box_start < interval.end)


def _project_sparse_matrix(
    shape: tuple[int, int],
    ownership: np.ndarray,
    segments: tuple[Segment, ...],
    rules: tuple[Rule, ...],
    nodes: tuple[RecursiveNode, ...],
) -> SparseSegmentMatrix:
    height, width = shape
    if not segments and not rules:
        return SparseSegmentMatrix(rows=(), columns=(), cells=(), spans=())
    row_cuts = {0, height}
    column_cuts = {0, width}
    # The matrix is one physical rule partition, not the Cartesian product of
    # every object-local recursive decision.  Local whitespace/valley seams
    # remain losslessly represented by ``nodes``.  Projecting those local
    # coordinates globally made unrelated page regions cross each other and
    # produced hundreds of thousands of false sparse cells on long scans.
    for rule in rules:
        box = rule.bbox
        if rule.axis is RuleAxis.HORIZONTAL:
            row_cuts.update((box.top, box.bottom))
        else:
            column_cuts.update((box.left, box.right))
    rows = _intervals(row_cuts)
    columns = _intervals(column_cuts)
    row_lookup = np.empty(height, dtype=np.int32)
    column_lookup = np.empty(width, dtype=np.int32)
    for interval in rows:
        row_lookup[interval.start : interval.end] = interval.index
    for interval in columns:
        column_lookup[interval.start : interval.end] = interval.index

    cell_entries: set[tuple[int, int, str]] = set()
    for pixel_row in range(height):
        occupied_columns = np.flatnonzero(ownership[pixel_row] >= 0)
        if occupied_columns.size == 0:
            continue
        pairs = np.stack(
            (
                column_lookup[occupied_columns],
                ownership[pixel_row, occupied_columns],
            ),
            axis=1,
        )
        matrix_row = int(row_lookup[pixel_row])
        for column, segment_index in np.unique(pairs, axis=0):
            cell_entries.add(
                (
                    matrix_row,
                    int(column),
                    segments[int(segment_index)].segment_id,
                )
            )
    cells = tuple(
        SparseCell(row=row, column=column, segment_id=segment_id) for row, column, segment_id in sorted(cell_entries)
    )
    cells_by_segment: dict[str, list[SparseCell]] = {
        segment.segment_id: [] for segment in segments
    }
    for cell in cells:
        cells_by_segment[cell.segment_id].append(cell)
    spans: list[SegmentSpan] = []
    for segment in segments:
        owned_cells = cells_by_segment[segment.segment_id]
        if not owned_cells:
            raise RuntimeError(f"segment {segment.segment_id} has no sparse cells")
        spans.append(
            SegmentSpan(
                segment_id=segment.segment_id,
                row_start=min(cell.row for cell in owned_cells),
                row_stop=max(cell.row for cell in owned_cells) + 1,
                column_start=min(cell.column for cell in owned_cells),
                column_stop=max(cell.column for cell in owned_cells) + 1,
            )
        )
    horizontal_rows = tuple(
        sorted(
            {
                index
                for rule in rules
                if rule.axis is RuleAxis.HORIZONTAL
                for index in _overlapping_interval_indices(rows, rule.bbox.top, rule.bbox.bottom)
            }
        )
    )
    vertical_columns = tuple(
        sorted(
            {
                index
                for rule in rules
                if rule.axis is RuleAxis.VERTICAL
                for index in _overlapping_interval_indices(columns, rule.bbox.left, rule.bbox.right)
            }
        )
    )
    return SparseSegmentMatrix(
        rows=rows,
        columns=columns,
        cells=cells,
        spans=tuple(spans),
        horizontal_rule_rows=horizontal_rows,
        vertical_rule_columns=vertical_columns,
    )


def _mask_bbox(mask: np.ndarray) -> Box | None:
    coordinates = np.argwhere(mask)
    if coordinates.size == 0:
        return None
    top, left = coordinates.min(axis=0)
    bottom, right = coordinates.max(axis=0) + 1
    return Box(int(left), int(top), int(right), int(bottom))
