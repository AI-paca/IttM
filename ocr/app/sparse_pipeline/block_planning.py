from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum

from app.sparse_pipeline.contracts import (
    Box,
    Segment,
    SegmentSpan,
    SparseCell,
    SparseCoordinateMode,
    SparseSegmentMatrix,
)
from app.sparse_pipeline.object_reconstruction import (
    DocumentObject,
    ObjectKind,
    ObjectReconstructionResult,
)
from app.sparse_pipeline.v16_sparse_codes import (
    MERGE_LEFT_CODE,
    sparse_code_components,
)


class BlockPlanStatus(str, Enum):
    COMPLETE = "complete"


class BlockPlanningMode(str, Enum):
    """Geometry used to form OCR context blocks.

    ``FULL_WIDTH`` is the original Stage 5 contract.  ``SPATIAL_2D`` keeps
    bounded neighbouring rows and columns in the same crop while preserving
    the exact canonical core partition.
    """

    FULL_WIDTH = "full_width"
    SPATIAL_2D = "spatial_2d"


class BlockPlanningInvariantError(ValueError):
    """Raised when stage 1/6 evidence disagrees at the block boundary."""


class BlockPlanningLimitError(RuntimeError):
    """Raised before a bounded overlapping block plan would exceed limits."""


_MAX_BLOCK_PLAN_OVERLAP_INVARIANT_CHECKS = 2_000_000


@dataclass
class _WorkBudget:
    maximum: int
    operation: str
    checks: int = 0

    def consume(self, count: int) -> None:
        if type(count) is not int or count < 0:
            raise ValueError("work budget increments must be non-negative integers")
        if self.checks + count > self.maximum:
            raise BlockPlanningLimitError(
                f"{self.operation} exceeds configured work limit "
                f"{self.maximum}"
            )
        self.checks += count


class MembershipUnitKind(str, Enum):
    """Resolution available from one exact block-membership signature."""

    SEGMENT = "segment"
    SUBBLOCK = "subblock"


def sparse_matrix_payload(matrix: SparseSegmentMatrix) -> dict[str, object]:
    """Return canonical JSON-compatible Stage 1 matrix evidence."""
    if not isinstance(matrix, SparseSegmentMatrix):
        raise TypeError("matrix must be a SparseSegmentMatrix")
    payload: dict[str, object] = {
        "rows": [
            [item.index, item.start, item.end] for item in matrix.rows
        ],
        "columns": [
            [item.index, item.start, item.end] for item in matrix.columns
        ],
        "cells": [
            [item.row, item.column, item.segment_id]
            for item in matrix.cells
        ],
        "spans": [
            [
                item.segment_id,
                item.row_start,
                item.row_stop,
                item.column_start,
                item.column_stop,
            ]
            for item in matrix.spans
        ],
        "horizontal_rule_rows": list(matrix.horizontal_rule_rows),
        "vertical_rule_columns": list(matrix.vertical_rule_columns),
    }
    if matrix.coordinate_mode.value != "pixel_partition":
        payload.update(
            {
                "coordinate_mode": matrix.coordinate_mode.value,
                "projection_sha256": matrix.projection_sha256,
                "structural_codes": [
                    [item.row, item.column, item.segment_id, item.code]
                    for item in matrix.structural_codes
                ],
            }
        )
    return payload


def sparse_matrix_sha256(matrix: SparseSegmentMatrix) -> str:
    """Return a canonical digest for the exact Stage 1 planning matrix."""

    payload = sparse_matrix_payload(matrix)
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()


def _clamped_spatial_bbox(
    member_bbox: Box,
    *,
    excluded_segments: tuple[Segment, ...],
    aligned_size: tuple[int, int],
    padding: int,
    work_budget: _WorkBudget | None = None,
) -> Box:
    """Apply non-cascading padding without exposing excluded segments."""

    width, height = aligned_size
    bbox = Box(
        max(0, member_bbox.left - padding),
        max(0, member_bbox.top - padding),
        min(width, member_bbox.right + padding),
        min(height, member_bbox.bottom + padding),
    )
    for segment in excluded_segments:
        if work_budget is not None:
            work_budget.consume(1)
        if bbox.intersection(segment.bbox) is None:
            continue
        candidates: list[tuple[int, int, Box]] = []
        if segment.bbox.right <= member_bbox.left:
            candidate = Box(
                max(bbox.left, segment.bbox.right),
                bbox.top,
                bbox.right,
                bbox.bottom,
            )
            candidates.append((bbox.area - candidate.area, 0, candidate))
        if segment.bbox.left >= member_bbox.right:
            candidate = Box(
                bbox.left,
                bbox.top,
                min(bbox.right, segment.bbox.left),
                bbox.bottom,
            )
            candidates.append((bbox.area - candidate.area, 1, candidate))
        if segment.bbox.bottom <= member_bbox.top:
            candidate = Box(
                bbox.left,
                max(bbox.top, segment.bbox.bottom),
                bbox.right,
                bbox.bottom,
            )
            candidates.append((bbox.area - candidate.area, 2, candidate))
        if segment.bbox.top >= member_bbox.bottom:
            candidate = Box(
                bbox.left,
                bbox.top,
                bbox.right,
                min(bbox.bottom, segment.bbox.top),
            )
            candidates.append((bbox.area - candidate.area, 3, candidate))
        if not candidates:
            # Object-local segment bboxes may overlap even though their exact
            # Stage 1 ownership pixels do not.  There is no rectangular clamp
            # that can remove such an enclosure without also removing member
            # geometry.  Keep the unpadded overlap for the literal ownership
            # gate in BlockCropper; production spatial callers always provide
            # that raster and fail before OCR if a foreign pixel is real.
            if member_bbox.intersection(segment.bbox) is not None:
                continue
            raise BlockPlanningInvariantError(
                "spatial padding cannot exclude a non-member segment"
            )
        bbox = min(candidates, key=lambda item: (item[0], item[1]))[2]
    for item in excluded_segments:
        if work_budget is not None:
            work_budget.consume(1)
        if bbox.intersection(item.bbox) is not None:
            if member_bbox.intersection(item.bbox) is not None:
                continue
            raise BlockPlanningInvariantError(
                "spatial padding intersects an excluded segment"
            )
    if member_bbox.intersection(bbox) != member_bbox:
        raise BlockPlanningInvariantError(
            "spatial padding clamp removed member geometry"
        )
    return bbox


@dataclass(frozen=True)
class BlockPlanningConfig:
    """Bounded Stage 5 limits.

    ``spatial_rows``, ``spatial_columns`` and their overlap fields, plus
    ``max_singleton_probes``, remain constructor-compatible with the first
    experimental spatial planner.  Matrix-native planning ignores them and
    records that fact in diagnostics; singleton probes are now forbidden by
    contract rather than budgeted.
    """

    max_segments: int = 100_000
    max_objects: int = 100_000
    max_core_segments: int = 24
    max_block_pixels: int = 16_000_000
    context_segments: int = 4
    padding: int = 8
    max_blocks: int = 100_000
    max_pair_memberships: int = 2_000_000
    mode: BlockPlanningMode = BlockPlanningMode.FULL_WIDTH
    spatial_rows: int = 2
    spatial_columns: int = 5
    spatial_row_overlap: int = 1
    spatial_column_overlap: int = 1
    max_block_segments: int = 4_096
    max_segment_memberships: int = 128
    max_overlap_pairs: int = 200_000
    max_singleton_probes: int = 8
    max_signature_candidates: int = 4_096
    max_signature_candidate_checks: int = 10_000_000
    max_total_block_memberships: int = 2_000_000
    max_logical_row_checks: int = 2_000_000
    max_column_region_checks: int = 4_000_000
    max_padding_exclusion_checks: int = 20_000_000
    max_evidence_revalidation_checks: int = 20_000_000
    max_scope_gap_pixels: int = 64
    max_scope_span_pixels: int = 512
    object_local: bool = False
    adaptive_table_windows: bool = False

    def __post_init__(self) -> None:
        positive = (
            self.max_segments,
            self.max_objects,
            self.max_core_segments,
            self.max_block_pixels,
            self.context_segments,
            self.max_blocks,
            self.max_pair_memberships,
            self.spatial_rows,
            self.spatial_columns,
            self.spatial_row_overlap,
            self.spatial_column_overlap,
            self.max_block_segments,
            self.max_segment_memberships,
            self.max_overlap_pairs,
            self.max_singleton_probes,
            self.max_signature_candidates,
            self.max_signature_candidate_checks,
            self.max_total_block_memberships,
            self.max_logical_row_checks,
            self.max_column_region_checks,
            self.max_padding_exclusion_checks,
            self.max_evidence_revalidation_checks,
            self.max_scope_span_pixels,
        )
        if any(type(value) is not int or value < 1 for value in positive):
            raise ValueError("block planning limits must be positive integers")
        if type(self.padding) is not int or self.padding < 0:
            raise ValueError("block padding must be a non-negative integer")
        if (
            type(self.max_scope_gap_pixels) is not int
            or self.max_scope_gap_pixels < 0
        ):
            raise ValueError("scope gap must be a non-negative integer")
        if not isinstance(self.mode, BlockPlanningMode):
            raise ValueError("block planning mode must be a BlockPlanningMode")
        if type(self.object_local) is not bool:
            raise ValueError("object_local must be a boolean")
        if type(self.adaptive_table_windows) is not bool:
            raise ValueError("adaptive_table_windows must be a boolean")
        if self.object_local and self.mode is not BlockPlanningMode.SPATIAL_2D:
            raise ValueError("object-local planning requires spatial 2D mode")
        if self.object_local and self.adaptive_table_windows:
            raise ValueError(
                "adaptive table windows use the shared spatial planner, not "
                "object-local literal blocks"
            )
        if self.spatial_rows < 2 or self.spatial_columns < 2:
            raise ValueError("spatial block dimensions must be at least two")
        if self.spatial_row_overlap >= self.spatial_rows:
            raise ValueError("spatial row overlap must be below spatial rows")
        if self.spatial_column_overlap >= self.spatial_columns:
            raise ValueError("spatial column overlap must be below spatial columns")
        if (
            self.mode is BlockPlanningMode.SPATIAL_2D
            and self.spatial_row_overlap != self.spatial_rows - 1
        ):
            raise ValueError(
                "spatial 2D row overlap must leave one canonical core row"
            )
        if (
            self.mode is BlockPlanningMode.SPATIAL_2D
            and self.spatial_columns - self.spatial_column_overlap
            > self.max_core_segments
        ):
            raise ValueError(
                "spatial column stride exceeds the core segment limit"
            )


def _string_tuple(name: str, value: tuple[str, ...], *, allow_empty: bool) -> None:
    if type(value) is not tuple or any(type(item) is not str or not item for item in value):
        raise ValueError(f"{name} must be an immutable string tuple")
    if not allow_empty and not value:
        raise ValueError(f"{name} must not be empty")
    if len(value) != len(set(value)):
        raise ValueError(f"{name} must contain unique identifiers")


@dataclass(frozen=True)
class RecognitionBlock:
    block_id: str
    bbox: Box
    core_segment_ids: tuple[str, ...]
    segment_ids: tuple[str, ...]
    context_segment_ids: tuple[str, ...]
    object_ids: tuple[str, ...]
    scope_id: str | None = None
    matrix_window: tuple[int, int, int, int] | None = None
    matrix_window_kind: str | None = None
    matrix_segment_shape: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        # Spatial plans may append context-only OCR probes.  They deliberately
        # own no core segments: ownership remains an exact partition across
        # the primary blocks while the probes add an independent membership
        # bit for OR/XOR recovery.
        _string_tuple("core_segment_ids", self.core_segment_ids, allow_empty=True)
        _string_tuple("segment_ids", self.segment_ids, allow_empty=False)
        _string_tuple("context_segment_ids", self.context_segment_ids, allow_empty=True)
        _string_tuple("object_ids", self.object_ids, allow_empty=True)
        if type(self.block_id) is not str or not self.block_id.startswith("block-"):
            raise ValueError("block_id must use the canonical block prefix")
        if self.scope_id is not None and (
            type(self.scope_id) is not str
            or not self.scope_id.startswith("scope-")
        ):
            raise ValueError("scope_id must use the canonical scope prefix")
        if self.matrix_window is not None:
            if (
                type(self.matrix_window) is not tuple
                or len(self.matrix_window) != 4
                or any(type(item) is not int for item in self.matrix_window)
            ):
                raise ValueError(
                    "matrix_window must be four immutable integer bounds"
                )
            row_start, row_stop, column_start, column_stop = self.matrix_window
            if (
                row_start < 0
                or column_start < 0
                or row_stop <= row_start
                or column_stop <= column_start
            ):
                raise ValueError("matrix_window bounds are invalid")
            if self.matrix_window_kind not in {
                "local",
                "row-control",
                "column-control",
                "context-bridge",
                "structural-singleton",
                "dyadic-mask",
            }:
                raise ValueError("matrix window kind is invalid")
            if (
                type(self.matrix_segment_shape) is not tuple
                or len(self.matrix_segment_shape) != 2
                or any(
                    type(item) is not int or item < 1
                    for item in self.matrix_segment_shape
                )
            ):
                raise ValueError("matrix segment shape is invalid")
        elif (
            self.matrix_window_kind is not None
            or self.matrix_segment_shape is not None
        ):
            raise ValueError(
                "matrix window metadata requires logical window bounds"
            )
        if not isinstance(self.bbox, Box):
            raise ValueError("block bbox must be a Box")
        members = set(self.segment_ids)
        core = set(self.core_segment_ids)
        context = set(self.context_segment_ids)
        if not core.issubset(members):
            raise ValueError("block core segments must be members")
        if core & context or context != members - core:
            raise ValueError("block context must be exactly members minus core")
        if bool(core) != bool(self.object_ids):
            raise ValueError(
                "context-only probes need empty object IDs and core blocks need owners"
            )


@dataclass(frozen=True)
class MembershipUnit:
    """Canonical segment or inseparable subblock recovered by one signature."""

    unit_id: str
    kind: MembershipUnitKind
    segment_ids: tuple[str, ...]
    block_ids: tuple[str, ...]
    scope_id: str

    def __post_init__(self) -> None:
        if (
            type(self.unit_id) is not str
            or not self.unit_id.startswith("membership-unit-")
        ):
            raise ValueError("membership unit ID must use the canonical prefix")
        if not isinstance(self.kind, MembershipUnitKind):
            raise ValueError("membership unit kind is invalid")
        _string_tuple("membership segment_ids", self.segment_ids, allow_empty=False)
        _string_tuple("membership block_ids", self.block_ids, allow_empty=False)
        if type(self.scope_id) is not str or not self.scope_id.startswith("scope-"):
            raise ValueError("membership unit scope ID is invalid")
        expected_kind = (
            MembershipUnitKind.SEGMENT
            if len(self.segment_ids) == 1
            else MembershipUnitKind.SUBBLOCK
        )
        if self.kind is not expected_kind:
            raise ValueError("membership unit kind disagrees with its cardinality")


@dataclass(frozen=True)
class BlockSetAlgebra:
    first_block_id: str
    second_block_id: str
    intersection_segment_ids: tuple[str, ...]
    union_segment_ids: tuple[str, ...]
    xor_segment_ids: tuple[str, ...]
    first_only_segment_ids: tuple[str, ...]
    second_only_segment_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.first_block_id) is not str
            or not self.first_block_id
            or type(self.second_block_id) is not str
            or not self.second_block_id
            or self.first_block_id == self.second_block_id
        ):
            raise ValueError("block algebra needs two distinct block identifiers")
        for name, value, allow_empty in (
            ("intersection_segment_ids", self.intersection_segment_ids, False),
            ("union_segment_ids", self.union_segment_ids, False),
            ("xor_segment_ids", self.xor_segment_ids, True),
            ("first_only_segment_ids", self.first_only_segment_ids, True),
            ("second_only_segment_ids", self.second_only_segment_ids, True),
        ):
            _string_tuple(name, value, allow_empty=allow_empty)
        intersection = set(self.intersection_segment_ids)
        first_only = set(self.first_only_segment_ids)
        second_only = set(self.second_only_segment_ids)
        if intersection & first_only or intersection & second_only or first_only & second_only:
            raise ValueError("block algebra partitions must be disjoint")
        if set(self.union_segment_ids) != intersection | first_only | second_only:
            raise ValueError("block OR/union identity is invalid")
        if set(self.xor_segment_ids) != first_only | second_only:
            raise ValueError("block XOR identity is invalid")
        if intersection != set(self.union_segment_ids) - set(self.xor_segment_ids):
            raise ValueError("block intersection must equal OR minus XOR")


