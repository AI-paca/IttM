#!/usr/bin/env python3
"""Exercise the frozen v16 matrix hand-off without rewriting Stage 1.

The lab has four deliberately isolated parts:

* literal v16 ``recursion.json``/``matrix.tsv`` loading and replay of its
  serialized group boundaries from contiguous occupied rows;
* object-scoped sliding block topologies and actual PNG crops, alongside an
  explicit current-production-contract incompatibility result;
* a synthetic observed-word lattice decoder which uses block membership,
  transform coverage and OCR word geometry, never segment bounding boxes;
* production Stage 7 rendering from already prepared synthetic segment text.

The frozen v16 group metadata is loaded only after matrix replay and is used
solely for an integrity comparison.  This is not independent object detection:
historical v16 inserted the same blank rows after grouping its leaves.  This
script does not import or call Stage 1
``GeometryAnalyzer`` and does not manufacture a current ``SparseSegmentMatrix``
from legacy anchors/codes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "ocr"))

from app.sparse_pipeline.atomic_publish import rename_no_replace  # noqa: E402
from app.sparse_pipeline.contracts import (  # noqa: E402
    AxisInterval,
    Box,
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
)


SCHEMA_VERSION = 1
DEFAULT_INPUT_ROOT = (
    REPOSITORY_ROOT
    / "debug"
    / "labs"
    / "legacy-recursion-v8-v16-20260720-v1"
    / "v16"
)
DEFAULT_OUTPUT_ROOT = REPOSITORY_ROOT / "debug" / "labs"
DEFAULT_RUN_ID = "v16-matrix-handoff-20260720-v1"
SOURCE_KEYS = ("000041", "09")
EXPECTED_TRANSFORMS = ("raw", "gamma")
EXPECTED_READY_TEXT = "item\tbeta\ngamma\t|\nitem\tzeta"
EXPECTED_READY_MARKDOWN = "| item | beta |\n| gamma | \\| |\n| item | zeta |"
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
FROZEN_INPUT_DIGESTS = {
    "000041": {
        "source": "e39ad3e7d7e2d83489f82c88b616c7a425d541ac077771c792c967d5438f137c",
        "recursion": "666387dcbbc2e892a63e96e45e5264363deaeac8f8ec624a7aaf85c9eb98f708",
        "matrix_tsv": "a18a27b91f06e432fd1c3a697f87cbd848270a704be1cb52089bde980b9e0fa9",
    },
    "09": {
        "source": "d446ce8dffd91f434b06280aba8884ad0abae0a984001320774d713945e9cf70",
        "recursion": "27bb83a37e5cd8be0e14f645be66be2dc50db37e790b04085b2f253117230d4b",
        "matrix_tsv": "46550cc234efdf7f8509d9c7703dae1e3cf24a997b83b816ade9d51b56292306",
    },
}


class FrozenV16InvariantError(ValueError):
    """Raised when a literal frozen v16 artifact is internally inconsistent."""


class LabInvariantError(ValueError):
    """Raised when isolated hand-off evidence crosses a forbidden boundary."""


class ProductionContractMismatch(RuntimeError):
    """Raised instead of laundering legacy logical coordinates into pixel axes."""


LegacyBox = tuple[int, int, int, int]


@dataclass(frozen=True, order=True)
class LegacyCode:
    row: int
    column: int
    code: int

    def __post_init__(self) -> None:
        if min(self.row, self.column, self.code) < 0:
            raise FrozenV16InvariantError("legacy sparse codes must be non-negative")


@dataclass(frozen=True)
class LegacyMatrixRecord:
    segment_id: str
    leaf_index: int
    anchor_row: int
    anchor_column: int
    codes: tuple[LegacyCode, ...]
    source_bbox: LegacyBox
    content_bbox: LegacyBox
    source_crop_sha256: str

    def __post_init__(self) -> None:
        if self.segment_id != f"leaf-{self.leaf_index:06d}":
            raise FrozenV16InvariantError("legacy leaf identifier is not canonical")
        if min(self.leaf_index, self.anchor_row, self.anchor_column) < 0:
            raise FrozenV16InvariantError("legacy indexes must be non-negative")
        _validate_box(self.source_bbox, "source_bbox")
        _validate_box(self.content_bbox, "content_bbox")
        _validate_sha256(self.source_crop_sha256, "source crop SHA-256")


@dataclass(frozen=True)
class FrozenV16Snapshot:
    source_key: str
    source_path: Path
    source_sha256: str
    page_size: tuple[int, int]
    matrix_shape: tuple[int, int]
    x_tracks: tuple[int, ...]
    records: tuple[LegacyMatrixRecord, ...]
    recursion_sha256: str
    matrix_tsv_sha256: str

    def __post_init__(self) -> None:
        if self.source_key not in SOURCE_KEYS:
            raise FrozenV16InvariantError("unknown frozen source key")
        _validate_sha256(self.source_sha256, "source SHA-256")
        _validate_sha256(self.recursion_sha256, "recursion SHA-256")
        _validate_sha256(self.matrix_tsv_sha256, "matrix TSV SHA-256")
        if len(self.page_size) != 2 or min(self.page_size) < 1:
            raise FrozenV16InvariantError("page_size must be positive")
        if len(self.matrix_shape) != 2 or min(self.matrix_shape) < 1:
            raise FrozenV16InvariantError("matrix_shape must be positive")
        if len(self.x_tracks) != self.matrix_shape[1]:
            raise FrozenV16InvariantError(
                "legacy x-track count disagrees with sparse column count"
            )
        indexes = tuple(item.leaf_index for item in self.records)
        if indexes != tuple(range(len(self.records))):
            raise FrozenV16InvariantError("legacy leaves must be contiguous")
        segment_ids = tuple(item.segment_id for item in self.records)
        if len(segment_ids) != len(set(segment_ids)):
            raise FrozenV16InvariantError("legacy segment IDs must be unique")
        rows, columns = self.matrix_shape
        for record in self.records:
            if record.anchor_row >= rows or record.anchor_column >= columns:
                raise FrozenV16InvariantError("legacy anchor lies outside matrix")
            if any(item.row >= rows or item.column >= columns for item in record.codes):
                raise FrozenV16InvariantError("legacy code lies outside matrix")

    @property
    def segment_ids(self) -> tuple[str, ...]:
        return tuple(item.segment_id for item in self.records)

    @property
    def record_by_id(self) -> dict[str, LegacyMatrixRecord]:
        return {item.segment_id: item for item in self.records}


@dataclass(frozen=True)
class FrozenSerializedGroupMetadata:
    source_key: str
    memberships: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class SerializedMatrixGroup:
    group_id: str
    row_start: int
    row_stop: int
    segment_ids: tuple[str, ...]
    bbox: LegacyBox

    def __post_init__(self) -> None:
        if not self.group_id or self.row_start < 0 or self.row_stop <= self.row_start:
            raise LabInvariantError("invalid serialized matrix group")
        if not self.segment_ids or len(self.segment_ids) != len(set(self.segment_ids)):
            raise LabInvariantError("matrix group must own unique segments")
        _validate_box(self.bbox, "serialized matrix group bbox")


@dataclass(frozen=True)
class MatrixGroupReplay:
    groups: tuple[SerializedMatrixGroup, ...]
    occupied_rows: tuple[int, ...]
    gap_rows: tuple[int, ...]


@dataclass(frozen=True)
class TopologyBlock:
    block_id: str
    scope_id: str
    strategy: str
    segment_ids: tuple[str, ...]
    row_start: int
    row_stop: int
    crop_bbox: LegacyBox
    expected_transforms: tuple[str, ...] = EXPECTED_TRANSFORMS

    def __post_init__(self) -> None:
        if not self.block_id or not self.scope_id or not self.strategy:
            raise LabInvariantError("block identity must not be empty")
        if not self.segment_ids or len(self.segment_ids) != len(set(self.segment_ids)):
            raise LabInvariantError("block membership must be non-empty and unique")
        if self.row_start < 0 or self.row_stop <= self.row_start:
            raise LabInvariantError("block row interval is invalid")
        _validate_box(self.crop_bbox, "block crop bbox")
        if self.expected_transforms != EXPECTED_TRANSFORMS:
            raise LabInvariantError("block transform lattice is not frozen")


@dataclass(frozen=True, order=True)
class WordBox:
    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        if min(self.left, self.top) < 0 or self.right <= self.left or self.bottom <= self.top:
            raise LabInvariantError("word bbox is invalid")

    @property
    def area(self) -> int:
        return (self.right - self.left) * (self.bottom - self.top)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.left + self.right) / 2.0, (self.top + self.bottom) / 2.0)

    def translate(self, x: int, y: int) -> WordBox:
        return WordBox(
            self.left + x,
            self.top + y,
            self.right + x,
            self.bottom + y,
        )


@dataclass(frozen=True)
class ObservedWord:
    observation_id: str
    block_id: str
    transform_id: str
    text: str
    local_bbox: WordBox
    confidence: float

    def __post_init__(self) -> None:
        if not self.observation_id or not self.block_id or not self.text:
            raise LabInvariantError("word observation identity/text must not be empty")
        if self.transform_id not in EXPECTED_TRANSFORMS:
            raise LabInvariantError("word observation transform is unknown")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise LabInvariantError("word confidence must be between zero and one")


@dataclass(frozen=True)
class WordCluster:
    cluster_id: str
    text: str
    page_bbox: WordBox
    observation_ids: tuple[str, ...]
    lattice: tuple[tuple[str, str], ...]
    median_confidence: float


@dataclass(frozen=True)
class WordDecodeResult:
    segment_texts: tuple[tuple[str, str], ...]
    assignments: tuple[tuple[str, str], ...]
    unassigned: tuple[tuple[str, str], ...]
    clusters: tuple[WordCluster, ...]


@dataclass(frozen=True)
class _ReadyTextTrace:
    selected_observation_id: None = None


def _validate_box(value: LegacyBox, name: str) -> None:
    if (
        len(value) != 4
        or min(value[0], value[1]) < 0
        or value[2] <= value[0]
        or value[3] <= value[1]
    ):
        raise FrozenV16InvariantError(f"{name} is invalid")


def _validate_sha256(value: str, name: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FrozenV16InvariantError(f"{name} is invalid")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


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
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: Iterable[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        for value in values:
            stream.write(_canonical_json_bytes(value))


def _union_boxes(values: Sequence[LegacyBox]) -> LegacyBox:
    if not values:
        raise LabInvariantError("cannot union an empty box sequence")
    return (
        min(item[0] for item in values),
        min(item[1] for item in values),
        max(item[2] for item in values),
        max(item[3] for item in values),
    )


def _intersection(first: LegacyBox, second: LegacyBox) -> LegacyBox | None:
    value = (
        max(first[0], second[0]),
        max(first[1], second[1]),
        min(first[2], second[2]),
        min(first[3], second[3]),
    )
    return value if value[2] > value[0] and value[3] > value[1] else None


def _parse_codes(value: object) -> tuple[LegacyCode, ...]:
    if not isinstance(value, list):
        raise FrozenV16InvariantError("legacy codes must be a JSON list")
    result = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 3
            or any(type(part) is not int for part in item)
        ):
            raise FrozenV16InvariantError("legacy code entry is invalid")
        result.append(LegacyCode(*item))
    if tuple(sorted(set(result))) != tuple(result):
        raise FrozenV16InvariantError("legacy codes are not canonical")
    return tuple(result)


def _parse_box(value: object, name: str) -> LegacyBox:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(type(item) is not int for item in value)
    ):
        raise FrozenV16InvariantError(f"{name} is not an integer box")
    result = tuple(value)
    _validate_box(result, name)  # type: ignore[arg-type]
    return result  # type: ignore[return-value]


def _read_matrix_tsv(path: Path) -> tuple[dict[str, object], ...]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if reader.fieldnames != ["leaf", "row", "column", "codes", "bbox"]:
            raise FrozenV16InvariantError("matrix.tsv header is not literal v16")
        values = []
        for row in reader:
            try:
                values.append(
                    {
                        "leaf": int(row["leaf"]),
                        "row": int(row["row"]),
                        "column": int(row["column"]),
                        "codes": json.loads(row["codes"]),
                        "bbox": json.loads(row["bbox"]),
                    }
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise FrozenV16InvariantError("matrix.tsv row is invalid") from exc
    return tuple(values)


def load_frozen_v16(
    input_root: Path,
    source_key: str,
) -> tuple[FrozenV16Snapshot, FrozenSerializedGroupMetadata]:
    """Load literal artifacts; keep serialized group metadata separate."""

    if source_key not in SOURCE_KEYS:
        raise FrozenV16InvariantError(f"unsupported source key {source_key!r}")
    source_root = input_root / source_key
    recursion_path = source_root / "recursion.json"
    matrix_path = source_root / "matrix.tsv"
    try:
        recursion_raw = recursion_path.read_bytes()
        matrix_raw = matrix_path.read_bytes()
        recursion = json.loads(recursion_raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrozenV16InvariantError("frozen v16 input is unreadable") from exc
    if not isinstance(recursion, dict):
        raise FrozenV16InvariantError("recursion.json root must be an object")
    expected_digests = FROZEN_INPUT_DIGESTS[source_key]
    if _sha256_bytes(recursion_raw) != expected_digests["recursion"]:
        raise FrozenV16InvariantError("recursion.json is not the pinned v16 artifact")
    if _sha256_bytes(matrix_raw) != expected_digests["matrix_tsv"]:
        raise FrozenV16InvariantError("matrix.tsv is not the pinned v16 artifact")

    source_name = Path(str(recursion.get("source", ""))).name
    source_path = REPOSITORY_ROOT / "debug" / "fixtures" / source_name
    source_sha256 = recursion.get("source_sha256")
    if not isinstance(source_sha256, str):
        raise FrozenV16InvariantError("source SHA-256 is missing")
    _validate_sha256(source_sha256, "source SHA-256")
    if source_sha256 != expected_digests["source"]:
        raise FrozenV16InvariantError("source digest is not the pinned v16 fixture")
    if not source_path.is_file() or _sha256_path(source_path) != source_sha256:
        raise FrozenV16InvariantError("frozen source image digest mismatch")
    try:
        with Image.open(source_path) as opened:
            page_size = opened.size
            opened.verify()
    except (OSError, SyntaxError, UnidentifiedImageError) as exc:
        raise FrozenV16InvariantError("frozen source image is invalid") from exc

    shape_raw = recursion.get("sparse_shape")
    if (
        not isinstance(shape_raw, list)
        or len(shape_raw) != 2
        or any(type(item) is not int for item in shape_raw)
    ):
        raise FrozenV16InvariantError("legacy sparse shape is invalid")
    matrix_shape = tuple(shape_raw)
    x_tracks_raw = recursion.get("x_tracks")
    if not isinstance(x_tracks_raw, list) or any(
        type(item) is not int for item in x_tracks_raw
    ):
        raise FrozenV16InvariantError("legacy x_tracks are invalid")
    x_tracks = tuple(x_tracks_raw)
    leaves = recursion.get("leaves")
    if not isinstance(leaves, list):
        raise FrozenV16InvariantError("legacy leaves are missing")
    matrix_rows = _read_matrix_tsv(matrix_path)
    if len(matrix_rows) != len(leaves):
        raise FrozenV16InvariantError("matrix.tsv and recursion leaf counts differ")

    records = []
    for expected_index, (leaf, matrix_row) in enumerate(
        zip(leaves, matrix_rows, strict=True)
    ):
        if not isinstance(leaf, dict) or leaf.get("index") != expected_index:
            raise FrozenV16InvariantError("legacy leaf index is not canonical")
        if matrix_row["leaf"] != expected_index:
            raise FrozenV16InvariantError("matrix.tsv leaf index disagrees")
        anchor = leaf.get("anchor")
        if (
            not isinstance(anchor, list)
            or len(anchor) != 2
            or any(type(item) is not int for item in anchor)
        ):
            raise FrozenV16InvariantError("legacy leaf anchor is invalid")
        codes = _parse_codes(leaf.get("codes"))
        matrix_codes = _parse_codes(matrix_row["codes"])
        source_bbox = _parse_box(leaf.get("source_bbox"), "source_bbox")
        matrix_bbox = _parse_box(matrix_row["bbox"], "matrix bbox")
        if (
            tuple(anchor) != (matrix_row["row"], matrix_row["column"])
            or codes != matrix_codes
            or source_bbox != matrix_bbox
        ):
            raise FrozenV16InvariantError(
                "literal matrix.tsv row disagrees with recursion.json"
            )
        records.append(
            LegacyMatrixRecord(
                segment_id=f"leaf-{expected_index:06d}",
                leaf_index=expected_index,
                anchor_row=anchor[0],
                anchor_column=anchor[1],
                codes=codes,
                source_bbox=source_bbox,
                content_bbox=_parse_box(leaf.get("content_bbox"), "content_bbox"),
                source_crop_sha256=str(leaf.get("source_crop_sha256", "")),
            )
        )

    declared_anchors = recursion.get("anchors")
    declared_codes = recursion.get("codes")
    actual_anchors = sorted(
        [record.anchor_row, record.anchor_column] for record in records
    )
    actual_codes = sorted(
        [code.row, code.column, code.code]
        for record in records
        for code in record.codes
    )
    if declared_anchors != actual_anchors or declared_codes != actual_codes:
        raise FrozenV16InvariantError(
            "recursion summary anchors/codes disagree with leaf records"
        )

    snapshot = FrozenV16Snapshot(
        source_key=source_key,
        source_path=source_path,
        source_sha256=source_sha256,
        page_size=page_size,
        matrix_shape=matrix_shape,  # type: ignore[arg-type]
        x_tracks=x_tracks,
        records=tuple(records),
        recursion_sha256=_sha256_bytes(recursion_raw),
        matrix_tsv_sha256=_sha256_bytes(matrix_raw),
    )

    # Metadata extraction happens after the snapshot has been constructed.  The
    # replay below accepts only the snapshot and cannot see this value.  This is
    # an API-isolation property, not evidence of historical independence: v16
    # generated the gap rows from these same recursive groups.
    groups = recursion.get("groups")
    if not isinstance(groups, list):
        raise FrozenV16InvariantError("frozen serialized group metadata is missing")
    metadata_values = []
    for expected_index, group in enumerate(groups):
        if not isinstance(group, dict) or group.get("index") != expected_index:
            raise FrozenV16InvariantError("frozen group metadata is invalid")
        indexes = group.get("leaf_indexes")
        if not isinstance(indexes, list) or any(type(item) is not int for item in indexes):
            raise FrozenV16InvariantError("frozen group membership is invalid")
        metadata_values.append(tuple(f"leaf-{item:06d}" for item in indexes))
    metadata = FrozenSerializedGroupMetadata(source_key, tuple(metadata_values))
    return snapshot, metadata


def _serialized_membership_runs(
    snapshot: FrozenV16Snapshot,
) -> tuple[tuple[int, int, tuple[str, ...]], ...]:
    """Replay serialized groups from occupied rows, without metadata input."""

    matrix_rows = snapshot.matrix_shape[0]
    occupied = {
        row
        for record in snapshot.records
        for row in (record.anchor_row, *(code.row for code in record.codes))
    }
    if any(row < 0 or row >= matrix_rows for row in occupied):
        raise LabInvariantError("occupied row lies outside frozen matrix")
    ordered_rows = tuple(sorted(occupied))
    runs: list[tuple[int, int]] = []
    for row in ordered_rows:
        if runs and row == runs[-1][1]:
            runs[-1] = (runs[-1][0], row + 1)
        else:
            runs.append((row, row + 1))
    result = []
    owned: set[str] = set()
    for row_start, row_stop in runs:
        members = tuple(
            record.segment_id
            for record in snapshot.records
            if row_start <= record.anchor_row < row_stop
        )
        if not members:
            raise LabInvariantError("occupied row run has no payload anchor")
        if owned.intersection(members):
            raise LabInvariantError("serialized matrix groups overlap")
        owned.update(members)
        result.append((row_start, row_stop, members))
    if owned != set(snapshot.segment_ids):
        raise LabInvariantError("serialized matrix groups do not partition segments")
    return tuple(result)


def replay_serialized_matrix_groups(
    snapshot: FrozenV16Snapshot,
) -> MatrixGroupReplay:
    runs = _serialized_membership_runs(snapshot)
    record_by_id = snapshot.record_by_id
    groups = tuple(
        SerializedMatrixGroup(
            group_id=f"matrix-group-{index:06d}",
            row_start=row_start,
            row_stop=row_stop,
            segment_ids=segment_ids,
            bbox=_union_boxes(
                tuple(record_by_id[item].source_bbox for item in segment_ids)
            ),
        )
        for index, (row_start, row_stop, segment_ids) in enumerate(runs)
    )
    occupied = tuple(
        sorted(
            {
                row
                for record in snapshot.records
                for row in (
                    record.anchor_row,
                    *(code.row for code in record.codes),
                )
            }
        )
    )
    gaps = tuple(row for row in range(snapshot.matrix_shape[0]) if row not in set(occupied))
    return MatrixGroupReplay(groups=groups, occupied_rows=occupied, gap_rows=gaps)


def compare_with_serialized_group_metadata(
    replay: MatrixGroupReplay,
    metadata: FrozenSerializedGroupMetadata,
) -> dict[str, object]:
    detected = tuple(item.segment_ids for item in replay.groups)
    expected = metadata.memberships
    return {
        "status": (
            "serialized-group-boundary-replay-exact"
            if detected == expected
            else "serialized-group-boundary-replay-different"
        ),
        "independent_object_detection": False,
        "object_extraction_status": "not-tested",
        "historical_circularity": (
            "v16 project_sparse_shadow called group_recursive_leaves first, "
            "then inserted one blank row between those same groups"
        ),
        "v16_source_commit": "4e86ef051c99960eb93a85501c7d66f7cef8be8b",
        "v16_source_lines": "ocr/app/layout/recursive_grid.py:280,302-305",
        "replayed_count": len(detected),
        "metadata_count": len(expected),
        "membership_exact": detected == expected,
        "replayed": [list(item) for item in detected],
        "serialized_metadata": [list(item) for item in expected],
    }


def current_stage5_contract_mismatch(
    snapshot: FrozenV16Snapshot,
) -> dict[str, object]:
    """Describe why no honest current SparseSegmentMatrix adapter exists."""

    rows, columns = snapshot.matrix_shape
    nonrectangular = []
    for record in snapshot.records:
        coordinates = {
            (record.anchor_row, record.anchor_column),
            *((item.row, item.column) for item in record.codes),
        }
        row_start = min(item[0] for item in coordinates)
        row_stop = max(item[0] for item in coordinates) + 1
        column_start = min(item[1] for item in coordinates)
        column_stop = max(item[1] for item in coordinates) + 1
        span_area = (row_stop - row_start) * (column_stop - column_start)
        if len(coordinates) != span_area:
            nonrectangular.append(
                {
                    "segment_id": record.segment_id,
                    "legacy_coordinates": len(coordinates),
                    "rectangular_span_area": span_area,
                    "missing_coordinates": span_area - len(coordinates),
                }
            )
    diagnostic_adapter = {
        "000041": {
            "status": "rejected",
            "error": (
                "spatial membership closure left a visible non-member segment"
            ),
            "physical_overlap": {
                "first": "leaf-000011",
                "second": "leaf-000012",
                "intersection": [0, 2768, 2573, 2784],
            },
        },
        "09": {
            "status": "unresolved",
            "blocks": 102,
            "membership_units": 109,
            "singleton_units": 106,
            "unresolved_groups": [
                ["leaf-000019", "leaf-000020"],
                ["leaf-000048", "leaf-000049"],
                ["leaf-000083", "leaf-000084", "leaf-000085"],
            ],
            "column_probes": 0,
        },
    }[snapshot.source_key]
    diagnostic_adapter = {
        "stats_provenance": "frozen-precomputed-rejected-adapter-diagnostic",
        "computed_by_this_lab": False,
        **diagnostic_adapter,
    }
    return {
        "status": "incompatible",
        "exception": "ProductionContractMismatch",
        "legacy_shape": [rows, columns],
        "legacy_segments": len(snapshot.records),
        "nonrectangular_legacy_payloads": nonrectangular,
        "diagnostic_current_adapter": diagnostic_adapter,
        "issues": [
            {
                "current_requirement": (
                    "SparseSegmentMatrix.rows are contiguous half-open pixel "
                    "AxisIntervals covering the aligned canvas"
                ),
                "legacy_evidence": (
                    "v16 stores logical row indexes/count only; it has no y pixel "
                    "boundaries for those rows"
                ),
                "forbidden_conversion": (
                    "inventing y intervals from leaf bboxes or uniform row heights"
                ),
            },
            {
                "current_requirement": (
                    "SparseSegmentMatrix.columns are half-open pixel intervals"
                ),
                "legacy_evidence": (
                    "v16 x_tracks are anchor/merge track points, not an axis "
                    "partition covering [0,page_width)"
                ),
                "forbidden_conversion": "turning track points into synthetic cells",
            },
            {
                "current_requirement": (
                    "every SparseCell carries a payload segment_id and every "
                    "segment has one rectangular SegmentSpan"
                ),
                "legacy_evidence": (
                    "v16 numeric codes describe merge-up/merge-left/empty virtual "
                    "coordinates; only one leaf anchor has payload"
                ),
                "forbidden_conversion": (
                    "assigning a leaf ID to virtual cells or manufacturing IDs"
                ),
            },
            {
                "current_requirement": (
                    "current Segment is also an exact foreground owner with ink, "
                    "component and recursion provenance"
                ),
                "legacy_evidence": (
                    "v16 leaves are overlapping OCR context crops and omit some "
                    "source foreground"
                ),
                "forbidden_conversion": "presenting legacy leaves as lossless owners",
            },
            {
                "current_requirement": (
                    "Stage7 table drafts require rectangular per-segment sparse "
                    "occupancy"
                ),
                "legacy_evidence": (
                    f"{len(nonrectangular)} v16 payload coordinate sets are "
                    "non-rectangular under the current cell contract"
                ),
                "forbidden_conversion": (
                    "filling legacy holes or discarding merge/empty coordinates"
                ),
            },
        ],
        "called": [],
        "skipped": [
            "Stage1.GeometryAnalyzer",
            "Stage5.OverlappingBlockPlanner.plan",
            "Stage5.BlockCropper",
        ],
    }


def require_current_stage5_compatible(snapshot: FrozenV16Snapshot) -> None:
    mismatch = current_stage5_contract_mismatch(snapshot)
    raise ProductionContractMismatch(
        "literal v16 matrix cannot enter current Stage5 without pixel-axis "
        "laundering: "
        + "; ".join(
            str(item["legacy_evidence"]) for item in mismatch["issues"]
        )
    )


def plan_sliding_blocks(
    snapshot: FrozenV16Snapshot,
    serialized_group: SerializedMatrixGroup,
    *,
    window_size: int,
    strategy: str,
) -> tuple[TopologyBlock, ...]:
    """Build a debug-only linear membership topology inside one object.

    This intentionally does not claim to be the current 2-D Stage5 planner:
    literal v16 rows/codes cannot be converted to its pixel-axis contract
    without the laundering rejected above.  Bboxes are used only for PNG crops.
    """

    if window_size < 1:
        raise ValueError("window_size must be positive")
    record_by_id = snapshot.record_by_id
    members = serialized_group.segment_ids
    width = min(window_size, len(members))
    starts = range(0, len(members) - width + 1)
    blocks = []
    for block_index, start in enumerate(starts):
        selected = members[start : start + width]
        selected_records = tuple(record_by_id[item] for item in selected)
        blocks.append(
            TopologyBlock(
                block_id=f"{serialized_group.group_id}-{strategy}-{block_index:06d}",
                scope_id=serialized_group.group_id,
                strategy=strategy,
                segment_ids=selected,
                row_start=min(item.anchor_row for item in selected_records),
                row_stop=max(item.anchor_row for item in selected_records) + 1,
                crop_bbox=_union_boxes(
                    tuple(item.source_bbox for item in selected_records)
                ),
            )
        )
    if not blocks:
        raise LabInvariantError("non-empty object produced no topology block")
    if any(
        not set(item.segment_ids).issubset(serialized_group.segment_ids)
        or item.scope_id != serialized_group.group_id
        for item in blocks
    ):
        raise LabInvariantError("a topology block crosses its serialized group scope")
    return tuple(blocks)


def topology_signatures(
    segment_ids: tuple[str, ...],
    blocks: tuple[TopologyBlock, ...],
) -> dict[str, object]:
    values = {
        segment_id: tuple(
            block.block_id
            for block in blocks
            if segment_id in block.segment_ids
        )
        for segment_id in segment_ids
    }
    grouped: dict[tuple[str, ...], list[str]] = {}
    for segment_id, signature in values.items():
        grouped.setdefault(signature, []).append(segment_id)
    unresolved = tuple(
        tuple(members)
        for signature, members in grouped.items()
        if not signature or len(members) > 1
    )
    return {
        "status": "unique" if not unresolved else "unresolved",
        "signatures": {
            segment_id: list(signature) for segment_id, signature in values.items()
        },
        "unresolved_groups": [list(item) for item in unresolved],
        "covered_segments": sum(bool(item) for item in values.values()),
        "total_segments": len(segment_ids),
    }


def foreign_bbox_exposure(
    snapshot: FrozenV16Snapshot,
    serialized_group: SerializedMatrixGroup,
    blocks: tuple[TopologyBlock, ...],
) -> tuple[dict[str, object], ...]:
    own = set(serialized_group.segment_ids)
    values = []
    for block in blocks:
        foreign = []
        for record in snapshot.records:
            if record.segment_id in block.segment_ids:
                continue
            overlap = _intersection(block.crop_bbox, record.source_bbox)
            if overlap is not None:
                foreign.append(
                    {
                        "segment_id": record.segment_id,
                        "scope_relation": (
                            "same-serialized-group"
                            if record.segment_id in own
                            else "cross-serialized-group"
                        ),
                        "intersection": list(overlap),
                        "intersection_area": (
                            (overlap[2] - overlap[0])
                            * (overlap[3] - overlap[1])
                        ),
                    }
                )
        if foreign:
            values.append(
                {
                    "block_id": block.block_id,
                    "scope_id": block.scope_id,
                    "foreign": foreign,
                }
            )
    return tuple(values)


def _word_iou(first: WordBox, second: WordBox) -> float:
    left = max(first.left, second.left)
    top = max(first.top, second.top)
    right = min(first.right, second.right)
    bottom = min(first.bottom, second.bottom)
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    return intersection / (first.area + second.area - intersection)


def _small_box_overlap_ratio(first: WordBox, second: WordBox) -> float:
    left = max(first.left, second.left)
    top = max(first.top, second.top)
    right = min(first.right, second.right)
    bottom = min(first.bottom, second.bottom)
    if right <= left or bottom <= top:
        return 0.0
    return ((right - left) * (bottom - top)) / min(first.area, second.area)


def _normalize_word(value: str) -> str:
    return " ".join(value.casefold().split())


def _cluster_observed_words(
    blocks: tuple[TopologyBlock, ...],
    observations: tuple[ObservedWord, ...],
) -> tuple[WordCluster, ...]:
    block_by_id = {item.block_id: item for item in blocks}
    if len(block_by_id) != len(blocks):
        raise LabInvariantError("synthetic block IDs must be unique")
    page_values = []
    seen_observation_ids: set[str] = set()
    for item in observations:
        block = block_by_id.get(item.block_id)
        if block is None:
            raise LabInvariantError("word observation references unknown block")
        if item.observation_id in seen_observation_ids:
            raise LabInvariantError(
                "word observation identity must be globally unique"
            )
        seen_observation_ids.add(item.observation_id)
        block_width = block.crop_bbox[2] - block.crop_bbox[0]
        block_height = block.crop_bbox[3] - block.crop_bbox[1]
        if (
            item.local_bbox.right > block_width
            or item.local_bbox.bottom > block_height
        ):
            raise LabInvariantError(
                "word observation local bbox lies outside its bound block crop"
            )
        page_values.append(
            (
                item,
                item.local_bbox.translate(
                    block.crop_bbox[0],
                    block.crop_bbox[1],
                ),
            )
        )

    parent = list(range(len(page_values)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    for first_index, (first, first_bbox) in enumerate(page_values):
        for second_index in range(first_index + 1, len(page_values)):
            second, second_bbox = page_values[second_index]
            if (
                _normalize_word(first.text) == _normalize_word(second.text)
                and _word_iou(first_bbox, second_bbox) >= 0.60
            ):
                union(first_index, second_index)

    grouped: dict[int, list[tuple[ObservedWord, WordBox]]] = {}
    for index, value in enumerate(page_values):
        grouped.setdefault(find(index), []).append(value)
    clusters = []
    for cluster_index, values in enumerate(
        sorted(
            grouped.values(),
            key=lambda group: (
                min(item[1].top for item in group),
                min(item[1].left for item in group),
                _normalize_word(group[0][0].text),
            ),
        )
    ):
        texts = {_normalize_word(item[0].text) for item in values}
        if len(texts) != 1:
            raise LabInvariantError("word cluster contains conflicting text")
        boxes = tuple(item[1] for item in values)
        clusters.append(
            WordCluster(
                cluster_id=f"word-cluster-{cluster_index:06d}",
                text=values[0][0].text,
                page_bbox=WordBox(
                    round(median(item.left for item in boxes)),
                    round(median(item.top for item in boxes)),
                    round(median(item.right for item in boxes)),
                    round(median(item.bottom for item in boxes)),
                ),
                observation_ids=tuple(
                    sorted(item[0].observation_id for item in values)
                ),
                lattice=tuple(
                    sorted(
                        {
                            (item[0].block_id, item[0].transform_id)
                            for item in values
                        }
                    )
                ),
                median_confidence=float(
                    median(item[0].confidence for item in values)
                ),
            )
        )
    return tuple(clusters)


def decode_observed_word_lattice(
    *,
    blocks: tuple[TopologyBlock, ...],
    source_segment_ids: tuple[str, ...],
    observations: tuple[ObservedWord, ...],
) -> WordDecodeResult:
    """Decode words from block/transform incidence; no segment bbox is accepted."""

    if len(source_segment_ids) != len(set(source_segment_ids)):
        raise LabInvariantError("source segment IDs must be unique")
    source_set = set(source_segment_ids)
    if any(not set(item.segment_ids).issubset(source_set) for item in blocks):
        raise LabInvariantError("block membership references an unknown segment")
    expected_lattice = {
        segment_id: tuple(
            sorted(
                (block.block_id, transform)
                for block in blocks
                if segment_id in block.segment_ids
                for transform in block.expected_transforms
            )
        )
        for segment_id in source_segment_ids
    }
    reverse: dict[tuple[tuple[str, str], ...], list[str]] = {}
    for segment_id, lattice in expected_lattice.items():
        reverse.setdefault(lattice, []).append(segment_id)
    clusters = _cluster_observed_words(blocks, observations)
    provisional: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for cluster in clusters:
        matches = reverse.get(cluster.lattice, [])
        if len(matches) == 1:
            provisional[cluster.cluster_id] = matches[0]
        elif not matches:
            reasons[cluster.cluster_id] = "incomplete-or-unknown-observation-lattice"
        else:
            reasons[cluster.cluster_id] = "ambiguous-segment-signature"

    # Resolve word-geometry collisions without knowing segment boxes or token
    # semantics.  A low-confidence cluster whose observed page box is inside a
    # stronger cluster assigned to another lattice is rejected.  A standalone
    # token with a complete lattice has no collision and is retained.
    collisions: dict[str, set[str]] = {}
    cluster_by_id = {item.cluster_id: item for item in clusters}
    provisional_ids = tuple(provisional)
    for first_index, first_id in enumerate(provisional_ids):
        for second_id in provisional_ids[first_index + 1 :]:
            if provisional[first_id] == provisional[second_id]:
                continue
            first = cluster_by_id[first_id]
            second = cluster_by_id[second_id]
            if _small_box_overlap_ratio(first.page_bbox, second.page_bbox) >= 0.80:
                collisions.setdefault(first_id, set()).add(second_id)
                collisions.setdefault(second_id, set()).add(first_id)
    visited: set[str] = set()
    for start in tuple(collisions):
        if start in visited:
            continue
        component = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(collisions.get(current, ()))
        visited.update(component)
        ranked = sorted(
            component,
            key=lambda item: (
                cluster_by_id[item].median_confidence,
                len(cluster_by_id[item].lattice),
                cluster_by_id[item].page_bbox.area,
            ),
            reverse=True,
        )
        winner = ranked[0]
        runner_up = ranked[1] if len(ranked) > 1 else None
        decisive = (
            runner_up is None
            or cluster_by_id[winner].median_confidence
            >= cluster_by_id[runner_up].median_confidence + 0.10
        )
        rejected = ranked[1:] if decisive else ranked
        for cluster_id in rejected:
            provisional.pop(cluster_id, None)
            reasons[cluster_id] = "observed-word-geometry-collision"

    by_segment: dict[str, list[WordCluster]] = {
        segment_id: [] for segment_id in source_segment_ids
    }
    for cluster_id, segment_id in provisional.items():
        by_segment[segment_id].append(cluster_by_id[cluster_id])
    texts = []
    assignments = []
    for segment_id in source_segment_ids:
        ordered = sorted(
            by_segment[segment_id],
            key=lambda item: (
                item.page_bbox.top,
                item.page_bbox.left,
                item.cluster_id,
            ),
        )
        texts.append((segment_id, " ".join(item.text for item in ordered)))
        assignments.extend((item.cluster_id, segment_id) for item in ordered)
    unassigned = tuple(
        (cluster.cluster_id, reasons[cluster.cluster_id])
        for cluster in clusters
        if cluster.cluster_id not in provisional
    )
    return WordDecodeResult(
        segment_texts=tuple(texts),
        assignments=tuple(assignments),
        unassigned=unassigned,
        clusters=clusters,
    )


def synthetic_observed_word_case() -> tuple[
    Image.Image,
    tuple[TopologyBlock, ...],
    tuple[str, ...],
    tuple[ObservedWord, ...],
    tuple[tuple[str, str], ...],
]:
    segment_ids = tuple(f"synthetic-segment-{index}" for index in range(6))
    blocks = (
        TopologyBlock(
            "synthetic-block-a",
            "synthetic-object",
            "orthogonal-abc",
            segment_ids[0:4],
            0,
            2,
            (0, 0, 200, 200),
        ),
        TopologyBlock(
            "synthetic-block-b",
            "synthetic-object",
            "orthogonal-abc",
            segment_ids[2:6],
            1,
            3,
            (0, 100, 200, 300),
        ),
        TopologyBlock(
            "synthetic-block-c",
            "synthetic-object",
            "orthogonal-abc",
            (segment_ids[0], segment_ids[2], segment_ids[4]),
            0,
            3,
            (0, 0, 100, 300),
        ),
    )
    words = {
        segment_ids[0]: ("item", WordBox(20, 20, 55, 42)),
        segment_ids[1]: ("beta", WordBox(120, 20, 160, 42)),
        segment_ids[2]: ("gamma", WordBox(20, 120, 70, 144)),
        # Genuine standalone separator.  It is retained because its complete
        # A/B x raw/gamma lattice uniquely identifies segment 3.
        segment_ids[3]: ("|", WordBox(150, 120, 156, 146)),
        segment_ids[4]: ("item", WordBox(20, 220, 55, 242)),
        segment_ids[5]: ("zeta", WordBox(120, 220, 160, 242)),
    }
    page = Image.new("RGB", (200, 300), "white")
    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default()
    for text, bbox in words.values():
        draw.text((bbox.left, bbox.top), text, fill="black", font=font)

    observations = []
    observation_index = 0
    for block in blocks:
        for segment_id in block.segment_ids:
            text, page_bbox = words[segment_id]
            local = page_bbox.translate(-block.crop_bbox[0], -block.crop_bbox[1])
            for transform in EXPECTED_TRANSFORMS:
                observations.append(
                    ObservedWord(
                        observation_id=f"observed-{observation_index:06d}",
                        block_id=block.block_id,
                        transform_id=transform,
                        text=text,
                        local_bbox=local,
                        confidence=0.98,
                    )
                )
                observation_index += 1

    def append_extra(
        *,
        name: str,
        block_ids: tuple[str, ...],
        transforms: tuple[str, ...],
        page_bbox: WordBox,
        confidence: float,
    ) -> None:
        nonlocal observation_index
        block_by_id = {item.block_id: item for item in blocks}
        for block_id in block_ids:
            block = block_by_id[block_id]
            local = page_bbox.translate(-block.crop_bbox[0], -block.crop_bbox[1])
            for transform in transforms:
                observations.append(
                    ObservedWord(
                        observation_id=f"{name}-{observation_index:06d}",
                        block_id=block_id,
                        transform_id=transform,
                        text="|",
                        local_bbox=local,
                        confidence=confidence,
                    )
                )
                observation_index += 1

    # No Stage 1 segment has the C-only signature.
    append_extra(
        name="garbage-unmatched",
        block_ids=("synthetic-block-c",),
        transforms=EXPECTED_TRANSFORMS,
        page_bbox=WordBox(82, 250, 88, 276),
        confidence=0.50,
    )
    # Missing gamma transform: coverage is incomplete even for the A signature.
    append_extra(
        name="garbage-incomplete",
        block_ids=("synthetic-block-a",),
        transforms=("raw",),
        page_bbox=WordBox(82, 50, 88, 76),
        confidence=0.50,
    )
    # A/C happens to be segment 0's lattice, but the observed box collides
    # with high-confidence segment 2 word geometry.  The generic collision
    # guard rejects it without inspecting the token or any segment bbox.
    append_extra(
        name="garbage-wrong-collision",
        block_ids=("synthetic-block-a", "synthetic-block-c"),
        transforms=EXPECTED_TRANSFORMS,
        page_bbox=WordBox(28, 122, 34, 142),
        confidence=0.40,
    )
    expected = tuple((segment_id, words[segment_id][0]) for segment_id in segment_ids)
    return page, blocks, segment_ids, tuple(observations), expected


def _ready_segment_assembly(
    segment_id: str,
    object_id: str,
    text: str,
) -> SegmentTextAssembly:
    return SegmentTextAssembly(
        segment_id=segment_id,
        object_id=object_id,
        candidate_text=text,
        text=None,
        attribution_level=AttributionLevel.SEGMENT,
        selected_observation_id=None,
        evidence_slice_ids=(),
        status=AssemblyStatus.UNRESOLVED,
        reasons=("candidate-only-no-ocr-provenance",),
    )


def run_ready_text_stage7(
    segment_texts: tuple[tuple[str, str], ...],
    output_root: Path,
) -> dict[str, object]:
    """Call only the production Stage 7 ready-text rendering core."""

    output_root.mkdir(parents=True, exist_ok=False)
    segment_ids = tuple(item[0] for item in segment_texts)
    if segment_ids != tuple(f"synthetic-segment-{index}" for index in range(6)):
        raise LabInvariantError("ready-text Stage7 needs the frozen six IDs")
    rows = tuple(
        AxisInterval(index, index * 100, (index + 1) * 100)
        for index in range(3)
    )
    columns = tuple(
        AxisInterval(index, index * 100, (index + 1) * 100)
        for index in range(2)
    )
    cells = tuple(
        SparseCell(row=index // 2, column=index % 2, segment_id=segment_id)
        for index, segment_id in enumerate(segment_ids)
    )
    spans = tuple(
        SegmentSpan(
            segment_id=segment_id,
            row_start=index // 2,
            row_stop=index // 2 + 1,
            column_start=index % 2,
            column_stop=index % 2 + 1,
        )
        for index, segment_id in enumerate(segment_ids)
    )
    matrix = SparseSegmentMatrix(
        rows=rows,
        columns=columns,
        cells=cells,
        spans=spans,
    )
    document_object = DocumentObject(
        object_id="synthetic-object",
        kind=ObjectKind.TABLE,
        segment_ids=segment_ids,
        bbox=Box(0, 0, 200, 300),
        reading_index=0,
        row_start=0,
        row_stop=3,
        column_start=0,
        column_stop=2,
        confidence=1.0,
        evidence=("synthetic-ready-text-only",),
    )
    ready = {
        segment_id: _ready_segment_assembly(
            segment_id,
            document_object.object_id,
            text,
        )
        for segment_id, text in segment_texts
    }
    traces = {segment_id: _ReadyTextTrace() for segment_id in segment_ids}
    span_by_id = {item.segment_id: item for item in matrix.spans}
    coordinates = {
        segment_id: tuple(
            (item.row, item.column)
            for item in matrix.cells
            if item.segment_id == segment_id
        )
        for segment_id in segment_ids
    }
    assembler = DocumentAssembler()
    text, placements = assembler._render_segment_values(
        document_object=document_object,
        segment_values=tuple(ready[item] for item in segment_ids),
        span_by_id=span_by_id,
        fused_by_id=traces,  # type: ignore[arg-type]
        observation_by_id={},
        job_by_id={},
    )
    if placements:
        raise LabInvariantError("ready-text Stage7 manufactured OCR evidence")
    drafts = assembler._structural_drafts(
        document_object=document_object,
        span_by_id=span_by_id,
        matrix=matrix,
        coordinates_by_segment=coordinates,
    )
    units = []
    for index, draft in enumerate(drafts):
        unit_text, unit_placements = assembler._render_segment_values(
            document_object=document_object,
            segment_values=tuple(ready[item] for item in draft.segment_ids),
            span_by_id=span_by_id,
            fused_by_id=traces,  # type: ignore[arg-type]
            observation_by_id={},
            job_by_id={},
        )
        if unit_placements:
            raise LabInvariantError("ready structural unit manufactured evidence")
        units.append(
            StructuralUnit(
                unit_id=f"synthetic-unit-{index:06d}",
                object_id=document_object.object_id,
                kind=document_object.kind,
                unit_kind=draft.unit_kind,
                segment_ids=draft.segment_ids,
                row_start=draft.row_start,
                row_stop=draft.row_stop,
                column_start=draft.column_start,
                column_stop=draft.column_stop,
                candidate_text=unit_text,
                text=None,
                evidence_slice_ids=(),
                status=AssemblyStatus.UNRESOLVED,
                reasons=("candidate-only-no-ocr-provenance",),
            )
        )
    table_rows, table_columns = assembler._table_axes(
        document_object=document_object,
        matrix=matrix,
    )
    markdown = assembler._object_markdown(
        ObjectKind.TABLE,
        text,
        tuple(units),
        table_row_indices=table_rows,
        table_column_indices=table_columns,
    )
    document_text, offsets = assembler._join_with_offsets((text,), separator="\n\n")
    document_markdown, markdown_offsets = assembler._join_with_offsets(
        (markdown,), separator="\n\n"
    )
    text_exact = document_text == EXPECTED_READY_TEXT
    markdown_exact = document_markdown == EXPECTED_READY_MARKDOWN
    if not text_exact or not markdown_exact:
        raise LabInvariantError(
            "ready-text Stage7 candidate differs from the explicit TXT/MD oracle"
        )
    (output_root / "candidate-document.txt").write_text(
        document_text, encoding="utf-8"
    )
    (output_root / "candidate-document.md").write_text(
        document_markdown, encoding="utf-8"
    )
    _write_jsonl(
        output_root / "ready-segment-texts.jsonl",
        (
            {"segment_id": segment_id, "text": value}
            for segment_id, value in segment_texts
        ),
    )
    summary = {
        "status": "candidate-exact-production-unresolved",
        "candidate_status": "exact",
        "candidate_text_exact": text_exact,
        "candidate_markdown_exact": markdown_exact,
        "production_certification": "unresolved-no-ocr-provenance",
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
            "Stage4 enhancement",
            "Stage2 OCR/fusion",
            "Stage7.DocumentAssembler.assemble provenance replay",
        ],
        "source_segment_ids": list(segment_ids),
        "text_sha256": _sha256_bytes(document_text.encode("utf-8")),
        "markdown_sha256": _sha256_bytes(document_markdown.encode("utf-8")),
        "expected_text_sha256": _sha256_bytes(EXPECTED_READY_TEXT.encode("utf-8")),
        "expected_markdown_sha256": _sha256_bytes(
            EXPECTED_READY_MARKDOWN.encode("utf-8")
        ),
        "object_offsets": [list(item) for item in offsets],
        "markdown_offsets": [list(item) for item in markdown_offsets],
        "standalone_separator_retained": dict(segment_texts)[segment_ids[3]] == "|",
        "segment_assembly_statuses": sorted(
            {item.status.value for item in ready.values()}
        ),
        "structural_unit_statuses": sorted({item.status.value for item in units}),
    }
    _write_json(output_root / "summary.json", summary)
    return summary


def _save_crop(page: Image.Image, bbox: LegacyBox, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    crop = page.crop(bbox)
    try:
        # The frozen source-crop digest was produced with Pillow's default PNG
        # encoder.  Keep that exact byte contract; a different compression
        # level changes the file SHA despite identical decoded pixels.
        crop.save(path, format="PNG")
    finally:
        crop.close()


def _write_contact_sheet_page(
    entries: Sequence[tuple[str, Path]],
    path: Path,
    *,
    columns: int = 2,
    cell_width: int = 620,
    cell_height: int = 250,
) -> None:
    rows = max(1, math.ceil(len(entries) / columns))
    sheet = Image.new(
        "RGB",
        (columns * cell_width, rows * cell_height),
        "white",
    )
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, (label, source) in enumerate(entries):
        x = (index % columns) * cell_width
        y = (index // columns) * cell_height
        with Image.open(source) as opened:
            image = opened.convert("RGB")
            image.thumbnail((cell_width - 20, cell_height - 42))
            sheet.paste(image, (x + 10, y + 28))
            image.close()
        draw.text((x + 10, y + 8), label, fill="black", font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, format="PNG", compress_level=9, optimize=False)
    sheet.close()


def _write_contact_sheets(
    entries: Sequence[tuple[str, Path]],
    root: Path,
    *,
    prefix: str,
    per_page: int = 24,
) -> tuple[str, ...]:
    values = []
    for page_index in range(0, len(entries), per_page):
        name = f"{prefix}-{page_index // per_page:03d}.png"
        _write_contact_sheet_page(
            entries[page_index : page_index + per_page],
            root / name,
        )
        values.append(name)
    return tuple(values)


def _record_payload(record: LegacyMatrixRecord) -> dict[str, object]:
    return {
        "segment_id": record.segment_id,
        "leaf_index": record.leaf_index,
        "anchor": [record.anchor_row, record.anchor_column],
        "codes": [
            [item.row, item.column, item.code] for item in record.codes
        ],
        "source_bbox": list(record.source_bbox),
        "content_bbox": list(record.content_bbox),
        "source_crop_sha256": record.source_crop_sha256,
    }


def _block_payload(block: TopologyBlock) -> dict[str, object]:
    return {
        "block_id": block.block_id,
        "scope_id": block.scope_id,
        "strategy": block.strategy,
        "segment_ids": list(block.segment_ids),
        "row_start": block.row_start,
        "row_stop": block.row_stop,
        "crop_bbox": list(block.crop_bbox),
        "expected_transforms": list(block.expected_transforms),
    }


def _write_source_lab(
    snapshot: FrozenV16Snapshot,
    metadata: FrozenSerializedGroupMetadata,
    root: Path,
) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=False)
    replay = replay_serialized_matrix_groups(snapshot)
    metadata_comparison = compare_with_serialized_group_metadata(replay, metadata)
    mismatch = current_stage5_contract_mismatch(snapshot)
    with Image.open(snapshot.source_path) as opened:
        page = opened.convert("RGB")

    stage_a = root / "stage-a-serialized-group-integrity"
    segment_dir = stage_a / "actual-segments"
    group_dir = stage_a / "actual-serialized-group-contexts"
    segment_entries = []
    for record in snapshot.records:
        path = segment_dir / f"{record.segment_id}.png"
        _save_crop(page, record.source_bbox, path)
        if _sha256_path(path) != record.source_crop_sha256:
            raise FrozenV16InvariantError(
                f"literal crop digest mismatch for {record.segment_id}"
            )
        segment_entries.append((record.segment_id, path))
    group_entries = []
    for serialized_group in replay.groups:
        path = group_dir / f"{serialized_group.group_id}.png"
        _save_crop(page, serialized_group.bbox, path)
        group_entries.append((serialized_group.group_id, path))
    segment_sheets = _write_contact_sheets(
        segment_entries,
        stage_a / "segment-contact-sheets",
        prefix="segments",
    )
    group_sheets = _write_contact_sheets(
        group_entries,
        stage_a / "serialized-group-contact-sheets",
        prefix="serialized-groups",
    )
    _write_jsonl(stage_a / "matrix-records.jsonl", map(_record_payload, snapshot.records))
    _write_jsonl(
        stage_a / "replayed-serialized-groups.jsonl",
        (
            {
                "group_id": item.group_id,
                "row_start": item.row_start,
                "row_stop": item.row_stop,
                "segment_ids": list(item.segment_ids),
                "bbox": list(item.bbox),
            }
            for item in replay.groups
        ),
    )
    _write_json(stage_a / "serialized-metadata-comparison.json", metadata_comparison)
    (stage_a / "gap-rows.txt").write_text(
        "\n".join(map(str, replay.gap_rows)) + "\n",
        encoding="utf-8",
    )
    _write_json(stage_a / "contact-sheets.json", {
        "individual_actual_pngs": (
            "literal source-coordinate crops at native crop resolution"
        ),
        "contact_sheet_rendering": "resized and labeled previews",
        "segments": list(segment_sheets),
        "serialized_groups": list(group_sheets),
    })
    (stage_a / "provenance-warning.txt").write_text(
        "independent_object_detection=false\n"
        "object_extraction_status=not-tested\n"
        "v16 project_sparse_shadow called group_recursive_leaves before "
        "inserting the blank rows replayed here\n",
        encoding="utf-8",
    )

    contract_root = root / "stage5-production-contract"
    contract_root.mkdir()
    _write_json(contract_root / "mismatch.json", mismatch)
    (contract_root / "mismatch.txt").write_text(
        "status=incompatible\n"
        "conversion=forbidden\n"
        + "\n".join(
            f"- {item['legacy_evidence']}"
            for item in mismatch["issues"]
        )
        + "\n",
        encoding="utf-8",
    )

    topology_summaries = []
    strategies = (("sliding-2", 2), ("sliding-4", 4))
    for strategy, window_size in strategies:
        strategy_root = root / "stage5-debug-linear-topology" / strategy
        all_entries: list[tuple[str, Path]] = []
        group_values = []
        contaminated_blocks = 0
        nonmember_intersections = 0
        same_group_intersections = 0
        cross_group_intersections = 0
        unresolved_signature_groups = 0
        for serialized_group in replay.groups:
            blocks = plan_sliding_blocks(
                snapshot,
                serialized_group,
                window_size=window_size,
                strategy=strategy,
            )
            signatures = topology_signatures(serialized_group.segment_ids, blocks)
            exposures = foreign_bbox_exposure(snapshot, serialized_group, blocks)
            contaminated_blocks += len(exposures)
            unresolved_signature_groups += len(signatures["unresolved_groups"])
            flat_exposures = tuple(
                foreign
                for exposure in exposures
                for foreign in exposure["foreign"]
            )
            nonmember_intersections += len(flat_exposures)
            same_group_intersections += sum(
                item["scope_relation"] == "same-serialized-group"
                for item in flat_exposures
            )
            cross_group_intersections += sum(
                item["scope_relation"] == "cross-serialized-group"
                for item in flat_exposures
            )
            group_root = strategy_root / serialized_group.group_id
            block_root = group_root / "actual-blocks"
            records = []
            for block in blocks:
                path = block_root / f"{block.block_id}.png"
                _save_crop(page, block.crop_bbox, path)
                all_entries.append((block.block_id, path))
                value = _block_payload(block)
                value["png"] = str(path.relative_to(strategy_root))
                records.append(value)
            _write_jsonl(group_root / "blocks.jsonl", records)
            _write_json(group_root / "signatures.json", signatures)
            _write_jsonl(group_root / "nonmember-bbox-exposure.jsonl", exposures)
            group_values.append(
                {
                    "group_id": serialized_group.group_id,
                    "segments": len(serialized_group.segment_ids),
                    "blocks": len(blocks),
                    "signature_status": signatures["status"],
                    "unresolved_groups": len(signatures["unresolved_groups"]),
                    "contaminated_blocks": len(exposures),
                    "nonmember_intersections": len(flat_exposures),
                    "same_group_intersections": sum(
                        item["scope_relation"] == "same-serialized-group"
                        for item in flat_exposures
                    ),
                    "cross_group_intersections": sum(
                        item["scope_relation"] == "cross-serialized-group"
                        for item in flat_exposures
                    ),
                }
            )
        sheets = _write_contact_sheets(
            all_entries,
            strategy_root / "block-contact-sheets",
            prefix="blocks",
        )
        topology_signature_eligible = unresolved_signature_groups == 0
        physical_crop_membership_eligible = nonmember_intersections == 0
        sole_decoder_eligible = (
            topology_signature_eligible and physical_crop_membership_eligible
        )
        if not topology_signature_eligible:
            sole_decoder_status = "rejected-topology-signature-collisions"
        elif not physical_crop_membership_eligible:
            sole_decoder_status = "rejected-crop-membership-contamination"
        else:
            sole_decoder_status = "eligible-topology-and-crop-membership"
        role = (
            "small-window-baseline-blocked-by-crop-contamination"
            if strategy == "sliding-2"
            else "supplemental-context-only-rejected-as-sole-decoder"
        )
        summary = {
            "strategy": strategy,
            "topology_kind": "debug-linear-source-order-window",
            "production_stage5": False,
            "window_size": window_size,
            "step": 1,
            "status": sole_decoder_status,
            "role": role,
            "topology_signature_eligible": topology_signature_eligible,
            "physical_crop_membership_eligible": (
                physical_crop_membership_eligible
            ),
            "sole_decoder_eligible": sole_decoder_eligible,
            "unresolved_signature_groups": unresolved_signature_groups,
            "real_ocr_status": (
                "pending" if sole_decoder_eligible else "blocked-invalid-blocks"
            ),
            "winner": None,
            "winner_reason": (
                "real OCR timing/quality metrics were not run because the "
                "literal crops fail the declared-membership isolation gate"
                if not physical_crop_membership_eligible
                else "real OCR timing/quality metrics were not run"
            ),
            "limitation": (
                "not a 2-D orthogonal sparse-matrix planner; the synthetic "
                "decoder separately exercises horizontal/vertical A/B/C blocks"
            ),
            "crop_membership_isolation": (
                "exact" if nonmember_intersections == 0 else "failed"
            ),
            "groups": group_values,
            "blocks": sum(item["blocks"] for item in group_values),
            "contaminated_blocks": contaminated_blocks,
            "nonmember_intersections": nonmember_intersections,
            "same_group_intersections": same_group_intersections,
            "cross_group_intersections": cross_group_intersections,
            "contact_sheets": list(sheets),
        }
        _write_json(strategy_root / "summary.json", summary)
        topology_summaries.append(summary)
    page.close()

    summary = {
        "source_key": snapshot.source_key,
        "status": "fixture-integrity-pass-production-handoff-reject",
        "fixture_integrity": "pass",
        "upstream_geometry_quality": "rejected-by-legacy-audit",
        "matrix_shape": list(snapshot.matrix_shape),
        "segments": len(snapshot.records),
        "serialized_groups": len(replay.groups),
        "gap_rows": len(replay.gap_rows),
        "matrix_group_integrity": metadata_comparison["status"],
        "independent_object_extraction": "not-tested",
        "historical_group_boundary_circularity": True,
        "production_stage5": "incompatible",
        "production_handoff": "rejected",
        "topology_strategies": topology_summaries,
        "winner": None,
        "winner_reason": (
            "real OCR comparison is blocked until block crops pass declared-"
            "membership isolation"
        ),
    }
    _write_json(root / "summary.json", summary)
    report = [
        f"# Frozen v16 hand-off: {snapshot.source_key}",
        "",
        f"Serialized matrix group integrity: **{metadata_comparison['status']}**.",
        "",
        "Independent object extraction: **NOT TESTED**. Historical v16 formed `group_recursive_leaves` first and inserted these blank rows afterward (`4e86ef05`, `recursive_grid.py:280,302-305`), so the replay is circular serialization evidence, not an object detector.",
        "",
        "Current production Stage5 compatibility: **INCOMPATIBLE**. No pixel-axis conversion was attempted.",
        "",
        "| strategy | window | blocks | signature gate | nonmember intersections | role |",
        "| --- | ---: | ---: | --- | ---: | --- |",
    ]
    report.extend(
        f"| {item['strategy']} | {item['window_size']} | {item['blocks']} | "
        f"{item['status']} | {item['nonmember_intersections']} | {item['role']} |"
        for item in topology_summaries
    )
    report.extend(
        [
            "",
            "The API replays contiguous occupied rows before reading group metadata, but the historical producer encoded those gaps from the same groups; this is only an integrity round-trip.",
            "",
            "The sliding-2/sliding-4 crops are a debug-only linear source-order topology, not a compatible current 2-D Stage5 plan. The synthetic decoder separately exercises orthogonal A/B/C membership.",
            "",
            "`sliding-4` has membership-signature collisions on both fixtures and is rejected as the sole decoder; it may only be evaluated later as supplemental OCR context. `sliding-2` has unique topology signatures but also fails physical crop membership isolation, so it is not sole-decoder eligible and real OCR is blocked rather than benchmarked on invalid blocks.",
            "",
            "Nonmember bbox exposure is measured against each declared block membership, including same-group and cross-group segments. Therefore the report does not assume that a block crop contains only its declared IDs.",
            "",
            "Every individual PNG below an `actual-*` directory is a literal source-coordinate crop at its native crop resolution, not an overlay.",
            "",
            "Contact sheets are resized and labeled previews for navigation; they are not literal crop artifacts and are never used as decoder input.",
            "",
        ]
    )
    (root / "report.md").write_text("\n".join(report), encoding="utf-8")
    return summary


def _write_synthetic_decoder(root: Path) -> tuple[dict[str, object], WordDecodeResult]:
    root.mkdir(parents=True, exist_ok=False)
    page, blocks, segment_ids, observations, expected = synthetic_observed_word_case()
    page.save(root / "page.png", format="PNG", compress_level=9, optimize=False)
    block_entries = []
    for block in blocks:
        path = root / "actual-blocks" / f"{block.block_id}.png"
        _save_crop(page, block.crop_bbox, path)
        value = _block_payload(block)
        value["png"] = str(path.relative_to(root))
        block_entries.append(value)
    page.close()
    result = decode_observed_word_lattice(
        blocks=blocks,
        source_segment_ids=segment_ids,
        observations=observations,
    )
    _write_jsonl(root / "blocks.jsonl", block_entries)
    _write_jsonl(
        root / "observed-words.jsonl",
        (
            {
                "observation_id": item.observation_id,
                "block_id": item.block_id,
                "transform_id": item.transform_id,
                "text": item.text,
                "local_bbox": [
                    item.local_bbox.left,
                    item.local_bbox.top,
                    item.local_bbox.right,
                    item.local_bbox.bottom,
                ],
                "confidence": item.confidence,
            }
            for item in observations
        ),
    )
    _write_jsonl(
        root / "word-clusters.jsonl",
        (
            {
                "cluster_id": item.cluster_id,
                "text": item.text,
                "page_bbox": [
                    item.page_bbox.left,
                    item.page_bbox.top,
                    item.page_bbox.right,
                    item.page_bbox.bottom,
                ],
                "observation_ids": list(item.observation_ids),
                "lattice": [list(value) for value in item.lattice],
                "median_confidence": item.median_confidence,
            }
            for item in result.clusters
        ),
    )
    _write_jsonl(
        root / "decoded-segment-texts.jsonl",
        (
            {"segment_id": segment_id, "text": text}
            for segment_id, text in result.segment_texts
        ),
    )
    _write_jsonl(
        root / "unassigned-word-clusters.jsonl",
        (
            {"cluster_id": cluster_id, "reason": reason}
            for cluster_id, reason in result.unassigned
        ),
    )
    exact = result.segment_texts == expected
    genuine_pipe = dict(result.segment_texts)[segment_ids[3]] == "|"
    garbage_clusters = tuple(
        cluster
        for cluster in result.clusters
        if cluster.text == "|" and cluster.cluster_id not in dict(result.assignments)
    )
    summary = {
        "status": "exact" if exact and genuine_pipe else "failed",
        "segment_texts_exact": exact,
        "segment_bbox_routing": False,
        "membership_evidence": "block x transform observation lattice",
        "geometry_evidence": "observed OCR word boxes mapped by block crop origin",
        "encoder_independence": "synthetic-oracle-generated-observations",
        "crop_to_membership_contamination_tested": False,
        "genuine_standalone_separator_retained": genuine_pipe,
        "unassigned_clusters": len(result.unassigned),
        "unassigned_separator_clusters": len(garbage_clusters),
        "called": [
            "debug-only observed word clustering",
            "debug-only block/transform lattice decoder",
            "debug-only observed geometry collision guard",
        ],
        "skipped": [
            "Stage1.GeometryAnalyzer",
            "segment bbox lookup",
            "real OCR engine",
            "Stage7 assembly",
        ],
    }
    _write_json(root / "summary.json", summary)
    (root / "report.md").write_text(
        "\n".join(
            (
                "# Synthetic observed-word OR/XOR decoder",
                "",
                f"Status: **{summary['status']}**",
                "",
                "Routing used block membership, transform coverage and observed word geometry only.",
                "",
                "This is a decoder unit test with synthetic oracle-generated observations; it does not prove that a real block crop emits only its declared segment membership.",
                "",
                "A genuine standalone `|` with a complete lattice was retained. Incomplete, unmatched, and geometry-colliding garbage observations stayed unassigned.",
                "",
            )
        ),
        encoding="utf-8",
    )
    return summary, result


def _script_sha256() -> str:
    return _sha256_path(Path(__file__).resolve())


def _write_first_eight_review_inventory(
    run_root: Path,
    source_keys: tuple[str, ...],
) -> dict[str, object]:
    """Bind the exact actual PNGs covered by a first-eight visual review."""

    entries = []
    source_counts = []
    for source_key in source_keys:
        source_root = run_root / "sources" / source_key
        segment_root = (
            source_root
            / "stage-a-serialized-group-integrity"
            / "actual-segments"
        )
        sliding_two_root = (
            source_root
            / "stage5-debug-linear-topology"
            / "sliding-2"
        )
        segment_paths = tuple(sorted(segment_root.glob("*.png")))[:8]
        block_paths = tuple(
            sorted(sliding_two_root.glob("*/actual-blocks/*.png"))
        )[:8]
        if len(segment_paths) != 8 or len(block_paths) != 8:
            raise LabInvariantError(
                f"first-eight review inventory is incomplete for {source_key}"
            )
        for kind, paths in (
            ("actual-segment", segment_paths),
            ("sliding-2-actual-block", block_paths),
        ):
            for ordinal, path in enumerate(paths):
                entries.append(
                    {
                        "source_key": source_key,
                        "kind": kind,
                        "ordinal": ordinal,
                        "path": path.relative_to(run_root).as_posix(),
                        "sha256": _sha256_path(path),
                    }
                )
        source_counts.append(
            {
                "source_key": source_key,
                "actual_segments": len(segment_paths),
                "sliding_2_actual_blocks": len(block_paths),
            }
        )
    payload = {
        "schema": "v16-matrix-handoff-first-eight-review-v1",
        "manual_review_status": "first-eight-inspected",
        "selection": "lexicographically-first-eight-relative-paths-per-kind",
        "individual_png_contract": (
            "literal source-coordinate crops at native crop resolution"
        ),
        "contact_sheets_excluded": True,
        "sources": source_counts,
        "files": entries,
    }
    relative_path = Path("manual-review") / "first-eight-reviewed-files.json"
    inventory_path = run_root / relative_path
    _write_json(inventory_path, payload)
    return {
        "path": relative_path.as_posix(),
        "sha256": _sha256_path(inventory_path),
        "files": len(entries),
        "selection": payload["selection"],
    }


def run_lab(
    *,
    input_root: Path,
    output_root: Path,
    run_id: str,
    source_keys: tuple[str, ...] = SOURCE_KEYS,
    manual_review_status: str = "pending",
) -> Path:
    if not _SAFE_RUN_ID.fullmatch(run_id):
        raise ValueError("run_id is not a safe immutable identifier")
    if manual_review_status not in {"pending", "first-eight-inspected"}:
        raise ValueError("manual_review_status is invalid")
    if not source_keys or any(item not in SOURCE_KEYS for item in source_keys):
        raise ValueError("source_keys contain an unsupported source")
    destination = output_root / run_id
    if destination.exists():
        raise FileExistsError(destination)
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}-", dir=output_root))
    try:
        source_summaries = []
        source_inputs = []
        for source_key in source_keys:
            snapshot, metadata = load_frozen_v16(input_root, source_key)
            source_summaries.append(
                _write_source_lab(
                    snapshot, metadata, temporary / "sources" / source_key
                )
            )
            source_inputs.append(
                {
                    "source_key": source_key,
                    "source_path": str(snapshot.source_path),
                    "source_sha256": snapshot.source_sha256,
                    "recursion_sha256": snapshot.recursion_sha256,
                    "matrix_tsv_sha256": snapshot.matrix_tsv_sha256,
                }
            )
        decoder_summary, decoder_result = _write_synthetic_decoder(
            temporary / "synthetic-observed-word-decoder"
        )
        stage7_summary = run_ready_text_stage7(
            decoder_result.segment_texts,
            temporary / "ready-text-stage7",
        )
        reviewed_files_inventory = (
            _write_first_eight_review_inventory(temporary, source_keys)
            if manual_review_status == "first-eight-inspected"
            else None
        )
        script_path = Path(__file__).resolve()
        shutil.copyfile(script_path, temporary / "lab-source.py")
        manifest = {
            "schema": "v16-matrix-handoff-lab-v1",
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "immutable": True,
            "status": "fixture-integrity-pass-production-handoff-reject",
            "manual_review_status": manual_review_status,
            "reviewed_files_inventory": reviewed_files_inventory,
            "input_root": str(input_root),
            "inputs": source_inputs,
            "script_sha256": _script_sha256(),
            "sources": source_summaries,
            "synthetic_decoder": decoder_summary,
            "ready_text_stage7": stage7_summary,
            "called": [
                "literal v16 artifact loader",
                "serialized group-boundary integrity replay",
                "debug-only serialized-group-scoped linear topology",
                "debug-only observed-word lattice decoder",
                *stage7_summary["called"],
            ],
            "skipped": [
                "Stage1.GeometryAnalyzer and geometry.py",
                "independent object extraction from v16 matrix",
                "Stage6.ObjectReconstructor for frozen v16",
                "Stage5.OverlappingBlockPlanner for frozen v16",
                "pixel-axis conversion/laundering",
                "real OCR",
                "end-to-end DocumentAssembler.assemble",
            ],
        }
        _write_json(temporary / "manifest.json", manifest)
        _write_jsonl(temporary / "input-files.jsonl", source_inputs)
        report = [
            "# Frozen v16 matrix hand-off lab",
            "",
            f"Status: **{manifest['status']}**",
            "",
            "Stage 1 was not run or rewritten. Literal v16 segment IDs, anchors and codes were frozen.",
            "",
            "| source | serialized group integrity | independent objects | production handoff |",
            "| --- | --- | --- | --- |",
        ]
        report.extend(
            f"| {item['source_key']} | {item['matrix_group_integrity']} | "
            f"{item['independent_object_extraction']} | {item['production_handoff']} |"
            for item in source_summaries
        )
        report.extend(
            [
                "",
                "The production Stage5 mismatch is an expected fail-closed result: no legacy logical coordinate was converted into a synthetic pixel axis.",
                "",
                "The 10/18 group counts are a serialization integrity replay, not an object extraction result: historical v16 created the blank rows from the same recursive groups.",
                "",
                "`sliding-2` has unique topology signatures but fails physical crop membership isolation on both fixtures, so it is blocked as a sole decoder and real OCR is not run on invalid blocks. `sliding-4` additionally has signature collisions and remains supplemental context only.",
                "",
                "Those frozen-v16 strategies are debug-only linear source-order windows, not a 2-D production Stage5 adapter. Orthogonal A/B/C membership is exercised only by the synthetic decoder. Actual block crops are also audited for nonmember bbox exposure instead of assuming declared membership equals visible content.",
                "",
                f"Synthetic observed-word decoder: **{decoder_summary['status']}**; ready-text Stage7 candidate: **{stage7_summary['status']}**.",
                "",
                "Stage7 output is deliberately not certified: the candidate text has no real OCR/provenance chain, so public `DocumentAssembler.assemble` was not called.",
                "",
                "Every individual PNG below an `actual-*` directory is a literal source-coordinate crop at its native crop resolution, never an overlay.",
                "",
                "Contact sheets are resized and labeled previews for navigation; they are not literal crop artifacts and are never used as decoder input.",
                "",
            ]
        )
        (temporary / "report.md").write_text(
            "\n".join(report), encoding="utf-8"
        )
        rename_no_replace(temporary, destination)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return destination


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the isolated frozen v16 matrix hand-off lab.",
    )
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument(
        "--source",
        dest="source_keys",
        action="append",
        choices=SOURCE_KEYS,
        help="Run one source key; repeat for both. Defaults to both.",
    )
    parser.add_argument(
        "--manual-review-status",
        choices=("pending", "first-eight-inspected"),
        default="pending",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    destination = run_lab(
        input_root=args.input_root,
        output_root=args.output_root,
        run_id=args.run_id,
        source_keys=(
            tuple(args.source_keys)
            if args.source_keys
            else SOURCE_KEYS
        ),
        manual_review_status=args.manual_review_status,
    )
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
