#!/usr/bin/env python3
"""Recognize stored Stage 6 objects without replaying earlier stages.

The input boundary is deliberately narrow: an item directory containing
``01-geometry`` and ``06-objects``.  The debug output keeps every boundary
observable instead of publishing one ambiguous ``blocks`` directory:

* ``01-find-object``: stored object PNG, kind, metadata, and local matrix;
* ``02-separate-block``: block PNGs and exact source-segment memberships;
* ``03-ocr-blocks``: OCR evidence for each block;
* ``04-get-segment``: OR/XOR/AND fusion back to segment text;
* ``05-generate-object``: text assembled for each source object.

Every object-local sparse matrix is adapted to the production Stage 5 planner
and cropped from the already stored object image.  Tables use dyadic
non-Cartesian cell masks; paragraph/list objects compare complete context
with overlapping line pairs.

* tables: ``matrix-orxor`` -- matrix-native 2-D blocks plus OR/XOR/AND;
* paragraphs/lists A/B: ``whole-object`` versus ``line-windows``.

The published tree uses semantic names and one engine log.  Legacy numeric
stage directories remain immutable inputs, so this experiment cannot hide a
failure by silently rebuilding geometry or objects.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace as dataclass_replace
from pathlib import Path
from typing import Iterable, Mapping, Protocol

import numpy as np
from PIL import Image, ImageDraw

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "ocr"))

from app.sparse_pipeline.block_crops import BlockCropPair, BlockCropper  # noqa: E402
from app.sparse_pipeline.block_planning import (  # noqa: E402
    BlockPlan,
    BlockPlanningConfig,
    BlockPlanningMode,
    BlockSetAlgebra,
    MembershipUnit,
    MembershipUnitKind,
    OverlappingBlockPlanner,
    RecognitionBlock,
    sparse_matrix_sha256,
)
from app.sparse_pipeline.contracts import (  # noqa: E402
    AxisInterval,
    Box,
    Segment,
    SegmentKind,
    SegmentSpan,
    SparseCell,
    SparseCoordinateMode,
    SparseSegmentMatrix,
    SparseStructuralCode,
)
from app.sparse_pipeline.crop_enhancement import CropInput  # noqa: E402
from app.sparse_pipeline.object_reconstruction import (  # noqa: E402
    DocumentObject,
    ObjectKind,
    ObjectReconstructionResult,
    SegmentObjectOwnership,
)
from app.sparse_pipeline.ocr_adapters import (  # noqa: E402
    TesseractConfig,
    make_tesseract_lane,
)
from app.sparse_pipeline.ocr_fusion import (  # noqa: E402
    OcrEvidenceFusion,
    OcrFusionConfig,
    OcrRoutingMode,
    align_exact_text,
    compact_ocr_text,
)
from app.sparse_pipeline.quality_metrics import exact_levenshtein  # noqa: E402
from app.sparse_pipeline.ocr_queue import (  # noqa: E402
    OcrJobResult,
    OcrJobStatus,
    OcrQueueResult,
    OcrTransform,
)
from app.sparse_pipeline.adaptive_language_ocr import (  # noqa: E402
    AdaptivePersistentOcrSession as PersistentOcrSession,
    BlockCompaction,
    build_deferred_compact_crops,
)


SCHEMA = "object-recognition-debug-v1"
POLICIES = (
    "matrix-orxor",
    "fixed-flow",
    "whole-object",
    "line-windows",
)


class ObjectRecognitionInvariantError(ValueError):
    """Raised when saved object evidence is inconsistent or incomplete."""


class OcrSession(Protocol):
    def run(
        self,
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
    ) -> OcrQueueResult: ...


@dataclass(frozen=True)
class StoredObject:
    source_object_id: str
    source_kind: str
    source_dir: Path
    image_png: bytes
    aligned_size: tuple[int, int]
    record: Mapping[str, object]
    matrix_payload: Mapping[str, object]
    segments: tuple[Segment, ...]
    matrix: SparseSegmentMatrix
    objects_result: ObjectReconstructionResult


@dataclass(frozen=True)
class SeparatedBlocks:
    policy: str
    plan: BlockPlan
    crops: tuple[BlockCropPair, ...]
    planning_seconds: float
    crop_seconds: float
    compactions: tuple[BlockCompaction, ...] = ()


@dataclass(frozen=True)
class BlockOcrResult:
    separated: SeparatedBlocks
    queue: OcrQueueResult
    ocr_seconds: float
    cache_metrics: Mapping[str, int | float] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyRun:
    policy: str
    plan: BlockPlan
    crops: tuple[BlockCropPair, ...]
    queue: OcrQueueResult
    segment_lines: tuple[str, ...]
    result_text: str
    unresolved_units: int
    planning_seconds: float
    crop_seconds: float
    ocr_seconds: float
    fusion_seconds: float
    fusion_status: str
    fusion_error: str = ""
    cache_metrics: Mapping[str, int | float] = field(default_factory=dict)

    @property
    def total_seconds(self) -> float:
        return (
            self.planning_seconds
            + self.crop_seconds
            + self.ocr_seconds
            + self.fusion_seconds
        )


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ObjectRecognitionInvariantError(f"JSON root must be an object: {path}")
    return value


def _read_jsonl(path: Path) -> tuple[Mapping[str, object], ...]:
    values: list[Mapping[str, object]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ObjectRecognitionInvariantError(
                f"JSONL record {line_number} must be an object: {path}"
            )
        values.append(value)
    return tuple(values)


def _int_tuple(value: object, *, name: str, size: int) -> tuple[int, ...]:
    if (
        not isinstance(value, list)
        or len(value) != size
        or any(type(item) is not int for item in value)
    ):
        raise ObjectRecognitionInvariantError(
            f"{name} must contain {size} integers"
        )
    return tuple(value)


def _box(value: object, *, name: str) -> Box:
    if isinstance(value, dict):
        coordinates = tuple(
            value.get(key) for key in ("left", "top", "right", "bottom")
        )
        if any(type(item) is not int for item in coordinates):
            raise ObjectRecognitionInvariantError(f"{name} is invalid")
        return Box(*coordinates)  # type: ignore[arg-type]
    return Box(*_int_tuple(value, name=name, size=4))


def object_matrix_to_sparse(
    payload: Mapping[str, object],
    *,
    source_segment_ids: tuple[str, ...],
) -> SparseSegmentMatrix:
    """Adapt one saved object-local matrix to the production Stage 5 type."""

    if payload.get("schema") != "object-local-sparse-topology-v1":
        raise ObjectRecognitionInvariantError("unsupported object matrix schema")
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ObjectRecognitionInvariantError("object matrix must contain rows")
    rows: list[Mapping[str, object]] = []
    column_count = 0
    for expected_row, raw_row in enumerate(raw_rows):
        if not isinstance(raw_row, dict) or raw_row.get("row") != expected_row:
            raise ObjectRecognitionInvariantError(
                "object matrix rows must be contiguous and canonical"
            )
        raw_segments = raw_row.get("segments")
        if not isinstance(raw_segments, list):
            raise ObjectRecognitionInvariantError(
                "object matrix row segments must be a list"
            )
        compressed = raw_row.get("compressed_codes")
        if not isinstance(compressed, list) or not compressed:
            raise ObjectRecognitionInvariantError(
                "object matrix row must retain compressed codes"
            )
        real_code_count = len(compressed) - int(compressed[-1] is None)
        tail = raw_row.get("null_tail")
        tail_stop = 0
        if isinstance(tail, dict):
            through = tail.get("through_column_exclusive")
            if type(through) is int:
                tail_stop = through
        segment_stop = max(
            (
                int(item["column"]) + 1
                for item in raw_segments
                if isinstance(item, dict) and type(item.get("column")) is int
            ),
            default=0,
        )
        column_count = max(column_count, real_code_count, tail_stop, segment_stop)
        rows.append(raw_row)
    if column_count < 1:
        raise ObjectRecognitionInvariantError("object matrix has no columns")

    saved_row_intervals: list[tuple[int, int]] = []
    for row_index, row in enumerate(rows):
        start, stop = _int_tuple(
            row.get("matrix_y"),
            name=f"matrix row {row_index} pixel interval",
            size=2,
        )
        if start < 0 or stop <= start or (
            saved_row_intervals
            and saved_row_intervals[-1][1] > start
        ):
            raise ObjectRecognitionInvariantError(
                "object matrix pixel rows must be positive and non-overlapping"
            )
        saved_row_intervals.append((start, stop))
    # SparseSegmentMatrix axes are exact partitions.  Saved object rows may
    # have whitespace gaps, so absorb leading whitespace into the first row
    # and each internal gap into the preceding interval.  This avoids
    # inventing a payload row or changing the stored image.
    row_intervals = [
        AxisInterval(
            index,
            0 if index == 0 else start,
            (
                saved_row_intervals[index + 1][0]
                if index + 1 < len(saved_row_intervals)
                else stop
            ),
        )
        for index, (start, stop) in enumerate(saved_row_intervals)
    ]

    column_pixels: dict[int, tuple[int, int]] = {}
    consistent_column_pixels = True
    for row in rows:
        raw_segments = row["segments"]
        assert isinstance(raw_segments, list)
        for raw_slot in raw_segments:
            if (
                not isinstance(raw_slot, dict)
                or type(raw_slot.get("column")) is not int
            ):
                raise ObjectRecognitionInvariantError("matrix slot is invalid")
            column = int(raw_slot["column"])
            interval = _int_tuple(
                raw_slot.get("matrix_x"),
                name=f"matrix column {column} pixel interval",
                size=2,
            )
            previous = column_pixels.setdefault(column, (interval[0], interval[1]))
            if previous != interval:
                consistent_column_pixels = False
    consistent_column_pixels = consistent_column_pixels and set(
        column_pixels
    ) == set(range(column_count))
    if consistent_column_pixels:
        column_intervals = tuple(
            AxisInterval(index, *column_pixels[index])
            for index in range(column_count)
        )
        consistent_column_pixels = (
            column_intervals[0].start == 0
            and all(
                first.end == second.start
                for first, second in zip(
                    column_intervals, column_intervals[1:]
                )
            )
        )
    if not consistent_column_pixels:
        # Flow objects can have row-local x tracks.  They are recognized as
        # one whole object and retain literal logical ordinals; only a table
        # with a stable shared x axis may use physical 2-D window crops.
        column_intervals = tuple(
            AxisInterval(index, index, index + 1)
            for index in range(column_count)
        )

    known = set(source_segment_ids)
    cells: list[SparseCell] = []
    structural: list[SparseStructuralCode] = []
    for row_index, row in enumerate(rows):
        raw_segments = row["segments"]
        assert isinstance(raw_segments, list)
        for raw_slot in raw_segments:
            if not isinstance(raw_slot, dict):
                raise ObjectRecognitionInvariantError("matrix slot must be an object")
            column = raw_slot.get("column")
            code = raw_slot.get("code")
            source_ids = raw_slot.get("source_segment_ids")
            if (
                type(column) is not int
                or column < 0
                or column >= column_count
                or type(code) is not int
                or code < 0
                or not isinstance(source_ids, list)
                or any(type(item) is not str for item in source_ids)
            ):
                raise ObjectRecognitionInvariantError("matrix slot is invalid")
            if len(source_ids) != len(set(source_ids)) or not set(source_ids).issubset(
                known
            ):
                raise ObjectRecognitionInvariantError(
                    "matrix slot contains duplicate or foreign source segments"
                )
            for segment_id in source_ids:
                cells.append(SparseCell(row_index, column, segment_id))
                if code:
                    structural.append(
                        SparseStructuralCode(row_index, column, code, segment_id)
                    )

    cells_tuple = tuple(
        sorted(cells, key=lambda item: (item.row, item.column, item.segment_id))
    )
    represented = {item.segment_id for item in cells_tuple}
    if represented != known:
        missing = tuple(item for item in source_segment_ids if item not in represented)
        extra = tuple(sorted(represented - known))
        raise ObjectRecognitionInvariantError(
            "object matrix must represent every owned source segment: "
            f"missing={missing[:8]!r} extra={extra[:8]!r}"
        )
    spans = tuple(
        SegmentSpan(
            segment_id,
            min(item.row for item in cells_tuple if item.segment_id == segment_id),
            max(item.row for item in cells_tuple if item.segment_id == segment_id) + 1,
            min(item.column for item in cells_tuple if item.segment_id == segment_id),
            max(item.column for item in cells_tuple if item.segment_id == segment_id)
            + 1,
        )
        for segment_id in source_segment_ids
    )
    projection_sha256 = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    return SparseSegmentMatrix(
        rows=tuple(row_intervals),
        columns=column_intervals,
        cells=cells_tuple,
        spans=spans,
        coordinate_mode=SparseCoordinateMode.LOGICAL_PROJECTION,
        structural_codes=tuple(
            sorted(
                structural,
                key=lambda item: (item.row, item.column, item.segment_id, item.code),
            )
        ),
        projection_sha256=projection_sha256,
    )


def _segment_kind(value: object) -> SegmentKind:
    try:
        return SegmentKind(str(value))
    except ValueError:
        return SegmentKind.UNKNOWN


def load_stored_object(
    object_dir: Path,
    *,
    segments_path: Path,
) -> StoredObject:
    record = _read_json(object_dir / "object.json")
    matrix_payload = _read_json(object_dir / "matrix.json")
    source_object_id = record.get("object_id")
    source_kind = record.get("kind")
    raw_source_ids = record.get("segment_ids")
    if (
        type(source_object_id) is not str
        or not source_object_id
        or type(source_kind) is not str
        or not isinstance(raw_source_ids, list)
        or not raw_source_ids
        or any(type(item) is not str for item in raw_source_ids)
    ):
        raise ObjectRecognitionInvariantError("object.json identity is invalid")
    source_ids = tuple(raw_source_ids)
    if len(source_ids) != len(set(source_ids)):
        raise ObjectRecognitionInvariantError("object segment IDs must be unique")
    object_bbox = _box(record.get("bbox"), name="object bbox")
    image_png = (object_dir / "object.png").read_bytes()
    with Image.open(io.BytesIO(image_png)) as opened:
        opened.load()
        if opened.format != "PNG":
            raise ObjectRecognitionInvariantError("stored object image must be PNG")
        aligned_size = opened.size
    if aligned_size != (object_bbox.width, object_bbox.height):
        raise ObjectRecognitionInvariantError(
            "object image dimensions disagree with object bbox"
        )

    geometry_by_id = {
        str(item.get("segment_id")): item for item in _read_jsonl(segments_path)
    }
    if any(segment_id not in geometry_by_id for segment_id in source_ids):
        raise ObjectRecognitionInvariantError(
            "object references a segment absent from geometry"
        )
    matrix = object_matrix_to_sparse(
        matrix_payload,
        source_segment_ids=source_ids,
    )
    span_by_id = {item.segment_id: item for item in matrix.spans}
    segments: list[Segment] = []
    for segment_id in source_ids:
        source = geometry_by_id[segment_id]
        page_bbox = _box(source.get("bbox"), name=f"{segment_id} bbox")
        local_bbox = Box(
            page_bbox.left - object_bbox.left,
            page_bbox.top - object_bbox.top,
            page_bbox.right - object_bbox.left,
            page_bbox.bottom - object_bbox.top,
        )
        canvas = Box(0, 0, *aligned_size)
        if local_bbox.intersection(canvas) != local_bbox:
            raise ObjectRecognitionInvariantError(
                f"{segment_id} lies outside its stored object image"
            )
        span = span_by_id[segment_id]
        raw_parent_path = source.get("parent_path")
        parent_path = (
            tuple(raw_parent_path)
            if isinstance(raw_parent_path, list)
            and raw_parent_path
            and all(type(item) is str and item for item in raw_parent_path)
            else (source_object_id,)
        )
        raw_components = source.get("component_ids", [])
        if not isinstance(raw_components, list) or any(
            type(item) is not int or item < 0 for item in raw_components
        ):
            raise ObjectRecognitionInvariantError(
                f"{segment_id} component IDs are invalid"
            )
        ink_pixels = source.get("ink_pixels")
        if type(ink_pixels) is not int or ink_pixels < 1:
            raise ObjectRecognitionInvariantError(
                f"{segment_id} ink pixel count is invalid"
            )
        segments.append(
            Segment(
                segment_id=segment_id,
                bbox=local_bbox,
                source_bbox=local_bbox,
                kind=_segment_kind(source.get("kind")),
                ink_pixels=ink_pixels,
                row_index=span.row_start,
                order_key=(span.row_start, span.column_start),
                parent_path=parent_path,
                component_ids=tuple(raw_components),
            )
        )
    segment_tuple = tuple(segments)
    local_object_bbox = Box.union(item.bbox for item in segment_tuple)
    # Stage 6 object crops may retain structural rule pixels around the owned
    # payload segments.  Those pixels are valid OCR context, so the segment
    # union only has to stay inside the immutable object image; requiring it to
    # fill the entire crop rejects ruled tables with a narrow lattice margin.
    # Each translated segment was checked against this canvas above, and the
    # production planner deliberately receives the payload union as the
    # DocumentObject bbox while it keeps the full crop as ``aligned_size``.
    kind = {
        "paragraph": ObjectKind.PARAGRAPH,
        "list": ObjectKind.LIST,
        "table": ObjectKind.TABLE,
        "flow": ObjectKind.UNKNOWN,
    }.get(source_kind, ObjectKind.UNKNOWN)
    document_object = DocumentObject(
        object_id="object-000000",
        kind=kind,
        segment_ids=source_ids,
        bbox=local_object_bbox,
        reading_index=0,
        row_start=min(item.row_start for item in matrix.spans),
        row_stop=max(item.row_stop for item in matrix.spans),
        column_start=min(item.column_start for item in matrix.spans),
        column_stop=max(item.column_stop for item in matrix.spans),
        confidence=1.0,
        evidence=tuple(
            item
            for item in record.get("evidence", [])
            if type(item) is str and item
        ),
    )
    objects_result = ObjectReconstructionResult(
        aligned_size=aligned_size,
        source_segment_ids=source_ids,
        objects=(document_object,),
        segment_ownership=tuple(
            SegmentObjectOwnership(segment_id, "object-000000")
            for segment_id in source_ids
        ),
    )
    return StoredObject(
        source_object_id=source_object_id,
        source_kind=source_kind,
        source_dir=object_dir,
        image_png=image_png,
        aligned_size=aligned_size,
        record=record,
        matrix_payload=matrix_payload,
        segments=segment_tuple,
        matrix=matrix,
        objects_result=objects_result,
    )


def matrix_orxor_plan(stored: StoredObject) -> BlockPlan:
    return OverlappingBlockPlanner(
        BlockPlanningConfig(
            mode=BlockPlanningMode.SPATIAL_2D,
            object_local=False,
            adaptive_table_windows=True,
            padding=8,
        )
    ).plan(
        aligned_size=stored.aligned_size,
        segments=stored.segments,
        objects_result=stored.objects_result,
        matrix=stored.matrix,
    )


def whole_object_plan(stored: StoredObject) -> BlockPlan:
    source_ids = tuple(item.segment_id for item in stored.segments)
    block_id = "block-000000"
    scope_id = "scope-000000"
    return BlockPlan(
        aligned_size=stored.aligned_size,
        source_segment_ids=source_ids,
        blocks=(
            RecognitionBlock(
                block_id=block_id,
                bbox=Box(0, 0, *stored.aligned_size),
                core_segment_ids=source_ids,
                segment_ids=source_ids,
                context_segment_ids=(),
                object_ids=("object-000000",),
                scope_id=scope_id,
            ),
        ),
        adjacent_algebra=(),
        diagnostics=(
            "mode=whole-object-control",
            "context=complete-object",
        ),
        mode=BlockPlanningMode.SPATIAL_2D,
        membership_units=(
            MembershipUnit(
                unit_id="membership-unit-000000",
                kind=(
                    MembershipUnitKind.SEGMENT
                    if len(source_ids) == 1
                    else MembershipUnitKind.SUBBLOCK
                ),
                segment_ids=source_ids,
                block_ids=(block_id,),
                scope_id=scope_id,
            ),
        ),
        matrix_sha256=sparse_matrix_sha256(stored.matrix),
    )


def line_windows_plan(stored: StoredObject) -> BlockPlan:
    """Build [line 1 + line 2], [line 2 + line 3] OCR contexts."""

    source_ids = tuple(item.segment_id for item in stored.segments)
    source_order = {
        segment_id: index for index, segment_id in enumerate(source_ids)
    }
    span_by_id = {item.segment_id: item for item in stored.matrix.spans}
    if set(span_by_id) != set(source_ids):
        raise ObjectRecognitionInvariantError(
            "line-window matrix spans disagree with object segments"
        )
    grouped: dict[tuple[int, int], list[str]] = {}
    for segment_id in source_ids:
        span = span_by_id[segment_id]
        grouped.setdefault((span.row_start, span.row_stop), []).append(
            segment_id
        )
    lines = tuple(
        tuple(sorted(segment_ids, key=source_order.__getitem__))
        for _row_span, segment_ids in sorted(grouped.items())
    )
    if not lines:
        raise ObjectRecognitionInvariantError("flow object has no matrix lines")
    line_pairs = (
        (lines[0],)
        if len(lines) == 1
        else tuple((first, second) for first, second in zip(lines, lines[1:]))
    )
    memberships = tuple(
        tuple(
            segment_id
            for segment_id in source_ids
            if any(segment_id in line for line in pair)
        )
        for pair in line_pairs
    )
    carrier_by_segment = {
        segment_id: max(
            index
            for index, member_ids in enumerate(memberships)
            if segment_id in member_ids
        )
        for segment_id in source_ids
    }
    segment_by_id = {item.segment_id: item for item in stored.segments}
    scope_id = "scope-000000"
    width, _height = stored.aligned_size
    blocks: list[RecognitionBlock] = []
    for index, member_ids in enumerate(memberships):
        core_ids = tuple(
            segment_id
            for segment_id in source_ids
            if carrier_by_segment[segment_id] == index
        )
        member_bbox = Box.union(
            segment_by_id[segment_id].bbox for segment_id in member_ids
        )
        core_set = set(core_ids)
        blocks.append(
            RecognitionBlock(
                block_id=f"block-{index:06d}",
                bbox=Box(0, member_bbox.top, width, member_bbox.bottom),
                core_segment_ids=core_ids,
                segment_ids=member_ids,
                context_segment_ids=tuple(
                    segment_id
                    for segment_id in member_ids
                    if segment_id not in core_set
                ),
                object_ids=("object-000000",) if core_ids else (),
                scope_id=scope_id,
            )
        )
    block_tuple = tuple(blocks)

    def canonical(values: set[str]) -> tuple[str, ...]:
        return tuple(item for item in source_ids if item in values)

    algebra: list[BlockSetAlgebra] = []
    for first, second in zip(block_tuple, block_tuple[1:]):
        first_ids = set(first.segment_ids)
        second_ids = set(second.segment_ids)
        intersection = first_ids & second_ids
        if not intersection:
            continue
        first_only = first_ids - second_ids
        second_only = second_ids - first_ids
        algebra.append(
            BlockSetAlgebra(
                first_block_id=first.block_id,
                second_block_id=second.block_id,
                intersection_segment_ids=canonical(intersection),
                union_segment_ids=canonical(first_ids | second_ids),
                xor_segment_ids=canonical(first_only | second_only),
                first_only_segment_ids=canonical(first_only),
                second_only_segment_ids=canonical(second_only),
            )
        )
    return BlockPlan(
        aligned_size=stored.aligned_size,
        source_segment_ids=source_ids,
        blocks=block_tuple,
        adjacent_algebra=tuple(algebra),
        diagnostics=(
            "mode=overlapping-line-pairs",
            f"matrix-lines={len(lines)}",
            "single-segment-ocr=forbidden",
        ),
        mode=BlockPlanningMode.SPATIAL_2D,
        membership_units=OverlappingBlockPlanner._membership_units(
            blocks=block_tuple,
            source_ids=source_ids,
        ),
        matrix_sha256=sparse_matrix_sha256(stored.matrix),
    )


def _best_job_by_block(queue: OcrQueueResult) -> dict[str, OcrJobResult]:
    candidates: dict[str, list[OcrJobResult]] = {}
    for job in queue.jobs:
        if job.status is OcrJobStatus.COMPLETE and job.output is not None:
            candidates.setdefault(job.block_id, []).append(job)

    def score(job: OcrJobResult) -> tuple[int, float, int, int]:
        assert job.output is not None
        confidence = (
            sum(item.confidence for item in job.output.words) / len(job.output.words)
            if job.output.words
            else 0.0
        )
        return (
            int(bool(job.output.text.strip())),
            confidence,
            len(compact_ocr_text(job.output.text)),
            int(job.transform is OcrTransform.RAW),
        )

    return {
        block_id: max(values, key=score) for block_id, values in candidates.items()
    }


def _fusion_lines(
    plan: BlockPlan,
    fusion: object,
) -> tuple[tuple[str, ...], str, int]:
    segment_by_id = {item.segment_id: item for item in fusion.segments}
    group_by_id = {item.unit_id: item for item in fusion.segment_groups}
    lines: list[str] = []
    text_parts: list[str] = []
    unresolved = 0
    for unit in plan.membership_units:
        selected = (
            segment_by_id[unit.segment_ids[0]]
            if unit.kind is MembershipUnitKind.SEGMENT
            else group_by_id[unit.unit_id]
        )
        text = selected.selected_text
        is_unresolved = selected.unresolved or text is None
        unresolved += int(is_unresolved)
        label = (
            unit.segment_ids[0]
            if unit.kind is MembershipUnitKind.SEGMENT
            else "+".join(unit.segment_ids)
        )
        rendered = "[UNRESOLVED]" if text is None else text.replace("\t", " ")
        lines.append(f"{label}\t{rendered}")
        if text is not None and text.strip():
            text_parts.append(text.strip())
    return tuple(lines), "\n".join(text_parts), unresolved


def separate_blocks(
    stored: StoredObject,
    *,
    policy: str,
) -> SeparatedBlocks:
    planning_started = time.perf_counter()
    if policy == "matrix-orxor":
        plan = matrix_orxor_plan(stored)
    elif policy in {"whole-object", "fixed-flow"}:
        plan = whole_object_plan(stored)
    elif policy == "line-windows":
        plan = line_windows_plan(stored)
    else:
        raise ValueError(f"unknown policy: {policy}")
    planning_seconds = time.perf_counter() - planning_started

    crop_started = time.perf_counter()
    ownership: np.ndarray | None = None
    ownership_segment_ids: tuple[str, ...] | None = None
    ownership_path = stored.source_dir / "ownership.png"
    if policy == "matrix-orxor":
        if not ownership_path.is_file():
            raise ObjectRecognitionInvariantError(
                f"table debug crop requires literal ownership: {ownership_path}"
            )
        with Image.open(ownership_path) as opened:
            opened.load()
            if opened.format != "PNG" or opened.size != stored.aligned_size:
                raise ObjectRecognitionInvariantError(
                    "stored ownership PNG disagrees with the object image"
                )
            ownership_rgb = np.asarray(opened.convert("RGB"))
        ownership = np.full(
            (stored.aligned_size[1], stored.aligned_size[0]),
            -1,
            dtype=np.int32,
        )
        ownership_segment_ids = tuple(
            segment.segment_id for segment in stored.segments
        )
        for label, segment in enumerate(stored.segments):
            suffix = segment.segment_id.removeprefix("segment-")
            if suffix == segment.segment_id or not suffix.isdigit():
                raise ObjectRecognitionInvariantError(
                    "stored ownership decoding requires canonical segment IDs"
                )
            digest = hashlib.sha256(
                f"segment-{int(suffix)}".encode("ascii")
            ).digest()
            color = (
                64 + digest[0] // 2,
                64 + digest[1] // 2,
                64 + digest[2] // 2,
            )
            bbox = segment.bbox
            region = ownership_rgb[
                bbox.top : bbox.bottom,
                bbox.left : bbox.right,
            ]
            matches = np.all(
                region == np.asarray(color, dtype=np.uint8),
                axis=2,
            )
            if not np.any(matches):
                raise ObjectRecognitionInvariantError(
                    f"stored ownership has no pixels for {segment.segment_id}"
                )
            target = ownership[
                bbox.top : bbox.bottom,
                bbox.left : bbox.right,
            ]
            if np.any(target[matches] >= 0):
                raise ObjectRecognitionInvariantError(
                    "stored ownership assigns one pixel to multiple segments"
                )
            target[matches] = label

    crops, compactions = build_deferred_compact_crops(
        stored.image_png,
        aligned_size=stored.aligned_size,
        plan=plan,
        ownership=ownership,
        ownership_segment_ids=ownership_segment_ids,
        segment_spans=stored.matrix.spans,
        segment_bboxes={
            segment.segment_id: segment.bbox for segment in stored.segments
        },
    )
    crop_seconds = time.perf_counter() - crop_started
    return SeparatedBlocks(
        policy=policy,
        plan=plan,
        crops=crops,
        planning_seconds=planning_seconds,
        crop_seconds=crop_seconds,
        compactions=compactions,
    )


def canonicalize_locality_separated(
    stored: StoredObject,
    separated: SeparatedBlocks,
) -> SeparatedBlocks:
    """Re-render a persisted local plan through the canonical renderer.

    Debug handoffs can originate in another worktree.  Their typed plan is
    authoritative, but their raster is not accepted unless current code can
    reproduce the same render contract without changing that plan.
    """

    if not any(
        block.matrix_window_kind
        in {"polar-local-full", "polar-local-signature"}
        for block in separated.plan.blocks
    ):
        return separated
    rendered = separate_blocks(stored, policy=separated.policy)
    expected_contract = (
        separated.plan.aligned_size,
        separated.plan.source_segment_ids,
        separated.plan.blocks,
        separated.plan.adjacent_algebra,
        separated.plan.membership_units,
        separated.plan.matrix_sha256,
    )
    actual_contract = (
        rendered.plan.aligned_size,
        rendered.plan.source_segment_ids,
        rendered.plan.blocks,
        rendered.plan.adjacent_algebra,
        rendered.plan.membership_units,
        rendered.plan.matrix_sha256,
    )
    if actual_contract != expected_contract:
        raise ObjectRecognitionInvariantError(
            "persisted locality plan disagrees with canonical renderer plan"
        )
    return dataclass_replace(
        separated,
        crops=rendered.crops,
        compactions=rendered.compactions,
        crop_seconds=rendered.crop_seconds,
    )


def ocr_blocks(
    separated: SeparatedBlocks,
    *,
    session: OcrSession,
) -> BlockOcrResult:
    cache_metrics = getattr(session, "cache_metrics", None)
    cache_before = cache_metrics() if callable(cache_metrics) else {}
    ocr_started = time.perf_counter()
    run_with_compaction = getattr(session, "run_with_compaction", None)
    if callable(run_with_compaction):
        queue = run_with_compaction(
            plan=separated.plan,
            crops=separated.crops,
            compactions=getattr(separated, "compactions", ()),
        )
    else:
        queue = session.run(plan=separated.plan, crops=separated.crops)
    wall_seconds = time.perf_counter() - ocr_started
    cache_after = cache_metrics() if callable(cache_metrics) else {}
    cache_delta = {
        key: cache_after[key] - cache_before.get(key, 0)
        for key in cache_after
        if key != "entries"
    }
    cache_delta["entries_added"] = (
        cache_after.get("entries", 0) - cache_before.get("entries", 0)
    )
    return BlockOcrResult(
        separated=separated,
        queue=queue,
        ocr_seconds=wall_seconds,
        cache_metrics=cache_delta,
    )


def _fusion_metadata_crops(
    separated: SeparatedBlocks,
) -> tuple[BlockCropPair, ...]:
    """Expose compact atlases to fusion in their original block geometry."""

    def metadata_view(
        crop: BlockCropPair,
        *,
        bbox: Box,
    ) -> BlockCropPair:
        # BlockCropPair construction correctly binds bbox dimensions to PNG
        # dimensions.  Fusion's membership-only contract instead requires the
        # original plan bbox while still hashing the immutable compact PNG.
        # Build a read-only view of the already validated crop rather than
        # mutating it or manufacturing different image/segment evidence.
        view = object.__new__(BlockCropPair)
        for field_name in BlockCropPair.__dataclass_fields__:
            object.__setattr__(
                view,
                field_name,
                bbox if field_name == "bbox" else getattr(crop, field_name),
            )
        return view

    if not separated.compactions:
        return separated.crops
    block_by_id = {
        block.block_id: block for block in separated.plan.blocks
    }
    compacted_block_ids = tuple(
        item.block_id for item in separated.compactions
    )
    if len(compacted_block_ids) != len(set(compacted_block_ids)):
        raise ObjectRecognitionInvariantError(
            "compacted OCR metadata contains duplicate block IDs"
        )
    unknown_block_ids = tuple(
        block_id
        for block_id in compacted_block_ids
        if block_id not in block_by_id
    )
    if unknown_block_ids:
        raise ObjectRecognitionInvariantError(
            "compacted OCR metadata references unknown blocks: "
            + ",".join(unknown_block_ids)
        )
    compacted = set(compacted_block_ids)
    normalized: list[BlockCropPair] = []
    for crop in separated.crops:
        if crop.block_id not in compacted:
            normalized.append(crop)
            continue
        block = block_by_id.get(crop.block_id)
        if block is None:
            raise ObjectRecognitionInvariantError(
                f"compacted crop references unknown block {crop.block_id}"
            )
        if crop.segment_ids != block.segment_ids:
            raise ObjectRecognitionInvariantError(
                f"compacted crop membership disagrees with {crop.block_id}"
            )
        normalized.append(
            metadata_view(crop, bbox=block.bbox)
        )
    missing_crop_ids = tuple(
        block_id
        for block_id in compacted_block_ids
        if all(crop.block_id != block_id for crop in separated.crops)
    )
    if missing_crop_ids:
        raise ObjectRecognitionInvariantError(
            "compacted OCR metadata has no crop for blocks: "
            + ",".join(missing_crop_ids)
        )
    return tuple(normalized)


def get_segments(
    stored: StoredObject,
    block_ocr: BlockOcrResult,
) -> PolicyRun:
    separated = block_ocr.separated
    policy = separated.policy
    plan = separated.plan
    crops = separated.crops
    queue = block_ocr.queue
    fusion_started = time.perf_counter()
    fusion_error = ""
    complete_block_ids = set(_best_job_by_block(queue))
    missing_block_ids = tuple(
        block.block_id
        for block in plan.blocks
        if block.block_id not in complete_block_ids
    )
    if missing_block_ids:
        segment_lines = ()
        result_text = ""
        unresolved = len(plan.membership_units)
        fusion_status = "skipped"
        fusion_error = (
            "upstream ocr-blocks failed for " + ",".join(missing_block_ids)
        )
    else:
        try:
            fusion_crops = _fusion_metadata_crops(separated)
            fusion = OcrEvidenceFusion(
                OcrFusionConfig(
                    routing_mode=OcrRoutingMode.BLOCK_MEMBERSHIP,
                    membership_assume_complete_observations=False,
                    require_exact_job_matrix=False,
                )
            ).fuse(
                plan=plan,
                segments=stored.segments,
                crops=fusion_crops,
                queue=queue,
            )
            segment_lines, result_text, unresolved = _fusion_lines(plan, fusion)
            fusion_status = fusion.status.value
        except Exception as exc:
            fusion_error = f"{type(exc).__name__}: {exc}"
            segment_lines = tuple(
                f"{'+'.join(unit.segment_ids)}\t[UNRESOLVED]"
                for unit in plan.membership_units
            )
            result_text = ""
            unresolved = len(plan.membership_units)
            fusion_status = "failed"
    fusion_seconds = time.perf_counter() - fusion_started

    # A one-block control deliberately retains the OCR engine's complete
    # paragraph text even when Stage 2 cannot split that block into individual
    # source segments.  The unresolved count still records that limitation.
    if policy in {"whole-object", "fixed-flow"}:
        best = _best_job_by_block(queue).get("block-000000")
        if best is not None and best.output is not None:
            result_text = best.output.text.strip()
            label = "+".join(plan.source_segment_ids)
            segment_lines = (f"{label}\t{result_text.replace(chr(9), ' ')}",)
            unresolved = 0
            fusion_status = "complete"
            fusion_error = ""
    return PolicyRun(
        policy=policy,
        plan=plan,
        crops=crops,
        queue=queue,
        segment_lines=segment_lines,
        result_text=result_text,
        unresolved_units=unresolved,
        planning_seconds=separated.planning_seconds,
        crop_seconds=separated.crop_seconds,
        ocr_seconds=block_ocr.ocr_seconds,
        fusion_seconds=fusion_seconds,
        fusion_status=fusion_status,
        fusion_error=fusion_error,
        cache_metrics=block_ocr.cache_metrics,
    )


def run_policy(
    stored: StoredObject,
    *,
    policy: str,
    session: OcrSession,
) -> PolicyRun:
    """Compatibility wrapper around the five observable debug boundaries."""

    separated = separate_blocks(stored, policy=policy)
    block_ocr = ocr_blocks(separated, session=session)
    return get_segments(stored, block_ocr)


def _algebra_payload(plan: BlockPlan) -> list[dict[str, object]]:
    return [
        {
            "first_block": item.first_block_id,
            "second_block": item.second_block_id,
            "and": list(item.intersection_segment_ids),
            "or": list(item.union_segment_ids),
            "xor": list(item.xor_segment_ids),
            "first_only": list(item.first_only_segment_ids),
            "second_only": list(item.second_only_segment_ids),
        }
        for item in plan.adjacent_algebra
    ]


def _object_artifact_stem(stored: StoredObject) -> str:
    return f"{stored.source_object_id}-{stored.source_kind}"


def _block_artifact_stem(index: int, block: RecognitionBlock) -> str:
    width = block.bbox.right - block.bbox.left
    height = block.bbox.bottom - block.bbox.top
    return (
        f"block-{index:06d}-segments-{len(block.segment_ids):03d}-"
        f"{width}x{height}"
    )


def _write_find_object_stage(root: Path, stored: StoredObject) -> None:
    output = root / "01-find-object" / "objects"
    stem = _object_artifact_stem(stored)
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{stem}.png").write_bytes(stored.image_png)
    _write_json(output / f"{stem}.object.json", stored.record)
    _write_json(output / f"{stem}.matrix.json", stored.matrix_payload)


def _write_separate_block_stage(
    root: Path,
    stored: StoredObject,
    separated: SeparatedBlocks,
) -> None:
    output = (
        root
        / "02-separate-block"
        / "objects"
        / _object_artifact_stem(stored)
        / separated.policy
    )
    output.mkdir(parents=True, exist_ok=True)
    block_rows: list[dict[str, object]] = []
    for index, (block, crop) in enumerate(
        zip(separated.plan.blocks, separated.crops),
        start=1,
    ):
        stem = _block_artifact_stem(index, block)
        (output / f"{stem}.png").write_bytes(crop.raw.png_bytes)
        row = {
            "file": f"{stem}.png",
            "source_block_id": block.block_id,
            "bbox": list(block.bbox.as_tuple()),
            "segment_count": len(block.segment_ids),
            "segment_ids": list(block.segment_ids),
            "core_segment_ids": list(block.core_segment_ids),
            "context_segment_ids": list(block.context_segment_ids),
            "matrix_window": (
                list(block.matrix_window)
                if block.matrix_window is not None
                else None
            ),
            "matrix_window_kind": block.matrix_window_kind,
            "compaction": next(
                (
                    {
                        "nonempty_units": len(item.placements),
                        "omitted_empty_units": list(item.omitted_empty_units),
                        "raster_kind": item.raster_kind.value,
                        "raw_sha256": item.raw_sha256,
                        "placements": [
                            {
                                "unit_id": placement.unit_id,
                                "segment_ids": list(placement.segment_ids),
                                "source_bbox": list(
                                    placement.source_bbox.as_tuple()
                                ),
                                "crop_bbox": list(
                                    placement.crop_bbox.as_tuple()
                                ),
                            }
                            for placement in item.placements
                        ],
                    }
                    for item in getattr(separated, "compactions", ())
                    if item.block_id == block.block_id
                ),
                None,
            ),
        }
        block_rows.append(row)
        _write_json(output / f"{stem}.json", row)
    _write_json(
        output / "manifest.json",
        {
            "object_id": stored.source_object_id,
            "object_kind": stored.source_kind,
            "policy": separated.policy,
            "source_segment_count": len(stored.segments),
            "block_count": len(separated.plan.blocks),
            "blocks": block_rows,
            "adjacent_algebra": _algebra_payload(separated.plan),
            "diagnostics": list(separated.plan.diagnostics),
        },
    )


def _write_ocr_blocks_stage(
    root: Path,
    stored: StoredObject,
    block_ocr: BlockOcrResult,
) -> None:
    separated = block_ocr.separated
    output = (
        root
        / "03-ocr-blocks"
        / "objects"
        / _object_artifact_stem(stored)
        / separated.policy
    )
    output.mkdir(parents=True, exist_ok=True)
    best_by_block = _best_job_by_block(block_ocr.queue)
    for index, block in enumerate(separated.plan.blocks, start=1):
        stem = _block_artifact_stem(index, block)
        best = best_by_block.get(block.block_id)
        recognized = (
            best.output.text.strip()
            if best is not None and best.output is not None
            else ""
        )
        (output / f"{stem}.txt").write_text(
            recognized + ("\n" if recognized else ""),
            encoding="utf-8",
        )
        (output / f"{stem}.md").write_text(
            f"# {stem}\n\n"
            f"- Profile: `{best.lane_id if best is not None else 'none'}`\n"
            f"- Transform: "
            f"`{best.transform.value if best is not None else 'none'}`\n\n"
            f"```text\n{recognized}\n```\n",
            encoding="utf-8",
        )
        _write_json(
            output / f"{stem}.jobs.json",
            [
                {
                    "job_id": job.job_id,
                    "transform": job.transform.value,
                    "status": job.status.value,
                    "error_type": job.error_type,
                    "error_message": job.error_message,
                }
                for job in block_ocr.queue.jobs
                if job.block_id == block.block_id
            ],
        )


def _write_get_segment_stage(
    root: Path,
    stored: StoredObject,
    run: PolicyRun,
) -> None:
    output = (
        root
        / "04-get-segment"
        / "objects"
        / _object_artifact_stem(stored)
        / run.policy
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "segments.txt").write_text(
        "\n".join(run.segment_lines) + ("\n" if run.segment_lines else ""),
        encoding="utf-8",
    )
    (output / "segments.md").write_text(
        "# Segments\n\n```text\n"
        + "\n".join(run.segment_lines)
        + "\n```\n",
        encoding="utf-8",
    )
    _write_json(
        output / "status.json",
        {
            "status": run.fusion_status,
            "error": run.fusion_error or None,
            "membership_units": len(run.plan.membership_units),
            "unresolved_units": run.unresolved_units,
        },
    )


def _write_generate_object_stage(
    root: Path,
    stored: StoredObject,
    run: PolicyRun,
) -> None:
    output = root / "05-generate-object" / "objects"
    output.mkdir(parents=True, exist_ok=True)
    stem = _object_artifact_stem(stored)
    (output / f"{stem}.txt").write_text(
        run.result_text + ("\n" if run.result_text else ""),
        encoding="utf-8",
    )
    (output / f"{stem}.md").write_text(
        f"# {stem}\n\n```text\n{run.result_text}\n```\n",
        encoding="utf-8",
    )
    _write_json(
        output / f"{stem}.status.json",
        {
            "status": (
                "skipped"
                if run.fusion_status == "skipped"
                else "complete"
                if run.fusion_status == "complete"
                else "failed"
            ),
            "policy": run.policy,
            "fusion_status": run.fusion_status,
            "recognized_characters": len(compact_ocr_text(run.result_text)),
        },
    )


def _policy_payload(run: PolicyRun) -> dict[str, object]:
    return {
        "policy": run.policy,
        "blocks": len(run.plan.blocks),
        "block_pixels": sum(item.bbox.area for item in run.plan.blocks),
        "jobs": len(run.queue.jobs),
        "complete_jobs": run.queue.complete,
        "failed_jobs": run.queue.failed,
        "membership_units": len(run.plan.membership_units),
        "unresolved_units": run.unresolved_units,
        "recognized_characters": len(compact_ocr_text(run.result_text)),
        "planning_seconds": run.planning_seconds,
        "crop_seconds": run.crop_seconds,
        "ocr_seconds": run.ocr_seconds,
        "ocr_wall_seconds": run.ocr_seconds,
        "ocr_work_seconds": float(
            run.cache_metrics.get("ocr_work_seconds", run.ocr_seconds)
        ),
        "cache_requests": int(run.cache_metrics.get("requests", 0)),
        "cache_hits": int(run.cache_metrics.get("hits", 0)),
        "cache_misses": int(run.cache_metrics.get("misses", 0)),
        "exact_duplicate_calls": int(
            run.cache_metrics.get("exact_duplicate_calls_avoided", 0)
        ),
        "fusion_seconds": run.fusion_seconds,
        "total_seconds": run.total_seconds,
        "fusion_status": run.fusion_status,
        "fusion_error": run.fusion_error,
        "matrix_sha256": run.plan.matrix_sha256,
        "diagnostics": list(run.plan.diagnostics),
    }


def _write_policy_artifacts(
    object_output: Path,
    *,
    run: PolicyRun,
) -> None:
    policy_output = object_output / "ab" / run.policy
    blocks_output = policy_output / "blocks"
    blocks_output.mkdir(parents=True)
    best_by_block = _best_job_by_block(run.queue)
    crop_by_block = {item.block_id: item for item in run.crops}
    block_by_id = {item.block_id: item for item in run.plan.blocks}
    for block_index, block in enumerate(run.plan.blocks, start=1):
        directory = blocks_output / f"block-{block_index:06d}"
        directory.mkdir(parents=True)
        crop = crop_by_block[block.block_id]
        (directory / "image.png").write_bytes(crop.raw.png_bytes)
        best = best_by_block.get(block.block_id)
        recognized = (
            best.output.text.strip()
            if best is not None and best.output is not None
            else ""
        )
        (directory / "recognized.txt").write_text(
            recognized + ("\n" if recognized else ""), encoding="utf-8"
        )
        jobs = [
            {
                "job_id": job.job_id,
                "transform": job.transform.value,
                "status": job.status.value,
                "elapsed_seconds": job.elapsed_seconds,
                "text": job.output.text if job.output is not None else None,
                "mean_word_confidence": (
                    sum(item.confidence for item in job.output.words)
                    / len(job.output.words)
                    if job.output is not None and job.output.words
                    else None
                ),
                "error": job.error_message,
            }
            for job in run.queue.jobs
            if job.block_id == block.block_id
        ]
        _write_json(
            directory / "block.json",
            {
                "policy": run.policy,
                "source_block_id": block.block_id,
                "bbox": list(block.bbox.as_tuple()),
                "matrix_window": (
                    list(block.matrix_window)
                    if block.matrix_window is not None
                    else None
                ),
                "matrix_window_kind": block.matrix_window_kind,
                "matrix_segment_shape": (
                    list(block.matrix_segment_shape)
                    if block.matrix_segment_shape is not None
                    else None
                ),
                "core_segment_ids": list(block.core_segment_ids),
                "segment_ids": list(block.segment_ids),
                "context_segment_ids": list(block.context_segment_ids),
                "selected_job_id": best.job_id if best is not None else None,
                "jobs": jobs,
            },
        )
    if set(crop_by_block) != set(block_by_id):
        raise ObjectRecognitionInvariantError("crop set disagrees with block plan")
    (policy_output / "segments.txt").write_text(
        "\n".join(run.segment_lines) + ("\n" if run.segment_lines else ""),
        encoding="utf-8",
    )
    (policy_output / "result.txt").write_text(
        run.result_text + ("\n" if run.result_text else ""), encoding="utf-8"
    )
    _write_json(policy_output / "metrics.json", _policy_payload(run))


def _write_canonical_object_artifacts(
    object_output: Path,
    *,
    run: PolicyRun,
) -> None:
    """Write exactly the user-facing object -> blocks hierarchy."""

    if run.policy not in POLICIES:
        raise ObjectRecognitionInvariantError("unknown canonical OCR policy")
    best_by_block = _best_job_by_block(run.queue)
    crop_by_block = {item.block_id: item for item in run.crops}
    comparisons_by_block: dict[str, list[str]] = {
        item.block_id: [] for item in run.plan.blocks
    }
    for algebra in run.plan.adjacent_algebra:
        comparisons_by_block[algebra.first_block_id].append(
            algebra.second_block_id
        )
        comparisons_by_block[algebra.second_block_id].append(
            algebra.first_block_id
        )
    for block_index, block in enumerate(run.plan.blocks, start=1):
        directory = object_output / "blocks" / f"block-{block_index:06d}"
        directory.mkdir(parents=True)
        crop = crop_by_block[block.block_id]
        (directory / "image.png").write_bytes(crop.raw.png_bytes)
        best = best_by_block.get(block.block_id)
        recognized = (
            best.output.text.strip()
            if best is not None and best.output is not None
            else ""
        )
        (directory / "recognized.txt").write_text(
            recognized + ("\n" if recognized else ""), encoding="utf-8"
        )
        _write_json(
            directory / "block.json",
            {
                "policy": run.policy,
                "source_block_id": block.block_id,
                "bbox": list(block.bbox.as_tuple()),
                "matrix_window": (
                    list(block.matrix_window)
                    if block.matrix_window is not None
                    else None
                ),
                "matrix_window_kind": block.matrix_window_kind,
                "matrix_segment_shape": (
                    list(block.matrix_segment_shape)
                    if block.matrix_segment_shape is not None
                    else None
                ),
                "matrix_window_rows": (
                    block.matrix_window[1] - block.matrix_window[0]
                    if block.matrix_window is not None
                    else None
                ),
                "matrix_window_columns": (
                    block.matrix_window[3] - block.matrix_window[2]
                    if block.matrix_window is not None
                    else None
                ),
                "segment_ids": list(block.segment_ids),
                "orthogonal_comparison_blocks": comparisons_by_block[
                    block.block_id
                ],
                "recognized_text": recognized,
            },
        )
    if set(crop_by_block) != {item.block_id for item in run.plan.blocks}:
        raise ObjectRecognitionInvariantError("crop set disagrees with block plan")
    (object_output / "segments.txt").write_text(
        "\n".join(run.segment_lines) + ("\n" if run.segment_lines else ""),
        encoding="utf-8",
    )
    (object_output / "result.txt").write_text(
        run.result_text + ("\n" if run.result_text else ""), encoding="utf-8"
    )
    _write_json(
        object_output / "blocks" / "manifest.json",
        {
            "policy": run.policy,
            "block_count": len(run.plan.blocks),
            "window_start_positions": [
                len({
                    item.matrix_window[0]
                    for item in run.plan.blocks
                    if item.matrix_window is not None
                }),
                len({
                    item.matrix_window[2]
                    for item in run.plan.blocks
                    if item.matrix_window is not None
                }),
            ],
            "logical_extent": [
                max(
                    (
                        item.matrix_window[1]
                        for item in run.plan.blocks
                        if item.matrix_window is not None
                    ),
                    default=0,
                ),
                max(
                    (
                        item.matrix_window[3]
                        for item in run.plan.blocks
                        if item.matrix_window is not None
                    ),
                    default=0,
                ),
            ],
            "window_kinds": {
                kind: sum(
                    item.matrix_window_kind == kind for item in run.plan.blocks
                )
                for kind in sorted(
                    {
                        item.matrix_window_kind
                        for item in run.plan.blocks
                        if item.matrix_window_kind is not None
                    }
                )
            },
            "member_segment_shapes": {
                f"{shape[0]}x{shape[1]}": sum(
                    item.matrix_segment_shape == shape
                    for item in run.plan.blocks
                )
                for shape in sorted(
                    {
                        item.matrix_segment_shape
                        for item in run.plan.blocks
                        if item.matrix_segment_shape is not None
                    }
                )
            },
            "block_segment_counts": {
                str(count): sum(
                    len(item.segment_ids) == count for item in run.plan.blocks
                )
                for count in sorted(
                    {len(item.segment_ids) for item in run.plan.blocks}
                )
            },
            "minimum_block_segments_by_member_shape": {
                f"{shape[0]}x{shape[1]}": min(
                    len(item.segment_ids)
                    for item in run.plan.blocks
                    if item.matrix_segment_shape == shape
                )
                for shape in sorted(
                    {
                        item.matrix_segment_shape
                        for item in run.plan.blocks
                        if item.matrix_segment_shape is not None
                    }
                )
            },
            "algebra": _algebra_payload(run.plan),
            "diagnostics": list(run.plan.diagnostics),
        },
    )


def _contact_sheet(object_output: Path, runs: tuple[PolicyRun, ...]) -> None:
    entries = [
        (
            run.policy,
            index + 1,
            block,
            crop.raw.png_bytes,
        )
        for run in runs
        for index, (block, crop) in enumerate(
            zip(run.plan.blocks, run.crops)
        )
    ]
    if not entries:
        return
    thumbnails: list[tuple[str, Image.Image]] = []
    for policy, index, block, png in entries:
        with Image.open(io.BytesIO(png)) as opened:
            thumbnail = opened.convert("RGB")
            thumbnail.thumbnail((440, 260))
        shape = (
            "none"
            if block.matrix_segment_shape is None
            else "x".join(str(item) for item in block.matrix_segment_shape)
        )
        thumbnails.append(
            (
                f"{policy} #{index} members={len(block.segment_ids)} "
                f"member-shape={shape} kind={block.matrix_window_kind}",
                thumbnail,
            )
        )
    cell_width = 480
    cell_height = 310
    columns = 2
    rows = (len(thumbnails) + columns - 1) // columns
    sheet = Image.new("RGB", (cell_width * columns, cell_height * rows), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        for index, (label, thumbnail) in enumerate(thumbnails):
            left = (index % columns) * cell_width
            top = (index // columns) * cell_height
            draw.text((left + 12, top + 8), label, fill="black")
            sheet.paste(thumbnail, (left + 12, top + 36))
        output = object_output / "blocks" / "contact-sheet.png"
        sheet.save(output, format="PNG")
    finally:
        sheet.close()
        for _, thumbnail in thumbnails:
            thumbnail.close()


def _metric(
    reference: str,
    recognized: str,
    *,
    debug_full_alignment: bool = False,
) -> dict[str, object]:
    compact_reference = compact_ocr_text(reference)
    compact_recognized = compact_ocr_text(recognized)
    if debug_full_alignment:
        distance = align_exact_text(
            compact_reference,
            compact_recognized,
        ).distance
    else:
        shorter = min(len(compact_reference), len(compact_recognized))
        longer = max(len(compact_reference), len(compact_recognized))
        bit_vector_work = longer * ((shorter + 63) // 64)
        distance = exact_levenshtein(
            compact_reference,
            compact_recognized,
            max_cells=max(1, bit_vector_work),
        )
    accuracy = (
        100.0 if distance == 0 else 0.0
        if not compact_reference
        else 100.0
        * max(0.0, 1.0 - distance / len(compact_reference))
    )
    return {
        "lost_characters": distance,
        "reference_characters": len(compact_reference),
        "recognized_characters": len(compact_recognized),
        "accuracy_percent": accuracy,
    }


def canonical_paragraph_list_policy(stored: StoredObject) -> str:
    if stored.source_kind == ObjectKind.LIST.value:
        return "whole-object"
    if stored.source_kind == ObjectKind.PARAGRAPH.value:
        return "line-windows"
    raise ValueError("canonical paragraph/list policy requires a flow object")


def alternate_paragraph_list_policy(stored: StoredObject) -> str:
    canonical = canonical_paragraph_list_policy(stored)
    return "line-windows" if canonical == "whole-object" else "whole-object"


def _queue_grammar_percentages(queue: OcrQueueResult) -> tuple[int, ...]:
    percentages = []
    for diagnostic in queue.diagnostics:
        for part in diagnostic.split(";"):
            if part.startswith("grammar="):
                value = part.removeprefix("grammar=")
                if value.isdigit():
                    percentages.append(int(value))
    return tuple(percentages)


def lazy_paragraph_list_fallback_reason(run: PolicyRun) -> str | None:
    if run.fusion_status != "complete":
        return f"fusion-{run.fusion_status}"
    if run.unresolved_units:
        return f"unresolved-{run.unresolved_units}"
    usable_job_evidence = any(
        job.status is OcrJobStatus.COMPLETE
        and job.output is not None
        and bool(compact_ocr_text(job.output.text))
        for job in run.queue.jobs
    )
    if (
        not compact_ocr_text(run.result_text)
        or not usable_job_evidence
    ):
        return "empty-evidence"
    return None


def _paragraph_list_policy_evidence(
    run: PolicyRun,
    *,
    maximum_text_characters: int,
) -> dict[str, object]:
    complete_block_ids = {
        job.block_id
        for job in run.queue.jobs
        if job.status is OcrJobStatus.COMPLETE and job.output is not None
    }
    block_by_id = {block.block_id: block for block in run.plan.blocks}
    covered_segment_ids = {
        segment_id
        for block_id in complete_block_ids
        if block_id in block_by_id
        for segment_id in block_by_id[block_id].segment_ids
    }
    source_segment_ids = set(run.plan.source_segment_ids)
    membership_units = len(run.plan.membership_units)
    resolved_membership_coverage = (
        max(0.0, 1.0 - run.unresolved_units / membership_units)
        if membership_units
        else 1.0
    )
    source_segment_coverage = (
        len(covered_segment_ids & source_segment_ids) / len(source_segment_ids)
        if source_segment_ids
        else 1.0
    )
    text_characters = len(compact_ocr_text(run.result_text))
    relative_text_coverage = (
        text_characters / maximum_text_characters
        if maximum_text_characters
        else 0.0
    )
    grammar = _queue_grammar_percentages(run.queue)
    grammar_percent = (
        sum(grammar) / len(grammar)
        if grammar
        else 0.0
    )
    confidences = tuple(
        word.confidence
        for job in run.queue.jobs
        if job.status is OcrJobStatus.COMPLETE and job.output is not None
        for word in job.output.words
    )
    mean_word_confidence = (
        sum(confidences) / len(confidences)
        if confidences
        else 0.0
    )
    coverage_score = 100.0 * (
        resolved_membership_coverage * 0.35
        + source_segment_coverage * 0.30
        + relative_text_coverage * 0.15
    )
    quality_score = (
        grammar_percent * 0.15
        + mean_word_confidence * 100.0 * 0.05
    )
    return {
        "policy": run.policy,
        "fusion_complete": run.fusion_status == "complete",
        "fusion_status": run.fusion_status,
        "unresolved_units": run.unresolved_units,
        "resolved_membership_coverage": resolved_membership_coverage,
        "source_segment_coverage": source_segment_coverage,
        "relative_text_coverage": relative_text_coverage,
        "text_characters": text_characters,
        "grammar_percent": grammar_percent,
        "mean_word_confidence": mean_word_confidence,
        "coverage_score": coverage_score,
        "quality_score": quality_score,
        "evidence_score": coverage_score + quality_score,
    }


def select_paragraph_list_policy_run(
    stored: StoredObject,
    runs: Mapping[str, PolicyRun],
) -> PolicyRun:
    if stored.source_kind not in {
        ObjectKind.PARAGRAPH.value,
        ObjectKind.LIST.value,
    }:
        raise ValueError("policy evidence selection requires paragraph/list")
    candidates = tuple(
        run
        for policy in (
            canonical_paragraph_list_policy(stored),
            alternate_paragraph_list_policy(stored),
        )
        if (run := runs.get(policy)) is not None
    )
    if not candidates:
        raise ObjectRecognitionInvariantError(
            "paragraph/list has no recognized policy"
        )
    maximum_text_characters = max(
        len(compact_ocr_text(run.result_text))
        for run in candidates
    )
    canonical = canonical_paragraph_list_policy(stored)
    evidence_by_policy = {
        run.policy: _paragraph_list_policy_evidence(
            run,
            maximum_text_characters=maximum_text_characters,
        )
        for run in candidates
    }
    return max(
        candidates,
        key=lambda run: (
            int(bool(evidence_by_policy[run.policy]["fusion_complete"])),
            float(evidence_by_policy[run.policy]["evidence_score"]),
            int(run.policy == canonical),
        ),
    )


def _paragraph_list_selection_payload(
    stored: StoredObject,
    runs: Mapping[str, PolicyRun],
) -> dict[str, object]:
    selected = select_paragraph_list_policy_run(stored, runs)
    maximum_text_characters = max(
        len(compact_ocr_text(run.result_text))
        for run in runs.values()
    )
    return {
        "selected_policy": selected.policy,
        "canonical_policy": canonical_paragraph_list_policy(stored),
        "policies": {
            policy: _paragraph_list_policy_evidence(
                run,
                maximum_text_characters=maximum_text_characters,
            )
            for policy, run in sorted(runs.items())
        },
    }


def _object_policies(
    stored: StoredObject,
    *,
    paragraph_list_ab: bool | None = None,
) -> tuple[str, ...]:
    """Route canonical flow first; preserve legacy direct-call A/B diagnostics."""

    if stored.source_kind == ObjectKind.TABLE.value:
        return ("matrix-orxor",)
    if stored.source_kind in {
        ObjectKind.PARAGRAPH.value,
        ObjectKind.LIST.value,
    }:
        if paragraph_list_ab is None:
            return ("whole-object", "line-windows")
        canonical = canonical_paragraph_list_policy(stored)
        if paragraph_list_ab:
            return (canonical, alternate_paragraph_list_policy(stored))
        return (canonical,)
    return ("fixed-flow",)


def _copy_clean_inputs(geometry_dir: Path, output: Path) -> Path:
    grid = output / "grid"
    matrix = grid / "matrix"
    matrix.mkdir(parents=True)
    grid_source = geometry_dir / "matrix-overlay.png"
    if not grid_source.is_file():
        grid_source = geometry_dir / "aligned.png"
    shutil.copyfile(grid_source, grid / "image.png")
    shutil.copyfile(geometry_dir / "matrix.json", matrix / "matrix.json")
    return matrix / "objects"


def recognize_item(
    *,
    item_dir: Path,
    output_dir: Path,
    session: OcrSession,
    reference_path: Path | None = None,
    debug_full_metric_alignment: bool = False,
    replace_existing: bool = False,
    geometry_dir: Path | None = None,
    objects_dir: Path | None = None,
    paragraph_list_ab: bool = False,
) -> Path:
    geometry_dir = geometry_dir or item_dir / "01-geometry"
    objects_dir = objects_dir or item_dir / "06-objects" / "objects"
    if (objects_dir / "objects").is_dir():
        objects_dir = objects_dir / "objects"
    if not (geometry_dir / "segments.jsonl").is_file():
        raise FileNotFoundError(geometry_dir / "segments.jsonl")
    if not objects_dir.is_dir():
        raise FileNotFoundError(objects_dir)
    if output_dir.exists() and not replace_existing:
        raise FileExistsError(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.partial-", dir=output_dir.parent)
    )
    log_lines = [
        f"schema={SCHEMA}",
        f"input={item_dir.resolve()}",
        "stages=find-object -> separate-block -> ocr-blocks -> "
        "get-segment -> generate-object",
    ]
    object_summaries: list[dict[str, object]] = []
    processed_objects: list[
        tuple[Path, StoredObject, tuple[PolicyRun, ...]]
    ] = []
    try:
        objects_output = _copy_clean_inputs(geometry_dir, temporary)
        objects_output.mkdir(parents=True, exist_ok=True)
        object_dirs = tuple(sorted(objects_dir.glob("object-*")))
        if not object_dirs:
            raise ObjectRecognitionInvariantError("Stage 6 emitted no object folders")
        for object_dir in object_dirs:
            object_output = objects_output / object_dir.name
            object_output.mkdir()
            stored = load_stored_object(
                object_dir,
                segments_path=geometry_dir / "segments.jsonl",
            )
            _write_find_object_stage(temporary, stored)
            shutil.copyfile(object_dir / "matrix.json", object_output / "matrix.json")
            shutil.copyfile(object_dir / "object.png", object_output / "image.png")
            (object_output / "blocks").mkdir()
            runs: list[PolicyRun] = []
            policies = list(
                _object_policies(
                    stored,
                    paragraph_list_ab=paragraph_list_ab,
                )
            )
            policy_index = 0
            while policy_index < len(policies):
                policy = policies[policy_index]
                policy_index += 1
                started = time.perf_counter()
                try:
                    separated = separate_blocks(stored, policy=policy)
                    role = (
                        "canonical"
                        if stored.source_kind not in {"paragraph", "list"}
                        or policy == canonical_paragraph_list_policy(stored)
                        else "alternate"
                    )
                    separated = dataclass_replace(
                        separated,
                        plan=dataclass_replace(
                            separated.plan,
                            diagnostics=(
                                *separated.plan.diagnostics,
                                f"paragraph-list-policy-role={role}",
                            ),
                        ),
                    )
                    _write_separate_block_stage(temporary, stored, separated)
                    block_ocr = ocr_blocks(separated, session=session)
                    _write_ocr_blocks_stage(temporary, stored, block_ocr)
                    run = get_segments(stored, block_ocr)
                    _write_get_segment_stage(temporary, stored, run)
                    runs.append(run)
                    if (
                        stored.source_kind in {"paragraph", "list"}
                        and policy
                        == canonical_paragraph_list_policy(stored)
                        and len(policies) == 1
                        and (
                            fallback_reason
                            := lazy_paragraph_list_fallback_reason(run)
                        )
                        is not None
                    ):
                        policies.append(
                            alternate_paragraph_list_policy(stored)
                        )
                        log_lines.append(
                            f"object={stored.source_object_id} "
                            f"lazy_alternate={policies[-1]} "
                            f"reason={fallback_reason}"
                        )
                    log_lines.append(
                        f"object={stored.source_object_id} policy={policy} "
                        f"segments={len(stored.segments)} "
                        f"blocks={len(run.plan.blocks)} "
                        f"jobs={len(run.queue.jobs)} ocr_seconds={run.ocr_seconds:.6f} "
                        f"unresolved={run.unresolved_units}"
                    )
                    log_lines.extend(
                        (
                            f"object={stored.source_object_id} policy={policy} "
                            f"job={job.job_id} transform={job.transform.value} "
                            f"status=failed error={job.error_type}: "
                            f"{job.error_message}"
                        )
                        for job in run.queue.jobs
                        if job.status is OcrJobStatus.FAILED
                    )
                except Exception as exc:
                    log_lines.append(
                        (
                            f"object={stored.source_object_id} policy={policy} "
                            f"status=failed elapsed_seconds="
                            f"{time.perf_counter() - started:.6f} "
                            f"error={type(exc).__name__}: {exc}"
                        )
                    )
                    raise
            run_tuple = tuple(runs)
            if len(run_tuple) > 1:
                for run in run_tuple:
                    _write_policy_artifacts(object_output, run=run)
            processed_objects.append((object_output, stored, run_tuple))
            object_summaries.append(
                {
                    "object_id": stored.source_object_id,
                    "kind": stored.source_kind,
                    "segments": len(stored.segments),
                    "matrix_rows": len(stored.matrix.rows),
                    "matrix_columns": len(stored.matrix.columns),
                    "policies": [_policy_payload(item) for item in run_tuple],
                }
            )

        def selected_run(
            stored: StoredObject,
            runs: tuple[PolicyRun, ...],
        ) -> PolicyRun:
            by_policy = {item.policy: item for item in runs}
            if stored.source_kind == ObjectKind.TABLE.value:
                return by_policy["matrix-orxor"]
            if stored.source_kind in {"paragraph", "list"}:
                return select_paragraph_list_policy_run(
                    stored,
                    by_policy,
                )
            return by_policy["fixed-flow"]

        def document_variant(flow_policy: str | None) -> str:
            texts: list[str] = []
            for _object_output, stored, runs in processed_objects:
                by_policy = {item.policy: item for item in runs}
                if stored.source_kind == ObjectKind.TABLE.value:
                    selected = by_policy["matrix-orxor"]
                elif flow_policy is not None and flow_policy in by_policy:
                    selected = by_policy[flow_policy]
                elif stored.source_kind in {"paragraph", "list"}:
                    selected = selected_run(stored, runs)
                else:
                    selected = by_policy["fixed-flow"]
                if selected.result_text.strip():
                    texts.append(selected.result_text)
            return "\n".join(texts)

        flow_policies = tuple(
            dict.fromkeys(
                run.policy
                for _object_output, stored, runs in processed_objects
                if stored.source_kind in {"paragraph", "list"}
                for run in runs
            )
        )
        variant_results = {
            policy: document_variant(policy) for policy in flow_policies
        }
        reference = (
            reference_path.read_text(encoding="utf-8")
            if reference_path is not None
            else None
        )
        variant_metrics = (
            {
                policy: _metric(
                    reference,
                    result,
                    debug_full_alignment=debug_full_metric_alignment,
                )
                for policy, result in variant_results.items()
            }
            if reference is not None
            else {}
        )
        canonical_flow_policy = "canonical-first"
        canonical_result = document_variant(None)
        canonical_metric = (
            _metric(reference, canonical_result)
            if reference is not None
            else None
        )
        canonical_runs: list[tuple[str, PolicyRun]] = []
        paragraph_list_selections: dict[str, dict[str, object]] = {}
        for object_output, stored, runs in processed_objects:
            canonical_run = selected_run(stored, runs)
            canonical_runs.append((stored.source_object_id, canonical_run))
            if stored.source_kind in {"paragraph", "list"}:
                paragraph_list_selections[stored.source_object_id] = (
                    _paragraph_list_selection_payload(
                        stored,
                        {run.policy: run for run in runs},
                    )
                )
            _write_generate_object_stage(temporary, stored, canonical_run)
            _write_canonical_object_artifacts(
                object_output,
                run=canonical_run,
            )
            _contact_sheet(object_output, (canonical_run,))
        (temporary / "result.txt").write_text(
            canonical_result + ("\n" if canonical_result else ""),
            encoding="utf-8",
        )
        if len(flow_policies) > 1:
            for policy, result in variant_results.items():
                variant_output = temporary / "ab" / policy
                variant_output.mkdir(parents=True)
                (variant_output / "result.txt").write_text(
                    result + ("\n" if result else ""),
                    encoding="utf-8",
                )
                if policy in variant_metrics:
                    _write_json(
                        variant_output / "metric.json",
                        variant_metrics[policy],
                    )
        aggregates: dict[str, dict[str, object]] = {}
        for policy in POLICIES:
            rows = [
                policy_row
                for object_row in object_summaries
                for policy_row in object_row["policies"]
                if policy_row["policy"] == policy
            ]
            aggregates[policy] = {
                "objects": len(rows),
                "blocks": sum(int(item["blocks"]) for item in rows),
                "jobs": sum(int(item["jobs"]) for item in rows),
                "failed_jobs": sum(int(item["failed_jobs"]) for item in rows),
                "block_pixels": sum(int(item["block_pixels"]) for item in rows),
                "ocr_seconds": sum(float(item["ocr_seconds"]) for item in rows),
                "ocr_work_seconds": sum(
                    float(item["ocr_work_seconds"]) for item in rows
                ),
                "exact_duplicate_calls": sum(
                    int(item["exact_duplicate_calls"]) for item in rows
                ),
                "total_seconds": sum(float(item["total_seconds"]) for item in rows),
                "unresolved_units": sum(
                    int(item["unresolved_units"]) for item in rows
                ),
            }
            if policy in variant_metrics:
                aggregates[policy]["accuracy_percent"] = variant_metrics[
                    policy
                ]["accuracy_percent"]
        canonical_missing_blocks: dict[str, list[str]] = {}
        for object_id, run in canonical_runs:
            complete_block_ids = {
                job.block_id
                for job in run.queue.jobs
                if job.status is OcrJobStatus.COMPLETE and job.output is not None
            }
            missing_block_ids = [
                block.block_id
                for block in run.plan.blocks
                if block.block_id not in complete_block_ids
            ]
            if missing_block_ids:
                canonical_missing_blocks[object_id] = missing_block_ids
        canonical_jobs = sum(len(run.queue.jobs) for _object_id, run in canonical_runs)
        canonical_complete_jobs = sum(
            run.queue.complete for _object_id, run in canonical_runs
        )
        canonical_failed_jobs = sum(
            run.queue.failed for _object_id, run in canonical_runs
        )
        stage_status = (
            "failed"
            if canonical_jobs == 0 or canonical_missing_blocks
            else "complete"
        )
        failure_reason = (
            "no_canonical_ocr_jobs"
            if canonical_jobs == 0
            else "canonical_blocks_without_successful_ocr"
            if canonical_missing_blocks
            else None
        )
        comparison_status = (
            "failed"
            if stage_status == "failed"
            else "scored"
            if reference is not None
            else "unscored"
        )
        comparison = {
            "stage_status": stage_status,
            "comparison_status": comparison_status,
            "failure_reason": failure_reason,
            "canonical_jobs": canonical_jobs,
            "canonical_complete_jobs": canonical_complete_jobs,
            "canonical_failed_jobs": canonical_failed_jobs,
            "canonical_missing_blocks": canonical_missing_blocks,
            "table_policy": "matrix-orxor",
            "unknown_flow_policy": "fixed-flow",
            "paragraph_list_ab_policies": [*flow_policies],
            "canonical_paragraph_list_policy": canonical_flow_policy,
            "paragraph_list_selections": paragraph_list_selections,
            "tables_excluded_from_context_ab": True,
            "variant_accuracy_percent": {
                policy: metric["accuracy_percent"]
                for policy, metric in variant_metrics.items()
            },
            "canonical_accuracy_percent": (
                canonical_metric["accuracy_percent"]
                if canonical_metric is not None
                else None
            ),
        }
        fusion_failures = {
            object_id: run.fusion_status
            for object_id, run in canonical_runs
            if run.fusion_status != "complete"
        }
        get_segment_status = (
            "SKIPPED"
            if stage_status == "failed"
            else "FAILED"
            if fusion_failures
            else "COMPLETE"
        )
        generate_object_status = (
            "COMPLETE" if get_segment_status == "COMPLETE" else "SKIPPED"
        )
        stage_lines = [
            "stage\tstatus\tseconds\tartifact\tdetail",
            f"find-object\tCOMPLETE\t0\t01-find-object\t"
            f"objects={len(processed_objects)}",
            "separate-block\tCOMPLETE\t"
            f"{sum(run.planning_seconds + run.crop_seconds for _, _, runs in processed_objects for run in runs):.6f}"
            "\t02-separate-block\tblock images and memberships published",
            f"ocr-blocks\t{stage_status.upper()}\t"
            f"{sum(run.ocr_seconds for _, _, runs in processed_objects for run in runs):.6f}"
            f"\t03-ocr-blocks\tmissing={json.dumps(canonical_missing_blocks, sort_keys=True)}",
            f"get-segment\t{get_segment_status}\t"
            f"{sum(run.fusion_seconds for _, run in canonical_runs):.6f}"
            f"\t04-get-segment\tfailures={json.dumps(fusion_failures, sort_keys=True)}",
            f"generate-object\t{generate_object_status}\t0\t"
            "05-generate-object\tstrict upstream gate",
        ]
        benchmark_lines = [
            "policy\tobjects\tblocks\tjobs\tfailed_jobs\tblock_pixels\t"
            "ocr_work_seconds\tocr_wall_seconds\texact_duplicate_calls\t"
            "total_seconds\tunresolved_units\taccuracy_percent"
        ]
        for policy in POLICIES:
            row = aggregates[policy]
            benchmark_lines.append(
                "\t".join(
                    (
                        policy,
                        str(row["objects"]),
                        str(row["blocks"]),
                        str(row["jobs"]),
                        str(row["failed_jobs"]),
                        str(row["block_pixels"]),
                        f"{float(row['ocr_work_seconds']):.6f}",
                        f"{float(row['ocr_seconds']):.6f}",
                        str(row["exact_duplicate_calls"]),
                        f"{float(row['total_seconds']):.6f}",
                        str(row["unresolved_units"]),
                        (
                            f"{float(row['accuracy_percent']):.6f}"
                            if "accuracy_percent" in row
                            else ""
                        ),
                    )
                )
            )
        (temporary / "benchmark.tsv").write_text(
            "\n".join(benchmark_lines) + "\n", encoding="utf-8"
        )
        _write_json(temporary / "comparison.json", comparison)
        (temporary / "stages.tsv").write_text(
            "\n".join(stage_lines) + "\n",
            encoding="utf-8",
        )
        log_lines.extend(
            f"comparison.{key}={value}" for key, value in comparison.items()
        )
        (temporary / "engine.log").write_text(
            "\n".join(log_lines) + "\n", encoding="utf-8"
        )
        if output_dir.exists():
            shutil.rmtree(output_dir)
        temporary.rename(output_dir)
        return output_dir
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--item-dir",
        type=Path,
        help="stored separated-run item containing 01-geometry and 06-objects",
    )
    parser.add_argument(
        "--geometry-dir",
        type=Path,
        help="inject an explicit saved geometry directory",
    )
    parser.add_argument(
        "--objects-dir",
        type=Path,
        help="inject an explicit 06-objects directory or its objects child",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="engine output tree (default: ITEM/tesseract)",
    )
    parser.add_argument("--reference", type=Path)
    parser.add_argument(
        "--debug-full-metric-alignment",
        action="store_true",
        help="build a bounded O(n*m) edit script instead of distance-only scoring",
    )
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--tesseract-executable", default="tesseract")
    parser.add_argument("--tessdata", type=Path)
    parser.add_argument("--tesseract-workers", type=int, default=4)
    parser.add_argument("--tesseract-psm", type=int, choices=(4, 6), default=4)
    parser.add_argument(
        "--paragraph-list-ab",
        action="store_true",
        help="always retain both paragraph/list context policies",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.tesseract_workers < 1:
        raise ValueError("tesseract workers must be positive")
    if args.item_dir is None and (
        args.geometry_dir is None or args.objects_dir is None
    ):
        raise ValueError(
            "provide --item-dir or both --geometry-dir and --objects-dir"
        )
    geometry_dir = (
        args.geometry_dir.resolve() if args.geometry_dir is not None else None
    )
    objects_dir = (
        args.objects_dir.resolve() if args.objects_dir is not None else None
    )
    item_dir = (
        args.item_dir.resolve()
        if args.item_dir is not None
        else geometry_dir.parent
    )
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else item_dir / "tesseract"
    )
    lane = make_tesseract_lane(
        "tesseract-multilingual",
        config=TesseractConfig(
            executable=args.tesseract_executable,
            tessdata_directory=(
                args.tessdata.resolve() if args.tessdata is not None else None
            ),
            languages=("eng", "chi_sim", "rus"),
            psm=args.tesseract_psm,
        ),
        max_workers=args.tesseract_workers,
    )
    with PersistentOcrSession((lane,)) as session:
        published = recognize_item(
            item_dir=item_dir,
            output_dir=output_dir,
            session=session,
            reference_path=(
                args.reference.resolve() if args.reference is not None else None
            ),
            debug_full_metric_alignment=args.debug_full_metric_alignment,
            replace_existing=args.replace,
            geometry_dir=geometry_dir,
            objects_dir=objects_dir,
            paragraph_list_ab=args.paragraph_list_ab,
        )
    print(published)
    comparison = json.loads(
        (published / "comparison.json").read_text(encoding="utf-8")
    )
    if comparison["stage_status"] != "complete":
        print(
            "block OCR did not produce successful evidence for every "
            "canonical block",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
