#!/usr/bin/env python3
"""Run isolated sparse-pipeline labs over frozen Stage 1/6 evidence.

This tool intentionally does *not* run an end-to-end OCR pipeline.  It keeps
three questions in separate artifact trees:

* ``orxor``: can object-local Stage 5 blocks recover exact membership units?
* ``assembly``: can the production Stage 7 rendering core assemble already
  prepared segment text without geometry, planning, OCR, or fusion?
* ``granularity``: what topology is produced by whole-paragraph, overlapping
  multi-line, per-line, list-item, and table-local policies?

The default fixture is deterministic and contains a paragraph, a list, and a
3x2 ruled table.  A later manually accepted Stage 1 run can be normalized to
the same ``bundle.json`` + ``page.png`` format and passed with ``--bundle``.
Every invocation is published to a new immutable run directory.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from PIL import Image, ImageDraw, UnidentifiedImageError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "ocr"))

from app.sparse_pipeline.atomic_publish import rename_no_replace  # noqa: E402
from app.sparse_pipeline.block_planning import (  # noqa: E402
    BlockPlan,
    BlockPlanningConfig,
    BlockPlanningMode,
    OverlappingBlockPlanner,
    RecognitionBlock,
    sparse_matrix_payload,
)
from app.sparse_pipeline.contracts import (  # noqa: E402
    AxisInterval,
    Box,
    Segment,
    SegmentKind,
    SegmentSpan,
    SparseCell,
    SparseSegmentMatrix,
)
from app.sparse_pipeline.document_assembly import (  # noqa: E402
    AssemblyStatus,
    AttributionLevel,
    DocumentAssembler,
    SegmentTextAssembly,
    StructuralUnit,
)
from app.sparse_pipeline.object_reconstruction import (  # noqa: E402
    DocumentObject,
    ObjectKind,
    ObjectReconstructionResult,
    SegmentObjectOwnership,
)


SCHEMA_VERSION = 1
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class FrozenBundleInvariantError(ValueError):
    """Raised when a supposedly frozen Stage 1/6 bundle is inconsistent."""


class LabInvariantError(ValueError):
    """Raised when one isolated lab crosses a forbidden evidence boundary."""


@dataclass(frozen=True)
class FrozenBundle:
    bundle_id: str
    aligned_size: tuple[int, int]
    page_png: bytes
    segments: tuple[Segment, ...]
    matrix: SparseSegmentMatrix
    objects: ObjectReconstructionResult
    segment_texts: tuple[tuple[str, str | None], ...]
    expected_object_texts: tuple[tuple[str, str], ...] = ()
    expected_document_text: str | None = None
    expected_document_markdown: str | None = None
    stage1_manual_audit_status: str = "synthetic"

    def __post_init__(self) -> None:
        if not self.bundle_id:
            raise FrozenBundleInvariantError("bundle_id must not be empty")
        if (
            len(self.aligned_size) != 2
            or any(type(value) is not int or value < 1 for value in self.aligned_size)
        ):
            raise FrozenBundleInvariantError(
                "aligned_size must contain two positive integers"
            )
        segment_ids = tuple(item.segment_id for item in self.segments)
        if len(segment_ids) != len(set(segment_ids)):
            raise FrozenBundleInvariantError("segment IDs must be unique")
        if self.matrix.segment_ids() != frozenset(segment_ids):
            raise FrozenBundleInvariantError(
                "matrix must contain exactly the frozen segment IDs"
            )
        if self.objects.aligned_size != self.aligned_size:
            raise FrozenBundleInvariantError(
                "Stage 6 object canvas disagrees with frozen canvas"
            )
        if self.objects.source_segment_ids != segment_ids:
            raise FrozenBundleInvariantError(
                "Stage 6 canonical segment order must equal frozen order"
            )
        text_ids = tuple(item[0] for item in self.segment_texts)
        if text_ids != segment_ids:
            raise FrozenBundleInvariantError(
                "segment_texts must follow the exact frozen segment order"
            )
        if any(value is not None and type(value) is not str for _, value in self.segment_texts):
            raise FrozenBundleInvariantError(
                "ready segment text must be a string or null"
            )
        object_ids = tuple(item.object_id for item in self.objects.objects)
        expected_ids = tuple(item[0] for item in self.expected_object_texts)
        if len(expected_ids) != len(set(expected_ids)) or not set(
            expected_ids
        ).issubset(object_ids):
            raise FrozenBundleInvariantError(
                "expected object texts reference unknown or duplicate objects"
            )
        if self.stage1_manual_audit_status not in {
            "synthetic",
            "accepted",
            "pending",
            "rejected",
        }:
            raise FrozenBundleInvariantError(
                "stage1_manual_audit_status is not a known fail-closed state"
            )
        self._validate_page()
        segment_by_id = {item.segment_id: item for item in self.segments}
        for document_object in self.objects.objects:
            actual = Box.union(
                segment_by_id[item].bbox for item in document_object.segment_ids
            )
            if actual != document_object.bbox:
                raise FrozenBundleInvariantError(
                    f"object {document_object.object_id} bbox is not the exact "
                    "union of its frozen segments"
                )

    def _validate_page(self) -> None:
        try:
            with Image.open(io.BytesIO(self.page_png)) as opened:
                if (
                    opened.format != "PNG"
                    or opened.mode != "RGB"
                    or opened.size != self.aligned_size
                    or getattr(opened, "n_frames", 1) != 1
                ):
                    raise FrozenBundleInvariantError(
                        "page.png must be one RGB PNG at aligned_size"
                    )
                opened.verify()
        except FrozenBundleInvariantError:
            raise
        except (OSError, SyntaxError, UnidentifiedImageError) as exc:
            raise FrozenBundleInvariantError("page_png is not a valid PNG") from exc

    @property
    def text_by_segment(self) -> dict[str, str | None]:
        return dict(self.segment_texts)

    @property
    def object_by_id(self) -> dict[str, DocumentObject]:
        return {item.object_id: item for item in self.objects.objects}


@dataclass(frozen=True)
class LabBlock:
    block_id: str
    object_id: str
    segment_ids: tuple[str, ...]
    bbox: Box
    policy: str

    def __post_init__(self) -> None:
        if not self.block_id or not self.object_id or not self.policy:
            raise ValueError("lab block identity must not be empty")
        if not self.segment_ids or len(self.segment_ids) != len(
            set(self.segment_ids)
        ):
            raise ValueError("lab block needs unique segment members")


@dataclass(frozen=True)
class _ReadyTextTrace:
    """Minimal adapter expected by Stage 7's ready-text rendering core."""

    selected_observation_id: None = None


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )


