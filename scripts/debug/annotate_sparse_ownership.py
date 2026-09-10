#!/usr/bin/env python3
"""Render a stored Stage 1 sparse matrix over its stored ownership image.

This utility deliberately does not import or execute the geometry analyzer.
It accepts only serialized Stage 1 artifacts, so an already-good geometry run
can be inspected repeatedly without rerunning Stage 1 or any OCR stage.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "ocr"))

from app.sparse_pipeline.sparse_topology import (  # noqa: E402
    EMPTY,
    EMPTY_SLOT_CODE,
    MERGE_LEFT_CODE,
    MERGE_UP_CODE,
    SpatialSlot,
    encode_ragged_topology,
    encode_spatial_topology,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Annotate a stored Stage 1 ownership image with literal sparse "
            "topology codes on locally detected geometry"
        )
    )
    parser.add_argument("stage_dir", type=Path)
    parser.add_argument("--matrix", default="matrix.json")
    parser.add_argument("--segments", default="segments.jsonl")
    parser.add_argument("--ownership", default="ownership.png")
    parser.add_argument("--plain-ownership", default="ownership-plain.png")
    parser.add_argument("--rule-mask", default="rule-mask.png")
    parser.add_argument("--ledger", default="ownership-overlay.json")
    parser.add_argument("--topology", default="sparse-topology.json")
    parser.add_argument("--ocr-hints", default="ocr-hints.json")
    parser.add_argument("--minimum-label-side", type=int, default=12)
    parser.add_argument("--horizontal-rule-coverage", type=float, default=0.50)
    parser.add_argument("--vertical-rule-coverage", type=float, default=0.25)
    parser.add_argument(
        "--skip-ocr-hints",
        action="store_true",
        help="Do not derive advisory, currently unused OCR geometry hints",
    )
    return parser.parse_args()


def _json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> tuple[dict[str, object], ...]:
    return tuple(
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


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


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _intervals(
    values: object,
    *,
    name: str,
    limit: int,
) -> dict[int, tuple[int, int]]:
    if not isinstance(values, list):
        raise ValueError(f"matrix {name} must be a list")
    result: dict[int, tuple[int, int]] = {}
    for value in values:
        if not isinstance(value, dict):
            raise ValueError(f"matrix {name} item must be an object")
        index = value.get("index")
        start = value.get("start")
        end = value.get("end")
        if not all(type(item) is int for item in (index, start, end)):
            raise ValueError(f"matrix {name} interval is not integral")
        if index in result or not 0 <= start < end <= limit:
            raise ValueError(f"matrix {name} interval is invalid")
        result[index] = (start, end)
    if tuple(sorted(result)) != tuple(range(len(result))):
        raise ValueError(f"matrix {name} indexes are not contiguous")
    return result


def _dense_rule_runs(
    coverage: np.ndarray,
    *,
    threshold: float,
    merge_gap: int,
) -> tuple[tuple[int, int], ...]:
    positions = tuple(int(item) for item in np.flatnonzero(coverage >= threshold))
    runs: list[tuple[int, int]] = []
    for position in positions:
        if runs and position - runs[-1][1] <= merge_gap:
            runs[-1] = (runs[-1][0], position + 1)
        else:
            runs.append((position, position + 1))
    return tuple(runs)


def _rule_grid(
    rule_mask: np.ndarray,
    *,
    horizontal_coverage: float,
    vertical_coverage: float,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if (
        rule_mask.ndim != 2
        or not 0.0 < horizontal_coverage <= 1.0
        or not 0.0 < vertical_coverage <= 1.0
    ):
        raise ValueError("rule mask or coverage threshold is invalid")
    horizontal = _dense_rule_runs(
        rule_mask.mean(axis=1),
        threshold=horizontal_coverage,
        merge_gap=8,
    )
    vertical = _dense_rule_runs(
        rule_mask.mean(axis=0),
        threshold=vertical_coverage,
        merge_gap=3,
    )
    if len(horizontal) < 2 or len(vertical) < 2:
        raise ValueError("stored rule mask does not contain a bounded 2-D grid")
    y_lines = tuple((start + stop - 1) // 2 for start, stop in horizontal)
    x_lines = tuple((start + stop - 1) // 2 for start, stop in vertical)
    return x_lines, y_lines


def _local_row_boundaries(
    rule_mask: np.ndarray,
    *,
    x_lines: tuple[int, ...],
    top: int,
    bottom: int,
    coverage_threshold: float,
) -> tuple[int, ...]:
    """Return only vertical rules physically present inside one row band."""

    margin = max(2, min(8, (bottom - top) // 8))
    row_start = top + margin
    row_stop = bottom - margin
    active: list[int] = []
    for x in x_lines:
        slab = rule_mask[
            row_start:row_stop,
            max(0, x - 8) : min(rule_mask.shape[1], x + 9),
        ]
        if slab.size and float(slab.mean()) >= coverage_threshold:
            active.append(x)
    if len(active) < 2:
        raise ValueError("table row does not contain two bounded vertical rules")
    return tuple(active)


def _segment_box(segment: dict[str, object]) -> tuple[int, int, int, int]:
    value = segment.get("bbox")
    if not isinstance(value, dict):
        raise ValueError("segment bbox must be an object")
    box = tuple(value.get(name) for name in ("left", "top", "right", "bottom"))
    if not all(type(item) is int for item in box):
        raise ValueError("segment bbox must be integral")
    left, top, right, bottom = box
    if not left < right or not top < bottom:
        raise ValueError("segment bbox must have positive area")
    return left, top, right, bottom


def _boxes_intersect(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int],
) -> bool:
    return (
        max(first[0], second[0]) < min(first[2], second[2])
        and max(first[1], second[1]) < min(first[3], second[3])
    )


def _draw_centered_code(
    draw: ImageDraw.ImageDraw,
    *,
    box: tuple[int, int, int, int],
    code: int,
    minimum_side: int,
) -> bool:
    left, top, right, bottom = box
    width = right - left
    height = bottom - top
    if min(width, height) < minimum_side:
        return False
    value = str(code)
    size = max(
        9,
        min(
            34,
            height - 4,
            max(9, (width - 6) // max(1, len(value))),
        ),
    )
    font = _font(size)
    text_box = draw.textbbox((0, 0), value, font=font, stroke_width=1)
    text_width = text_box[2] - text_box[0]
    text_height = text_box[3] - text_box[1]
    x = left + (width - text_width) // 2 - text_box[0]
    y = top + (height - text_height) // 2 - text_box[1]
    draw.text(
        (x, y),
        value,
        font=font,
        fill=(255, 255, 255, 255),
        stroke_width=3,
        stroke_fill=(0, 0, 0, 255),
    )
    return True


def _estimate_ocr_hints(
    *,
    rows: dict[int, tuple[int, int]],
    cells: tuple[dict[str, object], ...],
) -> dict[str, object]:
    owners_by_row: dict[int, set[str]] = defaultdict(set)
    for cell in cells:
        owners_by_row[int(cell["row"])].add(str(cell["segment_id"]))
    maximum_parallel_owners = max(map(len, owners_by_row.values()), default=0)
    parallel_threshold = max(3, math.ceil(maximum_parallel_owners * 0.7))
    candidates = tuple(
        (row, rows[row][0], rows[row][1])
        for row, owners in sorted(owners_by_row.items())
        if len(owners) >= parallel_threshold
        and rows[row][1] - rows[row][0] >= 8
    )
    heights = tuple(stop - start for _, start, stop in candidates)
    text_height = int(round(statistics.median(heights))) if heights else None
    pitch_candidates: tuple[int, ...] = ()
    table_pitch = None
    if text_height is not None:
        centers = tuple(
            (start + stop) // 2
            for _, start, stop in candidates
            if stop - start <= text_height * 2
        )
        pitch_candidates = tuple(
            current - previous
            for previous, current in zip(centers, centers[1:])
            if text_height * 2 <= current - previous <= text_height * 8
        )
        if pitch_candidates:
            table_pitch = int(round(statistics.median(pitch_candidates)))
    return {
        "schema": "sparse-ocr-geometry-hints-v1",
        "status": "advisory-unused",
        "consumed_by_ocr": False,
        "source": "stored Stage 1 matrix.json only",
        "estimated_text_band_height_pixels": text_height,
        "estimated_table_row_pitch_pixels": table_pitch,
        "method": (
            "median height of sparse row intervals carrying at least 70% "
            "of the maximum parallel segment-owner count; pitch is the "
            "median bounded distance between their centers"
        ),
        "parallel_owner_threshold": parallel_threshold,
        "candidate_text_band_heights": list(heights),
        "candidate_row_pitches": list(pitch_candidates),
    }


def main() -> int:
    args = parse_args()
    if args.minimum_label_side < 4:
        raise ValueError("minimum label side must be at least four pixels")
    stage = args.stage_dir.resolve()
    matrix_path = stage / args.matrix
    segments_path = stage / args.segments
    ownership_path = stage / args.ownership
    plain_path = stage / args.plain_ownership
    rule_mask_path = stage / args.rule_mask
    for path in (
        matrix_path,
        segments_path,
        ownership_path,
        rule_mask_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)

    if plain_path.is_file():
        base_path = plain_path
    else:
        shutil.copy2(ownership_path, plain_path)
        base_path = plain_path

    matrix = _json(matrix_path)
    segments = _jsonl(segments_path)
    if not isinstance(matrix, dict):
        raise ValueError("matrix.json must contain an object")
    segment_ids = tuple(str(item["segment_id"]) for item in segments)
    segment_id_set = set(segment_ids)
    if len(segment_ids) != len(segment_id_set):
        raise ValueError("segment IDs must be unique")
    segment_boxes = tuple(_segment_box(item) for item in segments)

    with Image.open(base_path) as opened:
        image = opened.convert("RGB")
    with Image.open(rule_mask_path) as opened:
        rule_mask = np.asarray(opened.convert("L"), dtype=np.uint8) > 0
    width, height = image.size
    if rule_mask.shape != (height, width):
        raise ValueError("rule mask and ownership dimensions disagree")
    rows = _intervals(matrix.get("rows"), name="rows", limit=height)
    columns = _intervals(matrix.get("columns"), name="columns", limit=width)
    raw_cells = matrix.get("cells")
    if not isinstance(raw_cells, list):
        raise ValueError("matrix cells must be a list")
    cells = tuple(raw_cells)
    seen_coordinates: set[tuple[int, int]] = set()
    for cell in cells:
        if not isinstance(cell, dict):
            raise ValueError("matrix cell must be an object")
        coordinate = (cell.get("row"), cell.get("column"))
        segment_id = str(cell.get("segment_id"))
        if (
            coordinate in seen_coordinates
            or coordinate[0] not in rows
            or coordinate[1] not in columns
            or segment_id not in segment_id_set
        ):
            raise ValueError("matrix cell is invalid or ambiguous")
        seen_coordinates.add(coordinate)

    x_lines, y_lines = _rule_grid(
        rule_mask,
        horizontal_coverage=args.horizontal_rule_coverage,
        vertical_coverage=args.vertical_rule_coverage,
    )
    table_box = (x_lines[0], y_lines[0], x_lines[-1], y_lines[-1])
    table_slots: list[tuple[SpatialSlot, ...]] = []
    table_cell_boxes: list[list[tuple[int, int, int, int]]] = []
    table_row_boundaries: list[tuple[int, ...]] = []
    occupied_table_cells: list[list[bool]] = []
    for top, bottom in zip(y_lines, y_lines[1:]):
        local_boundaries = _local_row_boundaries(
            rule_mask,
            x_lines=x_lines,
            top=top,
            bottom=bottom,
            coverage_threshold=args.vertical_rule_coverage,
        )
        table_row_boundaries.append(local_boundaries)
        occupied_row: list[bool] = []
        row_boxes: list[tuple[int, int, int, int]] = []
        row_slots: list[SpatialSlot] = []
        for left, right in zip(local_boundaries, local_boundaries[1:]):
            cell_box = (left + 1, top + 1, right, bottom)
            occupied = any(
                min(box[2] - box[0], box[3] - box[1]) > 2
                and _boxes_intersect(box, cell_box)
                for box in segment_boxes
            )
            occupied_row.append(occupied)
            row_boxes.append((left, top, right, bottom))
            row_slots.append(
                SpatialSlot(
                    start=left,
                    end=right,
                    value="ruled-grid-000000" if occupied else EMPTY,
                )
            )
        occupied_table_cells.append(occupied_row)
        table_cell_boxes.append(row_boxes)
        table_slots.append(tuple(row_slots))
    table_codes = encode_spatial_topology(tuple(table_slots))

    meaningful_flow_boxes = tuple(
        box
        for box in segment_boxes
        if min(box[2] - box[0], box[3] - box[1]) >= 8
        and not _boxes_intersect(box, table_box)
    )
    flow_components: list[str] = []
    previous: tuple[int, int, int, int] | None = None
    component_index = -1
    for box in sorted(meaningful_flow_boxes, key=lambda item: (item[1], item[0])):
        same_vertical_flow = (
            previous is not None
            and abs(box[0] - previous[0]) <= max(16, width // 200)
            and 0 <= box[1] - previous[3] <= max(48, box[3] - box[1])
        )
        if not same_vertical_flow:
            component_index += 1
        flow_components.append(f"flow-{component_index:06d}")
        previous = box
    flow_codes = encode_ragged_topology(
        tuple((component,) for component in flow_components)
    )

    image = image.convert("RGBA")
    draw = ImageDraw.Draw(image)
    labels_written = 0
    labels_skipped = 0
    local_grid_color = (35, 45, 235, 255)
    for y in y_lines:
        draw.line((x_lines[0], y, x_lines[-1], y), fill=local_grid_color, width=2)
    for row_index, (top, bottom) in enumerate(zip(y_lines, y_lines[1:])):
        for x in table_row_boundaries[row_index]:
            draw.line((x, top, x, bottom), fill=local_grid_color, width=2)
        for column, (left, _, right, _) in enumerate(table_cell_boxes[row_index]):
            code = table_codes[row_index][column]
            if _draw_centered_code(
                draw,
                box=(left, top, right, bottom),
                code=code,
                minimum_side=args.minimum_label_side,
            ):
                labels_written += 1
            else:
                labels_skipped += 1

    ordered_flow_boxes = tuple(
        sorted(meaningful_flow_boxes, key=lambda item: (item[1], item[0]))
    )
    for index, box in enumerate(ordered_flow_boxes):
        draw.rectangle(box, outline=(0, 190, 210, 255), width=2)
        code = flow_codes[index][0]
        assert code is not None
        if _draw_centered_code(
            draw,
            box=box,
            code=code,
            minimum_side=args.minimum_label_side,
        ):
            labels_written += 1
        else:
            labels_skipped += 1

    temporary_image = ownership_path.with_name(
        f".{ownership_path.name}.partial.png"
    )
    image.convert("RGB").save(temporary_image, format="PNG")
    temporary_image.replace(ownership_path)

    topology = {
        "schema": "ragged-sparse-topology-v1",
        "codes": {
            "nothing": 0,
            "merge_up": MERGE_UP_CODE,
            "merge_left": MERGE_LEFT_CODE,
            "empty": EMPTY_SLOT_CODE,
            "merge_both": MERGE_UP_CODE + MERGE_LEFT_CODE,
        },
        "null_semantics": "no discovered coordinate; omitted from row tail",
        "regions": [
            {
                "region_id": "ruled-grid-000000",
                "bbox": list(table_box),
                "x_lines": list(x_lines),
                "y_lines": list(y_lines),
                "row_x_lines": [list(row) for row in table_row_boundaries],
                "cell_boxes": [
                    [list(box) for box in row] for row in table_cell_boxes
                ],
                "codes": [list(row) for row in table_codes],
                "occupied": occupied_table_cells,
            },
            {
                "region_id": "flow-outside-grid",
                "boxes": [list(item) for item in ordered_flow_boxes],
                "codes": [list(row) for row in flow_codes],
            },
        ],
    }
    _write_json(stage / args.topology, topology)
    ledger = {
        "schema": "sparse-ownership-topology-overlay-v2",
        "inputs": {
            "matrix": matrix_path.name,
            "segments": segments_path.name,
            "plain_ownership": plain_path.name,
            "rule_mask": rule_mask_path.name,
        },
        "output": ownership_path.name,
        "number_semantics": (
            "0=nothing, 3=merge-up, 5=merge-left, 7=explicit empty; "
            "signals are additive and null is not stored"
        ),
        "grid_semantics": (
            "lines are bounded to the physical rule network or the local "
            "flow bbox; no page-edge-to-page-edge debug grid"
        ),
        "occupied_sparse_cells": len(cells),
        "topology_cells": sum(len(row) for row in table_codes)
        + len(flow_codes),
        "labels_written": labels_written,
        "labels_skipped_too_small": labels_skipped,
        "minimum_label_side": args.minimum_label_side,
        "stage_execution": "artifact-only; geometry and OCR were not run",
    }
    _write_json(stage / args.ledger, ledger)
    if not args.skip_ocr_hints:
        _write_json(
            stage / args.ocr_hints,
            _estimate_ocr_hints(rows=rows, cells=cells),
        )

    print(ownership_path)
    print(
        f"rows={len(table_codes)} "
        f"widths={','.join(str(len(row)) for row in table_codes)} "
        f"labels={labels_written} skipped={labels_skipped}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
