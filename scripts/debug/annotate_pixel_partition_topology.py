#!/usr/bin/env python3
"""Build 0/3/5/7/null topology from stored physical Stage 1 artifacts.

This is an artifact-only sweep over a ``pixel_partition`` matrix. Local
vertical rule bboxes divide each horizontal band into real regions. Regions
outside a table, including its left indentation, remain explicit segments;
their foreground state decides between payload and empty=7. Physical rules
are drawn only over the bbox where Stage 1 found them.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "ocr"))

from app.sparse_pipeline.sparse_topology import (  # noqa: E402
    EMPTY,
    SpatialSlot,
    encode_spatial_topology,
)


@dataclass(frozen=True)
class PhysicalRow:
    source_rows: tuple[int, ...]
    top: int
    bottom: int
    slots: tuple[SpatialSlot, ...]


@dataclass(frozen=True)
class RuledNetwork:
    """One connected two-dimensional network of stored physical rules."""

    bbox: tuple[int, int, int, int]
    x_lines: tuple[int, ...]
    y_lines: tuple[int, ...]


@dataclass(frozen=True)
class VisualRow:
    """One geometry-derived visual row, before numeric topology encoding."""

    row_index: int
    boxes: tuple[tuple[int, int, int, int], ...]


@dataclass(frozen=True)
class _EdgeOpenRuleCell:
    """Three stored finite rules proving one cell truncated by a raster edge."""

    edge: str
    cross_coordinate: int
    span_start: int
    span_stop: int
    rule_indexes: tuple[int, int, int]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build canonical topology from stored pixel partition geometry"
    )
    parser.add_argument("stage_dir", type=Path)
    parser.add_argument("--matrix", default="matrix.json")
    parser.add_argument("--segments", default="segments.jsonl")
    parser.add_argument("--rules", default="rules.jsonl")
    parser.add_argument("--rule-mask", default="rule-mask.png")
    parser.add_argument("--ownership", default="ownership.png")
    parser.add_argument("--plain-ownership", default="ownership-plain.png")
    parser.add_argument("--output", default="ownership-topology.png")
    parser.add_argument("--topology", default="sparse-topology-canonical.json")
    parser.add_argument("--methodology", default="sparse-topology-methodology.txt")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="write topology artifacts outside the read-only geometry directory",
    )
    parser.add_argument("--horizontal-rule-coverage", type=float, default=0.50)
    parser.add_argument("--vertical-rule-coverage", type=float, default=0.25)
    return parser.parse_args()


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> tuple[dict[str, object], ...]:
    return tuple(
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


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


def _bbox(value: object, *, name: str) -> tuple[int, int, int, int]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    result = tuple(value.get(key) for key in ("left", "top", "right", "bottom"))
    if not all(type(item) is int for item in result):
        raise ValueError(f"{name} bbox must be integral")
    left, top, right, bottom = result
    if left >= right or top >= bottom:
        raise ValueError(f"{name} bbox must have positive area")
    return left, top, right, bottom


def _axis(
    values: object,
    *,
    name: str,
    limit: int,
) -> tuple[tuple[int, int], ...]:
    if not isinstance(values, list):
        raise ValueError(f"matrix {name} must be a list")
    result: list[tuple[int, int]] = []
    for expected, item in enumerate(values):
        if not isinstance(item, dict):
            raise ValueError(f"matrix {name} item must be an object")
        index, start, end = (item.get(key) for key in ("index", "start", "end"))
        if (
            index != expected
            or type(start) is not int
            or type(end) is not int
            or start < 0
            or start >= end
            or end > limit
            or (result and result[-1][1] != start)
        ):
            raise ValueError(f"matrix {name} axis is invalid")
        result.append((start, end))
    return tuple(result)


def _overlap(first: tuple[int, int], second: tuple[int, int]) -> bool:
    return max(first[0], second[0]) < min(first[1], second[1])


def _boxes_near(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int],
    *,
    tolerance: int = 2,
) -> bool:
    """Return whether two stored rule fragments touch within detector jitter."""

    return (
        first[0] <= second[2] + tolerance
        and second[0] <= first[2] + tolerance
        and first[1] <= second[3] + tolerance
        and second[1] <= first[3] + tolerance
    )


def _dense_rule_runs(
    coverage: np.ndarray,
    *,
    threshold: float,
    merge_gap: int,
) -> tuple[tuple[int, int], ...]:
    positions = tuple(int(value) for value in np.flatnonzero(coverage >= threshold))
    runs: list[tuple[int, int]] = []
    for position in positions:
        if runs and position - runs[-1][1] <= merge_gap:
            runs[-1] = (runs[-1][0], position + 1)
        else:
            runs.append((position, position + 1))
    return tuple(runs)


def _edge_open_rule_families(
    *,
    rule_boxes: tuple[tuple[str, tuple[int, int, int, int]], ...],
    width: int,
    height: int,
    tolerance: int = 8,
) -> tuple[tuple[str, frozenset[int]], ...]:
    """Group repeated stored cells whose closing side is outside the raster.

    The geometry stage has already decided which finite bboxes are physical
    rules.  This artifact-only pass neither extends those bboxes nor changes
    the rule mask.  It merely recognizes at least three adjacent crossbar +
    two-rail witnesses ending at the same image edge so their coordinates can
    participate in one local sparse matrix.
    """

    if tolerance < 0:
        raise ValueError("edge-open tolerance must be non-negative")

    def line_length(axis: str, box: tuple[int, int, int, int]) -> int:
        return box[2] - box[0] if axis == "horizontal" else box[3] - box[1]

    def line_thickness(axis: str, box: tuple[int, int, int, int]) -> int:
        return box[3] - box[1] if axis == "horizontal" else box[2] - box[0]

    def interval_gap(point: int, interval: tuple[int, int]) -> int:
        return max(interval[0] - point, point - interval[1], 0)

    maximum_thickness = max(16, round(min(width, height) * 0.03))
    minimum_crossbar = max(32, 4 * max(1, tolerance))

    cells: list[_EdgeOpenRuleCell] = []
    for edge in ("top", "bottom", "left", "right"):
        horizontal_crossbar = edge in ("top", "bottom")
        crossbar_axis = "horizontal" if horizontal_crossbar else "vertical"
        rail_axis = "vertical" if horizontal_crossbar else "horizontal"
        crossbars = tuple(
            (index, box)
            for index, (axis, box) in enumerate(rule_boxes)
            if axis == crossbar_axis
            and line_length(axis, box) >= minimum_crossbar
            and line_thickness(axis, box) <= maximum_thickness
        )
        rails = tuple(
            (index, box)
            for index, (axis, box) in enumerate(rule_boxes)
            if axis == rail_axis
            and line_thickness(axis, box) <= maximum_thickness
        )
        for crossbar_index, crossbar in crossbars:
            if horizontal_crossbar:
                coordinate = (crossbar[1] + crossbar[3]) // 2
                span = (crossbar[0], crossbar[2])
            else:
                coordinate = (crossbar[0] + crossbar[2]) // 2
                span = (crossbar[1], crossbar[3])
            crossbar_length = span[1] - span[0]
            minimum_rail = max(2 * maximum_thickness, round(0.25 * crossbar_length))
            eligible: list[tuple[int, tuple[int, int, int, int]]] = []
            for rail_index, rail in rails:
                if line_length(rail_axis, rail) < minimum_rail:
                    continue
                if edge == "bottom":
                    touches_edge = rail[3] >= height - tolerance
                    starts_at_crossbar = abs(rail[1] - coordinate) <= 2 * tolerance
                elif edge == "top":
                    touches_edge = rail[1] <= tolerance
                    starts_at_crossbar = abs(rail[3] - coordinate) <= 2 * tolerance
                elif edge == "right":
                    touches_edge = rail[2] >= width - tolerance
                    starts_at_crossbar = abs(rail[0] - coordinate) <= 2 * tolerance
                else:
                    touches_edge = rail[0] <= tolerance
                    starts_at_crossbar = abs(rail[2] - coordinate) <= 2 * tolerance
                if touches_edge and starts_at_crossbar:
                    eligible.append((rail_index, rail))

            def nearest(endpoint: int) -> tuple[int, ...]:
                return tuple(
                    index
                    for index, _ in sorted(
                        (
                            (rail_index, rail)
                            for rail_index, rail in eligible
                            if interval_gap(
                                endpoint,
                                (
                                    (rail[0], rail[2])
                                    if horizontal_crossbar
                                    else (rail[1], rail[3])
                                ),
                            )
                            <= 2 * tolerance
                        ),
                        key=lambda value: (
                            interval_gap(
                                endpoint,
                                (
                                    (value[1][0], value[1][2])
                                    if horizontal_crossbar
                                    else (value[1][1], value[1][3])
                                ),
                            ),
                            -line_length(rail_axis, value[1]),
                            value[0],
                        ),
                    )
                )

            first = nearest(span[0])
            second = nearest(span[1])
            if not first or not second or first[0] == second[0]:
                continue
            cells.append(
                _EdgeOpenRuleCell(
                    edge=edge,
                    cross_coordinate=coordinate,
                    span_start=span[0],
                    span_stop=span[1],
                    rule_indexes=(crossbar_index, first[0], second[0]),
                )
            )

    distinct: list[_EdgeOpenRuleCell] = []
    for cell in sorted(
        cells,
        key=lambda value: (
            value.edge,
            value.cross_coordinate,
            value.span_start,
            value.span_stop,
        ),
    ):
        if any(
            accepted.edge == cell.edge
            and abs(accepted.cross_coordinate - cell.cross_coordinate)
            <= 2 * tolerance
            and (
                min(accepted.span_stop, cell.span_stop)
                - max(accepted.span_start, cell.span_start)
            )
            >= 0.75
            * min(
                accepted.span_stop - accepted.span_start,
                cell.span_stop - cell.span_start,
            )
            for accepted in distinct
        ):
            continue
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
                and abs(left.cross_coordinate - right.cross_coordinate)
                <= 2 * tolerance
                and max(
                    left.span_start - right.span_stop,
                    right.span_start - left.span_stop,
                    0,
                )
                <= max(24, 3 * tolerance)
            ):
                union(first, second)

    grouped: dict[int, list[_EdgeOpenRuleCell]] = {}
    for index, cell in enumerate(distinct):
        grouped.setdefault(find(index), []).append(cell)
    return tuple(
        (
            members[0].edge,
            frozenset(
                rule_index
                for member in members
                for rule_index in member.rule_indexes
            ),
        )
        for members in grouped.values()
        if len(members) >= 3
    )


def _rule_networks(
    *,
    rule_boxes: tuple[tuple[str, tuple[int, int, int, int]], ...],
    rule_mask: np.ndarray,
    horizontal_coverage: float,
    vertical_coverage: float,
) -> tuple[RuledNetwork, ...]:
    """Find geometric 2-D networks without assigning an object type.

    Connectivity comes from the stored finite rule bboxes. Projections are
    then measured only inside each connected component, so an unrelated rule
    elsewhere on the page cannot turn into a page-wide grid line.
    """

    if not 0.0 < horizontal_coverage <= 1.0:
        raise ValueError("horizontal rule coverage must be in (0, 1]")
    if not 0.0 < vertical_coverage <= 1.0:
        raise ValueError("vertical rule coverage must be in (0, 1]")

    parents = list(range(len(rule_boxes)))

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

    edge_families = _edge_open_rule_families(
        rule_boxes=rule_boxes,
        width=rule_mask.shape[1],
        height=rule_mask.shape[0],
    )
    for _, indexes in edge_families:
        ordered = tuple(sorted(indexes))
        for index in ordered[1:]:
            union(ordered[0], index)

    for first, (_, first_box) in enumerate(rule_boxes):
        for second in range(first + 1, len(rule_boxes)):
            if _boxes_near(first_box, rule_boxes[second][1]):
                union(first, second)

    components: dict[int, list[int]] = {}
    for index in range(len(rule_boxes)):
        components.setdefault(find(index), []).append(index)

    height, width = rule_mask.shape
    networks: list[RuledNetwork] = []
    for indexes in components.values():
        axes = tuple(rule_boxes[index][0] for index in indexes)
        component_edges = {
            edge
            for edge, family_indexes in edge_families
            if family_indexes.intersection(indexes)
        }
        horizontal_count = axes.count("horizontal")
        vertical_count = axes.count("vertical")
        ordinary_lattice = horizontal_count >= 2 and vertical_count >= 2
        edge_lattice = any(
            (
                edge in ("top", "bottom")
                and horizontal_count >= 1
                and vertical_count >= 2
            )
            or (
                edge in ("left", "right")
                and vertical_count >= 1
                and horizontal_count >= 2
            )
            for edge in component_edges
        )
        if not ordinary_lattice and not edge_lattice:
            continue
        boxes = tuple(rule_boxes[index][1] for index in indexes)
        left = max(0, min(box[0] for box in boxes))
        top = max(0, min(box[1] for box in boxes))
        right = min(width, max(box[2] for box in boxes))
        bottom = min(height, max(box[3] for box in boxes))
        if "left" in component_edges:
            left = 0
        if "top" in component_edges:
            top = 0
        if "right" in component_edges:
            right = width
        if "bottom" in component_edges:
            bottom = height
        if left >= right or top >= bottom:
            continue
        local_mask = rule_mask[top:bottom, left:right]
        horizontal_runs = _dense_rule_runs(
            local_mask.mean(axis=1),
            threshold=horizontal_coverage,
            merge_gap=8,
        )
        vertical_runs = _dense_rule_runs(
            local_mask.mean(axis=0),
            threshold=vertical_coverage,
            merge_gap=8,
        )
        if not horizontal_runs or not vertical_runs:
            continue
        x_lines = {
            left + (start + stop - 1) // 2
            for start, stop in vertical_runs
        }
        # A border coincident with the raster edge has no outside pixel from
        # which an RGB gradient can be sampled.  Repeated finite horizontal
        # rules ending at that edge still prove the outer table boundary.
        if left == 0:
            x_lines.add(0)
        if right == width:
            x_lines.add(width)
        if "left" in component_edges:
            x_lines.add(0)
        if "right" in component_edges:
            x_lines.add(width)
        y_lines = {
            top + (start + stop - 1) // 2
            for start, stop in horizontal_runs
        }
        if top == 0:
            y_lines.add(0)
        if bottom == height:
            y_lines.add(height)
        if "top" in component_edges:
            y_lines.add(0)
        if "bottom" in component_edges:
            y_lines.add(height)
        if len(x_lines) < 2 or len(y_lines) < 2:
            continue
        networks.append(
            RuledNetwork(
                bbox=(left, top, right, bottom),
                x_lines=tuple(sorted(x_lines)),
                y_lines=tuple(sorted(y_lines)),
            )
        )
    return tuple(sorted(networks, key=lambda value: (value.bbox[1], value.bbox[0])))


def _active_network_x_lines(
    *,
    network: RuledNetwork,
    rule_mask: np.ndarray,
    top: int,
    bottom: int,
    coverage_threshold: float,
) -> tuple[int, ...]:
    """Keep only vertical boundaries physically present in this row band."""

    margin = max(2, min(8, (bottom - top) // 8))
    row_start = min(bottom, top + margin)
    row_stop = max(row_start + 1, bottom - margin)
    active = {network.x_lines[0], network.x_lines[-1]}
    for x in network.x_lines[1:-1]:
        slab = rule_mask[
            row_start:row_stop,
            max(0, x - 8) : min(rule_mask.shape[1], x + 9),
        ]
        # The ±8 window is localization tolerance, not rule thickness.  A
        # two-pixel finite rule should therefore be scored by its strongest
        # candidate column instead of being diluted over all 17 columns.
        if (
            slab.size
            and float(slab.mean(axis=0).max(initial=0.0))
            >= coverage_threshold
        ):
            active.add(x)
    return tuple(sorted(active))


def _box_has_payload(
    box: tuple[int, int, int, int],
    occupied_boxes: tuple[tuple[int, int, int, int], ...],
) -> bool:
    left, top, right, bottom = box
    return any(
        max(left, other_left) < min(right, other_right)
        and max(top, other_top) < min(bottom, other_bottom)
        for other_left, other_top, other_right, other_bottom in occupied_boxes
    )


def _visual_rows(
    values: tuple[dict[str, object], ...],
) -> tuple[VisualRow, ...]:
    """Group Stage 1 segments by their visual row, never by word boxes."""

    grouped: dict[int, list[tuple[int, int, int, int]]] = {}
    for value in values:
        row_index = value.get("row_index")
        if type(row_index) is not int or row_index < 0:
            raise ValueError("segment row_index must be a non-negative integer")
        grouped.setdefault(row_index, []).append(
            _bbox(value.get("bbox"), name="segment")
        )
    return tuple(
        VisualRow(row_index=row_index, boxes=tuple(boxes))
        for row_index, boxes in sorted(grouped.items())
    )


def _outside_visual_rows(
    *,
    network: RuledNetwork,
    visual_rows: tuple[VisualRow, ...],
) -> tuple[VisualRow, ...]:
    """Clip visual rows to the finite space left/right of one rule network.

    All boxes on one side and visual baseline are coalesced to one interval.
    Consequently character or word boxes can never become sparse segments.
    """

    network_left = network.x_lines[0]
    network_top = network.y_lines[0]
    network_right = network.x_lines[-1]
    network_bottom = network.y_lines[-1]
    result: list[VisualRow] = []
    for row in visual_rows:
        left_boxes: list[tuple[int, int, int, int]] = []
        right_boxes: list[tuple[int, int, int, int]] = []
        for left, top, right, bottom in row.boxes:
            clipped_top = max(top, network_top)
            clipped_bottom = min(bottom, network_bottom)
            if clipped_top >= clipped_bottom:
                continue
            if left < network_left:
                left_boxes.append(
                    (
                        left,
                        clipped_top,
                        min(right, network_left),
                        clipped_bottom,
                    )
                )
            if right > network_right:
                right_boxes.append(
                    (
                        max(left, network_right),
                        clipped_top,
                        right,
                        clipped_bottom,
                    )
                )
        boxes: list[tuple[int, int, int, int]] = []
        for side_boxes in (left_boxes, right_boxes):
            side_boxes = [box for box in side_boxes if box[0] < box[2]]
            if not side_boxes:
                continue
            boxes.append(
                (
                    min(box[0] for box in side_boxes),
                    min(box[1] for box in side_boxes),
                    max(box[2] for box in side_boxes),
                    max(box[3] for box in side_boxes),
                )
            )
        if boxes:
            result.append(VisualRow(row_index=row.row_index, boxes=tuple(boxes)))
    return tuple(result)


def _ruled_network_rows(
    *,
    network: RuledNetwork,
    rule_mask: np.ndarray,
    rows: tuple[tuple[int, int], ...],
    occupied_boxes: tuple[tuple[int, int, int, int], ...],
    width: int,
    vertical_coverage: float,
    payload_value: str,
    visual_rows: tuple[VisualRow, ...] = (),
) -> tuple[PhysicalRow, ...]:
    """Materialize a finite rule network and concurrent outside flow rows.

    A network row may be split at the top/bottom of a geometry visual row.
    Its table-cell values are copied into each subdivision, so the numeric
    result expresses continuation with merge-up instead of inventing cells.
    """

    result: list[PhysicalRow] = []
    outside_rows = _outside_visual_rows(
        network=network,
        visual_rows=visual_rows,
    )
    for top, bottom in zip(network.y_lines, network.y_lines[1:]):
        local_lines = _active_network_x_lines(
            network=network,
            rule_mask=rule_mask,
            top=top,
            bottom=bottom,
            coverage_threshold=vertical_coverage,
        )
        table_slots = tuple(
            SpatialSlot(
                start=left,
                end=right,
                value=(
                    payload_value
                    if _box_has_payload(
                        (left, top + 1, right, max(top + 2, bottom)),
                        occupied_boxes,
                    )
                    else EMPTY
                ),
            )
            for left, right in zip(local_lines, local_lines[1:])
            if left < right
        )
        intersecting_visual_rows = tuple(
            row
            for row in outside_rows
            if any(_overlap((top, bottom), (box[1], box[3])) for box in row.boxes)
        )
        y_boundaries = {top, bottom}
        for row in intersecting_visual_rows:
            for box in row.boxes:
                if _overlap((top, bottom), (box[1], box[3])):
                    y_boundaries.update((max(top, box[1]), min(bottom, box[3])))
        for local_top, local_bottom in zip(
            sorted(y_boundaries), sorted(y_boundaries)[1:]
        ):
            active_visual_rows = tuple(
                row
                for row in intersecting_visual_rows
                if any(
                    _overlap((local_top, local_bottom), (box[1], box[3]))
                    for box in row.boxes
                )
            )
            boundaries = {0, width, *local_lines}
            for row in active_visual_rows:
                for box in row.boxes:
                    if _overlap((local_top, local_bottom), (box[1], box[3])):
                        boundaries.update((box[0], box[2]))
            ordered = tuple(sorted(boundaries))
            slots: list[SpatialSlot] = []
            for left, right in zip(ordered, ordered[1:]):
                table_slot = next(
                    (
                        slot
                        for slot in table_slots
                        if slot.start <= left and right <= slot.end
                    ),
                    None,
                )
                if table_slot is not None:
                    value = table_slot.value
                else:
                    payload_rows = tuple(
                        row.row_index
                        for row in active_visual_rows
                        if any(
                            max(left, box[0]) < min(right, box[2])
                            and _overlap(
                                (local_top, local_bottom),
                                (box[1], box[3]),
                            )
                            for box in row.boxes
                        )
                    )
                    value = (
                        ("visual-flow-row", *payload_rows)
                        if payload_rows
                        else EMPTY
                    )
                slots.append(SpatialSlot(start=left, end=right, value=value))
            source_rows = tuple(
                index
                for index, interval in enumerate(rows)
                if _overlap((local_top, local_bottom), interval)
            )
            result.append(
                PhysicalRow(
                    source_rows=source_rows,
                    top=local_top,
                    bottom=local_bottom,
                    slots=tuple(slots),
                )
            )
    return tuple(result)


def _subtract_box(
    box: tuple[int, int, int, int],
    blocker: tuple[int, int, int, int],
) -> tuple[tuple[int, int, int, int], ...]:
    """Subtract one finite rectangle without extending either of its axes."""

    left, top, right, bottom = box
    cut_left = max(left, blocker[0])
    cut_top = max(top, blocker[1])
    cut_right = min(right, blocker[2])
    cut_bottom = min(bottom, blocker[3])
    if cut_left >= cut_right or cut_top >= cut_bottom:
        return (box,)
    pieces = (
        (left, top, right, cut_top),
        (left, cut_bottom, right, bottom),
        (left, cut_top, cut_left, cut_bottom),
        (cut_right, cut_top, right, cut_bottom),
    )
    return tuple(
        piece
        for piece in pieces
        if piece[0] < piece[2] and piece[1] < piece[3]
    )


def _outside_all_networks_visual_rows(
    *,
    networks: tuple[RuledNetwork, ...],
    visual_rows: tuple[VisualRow, ...],
) -> tuple[VisualRow, ...]:
    """Retain only visual-row rectangles outside every finite rule lattice."""

    blockers = tuple(
        (
            network.x_lines[0],
            network.y_lines[0],
            network.x_lines[-1],
            network.y_lines[-1],
        )
        for network in networks
    )
    result: list[VisualRow] = []
    for row in visual_rows:
        pieces = row.boxes
        for blocker in blockers:
            pieces = tuple(
                remainder
                for piece in pieces
                for remainder in _subtract_box(piece, blocker)
            )
        if pieces:
            result.append(VisualRow(row_index=row.row_index, boxes=pieces))
    return tuple(result)


def _network_table_slots(
    *,
    network: RuledNetwork,
    payload_value: str,
    local_top: int,
    local_bottom: int,
    rule_mask: np.ndarray,
    occupied_boxes: tuple[tuple[int, int, int, int], ...],
    vertical_coverage: float,
) -> tuple[SpatialSlot, ...]:
    """Return the unsplit cells of the network band containing a planar row."""

    source_band = next(
        (
            (top, bottom)
            for top, bottom in zip(network.y_lines, network.y_lines[1:])
            if top <= local_top < local_bottom <= bottom
        ),
        None,
    )
    if source_band is None:
        return ()
    top, bottom = source_band
    local_lines = _active_network_x_lines(
        network=network,
        rule_mask=rule_mask,
        top=top,
        bottom=bottom,
        coverage_threshold=vertical_coverage,
    )
    return tuple(
        SpatialSlot(
            start=left,
            end=right,
            value=(
                payload_value
                if _box_has_payload(
                    (left, top + 1, right, max(top + 2, bottom)),
                    occupied_boxes,
                )
                else EMPTY
            ),
        )
        for left, right in zip(local_lines, local_lines[1:])
        if left < right
    )


def _coalesced_flow_intervals(
    *,
    visual_rows: tuple[VisualRow, ...],
    active_networks: tuple[RuledNetwork, ...],
    top: int,
    bottom: int,
    width: int,
) -> tuple[tuple[int, int, int], ...]:
    """Make one interval per visual row and free planar X region."""

    blocked = tuple(
        sorted((network.x_lines[0], network.x_lines[-1]) for network in active_networks)
    )
    if any(first[1] > second[0] for first, second in zip(blocked, blocked[1:])):
        raise ValueError("concurrent ruled networks must not overlap in X")
    free_regions: list[tuple[int, int]] = []
    cursor = 0
    for left, right in blocked:
        if cursor < left:
            free_regions.append((cursor, left))
        cursor = max(cursor, right)
    if cursor < width:
        free_regions.append((cursor, width))

    result: list[tuple[int, int, int]] = []
    for row in visual_rows:
        for region_left, region_right in free_regions:
            fragments = tuple(
                (max(box[0], region_left), min(box[2], region_right))
                for box in row.boxes
                if _overlap((top, bottom), (box[1], box[3]))
                and max(box[0], region_left) < min(box[2], region_right)
            )
            if fragments:
                result.append(
                    (
                        min(fragment[0] for fragment in fragments),
                        max(fragment[1] for fragment in fragments),
                        row.row_index,
                    )
                )
    return tuple(result)


def _combined_ruled_network_rows(
    *,
    networks: tuple[RuledNetwork, ...],
    rule_mask: np.ndarray,
    rows: tuple[tuple[int, int], ...],
    occupied_boxes: tuple[tuple[int, int, int, int], ...],
    width: int,
    vertical_coverage: float,
    visual_rows: tuple[VisualRow, ...] = (),
) -> tuple[PhysicalRow, ...]:
    """Compose concurrent finite rule networks into one planar row sweep."""

    if not networks:
        return ()
    if len(networks) == 1:
        return _ruled_network_rows(
            network=networks[0],
            rule_mask=rule_mask,
            rows=rows,
            occupied_boxes=occupied_boxes,
            width=width,
            vertical_coverage=vertical_coverage,
            payload_value="ruled-network-000000",
            visual_rows=visual_rows,
        )

    outside_rows = _outside_all_networks_visual_rows(
        networks=networks,
        visual_rows=visual_rows,
    )
    y_boundaries = {
        line
        for network in networks
        for line in network.y_lines
    }
    minimum_top = min(network.y_lines[0] for network in networks)
    maximum_bottom = max(network.y_lines[-1] for network in networks)
    for row in outside_rows:
        for box in row.boxes:
            if _overlap((minimum_top, maximum_bottom), (box[1], box[3])):
                y_boundaries.update(
                    (
                        max(minimum_top, box[1]),
                        min(maximum_bottom, box[3]),
                    )
                )

    result: list[PhysicalRow] = []
    ordered_y = tuple(sorted(y_boundaries))
    for local_top, local_bottom in zip(ordered_y, ordered_y[1:]):
        active = tuple(
            (index, network)
            for index, network in enumerate(networks)
            if network.y_lines[0] <= local_top
            and local_bottom <= network.y_lines[-1]
        )
        if not active:
            continue
        active_networks = tuple(network for _, network in active)
        table_slots = tuple(
            slot
            for index, network in active
            for slot in _network_table_slots(
                network=network,
                payload_value=f"ruled-network-{index:06d}",
                local_top=local_top,
                local_bottom=local_bottom,
                rule_mask=rule_mask,
                occupied_boxes=occupied_boxes,
                vertical_coverage=vertical_coverage,
            )
        )
        flow_intervals = _coalesced_flow_intervals(
            visual_rows=outside_rows,
            active_networks=active_networks,
            top=local_top,
            bottom=local_bottom,
            width=width,
        )
        boundaries = {
            0,
            width,
            *(slot.start for slot in table_slots),
            *(slot.end for slot in table_slots),
            *(left for left, _, _ in flow_intervals),
            *(right for _, right, _ in flow_intervals),
        }
        ordered_x = tuple(sorted(boundaries))
        slots: list[SpatialSlot] = []
        for left, right in zip(ordered_x, ordered_x[1:]):
            table_slot = next(
                (
                    slot
                    for slot in table_slots
                    if slot.start <= left and right <= slot.end
                ),
                None,
            )
            if table_slot is not None:
                value = table_slot.value
            else:
                payload_rows = tuple(
                    row_index
                    for flow_left, flow_right, row_index in flow_intervals
                    if max(left, flow_left) < min(right, flow_right)
                )
                value = (
                    ("visual-flow-row", *payload_rows)
                    if payload_rows
                    else EMPTY
                )
            slots.append(SpatialSlot(start=left, end=right, value=value))
        source_rows = tuple(
            index
            for index, interval in enumerate(rows)
            if _overlap((local_top, local_bottom), interval)
        )
        result.append(
            PhysicalRow(
                source_rows=source_rows,
                top=local_top,
                bottom=local_bottom,
                slots=tuple(slots),
            )
        )
    return tuple(result)


def _row_slots(
    *,
    row: int,
    row_interval: tuple[int, int],
    columns: tuple[tuple[int, int], ...],
    occupied_columns: frozenset[int],
    vertical_rules: tuple[tuple[int, int, int, int], ...],
    visual_rows: tuple[VisualRow, ...],
    width: int,
) -> tuple[SpatialSlot, ...]:
    """Materialize every finite segment and the empty space between them.

    The dense pixel matrix is deliberately finer than a document segment.
    Collapsing all occupied dense columns to their outer bounding interval is
    therefore lossy: two independent Stage 1 segments on the same physical
    band become one payload cell and the empty object boundary between them
    disappears.  Stage 1 visual rows are already above word/glyph granularity,
    so use those retained boxes as the finite X tracks.  The dense matrix is
    only a fallback for structural payload without a visual-row segment.
    """

    top, bottom = row_interval
    active_rules = tuple(
        box
        for box in vertical_rules
        if _overlap((top, bottom), (box[1], box[3]))
    )
    flow_intervals: list[tuple[int, int, int]] = []
    for visual_row in visual_rows:
        fragments = tuple(
            (box[0], box[2])
            for box in visual_row.boxes
            if _overlap((top, bottom), (box[1], box[3]))
        )
        if fragments:
            # Several boxes with one visual-row id are words/fragments of the
            # same Stage 1 segment.  Coalesce only those; distinct segment ids
            # remain distinct finite intervals with an explicit empty slot in
            # between.
            flow_intervals.append(
                (
                    min(value[0] for value in fragments),
                    max(value[1] for value in fragments),
                    visual_row.row_index,
                )
            )
    boundaries = {0, width}
    boundaries.update((box[0] + box[2]) // 2 for box in active_rules)
    boundaries.update(left for left, _, _ in flow_intervals)
    boundaries.update(right for _, right, _ in flow_intervals)

    if not flow_intervals and not active_rules and occupied_columns:
        occupied_intervals = tuple(columns[index] for index in occupied_columns)
        boundaries.add(min(start for start, _ in occupied_intervals))
        boundaries.add(max(end for _, end in occupied_intervals))

    ordered = tuple(sorted(boundaries))
    slots: list[SpatialSlot] = []
    for left, right in zip(ordered, ordered[1:]):
        payload_rows = tuple(
            row_index
            for flow_left, flow_right, row_index in flow_intervals
            if _overlap((left, right), (flow_left, flow_right))
        )
        fallback_payload = not flow_intervals and any(
            _overlap((left, right), columns[column])
            for column in occupied_columns
        )
        slots.append(
            SpatialSlot(
                start=left,
                end=right,
                value=(
                    ("visual-flow-row", *payload_rows)
                    if payload_rows
                    else (("dense-matrix-row", row) if fallback_payload else EMPTY)
                ),
            )
        )
    return tuple(slots)


def _physical_rows(
    *,
    rows: tuple[tuple[int, int], ...],
    columns: tuple[tuple[int, int], ...],
    cells_by_row: dict[int, frozenset[int]],
    horizontal_rule_rows: frozenset[int],
    vertical_rules: tuple[tuple[int, int, int, int], ...],
    visual_rows: tuple[VisualRow, ...],
    width: int,
    excluded_y: tuple[tuple[int, int], ...] = (),
) -> tuple[PhysicalRow, ...]:
    values: list[PhysicalRow] = []
    rule_barrier = False
    for row, interval in enumerate(rows):
        if row in horizontal_rule_rows or any(
            _overlap(interval, excluded) for excluded in excluded_y
        ):
            rule_barrier = True
            continue
        slots = _row_slots(
            row=row,
            row_interval=interval,
            columns=columns,
            occupied_columns=cells_by_row.get(row, frozenset()),
            vertical_rules=vertical_rules,
            visual_rows=visual_rows,
            width=width,
        )
        if (
            values
            and not rule_barrier
            and values[-1].bottom == interval[0]
            and values[-1].slots == slots
        ):
            previous = values[-1]
            values[-1] = PhysicalRow(
                source_rows=(*previous.source_rows, row),
                top=previous.top,
                bottom=interval[1],
                slots=slots,
            )
        else:
            values.append(
                PhysicalRow(
                    source_rows=(row,),
                    top=interval[0],
                    bottom=interval[1],
                    slots=slots,
                )
            )
        rule_barrier = False
    return tuple(values)


def _encode_physical_rows(
    rows: tuple[PhysicalRow, ...],
) -> tuple[tuple[int, ...], ...]:
    """Encode only physically adjacent row runs.

    A missing Y band is an unobserved frontier, not vertical-continuation
    evidence.  Reset the encoder after every gap so a later 3/8 can never
    point through omitted geometry.
    """

    result: list[tuple[int, ...]] = []
    group_start = 0
    for index in range(1, len(rows) + 1):
        boundary = index == len(rows) or rows[index - 1].bottom != rows[index].top
        if not boundary:
            continue
        result.extend(
            encode_spatial_topology(
                tuple(row.slots for row in rows[group_start:index])
            )
        )
        group_start = index
    return tuple(result)


def _draw_code(
    draw: ImageDraw.ImageDraw,
    *,
    box: tuple[int, int, int, int],
    code: int,
    empty: bool,
) -> None:
    left, top, right, bottom = box
    width = right - left
    height = bottom - top
    if width < 6 or height < 6:
        return
    text = str(code)
    font = _font(max(8, min(28, height - 3, width // max(1, len(text)))))
    measured = draw.textbbox((0, 0), text, font=font, stroke_width=1)
    x = left + (width - (measured[2] - measured[0])) // 2 - measured[0]
    y = top + (height - (measured[3] - measured[1])) // 2 - measured[1]
    draw.text(
        (x, y),
        text,
        font=font,
        fill=(255, 235, 80, 255) if empty else (255, 255, 255, 255),
        stroke_width=2,
        stroke_fill=(0, 0, 0, 255),
    )


def main() -> int:
    args = parse_args()
    stage = args.stage_dir.resolve()
    output_stage = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else stage
    )
    if output_stage != stage:
        if output_stage.exists():
            raise FileExistsError(output_stage)
        output_stage.mkdir(parents=True)
    matrix_path = stage / args.matrix
    segments_path = stage / args.segments
    rules_path = stage / args.rules
    rule_mask_path = stage / args.rule_mask
    ownership_path = stage / args.ownership
    plain_path = stage / args.plain_ownership
    for path in (
        matrix_path,
        segments_path,
        rules_path,
        rule_mask_path,
        ownership_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    if not plain_path.is_file() and output_stage == stage:
        shutil.copy2(ownership_path, plain_path)
    elif not plain_path.is_file():
        plain_path = ownership_path

    with Image.open(plain_path) as opened:
        image = opened.convert("RGBA")
    width, height = image.size
    with Image.open(rule_mask_path) as opened:
        rule_mask = np.asarray(opened.convert("L"), dtype=np.uint8) > 0
    if rule_mask.shape != (height, width):
        raise ValueError("rule mask and ownership dimensions disagree")
    raw_matrix = _read_json(matrix_path)
    if not isinstance(raw_matrix, dict):
        raise ValueError("matrix must be an object")
    if raw_matrix.get("coordinate_mode", "pixel_partition") != "pixel_partition":
        raise ValueError("this renderer requires a physical pixel_partition matrix")
    rows = _axis(raw_matrix.get("rows"), name="rows", limit=height)
    columns = _axis(raw_matrix.get("columns"), name="columns", limit=width)
    raw_cells = raw_matrix.get("cells")
    if not isinstance(raw_cells, list):
        raise ValueError("matrix cells must be a list")
    cell_values: dict[int, set[int]] = {}
    for cell in raw_cells:
        if not isinstance(cell, dict):
            raise ValueError("matrix cell must be an object")
        row, column = cell.get("row"), cell.get("column")
        if (
            type(row) is not int
            or type(column) is not int
            or not 0 <= row < len(rows)
            or not 0 <= column < len(columns)
        ):
            raise ValueError("matrix cell coordinate is invalid")
        cell_values.setdefault(row, set()).add(column)
    cells_by_row = {
        row: frozenset(values) for row, values in cell_values.items()
    }

    rules = _read_jsonl(rules_path)
    rule_boxes = tuple(
        (str(rule.get("axis")), _bbox(rule.get("bbox"), name="rule"))
        for rule in rules
    )
    vertical_rules = tuple(box for axis, box in rule_boxes if axis == "vertical")
    horizontal_rule_rows = frozenset(
        int(value) for value in raw_matrix.get("horizontal_rule_rows", [])
    )
    vertical_rule_columns = frozenset(
        int(value) for value in raw_matrix.get("vertical_rule_columns", [])
    )
    occupied_boxes = tuple(
        (
            columns[int(cell["column"])][0],
            rows[int(cell["row"])][0],
            columns[int(cell["column"])][1],
            rows[int(cell["row"])][1],
        )
        for cell in raw_cells
        if int(cell["row"]) not in horizontal_rule_rows
        and int(cell["column"]) not in vertical_rule_columns
    )
    networks = _rule_networks(
        rule_boxes=rule_boxes,
        rule_mask=rule_mask,
        horizontal_coverage=args.horizontal_rule_coverage,
        vertical_coverage=args.vertical_rule_coverage,
    )
    source_segments = _read_jsonl(segments_path)
    visual_rows = _visual_rows(source_segments)
    excluded_y = tuple(
        (network.y_lines[0], network.y_lines[-1]) for network in networks
    )
    flow_rows = _physical_rows(
        rows=rows,
        columns=columns,
        cells_by_row=cells_by_row,
        horizontal_rule_rows=horizontal_rule_rows,
        vertical_rules=vertical_rules,
        visual_rows=visual_rows,
        width=width,
        excluded_y=excluded_y,
    )
    network_rows = _combined_ruled_network_rows(
        networks=networks,
        rule_mask=rule_mask,
        rows=rows,
        occupied_boxes=occupied_boxes,
        width=width,
        vertical_coverage=args.vertical_rule_coverage,
        visual_rows=visual_rows,
    )
    physical_rows = tuple(sorted((*flow_rows, *network_rows), key=lambda row: row.top))
    codes = _encode_physical_rows(physical_rows)
    maximum_segments = max((len(row.slots) for row in physical_rows), default=0)

    draw = ImageDraw.Draw(image)
    for row_index, row in enumerate(physical_rows):
        for column, slot in enumerate(row.slots):
            box = (slot.start, row.top, slot.end, row.bottom)
            _draw_code(
                draw,
                box=box,
                code=codes[row_index][column],
                empty=slot.value is EMPTY,
            )
    for axis, box in rule_boxes:
        color = (35, 45, 235, 255)
        draw.rectangle(box, outline=color, fill=color)

    output_path = output_stage / args.output
    temporary = output_path.with_name(f".{output_path.name}.partial.png")
    image.convert("RGB").save(temporary, format="PNG")
    temporary.replace(output_path)

    serialized_rows = []
    for row_index, row in enumerate(physical_rows):
        row_codes = codes[row_index]
        serialized_rows.append(
            {
                "row": row_index,
                "source_matrix_rows": list(row.source_rows),
                "y": [row.top, row.bottom],
                "segments": [
                    {
                        "column": column,
                        "x": [slot.start, slot.end],
                        "state": "empty" if slot.value is EMPTY else "payload",
                        "code": row_codes[column],
                    }
                    for column, slot in enumerate(row.slots)
                ],
                "compressed_codes": [*row_codes, None],
                "null_tail": {
                    "from_column": len(row.slots),
                    "through_column_exclusive": maximum_segments,
                    "repeat_last_code": row_codes[-1] if row_codes else None,
                },
            }
        )
    _write_json(
        output_stage / args.topology,
        {
            "schema": "physical-sparse-topology-v1",
            "source": {
                "matrix": matrix_path.name,
                "segments": segments_path.name,
                "rules": rules_path.name,
                "rule_mask": rule_mask_path.name,
                "ownership": plain_path.name,
            },
            "codes": {
                "nothing": 0,
                "merge_up": 3,
                "merge_left": 5,
                "empty": 7,
                "merge_both": 8,
                "empty_merge_up": 10,
            },
            "null_semantics": "repeat the last segment code through the logical row tail",
            "rows": serialized_rows,
            "ruled_networks": [
                {
                    "bbox": list(network.bbox),
                    "x_lines": list(network.x_lines),
                    "y_lines": list(network.y_lines),
                }
                for network in networks
            ],
            "stage_execution": "artifact-only; no geometry, Stage 6, OCR, or Stage 5 execution",
        },
    )
    (output_stage / args.methodology).write_text(
        "\n".join(
            (
                "Physical sparse topology methodology",
                "1. Read stored physical row/column intervals, ownership cells, and local rule bboxes.",
                "2. Connect touching finite rule bboxes; also logically unite at least three repeated crossbar-plus-two-rail cells truncated by the same raster edge.",
                "3. Never extend a stored bbox or modify the rule mask; a proven truncating raster edge contributes only its terminal matrix coordinate.",
                "4. For every resulting local 2-D network, recover horizontal and vertical coordinates from its local rule mask.",
                "5. Compose side-by-side concurrent networks in one union-Y planar sweep; never duplicate a page row.",
                "6. Inside each network, begin with rows between horizontal rules; word bbox edges never create rows.",
                "7. Retain Stage 1 visual-row bands outside finite network X extents, even when their Y overlaps.",
                "8. A concurrent boundary may subdivide a table row; copy its cell so the next code is merge-up.",
                "9. In every row, keep only vertical rules physically active at that height (merged cells stay wide).",
                "10. Add page edges around local rules and outside visual rows, preserving every empty region as 7.",
                "11. Mark a table region payload when a non-rule ownership cell intersects its unsplit cell.",
                "12. Outside 2-D networks, retain every Stage 1 segment X track and every finite empty interval between tracks; dense words/glyphs never become segments.",
                "13. Add merge-up=3 only across overlapping regions with the same empty/payload identity.",
                "14. Add merge-left=5 only between adjacent payload regions; empty regions break it.",
                "15. Serialize the final segment once; null repeats its code through the logical row tail.",
                "16. Draw physical rules only inside their stored bbox.",
                "17. Perform no semantic object classification before Stage 6.",
                "",
            )
        ),
        encoding="utf-8",
    )
    valid_codes = frozenset((0, 3, 5, 7, 8, 10))
    row_bounds_valid = all(
        0 <= row.top < row.bottom <= height for row in physical_rows
    )
    rows_do_not_overlap = all(
        first.bottom <= second.top
        for first, second in zip(
            physical_rows,
            physical_rows[1:],
        )
    )
    slots_partition_width = all(
        bool(row.slots)
        and row.slots[0].start == 0
        and row.slots[-1].end == width
        and all(
            first.end == second.start
            for first, second in zip(row.slots, row.slots[1:])
        )
        for row in physical_rows
    )
    codes_match_slots = all(
        len(row.slots) == len(row_codes)
        for row, row_codes in zip(physical_rows, codes, strict=True)
    )
    codes_are_known = all(
        code in valid_codes for row_codes in codes for code in row_codes
    )
    payload_slots = sum(
        slot.value is not EMPTY
        for row in physical_rows
        for slot in row.slots
    )
    payload_presence_matches_source = bool(payload_slots) == bool(
        source_segments
    )
    geometry_manifest_path = stage / "manifest.json"
    geometry_manifest = (
        _read_json(geometry_manifest_path)
        if geometry_manifest_path.is_file()
        else {}
    )
    geometry_is_complete = (
        isinstance(geometry_manifest, dict)
        and geometry_manifest.get("status") == "complete"
        and geometry_manifest.get("limit_leaf_count") == 0
    )
    invariants = {
        "geometry_input_complete": geometry_is_complete,
        "row_bounds_valid": row_bounds_valid,
        "rows_do_not_overlap": rows_do_not_overlap,
        "slots_partition_full_width": slots_partition_width,
        "codes_match_slots": codes_match_slots,
        "codes_are_known": codes_are_known,
        "payload_presence_matches_source": payload_presence_matches_source,
    }
    status = "complete" if all(invariants.values()) else "bad"
    _write_json(
        output_stage / "manifest.json",
        {
            "schema": "debug-topology-stage-v1",
            "stage_name": "physical-sparse-topology",
            "status": status,
            "source": {
                "geometry": str(stage),
                "geometry_status": geometry_manifest.get("status")
                if isinstance(geometry_manifest, dict)
                else None,
                "segments": len(source_segments),
                "matrix_cells": len(raw_cells),
                "rules": len(rules),
            },
            "result": {
                "rows": len(physical_rows),
                "ruled_networks": len(networks),
                "payload_slots": payload_slots,
                "empty_slots": sum(
                    slot.value is EMPTY
                    for row in physical_rows
                    for slot in row.slots
                ),
                "maximum_segments_per_row": maximum_segments,
            },
            "invariants": invariants,
            "manual_review_required": True,
            "visual_evidence": args.output,
            "canonical_topology": args.topology,
        },
    )
    if status != "complete":
        failed = ", ".join(
            name for name, passed in invariants.items() if not passed
        )
        raise RuntimeError(f"topology invariants failed: {failed}")
    print(output_path)
    print(output_stage / args.topology)
    print(
        f"rows={len(physical_rows)} max_segments={maximum_segments} "
        f"segments={sum(len(row.slots) for row in physical_rows)} "
        f"ruled_networks={len(networks)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