@dataclass(frozen=True)
class BlockPlan:
    aligned_size: tuple[int, int]
    source_segment_ids: tuple[str, ...]
    blocks: tuple[RecognitionBlock, ...]
    adjacent_algebra: tuple[BlockSetAlgebra, ...]
    status: BlockPlanStatus = BlockPlanStatus.COMPLETE
    diagnostics: tuple[str, ...] = ()
    mode: BlockPlanningMode = BlockPlanningMode.FULL_WIDTH
    membership_units: tuple[MembershipUnit, ...] = ()
    matrix_sha256: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.aligned_size) is not tuple
            or len(self.aligned_size) != 2
            or any(type(value) is not int or value < 1 for value in self.aligned_size)
        ):
            raise ValueError("aligned_size must contain two positive integers")
        _string_tuple("source_segment_ids", self.source_segment_ids, allow_empty=True)
        if type(self.blocks) is not tuple or any(not isinstance(item, RecognitionBlock) for item in self.blocks):
            raise ValueError("blocks must be an immutable RecognitionBlock tuple")
        if type(self.adjacent_algebra) is not tuple or any(
            not isinstance(item, BlockSetAlgebra) for item in self.adjacent_algebra
        ):
            raise ValueError("adjacent_algebra must be an immutable tuple")
        if not isinstance(self.status, BlockPlanStatus):
            raise ValueError("block plan status is invalid")
        if not isinstance(self.mode, BlockPlanningMode):
            raise ValueError("block plan mode is invalid")
        if type(self.membership_units) is not tuple or any(
            not isinstance(item, MembershipUnit) for item in self.membership_units
        ):
            raise ValueError("membership_units must be an immutable tuple")
        if self.matrix_sha256 is not None and (
            type(self.matrix_sha256) is not str
            or len(self.matrix_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.matrix_sha256
            )
        ):
            raise ValueError("matrix_sha256 must be a lowercase SHA-256 digest")
        if type(self.diagnostics) is not tuple or any(type(item) is not str or not item for item in self.diagnostics):
            raise ValueError("diagnostics must be an immutable string tuple")
        expected_ids = tuple(f"block-{index:06d}" for index in range(len(self.blocks)))
        if tuple(item.block_id for item in self.blocks) != expected_ids:
            raise ValueError("block identifiers must be contiguous and canonical")
        source_order = {segment_id: index for index, segment_id in enumerate(self.source_segment_ids)}
        source_set = set(self.source_segment_ids)
        core_ids = tuple(
            segment_id
            for block in self.blocks
            for segment_id in block.core_segment_ids
        )
        if self.mode is BlockPlanningMode.FULL_WIDTH:
            if core_ids != self.source_segment_ids:
                raise ValueError(
                    "full-width block cores must be an exact ordered segment "
                    "partition"
                )
        elif (
            len(core_ids) != len(set(core_ids))
            or set(core_ids) != source_set
        ):
            # Spatial blocks are ordered inside independent Stage 6 object
            # scopes.  Real document objects may interleave in the global
            # Stage 1 reading order, so flattening object-local cores is not a
            # meaningful global ordering constraint.  The immutable
            # source_segment_ids tuple remains the one canonical page order.
            raise ValueError(
                "spatial block cores must be an exact segment partition"
            )
        width, height = self.aligned_size
        canvas = Box(0, 0, width, height)
        for block in self.blocks:
            if not set(block.segment_ids).issubset(source_set):
                raise ValueError("block contains a forged segment identifier")
            if tuple(source_order[item] for item in block.segment_ids) != tuple(
                sorted(source_order[item] for item in block.segment_ids)
            ):
                raise ValueError("block members must follow canonical source order")
            if tuple(
                source_order[item] for item in block.core_segment_ids
            ) != tuple(
                sorted(source_order[item] for item in block.core_segment_ids)
            ):
                raise ValueError(
                    "block core members must follow canonical source order"
                )
            expected_context = tuple(item for item in block.segment_ids if item not in set(block.core_segment_ids))
            if block.context_segment_ids != expected_context:
                raise ValueError("block context order disagrees with member order")
            if block.bbox.intersection(canvas) != block.bbox:
                raise ValueError("block bbox lies outside the aligned canvas")
            if (
                self.mode is BlockPlanningMode.FULL_WIDTH
                and (block.bbox.left != 0 or block.bbox.right != width)
            ):
                raise ValueError("recognition blocks must retain the full aligned width")
            if (
                self.mode is BlockPlanningMode.FULL_WIDTH
                and not block.core_segment_ids
            ):
                raise ValueError("full-width recognition blocks must own core segments")
            if self.mode is BlockPlanningMode.FULL_WIDTH and block.scope_id is not None:
                raise ValueError("full-width blocks must not claim spatial scopes")
        if not self.blocks and self.source_segment_ids:
            raise ValueError("non-empty source segments require recognition blocks")
        if self.mode is BlockPlanningMode.SPATIAL_2D:
            block_keys: set[tuple[tuple[str, ...], Box]] = set()
            for block in self.blocks:
                if block.scope_id is None:
                    raise ValueError("spatial recognition blocks require a scope ID")
                key = (block.segment_ids, block.bbox)
                if key in block_keys:
                    raise ValueError(
                        "spatial recognition blocks must not duplicate both "
                        "membership and crop geometry"
                    )
                block_keys.add(key)
            self._validate_spatial_membership_units(source_order)
            self._validate_spatial_object_scopes()
            self._validate_spatial_scope_connectivity()
            if self.matrix_sha256 is None:
                raise ValueError("spatial block plans require matrix provenance")
        elif self.membership_units or self.matrix_sha256 is not None:
            raise ValueError(
                "full-width plans must not carry spatial membership provenance"
            )
        if len(self.blocks) <= 1:
            if self.adjacent_algebra:
                raise ValueError("a zero/one-block plan cannot have adjacent algebra")
            return
        block_by_id = {block.block_id: block for block in self.blocks}
        block_order = {
            block.block_id: index for index, block in enumerate(self.blocks)
        }
        if self.mode is BlockPlanningMode.FULL_WIDTH:
            expected_pairs = tuple(
                (first.block_id, second.block_id)
                for first, second in zip(self.blocks, self.blocks[1:])
            )
            actual_pairs = tuple(
                (item.first_block_id, item.second_block_id)
                for item in self.adjacent_algebra
            )
            if actual_pairs != expected_pairs:
                raise ValueError(
                    "every adjacent full-width block pair needs one algebra record"
                )

        algebra_pairs: list[tuple[int, int]] = []
        for algebra in self.adjacent_algebra:
            if (
                algebra.first_block_id not in block_by_id
                or algebra.second_block_id not in block_by_id
            ):
                raise ValueError("block algebra references an unknown block")
            first = block_by_id[algebra.first_block_id]
            second = block_by_id[algebra.second_block_id]
            first_index = block_order[first.block_id]
            second_index = block_order[second.block_id]
            if first_index >= second_index:
                raise ValueError("block algebra pairs must follow canonical block order")
            algebra_pairs.append((first_index, second_index))
            first_set = set(first.segment_ids)
            second_set = set(second.segment_ids)
            expected = {
                "intersection": first_set & second_set,
                "union": first_set | second_set,
                "xor": first_set ^ second_set,
                "first": first_set - second_set,
                "second": second_set - first_set,
            }
            actual = {
                "intersection": set(algebra.intersection_segment_ids),
                "union": set(algebra.union_segment_ids),
                "xor": set(algebra.xor_segment_ids),
                "first": set(algebra.first_only_segment_ids),
                "second": set(algebra.second_only_segment_ids),
            }
            if expected != actual or not expected["intersection"]:
                raise ValueError("adjacent block algebra disagrees with actual members")
            for name, values in (
                ("intersection", algebra.intersection_segment_ids),
                ("union", algebra.union_segment_ids),
                ("xor", algebra.xor_segment_ids),
                ("first", algebra.first_only_segment_ids),
                ("second", algebra.second_only_segment_ids),
            ):
                canonical = tuple(segment_id for segment_id in self.source_segment_ids if segment_id in expected[name])
                if values != canonical:
                    raise ValueError("block algebra tuples must use canonical source order")
        if algebra_pairs != sorted(set(algebra_pairs)):
            raise ValueError("block algebra pairs must be unique and canonical")
        if self.mode is BlockPlanningMode.SPATIAL_2D:
            blocks_by_segment: dict[str, list[int]] = {
                segment_id: [] for segment_id in self.source_segment_ids
            }
            for block_index, block in enumerate(self.blocks):
                for segment_id in block.segment_ids:
                    blocks_by_segment[segment_id].append(block_index)
            expected_overlap_pairs: set[tuple[int, int]] = set()
            overlap_invariant_checks = 0
            for indexes in blocks_by_segment.values():
                for offset, first_index in enumerate(indexes):
                    for second_index in indexes[offset + 1 :]:
                        overlap_invariant_checks += 1
                        if (
                            overlap_invariant_checks
                            > _MAX_BLOCK_PLAN_OVERLAP_INVARIANT_CHECKS
                        ):
                            raise BlockPlanningLimitError(
                                "spatial plan overlap invariant checks exceed "
                                "the static safety limit"
                            )
                        first = self.blocks[first_index]
                        second = self.blocks[second_index]
                        if self._requires_spatial_algebra(first, second):
                            expected_overlap_pairs.add(
                                (first_index, second_index)
                            )
            if algebra_pairs != sorted(expected_overlap_pairs):
                raise ValueError(
                    "spatial algebra must cover every actual overlapping "
                    "orthogonal block pair exactly once"
                )
        # A page may contain several disjoint sparse objects or table regions.
        # Their OCR crops must not be joined by a forged full-page bridge merely
        # to make the overlap graph connected.  Algebra is therefore certified
        # independently for every real overlap edge; isolated one-block
        # components remain valid evidence components.

    def _validate_spatial_object_scopes(self) -> None:
        owner_by_segment: dict[str, str] = {}
        owner_by_scope: dict[str, str] = {}
        scope_by_owner: dict[str, str] = {}
        for block in self.blocks:
            assert block.scope_id is not None
            if not block.core_segment_ids:
                continue
            if len(block.object_ids) != 1:
                raise ValueError(
                    "every spatial core block must belong to exactly one object"
                )
            owner = block.object_ids[0]
            previous_owner = owner_by_scope.setdefault(block.scope_id, owner)
            if previous_owner != owner:
                raise ValueError("one spatial scope cannot cross object owners")
            previous_scope = scope_by_owner.setdefault(owner, block.scope_id)
            if previous_scope != block.scope_id:
                raise ValueError("one Stage 6 object must have exactly one scope")
            for segment_id in block.core_segment_ids:
                if segment_id in owner_by_segment:
                    raise ValueError("spatial core ownership is not a partition")
                owner_by_segment[segment_id] = owner
        if set(owner_by_segment) != set(self.source_segment_ids):
            raise ValueError("spatial core ownership lost a source segment")
        if set(owner_by_scope) != {
            block.scope_id for block in self.blocks if block.scope_id is not None
        }:
            raise ValueError("every spatial scope must own core segments")
        for block in self.blocks:
            assert block.scope_id is not None
            expected_owner = owner_by_scope[block.scope_id]
            if any(
                owner_by_segment[segment_id] != expected_owner
                for segment_id in block.segment_ids
            ):
                raise ValueError("a spatial block cannot cross object owners")
        for unit in self.membership_units:
            owners = {owner_by_segment[value] for value in unit.segment_ids}
            if len(owners) != 1 or next(iter(owners)) != owner_by_scope[unit.scope_id]:
                raise ValueError("a membership unit cannot cross object owners")

    def _validate_spatial_membership_units(
        self,
        source_order: dict[str, int],
    ) -> None:
        expected_ids = tuple(
            f"membership-unit-{index:06d}"
            for index in range(len(self.membership_units))
        )
        if tuple(item.unit_id for item in self.membership_units) != expected_ids:
            raise ValueError("membership unit identifiers must be canonical")
        block_order = {item.block_id: index for index, item in enumerate(self.blocks)}
        block_ids_by_segment: dict[str, list[str]] = {
            segment_id: [] for segment_id in self.source_segment_ids
        }
        scopes_by_segment: dict[str, set[str | None]] = {
            segment_id: set() for segment_id in self.source_segment_ids
        }
        for block in self.blocks:
            for segment_id in block.segment_ids:
                block_ids_by_segment[segment_id].append(block.block_id)
                scopes_by_segment[segment_id].add(block.scope_id)
        expected_by_signature: dict[tuple[str, ...], list[str]] = {}
        scope_by_signature: dict[tuple[str, ...], str] = {}
        for segment_id in self.source_segment_ids:
            signature = tuple(block_ids_by_segment[segment_id])
            if not signature:
                raise ValueError("every spatial segment needs one membership bit")
            segment_scopes = scopes_by_segment[segment_id]
            if len(segment_scopes) != 1 or None in segment_scopes:
                raise ValueError("one segment cannot cross spatial planning scopes")
            expected_by_signature.setdefault(signature, []).append(segment_id)
            scope_by_signature[signature] = next(iter(segment_scopes))  # type: ignore[arg-type]
        expected_groups = tuple(
            (
                tuple(values),
                signature,
                scope_by_signature[signature],
            )
            for signature, values in sorted(
                expected_by_signature.items(),
                key=lambda item: min(source_order[value] for value in item[1]),
            )
        )
        actual_groups = tuple(
            (item.segment_ids, item.block_ids, item.scope_id)
            for item in self.membership_units
        )
        if actual_groups != expected_groups:
            raise ValueError(
                "membership units must be the canonical identical-signature partition"
            )
        for item in self.membership_units:
            if tuple(source_order[value] for value in item.segment_ids) != tuple(
                sorted(source_order[value] for value in item.segment_ids)
            ):
                raise ValueError("membership unit segments must be canonical")
            if tuple(block_order[value] for value in item.block_ids) != tuple(
                sorted(block_order[value] for value in item.block_ids)
            ):
                raise ValueError("membership unit blocks must be canonical")
        signatures = tuple(item.block_ids for item in self.membership_units)
        if len(signatures) != len(set(signatures)):
            raise ValueError("membership unit signatures must be unique")

    def _validate_spatial_scope_connectivity(self) -> None:
        blocks_by_scope: dict[str, list[int]] = {}
        scope_sources: dict[str, set[str]] = {}
        for index, block in enumerate(self.blocks):
            assert block.scope_id is not None
            blocks_by_scope.setdefault(block.scope_id, []).append(index)
            scope_sources.setdefault(block.scope_id, set()).update(
                block.segment_ids
            )
        expected_scopes = tuple(
            f"scope-{index:06d}" for index in range(len(blocks_by_scope))
        )
        if tuple(blocks_by_scope) != expected_scopes:
            raise ValueError("spatial scope identifiers must be canonical")

        parents = list(range(len(self.blocks)))

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

        first_block_by_scope_segment: dict[tuple[str, str], int] = {}
        for index, block in enumerate(self.blocks):
            assert block.scope_id is not None
            for segment_id in block.segment_ids:
                key = (block.scope_id, segment_id)
                first = first_block_by_scope_segment.setdefault(key, index)
                union(first, index)

        for scope_id, indexes in blocks_by_scope.items():
            disconnected = len({find(index) for index in indexes}) != 1
            literal_singletons = all(
                len(self.blocks[index].segment_ids) == 1
                and self.blocks[index].core_segment_ids
                == self.blocks[index].segment_ids
                and not self.blocks[index].context_segment_ids
                for index in indexes
            )
            sparse_matrix_code = all(
                self.blocks[index].matrix_window is not None
                and self.blocks[index].matrix_segment_shape is not None
                for index in indexes
            )
            if disconnected and not literal_singletons and not sparse_matrix_code:
                raise ValueError(
                    f"spatial scope {scope_id} has a disconnected block artifact"
                )
            if len(scope_sources[scope_id]) > 1 and any(
                not self.blocks[index].core_segment_ids
                and len(self.blocks[index].segment_ids) == 1
                for index in indexes
            ):
                raise ValueError("singleton spatial probes are forbidden")

    @staticmethod
    def _requires_spatial_algebra(
        first: RecognitionBlock,
        second: RecognitionBlock,
    ) -> bool:
        """Keep adaptive matrix comparisons horizontal/vertical, never diagonal."""

        if (
            first.matrix_window_kind == "dyadic-mask"
            and second.matrix_window_kind == "dyadic-mask"
        ):
            return first.scope_id == second.scope_id
        if first.matrix_window is None and second.matrix_window is None:
            return True
        if (
            first.matrix_window is None
            or second.matrix_window is None
            or first.scope_id != second.scope_id
            or first.matrix_segment_shape != second.matrix_segment_shape
        ):
            return False
        first_row_start, first_row_stop, first_column_start, first_column_stop = (
            first.matrix_window
        )
        second_row_start, second_row_stop, second_column_start, second_column_stop = (
            second.matrix_window
        )
        same_window_shape = (
            first_row_stop - first_row_start
            == second_row_stop - second_row_start
            and first_column_stop - first_column_start
            == second_column_stop - second_column_start
        )
        horizontal_step = (
            first_row_start == second_row_start
            and first_row_stop == second_row_stop
            and second_column_start == first_column_start + 1
            and second_column_stop == first_column_stop + 1
        )
        vertical_step = (
            first_column_start == second_column_start
            and first_column_stop == second_column_stop
            and second_row_start == first_row_start + 1
            and second_row_stop == first_row_stop + 1
        )
        local_step = (
            first.matrix_window_kind == second.matrix_window_kind
            and same_window_shape
            and (horizontal_step or vertical_step)
        )
        row_column_cross = {
            first.matrix_window_kind,
            second.matrix_window_kind,
        } == {"row-control", "column-control"}
        return local_step or row_column_cross