def _write_jsonl(path: Path, values: Iterable[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as output:
        for value in values:
            output.write(_canonical_json_bytes(value))


def _box_payload(box: Box) -> list[int]:
    return list(box.as_tuple())


def _segment_payload(
    segment: Segment,
    *,
    text: str | None,
) -> dict[str, object]:
    return {
        "segment_id": segment.segment_id,
        "bbox": _box_payload(segment.bbox),
        "source_bbox": _box_payload(segment.source_bbox),
        "kind": segment.kind.value,
        "ink_pixels": segment.ink_pixels,
        "row_index": segment.row_index,
        "order_key": list(segment.order_key),
        "parent_path": list(segment.parent_path),
        "component_ids": list(segment.component_ids),
        "text": text,
    }


def _object_payload(value: DocumentObject) -> dict[str, object]:
    return {
        "object_id": value.object_id,
        "kind": value.kind.value,
        "segment_ids": list(value.segment_ids),
        "bbox": _box_payload(value.bbox),
        "reading_index": value.reading_index,
        "row_start": value.row_start,
        "row_stop": value.row_stop,
        "column_start": value.column_start,
        "column_stop": value.column_stop,
        "confidence": value.confidence,
        "evidence": list(value.evidence),
    }


def frozen_bundle_payload(bundle: FrozenBundle) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": bundle.bundle_id,
        "aligned_size": list(bundle.aligned_size),
        "page_file": "page.png",
        "page_sha256": _sha256(bundle.page_png),
        "stage1_manual_audit_status": bundle.stage1_manual_audit_status,
        "segments": [
            _segment_payload(item, text=dict(bundle.segment_texts)[item.segment_id])
            for item in bundle.segments
        ],
        "matrix": sparse_matrix_payload(bundle.matrix),
        "objects": [_object_payload(item) for item in bundle.objects.objects],
        "expected": {
            "objects": dict(bundle.expected_object_texts),
            "document_text": bundle.expected_document_text,
            "document_markdown": bundle.expected_document_markdown,
        },
    }


def frozen_bundle_sha256(bundle: FrozenBundle) -> str:
    return _sha256(_canonical_json_bytes(frozen_bundle_payload(bundle)))


def _axis_from_payload(values: object, *, name: str) -> tuple[AxisInterval, ...]:
    if type(values) is not list:
        raise FrozenBundleInvariantError(f"matrix {name} must be a list")
    try:
        return tuple(AxisInterval(*value) for value in values)
    except (TypeError, ValueError) as exc:
        raise FrozenBundleInvariantError(f"invalid matrix {name}") from exc


def load_frozen_bundle(path: Path) -> FrozenBundle:
    metadata_path = path / "bundle.json" if path.is_dir() else path
    if not metadata_path.is_file():
        raise FileNotFoundError(metadata_path)
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FrozenBundleInvariantError("bundle.json is not valid UTF-8 JSON") from exc
    if type(payload) is not dict or payload.get("schema_version") != SCHEMA_VERSION:
        raise FrozenBundleInvariantError(
            f"bundle schema_version must equal {SCHEMA_VERSION}"
        )
    page_file = payload.get("page_file")
    if type(page_file) is not str or not page_file or Path(page_file).name != page_file:
        raise FrozenBundleInvariantError("page_file must be one local file name")
    page_png = (metadata_path.parent / page_file).read_bytes()
    if payload.get("page_sha256") != _sha256(page_png):
        raise FrozenBundleInvariantError("page_sha256 disagrees with page file")
    raw_segments = payload.get("segments")
    if type(raw_segments) is not list:
        raise FrozenBundleInvariantError("segments must be a list")
    segments: list[Segment] = []
    segment_texts: list[tuple[str, str | None]] = []
    try:
        for item in raw_segments:
            if type(item) is not dict:
                raise TypeError("segment entry must be an object")
            segment_id = item["segment_id"]
            segment = Segment(
                segment_id=segment_id,
                bbox=Box(*item["bbox"]),
                source_bbox=Box(*item.get("source_bbox", item["bbox"])),
                kind=SegmentKind(item.get("kind", "text")),
                ink_pixels=item["ink_pixels"],
                row_index=item["row_index"],
                order_key=tuple(item["order_key"]),
                parent_path=tuple(item["parent_path"]),
                component_ids=tuple(item.get("component_ids", ())),
            )
            segments.append(segment)
            segment_texts.append((segment_id, item.get("text")))
    except (KeyError, TypeError, ValueError) as exc:
        raise FrozenBundleInvariantError("invalid frozen segment record") from exc
    matrix_payload = payload.get("matrix")
    if type(matrix_payload) is not dict:
        raise FrozenBundleInvariantError("matrix must be an object")
    try:
        matrix = SparseSegmentMatrix(
            rows=_axis_from_payload(matrix_payload["rows"], name="rows"),
            columns=_axis_from_payload(
                matrix_payload["columns"], name="columns"
            ),
            cells=tuple(SparseCell(*item) for item in matrix_payload["cells"]),
            spans=tuple(SegmentSpan(*item) for item in matrix_payload["spans"]),
            horizontal_rule_rows=tuple(
                matrix_payload.get("horizontal_rule_rows", ())
            ),
            vertical_rule_columns=tuple(
                matrix_payload.get("vertical_rule_columns", ())
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FrozenBundleInvariantError("invalid sparse matrix record") from exc
    raw_objects = payload.get("objects")
    if type(raw_objects) is not list:
        raise FrozenBundleInvariantError("objects must be a list")
    try:
        objects = tuple(
            DocumentObject(
                object_id=item["object_id"],
                kind=ObjectKind(item["kind"]),
                segment_ids=tuple(item["segment_ids"]),
                bbox=Box(*item["bbox"]),
                reading_index=item["reading_index"],
                row_start=item["row_start"],
                row_stop=item["row_stop"],
                column_start=item["column_start"],
                column_stop=item["column_stop"],
                confidence=item["confidence"],
                evidence=tuple(item.get("evidence", ())),
            )
            for item in raw_objects
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FrozenBundleInvariantError("invalid Stage 6 object record") from exc
    source_ids = tuple(item.segment_id for item in segments)
    owner_by_segment = {
        segment_id: item.object_id
        for item in objects
        for segment_id in item.segment_ids
    }
    try:
        object_result = ObjectReconstructionResult(
            aligned_size=tuple(payload["aligned_size"]),
            source_segment_ids=source_ids,
            objects=objects,
            segment_ownership=tuple(
                SegmentObjectOwnership(item, owner_by_segment[item])
                for item in source_ids
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FrozenBundleInvariantError(
            "Stage 6 objects are not an exact canonical segment partition"
        ) from exc
    expected = payload.get("expected", {})
    if type(expected) is not dict or type(expected.get("objects", {})) is not dict:
        raise FrozenBundleInvariantError("expected references must be an object")
    return FrozenBundle(
        bundle_id=payload["bundle_id"],
        aligned_size=tuple(payload["aligned_size"]),
        page_png=page_png,
        segments=tuple(segments),
        matrix=matrix,
        objects=object_result,
        segment_texts=tuple(segment_texts),
        expected_object_texts=tuple(expected.get("objects", {}).items()),
        expected_document_text=expected.get("document_text"),
        expected_document_markdown=expected.get("document_markdown"),
        stage1_manual_audit_status=payload.get(
            "stage1_manual_audit_status", "pending"
        ),
    )


def write_frozen_bundle(root: Path, bundle: FrozenBundle) -> None:
    root.mkdir(parents=True, exist_ok=False)
    (root / "page.png").write_bytes(bundle.page_png)
    _write_json(root / "bundle.json", frozen_bundle_payload(bundle))


def _png_bytes(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def synthetic_frozen_bundle() -> FrozenBundle:
    width, height = (380, 300)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    definitions: list[tuple[str, str, Box, int, int, str]] = []

    paragraph_rows = (
        ("Alpha", "beta"),
        ("keeps", "context"),
        ("last", "line"),
    )
    for row_offset, texts in enumerate(paragraph_rows):
        matrix_row = row_offset * 2
        for column_offset, text in enumerate(texts):
            left = 10 + column_offset * 82
            bbox = Box(left, 10 + row_offset * 24, left + 76, 28 + row_offset * 24)
            definitions.append(
                (
                    f"paragraph-{row_offset}-{column_offset}",
                    text,
                    bbox,
                    matrix_row,
                    column_offset * 2,
                    "object-000000",
                )
            )

    for row_offset, texts in enumerate(
        (("1.", "first"), ("2.", "second"), ("3.", "third"))
    ):
        matrix_row = 6 + row_offset * 2
        for column_offset, text in enumerate(texts):
            left = 10 if column_offset == 0 else 48
            right = 42 if column_offset == 0 else 160
            definitions.append(
                (
                    f"list-{row_offset}-{column_offset}",
                    text,
                    Box(left, 92 + row_offset * 24, right, 110 + row_offset * 24),
                    matrix_row,
                    column_offset * 2,
                    "object-000001",
                )
            )

    for row_offset in range(3):
        for column_offset in range(2):
            matrix_row = 12 + row_offset * 2
            matrix_column = column_offset * 2
            left = 200 + column_offset * 82
            top = 190 + row_offset * 32
            text = f"{chr(ord('A') + column_offset)}{row_offset + 1}"
            definitions.append(
                (
                    f"table-{row_offset}-{column_offset}",
                    text,
                    Box(left, top, left + 72, top + 22),
                    matrix_row,
                    matrix_column,
                    "object-000002",
                )
            )

    palette = {
        "object-000000": (219, 238, 255),
        "object-000001": (228, 248, 224),
        "object-000002": (255, 235, 213),
    }
    segments: list[Segment] = []
    text_values: list[tuple[str, str | None]] = []
    cells: list[SparseCell] = []
    spans: list[SegmentSpan] = []
    by_object: dict[str, list[Segment]] = {
        "object-000000": [],
        "object-000001": [],
        "object-000002": [],
    }
    for index, (segment_id, text, bbox, row, column, object_id) in enumerate(
        definitions
    ):
        draw.rectangle(bbox.as_tuple(), fill=palette[object_id], outline=(30, 30, 30))
        draw.text((bbox.left + 3, bbox.top + 3), text, fill=(0, 0, 0))
        segment = Segment(
            segment_id=segment_id,
            bbox=bbox,
            source_bbox=bbox,
            kind=SegmentKind.TEXT,
            ink_pixels=max(1, len(text) * 8),
            row_index=row,
            order_key=(row, column),
            parent_path=("frozen", object_id),
            component_ids=(index,),
        )
        segments.append(segment)
        text_values.append((segment_id, text))
        by_object[object_id].append(segment)
        cells.append(SparseCell(row, column, segment_id))
        spans.append(SegmentSpan(segment_id, row, row + 1, column, column + 1))
    del draw

    objects = (
        DocumentObject(
            "object-000000",
            ObjectKind.PARAGRAPH,
            tuple(item.segment_id for item in by_object["object-000000"]),
            Box.union(item.bbox for item in by_object["object-000000"]),
            0,
            0,
            5,
            0,
            3,
            1.0,
            ("lab:frozen-boundary",),
        ),
        DocumentObject(
            "object-000001",
            ObjectKind.LIST,
            tuple(item.segment_id for item in by_object["object-000001"]),
            Box.union(item.bbox for item in by_object["object-000001"]),
            1,
            6,
            11,
            0,
            3,
            1.0,
            ("lab:frozen-boundary",),
        ),
        DocumentObject(
            "object-000002",
            ObjectKind.TABLE,
            tuple(item.segment_id for item in by_object["object-000002"]),
            Box.union(item.bbox for item in by_object["object-000002"]),
            2,
            12,
            17,
            0,
            3,
            1.0,
            ("lab:frozen-boundary",),
        ),
    )
    source_ids = tuple(item.segment_id for item in segments)
    owner_by_segment = {
        segment_id: item.object_id
        for item in objects
        for segment_id in item.segment_ids
    }
    matrix = SparseSegmentMatrix(
        rows=tuple(AxisInterval(index, index, index + 1) for index in range(17)),
        columns=tuple(AxisInterval(index, index, index + 1) for index in range(3)),
        cells=tuple(sorted(cells, key=lambda item: (item.row, item.column, item.segment_id))),
        spans=tuple(spans),
        horizontal_rule_rows=(13, 15),
        vertical_rule_columns=(1,),
    )
    object_result = ObjectReconstructionResult(
        aligned_size=(width, height),
        source_segment_ids=source_ids,
        objects=objects,
        segment_ownership=tuple(
            SegmentObjectOwnership(item, owner_by_segment[item])
            for item in source_ids
        ),
    )
    paragraph_text = "Alpha beta\nkeeps context\nlast line"
    list_text = "1. first\n2. second\n3. third"
    table_text = "A1\tB1\nA2\tB2\nA3\tB3"
    table_markdown = "| A1 | B1 |\n| A2 | B2 |\n| A3 | B3 |"
    return FrozenBundle(
        bundle_id="synthetic-object-local-v1",
        aligned_size=(width, height),
        page_png=_png_bytes(image),
        segments=tuple(segments),
        matrix=matrix,
        objects=object_result,
        segment_texts=tuple(text_values),
        expected_object_texts=(
            ("object-000000", paragraph_text),
            ("object-000001", list_text),
            ("object-000002", table_text),
        ),
        expected_document_text=(
            f"{paragraph_text}\n\n{list_text}\n\n{table_text}"
        ),
        expected_document_markdown=(
            f"{paragraph_text}\n\n{list_text}\n\n{table_markdown}"
        ),
        stage1_manual_audit_status="synthetic",
    )


def _slice_matrix_for_object(
    matrix: SparseSegmentMatrix,
    document_object: DocumentObject,
) -> SparseSegmentMatrix:
    """Return an index-shifted matrix containing one Stage 6 object only."""

    row_start, row_stop = document_object.row_start, document_object.row_stop
    column_start = document_object.column_start
    column_stop = document_object.column_stop
    if not (
        0 <= row_start < row_stop <= len(matrix.rows)
        and 0 <= column_start < column_stop <= len(matrix.columns)
    ):
        raise LabInvariantError(
            f"object {document_object.object_id} sparse slice lies outside matrix"
        )
    object_ids = set(document_object.segment_ids)
    cells = tuple(
        SparseCell(
            item.row - row_start,
            item.column - column_start,
            item.segment_id,
        )
        for item in matrix.cells
        if item.segment_id in object_ids
    )
    if {item.segment_id for item in cells} != object_ids:
        raise LabInvariantError(
            f"object {document_object.object_id} slice lost matrix members"
        )
    owned_cells: dict[str, list[SparseCell]] = {
        item: [] for item in document_object.segment_ids
    }
    for cell in cells:
        owned_cells[cell.segment_id].append(cell)
    span_by_id: dict[str, SegmentSpan] = {}
    for segment_id in document_object.segment_ids:
        owned = tuple(owned_cells[segment_id])
        span_by_id[segment_id] = SegmentSpan(
            segment_id,
            min(item.row for item in owned),
            max(item.row for item in owned) + 1,
            min(item.column for item in owned),
            max(item.column for item in owned) + 1,
        )
    row_widths = tuple(
        item.end - item.start for item in matrix.rows[row_start:row_stop]
    )
    column_widths = tuple(
        item.end - item.start
        for item in matrix.columns[column_start:column_stop]
    )

    def axes(widths: tuple[int, ...]) -> tuple[AxisInterval, ...]:
        offset = 0
        values: list[AxisInterval] = []
        for index, width in enumerate(widths):
            values.append(AxisInterval(index, offset, offset + width))
            offset += width
        return tuple(values)

    return SparseSegmentMatrix(
        rows=axes(row_widths),
        columns=axes(column_widths),
        cells=tuple(sorted(cells, key=lambda item: (item.row, item.column, item.segment_id))),
        spans=tuple(span_by_id[item] for item in document_object.segment_ids),
        horizontal_rule_rows=tuple(
            item - row_start
            for item in matrix.horizontal_rule_rows
            if row_start <= item < row_stop
        ),
        vertical_rule_columns=tuple(
            item - column_start
            for item in matrix.vertical_rule_columns
            if column_start <= item < column_stop
        ),
    )


def _single_object_result(
    bundle: FrozenBundle,
    document_object: DocumentObject,
) -> ObjectReconstructionResult:
    local_object = DocumentObject(
        object_id="object-000000",
        kind=document_object.kind,
        segment_ids=document_object.segment_ids,
        bbox=document_object.bbox,
        reading_index=0,
        row_start=0,
        row_stop=document_object.row_stop - document_object.row_start,
        column_start=0,
        column_stop=document_object.column_stop - document_object.column_start,
        confidence=document_object.confidence,
        evidence=document_object.evidence + (
            f"lab:source-object={document_object.object_id}",
        ),
    )
    return ObjectReconstructionResult(
        aligned_size=bundle.aligned_size,
        source_segment_ids=document_object.segment_ids,
        objects=(local_object,),
        segment_ownership=tuple(
            SegmentObjectOwnership(item, "object-000000")
            for item in document_object.segment_ids
        ),
        diagnostics=("lab=one-frozen-object-slice",),
    )


def _segments_for_object(
    bundle: FrozenBundle,
    document_object: DocumentObject,
) -> tuple[Segment, ...]:
    segment_by_id = {item.segment_id: item for item in bundle.segments}
    return tuple(segment_by_id[item] for item in document_object.segment_ids)


def plan_one_object(
    bundle: FrozenBundle,
    document_object: DocumentObject,
) -> tuple[BlockPlan, SparseSegmentMatrix]:
    matrix = _slice_matrix_for_object(bundle.matrix, document_object)
    plan = OverlappingBlockPlanner(
        BlockPlanningConfig(mode=BlockPlanningMode.SPATIAL_2D, padding=0)
    ).plan(
        aligned_size=bundle.aligned_size,
        segments=_segments_for_object(bundle, document_object),
        objects_result=_single_object_result(bundle, document_object),
        matrix=matrix,
    )
    assert_object_local_blocks(document_object, plan.blocks)
    return plan, matrix


def assert_object_local_blocks(
    document_object: DocumentObject,
    blocks: Sequence[RecognitionBlock | LabBlock | object],
) -> None:
    allowed = set(document_object.segment_ids)
    for block in blocks:
        member_ids = tuple(getattr(block, "segment_ids"))
        foreign = tuple(item for item in member_ids if item not in allowed)
        if foreign:
            raise LabInvariantError(
                f"block {getattr(block, 'block_id', '<unknown>')} crosses "
                f"object {document_object.object_id}: {foreign}"
            )
        object_ids = tuple(getattr(block, "object_ids", ()))
        if object_ids and len(object_ids) != 1:
            raise LabInvariantError("one block cannot claim multiple object owners")


def _independent_algebra(
    blocks: Sequence[RecognitionBlock | LabBlock],
    source_ids: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    order = {item: index for index, item in enumerate(source_ids)}
    values: list[dict[str, object]] = []
    for first_index, first in enumerate(blocks):
        first_members = set(first.segment_ids)
        for second in blocks[first_index + 1 :]:
            second_members = set(second.segment_ids)
            intersection = first_members & second_members
            if not intersection:
                continue

            def canonical(items: set[str]) -> list[str]:
                return sorted(items, key=order.__getitem__)

            values.append(
                {
                    "first_block_id": first.block_id,
                    "second_block_id": second.block_id,
                    "intersection_segment_ids": canonical(intersection),
                    "union_segment_ids": canonical(first_members | second_members),
                    "xor_segment_ids": canonical(first_members ^ second_members),
                    "first_only_segment_ids": canonical(
                        first_members - second_members
                    ),
                    "second_only_segment_ids": canonical(
                        second_members - first_members
                    ),
                }
            )
    return tuple(values)


def _decode_oracle_membership(
    blocks: Sequence[RecognitionBlock | LabBlock],
    source_ids: tuple[str, ...],
) -> tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]:
    block_ids_by_segment: dict[str, list[str]] = {item: [] for item in source_ids}
    for block in blocks:
        for segment_id in block.segment_ids:
            if segment_id not in block_ids_by_segment:
                raise LabInvariantError(
                    f"oracle block contains unknown segment {segment_id}"
                )
            block_ids_by_segment[segment_id].append(block.block_id)
    groups: dict[tuple[str, ...], list[str]] = {}
    for segment_id in source_ids:
        signature = tuple(block_ids_by_segment[segment_id])
        if not signature:
            continue
        groups.setdefault(signature, []).append(segment_id)
    source_order = {item: index for index, item in enumerate(source_ids)}
    return tuple(
        (
            f"decoded-{index:06d}",
            tuple(segment_ids),
            signature,
        )
        for index, (signature, segment_ids) in enumerate(
            sorted(
                groups.items(),
                key=lambda item: min(source_order[value] for value in item[1]),
            )
        )
    )


def _membership_diff(
    source_ids: tuple[str, ...],
    decoded: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...],
) -> dict[str, object]:
    flattened = [segment_id for _, members, _ in decoded for segment_id in members]
    actual = set(flattened)
    expected = set(source_ids)
    counts: dict[str, int] = {}
    for item in flattened:
        counts[item] = counts.get(item, 0) + 1
    duplicate_ids = [item for item in source_ids if counts.get(item, 0) > 1]
    merged = [
        {
            "unit_id": unit_id,
            "segment_ids": list(members),
            "block_ids": list(signature),
        }
        for unit_id, members, signature in decoded
        if len(members) > 1
    ]
    lost = [item for item in source_ids if item not in actual]
    extra = sorted(actual - expected)
    return {
        "status": (
            "exact"
            if not lost and not extra and not duplicate_ids and not merged
            else "failed"
        ),
        "expected_segment_ids": list(source_ids),
        "lost_segment_ids": lost,
        "extra_segment_ids": extra,
        "duplicate_segment_ids": duplicate_ids,
        "merged_units": merged,
    }


def _crop_png(image: Image.Image, box: Box) -> bytes:
    crop = image.crop(box.as_tuple())
    try:
        return _png_bytes(crop)
    finally:
        crop.close()


def _write_actual_segments(root: Path, bundle: FrozenBundle) -> None:
    owner = {
        item.segment_id: item.object_id for item in bundle.objects.segment_ownership
    }
    with Image.open(io.BytesIO(bundle.page_png)) as image:
        for segment in bundle.segments:
            destination = root / owner[segment.segment_id]
            destination.mkdir(parents=True, exist_ok=True)
            (destination / f"{segment.segment_id}.png").write_bytes(
                _crop_png(image, segment.bbox)
            )
    _write_jsonl(
        root / "segments.jsonl",
        (
            {
                "segment_id": item.segment_id,
                "object_id": owner[item.segment_id],
                "bbox": _box_payload(item.bbox),
                "png": f"{owner[item.segment_id]}/{item.segment_id}.png",
                "sha256": _sha256(
                    (root / owner[item.segment_id] / f"{item.segment_id}.png").read_bytes()
                ),
            }
            for item in bundle.segments
        ),
    )


def _block_record(block: RecognitionBlock | LabBlock, *, png: str) -> dict[str, object]:
    return {
        "block_id": block.block_id,
        "bbox": _box_payload(block.bbox),
        "segment_ids": list(block.segment_ids),
        "core_segment_ids": list(getattr(block, "core_segment_ids", ())),
        "context_segment_ids": list(getattr(block, "context_segment_ids", ())),
        "scope_id": getattr(block, "scope_id", None),
        "policy": getattr(block, "policy", "production-spatial-2d"),
        "png": png,
    }


def _write_blocks(
    root: Path,
    *,
    bundle: FrozenBundle,
    blocks: Sequence[RecognitionBlock | LabBlock],
) -> tuple[dict[str, object], ...]:
    root.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    with Image.open(io.BytesIO(bundle.page_png)) as image:
        for block in blocks:
            name = f"{block.block_id}.png"
            payload = _crop_png(image, block.bbox)
            (root / name).write_bytes(payload)
            record = _block_record(block, png=name)
            record["png_sha256"] = _sha256(payload)
            records.append(record)
    _write_jsonl(root / "blocks.jsonl", records)
    return tuple(records)


def run_orxor_lab(bundle: FrozenBundle, root: Path) -> dict[str, object]:
    """Run table membership algebra without OCR, fusion, or assembly."""

    root.mkdir(parents=True, exist_ok=False)
    summaries: list[dict[str, object]] = []
    for document_object in bundle.objects.objects:
        if document_object.kind is not ObjectKind.TABLE:
            summaries.append(
                {
                    "object_id": document_object.object_id,
                    "kind": document_object.kind.value,
                    "status": "skipped",
                    "reason": "OR/XOR lab targets table object slices only",
                }
            )
            continue
        object_root = root / document_object.object_id
        object_root.mkdir()
        plan, local_matrix = plan_one_object(bundle, document_object)
        records = _write_blocks(
            object_root / "blocks", bundle=bundle, blocks=plan.blocks
        )
        segment_by_id = {item.segment_id: item for item in bundle.segments}
        oracle_jobs = tuple(
            {
                "job_id": f"oracle-{block.block_id}",
                "block_id": block.block_id,
                "object_id": document_object.object_id,
                "engine": "deterministic-membership-oracle",
                "observations": [
                    {
                        "segment_id": segment_id,
                        # The oracle labels membership only. It deliberately
                        # does not read ready/reference text from the bundle.
                        "token": segment_id,
                        "local_bbox": [
                            segment.bbox.left - block.bbox.left,
                            segment.bbox.top - block.bbox.top,
                            segment.bbox.right - block.bbox.left,
                            segment.bbox.bottom - block.bbox.top,
                        ],
                    }
                    for segment_id in block.segment_ids
                    for segment in (segment_by_id[segment_id],)
                ],
            }
            for block in plan.blocks
        )
        _write_jsonl(object_root / "oracle-jobs.jsonl", oracle_jobs)
        decoded = _decode_oracle_membership(
            plan.blocks, document_object.segment_ids
        )
        decoded_records = tuple(
            {
                "unit_id": unit_id,
                "kind": "segment" if len(members) == 1 else "subblock",
                "segment_ids": list(members),
                "block_ids": list(signature),
            }
            for unit_id, members, signature in decoded
        )
        _write_jsonl(object_root / "decoded-membership.jsonl", decoded_records)
        expected_records = tuple(
            {
                "segment_id": segment_id,
                "expected_unit": [segment_id],
            }
            for segment_id in document_object.segment_ids
        )
        _write_jsonl(object_root / "expected-membership.jsonl", expected_records)
        diff = _membership_diff(document_object.segment_ids, decoded)
        independent_algebra = _independent_algebra(
            plan.blocks, document_object.segment_ids
        )
        production_algebra = tuple(
            {
                "first_block_id": item.first_block_id,
                "second_block_id": item.second_block_id,
                "intersection_segment_ids": list(item.intersection_segment_ids),
                "union_segment_ids": list(item.union_segment_ids),
                "xor_segment_ids": list(item.xor_segment_ids),
                "first_only_segment_ids": list(item.first_only_segment_ids),
                "second_only_segment_ids": list(item.second_only_segment_ids),
            }
            for item in plan.adjacent_algebra
        )
        algebra_exact = independent_algebra == production_algebra
        diff["algebra_status"] = "exact" if algebra_exact else "failed"
        diff["production_membership_units"] = [
            {
                "kind": item.kind.value,
                "segment_ids": list(item.segment_ids),
                "block_ids": list(item.block_ids),
            }
            for item in plan.membership_units
        ]
        _write_jsonl(object_root / "algebra-independent.jsonl", independent_algebra)
        _write_jsonl(object_root / "algebra-production.jsonl", production_algebra)
        _write_json(object_root / "diff.json", diff)
        (object_root / "diff.txt").write_text(
            "\n".join(
                (
                    f"membership={diff['status']}",
                    f"algebra={diff['algebra_status']}",
                    "lost=" + ",".join(diff["lost_segment_ids"]),
                    "extra=" + ",".join(diff["extra_segment_ids"]),
                    "duplicate=" + ",".join(diff["duplicate_segment_ids"]),
                    "merged_units=" + str(len(diff["merged_units"])),
                    "",
                )
            ),
            encoding="utf-8",
        )
        _write_json(object_root / "matrix-slice.json", sparse_matrix_payload(local_matrix))
        status = (
            "exact"
            if diff["status"] == "exact" and diff["algebra_status"] == "exact"
            else "failed"
        )
        report = [
            f"# OR/XOR lab: {document_object.object_id}",
            "",
            f"Status: **{status}**",
            "",
            "Called: production Stage 5 object-local spatial planner; independent deterministic membership oracle.",
            "",
            "Skipped: Stage 1 geometry, Stage 2 OCR/fusion, Stage 7 assembly.",
            "",
            (
                f"Segments: {len(document_object.segment_ids)}; actual blocks: "
                f"{len(plan.blocks)}; decoded units: {len(decoded)}."
            ),
            "",
            "Actual block PNGs (not overlays):",
            "",
        ]
        report.extend(
            f"- [{item['block_id']}](blocks/{item['png']}): "
            + ", ".join(item["segment_ids"])
            for item in records
        )
        report.extend(
            [
                "",
                (
                    f"Lost: {diff['lost_segment_ids'] or 'none'}; extra: "
                    f"{diff['extra_segment_ids'] or 'none'}; merged: "
                    f"{len(diff['merged_units'])}."
                ),
                "",
            ]
        )
        (object_root / "report.md").write_text(
            "\n".join(report), encoding="utf-8"
        )
        summaries.append(
            {
                "object_id": document_object.object_id,
                "kind": document_object.kind.value,
                "status": status,
                "segments": len(document_object.segment_ids),
                "blocks": len(plan.blocks),
                "decoded_units": len(decoded),
                "merged_units": len(diff["merged_units"]),
            }
        )
    tested = [item for item in summaries if item["status"] != "skipped"]
    status = "exact" if tested and all(item["status"] == "exact" for item in tested) else "failed"
    summary = {
        "lab": "orxor-only",
        "status": status,
        "called": [
            "Stage5.OverlappingBlockPlanner.plan(one Stage6 object slice)",
            "independent deterministic membership oracle",
        ],
        "skipped": [
            "Stage1.GeometryAnalyzer",
            "Stage2.OCR",
            "Stage2.OcrEvidenceFusion",
            "Stage7.DocumentAssembler",
        ],
        "objects": summaries,
    }
    _write_json(root / "summary.json", summary)
    report_lines = [
        "# OR/XOR-only lab",
        "",
        f"Status: **{status}**",
        "",
        "| object | kind | status | segments | blocks | decoded units | merged |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    report_lines.extend(
        "| {object_id} | {kind} | {status} | {segments} | {blocks} | "
        "{decoded_units} | {merged_units} |".format(**item)
        for item in summaries
        if item["status"] != "skipped"
    )
    report_lines.extend(("", "Each tested object has its own `report.md`.", ""))
    (root / "report.md").write_text(
        "\n".join(report_lines), encoding="utf-8"
    )
    return summary


def _ready_segment_assembly(
    segment_id: str,
    object_id: str,
    text: str,
) -> SegmentTextAssembly:
    return SegmentTextAssembly(
        segment_id=segment_id,
        object_id=object_id,
        candidate_text=text,
        text=text,
        attribution_level=AttributionLevel.SEGMENT,
        selected_observation_id=None,
        evidence_slice_ids=(),
        status=AssemblyStatus.COMPLETE,
    )


def run_assembly_only_lab(bundle: FrozenBundle, root: Path) -> dict[str, object]:
    """Assemble ready segment text via the production Stage 7 rendering core.

    ``DocumentAssembler.assemble`` is deliberately not used: its provenance
    guard correctly replays Stages 6, 5, 4, and 2.  This lab instead calls the
    production ready-text rendering/grammar/Markdown methods directly, so no
    upstream stage can contaminate the result.
    """

    root.mkdir(parents=True, exist_ok=False)
    text_by_segment = bundle.text_by_segment
    missing = [item for item, text in bundle.segment_texts if text is None]
    if missing:
        raise LabInvariantError(
            "assembly-only lab needs ready text for every segment; missing "
            + ", ".join(missing)
        )
    assembler = DocumentAssembler()
    span_by_id = {item.segment_id: item for item in bundle.matrix.spans}
    coordinates_by_segment: dict[str, list[tuple[int, int]]] = {
        item.segment_id: [] for item in bundle.segments
    }
    for cell in bundle.matrix.cells:
        coordinates_by_segment[cell.segment_id].append((cell.row, cell.column))
    ready_by_id: dict[str, SegmentTextAssembly] = {}
    trace_by_id: dict[str, _ReadyTextTrace] = {}
    owner_by_segment = {
        item.segment_id: item.object_id for item in bundle.objects.segment_ownership
    }
    for segment_id, value in bundle.segment_texts:
        assert value is not None
        ready_by_id[segment_id] = _ready_segment_assembly(
            segment_id, owner_by_segment[segment_id], value
        )
        trace_by_id[segment_id] = _ReadyTextTrace()
    object_results: list[dict[str, object]] = []
    object_texts: list[str] = []
    object_markdown: list[str] = []
    expected_by_object = dict(bundle.expected_object_texts)
    for document_object in bundle.objects.objects:
        object_root = root / "objects" / document_object.object_id
        object_root.mkdir(parents=True)
        segment_values = tuple(
            ready_by_id[item] for item in document_object.segment_ids
        )
        text, placements = assembler._render_segment_values(
            document_object=document_object,
            segment_values=segment_values,
            span_by_id=span_by_id,
            fused_by_id=trace_by_id,  # type: ignore[arg-type]
            observation_by_id={},
            job_by_id={},
        )
        if placements:
            raise LabInvariantError(
                "ready-text adapter unexpectedly manufactured OCR evidence"
            )
        drafts = assembler._structural_drafts(
            document_object=document_object,
            span_by_id=span_by_id,
            matrix=bundle.matrix,
            coordinates_by_segment={
                item: tuple(coordinates_by_segment[item])
                for item in document_object.segment_ids
            },
        )
        units: list[StructuralUnit] = []
        for draft in drafts:
            draft_values = tuple(ready_by_id[item] for item in draft.segment_ids)
            unit_text, unit_placements = assembler._render_segment_values(
                document_object=document_object,
                segment_values=draft_values,
                span_by_id=span_by_id,
                fused_by_id=trace_by_id,  # type: ignore[arg-type]
                observation_by_id={},
                job_by_id={},
            )
            if unit_placements:
                raise LabInvariantError(
                    "ready-text unit unexpectedly manufactured OCR evidence"
                )
            units.append(
                StructuralUnit(
                    unit_id=f"unit-{sum(len(item.get('units', [])) for item in object_results) + len(units):08d}",
                    object_id=document_object.object_id,
                    kind=document_object.kind,
                    unit_kind=draft.unit_kind,
                    segment_ids=draft.segment_ids,
                    row_start=draft.row_start,
                    row_stop=draft.row_stop,
                    column_start=draft.column_start,
                    column_stop=draft.column_stop,
                    candidate_text=unit_text,
                    text=unit_text,
                    evidence_slice_ids=(),
                    status=AssemblyStatus.COMPLETE,
                )
            )
        rows, columns = assembler._table_axes(
            document_object=document_object,
            matrix=bundle.matrix,
        )
        markdown = assembler._object_markdown(
            document_object.kind,
            text,
            tuple(units),
            table_row_indices=rows,
            table_column_indices=columns,
        )
        expected = expected_by_object.get(document_object.object_id)
        exact = None if expected is None else text == expected
        (object_root / "object.txt").write_text(text, encoding="utf-8")
        (object_root / "object.md").write_text(markdown, encoding="utf-8")
        unit_records = tuple(
            {
                "unit_id": item.unit_id,
                "kind": item.unit_kind.value,
                "segment_ids": list(item.segment_ids),
                "row_start": item.row_start,
                "row_stop": item.row_stop,
                "column_start": item.column_start,
                "column_stop": item.column_stop,
                "text": item.candidate_text,
            }
            for item in units
        )
        _write_jsonl(object_root / "units.jsonl", unit_records)
        object_results.append(
            {
                "object_id": document_object.object_id,
                "kind": document_object.kind.value,
                "text": text,
                "markdown": markdown,
                "reference_status": (
                    "pending" if exact is None else "exact" if exact else "failed"
                ),
                "units": list(unit_records),
            }
        )
        object_texts.append(text)
        object_markdown.append(markdown)
    document_text, offsets = assembler._join_with_offsets(
        tuple(object_texts), separator="\n\n"
    )
    document_markdown, markdown_offsets = assembler._join_with_offsets(
        tuple(object_markdown), separator="\n\n"
    )
    (root / "document.txt").write_text(document_text, encoding="utf-8")
    (root / "document.md").write_text(document_markdown, encoding="utf-8")
    text_exact = (
        None
        if bundle.expected_document_text is None
        else document_text == bundle.expected_document_text
    )
    markdown_exact = (
        None
        if bundle.expected_document_markdown is None
        else document_markdown == bundle.expected_document_markdown
    )
    failed_object = any(
        item["reference_status"] == "failed" for item in object_results
    )
    pending_reference = (
        text_exact is None
        or markdown_exact is None
        or any(item["reference_status"] == "pending" for item in object_results)
    )
    status = (
        "failed"
        if failed_object or text_exact is False or markdown_exact is False
        else "pending-reference"
        if pending_reference
        else "exact"
    )
    diff = {
        "status": status,
        "document_text_exact": text_exact,
        "document_markdown_exact": markdown_exact,
        "expected_document_text": bundle.expected_document_text,
        "actual_document_text": document_text,
        "expected_document_markdown": bundle.expected_document_markdown,
        "actual_document_markdown": document_markdown,
        "object_offsets": [list(item) for item in offsets],
        "object_markdown_offsets": [list(item) for item in markdown_offsets],
        "objects": object_results,
    }
    _write_json(root / "diff.json", diff)
    (root / "diff.txt").write_text(
        "\n".join(
            (
                f"status={status}",
                f"document_text_exact={text_exact}",
                f"document_markdown_exact={markdown_exact}",
                *(
                    f"{item['object_id']}={item['reference_status']}"
                    for item in object_results
                ),
                "",
            )
        ),
        encoding="utf-8",
    )
    summary = {
        "lab": "assembly-only",
        "status": status,
        "called": [
            "Stage7.DocumentAssembler._render_segment_values",
            "Stage7.DocumentAssembler._structural_drafts",
            "Stage7.DocumentAssembler._table_axes",
            "Stage7.DocumentAssembler._object_markdown",
            "Stage7.DocumentAssembler._join_with_offsets",
        ],
        "skipped": [
            "Stage1.GeometryAnalyzer",
            "Stage6.ObjectReconstructor",
            "Stage5.OverlappingBlockPlanner",
            "Stage4.BlockCropper/enhancer",
            "Stage2.OCR queue",
            "Stage2.OcrEvidenceFusion",
            "Stage7.DocumentAssembler.assemble provenance replay",
        ],
        "objects": len(object_results),
        "segments": len(bundle.segments),
        "document_text_sha256": _sha256(document_text.encode("utf-8")),
        "document_markdown_sha256": _sha256(document_markdown.encode("utf-8")),
    }
    _write_json(root / "summary.json", summary)
    (root / "report.md").write_text(
        "\n".join(
            (
                "# Assembly-only lab",
                "",
                f"Status: **{status}**",
                "",
                "Input is ready segment text plus frozen matrix/object boundaries.",
                "",
                (
                    "The lab calls the production Stage 7 rendering, "
                    "structural-draft, table-axis, Markdown, and join core "
                    "directly. It does not call geometry, object reconstruction, "
                    "block planning, crop enhancement, OCR, or fusion."
                ),
                "",
                (
                    "Per-object files are under `objects/<object-id>/`; the "
                    "assembled outputs are `document.txt` and `document.md`."
                ),
                "",
            )
        ),
        encoding="utf-8",
    )
    return summary


def _rows_for_object(
    document_object: DocumentObject,
    matrix: SparseSegmentMatrix,
) -> tuple[tuple[str, ...], ...]:
    span_by_id = {item.segment_id: item for item in matrix.spans}
    grouped: dict[int, list[str]] = {}
    for segment_id in document_object.segment_ids:
        grouped.setdefault(span_by_id[segment_id].row_start, []).append(segment_id)
    return tuple(tuple(values) for _, values in sorted(grouped.items()))


def paragraph_blocks(
    bundle: FrozenBundle,
    document_object: DocumentObject,
    *,
    policy: str,
) -> tuple[LabBlock, ...]:
    if document_object.kind is not ObjectKind.PARAGRAPH:
        raise LabInvariantError("paragraph policy needs a paragraph object")
    rows = _rows_for_object(document_object, bundle.matrix)
    if policy == "whole-object":
        groups = (document_object.segment_ids,)
    elif policy == "overlapping-multiline":
        groups = (
            tuple(segment_id for row in pair for segment_id in row)
            for pair in zip(rows, rows[1:])
        )
        groups = tuple(groups) or (document_object.segment_ids,)
    elif policy == "per-line-control":
        groups = rows
    else:
        raise ValueError(f"unknown paragraph policy: {policy}")
    segment_by_id = {item.segment_id: item for item in bundle.segments}
    values = tuple(
        LabBlock(
            block_id=f"{policy}-block-{index:06d}",
            object_id=document_object.object_id,
            segment_ids=tuple(group),
            bbox=Box.union(segment_by_id[item].bbox for item in group),
            policy=policy,
        )
        for index, group in enumerate(groups)
    )
    assert_object_local_blocks(document_object, values)
    return values


def list_item_blocks(
    bundle: FrozenBundle,
    document_object: DocumentObject,
) -> tuple[LabBlock, ...]:
    if document_object.kind is not ObjectKind.LIST:
        raise LabInvariantError("list item policy needs a list object")
    rows = _rows_for_object(document_object, bundle.matrix)
    segment_by_id = {item.segment_id: item for item in bundle.segments}
    values = tuple(
        LabBlock(
            block_id=f"item-aware-block-{index:06d}",
            object_id=document_object.object_id,
            segment_ids=row,
            bbox=Box.union(segment_by_id[item].bbox for item in row),
            policy="item-aware",
        )
        for index, row in enumerate(rows)
    )
    assert_object_local_blocks(document_object, values)
    return values


def _topology_metrics(
    document_object: DocumentObject,
    blocks: Sequence[RecognitionBlock | LabBlock],
    *,
    stage1_manual_audit_status: str,
) -> dict[str, object]:
    source_ids = document_object.segment_ids
    decoded = _decode_oracle_membership(blocks, source_ids)
    diff = _membership_diff(source_ids, decoded)
    total_pairs = len(source_ids) * (len(source_ids) - 1) // 2
    cooccurring: set[tuple[str, str]] = set()
    order = {item: index for index, item in enumerate(source_ids)}
    for block in blocks:
        for left_index, first in enumerate(block.segment_ids):
            for second in block.segment_ids[left_index + 1 :]:
                pair = (
                    (first, second)
                    if order[first] < order[second]
                    else (second, first)
                )
                cooccurring.add(pair)
    total_memberships = sum(len(item.segment_ids) for item in blocks)
    return {
        "block_count": len(blocks),
        "crop_pixels": sum(item.bbox.area for item in blocks),
        "total_segment_memberships": total_memberships,
        "duplicate_memberships": total_memberships - len(source_ids),
        "whole_object_block_count": sum(
            tuple(item.segment_ids) == source_ids for item in blocks
        ),
        "singleton_block_count": sum(len(item.segment_ids) == 1 for item in blocks),
        "oracle_membership_status": diff["status"],
        "oracle_singleton_units": sum(len(item[1]) == 1 for item in decoded),
        "oracle_merged_units": len(diff["merged_units"]),
        "topology_context_pair_fraction": (
            1.0 if total_pairs == 0 else len(cooccurring) / total_pairs
        ),
        "real_ocr": {
            "status": "pending",
            "elapsed_seconds": None,
            "text_accuracy_percent": None,
            "assembly_accuracy_percent": None,
            "reason": (
                "real OCR adapter is intentionally disabled in the topology lab"
                if stage1_manual_audit_status == "accepted"
                else "frozen Stage 1 crops have not passed complete manual audit"
            ),
        },
    }


def run_granularity_lab(bundle: FrozenBundle, root: Path) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=False)
    results: list[dict[str, object]] = []
    for document_object in bundle.objects.objects:
        strategies: list[tuple[str, Sequence[RecognitionBlock | LabBlock]]] = []
        if document_object.kind is ObjectKind.PARAGRAPH:
            strategies.extend(
                (
                    policy,
                    paragraph_blocks(
                        bundle, document_object, policy=policy
                    ),
                )
                for policy in (
                    "whole-object",
                    "overlapping-multiline",
                    "per-line-control",
                )
            )
        elif document_object.kind is ObjectKind.LIST:
            strategies.append(
                ("item-aware", list_item_blocks(bundle, document_object))
            )
        elif document_object.kind is ObjectKind.TABLE:
            plan, _matrix = plan_one_object(bundle, document_object)
            strategies.append(("table-object-local-orxor", plan.blocks))
        for policy, blocks in strategies:
            strategy_root = root / document_object.object_id / policy
            records = _write_blocks(
                strategy_root / "blocks", bundle=bundle, blocks=blocks
            )
            metrics = _topology_metrics(
                document_object,
                blocks,
                stage1_manual_audit_status=bundle.stage1_manual_audit_status,
            )
            if policy == "item-aware":
                metrics["item_boundary_exact"] = tuple(
                    item.segment_ids for item in blocks
                ) == _rows_for_object(document_object, bundle.matrix)
            _write_json(strategy_root / "metrics.json", metrics)
            _write_jsonl(
                strategy_root / "oracle-membership.jsonl",
                (
                    {
                        "unit_id": unit_id,
                        "segment_ids": list(members),
                        "block_ids": list(signature),
                    }
                    for unit_id, members, signature in _decode_oracle_membership(
                        blocks, document_object.segment_ids
                    )
                ),
            )
            results.append(
                {
                    "object_id": document_object.object_id,
                    "kind": document_object.kind.value,
                    "policy": policy,
                    **metrics,
                    "actual_block_pngs": [
                        f"{document_object.object_id}/{policy}/blocks/{item['png']}"
                        for item in records
                    ],
                }
            )
    paragraph_results = [
        item for item in results if item["kind"] == ObjectKind.PARAGRAPH.value
    ]
    whole = next(
        (
            item
            for item in paragraph_results
            if item["policy"] == "whole-object"
        ),
        None,
    )
    invariants = {
        "whole_paragraph_is_one_block": bool(
            whole is not None and whole["block_count"] == 1
        ),
        "all_blocks_object_local": True,
        "list_items_are_separate": all(
            item.get("item_boundary_exact") is True
            for item in results
            if item["policy"] == "item-aware"
        ),
        "table_six_segments_recovered": any(
            item["policy"] == "table-object-local-orxor"
            and item["oracle_singleton_units"] == 6
            and item["oracle_merged_units"] == 0
            for item in results
        ),
    }
    status = "topology-exact-ocr-pending" if all(invariants.values()) else "failed"
    summary = {
        "lab": "paragraph-granularity",
        "status": status,
        "called": [
            "deterministic topology/oracle policies",
            "Stage5.OverlappingBlockPlanner.plan(table object slice only)",
        ],
        "skipped": [
            "Stage1.GeometryAnalyzer",
            "Stage2 real OCR/fusion",
            "Stage7 assembly",
        ],
        "stage1_manual_audit_status": bundle.stage1_manual_audit_status,
        "invariants": invariants,
        "strategies": results,
        "decision": {
            "status": "pending-real-ocr",
            "reason": "topology cannot prove speed or OCR/context quality",
            "required_metrics": [
                "elapsed_seconds",
                "text_accuracy_percent",
                "assembly_accuracy_percent",
                "duplicate_text_count",
                "context_loss_count",
            ],
        },
    }
    _write_json(root / "summary.json", summary)
    report = [
        "# Paragraph/block granularity lab",
        "",
        f"Status: **{status}**",
        "",
        (
            "This run proves only topology with a deterministic oracle. Real "
            "OCR time and quality remain fail-closed pending; no policy is "
            "promoted from these proxy metrics."
        ),
        "",
        (
            "| object | kind | policy | blocks | memberships | context-pair "
            "fraction | resolved singleton units | real OCR |"
        ),
        "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    report.extend(
        "| {object_id} | {kind} | {policy} | {block_count} | "
        "{total_segment_memberships} | {topology_context_pair_fraction:.3f} | "
        "{oracle_singleton_units} | pending |".format(**item)
        for item in results
    )
    report.extend(
        (
            "",
            "Each strategy directory contains actual cropped block PNGs, not page overlays.",
            "",
        )
    )
    (root / "report.md").write_text("\n".join(report), encoding="utf-8")
    return summary


def _inventory(root: Path) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path.read_bytes()),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    )


def run_labs(
    bundle: FrozenBundle,
    *,
    output_root: Path,
    run_id: str,
    labs: tuple[str, ...] = ("orxor", "assembly", "granularity"),
) -> Path:
    if not _SAFE_RUN_ID.fullmatch(run_id):
        raise ValueError("run_id must be a safe 1-128 character path component")
    unknown = set(labs) - {"orxor", "assembly", "granularity"}
    if unknown or not labs or len(labs) != len(set(labs)):
        raise ValueError("labs must be a unique non-empty known lab tuple")
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / run_id
    staging = Path(
        tempfile.mkdtemp(prefix=f".{run_id}.", dir=output_root)
    )
    try:
        input_root = staging / "input"
        input_root.mkdir()
        (input_root / "page.png").write_bytes(bundle.page_png)
        _write_json(input_root / "bundle.json", frozen_bundle_payload(bundle))
        _write_actual_segments(staging / "actual-segments", bundle)
        summaries: dict[str, object] = {}
        if "orxor" in labs:
            summaries["orxor"] = run_orxor_lab(bundle, staging / "orxor")
        if "assembly" in labs:
            summaries["assembly"] = run_assembly_only_lab(
                bundle, staging / "assembly"
            )
        if "granularity" in labs:
            summaries["granularity"] = run_granularity_lab(
                bundle, staging / "granularity"
            )
        statuses = [
            value["status"] for value in summaries.values()  # type: ignore[index]
        ]
        failed = any(value == "failed" for value in statuses)
        pending_reference = any(value == "pending-reference" for value in statuses)
        report = [
            "# Isolated object-local sparse labs",
            "",
            f"Frozen input: `{bundle.bundle_id}` (`{frozen_bundle_sha256(bundle)}`).",
            "",
            "| lab | status | report |",
            "| --- | --- | --- |",
        ]
        if "orxor" in summaries:
            report.append(
                f"| OR/XOR only | {summaries['orxor']['status']} | "  # type: ignore[index]
                "[report](orxor/report.md) |"
            )
        if "assembly" in summaries:
            report.append(
                f"| assembly only | {summaries['assembly']['status']} | "  # type: ignore[index]
                "[report](assembly/report.md) |"
            )
        if "granularity" in summaries:
            report.append(
                f"| granularity | {summaries['granularity']['status']} | "  # type: ignore[index]
                "[report](granularity/report.md) |"
            )
        report.extend(
            (
                "",
                "Actual segment crops are under `actual-segments/`; actual block "
                "crops are inside each lab. No overlay is used as evidence.",
                "",
                "Real OCR timing and quality are pending by design and cannot be "
                "inferred from the deterministic topology oracle.",
                "",
            )
        )
        (staging / "report.md").write_text("\n".join(report), encoding="utf-8")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "status": (
                "failed"
                if failed
                else "pending-reference-and-ocr"
                if pending_reference
                else "complete-with-pending-ocr"
            ),
            "immutable": True,
            "input": {
                "bundle_id": bundle.bundle_id,
                "bundle_sha256": frozen_bundle_sha256(bundle),
                "page_sha256": _sha256(bundle.page_png),
                "stage1_manual_audit_status": bundle.stage1_manual_audit_status,
                "normalized_bundle": "input/bundle.json",
                "page": "input/page.png",
            },
            "labs": summaries,
            "called_stages": {
                "orxor": [5],
                "assembly": [7],
                "granularity": [5],
            },
            "skipped_stages": {
                "orxor": [3, 1, 6, 4, 2, 7],
                "assembly": [3, 1, 6, 4, 5, 2],
                "granularity": [3, 1, 6, 4, 2, 7],
            },
            "real_ocr": {
                "status": "pending",
                "reason": (
                    "adapter intentionally disabled in this isolated topology run"
                    if bundle.stage1_manual_audit_status == "accepted"
                    else "disabled until a frozen Stage 1 bundle passes complete manual crop audit"
                ),
            },
            "inventory": _inventory(staging),
        }
        _write_json(staging / "manifest.json", manifest)
        rename_no_replace(staging, destination)
        return destination
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run isolated object-local OR/XOR, assembly, and granularity labs"
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        help="normalized bundle directory or bundle.json; default is synthetic",
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--lab",
        action="append",
        choices=("orxor", "assembly", "granularity"),
        dest="labs",
        help="run only selected lab(s); repeat option; default runs all",
    )
    parser.add_argument(
        "--export-synthetic-bundle",
        type=Path,
        help="write the normalized synthetic input and exit",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    bundle = (
        load_frozen_bundle(args.bundle)
        if args.bundle is not None
        else synthetic_frozen_bundle()
    )
    if args.export_synthetic_bundle is not None:
        write_frozen_bundle(args.export_synthetic_bundle, bundle)
        print(args.export_synthetic_bundle)
        return 0
    destination = run_labs(
        bundle,
        output_root=args.output_root,
        run_id=args.run_id,
        labs=tuple(args.labs) if args.labs else ("orxor", "assembly", "granularity"),
    )
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
