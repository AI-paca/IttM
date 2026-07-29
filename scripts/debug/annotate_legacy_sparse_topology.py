#!/usr/bin/env python3
"""Canonicalize and draw any stored V16 logical sparse projection.

The conversion is artifact-only. It does not rerun geometry, object
classification, OCR, or block planning. Every logical coordinate through the
last observed x-track is materialized: foreground tracks are payload, all
other line-bounded areas are explicit empty segments, and only the remaining
row tail is represented by ``null``/repeat-last.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "ocr"))

from app.sparse_pipeline.sparse_topology import (  # noqa: E402
    ObservedTopologyRow,
    TopologyEntry,
    encode_observed_topology,
)
from app.sparse_pipeline.v16_sparse_codes import (  # noqa: E402
    EMPTY_SLOT_CODE as LEGACY_EMPTY_SLOT_CODE,
    MERGE_LEFT_CODE as LEGACY_MERGE_LEFT_CODE,
    sparse_code_components,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build canonical 0/3/5/7/null topology from stored V16 artifacts"
    )
    parser.add_argument("stage_dir", type=Path)
    parser.add_argument("--trace", default="legacy-matrix.json")
    parser.add_argument("--ownership", default="ownership.png")
    parser.add_argument("--plain-ownership", default="ownership-plain.png")
    parser.add_argument("--output", default="ownership-topology.png")
    parser.add_argument("--topology", default="sparse-topology-canonical.json")
    parser.add_argument("--methodology", default="sparse-topology-methodology.txt")
    return parser.parse_args()


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _box(value: object, *, name: str) -> tuple[int, int, int, int]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    result = tuple(value.get(key) for key in ("left", "top", "right", "bottom"))
    if not all(type(item) is int for item in result):
        raise ValueError(f"{name} must contain integral coordinates")
    left, top, right, bottom = result
    if left >= right or top >= bottom:
        raise ValueError(f"{name} must have positive area")
    return left, top, right, bottom


def _draw_code(
    draw: ImageDraw.ImageDraw,
    *,
    box: tuple[int, int, int, int],
    code: int,
    empty: bool,
) -> None:
    left, top, right, bottom = box
    width = max(1, right - left)
    height = max(1, bottom - top)
    size = max(8, min(24, height - 3, width // max(1, len(str(code)))))
    font = _font(size)
    text = str(code)
    measured = draw.textbbox((0, 0), text, font=font, stroke_width=1)
    text_width = measured[2] - measured[0]
    text_height = measured[3] - measured[1]
    x = left + (width - text_width) // 2 - measured[0]
    y = top + (height - text_height) // 2 - measured[1]
    foreground = (255, 235, 80, 255) if empty else (255, 255, 255, 255)
    draw.text(
        (x, y),
        text,
        font=font,
        fill=foreground,
        stroke_width=2,
        stroke_fill=(0, 0, 0, 255),
    )


def _canonical_rows(
    trace: dict[str, object],
) -> tuple[
    tuple[ObservedTopologyRow, ...],
    dict[tuple[int, int], str],
    dict[int, dict[str, object]],
    dict[int, frozenset[int]],
]:
    row_count = trace.get("rows")
    leaves = trace.get("leaves")
    if type(row_count) is not int or row_count < 0 or not isinstance(leaves, list):
        raise ValueError("legacy trace rows/leaves are invalid")

    payload: dict[int, set[int]] = defaultdict(set)
    merge_left: dict[int, set[int]] = defaultdict(set)
    explicit_empty: dict[int, set[int]] = defaultdict(set)
    provenance: dict[tuple[int, int], str] = {}
    leaf_by_row: dict[int, dict[str, object]] = {}
    physical_columns: dict[int, set[int]] = defaultdict(set)

    for raw_leaf in leaves:
        if not isinstance(raw_leaf, dict):
            raise ValueError("legacy leaf must be an object")
        segment_id = raw_leaf.get("segment_id")
        anchor = raw_leaf.get("anchor")
        legacy_codes = raw_leaf.get("codes")
        if (
            type(segment_id) is not str
            or not isinstance(anchor, list)
            or len(anchor) != 2
            or not all(type(item) is int for item in anchor)
            or not isinstance(legacy_codes, list)
        ):
            raise ValueError("legacy leaf projection is invalid")
        row, anchor_column = anchor
        if row in leaf_by_row:
            raise ValueError("legacy projection must have one leaf per logical row")
        leaf_by_row[row] = raw_leaf
        payload[row].add(anchor_column)
        physical_columns[row].add(anchor_column)
        provenance[(row, anchor_column)] = segment_id

        for raw_code in legacy_codes:
            if (
                not isinstance(raw_code, list)
                or len(raw_code) != 3
                or not all(type(item) is int for item in raw_code)
            ):
                raise ValueError("legacy sparse code is invalid")
            code_row, column, legacy_code = raw_code
            if code_row != row:
                raise ValueError("legacy leaf code escaped its logical row")
            signals = sparse_code_components(legacy_code)
            physical_columns[row].add(column)
            provenance[(row, column)] = segment_id
            if LEGACY_MERGE_LEFT_CODE in signals:
                # V16 incorrectly added its old EMPTY signal to these table
                # tracks. A detected merge-left track is a real payload cell.
                payload[row].add(column)
                merge_left[row].add(column)
            elif LEGACY_EMPTY_SLOT_CODE in signals:
                explicit_empty[row].add(column)
            else:
                payload[row].add(column)

    rows: list[ObservedTopologyRow] = []
    for row in range(row_count):
        observed = payload[row] | explicit_empty[row]
        if not observed:
            # A logical separator row is still a real empty page segment.
            # Its code 7 repeats through the null tail and breaks payload
            # continuity between the objects above and below it.
            rows.append(
                ObservedTopologyRow(
                    payload_columns=(),
                    empty_columns=(0,),
                )
            )
            continue
        last_column = max(observed)
        empty = set(range(last_column + 1)) - payload[row]
        owner = str(leaf_by_row[row]["segment_id"])
        for column in empty:
            provenance.setdefault((row, column), owner)
        rows.append(
            ObservedTopologyRow(
                payload_columns=tuple(sorted(payload[row])),
                merge_left_columns=tuple(sorted(merge_left[row])),
                empty_columns=tuple(sorted(empty)),
            )
        )
    return (
        tuple(rows),
        provenance,
        leaf_by_row,
        {row: frozenset(values) for row, values in physical_columns.items()},
    )


def _v16_table_regions(
    *,
    trace: dict[str, object],
    rows: tuple[ObservedTopologyRow, ...],
    leaf_by_row: dict[int, dict[str, object]],
    physical_columns: dict[int, frozenset[int]],
    x_tracks: list[int],
) -> tuple[tuple[ObservedTopologyRow, ...], tuple[dict[str, object], ...]]:
    """Restore table columns retained by v16's recursive header crop.

    V16 kept the real x-tracks and the recursive row crops, but its adapter
    serialized later table rows as anchor-only cells.  A group whose first
    leaf has at least three physical tracks is a local table seed.  Its column
    lattice continues through physically adjacent row crops until a real
    content gap ends the region.
    """

    raw_groups = trace.get("groups")
    if not isinstance(raw_groups, list):
        return rows, ()
    row_by_segment = {
        str(leaf.get("segment_id")): row for row, leaf in leaf_by_row.items()
    }
    mutable = list(rows)
    regions: list[dict[str, object]] = []
    consumed_rows: set[int] = set()
    for group in raw_groups:
        if not isinstance(group, dict):
            continue
        segment_ids = group.get("segment_ids")
        if not isinstance(segment_ids, list) or not segment_ids:
            continue
        first_row = row_by_segment.get(str(segment_ids[0]))
        if first_row is None or first_row in consumed_rows:
            continue
        raw_header_tracks = leaf_by_row[first_row].get("left_tracks")
        if not isinstance(raw_header_tracks, list) or not all(
            type(value) is int for value in raw_header_tracks
        ):
            continue
        candidate_tracks = tuple(raw_header_tracks)
        header_columns = tuple(sorted({
            min(
                range(len(x_tracks)),
                key=lambda column: abs(x_tracks[column] - track),
            )
            for track in candidate_tracks
        }))
        if len(header_columns) < 3:
            continue
        header_leaf = leaf_by_row[first_row]
        header_content = _box(
            header_leaf.get("content_bbox"), name="table header content bbox"
        )
        row_indexes = [first_row]
        previous_content = header_content
        previous_source = _box(
            header_leaf.get("source_bbox"), name="table header source bbox"
        )
        candidate_rows = sorted(row for row in leaf_by_row if row > first_row)
        for row in candidate_rows:
            leaf = leaf_by_row[row]
            source = _box(leaf.get("source_bbox"), name="table row source bbox")
            content = _box(leaf.get("content_bbox"), name="table row content bbox")
            normal_height = max(1, previous_content[3] - previous_content[1])
            content_gap = content[1] - previous_content[3]
            spans_columns = (
                source[0] <= x_tracks[header_columns[0]]
                and source[2] > x_tracks[header_columns[-1]]
            )
            follows = source[1] <= previous_source[3] + normal_height
            if not spans_columns or not follows or content_gap > max(24, normal_height):
                break
            row_indexes.append(row)
            previous_content = content
            previous_source = source

        if len(row_indexes) < 2:
            continue
        table_projection_rows = range(row_indexes[0], row_indexes[-1] + 1)
        for row in table_projection_rows:
            mutable[row] = ObservedTopologyRow(
                payload_columns=header_columns,
                merge_left_columns=header_columns[1:],
                empty_columns=tuple(
                    column
                    for column in range(header_columns[-1] + 1)
                    if column not in header_columns
                ),
            )
        consumed_rows.update(row_indexes)

        table_leaves = tuple(leaf_by_row[row] for row in row_indexes)
        source_boxes = tuple(
            _box(leaf.get("source_bbox"), name="table source bbox")
            for leaf in table_leaves
        )
        content_boxes = tuple(
            _box(leaf.get("content_bbox"), name="table content bbox")
            for leaf in table_leaves
        )
        right = max(box[2] for box in content_boxes)
        x_lines = tuple(x_tracks[column] for column in header_columns)
        if right <= x_lines[-1]:
            continue
        y_lines = tuple(
            dict.fromkeys(
                (
                    *(box[1] for box in source_boxes),
                    source_boxes[-1][3],
                )
            )
        )
        regions.append(
            {
                "bbox": [x_lines[0], source_boxes[0][1], right, source_boxes[-1][3]],
                "x_lines": [*x_lines, right],
                "y_lines": list(y_lines),
                "logical_rows": row_indexes,
                "evidence": "v16-recursive-row-and-x-track-lattice",
            }
        )
    return tuple(mutable), tuple(regions)


def main() -> int:
    args = parse_args()
    stage = args.stage_dir.resolve()
    trace_path = stage / args.trace
    ownership_path = stage / args.ownership
    plain_path = stage / args.plain_ownership
    for path in (trace_path, ownership_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    if not plain_path.is_file():
        shutil.copy2(ownership_path, plain_path)

    raw_trace = _read_json(trace_path)
    if not isinstance(raw_trace, dict):
        raise ValueError("legacy matrix trace must be an object")
    column_count = raw_trace.get("columns")
    x_tracks = raw_trace.get("x_tracks")
    if (
        type(column_count) is not int
        or column_count < 0
        or not isinstance(x_tracks, list)
        or len(x_tracks) != column_count
        or not all(type(item) is int for item in x_tracks)
    ):
        raise ValueError("legacy x-track axis is invalid")

    rows, provenance, leaf_by_row, physical_columns = _canonical_rows(raw_trace)
    rows, table_regions = _v16_table_regions(
        trace=raw_trace,
        rows=rows,
        leaf_by_row=leaf_by_row,
        physical_columns=physical_columns,
        x_tracks=x_tracks,
    )
    entries = encode_observed_topology(rows)
    entries_by_row: dict[int, list[TopologyEntry]] = defaultdict(list)
    for entry in entries:
        entries_by_row[entry.row].append(entry)

    with Image.open(plain_path) as opened:
        image = opened.convert("RGBA")
    width, height = image.size
    draw = ImageDraw.Draw(image)
    for row, row_entries in sorted(entries_by_row.items()):
        leaf = leaf_by_row.get(row)
        if leaf is None:
            continue
        content_box = _box(leaf.get("content_bbox"), name="leaf content bbox")
        left, top, right, bottom = content_box
        top = max(0, min(height - 1, top))
        bottom = max(top + 1, min(height, bottom))
        draw.rectangle(
            (left, top, right - 1, bottom - 1),
            outline=(0, 190, 215, 255),
            width=1,
        )
        observed_boundaries = physical_columns.get(row, frozenset())
        for entry in row_entries:
            x = max(0, min(width - 1, x_tracks[entry.column]))
            next_x = (
                x_tracks[entry.column + 1]
                if entry.column + 1 < len(x_tracks)
                else right
            )
            next_x = max(x + 1, min(width, next_x))
            if entry.column in observed_boundaries:
                draw.line((x, top, x, bottom - 1), fill=(35, 45, 235, 255), width=1)
            _draw_code(
                draw,
                box=(x, top, next_x, bottom),
                code=entry.code,
                empty=entry.empty,
            )

    output_path = stage / args.output
    temporary = output_path.with_name(f".{output_path.name}.partial.png")
    image.convert("RGB").save(temporary, format="PNG")
    temporary.replace(output_path)

    serialized_rows = []
    for logical_row in range(len(rows)):
        leaf = leaf_by_row.get(logical_row)
        if leaf is None:
            continue
        row_entries = entries_by_row.get(logical_row, [])
        source_box = _box(leaf.get("source_bbox"), name="leaf source bbox")
        content_box = _box(leaf.get("content_bbox"), name="leaf content bbox")
        cells = [
            {
                "column": column,
                "code": entry.code,
                "state": "empty" if entry.empty else "payload",
                "segment_id": provenance.get((entry.row, entry.column)),
                "x_track": x_tracks[entry.column],
                "x": [
                    x_tracks[entry.column],
                    (
                        x_tracks[entry.column + 1]
                        if entry.column + 1 < len(x_tracks)
                        else max(content_box[2], x_tracks[entry.column] + 1)
                    ),
                ],
                "physical_boundary": entry.column
                in physical_columns.get(logical_row, frozenset()),
            }
            for column, entry in enumerate(row_entries)
        ]
        last = row_entries[-1] if row_entries else None
        serialized_rows.append(
            {
                "row": len(serialized_rows),
                "logical_row": logical_row,
                "y": [source_box[1], source_box[3]],
                "segments": cells,
                "cells": cells,
                "compressed_codes": [
                    *(entry.code for entry in row_entries),
                    *([None] if last is not None else []),
                ],
                "null_tail": (
                    {
                        "from_column": last.column + 1,
                        "through_column_exclusive": column_count,
                        "repeat_last_code": last.code,
                    }
                    if last is not None
                    else None
                ),
            }
        )
    _write_json(
        stage / args.topology,
        {
            "schema": "canonical-v16-physical-topology-v2",
            "source": trace_path.name,
            "codes": {
                "nothing": 0,
                "merge_up": 3,
                "merge_left": 5,
                "empty": 7,
                "merge_both": 8,
            },
            "null_semantics": "repeat the last materialized code through the row tail",
            "rows": serialized_rows,
            "table_regions": list(table_regions),
            "stage_execution": "artifact-only; geometry, Stage 6, OCR, and Stage 5 were not run",
        },
    )
    methodology_path = stage / args.methodology
    methodology_path.write_text(
        "\n".join(
            (
                "Canonical sparse topology methodology",
                "1. Read physical x-tracks and logical rows from the stored Stage 1 trace.",
                "2. Scan rows top-to-bottom and tracks left-to-right.",
                "3. Treat anchors and merge-left tracks as payload segments.",
                "4. Materialize every leading/internal line-bounded gap as empty=7.",
                "5. Add merge-up=3 when the same coordinate existed in the preceding row.",
                "6. Add merge-left=5 only where Stage 1 found horizontal continuity.",
                "7. Do not materialize the tail; null repeats its final code to row end.",
                "8. Draw physical lines only inside the leaf where Stage 1 found them.",
                "9. Leave semantic object classification to Stage 6.",
                "",
            )
        ),
        encoding="utf-8",
    )
    print(output_path)
    print(stage / args.topology)
    print(f"rows={len(rows)} columns={column_count} cells={len(entries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