@dataclass(frozen=True)
class _CoreUnit:
    segment_ids: tuple[str, ...]
    object_ids: tuple[str, ...]


@dataclass(frozen=True)
class _MatrixBand:
    start: int
    stop: int


@dataclass(frozen=True)
class _PlanningScope:
    scope_id: str
    segment_ids: tuple[str, ...]
    ruled: bool


@dataclass(frozen=True)
class _HomogeneousTableRegion:
    segment_shape: tuple[int, int]
    segment_ids: tuple[str, ...]
    row_band_indexes: tuple[int, ...]
    column_band_indexes: tuple[int, ...]
    context_bridge_required: bool = False


class OverlappingBlockPlanner:
    """Pack Stage 6 objects into bounded blocks with real segment overlap."""

    def __init__(self, config: BlockPlanningConfig | None = None) -> None:
        if config is not None and not isinstance(config, BlockPlanningConfig):
            raise TypeError("config must be a BlockPlanningConfig")
        self.config = config or BlockPlanningConfig()

    def plan(
        self,
        *,
        aligned_size: tuple[int, int],
        segments: tuple[Segment, ...],
        objects_result: ObjectReconstructionResult,
        matrix: SparseSegmentMatrix | None = None,
    ) -> BlockPlan:
        if type(aligned_size) is not tuple:
            raise BlockPlanningInvariantError("aligned_size must be an immutable tuple")
        if type(segments) is not tuple or any(not isinstance(item, Segment) for item in segments):
            raise BlockPlanningInvariantError("segments must be an immutable Segment tuple")
        if not isinstance(objects_result, ObjectReconstructionResult):
            raise BlockPlanningInvariantError("objects_result must be an ObjectReconstructionResult")
        if matrix is not None and not isinstance(matrix, SparseSegmentMatrix):
            raise BlockPlanningInvariantError(
                "matrix must be a SparseSegmentMatrix or None"
            )
        if len(segments) > self.config.max_segments:
            raise BlockPlanningLimitError(f"segment count exceeds configured limit {self.config.max_segments}")
        if len(objects_result.objects) > self.config.max_objects:
            raise BlockPlanningLimitError(f"object count exceeds configured limit {self.config.max_objects}")
        segment_by_id, ordered_segments, owner_by_segment = self._validate_inputs(
            aligned_size=aligned_size,
            segments=segments,
            objects=objects_result,
        )
        source_ids = tuple(item.segment_id for item in ordered_segments)
        if (
            self.config.mode is BlockPlanningMode.SPATIAL_2D
            and matrix is None
        ):
            raise BlockPlanningInvariantError(
                "spatial 2D planning requires the Stage 1 sparse matrix"
            )
        if not source_ids:
            return BlockPlan(
                aligned_size,
                (),
                (),
                (),
                mode=self.config.mode,
                matrix_sha256=(
                    sparse_matrix_sha256(matrix)
                    if self.config.mode is BlockPlanningMode.SPATIAL_2D
                    and matrix is not None
                    else None
                ),
            )
        if self.config.mode is BlockPlanningMode.SPATIAL_2D:
            if matrix is None:  # narrowed above, kept explicit for type checkers
                raise BlockPlanningInvariantError(
                    "spatial 2D planning requires the Stage 1 sparse matrix"
                )
            if self.config.object_local:
                return self._plan_object_local(
                    aligned_size=aligned_size,
                    ordered_segments=ordered_segments,
                    segment_by_id=segment_by_id,
                    matrix=matrix,
                    objects_result=objects_result,
                )
            return self._plan_spatial_2d(
                aligned_size=aligned_size,
                ordered_segments=ordered_segments,
                segment_by_id=segment_by_id,
                owner_by_segment=owner_by_segment,
                matrix=matrix,
                objects_result=objects_result,
            )
        units = self._ownership_closure_units(
            ordered_segments=ordered_segments,
            objects_result=objects_result,
            owner_by_segment=owner_by_segment,
            aligned_size=aligned_size,
        )
        cores = self._pack_units(
            units,
            segment_by_id,
            aligned_size=aligned_size,
        )
        if len(cores) > self.config.max_blocks:
            raise BlockPlanningLimitError(f"block count exceeds configured limit {self.config.max_blocks}")
        blocks = self._finish_blocks(
            aligned_size=aligned_size,
            cores=cores,
            segment_by_id=segment_by_id,
        )
        algebra = self._adjacent_algebra(blocks, source_ids)
        return BlockPlan(
            aligned_size=aligned_size,
            source_segment_ids=source_ids,
            blocks=blocks,
            adjacent_algebra=algebra,
            diagnostics=(
                "core-partition=exact",
                "object-ownership=interval-closure",
                "mode=full-width",
                "overlap=left-context-segments",
                "raw-and-gamma-selection=deferred-to-stage2",
            ),
            mode=self.config.mode,
        )

    def _validate_inputs(
        self,
        *,
        aligned_size: tuple[int, int],
        segments: tuple[Segment, ...],
        objects: ObjectReconstructionResult,
    ) -> tuple[dict[str, Segment], tuple[Segment, ...], dict[str, str]]:
        if (
            len(aligned_size) != 2
            or any(type(value) is not int or value < 1 for value in aligned_size)
            or objects.aligned_size != aligned_size
        ):
            raise BlockPlanningInvariantError("aligned canvas and object geometry disagree")
        segment_ids = tuple(item.segment_id for item in segments)
        if len(segment_ids) != len(set(segment_ids)):
            raise BlockPlanningInvariantError("segment identifiers must be unique")
        if set(segment_ids) != set(objects.source_segment_ids):
            raise BlockPlanningInvariantError("segments and object ownership disagree")
        segment_by_id = {item.segment_id: item for item in segments}
        ordered = tuple(segment_by_id[item] for item in objects.source_segment_ids)
        width, height = aligned_size
        canvas = Box(0, 0, width, height)
        if any(item.bbox.intersection(canvas) != item.bbox for item in ordered):
            raise BlockPlanningInvariantError("a segment lies outside the aligned canvas")
        owner_by_segment: dict[str, str] = {}
        for document_object in objects.objects:
            member_segments = tuple(segment_by_id[item] for item in document_object.segment_ids)
            if Box.union(item.bbox for item in member_segments) != document_object.bbox:
                raise BlockPlanningInvariantError(
                    f"object {document_object.object_id} bbox disagrees with its segments"
                )
            for segment in member_segments:
                if segment.segment_id in owner_by_segment:
                    raise BlockPlanningInvariantError("a segment has multiple object owners")
                owner_by_segment[segment.segment_id] = document_object.object_id
        if set(owner_by_segment) != set(segment_ids):
            raise BlockPlanningInvariantError("object ownership is not an exact segment partition")
        return segment_by_id, ordered, owner_by_segment

    def _plan_spatial_2d(
        self,
        *,
        aligned_size: tuple[int, int],
        ordered_segments: tuple[Segment, ...],
        segment_by_id: dict[str, Segment],
        owner_by_segment: dict[str, str],
        matrix: SparseSegmentMatrix,
        objects_result: ObjectReconstructionResult,
    ) -> BlockPlan:
        """Build bounded row/cut block families from the Stage 1 matrix.

        Raw recursive axis intervals are deliberately not treated as rows or
        columns.  Ruled matrices are collapsed into occupied areas between
        contiguous rule runs; unruled areas are collapsed by equal cell
        incidence.  Column-prefix probes are scoped to maximal consecutive
        row regions with the same safe vertical cuts.  A wide header or a
        merged span crossing a cut therefore becomes a row barrier instead of
        widening a full-page column probe.
        """

        source_ids = tuple(item.segment_id for item in ordered_segments)
        if matrix.segment_ids() != frozenset(source_ids):
            raise BlockPlanningInvariantError(
                "Stage 1 sparse matrix and Stage 6 segment scope disagree"
            )
        logical_row_budget = _WorkBudget(
            self.config.max_logical_row_checks,
            "logical row analysis",
        )
        column_region_budget = _WorkBudget(
            self.config.max_column_region_checks,
            "column region analysis",
        )
        padding_exclusion_budget = _WorkBudget(
            self.config.max_padding_exclusion_checks,
            "padding exclusion analysis",
        )
        span_by_id = {item.segment_id: item for item in matrix.spans}
        mutable_cells_by_segment: dict[str, list[SparseCell]] = {}
        for cell in matrix.cells:
            mutable_cells_by_segment.setdefault(cell.segment_id, []).append(cell)
        cells_by_segment = {
            segment_id: tuple(cells)
            for segment_id, cells in mutable_cells_by_segment.items()
        }
        scopes = self._planning_scopes(objects_result=objects_result)
        scoped_layouts: list[tuple[_PlanningScope, tuple[_MatrixBand, ...]]] = []
        for scope in scopes:
            row_bands = self._logical_row_bands(
                matrix=matrix,
                source_ids=scope.segment_ids,
                span_by_id=span_by_id,
                cells_by_segment=cells_by_segment,
                ruled=scope.ruled,
                work_budget=logical_row_budget,
            )
            if row_bands:
                scoped_layouts.append((scope, row_bands))
        if not scoped_layouts:
            raise BlockPlanningInvariantError(
                "non-empty sparse geometry produced no occupied matrix region"
            )

        primary_members: list[tuple[str, ...]] = []
        primary_scope_ids: list[str] = []
        primary_bboxes: list[Box | None] = []
        primary_windows: list[tuple[int, int, int, int] | None] = []
        primary_window_kinds: list[str | None] = []
        primary_segment_shapes: list[tuple[int, int] | None] = []
        probe_members: list[tuple[str, ...]] = []
        probe_scope_ids: list[str] = []
        probe_bboxes: list[Box | None] = []
        probe_windows: list[tuple[int, int, int, int] | None] = []
        probe_window_kinds: list[str | None] = []
        probe_segment_shapes: list[tuple[int, int] | None] = []
        matrix_cells_by_row: dict[int, tuple[SparseCell, ...]] = {}
        mutable_cells_by_row: dict[int, list[SparseCell]] = {}
        for cell in matrix.cells:
            mutable_cells_by_row.setdefault(cell.row, []).append(cell)
        matrix_cells_by_row = {
            row: tuple(cells) for row, cells in mutable_cells_by_row.items()
        }
        seen_members: set[tuple[str, ...]] = set()
        identical_coalesced = 0
        candidate_count = 0
        candidate_checks = 0
        collision_groups: list[tuple[str, ...]] | None = None

        def append_family(
            target: list[tuple[str, ...]],
            *,
            row_start: int,
            row_stop: int,
            column_start: int,
            column_stop: int,
            scope: _PlanningScope,
            require_split: bool = False,
            exact_matrix_crop: bool = False,
            logical_window: tuple[int, int, int, int] | None = None,
            window_kind: str | None = None,
            segment_shape: tuple[int, int] | None = None,
            allowed_segment_ids: tuple[str, ...] | None = None,
        ) -> None:
            nonlocal identical_coalesced
            nonlocal candidate_count, candidate_checks, collision_groups
            candidate_count += 1
            if candidate_count > self.config.max_signature_candidates:
                raise BlockPlanningLimitError(
                    "matrix block candidates exceed configured limit "
                    f"{self.config.max_signature_candidates}"
                )
            row_slots = row_stop - row_start
            if (
                candidate_checks + row_slots
                > self.config.max_signature_candidate_checks
            ):
                raise BlockPlanningLimitError(
                    "matrix candidate row-slot checks exceed configured limit "
                    f"{self.config.max_signature_candidate_checks}"
                )
            candidate_ids = allowed_segment_ids or scope.segment_ids
            candidate_set = frozenset(candidate_ids)
            candidate_segments = tuple(
                segment_by_id[item] for item in candidate_ids
            )
            members, rectangle_checks = self._matrix_rectangle_members(
                matrix=matrix,
                row_start=row_start,
                row_stop=row_stop,
                column_start=column_start,
                column_stop=column_stop,
                ordered_segments=candidate_segments,
                scope_ids=candidate_ids,
                scope_set=candidate_set,
                cells_by_row=matrix_cells_by_row,
                max_closure_checks=(
                    self.config.max_signature_candidate_checks
                    - candidate_checks
                ),
                visible_closure=not exact_matrix_crop,
            )
            candidate_checks += rectangle_checks
            if not members:
                return
            if len(members) == 1 and len(candidate_ids) > 1:
                # OCR needs context.  A singleton candidate is not permitted
                # to manufacture segment-level uniqueness; the unresolved
                # signature remains an explicit subblock instead.
                return
            explicit_bbox = (
                self._matrix_rectangle_bbox(
                    matrix=matrix,
                    aligned_size=aligned_size,
                    scope=_PlanningScope(
                        scope.scope_id,
                        candidate_ids,
                        scope.ruled,
                    ),
                    span_by_id=span_by_id,
                    segment_by_id=segment_by_id,
                    member_ids=members,
                    row_start=row_start,
                    row_stop=row_stop,
                    column_start=column_start,
                    column_stop=column_stop,
                )
                if exact_matrix_crop
                else None
            )
            explicit_window = (
                logical_window
                if exact_matrix_crop
                else None
            )
            if exact_matrix_crop and explicit_window is None:
                raise BlockPlanningInvariantError(
                    "an exact adaptive crop requires its logical matrix window"
                )
            if exact_matrix_crop and (
                window_kind is None or segment_shape is None
            ):
                raise BlockPlanningInvariantError(
                    "an adaptive crop requires its kind and segment shape"
                )
            duplicate = members in seen_members and not (
                self.config.adaptive_table_windows
                and explicit_bbox is not None
                and all(
                    members != previous_members
                    or explicit_bbox != previous_bbox
                    for previous_members, previous_bbox in zip(
                        (*primary_members, *probe_members),
                        (*primary_bboxes, *probe_bboxes),
                    )
                )
            )
            if duplicate:
                identical_coalesced += 1
                return
            refined_collisions: list[tuple[str, ...]] | None = None
            if require_split:
                if collision_groups is None:
                    raise BlockPlanningInvariantError(
                        "signature collision state was not initialized"
                    )
                member_set = set(members)
                split_checks = sum(len(group) for group in collision_groups)
                if (
                    candidate_checks + split_checks
                    > self.config.max_signature_candidate_checks
                ):
                    raise BlockPlanningLimitError(
                        "matrix signature split checks exceed configured limit"
                    )
                candidate_checks += split_checks
                refined_collisions = []
                split = False
                for group in collision_groups:
                    inside = tuple(item for item in group if item in member_set)
                    outside = tuple(item for item in group if item not in member_set)
                    split = split or bool(inside and outside)
                    if len(inside) > 1:
                        refined_collisions.append(inside)
                    if len(outside) > 1:
                        refined_collisions.append(outside)
                if not split:
                    return
            if len(members) > self.config.max_block_segments:
                raise BlockPlanningLimitError(
                    "matrix block membership exceeds configured limit "
                    f"{self.config.max_block_segments}"
                )
            if (
                not self.config.adaptive_table_windows
                and len(primary_members) + len(probe_members) + 1
                > self.config.max_blocks
            ):
                raise BlockPlanningLimitError(
                    "matrix block count exceeds configured limit "
                    f"{self.config.max_blocks}"
                )
            seen_members.add(members)
            target.append(members)
            if target is primary_members:
                primary_scope_ids.append(scope.scope_id)
                primary_bboxes.append(explicit_bbox)
                primary_windows.append(explicit_window)
                primary_window_kinds.append(window_kind)
                primary_segment_shapes.append(segment_shape)
            else:
                probe_scope_ids.append(scope.scope_id)
                probe_bboxes.append(explicit_bbox)
                probe_windows.append(explicit_window)
                probe_window_kinds.append(window_kind)
                probe_segment_shapes.append(segment_shape)
            if require_split:
                assert refined_collisions is not None
                collision_groups = refined_collisions

        def append_dyadic_mask(
            *,
            members: tuple[str, ...],
            scope: _PlanningScope,
        ) -> None:
            nonlocal candidate_count, identical_coalesced
            candidate_count += 1
            if candidate_count > self.config.max_signature_candidates:
                raise BlockPlanningLimitError(
                    "matrix block candidates exceed configured limit "
                    f"{self.config.max_signature_candidates}"
                )
            if len(members) > self.config.max_block_segments:
                raise BlockPlanningLimitError(
                    "matrix block membership exceeds configured limit "
                    f"{self.config.max_block_segments}"
                )
            if members in seen_members:
                identical_coalesced += 1
                return
            seen_members.add(members)
            primary_members.append(members)
            primary_scope_ids.append(scope.scope_id)
            primary_bboxes.append(
                Box.union(
                    segment_by_id[segment_id].bbox
                    for segment_id in members
                )
            )
            dense_column_count = min(
                16,
                max(1, math.ceil(math.sqrt(len(members)))),
            )
            dense_row_count = math.ceil(
                len(members) / dense_column_count
            )
            if dense_row_count > 16:
                raise BlockPlanningInvariantError(
                    "dyadic OCR mask exceeds the 16x16 occupied-cell limit"
                )
            primary_windows.append(
                (0, dense_row_count, 0, dense_column_count)
            )
            primary_window_kinds.append("dyadic-mask")
            primary_segment_shapes.append((1, 1))

        row_probe_rectangles: list[
            tuple[_PlanningScope, int, int, int, int]
        ] = []
        adaptive_window_shapes: list[tuple[str, int, int, int, int]] = []
        adaptive_logical_column_count = 0
        adaptive_small_table_fallbacks = 0
        adaptive_homogeneous_regions = 0
        adaptive_context_bridges = 0
        adaptive_dyadic_codes: list[tuple[str, int, int, int]] = []
        adaptive_dyadic_masks = False
        for scope, row_bands in scoped_layouts:
            column_start = min(
                span_by_id[item].column_start for item in scope.segment_ids
            )
            column_stop = max(
                span_by_id[item].column_stop for item in scope.segment_ids
            )
            if self.config.adaptive_table_windows and not scope.ruled:
                # A paragraph/list is already a semantic Stage 6 object.  OCR
                # needs its complete language and line context, so it is one
                # block instead of an artificial sequence of row pairs.
                append_family(
                    primary_members,
                    row_start=row_bands[0].start,
                    row_stop=row_bands[-1].stop,
                    column_start=column_start,
                    column_stop=column_stop,
                    scope=scope,
                )
            elif self.config.adaptive_table_windows:
                column_bands = self._logical_table_column_bands(
                    matrix=matrix,
                    scope=scope,
                    span_by_id=span_by_id,
                    cells_by_segment=cells_by_segment,
                    work_budget=column_region_budget,
                )
                if not column_bands:
                    raise BlockPlanningInvariantError(
                        f"table scope {scope.scope_id} has no logical columns"
                    )
                adaptive_logical_column_count += len(column_bands)
                (
                    dyadic_masks,
                    dyadic_unit_count,
                    dyadic_bit_count,
                    dyadic_context_width,
                ) = self._dyadic_table_masks(
                    scope=scope,
                    span_by_id=span_by_id,
                    segment_by_id=segment_by_id,
                )
                adaptive_dyadic_masks = True
                adaptive_dyadic_codes.append(
                    (
                        scope.scope_id,
                        dyadic_unit_count,
                        dyadic_bit_count,
                        dyadic_context_width,
                    )
                )
                for members in dyadic_masks:
                    append_dyadic_mask(
                        members=members,
                        scope=scope,
                    )
                continue
                regions = self._homogeneous_table_regions(
                    scope=scope,
                    row_bands=row_bands,
                    column_bands=column_bands,
                    span_by_id=span_by_id,
                    cells_by_segment=cells_by_segment,
                    work_budget=column_region_budget,
                )
                adaptive_homogeneous_regions += len(regions)
                for region_index, region in enumerate(regions):
                    region_rows = tuple(
                        row_bands[index] for index in region.row_band_indexes
                    )
                    region_columns = tuple(
                        column_bands[index]
                        for index in region.column_band_indexes
                    )
                    if len(region.segment_ids) == 1:
                        if region.segment_shape == (1, 1):
                            raise BlockPlanningInvariantError(
                                "an isolated 1x1 table segment has no OCR "
                                "context and cannot form a recognition block"
                            )
                        append_family(
                            primary_members,
                            row_start=region_rows[0].start,
                            row_stop=region_rows[-1].stop,
                            column_start=region_columns[0].start,
                            column_stop=region_columns[-1].stop,
                            scope=scope,
                            exact_matrix_crop=True,
                            logical_window=(
                                region.row_band_indexes[0],
                                region.row_band_indexes[-1] + 1,
                                region.column_band_indexes[0],
                                region.column_band_indexes[-1] + 1,
                            ),
                            window_kind="structural-singleton",
                            segment_shape=region.segment_shape,
                            allowed_segment_ids=region.segment_ids,
                        )
                        continue
                    if region.context_bridge_required:
                        append_family(
                            primary_members,
                            row_start=region_rows[0].start,
                            row_stop=region_rows[-1].stop,
                            column_start=region_columns[0].start,
                            column_stop=region_columns[-1].stop,
                            scope=scope,
                            exact_matrix_crop=True,
                            logical_window=(
                                region.row_band_indexes[0],
                                region.row_band_indexes[-1] + 1,
                                region.column_band_indexes[0],
                                region.column_band_indexes[-1] + 1,
                            ),
                            window_kind="context-bridge",
                            segment_shape=region.segment_shape,
                            allowed_segment_ids=region.segment_ids,
                        )
                        adaptive_context_bridges += 1
                    row_window, column_window = (
                        self._adaptive_table_window_shape(
                            row_count=len(region_rows),
                            column_count=len(region_columns),
                        )
                    )
                    adaptive_window_shapes.append(
                        (
                            f"{scope.scope_id}/region-{region_index:06d}",
                            len(region_rows),
                            len(region_columns),
                            row_window,
                            column_window,
                        )
                    )
                    window_families = ((row_window, column_window),)
                    if (
                        row_window == len(region_rows)
                        and column_window == len(region_columns)
                        and len(region_rows) > 1
                        and len(region_columns) > 1
                    ):
                        window_families = (
                            (len(region_rows) - 1, len(region_columns)),
                            (len(region_rows), len(region_columns) - 1),
                        )
                        adaptive_small_table_fallbacks += 1
                    for family_rows, family_columns in window_families:
                        for row_offset in range(
                            len(region_rows) - family_rows + 1
                        ):
                            for column_offset in range(
                                len(region_columns) - family_columns + 1
                            ):
                                row_index = region.row_band_indexes[row_offset]
                                row_stop_index = region.row_band_indexes[
                                    row_offset + family_rows - 1
                                ]
                                column_index = region.column_band_indexes[
                                    column_offset
                                ]
                                column_stop_index = region.column_band_indexes[
                                    column_offset + family_columns - 1
                                ]
                                append_family(
                                    primary_members,
                                    row_start=row_bands[row_index].start,
                                    row_stop=row_bands[row_stop_index].stop,
                                    column_start=column_bands[
                                        column_index
                                    ].start,
                                    column_stop=column_bands[
                                        column_stop_index
                                    ].stop,
                                    scope=scope,
                                    exact_matrix_crop=True,
                                    logical_window=(
                                        row_index,
                                        row_stop_index + 1,
                                        column_index,
                                        column_stop_index + 1,
                                    ),
                                    window_kind="local",
                                    segment_shape=region.segment_shape,
                                    allowed_segment_ids=region.segment_ids,
                                )
                    # Orthogonal controls are candidates, not mandatory OCR
                    # work.  The sparse selector keeps only those needed to
                    # distinguish memberships more cheaply than local windows.
                    for row_index in region.row_band_indexes:
                        append_family(
                            primary_members,
                            row_start=row_bands[row_index].start,
                            row_stop=row_bands[row_index].stop,
                            column_start=region_columns[0].start,
                            column_stop=region_columns[-1].stop,
                            scope=scope,
                            exact_matrix_crop=True,
                            logical_window=(
                                row_index,
                                row_index + 1,
                                region.column_band_indexes[0],
                                region.column_band_indexes[-1] + 1,
                            ),
                            window_kind="row-control",
                            segment_shape=region.segment_shape,
                            allowed_segment_ids=region.segment_ids,
                        )
                    for column_index in region.column_band_indexes:
                        append_family(
                            primary_members,
                            row_start=region_rows[0].start,
                            row_stop=region_rows[-1].stop,
                            column_start=column_bands[column_index].start,
                            column_stop=column_bands[column_index].stop,
                            scope=scope,
                            exact_matrix_crop=True,
                            logical_window=(
                                region.row_band_indexes[0],
                                region.row_band_indexes[-1] + 1,
                                column_index,
                                column_index + 1,
                            ),
                            window_kind="column-control",
                            segment_shape=region.segment_shape,
                            allowed_segment_ids=region.segment_ids,
                        )
            elif len(row_bands) == 1:
                append_family(
                    primary_members,
                    row_start=row_bands[0].start,
                    row_stop=row_bands[0].stop,
                    column_start=column_start,
                    column_stop=column_stop,
                    scope=scope,
                )
            else:
                for first, second in zip(row_bands, row_bands[1:]):
                    append_family(
                        primary_members,
                        row_start=first.start,
                        row_stop=second.stop,
                        column_start=column_start,
                        column_stop=column_stop,
                        scope=scope,
                    )
                if len(row_bands) == 2:
                    # One adjacent pair alone gives both rows the same bit.
                    # The first-row prefix is the minimal orthogonal row code.
                    row_probe_rectangles.append(
                        (
                            scope,
                            row_bands[0].start,
                            row_bands[0].stop,
                            column_start,
                            column_stop,
                        )
                    )

        adaptive_generated_candidates = sum(
            item is not None for item in primary_windows
        )
        adaptive_selected_candidates = adaptive_generated_candidates
        adaptive_intrinsic_units = 0
        adaptive_max_memberships = 0
        adaptive_mean_memberships = 0.0
        minimize_dyadic_masks = (
            adaptive_dyadic_masks
            and adaptive_dyadic_codes
            and all(item[1] <= 16 for item in adaptive_dyadic_codes)
        )
        if self.config.adaptive_table_windows and (
            not adaptive_dyadic_masks or minimize_dyadic_masks
        ):
            selected_indexes, sparse_statistics = (
                self._sparse_identifying_candidate_indexes(
                    members=tuple(primary_members),
                    scope_ids=tuple(primary_scope_ids),
                    windows=tuple(primary_windows),
                    source_ids=source_ids,
                )
            )
            primary_members = [primary_members[index] for index in selected_indexes]
            primary_scope_ids = [
                primary_scope_ids[index] for index in selected_indexes
            ]
            primary_bboxes = [primary_bboxes[index] for index in selected_indexes]
            primary_windows = [primary_windows[index] for index in selected_indexes]
            primary_window_kinds = [
                primary_window_kinds[index] for index in selected_indexes
            ]
            primary_segment_shapes = [
                primary_segment_shapes[index] for index in selected_indexes
            ]
            adaptive_selected_candidates = int(
                sparse_statistics["selected_candidates"]
            )
            adaptive_intrinsic_units = int(
                sparse_statistics["intrinsic_units"]
            )
            adaptive_max_memberships = int(
                sparse_statistics["max_memberships"]
            )
            adaptive_mean_memberships = float(
                sparse_statistics["mean_memberships"]
            )
        elif self.config.adaptive_table_windows:
            adaptive_selected_candidates = adaptive_generated_candidates
            membership_indexes = {
                segment_id: tuple(
                    index
                    for index, members in enumerate(primary_members)
                    if segment_id in members
                )
                for segment_id in source_ids
            }
            adaptive_intrinsic_units = len(
                set(membership_indexes.values())
            )
            selected_counts = tuple(
                len(indexes)
                for indexes in membership_indexes.values()
                if indexes
            )
            adaptive_max_memberships = max(selected_counts, default=0)
            adaptive_mean_memberships = (
                sum(selected_counts) / len(selected_counts)
                if selected_counts
                else 0.0
            )

        primary_membership_count = sum(len(item) for item in primary_members)
        if primary_membership_count > self.config.max_total_block_memberships:
            raise BlockPlanningLimitError(
                "primary matrix memberships exceed configured aggregate limit"
            )
        primary_indexes: dict[str, list[int]] = {
            segment_id: [] for segment_id in source_ids
        }
        for index, members in enumerate(primary_members):
            for segment_id in members:
                primary_indexes[segment_id].append(index)
        primary_signatures: dict[tuple[int, ...], list[str]] = {}
        for segment_id in source_ids:
            signature = tuple(primary_indexes[segment_id])
            primary_signatures.setdefault(signature, []).append(segment_id)
        collision_groups = [
            tuple(group)
            for group in primary_signatures.values()
            if len(group) > 1
        ]
        row_prefix_probe_count = 0
        for scope, row_start, row_stop, column_start, column_stop in row_probe_rectangles:
            before = len(probe_members)
            append_family(
                probe_members,
                row_start=row_start,
                row_stop=row_stop,
                column_start=column_start,
                column_stop=column_stop,
                scope=scope,
                require_split=True,
            )
            row_prefix_probe_count += int(len(probe_members) > before)

        column_layouts = (
            ()
            if self.config.adaptive_table_windows
            else tuple(
                (
                    scope,
                    *self._column_probe_rectangles(
                        matrix=matrix,
                        scope=scope,
                        row_bands=row_bands,
                        span_by_id=span_by_id,
                        cells_by_segment=cells_by_segment,
                        work_budget=column_region_budget,
                    ),
                )
                for scope, row_bands in scoped_layouts
            )
        )
        logical_column_band_count = sum(item[2] for item in column_layouts)
        regional_column_probe_count = 0
        for scope, rectangles, _logical_columns in column_layouts:
            if not collision_groups:
                break
            for row_start, row_stop, column_start, column_stop in rectangles:
                if not collision_groups:
                    break
                before = len(probe_members)
                append_family(
                    probe_members,
                    row_start=row_start,
                    row_stop=row_stop,
                    column_start=column_start,
                    column_stop=column_stop,
                    scope=scope,
                    require_split=True,
                )
                regional_column_probe_count += int(len(probe_members) > before)

        if not primary_members:
            raise BlockPlanningInvariantError(
                "matrix regions produced no primary OCR context blocks"
            )
        all_members = (*primary_members, *probe_members)
        all_scope_ids = (*primary_scope_ids, *probe_scope_ids)
        all_bboxes = (*primary_bboxes, *probe_bboxes)
        all_windows = (*primary_windows, *probe_windows)
        all_window_kinds = (*primary_window_kinds, *probe_window_kinds)
        all_segment_shapes = (*primary_segment_shapes, *probe_segment_shapes)
        if len(all_members) > self.config.max_blocks:
            raise BlockPlanningLimitError(
                "selected matrix block count exceeds configured limit "
                f"{self.config.max_blocks}"
            )
        total_block_memberships = sum(len(item) for item in all_members)
        if total_block_memberships > self.config.max_total_block_memberships:
            raise BlockPlanningLimitError(
                "matrix block memberships exceed configured aggregate limit "
                f"{self.config.max_total_block_memberships}"
            )
        membership_indexes: dict[str, list[int]] = {
            segment_id: [] for segment_id in source_ids
        }
        for index, members in enumerate(all_members):
            for segment_id in members:
                membership_indexes[segment_id].append(index)
        signatures = {
            segment_id: tuple(membership_indexes[segment_id])
            for segment_id in source_ids
        }
        duplicate_groups: dict[tuple[int, ...], list[str]] = {}
        for segment_id, signature in signatures.items():
            duplicate_groups.setdefault(signature, []).append(segment_id)
        collisions = tuple(
            tuple(values)
            for values in duplicate_groups.values()
            if len(values) > 1
        )

        carrier_by_segment: dict[str, int] = {}
        for segment_id in source_ids:
            candidates = tuple(primary_indexes[segment_id])
            if not candidates:
                raise BlockPlanningInvariantError(
                    f"segment {segment_id} has no primary matrix block"
                )
            carrier = max(candidates)
            carrier_by_segment[segment_id] = carrier

        blocks: list[RecognitionBlock] = []
        for family_index, (
            members,
            scope_id,
            explicit_bbox,
            explicit_window,
            window_kind,
            segment_shape,
        ) in enumerate(
            zip(
                all_members,
                all_scope_ids,
                all_bboxes,
                all_windows,
                all_window_kinds,
                all_segment_shapes,
            )
        ):
            core_ids = (
                tuple(
                    segment_id
                    for segment_id in source_ids
                    if carrier_by_segment.get(segment_id) == family_index
                )
                if family_index < len(primary_members)
                else ()
            )
            member_set = set(members)
            if explicit_bbox is None:
                member_segments = tuple(segment_by_id[item] for item in members)
                padding_exclusion_budget.consume(len(ordered_segments))
                excluded_segments = tuple(
                    item
                    for item in ordered_segments
                    if item.segment_id not in member_set
                )
                bbox = self._spatial_padded_bbox(
                    member_segments,
                    excluded_segments=excluded_segments,
                    aligned_size=aligned_size,
                    work_budget=padding_exclusion_budget,
                )
            else:
                member_bbox = Box.union(
                    tuple(segment_by_id[member_id].bbox for member_id in members)
                )
                bbox = Box.union((explicit_bbox, member_bbox))
            if bbox.area > self.config.max_block_pixels:
                raise BlockPlanningLimitError(
                    "matrix block pixel footprint exceeds configured limit "
                    f"{self.config.max_block_pixels}; safe splitting is only "
                    "allowed at a matrix row/column boundary"
                )
            core_set = set(core_ids)
            blocks.append(
                RecognitionBlock(
                    block_id=f"block-{len(blocks):06d}",
                    bbox=bbox,
                    core_segment_ids=core_ids,
                    segment_ids=members,
                    context_segment_ids=tuple(
                        item for item in members if item not in core_set
                    ),
                    object_ids=tuple(
                        dict.fromkeys(owner_by_segment[item] for item in core_ids)
                    ),
                    scope_id=scope_id,
                    matrix_window=explicit_window,
                    matrix_window_kind=window_kind,
                    matrix_segment_shape=segment_shape,
                )
            )
        block_tuple = tuple(blocks)
        algebra = self._spatial_algebra(block_tuple, source_ids)
        component_count = self._overlap_component_count(block_tuple)
        membership_units = self._membership_units(
            blocks=block_tuple,
            source_ids=source_ids,
        )
        return BlockPlan(
            aligned_size=aligned_size,
            source_segment_ids=source_ids,
            blocks=block_tuple,
            adjacent_algebra=algebra,
            diagnostics=(
                "core-partition=exact",
                "core-order=canonical-within-object-scopes",
                "object-ownership=core-derived",
                "mode=spatial-2d",
                f"matrix-raw-rows={len(matrix.rows)}",
                f"matrix-raw-columns={len(matrix.columns)}",
                "matrix-planning-scopes="
                f"{len(scoped_layouts)}",
                "matrix-logical-row-bands="
                f"{sum(len(item[1]) for item in scoped_layouts)}",
                "matrix-logical-column-bands="
                f"{logical_column_band_count + adaptive_logical_column_count}",
                "matrix-table-window-mode="
                + (
                    "dyadic-axis-binary-code"
                    if self.config.adaptive_table_windows
                    else "legacy-row-pairs"
                ),
                "matrix-table-algebra="
                + (
                    "arbitrary-segment-set-and-xor"
                    if self.config.adaptive_table_windows
                    else "all-overlapping-pairs"
                ),
                "matrix-table-dyadic-codes="
                + (
                    ",".join(
                        f"{scope}:units={units},bits={bits},"
                        f"context-width={context_width}"
                        for scope, units, bits, context_width
                        in adaptive_dyadic_codes
                    )
                    if adaptive_dyadic_codes
                    else "none"
                ),
                "matrix-table-window-shapes="
                + (
                    ",".join(
                        f"{scope}:{rows}x{columns}->{window_rows}x{window_columns}"
                        for (
                            scope,
                            rows,
                            columns,
                            window_rows,
                            window_columns,
                        ) in adaptive_window_shapes
                    )
                    if adaptive_window_shapes
                    else "none"
                ),
                "matrix-table-homogeneous-regions="
                f"{adaptive_homogeneous_regions}",
                "matrix-table-generated-candidates="
                f"{adaptive_generated_candidates}",
                "matrix-table-selected-candidates="
                f"{adaptive_selected_candidates}",
                "matrix-table-intrinsic-membership-units="
                f"{adaptive_intrinsic_units}",
                "matrix-table-selected-max-memberships="
                f"{adaptive_max_memberships}",
                "matrix-table-selected-mean-memberships="
                f"{adaptive_mean_memberships:.6f}",
                "matrix-table-small-fallbacks="
                f"{adaptive_small_table_fallbacks}",
                "matrix-table-context-bridges="
                f"{adaptive_context_bridges}",
                "matrix-safe-column-regions="
                f"{regional_column_probe_count}",
                f"matrix-primary-row-families={len(primary_members)}",
                f"matrix-row-prefix-families={row_prefix_probe_count}",
                "matrix-column-sliding2-or-orthogonal-families="
                f"{regional_column_probe_count}",
                f"overlap-components={component_count}",
                "membership="
                + (
                    "exact-homogeneous-matrix-window"
                    if self.config.adaptive_table_windows
                    else "matrix-rectangle-plus-visible-closure"
                ),
                "padding=non-cascading-clamped",
                "identical-blocks-coalesced="
                f"{identical_coalesced}",
                f"matrix-candidates={candidate_count}",
                f"matrix-candidate-checks={candidate_checks}",
                f"logical-row-checks={logical_row_budget.checks}",
                f"column-region-checks={column_region_budget.checks}",
                "padding-exclusion-checks="
                f"{padding_exclusion_budget.checks}",
                f"total-block-memberships={total_block_memberships}",
                f"orthogonal-signature-probes={len(probe_members)}",
                "singleton-signature-probes=0",
                f"membership-segments={sum(item.kind is MembershipUnitKind.SEGMENT for item in membership_units)}",
                f"membership-subblocks={sum(item.kind is MembershipUnitKind.SUBBLOCK for item in membership_units)}",
                f"membership-collisions-preserved={len(collisions)}",
                "membership-signatures=unique-between-units",
                "deprecated-window-knobs=ignored",
                "raw-and-gamma-selection=deferred-to-stage2",
            ),
            mode=self.config.mode,
            membership_units=membership_units,
            matrix_sha256=sparse_matrix_sha256(matrix),
        )

    def _plan_object_local(
        self,
        *,
        aligned_size: tuple[int, int],
        ordered_segments: tuple[Segment, ...],
        segment_by_id: dict[str, Segment],
        matrix: SparseSegmentMatrix,
        objects_result: ObjectReconstructionResult,
    ) -> BlockPlan:
        """Build OCR blocks at the semantic resolution of Stage 6 objects.

        A flow object is one paragraph/list block containing all of its source
        segments.  A table-like object is represented by one literal block per
        Stage 1 segment.  Stage 2 may therefore recognize a paragraph as one
        typed segment group, while table observations never have to decode one
        cell/row segment from a wider overlapping strip.

        The v16 projection can conservatively label a narrow table as a
        paragraph.  Repeated merge-left witnesses are matrix evidence of a
        table even when Stage 6 did not promote that object to ``TABLE``.
        """

        source_ids = tuple(item.segment_id for item in ordered_segments)
        if matrix.segment_ids() != frozenset(source_ids):
            raise BlockPlanningInvariantError(
                "Stage 1 sparse matrix and Stage 6 segment scope disagree"
            )
        padding_budget = _WorkBudget(
            self.config.max_padding_exclusion_checks,
            "object-local padding exclusion analysis",
        )
        object_by_id = {
            item.object_id: item for item in objects_result.objects
        }
        scope_by_object = {
            item.object_id: f"scope-{index:06d}"
            for index, item in enumerate(objects_result.objects)
        }

        table_objects: set[str] = set()
        inferred_tables: set[str] = set()
        groups: list[tuple[str, tuple[str, ...]]] = []
        for document_object in objects_result.objects:
            table_like = self._object_is_table_like(
                document_object=document_object,
                matrix=matrix,
            )
            if table_like:
                table_objects.add(document_object.object_id)
                if document_object.kind is not ObjectKind.TABLE:
                    inferred_tables.add(document_object.object_id)
                groups.extend(
                    (document_object.object_id, (segment_id,))
                    for segment_id in document_object.segment_ids
                )
            else:
                groups.append(
                    (document_object.object_id, document_object.segment_ids)
                )

        if len(groups) > self.config.max_blocks:
            raise BlockPlanningLimitError(
                "object-local block count exceeds configured limit "
                f"{self.config.max_blocks}"
            )
        total_memberships = sum(len(segment_ids) for _, segment_ids in groups)
        if total_memberships > self.config.max_total_block_memberships:
            raise BlockPlanningLimitError(
                "object-local block memberships exceed configured aggregate "
                f"limit {self.config.max_total_block_memberships}"
            )

        blocks: list[RecognitionBlock] = []
        for object_id, segment_ids in groups:
            if len(segment_ids) > self.config.max_block_segments:
                raise BlockPlanningLimitError(
                    "one object-local block exceeds configured segment limit "
                    f"{self.config.max_block_segments}"
                )
            member_set = set(segment_ids)
            member_segments = tuple(
                segment_by_id[segment_id] for segment_id in segment_ids
            )
            padding_budget.consume(len(ordered_segments))
            bbox = self._spatial_padded_bbox(
                member_segments,
                excluded_segments=tuple(
                    segment
                    for segment in ordered_segments
                    if segment.segment_id not in member_set
                ),
                aligned_size=aligned_size,
                work_budget=padding_budget,
            )
            if bbox.area > self.config.max_block_pixels:
                raise BlockPlanningLimitError(
                    "object-local block pixel footprint exceeds configured "
                    f"limit {self.config.max_block_pixels}"
                )
            blocks.append(
                RecognitionBlock(
                    block_id=f"block-{len(blocks):06d}",
                    bbox=bbox,
                    core_segment_ids=segment_ids,
                    segment_ids=segment_ids,
                    context_segment_ids=(),
                    object_ids=(object_id,),
                    scope_id=scope_by_object[object_id],
                )
            )

        block_tuple = tuple(blocks)
        membership_units = self._membership_units(
            blocks=block_tuple,
            source_ids=source_ids,
        )
        paragraph_blocks = sum(
            object_by_id[object_id].kind is not ObjectKind.TABLE
            and object_id not in inferred_tables
            for object_id, _ in groups
        )
        table_blocks = len(groups) - paragraph_blocks
        return BlockPlan(
            aligned_size=aligned_size,
            source_segment_ids=source_ids,
            blocks=block_tuple,
            adjacent_algebra=(),
            diagnostics=(
                "core-partition=exact",
                "core-order=canonical-within-object-scopes",
                "object-ownership=core-derived",
                "mode=spatial-2d-object-local",
                "flow-blocks=one-block-per-object",
                "table-blocks=one-block-per-segment",
                f"planning-objects={len(objects_result.objects)}",
                f"table-objects={len(table_objects)}",
                f"matrix-inferred-table-objects={len(inferred_tables)}",
                f"paragraph-or-list-blocks={paragraph_blocks}",
                f"table-segment-blocks={table_blocks}",
                f"total-block-memberships={total_memberships}",
                f"padding-exclusion-checks={padding_budget.checks}",
                "overlap-components=object-local-disjoint",
                "membership=object-or-literal-segment",
                "raw-and-gamma-selection=deferred-to-stage2",
            ),
            mode=BlockPlanningMode.SPATIAL_2D,
            membership_units=membership_units,
            matrix_sha256=sparse_matrix_sha256(matrix),
        )

    @staticmethod
    def _object_is_table_like(
        *,
        document_object: DocumentObject,
        matrix: SparseSegmentMatrix,
    ) -> bool:
        if document_object.kind is ObjectKind.TABLE:
            return True
        segment_ids = set(document_object.segment_ids)
        merge_left = tuple(
            item
            for item in matrix.structural_codes
            if item.segment_id in segment_ids
            and MERGE_LEFT_CODE in sparse_code_components(item.code)
        )
        repeated_logical_grid = (
            len(merge_left) >= 2
            and len({item.row for item in merge_left}) >= 2
        )
        ruled_pixel_grid = bool(matrix.horizontal_rule_rows) and bool(
            matrix.vertical_rule_columns
        )
        return repeated_logical_grid or ruled_pixel_grid

    @staticmethod
    def _rule_runs(values: tuple[int, ...]) -> tuple[_MatrixBand, ...]:
        runs: list[_MatrixBand] = []
        for value in values:
            if runs and runs[-1].stop == value:
                runs[-1] = _MatrixBand(runs[-1].start, value + 1)
            else:
                runs.append(_MatrixBand(value, value + 1))
        return tuple(runs)

    def _planning_scopes(
        self,
        *,
        objects_result: ObjectReconstructionResult,
    ) -> tuple[_PlanningScope, ...]:
        """Map every Stage 6 object to exactly one independent OCR scope."""

        return tuple(
            _PlanningScope(
                scope_id=f"scope-{index:06d}",
                segment_ids=document_object.segment_ids,
                ruled=document_object.kind is ObjectKind.TABLE,
            )
            for index, document_object in enumerate(objects_result.objects)
        )

    def _logical_row_bands(
        self,
        *,
        matrix: SparseSegmentMatrix,
        source_ids: tuple[str, ...],
        span_by_id: dict[str, SegmentSpan],
        cells_by_segment: dict[str, tuple[SparseCell, ...]],
        ruled: bool,
        work_budget: _WorkBudget,
    ) -> tuple[_MatrixBand, ...]:
        work_budget.consume(2 * len(source_ids))
        scope_row_start = min(span_by_id[item].row_start for item in source_ids)
        scope_row_stop = max(span_by_id[item].row_stop for item in source_ids)
        rule_rows = (
            {
                item
                for item in matrix.horizontal_rule_rows
                if scope_row_start <= item < scope_row_stop
            }
            if ruled
            else set()
        )
        source_order = {
            segment_id: index for index, segment_id in enumerate(source_ids)
        }
        cells_by_row: dict[int, set[str]] = {}
        for segment_id in source_ids:
            work_budget.consume(len(cells_by_segment[segment_id]))
            for cell in cells_by_segment[segment_id]:
                cells_by_row.setdefault(cell.row, set()).add(cell.segment_id)
        bands: list[_MatrixBand] = []
        previous_incidence: tuple[str, ...] | None = None
        for row in range(scope_row_start, scope_row_stop):
            row_members = cells_by_row.get(row, set())
            work_budget.consume(1 + len(row_members))
            incidence = tuple(
                sorted(row_members, key=source_order.__getitem__)
            )
            if not incidence or row in rule_rows:
                previous_incidence = None
                continue
            # Horizontal *pixel* rules define structural row areas.  A v16
            # logical projection has no physical rule rows: ``ruled`` there
            # is an object classification, not permission to collapse every
            # contextual leaf into one giant OCR block.  Keep its changing
            # anchor incidence as literal row boundaries so adjacent sliding
            # windows retain context and independently decodable signatures.
            merge = bool(
                bands
                and bands[-1].stop == row
                and (
                    (
                        ruled
                        and matrix.coordinate_mode
                        is SparseCoordinateMode.PIXEL_PARTITION
                    )
                    or previous_incidence == incidence
                )
            )
            if merge:
                bands[-1] = _MatrixBand(bands[-1].start, row + 1)
            else:
                bands.append(_MatrixBand(row, row + 1))
            previous_incidence = incidence
        return tuple(bands)

    @staticmethod
    def _adaptive_table_window_shape(
        *,
        row_count: int,
        column_count: int,
    ) -> tuple[int, int]:
        """Choose a bounded 2-D OCR context window for one logical table.

        The longer side ``n`` follows ``ceil(log2(table side))`` and the
        shorter side ``m`` is kept strictly between ``n / 2`` and ``n`` when
        the table has enough rows/columns to satisfy that relation.  A 14x10
        homogeneous 14x10 region therefore starts with a 4x3 candidate
        window.  A later discrete selector sparsifies the complete candidate
        family; this function does not mistake occupied-cell density for the
        requested sparse block code.
        """

        if (
            type(row_count) is not int
            or type(column_count) is not int
            or row_count < 1
            or column_count < 1
        ):
            raise BlockPlanningInvariantError(
                "logical table dimensions must be positive integers"
            )
        rows_are_long = row_count >= column_count
        long_count = row_count if rows_are_long else column_count
        short_count = column_count if rows_are_long else row_count
        if short_count == 1:
            long_window = min(
                long_count,
                max(2, math.ceil(math.log2(long_count))),
            )
            short_window = 1
        else:
            long_window = min(
                long_count,
                max(3, math.ceil(math.log2(long_count))),
            )
            short_window = min(
                short_count,
                max(2, math.ceil(math.log2(short_count))),
                max(2, long_window - 1),
            )
            # A very elongated table may have too few columns/rows for the
            # logarithmic long side.  Shrink n until m > n/2 is true.
            long_window = min(long_window, 2 * short_window - 1)
            if long_window <= short_window and long_count > short_window:
                long_window = short_window + 1
            if long_window == short_window and short_window > 2:
                short_window -= 1
        return (
            (long_window, short_window)
            if rows_are_long
            else (short_window, long_window)
        )

    @staticmethod
    def _dyadic_table_masks(
        *,
        scope: _PlanningScope,
        span_by_id: dict[str, SegmentSpan],
        segment_by_id: dict[str, Segment],
    ) -> tuple[tuple[tuple[str, ...], ...], int, int, int]:
        """Encode table cells as non-Cartesian OCR membership masks.

        Segments with the same immutable matrix span are one logical cell.
        Two overlapping power-of-two column masks provide broad OCR context.
        Independent binary masks for row spans and column spans then
        distinguish every cell without flattening the table into a Cartesian
        row-major index.  The resulting masks are dyadic bands and unions of
        bands, matching the table geometry while still allowing disconnected
        components to participate in one OCR block.
        """

        units_by_span: dict[
            tuple[int, int, int, int],
            list[str],
        ] = {}
        for segment_id in scope.segment_ids:
            span = span_by_id[segment_id]
            key = (
                span.row_start,
                span.row_stop,
                span.column_start,
                span.column_stop,
            )
            units_by_span.setdefault(key, []).append(segment_id)
        ordered_units = tuple(
            (key, (segment_id,))
            for key, unit_ids in sorted(units_by_span.items())
            for segment_id in unit_ids
        )
        unit_count = len(ordered_units)
        if unit_count == 1 and len(scope.segment_ids) == 1:
            raise BlockPlanningInvariantError(
                "an isolated 1x1 table segment has no OCR context and cannot "
                "form a recognition block"
            )

        masks: list[tuple[str, ...]] = []
        seen: set[tuple[str, ...]] = set()

        def add_units(indexes: tuple[int, ...]) -> None:
            selected = {
                segment_id
                for index in indexes
                for segment_id in ordered_units[index][1]
            }
            members = tuple(
                segment_id
                for segment_id in scope.segment_ids
                if segment_id in selected
            )
            nontrivial_members = tuple(
                segment_id
                for segment_id in members
                if (
                    segment_by_id[segment_id].bbox.width
                    * segment_by_id[segment_id].bbox.height
                )
                > 1
            )
            noise_count = len(members) - len(nontrivial_members)
            if not nontrivial_members:
                return
            if noise_count > len(nontrivial_members):
                if len(nontrivial_members) < 2:
                    return
                members = nontrivial_members
            if len(members) < 2 or members in seen:
                return
            seen.add(members)
            masks.append(members)

        chunks: list[tuple[int, ...]] = []
        pending_indexes: list[int] = []
        pending_members = 0
        for index, (_key, segment_ids) in enumerate(ordered_units):
            segment_count = len(segment_ids)
            if segment_count > 256:
                raise BlockPlanningLimitError(
                    "one logical table cell exceeds the 16x16 segment limit"
                )
            if pending_indexes and (
                len(pending_indexes) >= 256
                or pending_members + segment_count > 256
            ):
                chunks.append(tuple(pending_indexes))
                pending_indexes = []
                pending_members = 0
            pending_indexes.append(index)
            pending_members += segment_count
        if pending_indexes:
            chunks.append(tuple(pending_indexes))
        if chunks and sum(
            len(ordered_units[index][1]) for index in chunks[-1]
        ) < 2:
            if len(chunks) == 1:
                raise BlockPlanningInvariantError(
                    "table chunk has no multi-segment OCR context"
                )
            chunks[-1] += chunks[-2][-1:]

        bit_count = 0
        maximum_context_width = 1
        for chunk in chunks:
            column_count = min(
                16,
                max(1, math.ceil(math.sqrt(len(chunk)))),
            )
            row_count = math.ceil(len(chunk) / column_count)
            dense_position = {
                unit_index: (
                    offset // column_count,
                    offset % column_count,
                )
                for offset, unit_index in enumerate(chunk)
            }
            context_mask_width = 1 << (column_count.bit_length() - 1)
            if context_mask_width == column_count and column_count > 1:
                context_mask_width //= 2
            context_width = max(
                1,
                context_mask_width * 2 - column_count,
            )
            maximum_context_width = max(
                maximum_context_width,
                context_width,
            )

            add_units(
                tuple(
                    index
                    for index in chunk
                    if dense_position[index][1] < context_mask_width
                )
            )
            add_units(
                tuple(
                    index
                    for index in chunk
                    if dense_position[index][1]
                    >= column_count - context_mask_width
                )
            )

            axes = (
                (
                    0,
                    max(1, (row_count - 1).bit_length()),
                ),
                (
                    1,
                    max(1, (column_count - 1).bit_length()),
                ),
            )
            bit_count += sum(
                axis_bits for _offset, axis_bits in axes
            )
            for offset, axis_bits in axes:
                for bit in range(axis_bits):
                    inside = tuple(
                        index
                        for index in chunk
                        if (dense_position[index][offset] >> bit) & 1
                    )
                    inside_members = sum(
                        len(ordered_units[index][1]) for index in inside
                    )
                    if inside_members < 2:
                        inside_set = set(inside)
                        inside = tuple(
                            index
                            for index in chunk
                            if index not in inside_set
                        )
                    add_units(inside)

            chunk_segment_ids = {
                segment_id
                for index in chunk
                for segment_id in ordered_units[index][1]
            }
            covered = {
                segment_id
                for members in masks
                for segment_id in members
                if segment_id in chunk_segment_ids
            }
            if covered != chunk_segment_ids:
                add_units(chunk)

        if not masks:
            raise BlockPlanningInvariantError(
                "table cells produced no multi-segment OCR context mask"
            )
        return (
            tuple(masks),
            unit_count,
            bit_count,
            maximum_context_width,
        )

    @staticmethod
    def _homogeneous_table_regions(
        *,
        scope: _PlanningScope,
        row_bands: tuple[_MatrixBand, ...],
        column_bands: tuple[_MatrixBand, ...],
        span_by_id: dict[str, SegmentSpan],
        cells_by_segment: dict[str, tuple[SparseCell, ...]],
        work_budget: _WorkBudget,
    ) -> tuple[_HomogeneousTableRegion, ...]:
        """Split a table at dimensional barriers before making OCR blocks."""

        row_band_by_slot = {
            row: band_index
            for band_index, band in enumerate(row_bands)
            for row in range(band.start, band.stop)
        }
        column_band_by_slot = {
            column: band_index
            for band_index, band in enumerate(column_bands)
            for column in range(band.start, band.stop)
        }
        cells_by_shape: dict[tuple[int, int], set[tuple[int, int]]] = {}
        segments_by_shape_cell: dict[
            tuple[tuple[int, int], tuple[int, int]], set[str]
        ] = {}
        coordinates_by_shape_segment: dict[
            tuple[tuple[int, int], str], set[tuple[int, int]]
        ] = {}
        for segment_id in scope.segment_ids:
            cells = cells_by_segment[segment_id]
            work_budget.consume(len(cells))
            span = span_by_id[segment_id]
            shape = (
                span.row_stop - span.row_start,
                span.column_stop - span.column_start,
            )
            for cell in cells:
                row = row_band_by_slot.get(cell.row)
                column = column_band_by_slot.get(cell.column)
                if row is not None and column is not None:
                    coordinate = (row, column)
                    cells_by_shape.setdefault(shape, set()).add(coordinate)
                    segments_by_shape_cell.setdefault(
                        (shape, coordinate), set()
                    ).add(segment_id)
                    coordinates_by_shape_segment.setdefault(
                        (shape, segment_id), set()
                    ).add(coordinate)
        if not cells_by_shape:
            raise BlockPlanningInvariantError(
                f"table scope {scope.scope_id} has no dimensional regions"
            )

        source_order = {
            segment_id: index
            for index, segment_id in enumerate(scope.segment_ids)
        }
        regions: list[_HomogeneousTableRegion] = []
        for shape, unvisited_source in sorted(cells_by_shape.items()):
            unvisited = set(unvisited_source)
            while unvisited:
                start = min(unvisited)
                stack = [start]
                component: set[tuple[int, int]] = set()
                expanded_segments: set[str] = set()
                while stack:
                    coordinate = stack.pop()
                    if coordinate not in unvisited:
                        continue
                    unvisited.remove(coordinate)
                    component.add(coordinate)
                    row, column = coordinate
                    neighbours = {
                        (row - 1, column),
                        (row + 1, column),
                        (row, column - 1),
                        (row, column + 1),
                    }
                    # A sparse merged segment may deliberately omit interior
                    # cells.  All cells carrying that source ID are one
                    # structural unit even when they are not 4-neighbours.
                    for segment_id in segments_by_shape_cell[
                        (shape, coordinate)
                    ]:
                        if segment_id not in expanded_segments:
                            owned_coordinates = coordinates_by_shape_segment[
                                (shape, segment_id)
                            ]
                            work_budget.consume(len(owned_coordinates))
                            neighbours.update(owned_coordinates)
                            expanded_segments.add(segment_id)
                    stack.extend(neighbours & unvisited)
                segment_ids = {
                    segment_id
                    for coordinate in component
                    for segment_id in segments_by_shape_cell[
                        (shape, coordinate)
                    ]
                }
                regions.append(
                    _HomogeneousTableRegion(
                        segment_shape=shape,
                        segment_ids=tuple(
                            sorted(segment_ids, key=source_order.__getitem__)
                        ),
                        row_band_indexes=tuple(
                            sorted({row for row, _column in component})
                        ),
                        column_band_indexes=tuple(
                            sorted({column for _row, column in component})
                        ),
                    )
                )
        # A disconnected ordinary cell still needs OCR context.  Attach such
        # a 1x1 unit to the nearest region of the same dimensionality before
        # candidate windows are generated.  Membership remains homogeneous;
        # only the sparse region becomes disconnected.  A true one-cell table
        # has no valid same-shape context and is rejected by the caller.
        while True:
            singleton_index = next(
                (
                    index
                    for index, region in enumerate(regions)
                    if region.segment_shape == (1, 1)
                    and len(region.segment_ids) == 1
                    and any(
                        other_index != index
                        and other.segment_shape == region.segment_shape
                        for other_index, other in enumerate(regions)
                    )
                ),
                None,
            )
            if singleton_index is None:
                break
            singleton = regions[singleton_index]

            def region_distance(other: _HomogeneousTableRegion) -> int:
                return min(
                    abs(first_row - second_row)
                    + abs(first_column - second_column)
                    for first_row in singleton.row_band_indexes
                    for first_column in singleton.column_band_indexes
                    for second_row in other.row_band_indexes
                    for second_column in other.column_band_indexes
                )

            target_index = min(
                (
                    index
                    for index, region in enumerate(regions)
                    if index != singleton_index
                    and region.segment_shape == singleton.segment_shape
                ),
                key=lambda index: (
                    region_distance(regions[index]),
                    int(len(regions[index].segment_ids) == 1),
                    regions[index].row_band_indexes[0],
                    regions[index].column_band_indexes[0],
                ),
            )
            target = regions[target_index]
            merged = _HomogeneousTableRegion(
                segment_shape=singleton.segment_shape,
                segment_ids=tuple(
                    sorted(
                        (*singleton.segment_ids, *target.segment_ids),
                        key=source_order.__getitem__,
                    )
                ),
                row_band_indexes=tuple(
                    sorted(
                        set(singleton.row_band_indexes)
                        | set(target.row_band_indexes)
                    )
                ),
                column_band_indexes=tuple(
                    sorted(
                        set(singleton.column_band_indexes)
                        | set(target.column_band_indexes)
                    )
                ),
                context_bridge_required=True,
            )
            for index in sorted(
                (singleton_index, target_index), reverse=True
            ):
                regions.pop(index)
            regions.append(merged)
        # A diagonal singleton can bridge two otherwise homogeneous row
        # components.  The first pass attaches it to one side; rejoin the
        # newly touching same-shape neighbour so adaptive sliding windows
        # cross that boundary instead of producing disconnected OCR islands.
        while True:
            bridge_pair: tuple[int, int] | None = None
            for first_index, first in enumerate(regions):
                for second_index in range(first_index + 1, len(regions)):
                    second = regions[second_index]
                    if (
                        first.segment_shape != second.segment_shape
                        or not (
                            first.context_bridge_required
                            or second.context_bridge_required
                        )
                    ):
                        continue
                    rows_touch = (
                        min(
                            abs(first_row - second_row)
                            for first_row in first.row_band_indexes
                            for second_row in second.row_band_indexes
                        )
                        <= 1
                    )
                    columns_overlap = bool(
                        set(first.column_band_indexes)
                        & set(second.column_band_indexes)
                    )
                    columns_touch = (
                        min(
                            abs(first_column - second_column)
                            for first_column in first.column_band_indexes
                            for second_column in second.column_band_indexes
                        )
                        <= 1
                    )
                    rows_overlap = bool(
                        set(first.row_band_indexes)
                        & set(second.row_band_indexes)
                    )
                    if (rows_touch and columns_overlap) or (
                        columns_touch and rows_overlap
                    ):
                        bridge_pair = (first_index, second_index)
                        break
                if bridge_pair is not None:
                    break
            if bridge_pair is None:
                break
            first_index, second_index = bridge_pair
            first = regions[first_index]
            second = regions[second_index]
            joined = _HomogeneousTableRegion(
                segment_shape=first.segment_shape,
                segment_ids=tuple(
                    sorted(
                        (*first.segment_ids, *second.segment_ids),
                        key=source_order.__getitem__,
                    )
                ),
                row_band_indexes=tuple(
                    sorted(
                        set(first.row_band_indexes)
                        | set(second.row_band_indexes)
                    )
                ),
                column_band_indexes=tuple(
                    sorted(
                        set(first.column_band_indexes)
                        | set(second.column_band_indexes)
                    )
                ),
                context_bridge_required=True,
            )
            for index in (second_index, first_index):
                regions.pop(index)
            regions.append(joined)
        assigned = tuple(
            segment_id
            for region in regions
            for segment_id in region.segment_ids
        )
        if len(assigned) != len(set(assigned)) or set(assigned) != set(
            scope.segment_ids
        ):
            raise BlockPlanningInvariantError(
                "a source segment crossed homogeneous table regions"
            )
        return tuple(
            sorted(
                regions,
                key=lambda item: (
                    item.row_band_indexes[0],
                    item.column_band_indexes[0],
                    item.segment_shape,
                ),
            )
        )

    def _sparse_identifying_candidate_indexes(
        self,
        *,
        members: tuple[tuple[str, ...], ...],
        scope_ids: tuple[str, ...],
        windows: tuple[tuple[int, int, int, int] | None, ...],
        source_ids: tuple[str, ...],
    ) -> tuple[tuple[int, ...], dict[str, int | float]]:
        """Select a bounded test cover instead of OCRing every sliding window.

        Full candidate signatures define the finest distinction the sparse
        matrix can express.  The greedy test-cover loop preserves exactly that
        partition and coverage while minimizing the number of selected tests;
        reverse deletion then removes candidates made redundant by later
        choices.  Repeated IDs inside a matrix rectangle are already one set
        member and therefore never create duplicate OCR obligations.
        """

        if not (
            len(members) == len(scope_ids) == len(windows)
            and len(members) <= self.config.max_signature_candidates
        ):
            raise BlockPlanningInvariantError(
                "adaptive candidate arrays disagree or exceed their limit"
            )
        selected: set[int] = {
            index for index, window in enumerate(windows) if window is None
        }
        intrinsic_units = 0
        selector_checks = 0
        memberships_by_source = {segment_id: 0 for segment_id in source_ids}
        table_scopes = tuple(
            dict.fromkeys(
                scope_id
                for scope_id, window in zip(scope_ids, windows)
                if window is not None
            )
        )
        source_order = {
            segment_id: index for index, segment_id in enumerate(source_ids)
        }

        for scope_id in table_scopes:
            candidate_indexes = tuple(
                index
                for index, (candidate_scope, window) in enumerate(
                    zip(scope_ids, windows)
                )
                if candidate_scope == scope_id and window is not None
            )
            scope_sources = tuple(
                segment_id
                for segment_id in source_ids
                if any(
                    segment_id in members[index]
                    for index in candidate_indexes
                )
            )
            full_signatures: dict[tuple[int, ...], list[str]] = {}
            for segment_id in scope_sources:
                signature = tuple(
                    index
                    for index in candidate_indexes
                    if segment_id in members[index]
                )
                selector_checks += len(candidate_indexes)
                if selector_checks > self.config.max_signature_candidate_checks:
                    raise BlockPlanningLimitError(
                        "sparse candidate signature analysis exceeds configured "
                        "work limit"
                    )
                if not signature:
                    raise BlockPlanningInvariantError(
                        f"segment {segment_id} has no adaptive table candidate"
                    )
                full_signatures.setdefault(signature, []).append(segment_id)
            atoms = tuple(
                tuple(sorted(values, key=source_order.__getitem__))
                for _signature, values in sorted(
                    full_signatures.items(),
                    key=lambda item: min(
                        source_order[value] for value in item[1]
                    ),
                )
            )
            intrinsic_units += len(atoms)
            atom_indexes_by_candidate: dict[int, frozenset[int]] = {}
            for candidate_index in candidate_indexes:
                candidate_set = set(members[candidate_index])
                atom_indexes: set[int] = set()
                for atom_index, atom in enumerate(atoms):
                    inside = tuple(
                        segment_id in candidate_set for segment_id in atom
                    )
                    selector_checks += len(atom)
                    if selector_checks > self.config.max_signature_candidate_checks:
                        raise BlockPlanningLimitError(
                            "sparse candidate atom analysis exceeds configured "
                            "work limit"
                        )
                    if any(inside) and not all(inside):
                        raise BlockPlanningInvariantError(
                            "one candidate split an intrinsic membership atom"
                        )
                    if all(inside):
                        atom_indexes.add(atom_index)
                atom_indexes_by_candidate[candidate_index] = frozenset(
                    atom_indexes
                )

            unresolved: list[tuple[int, ...]] = [tuple(range(len(atoms)))]
            covered: set[int] = set()
            membership_counts = [0] * len(atoms)
            remaining = set(candidate_indexes)
            scope_selected: list[int] = []
            while any(len(group) > 1 for group in unresolved) or len(
                covered
            ) < len(atoms):
                best: tuple[tuple[int, ...], int] | None = None
                for candidate_index in sorted(remaining):
                    candidate_atoms = atom_indexes_by_candidate[candidate_index]
                    pair_gain = 0
                    for group in unresolved:
                        inside_count = sum(
                            atom_index in candidate_atoms
                            for atom_index in group
                        )
                        pair_gain += inside_count * (
                            len(group) - inside_count
                        )
                        selector_checks += len(group)
                    coverage_gain = len(candidate_atoms - covered)
                    gain = pair_gain + coverage_gain
                    if not gain:
                        continue
                    repeat_burden = sum(
                        membership_counts[atom_index]
                        for atom_index in candidate_atoms
                    )
                    key = (
                        gain,
                        pair_gain,
                        coverage_gain,
                        -repeat_burden,
                        -len(candidate_atoms),
                        -candidate_index,
                    )
                    if best is None or key > best[0]:
                        best = (key, candidate_index)
                if selector_checks > self.config.max_signature_candidate_checks:
                    raise BlockPlanningLimitError(
                        "sparse candidate selection exceeds configured work limit"
                    )
                if best is None:
                    raise BlockPlanningInvariantError(
                        "adaptive candidates cannot preserve their own "
                        "membership distinctions"
                    )
                candidate_index = best[1]
                scope_selected.append(candidate_index)
                remaining.remove(candidate_index)
                candidate_atoms = atom_indexes_by_candidate[candidate_index]
                covered.update(candidate_atoms)
                for atom_index in candidate_atoms:
                    membership_counts[atom_index] += 1
                refined: list[tuple[int, ...]] = []
                for group in unresolved:
                    inside = tuple(
                        atom_index
                        for atom_index in group
                        if atom_index in candidate_atoms
                    )
                    outside = tuple(
                        atom_index
                        for atom_index in group
                        if atom_index not in candidate_atoms
                    )
                    if inside:
                        refined.append(inside)
                    if outside:
                        refined.append(outside)
                unresolved = refined

            def complete(indexes: tuple[int, ...]) -> bool:
                signatures = tuple(
                    tuple(
                        index
                        for index in indexes
                        if atom_index
                        in atom_indexes_by_candidate[index]
                    )
                    for atom_index in range(len(atoms))
                )
                return all(signatures) and len(signatures) == len(
                    set(signatures)
                )

            for candidate_index in tuple(reversed(scope_selected)):
                reduced = tuple(
                    index
                    for index in scope_selected
                    if index != candidate_index
                )
                if complete(reduced):
                    scope_selected.remove(candidate_index)
            selected.update(scope_selected)
            for atom_index, atom in enumerate(atoms):
                count = sum(
                    atom_index in atom_indexes_by_candidate[index]
                    for index in scope_selected
                )
                for segment_id in atom:
                    memberships_by_source[segment_id] = count

        selected_indexes = tuple(sorted(selected))
        selected_counts = tuple(
            memberships_by_source[segment_id]
            for segment_id in source_ids
            if memberships_by_source[segment_id]
        )
        return selected_indexes, {
            "selected_candidates": sum(
                windows[index] is not None for index in selected_indexes
            ),
            "intrinsic_units": intrinsic_units,
            "max_memberships": max(selected_counts, default=0),
            "mean_memberships": (
                sum(selected_counts) / len(selected_counts)
                if selected_counts
                else 0.0
            ),
        }

    @staticmethod
    def _matrix_rectangle_bbox(
        *,
        matrix: SparseSegmentMatrix,
        aligned_size: tuple[int, int],
        scope: _PlanningScope,
        span_by_id: dict[str, SegmentSpan],
        segment_by_id: dict[str, Segment],
        member_ids: tuple[str, ...],
        row_start: int,
        row_stop: int,
        column_start: int,
        column_stop: int,
    ) -> Box:
        """Project a logical table window onto immutable object pixels."""

        width, height = aligned_size
        axes_are_physical = (
            matrix.coordinate_mode is SparseCoordinateMode.PIXEL_PARTITION
            or (
                matrix.rows[-1].end > len(matrix.rows)
                and matrix.columns[-1].end > len(matrix.columns)
            )
        )
        if axes_are_physical:
            left = matrix.columns[column_start].start
            top = matrix.rows[row_start].start
            right = matrix.columns[column_stop - 1].end
            bottom = matrix.rows[row_stop - 1].end
        else:
            scope_bbox = Box.union(
                segment_by_id[item].bbox for item in scope.segment_ids
            )
            scope_row_start = min(
                span_by_id[item].row_start for item in scope.segment_ids
            )
            scope_row_stop = max(
                span_by_id[item].row_stop for item in scope.segment_ids
            )
            scope_column_start = min(
                span_by_id[item].column_start for item in scope.segment_ids
            )
            scope_column_stop = max(
                span_by_id[item].column_stop for item in scope.segment_ids
            )
            row_denominator = scope_row_stop - scope_row_start
            column_denominator = scope_column_stop - scope_column_start
            left = scope_bbox.left + (
                scope_bbox.width * (column_start - scope_column_start)
                // column_denominator
            )
            right = scope_bbox.left + (
                scope_bbox.width * (column_stop - scope_column_start)
                // column_denominator
            )
            top = scope_bbox.top + (
                scope_bbox.height * (row_start - scope_row_start)
                // row_denominator
            )
            bottom = scope_bbox.top + (
                scope_bbox.height * (row_stop - scope_row_start)
                // row_denominator
            )

        # Logical projections may use non-uniform cells.  Expand only far
        # enough to expose at least one pixel from every declared member; a
        # merged cell is intentionally clipped to this window, not allowed to
        # widen it to the complete table.
        for segment_id in member_ids:
            bbox = segment_by_id[segment_id].bbox
            if bbox.right <= left:
                left = bbox.right - 1
            if bbox.left >= right:
                right = bbox.left + 1
            if bbox.bottom <= top:
                top = bbox.bottom - 1
            if bbox.top >= bottom:
                bottom = bbox.top + 1
        return Box(
            max(0, left),
            max(0, top),
            min(width, right),
            min(height, bottom),
        )

    def _logical_table_column_bands(
        self,
        *,
        matrix: SparseSegmentMatrix,
        scope: _PlanningScope,
        span_by_id: dict[str, SegmentSpan],
        cells_by_segment: dict[str, tuple[SparseCell, ...]],
        work_budget: _WorkBudget,
    ) -> tuple[_MatrixBand, ...]:
        """Return real table columns, not full-height comparison probes."""

        column_start = min(
            span_by_id[item].column_start for item in scope.segment_ids
        )
        column_stop = max(
            span_by_id[item].column_stop for item in scope.segment_ids
        )
        occupied_columns: set[int] = set()
        for segment_id in scope.segment_ids:
            work_budget.consume(len(cells_by_segment[segment_id]))
            occupied_columns.update(
                item.column for item in cells_by_segment[segment_id]
            )

        # Object-local v16 matrices already expose one axis slot per logical
        # table column.  Preserve empty columns inside the declared span too:
        # they are structural context even when no OCR segment occupies them.
        if matrix.coordinate_mode is SparseCoordinateMode.LOGICAL_PROJECTION:
            return tuple(
                _MatrixBand(column, column + 1)
                for column in range(column_start, column_stop)
            )

        boundaries = tuple(
            item
            for item in self._rule_runs(matrix.vertical_rule_columns)
            if column_start < item.start < column_stop
        )
        if boundaries:
            bands: list[_MatrixBand] = []
            start = column_start
            for boundary in boundaries:
                if start < boundary.start and any(
                    start <= column < boundary.start
                    for column in occupied_columns
                ):
                    bands.append(_MatrixBand(start, boundary.start))
                start = boundary.stop
            if start < column_stop and any(
                start <= column < column_stop for column in occupied_columns
            ):
                bands.append(_MatrixBand(start, column_stop))
            if bands:
                return tuple(bands)

        # Unruled/pixel matrices can contain many raw pixel intervals inside a
        # cell.  Equal incidence is the stable matrix-native column identity.
        incidence_by_column: dict[int, frozenset[str]] = {}
        for column in range(column_start, column_stop):
            work_budget.consume(1 + len(scope.segment_ids))
            incidence_by_column[column] = frozenset(
                segment_id
                for segment_id in scope.segment_ids
                if any(
                    item.column == column
                    for item in cells_by_segment[segment_id]
                )
            )
        bands = []
        previous: frozenset[str] | None = None
        for column in range(column_start, column_stop):
            incidence = incidence_by_column[column]
            if not incidence:
                previous = None
                continue
            if bands and bands[-1].stop == column and incidence == previous:
                bands[-1] = _MatrixBand(bands[-1].start, column + 1)
            else:
                bands.append(_MatrixBand(column, column + 1))
            previous = incidence
        return tuple(bands)

    def _column_probe_rectangles(
        self,
        *,
        matrix: SparseSegmentMatrix,
        scope: _PlanningScope,
        row_bands: tuple[_MatrixBand, ...],
        span_by_id: dict[str, SegmentSpan],
        cells_by_segment: dict[str, tuple[SparseCell, ...]],
        work_budget: _WorkBudget,
    ) -> tuple[tuple[tuple[int, int, int, int], ...], int]:
        """Return local prefix rectangles, never a forged global column.

        For a ruled table, each vertical boundary is followed only through a
        maximal run of logical rows where no segment span crosses it.  A wide
        heading or merged cell terminates the run.  For unruled flow the only
        available matrix-native columns are equal cell-incidence bands.
        """

        column_start = min(
            span_by_id[item].column_start for item in scope.segment_ids
        )
        column_stop = max(
            span_by_id[item].column_stop for item in scope.segment_ids
        )
        cells_by_row: dict[int, list[SparseCell]] = {}
        incidence_by_column: dict[int, set[str]] = {}
        for segment_id in scope.segment_ids:
            work_budget.consume(len(cells_by_segment[segment_id]))
            for cell in cells_by_segment[segment_id]:
                cells_by_row.setdefault(cell.row, []).append(cell)
                incidence_by_column.setdefault(cell.column, set()).add(
                    cell.segment_id
                )

        if (
            not scope.ruled
            or matrix.coordinate_mode
            is SparseCoordinateMode.LOGICAL_PROJECTION
        ):
            bands: list[_MatrixBand] = []
            previous: frozenset[str] | None = None
            for column in range(column_start, column_stop):
                incidence = frozenset(incidence_by_column.get(column, ()))
                work_budget.consume(1 + len(incidence))
                if not incidence:
                    previous = None
                    continue
                if (
                    bands
                    and bands[-1].stop == column
                    and incidence == previous
                ):
                    bands[-1] = _MatrixBand(bands[-1].start, column + 1)
                else:
                    bands.append(_MatrixBand(column, column + 1))
                previous = incidence
            if not bands:
                return (), 0
            if len(bands) == 1:
                rectangles: tuple[tuple[int, int, int, int], ...] = ()
            elif len(bands) == 2:
                # Two columns need one orthogonal bit.  It is useful for a
                # multi-row 2-D object (the canonical 3x2 example), while
                # append_family rejects the resulting singleton for a 1x2
                # object and preserves that pair as an explicit SUBBLOCK.
                rectangles = (
                    (
                        row_bands[0].start,
                        row_bands[-1].stop,
                        bands[0].start,
                        bands[0].stop,
                    ),
                )
            else:
                # Sliding-2 is the sole-decoder column family.  Prefix or
                # sliding-4 supersets grow both crop area and overlap algebra
                # quadratically and are supplemental context at most.
                rectangles = tuple(
                    (
                        row_bands[0].start,
                        row_bands[-1].stop,
                        first.start,
                        second.stop,
                    )
                    for first, second in zip(bands, bands[1:])
                )
            return rectangles, len(bands)

        work_budget.consume(len(matrix.vertical_rule_columns))
        boundaries = tuple(
            item
            for item in self._rule_runs(matrix.vertical_rule_columns)
            if column_start < item.start < column_stop
        )
        rectangles: list[tuple[int, int, int, int]] = []
        if len(boundaries) == 1:
            window_specs = (
                (
                    column_start,
                    boundaries[0].start,
                    (boundaries[0],),
                ),
            )
        else:
            window_specs = tuple(
                (
                    (
                        column_start
                        if index == 0
                        else boundaries[index - 1].stop
                    ),
                    (
                        column_stop
                        if index + 1 == len(boundaries)
                        else boundaries[index + 1].start
                    ),
                    tuple(
                        boundary
                        for boundary in (
                            boundaries[index - 1] if index else None,
                            boundaries[index + 1]
                            if index + 1 < len(boundaries)
                            else None,
                        )
                        if boundary is not None
                    ),
                )
                for index in range(len(boundaries))
            )
        for window_start, window_stop, barrier_boundaries in window_specs:
            current: list[_MatrixBand] = []

            def flush() -> None:
                if current:
                    rectangles.append(
                        (
                            current[0].start,
                            current[-1].stop,
                            window_start,
                            window_stop,
                        )
                    )
                    current.clear()

            for row_band in row_bands:
                active: set[str] = set()
                for row in range(row_band.start, row_band.stop):
                    row_cells = cells_by_row.get(row, ())
                    work_budget.consume(1 + len(row_cells))
                    active.update(cell.segment_id for cell in row_cells)
                work_budget.consume(len(active))
                crosses = any(
                    span_by_id[item].column_start < boundary.start
                    and boundary.stop < span_by_id[item].column_stop
                    for item in active
                    for boundary in barrier_boundaries
                )
                if active and not crosses:
                    current.append(row_band)
                else:
                    flush()
            flush()
        return tuple(rectangles), len(boundaries) + 1

    def _matrix_rectangle_members(
        self,
        *,
        matrix: SparseSegmentMatrix,
        row_start: int,
        row_stop: int,
        column_start: int,
        column_stop: int,
        ordered_segments: tuple[Segment, ...],
        scope_ids: tuple[str, ...],
        scope_set: frozenset[str],
        cells_by_row: dict[int, tuple[SparseCell, ...]],
        max_closure_checks: int,
        visible_closure: bool = True,
    ) -> tuple[tuple[str, ...], int]:
        if not (
            0 <= row_start < row_stop <= len(matrix.rows)
            and 0 <= column_start < column_stop <= len(matrix.columns)
        ):
            raise BlockPlanningInvariantError(
                "matrix block rectangle lies outside declared axes"
            )
        row_slots = row_stop - row_start
        if row_slots > max_closure_checks:
            raise BlockPlanningLimitError(
                "matrix membership row-slot scan exceeds configured "
                "candidate work limit"
            )
        initial: set[str] = set()
        scan_checks = 0
        for row in range(row_start, row_stop):
            row_cells = cells_by_row.get(row, ())
            row_checks = 1 + len(row_cells)
            if scan_checks + row_checks > max_closure_checks:
                raise BlockPlanningLimitError(
                    "matrix membership cell scan exceeds configured "
                    "candidate work limit"
                )
            scan_checks += row_checks
            for cell in row_cells:
                if (
                    cell.segment_id in scope_set
                    and column_start <= cell.column < column_stop
                ):
                    initial.add(cell.segment_id)
        if not initial:
            return (), scan_checks
        if not visible_closure:
            ordering_checks = len(scope_ids)
            if scan_checks + ordering_checks > max_closure_checks:
                raise BlockPlanningLimitError(
                    "matrix membership ordering exceeds configured candidate "
                    "work limit"
                )
            return (
                tuple(item for item in scope_ids if item in initial),
                scan_checks + ordering_checks,
            )
        closed, closure_checks = self._spatial_membership_closure(
            initial,
            ordered_segments=ordered_segments,
            max_checks=max_closure_checks - scan_checks,
        )
        ordering_checks = len(scope_ids)
        if (
            scan_checks + closure_checks + ordering_checks
            > max_closure_checks
        ):
            raise BlockPlanningLimitError(
                "matrix membership ordering exceeds configured candidate work limit"
            )
        return (
            tuple(item for item in scope_ids if item in closed),
            scan_checks + closure_checks + ordering_checks,
        )

    @staticmethod
    def _overlap_component_count(
        blocks: tuple[RecognitionBlock, ...],
    ) -> int:
        if not blocks:
            return 0
        parents = list(range(len(blocks)))

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

        first_block_by_segment: dict[str, int] = {}
        for block_index, block in enumerate(blocks):
            for segment_id in block.segment_ids:
                first = first_block_by_segment.setdefault(
                    segment_id,
                    block_index,
                )
                union(first, block_index)
        return len({find(index) for index in range(len(blocks))})

    @staticmethod
    def _membership_units(
        *,
        blocks: tuple[RecognitionBlock, ...],
        source_ids: tuple[str, ...],
    ) -> tuple[MembershipUnit, ...]:
        """Partition source IDs by exact, canonical block signature."""

        source_order = {
            segment_id: index for index, segment_id in enumerate(source_ids)
        }
        grouped: dict[tuple[str, ...], list[str]] = {}
        scope_by_signature: dict[tuple[str, ...], str] = {}
        block_ids_by_segment: dict[str, list[str]] = {
            segment_id: [] for segment_id in source_ids
        }
        scopes_by_segment: dict[str, set[str | None]] = {
            segment_id: set() for segment_id in source_ids
        }
        for block in blocks:
            for segment_id in block.segment_ids:
                block_ids_by_segment[segment_id].append(block.block_id)
                scopes_by_segment[segment_id].add(block.scope_id)
        for segment_id in source_ids:
            signature = tuple(block_ids_by_segment[segment_id])
            if not signature:
                raise BlockPlanningInvariantError(
                    f"segment {segment_id} has no spatial membership signature"
                )
            scopes = scopes_by_segment[segment_id]
            if len(scopes) != 1 or None in scopes:
                raise BlockPlanningInvariantError(
                    f"segment {segment_id} crosses spatial planning scopes"
                )
            grouped.setdefault(signature, []).append(segment_id)
            scope_by_signature[signature] = next(iter(scopes))  # type: ignore[arg-type]
        ordered = sorted(
            grouped.items(),
            key=lambda item: min(source_order[value] for value in item[1]),
        )
        return tuple(
            MembershipUnit(
                unit_id=f"membership-unit-{index:06d}",
                kind=(
                    MembershipUnitKind.SEGMENT
                    if len(segment_ids) == 1
                    else MembershipUnitKind.SUBBLOCK
                ),
                segment_ids=tuple(segment_ids),
                block_ids=signature,
                scope_id=scope_by_signature[signature],
            )
            for index, (signature, segment_ids) in enumerate(ordered)
        )

    def _spatial_algebra(
        self,
        blocks: tuple[RecognitionBlock, ...],
        source_ids: tuple[str, ...],
    ) -> tuple[BlockSetAlgebra, ...]:
        memberships: dict[str, list[int]] = {
            segment_id: [] for segment_id in source_ids
        }
        for block_index, block in enumerate(blocks):
            for segment_id in block.segment_ids:
                indexes = memberships[segment_id]
                indexes.append(block_index)
                if len(indexes) > self.config.max_segment_memberships:
                    raise BlockPlanningLimitError(
                        "one segment exceeds configured block membership "
                        f"limit {self.config.max_segment_memberships}"
                    )

        pair_indexes: set[tuple[int, int]] = set()
        for indexes in memberships.values():
            for left_offset, first_index in enumerate(indexes):
                for second_index in indexes[left_offset + 1 :]:
                    if not BlockPlan._requires_spatial_algebra(
                        blocks[first_index], blocks[second_index]
                    ):
                        continue
                    pair_indexes.add((first_index, second_index))
                    if len(pair_indexes) > self.config.max_overlap_pairs:
                        raise BlockPlanningLimitError(
                            "spatial overlap pairs exceed configured limit "
                            f"{self.config.max_overlap_pairs}"
                        )

        values: list[BlockSetAlgebra] = []
        pair_memberships = 0
        for first_index, second_index in sorted(pair_indexes):
            first = blocks[first_index]
            second = blocks[second_index]
            first_set = set(first.segment_ids)
            second_set = set(second.segment_ids)
            intersection = first_set & second_set
            union = first_set | second_set
            xor = first_set ^ second_set
            first_only = first_set - second_set
            second_only = second_set - first_set
            pair_memberships += len(union)
            if pair_memberships > self.config.max_pair_memberships:
                raise BlockPlanningLimitError(
                    "block pair memberships exceed configured limit "
                    f"{self.config.max_pair_memberships}"
                )
            values.append(
                BlockSetAlgebra(
                    first_block_id=first.block_id,
                    second_block_id=second.block_id,
                    intersection_segment_ids=self._ordered_subset(
                        source_ids,
                        intersection,
                    ),
                    union_segment_ids=self._ordered_subset(
                        source_ids,
                        union,
                    ),
                    xor_segment_ids=self._ordered_subset(
                        source_ids,
                        xor,
                    ),
                    first_only_segment_ids=self._ordered_subset(
                        source_ids,
                        first_only,
                    ),
                    second_only_segment_ids=self._ordered_subset(
                        source_ids,
                        second_only,
                    ),
                )
            )
        return tuple(values)

    def _ownership_closure_units(
        self,
        *,
        ordered_segments: tuple[Segment, ...],
        objects_result: ObjectReconstructionResult,
        owner_by_segment: dict[str, str],
        aligned_size: tuple[int, int],
    ) -> tuple[_CoreUnit, ...]:
        """Close interleaved object spans without changing canonical segment order."""

        source_index = {
            segment.segment_id: index for index, segment in enumerate(ordered_segments)
        }
        intervals = sorted(
            (
                min(source_index[segment_id] for segment_id in document_object.segment_ids),
                max(source_index[segment_id] for segment_id in document_object.segment_ids) + 1,
            )
            for document_object in objects_result.objects
        )
        closures: list[tuple[int, int]] = []
        for start, stop in intervals:
            if closures and start < closures[-1][1]:
                previous_start, previous_stop = closures[-1]
                closures[-1] = (previous_start, max(previous_stop, stop))
            else:
                closures.append((start, stop))

        units: list[_CoreUnit] = []
        cursor = 0
        for start, stop in closures:
            if start != cursor:
                raise BlockPlanningInvariantError(
                    "object ownership closures do not cover canonical source order"
                )
            closure_segments = ordered_segments[start:stop]
            if self._fits(closure_segments, aligned_size=aligned_size):
                units.append(
                    _CoreUnit(
                        tuple(segment.segment_id for segment in closure_segments),
                        self._ordered_owners(closure_segments, owner_by_segment),
                    )
                )
            else:
                units.extend(
                    self._split_closure(
                        closure_segments,
                        owner_by_segment=owner_by_segment,
                        aligned_size=aligned_size,
                    )
                )
            cursor = stop
        if cursor != len(ordered_segments):
            raise BlockPlanningInvariantError(
                "object ownership closures do not cover canonical source order"
            )
        return tuple(units)

    def _fits(
        self,
        segments: tuple[Segment, ...],
        *,
        aligned_size: tuple[int, int],
    ) -> bool:
        if not segments or len(segments) > self.config.max_core_segments:
            return False
        return self._padded_bbox(segments, aligned_size=aligned_size).area <= self.config.max_block_pixels

    def _split_closure(
        self,
        segments: tuple[Segment, ...],
        *,
        owner_by_segment: dict[str, str],
        aligned_size: tuple[int, int],
    ) -> tuple[_CoreUnit, ...]:
        values: list[_CoreUnit] = []
        current: list[Segment] = []
        for row_group in self._row_groups(segments):
            candidate = tuple(current) + row_group
            if self._fits(candidate, aligned_size=aligned_size):
                current.extend(row_group)
                continue
            if not current:
                raise BlockPlanningLimitError(
                    f"row {row_group[0].row_index} pixel footprint cannot fit one bounded block"
                )
            current_tuple = tuple(current)
            values.append(
                _CoreUnit(
                    tuple(item.segment_id for item in current_tuple),
                    self._ordered_owners(current_tuple, owner_by_segment),
                )
            )
            current = list(row_group)
            if not self._fits(row_group, aligned_size=aligned_size):
                raise BlockPlanningLimitError(
                    f"row {row_group[0].row_index} pixel footprint cannot fit one bounded block"
                )
        if current:
            current_tuple = tuple(current)
            values.append(
                _CoreUnit(
                    tuple(item.segment_id for item in current_tuple),
                    self._ordered_owners(current_tuple, owner_by_segment),
                )
            )
        return tuple(values)

    @staticmethod
    def _ordered_owners(
        segments: tuple[Segment, ...],
        owner_by_segment: dict[str, str],
    ) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(owner_by_segment[segment.segment_id] for segment in segments)
        )

    def _pack_units(
        self,
        units: tuple[_CoreUnit, ...],
        segment_by_id: dict[str, Segment],
        *,
        aligned_size: tuple[int, int],
    ) -> tuple[_CoreUnit, ...]:
        units = self._merge_same_row_boundaries(
            units,
            segment_by_id,
            aligned_size=aligned_size,
        )
        packed: list[_CoreUnit] = []
        current_segments: tuple[str, ...] = ()
        current_objects: tuple[str, ...] = ()
        for unit in units:
            candidate_ids = current_segments + unit.segment_ids
            candidate_segments = tuple(segment_by_id[item] for item in candidate_ids)
            if current_segments and self._fits(
                candidate_segments,
                aligned_size=aligned_size,
            ):
                current_segments = candidate_ids
                current_objects = tuple(dict.fromkeys(current_objects + unit.object_ids))
                continue
            if current_segments:
                packed.append(_CoreUnit(current_segments, current_objects))
            current_segments = unit.segment_ids
            current_objects = unit.object_ids
        if current_segments:
            packed.append(_CoreUnit(current_segments, current_objects))
        return tuple(packed)

    def _merge_same_row_boundaries(
        self,
        units: tuple[_CoreUnit, ...],
        segment_by_id: dict[str, Segment],
        *,
        aligned_size: tuple[int, int],
    ) -> tuple[_CoreUnit, ...]:
        merged: list[_CoreUnit] = []
        for unit in units:
            if not merged:
                merged.append(unit)
                continue
            previous = merged[-1]
            previous_segments = tuple(segment_by_id[item] for item in previous.segment_ids)
            current_segments = tuple(segment_by_id[item] for item in unit.segment_ids)
            previous_row = self._row_groups(previous_segments)[-1]
            current_row = self._row_groups(current_segments)[0]
            if not self._vertical_envelopes_overlap(previous_row, current_row):
                merged.append(unit)
                continue
            candidate = _CoreUnit(
                segment_ids=previous.segment_ids + unit.segment_ids,
                object_ids=tuple(dict.fromkeys(previous.object_ids + unit.object_ids)),
            )
            candidate_segments = tuple(segment_by_id[item] for item in candidate.segment_ids)
            if not self._fits(candidate_segments, aligned_size=aligned_size):
                raise BlockPlanningLimitError("an indivisible physical row exceeds block limits")
            merged[-1] = candidate
        return tuple(merged)

    def _finish_blocks(
        self,
        *,
        aligned_size: tuple[int, int],
        cores: tuple[_CoreUnit, ...],
        segment_by_id: dict[str, Segment],
    ) -> tuple[RecognitionBlock, ...]:
        blocks: list[RecognitionBlock] = []
        for index, core in enumerate(cores):
            context: tuple[str, ...] = ()
            if index:
                previous = cores[index - 1].segment_ids
                previous_segments = tuple(segment_by_id[item] for item in previous)
                for candidate in self._context_candidates(previous_segments):
                    member_segments = tuple(segment_by_id[item] for item in candidate + core.segment_ids)
                    if (
                        self._padded_bbox(
                            member_segments,
                            aligned_size=aligned_size,
                        ).area
                        <= self.config.max_block_pixels
                    ):
                        context = candidate
                        break
                if not context:
                    raise BlockPlanningLimitError(f"required overlap cannot fit block-{index:06d}")
            member_ids = context + core.segment_ids
            if len(member_ids) > self.config.max_block_segments:
                raise BlockPlanningLimitError(
                    "block segment membership exceeds configured limit "
                    f"{self.config.max_block_segments}"
                )
            member_segments = tuple(segment_by_id[item] for item in member_ids)
            bbox = self._padded_bbox(member_segments, aligned_size=aligned_size)
            blocks.append(
                RecognitionBlock(
                    block_id=f"block-{index:06d}",
                    bbox=bbox,
                    core_segment_ids=core.segment_ids,
                    segment_ids=member_ids,
                    context_segment_ids=context,
                    object_ids=core.object_ids,
                )
            )
        return tuple(blocks)

    def _context_candidates(
        self,
        previous_segments: tuple[Segment, ...],
    ) -> tuple[tuple[str, ...], ...]:
        rows = self._row_groups(previous_segments)
        start = len(rows) - 1
        selected = len(rows[-1])
        while start > 0 and selected < self.config.context_segments:
            start -= 1
            selected += len(rows[start])
        return tuple(
            tuple(segment.segment_id for row in rows[index:] for segment in row) for index in range(start, len(rows))
        )

    @staticmethod
    def _row_groups(segments: tuple[Segment, ...]) -> tuple[tuple[Segment, ...], ...]:
        groups: list[list[Segment]] = []
        current_top = 0
        current_bottom = 0
        for segment in segments:
            if not groups:
                groups.append([segment])
                current_top = segment.bbox.top
                current_bottom = segment.bbox.bottom
                continue
            if max(current_top, segment.bbox.top) < min(current_bottom, segment.bbox.bottom):
                groups[-1].append(segment)
                current_top = min(current_top, segment.bbox.top)
                current_bottom = max(current_bottom, segment.bbox.bottom)
            else:
                groups.append([segment])
                current_top = segment.bbox.top
                current_bottom = segment.bbox.bottom
        return tuple(tuple(group) for group in groups)

    @staticmethod
    def _vertical_envelopes_overlap(
        first: tuple[Segment, ...],
        second: tuple[Segment, ...],
    ) -> bool:
        first_top = min(item.bbox.top for item in first)
        first_bottom = max(item.bbox.bottom for item in first)
        second_top = min(item.bbox.top for item in second)
        second_bottom = max(item.bbox.bottom for item in second)
        return max(first_top, second_top) < min(first_bottom, second_bottom)

    def _padded_bbox(
        self,
        segments: tuple[Segment, ...],
        *,
        aligned_size: tuple[int, int],
    ) -> Box:
        bbox = Box.union(item.bbox for item in segments)
        width, height = aligned_size
        return Box(
            0,
            max(0, bbox.top - self.config.padding),
            width,
            min(height, bbox.bottom + self.config.padding),
        )

    def _spatial_padded_bbox(
        self,
        segments: tuple[Segment, ...],
        *,
        excluded_segments: tuple[Segment, ...] = (),
        aligned_size: tuple[int, int],
        work_budget: _WorkBudget | None = None,
    ) -> Box:
        member_bbox = Box.union(item.bbox for item in segments)
        return _clamped_spatial_bbox(
            member_bbox,
            excluded_segments=excluded_segments,
            aligned_size=aligned_size,
            padding=self.config.padding,
            work_budget=work_budget,
        )

    def _spatial_membership_closure(
        self,
        initial_ids: set[str],
        *,
        ordered_segments: tuple[Segment, ...],
        max_checks: int,
    ) -> tuple[set[str], int]:
        """Close a spatial crop over every segment visible in its rectangle.

        Closure uses the unpadded union only.  Padding is applied afterwards
        and clamped separately, so padding cannot pull a neighbouring row or
        column into the block recursively.
        """

        member_ids = set(initial_ids)
        if not member_ids:
            raise BlockPlanningInvariantError(
                "spatial block requires at least one initial segment"
            )
        segment_by_id = {
            item.segment_id: item for item in ordered_segments
        }
        checks = 0
        while True:
            if len(member_ids) > self.config.max_block_segments:
                raise BlockPlanningLimitError(
                    "spatial block segment membership exceeds configured "
                    f"limit {self.config.max_block_segments}"
                )
            member_bbox = Box.union(
                segment_by_id[item].bbox for item in member_ids
            )
            checks += len(ordered_segments)
            if checks > max_checks:
                raise BlockPlanningLimitError(
                    "matrix visible-closure checks exceed configured candidate "
                    "work limit"
                )
            visible_ids = {
                item.segment_id
                for item in ordered_segments
                if item.bbox.intersection(member_bbox) is not None
            }
            expanded = member_ids | visible_ids
            if expanded == member_ids:
                return member_ids, checks
            member_ids = expanded

    def _adjacent_algebra(
        self,
        blocks: tuple[RecognitionBlock, ...],
        source_ids: tuple[str, ...],
    ) -> tuple[BlockSetAlgebra, ...]:
        values: list[BlockSetAlgebra] = []
        memberships = 0
        for first, second in zip(blocks, blocks[1:]):
            first_set = set(first.segment_ids)
            second_set = set(second.segment_ids)
            intersection = first_set & second_set
            if not intersection:
                raise BlockPlanningLimitError("adjacent recognition blocks do not overlap")
            union = first_set | second_set
            xor = first_set ^ second_set
            first_only = first_set - second_set
            second_only = second_set - first_set
            memberships += len(union)
            if memberships > self.config.max_pair_memberships:
                raise BlockPlanningLimitError(
                    f"block pair memberships exceed configured limit {self.config.max_pair_memberships}"
                )
            values.append(
                BlockSetAlgebra(
                    first_block_id=first.block_id,
                    second_block_id=second.block_id,
                    intersection_segment_ids=self._ordered_subset(source_ids, intersection),
                    union_segment_ids=self._ordered_subset(source_ids, union),
                    xor_segment_ids=self._ordered_subset(source_ids, xor),
                    first_only_segment_ids=self._ordered_subset(source_ids, first_only),
                    second_only_segment_ids=self._ordered_subset(source_ids, second_only),
                )
            )
        return tuple(values)

    @staticmethod
    def _ordered_subset(
        source_ids: tuple[str, ...],
        selected: set[str],
    ) -> tuple[str, ...]:
        return tuple(item for item in source_ids if item in selected)


__all__ = [
    "BlockPlan",
    "BlockPlanStatus",
    "BlockPlanningConfig",
    "BlockPlanningMode",
    "BlockPlanningInvariantError",
    "BlockPlanningLimitError",
    "BlockSetAlgebra",
    "MembershipUnit",
    "MembershipUnitKind",
    "OverlappingBlockPlanner",
    "RecognitionBlock",
    "sparse_matrix_payload",
    "sparse_matrix_sha256",
]
