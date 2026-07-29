"""Diagnostic adapter for the contextual alignment/crop grid frozen in v16.

The legacy grid has two independent kinds of evidence:

* contextual source-coordinate leaf rectangles used for OCR; and
* a logical sparse projection made of anchors, numeric structural codes and
  x-track indexes.

Those logical coordinates are deliberately not converted into pixel bands.
Pixel ownership is a separate, deterministic raster used only to isolate
physical OCR crops when contextual leaf rectangles overlap.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

import numpy as np
from PIL import Image

from app.sparse_pipeline.contracts import (
    AffineTransform,
    AlignmentTrace,
    AxisInterval,
    Box,
    GeometryResult,
    GeometryStatus,
    RecursiveNode,
    Segment,
    SegmentationResult,
    SegmentKind,
    SegmentSpan,
    SparseCell,
    SparseCoordinateMode,
    SparseSegmentMatrix,
    SparseStructuralCode,
    SplitAxis,
    StopReason,
)
from app.sparse_pipeline.geometry import (
    GeometryBundle,
    GeometryConfig,
    GeometryLimitError,
    _mask_bbox,
    _readonly,
    _source_rgb,
)
from app.sparse_pipeline.v16_recursive_grid import (
    RECURSIVE_GRID_TRACE_VERSION,
    RecursiveGridAnalysis,
    RecursiveGridConfig,
    RecursiveGridLeaf,
    RegionDecision,
    analyze_recursive_grid,
    group_recursive_leaves,
)


@dataclass(frozen=True)
class V16LeafTrace:
    segment_id: str
    source_bbox: Box
    content_bbox: Box
    left_tracks: tuple[int, ...]
    dash_track: int | None
    merge_left_tracks: tuple[int, ...]
    decisions: tuple[RegionDecision, ...]
    anchor: tuple[int, int]
    codes: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True)
class V16GroupTrace:
    index: int
    bbox: Box
    segment_ids: tuple[str, ...]


@dataclass(frozen=True)
class V16AdapterTrace:
    version: int
    rows: int
    columns: int
    x_tracks: tuple[int, ...]
    codes: tuple[tuple[int, int, int], ...]
    leaves: tuple[V16LeafTrace, ...]
    groups: tuple[V16GroupTrace, ...]
    projection_sha256: str
    foreground_definition: str
    foreground_pixels: int
    tracked_foreground_pixels: int
    excluded_foreground_pixels: int
    excluded_foreground_sha256: str
    duplicated_foreground_pixels: int
    maximum_bbox_multiplicity: int
    background_rgb: tuple[int, int, int]

    def __post_init__(self) -> None:
        if self.version != RECURSIVE_GRID_TRACE_VERSION:
            raise ValueError("unsupported recursive-grid trace version")
        if self.rows < 0 or self.columns < 0:
            raise ValueError("v16 logical projection dimensions must be non-negative")
        if bool(self.rows) != bool(self.columns):
            raise ValueError("v16 logical projection axes must both be empty or non-empty")
        if not self.leaves and any(
            (self.rows, self.columns, self.x_tracks, self.codes, self.groups)
        ):
            raise ValueError("empty v16 evidence cannot retain a projected matrix")
        if self.leaves and (not self.rows or not self.columns):
            raise ValueError("v16 leaf evidence requires a non-empty projection")
        if len(self.x_tracks) != self.columns:
            raise ValueError("v16 x-track count disagrees with logical columns")
        if len(self.leaves) != len({item.segment_id for item in self.leaves}):
            raise ValueError("v16 adapter segment IDs must be unique")
        if self.foreground_pixels != (
            self.tracked_foreground_pixels + self.excluded_foreground_pixels
        ):
            raise ValueError("v16 foreground accounting is not exact")
        for digest in (
            self.projection_sha256,
            self.excluded_foreground_sha256,
        ):
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("v16 adapter digest is invalid")


@dataclass(frozen=True)
class V16GeometryBundle(GeometryBundle):
    """Normal geometry evidence plus the literal legacy and exclusion layers."""

    v16_trace: V16AdapterTrace
    excluded_foreground_mask: np.ndarray

    def __post_init__(self) -> None:
        if not isinstance(self.v16_trace, V16AdapterTrace):
            raise ValueError("v16 bundle trace is invalid")
        if (
            not isinstance(self.excluded_foreground_mask, np.ndarray)
            or self.excluded_foreground_mask.dtype != np.bool_
            or self.excluded_foreground_mask.shape != self.foreground_mask.shape
            or self.excluded_foreground_mask.flags.writeable
        ):
            raise ValueError("v16 excluded foreground mask is invalid")
        if np.any(
            np.logical_and(
                self.excluded_foreground_mask,
                self.foreground_mask,
            )
        ):
            raise ValueError("tracked and excluded foreground must be disjoint")
        if int(self.excluded_foreground_mask.sum()) != (
            self.v16_trace.excluded_foreground_pixels
        ):
            raise ValueError("v16 excluded foreground count disagrees with mask")
        segment_ids = tuple(
            item.segment_id for item in self.result.segmentation.segments
        )
        if tuple(item.segment_id for item in self.v16_trace.leaves) != segment_ids:
            raise ValueError("v16 trace leaves disagree with geometry segments")
        matrix = self.result.matrix
        if (
            matrix.coordinate_mode is not SparseCoordinateMode.LOGICAL_PROJECTION
            or len(matrix.rows) != self.v16_trace.rows
            or len(matrix.columns) != self.v16_trace.columns
            or matrix.projection_sha256 != self.v16_trace.projection_sha256
        ):
            raise ValueError("v16 trace disagrees with logical sparse projection")
        if (
            self.ownership.dtype != np.int32
            or self.ownership.shape != self.foreground_mask.shape
            or self.ownership.flags.writeable
        ):
            raise ValueError("v16 ownership raster is invalid")
        if np.any(np.logical_and(self.foreground_mask, self.ownership < 0)):
            raise ValueError("tracked v16 foreground must have one owner")
        if np.any(np.logical_and(~self.foreground_mask, self.ownership >= 0)):
            raise ValueError("v16 ownership cannot claim background pixels")


class V16GeometryAnalyzer:
    """Replay legacy alignment crops; never use them as production objects."""

    def __init__(
        self,
        config: GeometryConfig | None = None,
        *,
        recursive_config: RecursiveGridConfig | None = None,
    ) -> None:
        if config is not None and not isinstance(config, GeometryConfig):
            raise TypeError("config must be a GeometryConfig")
        if recursive_config is not None and not isinstance(
            recursive_config,
            RecursiveGridConfig,
        ):
            raise TypeError("recursive_config must be a RecursiveGridConfig")
        self.config = config or GeometryConfig()
        self.recursive_config = recursive_config or RecursiveGridConfig()
        self._last_bundle: V16GeometryBundle | None = None

    @property
    def last_bundle(self) -> V16GeometryBundle | None:
        return self._last_bundle

    def analyze(self, image: Image.Image) -> GeometryResult:
        bundle = self.analyze_bundle(image)
        self._last_bundle = bundle
        return bundle.result

    def analyze_bundle(self, image: Image.Image) -> V16GeometryBundle:
        if not isinstance(image, Image.Image):
            raise TypeError("image must be a Pillow Image")
        if image.width < 1 or image.height < 1:
            raise ValueError("image dimensions must be positive")
        pixels = image.width * image.height
        if pixels > self.config.max_input_pixels:
            raise GeometryLimitError(
                f"input pixel limit exceeded: {pixels} > "
                f"{self.config.max_input_pixels}"
            )
        if pixels > self.config.max_aligned_pixels:
            raise GeometryLimitError(
                f"aligned pixel limit exceeded before allocation: {pixels} > "
                f"{self.config.max_aligned_pixels}"
            )

        source_rgb = _source_rgb(image, self.config.alpha_background_rgb)
        foreground, background = _audit_foreground(source_rgb)
        canonical_image = Image.fromarray(
            np.array(source_rgb, copy=True),
            mode="RGB",
        )
        recursive_config = replace(
            self.recursive_config,
            max_depth=min(
                self.recursive_config.max_depth,
                self.config.max_depth,
            ),
        )
        try:
            analysis = analyze_recursive_grid(canonical_image, recursive_config)
        finally:
            canonical_image.close()
        try:
            if len(analysis.leaves) > self.config.max_components:
                raise GeometryLimitError(
                    "v16 leaf/component count exceeds configured limit "
                    f"{self.config.max_components}"
                )
            if len(analysis.leaves) + 1 > self.config.max_nodes:
                raise GeometryLimitError(
                    "v16 recursive evidence exceeds configured node limit "
                    f"{self.config.max_nodes}"
                )
            bundle = self._adapt(
                source_rgb=source_rgb,
                foreground=foreground,
                background=background,
                analysis=analysis,
            )
        finally:
            for leaf in analysis.leaves:
                leaf.image.close()
        self._last_bundle = bundle
        return bundle

    def _adapt(
        self,
        *,
        source_rgb: np.ndarray,
        foreground: np.ndarray,
        background: tuple[int, int, int],
        analysis: RecursiveGridAnalysis,
    ) -> V16GeometryBundle:
        if not analysis.leaves:
            return _empty_bundle(
                source_rgb=source_rgb,
                foreground=foreground,
                background=background,
                reason="blank-or-unsegmentable-page",
                stop_reason=StopReason.EMPTY,
            )
        height, width = foreground.shape
        leaf_ids = {
            id(leaf): f"segment-{index:06d}"
            for index, leaf in enumerate(analysis.leaves)
        }
        projection_by_leaf = {
            id(leaf): (anchor, tuple(sorted(codes)))
            for leaf, anchor, codes in analysis.projection.leaf_projection
        }
        coverage = np.zeros((height, width), dtype=np.uint16)
        for leaf in analysis.leaves:
            left, top, right, bottom = leaf.source_bbox
            coverage[top:bottom, left:right] += 1
        foreground, rescued_pixels = _rescue_faint_leaf_foreground(
            source_rgb=source_rgb,
            foreground=foreground,
            background=background,
            analysis=analysis,
        )
        tracked = np.logical_and(foreground, coverage > 0)
        excluded = np.logical_and(foreground, coverage == 0)
        ownership = _resolve_contextual_ownership(
            tracked,
            analysis,
        )
        counts = np.bincount(
            ownership[ownership >= 0],
            minlength=len(analysis.leaves),
        )
        if np.any(counts == 0):
            missing = tuple(int(item) for item in np.flatnonzero(counts == 0))
            return _empty_bundle(
                source_rgb=source_rgb,
                foreground=foreground,
                background=background,
                reason=(
                    "unowned-contextual-leaves="
                    + ",".join(str(item) for item in missing[:8])
                ),
                stop_reason=StopReason.LIMIT,
            )

        leaf_traces = tuple(
            _leaf_trace(
                leaf,
                segment_id=leaf_ids[id(leaf)],
                projection=projection_by_leaf[id(leaf)],
            )
            for leaf in analysis.leaves
        )
        groups = tuple(
            V16GroupTrace(
                index=group.index,
                bbox=Box(*group.bbox),
                segment_ids=tuple(leaf_ids[id(leaf)] for leaf in group.leaves),
            )
            for group in group_recursive_leaves(list(analysis.leaves))
        )
        projection_payload = _projection_payload(
            analysis=analysis,
            leaves=leaf_traces,
            groups=groups,
        )
        projection_sha256 = _canonical_sha256(projection_payload)
        excluded_sha256 = _mask_sha256(excluded)

        cells = tuple(
            sorted(
                (
                    SparseCell(
                        row=trace.anchor[0],
                        column=trace.anchor[1],
                        segment_id=trace.segment_id,
                    )
                    for trace in leaf_traces
                ),
                key=lambda item: (item.row, item.column, item.segment_id),
            )
        )
        spans = tuple(
            SegmentSpan(
                segment_id=trace.segment_id,
                row_start=trace.anchor[0],
                row_stop=trace.anchor[0] + 1,
                column_start=trace.anchor[1],
                column_stop=trace.anchor[1] + 1,
            )
            for trace in leaf_traces
        )
        structural_codes = tuple(
            sorted(
                (
                    SparseStructuralCode(
                        row=row,
                        column=column,
                        code=code,
                        segment_id=trace.segment_id,
                    )
                    for trace in leaf_traces
                    for row, column, code in trace.codes
                ),
                key=lambda item: (
                    item.row,
                    item.column,
                    item.segment_id,
                    item.code,
                ),
            )
        )
        matrix = SparseSegmentMatrix(
            rows=tuple(
                AxisInterval(index, index, index + 1)
                for index in range(analysis.signature.rows)
            ),
            columns=tuple(
                AxisInterval(index, index, index + 1)
                for index in range(analysis.signature.cols)
            ),
            cells=cells,
            spans=spans,
            coordinate_mode=SparseCoordinateMode.LOGICAL_PROJECTION,
            structural_codes=structural_codes,
            projection_sha256=projection_sha256,
        )
        node_id_by_path = {
            node.path: (
                "geo-root"
                + "".join(f".{child_index}" for child_index in node.path)
            )
            for node in analysis.nodes
        }
        leaf_path_by_index = {
            node.leaf_index: node.path
            for node in analysis.nodes
            if node.leaf_index is not None
        }

        def parent_path(leaf_index: int) -> tuple[str, ...]:
            path = leaf_path_by_index.get(leaf_index)
            if path is None:
                raise ValueError("v16 recursive leaf lost its owning node")
            return tuple(
                node_id_by_path[path[:depth]]
                for depth in range(len(path) + 1)
            )

        segments = tuple(
            Segment(
                segment_id=trace.segment_id,
                bbox=trace.source_bbox,
                source_bbox=trace.source_bbox,
                kind=SegmentKind.TEXT,
                ink_pixels=int(counts[index]),
                row_index=trace.anchor[0],
                order_key=(trace.source_bbox.top, trace.source_bbox.left),
                parent_path=parent_path(index),
            )
            for index, trace in enumerate(leaf_traces)
        )
        segment_ids = tuple(item.segment_id for item in segments)
        segment_id_by_leaf = {
            index: segment.segment_id for index, segment in enumerate(segments)
        }
        trace_node_by_path = {node.path: node for node in analysis.nodes}

        def descendant_segment_ids(path: tuple[int, ...]) -> tuple[str, ...]:
            node = trace_node_by_path[path]
            if node.leaf_index is not None:
                return (segment_id_by_leaf[node.leaf_index],)
            return tuple(
                segment_id
                for child_path in node.child_paths
                for segment_id in descendant_segment_ids(child_path)
            )

        nodes = tuple(
            RecursiveNode(
                node_id=node_id_by_path[node.path],
                bbox=Box(*node.source_bbox),
                depth=len(node.path),
                parent_id=(
                    node_id_by_path[node.path[:-1]] if node.path else None
                ),
                axis=(SplitAxis(node.axis) if node.child_paths else None),
                child_ids=tuple(
                    node_id_by_path[child] for child in node.child_paths
                ),
                segment_ids=descendant_segment_ids(node.path),
                separator_boxes=tuple(
                    Box(*separator) for separator in node.separator_boxes
                ),
                split_coordinate=(
                    node.split_coordinate
                    if node.child_paths and not node.separator_boxes
                    else None
                ),
                stop_reason=(
                    None
                    if node.child_paths
                    else (
                        StopReason.CHARACTER_HEIGHT
                        if node.stop_flag is not None
                        else StopReason.ATOMIC
                    )
                ),
            )
            for node in analysis.nodes
        )
        tracked_pixels = int(tracked.sum())
        segmentation = SegmentationResult(
            segments=segments,
            rules=(),
            nodes=nodes,
            root_node_id="geo-root",
            aligned_size=(width, height),
            foreground_pixels=tracked_pixels,
            diagnostics=(
                "backend=v16-recursive-grid",
                "coordinates=literal-logical-projection",
                "ownership=deterministic-single-owner-context-overlap",
            ),
        )
        aligned_rgb = np.array(source_rgb, copy=True)
        aligned_rgb[excluded] = np.asarray(background, dtype=np.uint8)
        alignment = AlignmentTrace(
            transform=AffineTransform.identity((width, height)),
            correction_degrees=0.0,
            background_rgb=background,
            content_bbox=_mask_bbox(tracked),
            foreground_pixels=tracked_pixels,
        )
        result = GeometryResult(
            alignment=alignment,
            segmentation=segmentation,
            matrix=matrix,
            aligned_rgb_sha256=hashlib.sha256(
                memoryview(np.ascontiguousarray(aligned_rgb))
            ).hexdigest(),
            status=GeometryStatus.COMPLETE,
            diagnostics=(
                f"v16_leaves={len(segments)}",
                f"v16_groups={len(groups)}",
                f"v16_matrix={analysis.signature.rows}x{analysis.signature.cols}",
                f"v16_structural_codes={len(structural_codes)}",
                f"tracked_foreground_pixels={tracked_pixels}",
                f"excluded_foreground_pixels={int(excluded.sum())}",
                f"faint_leaf_rescue_pixels={rescued_pixels}",
                f"duplicated_foreground_pixels={int(np.logical_and(foreground, coverage > 1).sum())}",
            ),
        )
        trace = V16AdapterTrace(
            version=RECURSIVE_GRID_TRACE_VERSION,
            rows=analysis.signature.rows,
            columns=analysis.signature.cols,
            x_tracks=analysis.signature.x_tracks,
            codes=analysis.signature.codes,
            leaves=leaf_traces,
            groups=groups,
            projection_sha256=projection_sha256,
            foreground_definition=(
                "max RGB distance >=24 from median 16px page border; "
                "a leaf with no such evidence may add canonical source "
                "pixels at distance >=10"
            ),
            foreground_pixels=int(foreground.sum()),
            tracked_foreground_pixels=tracked_pixels,
            excluded_foreground_pixels=int(excluded.sum()),
            excluded_foreground_sha256=excluded_sha256,
            duplicated_foreground_pixels=int(
                np.logical_and(foreground, coverage > 1).sum()
            ),
            maximum_bbox_multiplicity=int(coverage.max(initial=0)),
            background_rgb=background,
        )
        bundle = V16GeometryBundle(
            result=result,
            source_rgb=_readonly(source_rgb),
            aligned_rgb=_readonly(aligned_rgb),
            foreground_mask=_readonly(tracked),
            rule_mask=_readonly(np.zeros_like(tracked, dtype=bool)),
            ownership=_readonly(ownership),
            v16_trace=trace,
            excluded_foreground_mask=_readonly(excluded),
        )
        return bundle


def _empty_bundle(
    *,
    source_rgb: np.ndarray,
    foreground: np.ndarray,
    background: tuple[int, int, int],
    reason: str,
    stop_reason: StopReason,
) -> V16GeometryBundle:
    """Return valid bounded geometry for a blank or fail-closed page."""

    height, width = foreground.shape
    projection_payload = {
        "version": RECURSIVE_GRID_TRACE_VERSION,
        "rows": 0,
        "columns": 0,
        "x_tracks": [],
        "codes": [],
        "leaves": [],
        "groups": [],
    }
    projection_sha256 = _canonical_sha256(projection_payload)
    matrix = SparseSegmentMatrix(
        rows=(),
        columns=(),
        cells=(),
        spans=(),
        coordinate_mode=SparseCoordinateMode.LOGICAL_PROJECTION,
        projection_sha256=projection_sha256,
    )
    node = RecursiveNode(
        node_id="geo-root",
        bbox=Box(0, 0, width, height),
        depth=0,
        parent_id=None,
        segment_ids=(),
        stop_reason=stop_reason,
    )
    segmentation = SegmentationResult(
        segments=(),
        rules=(),
        nodes=(node,),
        root_node_id="geo-root",
        aligned_size=(width, height),
        foreground_pixels=0,
        diagnostics=(
            "backend=v16-recursive-grid",
            "coordinates=literal-logical-projection",
            f"empty_reason={reason}",
        ),
    )
    excluded = np.array(foreground, copy=True, dtype=bool)
    tracked = np.zeros_like(excluded, dtype=bool)
    ownership = np.full((height, width), -1, dtype=np.int32)
    aligned_rgb = np.array(source_rgb, copy=True)
    aligned_rgb[excluded] = np.asarray(background, dtype=np.uint8)
    alignment = AlignmentTrace(
        transform=AffineTransform.identity((width, height)),
        correction_degrees=0.0,
        background_rgb=background,
        content_bbox=None,
        foreground_pixels=0,
    )
    status = (
        GeometryStatus.DEGRADED
        if stop_reason is StopReason.LIMIT
        else GeometryStatus.COMPLETE
    )
    result = GeometryResult(
        alignment=alignment,
        segmentation=segmentation,
        matrix=matrix,
        aligned_rgb_sha256=hashlib.sha256(
            memoryview(np.ascontiguousarray(aligned_rgb))
        ).hexdigest(),
        status=status,
        diagnostics=(
            "v16_leaves=0",
            "v16_groups=0",
            "v16_matrix=0x0",
            "v16_structural_codes=0",
            f"empty_reason={reason}",
            f"excluded_foreground_pixels={int(excluded.sum())}",
        ),
    )
    trace = V16AdapterTrace(
        version=RECURSIVE_GRID_TRACE_VERSION,
        rows=0,
        columns=0,
        x_tracks=(),
        codes=(),
        leaves=(),
        groups=(),
        projection_sha256=projection_sha256,
        foreground_definition=(
            "max RGB distance >=24 from median 16px page border; "
            "no segmentable contextual leaf"
        ),
        foreground_pixels=int(excluded.sum()),
        tracked_foreground_pixels=0,
        excluded_foreground_pixels=int(excluded.sum()),
        excluded_foreground_sha256=_mask_sha256(excluded),
        duplicated_foreground_pixels=0,
        maximum_bbox_multiplicity=0,
        background_rgb=background,
    )
    return V16GeometryBundle(
        result=result,
        source_rgb=_readonly(source_rgb),
        aligned_rgb=_readonly(aligned_rgb),
        foreground_mask=_readonly(tracked),
        rule_mask=_readonly(np.zeros_like(tracked, dtype=bool)),
        ownership=_readonly(ownership),
        v16_trace=trace,
        excluded_foreground_mask=_readonly(excluded),
    )


def _rescue_faint_leaf_foreground(
    *,
    source_rgb: np.ndarray,
    foreground: np.ndarray,
    background: tuple[int, int, int],
    analysis: RecursiveGridAnalysis,
) -> tuple[np.ndarray, int]:
    """Bind faint v16 leaves to real canonical-source pixels, never blanks."""

    expanded = np.array(foreground, copy=True, dtype=bool)
    missing = []
    for leaf in analysis.leaves:
        left, top, right, bottom = leaf.source_bbox
        if not np.any(foreground[top:bottom, left:right]):
            missing.append(leaf)
    if not missing:
        return expanded, 0

    background_array = np.asarray(background, dtype=np.int16)
    weak = np.max(
        np.abs(source_rgb.astype(np.int16) - background_array),
        axis=2,
    ) >= 10
    for leaf in missing:
        left, top, right, bottom = leaf.content_bbox
        local = weak[top:bottom, left:right]
        if np.any(local):
            expanded[top:bottom, left:right] |= local
            continue
        left, top, right, bottom = leaf.source_bbox
        expanded[top:bottom, left:right] |= weak[top:bottom, left:right]
    return expanded, int(np.logical_and(expanded, ~foreground).sum())


def _audit_foreground(
    rgb: np.ndarray,
) -> tuple[np.ndarray, tuple[int, int, int]]:
    height, width = rgb.shape[:2]
    border_width = max(1, min(16, height // 20, width // 20))
    border = np.concatenate(
        (
            rgb[:border_width].reshape(-1, 3),
            rgb[-border_width:].reshape(-1, 3),
            rgb[:, :border_width].reshape(-1, 3),
            rgb[:, -border_width:].reshape(-1, 3),
        ),
        axis=0,
    )
    background_array = np.median(border, axis=0).astype(np.int16)
    delta = np.max(
        np.abs(rgb.astype(np.int16) - background_array),
        axis=2,
    )
    return (
        delta >= 24,
        tuple(int(value) for value in background_array),
    )


def _resolve_contextual_ownership(
    tracked: np.ndarray,
    analysis: RecursiveGridAnalysis,
) -> np.ndarray:
    """Resolve bbox overlap while retaining literal contextual rectangles."""

    ownership = np.full(tracked.shape, -1, dtype=np.int32)
    # Reverse assignment makes lower canonical source order the stable winner.
    # Source rectangles establish complete tracked coverage first; content
    # rectangles then outrank context-only overlap without changing bboxes.
    for index in reversed(range(len(analysis.leaves))):
        left, top, right, bottom = analysis.leaves[index].source_bbox
        local = tracked[top:bottom, left:right]
        owners = ownership[top:bottom, left:right]
        owners[local] = index
    for index in reversed(range(len(analysis.leaves))):
        left, top, right, bottom = analysis.leaves[index].content_bbox
        local = tracked[top:bottom, left:right]
        owners = ownership[top:bottom, left:right]
        owners[local] = index
    if np.any(np.logical_and(tracked, ownership < 0)):
        raise RuntimeError("v16 contextual ownership lost tracked foreground")
    return ownership


def _leaf_trace(
    leaf: RecursiveGridLeaf,
    *,
    segment_id: str,
    projection: tuple[tuple[int, int], tuple[tuple[int, int, int], ...]],
) -> V16LeafTrace:
    # RecursiveGridLeaf stays image-bearing for exact v16 parity; the adapter
    # copies only immutable metadata before those images are closed.
    source_bbox = Box(*leaf.source_bbox)
    content_bbox = Box(*leaf.content_bbox)
    anchor, codes = projection
    return V16LeafTrace(
        segment_id=segment_id,
        source_bbox=source_bbox,
        content_bbox=content_bbox,
        left_tracks=leaf.left_tracks,
        dash_track=leaf.dash_track,
        merge_left_tracks=leaf.merge_left_tracks,
        decisions=leaf.decisions,
        anchor=anchor,
        codes=codes,
    )


def _projection_payload(
    *,
    analysis: RecursiveGridAnalysis,
    leaves: tuple[V16LeafTrace, ...],
    groups: tuple[V16GroupTrace, ...],
) -> dict[str, object]:
    return {
        "version": RECURSIVE_GRID_TRACE_VERSION,
        "rows": analysis.signature.rows,
        "columns": analysis.signature.cols,
        "x_tracks": list(analysis.signature.x_tracks),
        "codes": [list(item) for item in analysis.signature.codes],
        "leaves": [
            {
                "segment_id": item.segment_id,
                "source_bbox": list(item.source_bbox.as_tuple()),
                "content_bbox": list(item.content_bbox.as_tuple()),
                "left_tracks": list(item.left_tracks),
                "dash_track": item.dash_track,
                "merge_left_tracks": list(item.merge_left_tracks),
                "anchor": list(item.anchor),
                "codes": [list(code) for code in item.codes],
                "decisions": [decision.metadata() for decision in item.decisions],
            }
            for item in leaves
        ],
        "groups": [
            {
                "index": item.index,
                "bbox": list(item.bbox.as_tuple()),
                "segment_ids": list(item.segment_ids),
            }
            for item in groups
        ],
    }


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()


def _mask_sha256(mask: np.ndarray) -> str:
    digest = hashlib.sha256()
    digest.update(
        f"{mask.shape[0]}x{mask.shape[1]}:packbits-big".encode("ascii")
    )
    digest.update(np.packbits(mask, bitorder="big").tobytes())
    return digest.hexdigest()
