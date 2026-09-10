"""Recursive object partitioning over an already-built numeric topology.

This module does not inspect raster pixels and does not run geometry or OCR.
It consumes the retained Stage 1 recursion tree plus the canonical 0/3/5/7
topology.  Spanning finite empty corridors first divide recursion cameras;
only then are table candidates discovered from mixed-axis cycles inside those
cameras.  A cycle is only the two-dimensional footprint of one topology
owner; it becomes a table only when an independently detected structural
lattice supports it.  All remaining payload is partitioned bottom-up inside
the recursive tree so a tall column separator cannot be crossed by a flat
page-wide proximity pass.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from statistics import median

from app.sparse_pipeline.sparse_topology import (
    compose_sparse_code,
    sparse_code_components,
)

MERGE_UP = 3
MERGE_LEFT = 5

# A document object must occupy a real two-dimensional area.  These limits are
# deliberately about topology, not OCR confidence or visual quality: thinner
# candidates are Stage 1 rule/speck remnants and remain accounted for as
# structural residuals by the artifact writer.
MIN_OBJECT_WIDTH = 10
MIN_OBJECT_HEIGHT = 6

BoxTuple = tuple[int, int, int, int]


@dataclass(frozen=True)
class PartitionSegment:
    segment_id: str
    bbox: BoxTuple
    parent_path: tuple[str, ...]


@dataclass(frozen=True)
class PartitionNode:
    node_id: str
    bbox: BoxTuple
    axis: str | None
    child_ids: tuple[str, ...]
    segment_ids: tuple[str, ...]
    separator_boxes: tuple[BoxTuple, ...] = ()


@dataclass(frozen=True)
class NumericCell:
    bbox: BoxTuple
    code: int
    empty: bool


@dataclass(frozen=True)
class NumericRow:
    row: int
    top: int
    bottom: int
    cells: tuple[NumericCell, ...]


@dataclass(frozen=True)
class RuledNetwork:
    bbox: BoxTuple
    x_lines: tuple[int, ...]
    y_lines: tuple[int, ...]
    evidence: str = "connected-ruled-network"


@dataclass(frozen=True)
class HorizontalRuleTableRegion:
    """Stable row-rule run that can anchor a border-light table."""

    bbox: BoxTuple
    rule_boxes: tuple[BoxTuple, ...]
    cadence: int


@dataclass(frozen=True)
class StructuralRule:
    axis: str
    bbox: BoxTuple

    def __post_init__(self) -> None:
        if self.axis not in ("horizontal", "vertical"):
            raise ValueError("structural rule axis must be horizontal or vertical")
        if self.bbox[0] >= self.bbox[2] or self.bbox[1] >= self.bbox[3]:
            raise ValueError("structural rule bbox must have positive extent")


def horizontal_rule_table_regions(
    *,
    rules: tuple[BoxTuple, ...],
    page_bbox: BoxTuple,
) -> tuple[HorizontalRuleTableRegion, ...]:
    """Find regular near-page-width horizontal rules without raster heuristics."""

    page_width = page_bbox[2] - page_bbox[0]
    if page_width <= 0:
        raise ValueError("page bbox must have positive width")
    ordered = sorted(
        (box for box in rules if box[0] < box[2] and box[1] < box[3] and box[2] - box[0] >= page_width * 0.90),
        key=lambda box: (box[1], box[0]),
    )
    groups: list[list[BoxTuple]] = []
    for box in ordered:
        if (
            not groups
            or box[1] - groups[-1][-1][1] > 96
            or abs(box[0] - groups[-1][-1][0]) > 2
            or abs(box[2] - groups[-1][-1][2]) > 2
        ):
            groups.append([box])
        else:
            groups[-1].append(box)

    regions: list[HorizontalRuleTableRegion] = []
    for group in groups:
        if len(group) < 3:
            continue
        steps = tuple(second[1] - first[1] for first, second in zip(group, group[1:]))
        cadence = sorted(steps)[len(steps) // 2]
        if cadence < 8 or max(steps) - min(steps) > max(3.0, cadence * 0.20):
            continue
        regions.append(
            HorizontalRuleTableRegion(
                bbox=(
                    min(box[0] for box in group),
                    max(page_bbox[1], group[0][1] - cadence),
                    max(box[2] for box in group),
                    min(page_bbox[3], group[-1][3] + cadence),
                ),
                rule_boxes=tuple(group),
                cadence=cadence,
            )
        )
    return tuple(regions)


class PartitionMatrixBasis(str, Enum):
    """Physical source used to materialize an object's local matrix."""

    RULE_LATTICE = "rule-lattice"
    TOPOLOGY_SLICE = "topology-slice"


@dataclass(frozen=True)
class PartitionedObject:
    object_id: str
    kind: str
    bbox: BoxTuple
    matrix_bbox: BoxTuple
    segment_ids: tuple[str, ...]
    evidence: tuple[str, ...]
    matrix_basis: PartitionMatrixBasis


def has_two_dimensional_object_extent(value: PartitionedObject) -> bool:
    """Return whether a partition candidate can be emitted as an object.

    Confirmed tables are always objects because their connected ruled network
    supplies the two-dimensional support.  Flow candidates must have enough
    extent on both axes; a long thin rule, a narrow rule fragment, or a speck
    is geometry to account for, but is not a document object.
    """

    if value.kind == "table" and value.matrix_basis is PartitionMatrixBasis.RULE_LATTICE:
        return True
    if value.kind == "structural-residual":
        return False
    width = value.bbox[2] - value.bbox[0]
    height = value.bbox[3] - value.bbox[1]
    return width >= MIN_OBJECT_WIDTH and height >= MIN_OBJECT_HEIGHT


@dataclass
class _Draft:
    segment_ids: set[str]
    bbox: BoxTuple


@dataclass(frozen=True)
class _ObjectDraft:
    kind: str
    draft: _Draft
    crop_bbox: BoxTuple
    matrix_bbox: BoxTuple
    evidence: tuple[str, ...]
    matrix_basis: PartitionMatrixBasis


@dataclass(frozen=True)
class _FlowRow:
    segment_ids: tuple[str, ...]
    bbox: BoxTuple


@dataclass(frozen=True)
class _EmptyWall:
    bbox: BoxTuple
    bilateral_rows: int


@dataclass(frozen=True)
class _TopologyChamber:
    draft: _Draft
    separator_boxes: tuple[BoxTuple, ...]
    parallel_grid: bool = False


class _DisjointSet:
    def __init__(self, size: int) -> None:
        self.parents = list(range(size))

    def find(self, value: int) -> int:
        while self.parents[value] != value:
            self.parents[value] = self.parents[self.parents[value]]
            value = self.parents[value]
        return value

    def union(self, first: int, second: int) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root != second_root:
            self.parents[second_root] = first_root


def _validate_box(box: BoxTuple, name: str) -> None:
    if (
        type(box) is not tuple
        or len(box) != 4
        or any(type(value) is not int for value in box)
        or box[0] >= box[2]
        or box[1] >= box[3]
    ):
        raise ValueError(f"{name} must be a positive integer box")


def _intersects(first: BoxTuple, second: BoxTuple) -> bool:
    return max(first[0], second[0]) < min(first[2], second[2]) and max(first[1], second[1]) < min(first[3], second[3])


def _union_boxes(boxes: tuple[BoxTuple, ...]) -> BoxTuple:
    if not boxes:
        raise ValueError("cannot unite an empty box sequence")
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _upper_quartile(values: tuple[int, ...]) -> float:
    if not values:
        return 0.0
    ordered = tuple(sorted(values))
    return float(ordered[round(0.75 * (len(ordered) - 1))])


def _code_has(code: int, component: int) -> bool:
    return component in sparse_code_components(code)


def _mixed_axis_cycle_components(
    rows: tuple[NumericRow, ...],
) -> tuple[BoxTuple, ...]:
    """Return maximal payload regions containing a finite mixed-axis cycle.

    ``0 5 / 3 8`` is only the spelling at one normalized upper-left corner.
    The invariant is an undirected finite cycle containing both horizontal and
    vertical merge edges.  This also accepts a merged cell represented by a
    three-vertex orthogonal cycle.  Build the graph in physical coordinates,
    find cyclic connected components independently of the proposed crop
    corner, then expand through every observed 3/5/8 edge.  Explicit empty
    cells and unobserved null tails contribute no payload edge.
    """

    payload: list[tuple[int, int, NumericCell]] = []
    index_by_position: dict[tuple[int, int], int] = {}
    for row_index, row in enumerate(rows):
        for cell_index, cell in enumerate(row.cells):
            if cell.empty:
                continue
            index_by_position[(row_index, cell_index)] = len(payload)
            payload.append((row_index, cell_index, cell))
    if not payload:
        return ()

    disjoint = _DisjointSet(len(payload))
    invalid_edges: set[int] = set()
    graph_edges: dict[frozenset[int], str] = {}

    # Resolve every directional 5 first.  A merge bit without a finite
    # neighbour is not a new local origin: it is an incomplete crop.  In
    # particular, raw ``0 5 / 8 8`` at a left edge must be expanded left or
    # rejected; silently reading the lower-left 8 as 3 is the old bug.
    for row_index, row in enumerate(rows):
        for cell_index, cell in enumerate(row.cells):
            current = index_by_position.get((row_index, cell_index))
            if current is None or not _code_has(cell.code, MERGE_LEFT):
                continue
            previous = index_by_position.get((row_index, cell_index - 1))
            if cell_index == 0 or previous is None or row.cells[cell_index - 1].bbox[2] != cell.bbox[0]:
                invalid_edges.add(current)
                continue
            disjoint.union(previous, current)
            graph_edges[frozenset((previous, current))] = "horizontal"

    for row_index, row in enumerate(rows):
        for cell_index, cell in enumerate(row.cells):
            current = index_by_position.get((row_index, cell_index))
            if current is None or not _code_has(cell.code, MERGE_UP):
                continue
            if row_index == 0 or rows[row_index - 1].bottom != row.top:
                invalid_edges.add(current)
                continue
            upper_candidates = tuple(
                index_by_position[(row_index - 1, upper_index)]
                for upper_index, upper_cell in enumerate(rows[row_index - 1].cells)
                if (row_index - 1, upper_index) in index_by_position
                and max(upper_cell.bbox[0], cell.bbox[0]) < min(upper_cell.bbox[2], cell.bbox[2])
            )
            if not upper_candidates:
                invalid_edges.add(current)
                continue
            # One 3 bit proves one source continuation, not that a wide cell
            # owns every overlapping cell above it.  Multiple parents are
            # safe only when their finite horizontal 5 edges already prove
            # that they are one region.
            parent_roots = {disjoint.find(value) for value in upper_candidates}
            if len(parent_roots) != 1:
                invalid_edges.add(current)
                continue
            for upper in upper_candidates:
                disjoint.union(upper, current)
                graph_edges[frozenset((upper, current))] = "vertical"

    invalid_roots = {disjoint.find(value) for value in invalid_edges}
    nodes_by_root: dict[int, set[int]] = {}
    for index in range(len(payload)):
        nodes_by_root.setdefault(disjoint.find(index), set()).add(index)
    edges_by_root: dict[int, list[tuple[frozenset[int], str]]] = {}
    for edge, axis in graph_edges.items():
        first = next(iter(edge))
        edges_by_root.setdefault(disjoint.find(first), []).append((edge, axis))
    cycle_roots = set()
    for root, members in nodes_by_root.items():
        component_edges = edges_by_root.get(root, [])
        axes = {axis for _, axis in component_edges}
        # A connected simple undirected graph contains a cycle exactly when
        # E >= V.  A purely horizontal or vertical cycle is impossible in the
        # physical interval graph, but keep the axis check explicit because it
        # is the two-dimensional topology invariant promised downstream.  A
        # filled illustration can have this same footprint, so classification
        # still requires an independent structural lattice.
        if root not in invalid_roots and len(component_edges) >= len(members) and axes == {"horizontal", "vertical"}:
            cycle_roots.add(root)
    boxes_by_root: dict[int, list[BoxTuple]] = {}
    for index, (_, _, cell) in enumerate(payload):
        root = disjoint.find(index)
        if root in cycle_roots:
            boxes_by_root.setdefault(root, []).append(cell.bbox)
    return tuple(
        sorted(
            (_union_boxes(tuple(boxes)) for boxes in boxes_by_root.values()),
            key=lambda value: (value[1], value[0], value[3], value[2]),
        )
    )


def _payload_row_count(
    rows: tuple[NumericRow, ...],
    *,
    bbox: BoxTuple,
) -> int:
    return sum(
        any(not cell.empty and _intersects(cell.bbox, bbox) for cell in row.cells)
        for row in rows
        if max(row.top, bbox[1]) < min(row.bottom, bbox[3])
    )


def _spanning_empty_corridors(
    *,
    node: PartitionNode,
    rows: tuple[NumericRow, ...],
    active_segments: tuple[PartitionSegment, ...],
) -> tuple[_EmptyWall, ...]:
    """Return finite vertical 7/10 corridors spanning one recursion camera.

    The test is structural rather than pixel-scaled.  A candidate X slab must
    be explicitly observed as empty, must never carry payload through the
    node's full Y scope, and must leave payload in at least two observed row
    bands on each side.  Thus page margins and one-row inter-word gaps are not
    object cuts, while a persistent sidebar/content separator is.  This step
    intentionally knows nothing about table types.
    """

    left, top, right, bottom = node.bbox
    relevant_rows = tuple(row for row in rows if max(row.top, top) < min(row.bottom, bottom))
    if not relevant_rows:
        return ()
    boundaries = {left, right}
    for row in relevant_rows:
        for cell in row.cells:
            if cell.bbox[2] <= left or cell.bbox[0] >= right:
                continue
            boundaries.update((max(left, cell.bbox[0]), min(right, cell.bbox[2])))
    ordered = tuple(sorted(boundaries))
    empty_slabs: list[tuple[int, int]] = []
    for slab_left, slab_right in zip(ordered, ordered[1:]):
        if slab_left >= slab_right:
            continue
        slab = (slab_left, top, slab_right, bottom)
        if any(_intersects(segment.bbox, slab) for segment in active_segments):
            continue
        intersecting_cells = tuple(cell for row in relevant_rows for cell in row.cells if _intersects(cell.bbox, slab))
        if not intersecting_cells or any(not cell.empty for cell in intersecting_cells):
            continue
        if empty_slabs and empty_slabs[-1][1] == slab_left:
            empty_slabs[-1] = (empty_slabs[-1][0], slab_right)
        else:
            empty_slabs.append((slab_left, slab_right))

    result: list[_EmptyWall] = []
    for wall_left, wall_right in empty_slabs:
        left_scope = (left, top, wall_left, bottom)
        right_scope = (wall_right, top, right, bottom)
        if left_scope[0] >= left_scope[2] or right_scope[0] >= right_scope[2]:
            continue
        left_rows = _payload_row_count(rows, bbox=left_scope)
        right_rows = _payload_row_count(rows, bbox=right_scope)
        if min(left_rows, right_rows) < 2:
            continue
        result.append(
            _EmptyWall(
                bbox=(wall_left, top, wall_right, bottom),
                bilateral_rows=min(left_rows, right_rows),
            )
        )
    return tuple(result)


def _table_upper_left_anchor_pair(
    rows: tuple[NumericRow, ...],
    network: RuledNetwork,
) -> tuple[int, int] | None:
    """Return ``(anchor top, row boundary)`` for local ``0 5 / 3 8``.

    Stored page codes cannot be reused here: payload immediately to the left
    of a cropped network makes its first cell ``5`` even though that same cell
    is locally ``0``.  Re-encode the physical slots after cropping to the
    network, then inspect consecutive row pairs.  This is the same isolation
    rule used when the final object-local matrix is written.
    """

    left = network.x_lines[0]
    right = network.x_lines[-1]
    top = network.y_lines[0]
    bottom = network.y_lines[-1]
    cells_by_band: dict[tuple[int, int], list[NumericCell]] = {}
    for row in rows:
        if row.top < top or row.bottom > bottom:
            continue
        cells_by_band.setdefault((row.top, row.bottom), []).extend(row.cells)
    local_rows: list[tuple[int, int, tuple[int, ...]]] = []
    for (row_top, row_bottom), cells in sorted(cells_by_band.items()):
        codes: list[int] = []
        for column, (slot_left, slot_right) in enumerate(zip(network.x_lines, network.x_lines[1:])):
            if not left <= slot_left < slot_right <= right:
                continue
            overlapping = tuple(cell for cell in cells if max(slot_left, cell.bbox[0]) < min(slot_right, cell.bbox[2]))
            if not overlapping:
                continue
            payload = tuple(cell for cell in overlapping if not cell.empty)
            source = payload or overlapping
            source_components = frozenset(
                component for cell in source for component in sparse_code_components(cell.code)
            )
            codes.append(
                compose_sparse_code(
                    merge_up=MERGE_UP in source_components,
                    merge_left=(column > 0 and MERGE_LEFT in source_components),
                    empty=not payload,
                )
            )
        if codes:
            local_rows.append((row_top, row_bottom, tuple(codes)))
    for index, (first_row, second_row) in enumerate(zip(local_rows, local_rows[1:])):
        if local_rows[index][1] != local_rows[index + 1][0]:
            continue
        first = tuple(code - MERGE_UP if _code_has(code, MERGE_UP) else code for code in first_row[2])
        second = second_row[2]
        if (
            len(first) >= 2
            and len(second) >= 2
            and first[0] == 0
            and _code_has(first[1], MERGE_LEFT)
            and _code_has(second[0], MERGE_UP)
            and _code_has(second[1], MERGE_UP)
            and _code_has(second[1], MERGE_LEFT)
        ):
            return local_rows[index][0], local_rows[index][1]
    return None


def _table_upper_left_anchor(
    rows: tuple[NumericRow, ...],
    network: RuledNetwork,
) -> int | None:
    pair = _table_upper_left_anchor_pair(rows, network)
    return None if pair is None else pair[0]


def table_upper_left_boundary(
    rows: tuple[NumericRow, ...],
    network: RuledNetwork,
) -> int | None:
    """Return the proven boundary between a table's first two local rows."""

    pair = _table_upper_left_anchor_pair(rows, network)
    return None if pair is None else pair[1]


def table_upper_left_witness(
    rows: tuple[NumericRow, ...],
    network: RuledNetwork,
) -> bool:
    """Recognize a table from its first object-local ``0 5 / 3 8`` pair."""

    return _table_upper_left_anchor(rows, network) is not None


def _center_inside(box: BoxTuple, container: BoxTuple) -> bool:
    center_x = (box[0] + box[2]) / 2.0
    center_y = (box[1] + box[3]) / 2.0
    return container[0] <= center_x < container[2] and container[1] <= center_y < container[3]


def _row_link(first: _Draft, second: _Draft, typical_height: float) -> bool:
    upper, lower = sorted((first.bbox, second.bbox), key=lambda box: box[1])
    vertical_gap = max(0, lower[1] - upper[3])
    scale = max(4.0, typical_height)
    if vertical_gap > max(12.0, scale * 2.0):
        return False
    overlap = min(upper[2], lower[2]) - max(upper[0], lower[0])
    aligned_left = abs(upper[0] - lower[0]) <= max(4.0, scale * 1.25)
    return overlap > 0 or aligned_left


def _column_link(first: _Draft, second: _Draft, typical_height: float) -> bool:
    left, right = sorted((first.bbox, second.bbox), key=lambda box: box[0])
    vertical_overlap = min(left[3], right[3]) - max(left[1], right[1])
    horizontal_gap = max(0, right[0] - left[2])
    return vertical_overlap > 0 and horizontal_gap <= max(12.0, typical_height * 2.0)


def _has_empty_row_separator(
    node: PartitionNode,
    rows: tuple[NumericRow, ...],
    *,
    typical_height: float,
    active_segments: tuple[PartitionSegment, ...] = (),
) -> bool:
    """Confirm a recursive row cut by an explicit local 7/10-only band."""

    if node.axis != "rows":
        return False
    topology_heights = tuple(
        row.bottom - row.top
        for row in rows
        if row.bottom - row.top >= 3
        and _intersects(
            (node.bbox[0], row.top, node.bbox[2], row.bottom),
            node.bbox,
        )
        and any(not cell.empty for cell in row.cells)
    )
    local_scale = float(median(topology_heights)) if topology_heights else typical_height
    for separator in node.separator_boxes:
        separator_height = separator[3] - separator[1]
        if separator_height < max(8.0, local_scale):
            continue

        # Recursive seams are stored with enough context to include the last
        # edge pixels of the row above and the first edge pixels of the row
        # below.  Canonical topology bands consequently can graze either edge
        # by one or two pixels even though the separator has a genuinely blank
        # interior.  Testing the complete contextual box turns that harmless
        # contact into a bridge (a wide header can then transitively join two
        # independent body columns).  Only payload reaching the central seam
        # can disprove the recursive boundary.
        edge_inset = max(
            1,
            min(
                separator_height // 4,
                max(1, round(local_scale * 0.25)),
            ),
        )
        separator_core = (
            separator[0],
            separator[1] + edge_inset,
            separator[2],
            separator[3] - edge_inset,
        )
        if separator_core[1] >= separator_core[3]:
            continue
        if active_segments:
            # Numeric rows are deliberately sparse bands: a line touching the
            # last pixel of a band marks that complete band as payload.  They
            # must not be stretched back across a finer recursive whitespace
            # seam.  Exact retained segment boxes decide whether real payload
            # reaches the separator core; topology rows remain the fallback
            # for callers that do not carry segment geometry.
            if not any(_intersects(segment.bbox, separator_core) for segment in active_segments):
                return True
            continue
        candidates = tuple(row for row in rows if max(row.top, separator_core[1]) < min(row.bottom, separator_core[3]))
        # A retained recursive separator is itself blank-seam evidence.  The
        # canonical sparse projection may omit that physical band entirely,
        # so the absence of a materialized topology row must not erase the
        # recursive object boundary.  If rows do cover the band, keep the
        # stricter check and reject it when any payload crosses the separator.
        if not candidates or all(
            not any(
                not cell.empty and max(cell.bbox[0], separator_core[0]) < min(cell.bbox[2], separator_core[2])
                for cell in row.cells
            )
            for row in candidates
        ):
            return True
    return False


def _merge_drafts(
    drafts: list[_Draft],
    pairs: tuple[tuple[int, int], ...],
) -> list[_Draft]:
    disjoint = _DisjointSet(len(drafts))
    for first, second in pairs:
        disjoint.union(first, second)
    groups: dict[int, list[_Draft]] = {}
    for index, draft in enumerate(drafts):
        groups.setdefault(disjoint.find(index), []).append(draft)
    return [
        _Draft(
            segment_ids={segment_id for member in members for segment_id in member.segment_ids},
            bbox=_union_boxes(tuple(member.bbox for member in members)),
        )
        for members in groups.values()
    ]


def _flow_body_height(
    draft: _Draft,
    segment_by_id: dict[str, PartitionSegment],
) -> float:
    heights = tuple(
        segment_by_id[value].bbox[3] - segment_by_id[value].bbox[1]
        for value in draft.segment_ids
        if segment_by_id[value].bbox[3] - segment_by_id[value].bbox[1] >= 3
        and segment_by_id[value].bbox[2] - segment_by_id[value].bbox[0] >= 4
    )
    return float(median(heights)) if heights else 4.0


def _is_flow_fringe(box: BoxTuple, body_height: float) -> bool:
    width = box[2] - box[0]
    height = box[3] - box[1]
    return (width <= max(3.0, body_height * 0.20) and height >= body_height * 1.5) or (
        width <= max(3.0, body_height * 0.25) and height <= max(2.0, body_height * 0.18)
    )


def _same_flow_row(
    members: list[PartitionSegment],
    candidate: PartitionSegment,
) -> bool:
    center = float(median((item.bbox[1] + item.bbox[3]) / 2.0 for item in members))
    height = float(median(item.bbox[3] - item.bbox[1] for item in members))
    candidate_center = (candidate.bbox[1] + candidate.bbox[3]) / 2.0
    candidate_height = float(candidate.bbox[3] - candidate.bbox[1])
    smaller = min(height, candidate_height)
    larger = max(height, candidate_height)
    scale = smaller if larger > 3.0 * smaller else larger
    return abs(center - candidate_center) <= max(2.0, 0.65 * scale)


def _visual_flow_rows(
    draft: _Draft,
    segment_by_id: dict[str, PartitionSegment],
) -> tuple[tuple[_FlowRow, ...], tuple[str, ...], float]:
    body_height = _flow_body_height(draft, segment_by_id)
    ordered = tuple(
        sorted(
            (segment_by_id[value] for value in draft.segment_ids),
            key=lambda item: (
                item.bbox[1],
                item.bbox[0],
                item.bbox[3],
                item.segment_id,
            ),
        )
    )
    primary = tuple(item for item in ordered if not _is_flow_fringe(item.bbox, body_height))
    fringe = tuple(item.segment_id for item in ordered if _is_flow_fringe(item.bbox, body_height))
    if not primary:
        return (), fringe, body_height
    groups: list[list[PartitionSegment]] = []
    for segment in primary:
        if groups and _same_flow_row(groups[-1], segment):
            groups[-1].append(segment)
        else:
            groups.append([segment])
    rows = tuple(
        _FlowRow(
            segment_ids=tuple(
                item.segment_id
                for item in sorted(
                    members,
                    key=lambda item: (item.bbox[0], item.segment_id),
                )
            ),
            bbox=_union_boxes(tuple(item.bbox for item in members)),
        )
        for members in groups
    )
    return rows, fringe, body_height


def _marker_list_span(
    rows: tuple[_FlowRow, ...],
    segment_by_id: dict[str, PartitionSegment],
    body_height: float,
) -> tuple[int, int] | None:
    witnesses: list[tuple[int, BoxTuple, BoxTuple]] = []
    for index, row in enumerate(rows):
        members = tuple(segment_by_id[value] for value in row.segment_ids)
        if len(members) < 2:
            continue
        marker = members[0].bbox
        body = _union_boxes(tuple(item.bbox for item in members[1:]))
        marker_width = marker[2] - marker[0]
        body_width = body[2] - body[0]
        local_scale = float(median(item.bbox[3] - item.bbox[1] for item in members))
        if (
            marker_width <= max(2.0 * local_scale, body_width * 0.35)
            and body_width >= 2.0 * marker_width
            and marker[2] < body[0]
        ):
            witnesses.append((index, marker, body))
    if len(witnesses) < 2:
        return None
    marker_left = float(median(value[1][0] for value in witnesses))
    body_left = float(median(value[2][0] for value in witnesses))
    tolerance = max(4.0, body_height * 0.35)
    stable = tuple(
        value
        for value in witnesses
        if abs(value[1][0] - marker_left) <= tolerance and abs(value[2][0] - body_left) <= tolerance
    )
    if len(stable) < 2:
        return None
    return min(value[0] for value in stable), max(value[0] for value in stable) + 1


def _split_flow_rows(
    rows: tuple[_FlowRow, ...],
    body_height: float,
) -> tuple[tuple[_FlowRow, ...], ...]:
    if len(rows) < 2:
        return (rows,) if rows else ()
    gaps = tuple(max(0, current.bbox[1] - previous.bbox[3]) for previous, current in zip(rows, rows[1:]))
    positive = tuple(sorted(value for value in gaps if value > 0))
    if positive:
        lower = positive[: max(1, (len(positive) + 1) // 2)]
        ordinary_gap = float(median(lower))
    else:
        ordinary_gap = 0.0
    threshold = max(
        body_height * 0.35,
        ordinary_gap * 1.5,
        ordinary_gap + 4.0,
    )
    result: list[list[_FlowRow]] = [[]]
    for index, row in enumerate(rows):
        if index and gaps[index - 1] > threshold:
            result.append([])
        result[-1].append(row)
    return tuple(tuple(group) for group in result if group)


def _indented_list(
    rows: tuple[_FlowRow, ...],
    body_height: float,
) -> bool:
    if len(rows) < 4:
        return False
    lefts = tuple(row.bbox[0] for row in rows)
    outer = min(lefts)
    tolerance = max(4.0, body_height * 0.20)
    indentation = max(5.0, body_height * 0.25)
    inner = tuple(value for value in lefts if value >= outer + indentation)
    if len(inner) < 3 or len(inner) * 2 < len(rows):
        return False
    inner_track = float(median(inner))
    return abs(lefts[0] - outer) <= tolerance and max(abs(value - inner_track) for value in inner) <= tolerance


def _mixed_marker_body_list(
    rows: tuple[_FlowRow, ...],
    body_height: float,
) -> bool:
    """Recognize lists whose markers are fused into only some row segments.

    Recursive geometry intentionally stops once a leaf reaches one text-line
    cadence.  Depending on raster connectivity, a checkbox and its label can
    therefore be one row segment while another row starts at the label's
    inner track.  The alternating pair of stable left tracks is still direct
    geometry evidence, provided multiple outer rows extend through the inner
    track (combined marker+body) and no third track is present.
    """

    if len(rows) < 5:
        return False
    tolerance = max(4.0, body_height * 0.20)
    minimum_indent = max(5.0, body_height * 0.75)
    outer_track = float(min(row.bbox[0] for row in rows))
    inner_values = tuple(row.bbox[0] for row in rows if row.bbox[0] >= outer_track + minimum_indent)
    if len(inner_values) < 2:
        return False
    inner_track = float(median(inner_values))
    if inner_track - outer_track < minimum_indent:
        return False

    def track(row: _FlowRow) -> str | None:
        left = float(row.bbox[0])
        if abs(left - outer_track) <= tolerance:
            return "outer"
        if abs(left - inner_track) <= tolerance:
            return "inner"
        return None

    tracks = tuple(track(row) for row in rows)
    if tracks[0] != "outer" or any(value is None for value in tracks):
        return False
    item_tracks = tracks[1:]
    inner_count = item_tracks.count("inner")
    crossing = max(4.0, body_height * 0.50)
    combined_outer_count = sum(
        value == "outer" and row.bbox[2] >= inner_track + crossing
        for row, value in zip(rows[1:], item_tracks, strict=True)
    )
    transitions = sum(first != second for first, second in zip(item_tracks, item_tracks[1:]))
    return inner_count >= 2 and combined_outer_count >= 2 and transitions >= 2


def _geometry_list_witness(
    rows: tuple[_FlowRow, ...],
    body_height: float,
) -> bool:
    return _indented_list(rows, body_height) or _mixed_marker_body_list(
        rows,
        body_height,
    )


def _draft_from_rows(
    rows: tuple[_FlowRow, ...],
    segment_by_id: dict[str, PartitionSegment],
) -> _Draft:
    segment_ids = {segment_id for row in rows for segment_id in row.segment_ids}
    return _Draft(
        segment_ids=segment_ids,
        bbox=_union_boxes(tuple(segment_by_id[value].bbox for value in segment_ids)),
    )


def _semantic_flow_parts(
    draft: _Draft,
    segment_by_id: dict[str, PartitionSegment],
    page_bbox: BoxTuple,
) -> list[tuple[str, _Draft, tuple[str, ...]]]:
    if len(draft.segment_ids) == 1:
        only = segment_by_id[next(iter(draft.segment_ids))].bbox
        page_width = page_bbox[2] - page_bbox[0]
        page_height = page_bbox[3] - page_bbox[1]
        width = only[2] - only[0]
        height = only[3] - only[1]
        touches_horizontal_edge = only[1] == page_bbox[1] or only[3] == page_bbox[3]
        if (
            touches_horizontal_edge
            and width >= page_width * 0.80
            and height <= page_height * 0.08
            and width >= 12 * height
        ):
            return [
                (
                    "structural-residual",
                    draft,
                    ("wide-thin-page-edge-fringe",),
                )
            ]
    rows, fringe_ids, body_height = _visual_flow_rows(draft, segment_by_id)
    if not rows:
        if len(draft.segment_ids) == 1:
            return [
                (
                    "paragraph",
                    draft,
                    ("single-segment-paragraph",),
                )
            ]
        return [("flow", draft, ("recursive-local-connectivity",))]

    # A dewarped page edge can be broken into several finite vertical strips.
    # They are exactly accounted geometry, but attaching them to the nearest
    # paragraph expands its crop to x=0 and writes bogus narrow 3/7 segments
    # into the object-local matrix.  Only tall narrow fringe already rejected
    # as a visual row and physically touching a vertical raster edge is
    # separated here; ordinary bullets and glyphs remain with their flow.
    structural_fringe_ids = tuple(
        segment_id
        for segment_id in fringe_ids
        if (segment_by_id[segment_id].bbox[0] == page_bbox[0] or segment_by_id[segment_id].bbox[2] == page_bbox[2])
        and (segment_by_id[segment_id].bbox[3] - segment_by_id[segment_id].bbox[1] >= body_height * 1.5)
    )
    attachable_fringe_ids = tuple(segment_id for segment_id in fringe_ids if segment_id not in structural_fringe_ids)

    marker_span = _marker_list_span(rows, segment_by_id, body_height)
    groups: list[tuple[str | None, tuple[_FlowRow, ...]]] = []
    if marker_span is None and _geometry_list_witness(rows, body_height):
        # A section title followed by repeated indented rows is one list even
        # when the title gap is slightly larger than the item line spacing.
        # Split-after-classify would otherwise strand the title as a separate
        # one-line object (the Amazon sidebar regression).
        groups.append(("list", rows))
    elif marker_span is None:
        groups.extend((None, value) for value in _split_flow_rows(rows, body_height))
    else:
        start, stop = marker_span
        groups.extend((None, value) for value in _split_flow_rows(rows[:start], body_height))
        groups.append(("list", rows[start:stop]))
        groups.extend((None, value) for value in _split_flow_rows(rows[stop:], body_height))

    parts: list[tuple[str, _Draft, tuple[str, ...]]] = []
    for forced_kind, group in groups:
        if forced_kind == "list" or _geometry_list_witness(group, body_height):
            kind = "list"
            evidence = (
                "repeated-marker-body-rows" if forced_kind == "list" else "repeated-indented-rows",
                "geometry-only-visual-rows",
            )
        elif len(group) >= 2:
            kind = "paragraph"
            evidence = (
                "connected-multiline-flow",
                "geometry-only-visual-rows",
            )
        else:
            kind = "paragraph"
            evidence = (
                "single-visual-row-paragraph",
                "geometry-only-visual-rows",
            )
        parts.append((kind, _draft_from_rows(group, segment_by_id), evidence))

    # Narrow edge remnants and tiny isolated specks stay exactly accounted
    # for, but they cannot define rows or object type.  Attach each to the
    # vertically closest semantic part produced by the substantive geometry.
    for segment_id in attachable_fringe_ids:
        segment = segment_by_id[segment_id]
        selected = max(
            range(len(parts)),
            key=lambda index: (
                max(
                    0,
                    min(segment.bbox[3], parts[index][1].bbox[3]) - max(segment.bbox[1], parts[index][1].bbox[1]),
                ),
                -abs((segment.bbox[1] + segment.bbox[3]) - (parts[index][1].bbox[1] + parts[index][1].bbox[3])),
            ),
        )
        kind, member, evidence = parts[selected]
        member.segment_ids.add(segment_id)
        member.bbox = _union_boxes(tuple(segment_by_id[value].bbox for value in member.segment_ids))
        parts[selected] = (kind, member, evidence)
    parts.extend(
        (
            "structural-residual",
            _Draft(
                segment_ids={segment_id},
                bbox=segment_by_id[segment_id].bbox,
            ),
            ("tall-narrow-page-edge-fringe",),
        )
        for segment_id in structural_fringe_ids
    )
    return parts


def _merge_indented_flow_sections(
    drafts: list[_Draft],
    segment_by_id: dict[str, PartitionSegment],
) -> list[_Draft]:
    """Rejoin a one-line section title with its indented row stack.

    Recursive whitespace is allowed to separate objects, so a slightly larger
    title-to-first-item gap may produce two drafts.  Rejoining is available
    only when the combined geometry independently proves the repeated-indent
    list pattern; ordinary neighboring paragraphs therefore stay separate.
    """

    ordered = sorted(drafts, key=lambda item: (item.bbox[1], item.bbox[0]))
    result: list[_Draft] = []
    index = 0
    while index < len(ordered):
        first = ordered[index]
        if index + 1 >= len(ordered):
            result.append(first)
            break
        second = ordered[index + 1]
        first_rows, _, first_height = _visual_flow_rows(first, segment_by_id)
        combined = _Draft(
            segment_ids=set(first.segment_ids | second.segment_ids),
            bbox=_union_boxes((first.bbox, second.bbox)),
        )
        combined_rows, _, combined_height = _visual_flow_rows(
            combined,
            segment_by_id,
        )
        vertical_gap = max(0, second.bbox[1] - first.bbox[3])
        horizontal_overlap = min(first.bbox[2], second.bbox[2]) - max(first.bbox[0], second.bbox[0])
        if (
            len(first_rows) == 1
            and len(combined_rows) >= 4
            and vertical_gap <= max(16.0, 2.0 * first_height)
            and horizontal_overlap > 0
            and _geometry_list_witness(combined_rows, combined_height)
        ):
            result.append(combined)
            index += 2
        else:
            result.append(first)
            index += 1
    return result


def _rule_network_for_component(
    bbox: BoxTuple,
    networks: tuple[RuledNetwork, ...],
) -> RuledNetwork | None:
    """Return optional physical lattice evidence for a numeric component."""

    matches = tuple(
        network
        for network in networks
        if network.x_lines[0] == bbox[0]
        and network.x_lines[-1] == bbox[2]
        and network.y_lines[0] <= bbox[1] < bbox[3]
        and bbox[3] <= network.y_lines[-1]
        and bbox[1] in network.y_lines
        and bbox[3] in network.y_lines
    )
    return matches[0] if len(matches) == 1 else None


def _parallel_grid_witness(
    groups: tuple[set[str], ...],
    segment_by_id: dict[str, PartitionSegment],
) -> bool:
    """Return whether corridor-separated lanes form one aligned row grid."""

    if len(groups) < 2:
        return False
    lane_rows = tuple(
        _visual_flow_rows(
            _Draft(
                segment_ids=group,
                bbox=_union_boxes(tuple(segment_by_id[value].bbox for value in group)),
            ),
            segment_by_id,
        )[0]
        for group in groups
    )
    if any(len(rows) < 2 for rows in lane_rows):
        return False
    row_counts = {len(rows) for rows in lane_rows}
    if len(row_counts) == 1:
        row_count = next(iter(row_counts))
        if all(
            max(rows[row_index].bbox[1] for rows in lane_rows) < min(rows[row_index].bbox[3] for rows in lane_rows)
            for row_index in range(row_count)
        ):
            return True

    # Empty and merged table cells legitimately remove visual rows from one
    # lane. Requiring every lane to have the exact same row count turns the
    # resulting column whitespace into object cuts. Keep a ragged grid only
    # when three or more lanes form one connected graph of repeated vertical
    # row alignments; ordinary two-column prose still uses the strict witness.
    if len(lane_rows) < 3:
        return False

    def aligned_matches(
        first: tuple[_FlowRow, ...],
        second: tuple[_FlowRow, ...],
    ) -> int:
        first_index = 0
        second_index = 0
        matches = 0
        while first_index < len(first) and second_index < len(second):
            first_box = first[first_index].bbox
            second_box = second[second_index].bbox
            if max(first_box[1], second_box[1]) < min(first_box[3], second_box[3]):
                matches += 1
                first_index += 1
                second_index += 1
            elif first_box[3] <= second_box[1]:
                first_index += 1
            else:
                second_index += 1
        return matches

    links = {index: set() for index in range(len(lane_rows))}
    for first_index, first in enumerate(lane_rows):
        for second_index in range(first_index + 1, len(lane_rows)):
            second = lane_rows[second_index]
            matches = aligned_matches(first, second)
            minimum = max(3, (min(len(first), len(second)) + 1) // 2)
            if matches < minimum:
                continue
            links[first_index].add(second_index)
            links[second_index].add(first_index)
    reached = {0}
    pending = [0]
    while pending:
        current = pending.pop()
        for neighbor in links[current] - reached:
            reached.add(neighbor)
            pending.append(neighbor)
    return len(reached) == len(lane_rows)


def _stable_topology_table_witness(
    candidate: _ObjectDraft,
    rows: tuple[NumericRow, ...],
) -> tuple[tuple[tuple[int, int], ...], int] | None:
    """Return strong repeated-column evidence for an edge-open table.

    A physical table touching a raster edge can retain all of its horizontal
    and vertical topology while failing the closed rule-network proof.  Do not
    weaken that primary proof.  Promote only a tall paragraph whose local
    topology repeats the exact same two-or-more non-empty X intervals across
    at least five physical rows.
    """

    if candidate.kind not in {"paragraph", "list"} or candidate.matrix_basis is not PartitionMatrixBasis.TOPOLOGY_SLICE:
        return None
    left, top, right, bottom = candidate.matrix_bbox
    if bottom - top < 100:
        return None
    signatures: dict[tuple[tuple[int, int], ...], int] = {}
    for row in rows:
        if max(row.top, top) >= min(row.bottom, bottom):
            continue
        signature = tuple(
            (max(cell.bbox[0], left), min(cell.bbox[2], right))
            for cell in row.cells
            if not cell.empty and max(cell.bbox[0], left) < min(cell.bbox[2], right)
        )
        if len(signature) < 3:
            continue
        signatures[signature] = signatures.get(signature, 0) + 1
    if not signatures:
        return None
    signature, repetitions = max(
        signatures.items(),
        key=lambda item: (item[1], item[0]),
    )
    if repetitions < 5:
        return None
    return signature, repetitions


def _aligned_table_fragment_pair(
    first: _ObjectDraft,
    second: _ObjectDraft,
    segment_by_id: dict[str, PartitionSegment],
) -> bool:
    """Return whether two corridor siblings are lanes of one sparse table."""

    if (
        (first.kind != "table" and second.kind != "table")
        or first.matrix_basis is not PartitionMatrixBasis.TOPOLOGY_SLICE
        or second.matrix_basis is not PartitionMatrixBasis.TOPOLOGY_SLICE
    ):
        return False
    if first.kind != "table" or second.kind != "table":
        table_anchor = first if first.kind == "table" else second
        column_counts = tuple(
            int(value.partition("=")[2]) for value in table_anchor.evidence if value.startswith("column-count=")
        )
        if not column_counts or max(column_counts) < 3:
            return False
    first_corridors = {value for value in first.evidence if value.startswith("empty-corridor=")}
    second_corridors = {value for value in second.evidence if value.startswith("empty-corridor=")}
    if not first_corridors.intersection(second_corridors):
        return False
    first_box = first.draft.bbox
    second_box = second.draft.bbox
    if not (first_box[2] <= second_box[0] or second_box[2] <= first_box[0]):
        return False
    overlap = min(first_box[3], second_box[3]) - max(first_box[1], second_box[1])
    shorter_height = min(
        first_box[3] - first_box[1],
        second_box[3] - second_box[1],
    )
    if overlap <= 0 or overlap * 2 < shorter_height:
        return False

    lane_rows = []
    for candidate in (first, second):
        rows, _, _ = _visual_flow_rows(candidate.draft, segment_by_id)
        lane_rows.append(rows)
    first_rows, second_rows = lane_rows
    shorter_count = min(len(first_rows), len(second_rows))
    if shorter_count < 3:
        return False
    used: set[int] = set()
    matches = 0
    for first_row in first_rows:
        candidates = tuple(
            (
                min(first_row.bbox[3], second_row.bbox[3]) - max(first_row.bbox[1], second_row.bbox[1]),
                index,
            )
            for index, second_row in enumerate(second_rows)
            if index not in used
            and max(first_row.bbox[1], second_row.bbox[1]) < min(first_row.bbox[3], second_row.bbox[3])
        )
        if not candidates:
            continue
        _, selected = max(candidates)
        used.add(selected)
        matches += 1
    return matches >= max(3, (shorter_count + 1) // 2)


def _merge_aligned_topology_table_fragments(
    objects: list[_ObjectDraft],
    *,
    segment_by_id: dict[str, PartitionSegment],
) -> list[_ObjectDraft]:
    candidate_indices = tuple(
        index
        for index, value in enumerate(objects)
        if value.matrix_basis is PartitionMatrixBasis.TOPOLOGY_SLICE
        and any(evidence.startswith("empty-corridor=") for evidence in value.evidence)
    )
    adjacency: dict[int, set[int]] = {index: set() for index in candidate_indices}
    for offset, first_index in enumerate(candidate_indices):
        for second_index in candidate_indices[offset + 1 :]:
            if _aligned_table_fragment_pair(
                objects[first_index],
                objects[second_index],
                segment_by_id,
            ):
                adjacency[first_index].add(second_index)
                adjacency[second_index].add(first_index)

    components: list[tuple[int, ...]] = []
    unseen = set(candidate_indices)
    while unseen:
        pending = [min(unseen)]
        component: set[int] = set()
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(adjacency[current] - component)
        unseen -= component
        if len(component) >= 2:
            components.append(tuple(sorted(component)))
    if not components:
        return objects

    replacements: dict[int, _ObjectDraft] = {}
    consumed: set[int] = set()
    for component in components:
        members = tuple(objects[index] for index in component)
        first_index = min(index for index in component if objects[index].kind == "table")
        first = objects[first_index]
        evidence = tuple(dict.fromkeys(value for member in members for value in member.evidence))
        replacements[first_index] = replace(
            first,
            draft=_Draft(
                segment_ids={segment_id for member in members for segment_id in member.draft.segment_ids},
                bbox=_union_boxes(tuple(member.draft.bbox for member in members)),
            ),
            crop_bbox=_union_boxes(tuple(member.crop_bbox for member in members)),
            matrix_bbox=_union_boxes(tuple(member.matrix_bbox for member in members)),
            evidence=(*evidence, "merged-aligned-table-fragments"),
        )
        consumed.update(index for index in component if index != first_index)
    return [replacements.get(index, value) for index, value in enumerate(objects) if index not in consumed]


def _merge_contained_topology_tables(
    objects: list[_ObjectDraft],
) -> list[_ObjectDraft]:
    consumed: set[int] = set()
    replacements: dict[int, _ObjectDraft] = {}
    for parent_index, parent in enumerate(objects):
        if parent.kind != "table" or parent.matrix_basis is not PartitionMatrixBasis.RULE_LATTICE:
            continue
        left, top, right, bottom = parent.matrix_bbox
        child_indices = tuple(
            index
            for index, value in enumerate(objects)
            if index != parent_index
            and index not in consumed
            and value.kind == "table"
            and value.matrix_basis is PartitionMatrixBasis.TOPOLOGY_SLICE
            and left <= value.draft.bbox[0]
            and top <= value.draft.bbox[1]
            and value.draft.bbox[2] <= right
            and value.draft.bbox[3] <= bottom
        )
        if not child_indices:
            continue
        members = (parent, *(objects[index] for index in child_indices))
        replacements[parent_index] = replace(
            parent,
            draft=_Draft(
                segment_ids={segment_id for member in members for segment_id in member.draft.segment_ids},
                bbox=_union_boxes(tuple(member.draft.bbox for member in members)),
            ),
            crop_bbox=_union_boxes(tuple(member.crop_bbox for member in members)),
            evidence=(*parent.evidence, "absorbed-contained-table-fragments"),
        )
        consumed.update(child_indices)
    return [replacements.get(index, value) for index, value in enumerate(objects) if index not in consumed]


def _merge_table_fragments(
    objects: list[_ObjectDraft],
    *,
    segment_by_id: dict[str, PartitionSegment],
) -> list[_ObjectDraft]:
    aligned = _merge_aligned_topology_table_fragments(
        objects,
        segment_by_id=segment_by_id,
    )
    return _merge_contained_topology_tables(aligned)


def _merge_overlapping_structural_grid_tables(
    objects: list[_ObjectDraft],
    *,
    segment_by_id: dict[str, PartitionSegment],
) -> list[_ObjectDraft]:
    """Join only overlapping grid sections, never separated page sections."""

    structural = tuple(
        index
        for index, value in enumerate(objects)
        if value.kind == "table" and "stable-fragmented-rule-grid" in value.evidence
    )
    if not structural:
        return objects
    structural_set = set(structural)
    candidates = tuple(index for index, value in enumerate(objects) if value.kind == "table")
    parents = {index: index for index in candidates}

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parents[second_root] = first_root

    for offset, first in enumerate(candidates):
        first_box = objects[first].matrix_bbox
        for second in candidates[offset + 1 :]:
            second_box = objects[second].matrix_bbox
            horizontal_overlap = min(first_box[2], second_box[2]) - max(first_box[0], second_box[0])
            shorter_width = min(
                first_box[2] - first_box[0],
                second_box[2] - second_box[0],
            )
            vertical_gap = max(
                0,
                max(first_box[1], second_box[1]) - min(first_box[3], second_box[3]),
            )
            if horizontal_overlap > 0 and horizontal_overlap * 4 >= shorter_width * 3 and vertical_gap <= 4:
                union(first, second)

    groups: dict[int, list[int]] = {}
    for index in candidates:
        groups.setdefault(find(index), []).append(index)
    consumed: set[int] = set()
    replacements: dict[int, _ObjectDraft] = {}
    for indexes in groups.values():
        if not any(index in structural_set for index in indexes):
            continue
        table_box = _union_boxes(tuple(objects[index].matrix_bbox for index in indexes))
        selected = tuple(
            index
            for index, value in enumerate(objects)
            if index not in consumed
            and table_box[1] <= (value.draft.bbox[1] + value.draft.bbox[3]) / 2.0 < table_box[3]
            and (min(table_box[2], value.draft.bbox[2]) - max(table_box[0], value.draft.bbox[0])) * 2
            >= min(
                table_box[2] - table_box[0],
                value.draft.bbox[2] - value.draft.bbox[0],
            )
        )
        if not selected:
            continue
        if len(selected) == 1 and len(indexes) == 1:
            continue
        first_index = min(selected)
        first = objects[next(index for index in indexes if index in structural_set)]
        member_ids = {segment_id for index in selected for segment_id in objects[index].draft.segment_ids}
        member_bbox = _union_boxes(tuple(segment_by_id[value].bbox for value in member_ids))
        replacements[first_index] = _ObjectDraft(
            kind="table",
            draft=_Draft(segment_ids=member_ids, bbox=member_bbox),
            crop_bbox=_union_boxes(tuple(objects[index].crop_bbox for index in selected)),
            matrix_bbox=_union_boxes((table_box, member_bbox)),
            evidence=(
                *first.evidence,
                "merged-overlapping-structural-grid-sections",
                f"grid-sections={len(indexes)}",
                f"absorbed-objects={len(selected)}",
            ),
            matrix_basis=PartitionMatrixBasis.TOPOLOGY_SLICE,
        )
        consumed.update(index for index in selected if index != first_index)
    if not replacements:
        return objects
    result = [replacements.get(index, value) for index, value in enumerate(objects) if index not in consumed]
    result.sort(key=lambda value: (value.draft.bbox[1], value.draft.bbox[0]))
    return result


def _merge_stacked_wide_table_sections(
    objects: list[_ObjectDraft],
    *,
    segment_by_id: dict[str, PartitionSegment],
    page_bbox: BoxTuple,
) -> list[_ObjectDraft]:
    """Merge vertically stacked sections of one page-wide sparse table."""

    page_width = page_bbox[2] - page_bbox[0]
    if page_width <= 0:
        return objects
    candidates = tuple(
        index
        for index, value in enumerate(objects)
        if value.kind == "table" and value.matrix_bbox[2] - value.matrix_bbox[0] >= page_width * 0.75
    )
    if len(candidates) < 3:
        return objects

    maximum_gap = max(512, page_width // 4)
    maximum_structural_cluster_height = max(512, round(page_width * 1.25))
    clusters: list[tuple[int, ...]] = []
    current: list[int] = []
    current_box: BoxTuple | None = None
    for index in sorted(
        candidates,
        key=lambda value: (
            objects[value].matrix_bbox[1],
            objects[value].matrix_bbox[0],
        ),
    ):
        candidate_box = objects[index].matrix_bbox
        if current_box is None:
            current = [index]
            current_box = candidate_box
            continue
        overlap = min(current_box[2], candidate_box[2]) - max(current_box[0], candidate_box[0])
        shorter_width = min(
            current_box[2] - current_box[0],
            candidate_box[2] - candidate_box[0],
        )
        vertical_gap = candidate_box[1] - current_box[3]
        combined_box = _union_boxes((current_box, candidate_box))
        current_has_structural_grid = any("stable-fragmented-rule-grid" in objects[item].evidence for item in current)
        candidate_has_structural_grid = "stable-fragmented-rule-grid" in objects[index].evidence
        if (
            overlap > 0
            and overlap * 4 >= shorter_width * 3
            and vertical_gap <= maximum_gap
            and not (
                current_has_structural_grid
                and candidate_has_structural_grid
                and combined_box[3] - combined_box[1] > maximum_structural_cluster_height
            )
        ):
            current.append(index)
            current_box = combined_box
            continue
        if len(current) >= 3:
            clusters.append(tuple(current))
        current = [index]
        current_box = candidate_box
    if len(current) >= 3:
        clusters.append(tuple(current))
    if not clusters:
        return objects

    consumed: set[int] = set()
    replacements: dict[int, _ObjectDraft] = {}
    for cluster in clusters:
        table_box = _union_boxes(tuple(objects[index].matrix_bbox for index in cluster))
        tail_bottom = min(page_bbox[3], table_box[3] + 96)
        selected = tuple(
            index
            for index, value in enumerate(objects)
            if index not in consumed
            and table_box[1] <= (value.draft.bbox[1] + value.draft.bbox[3]) / 2.0 < tail_bottom
            and (min(table_box[2], value.draft.bbox[2]) - max(table_box[0], value.draft.bbox[0])) * 2
            >= min(
                table_box[2] - table_box[0],
                value.draft.bbox[2] - value.draft.bbox[0],
            )
        )
        if len(selected) < len(cluster):
            continue
        first_index = min(selected)
        first = objects[min(cluster)]
        member_ids = {segment_id for index in selected for segment_id in objects[index].draft.segment_ids}
        member_bbox = _union_boxes(tuple(segment_by_id[value].bbox for value in member_ids))
        replacements[first_index] = _ObjectDraft(
            kind="table",
            draft=_Draft(segment_ids=member_ids, bbox=member_bbox),
            crop_bbox=_union_boxes(tuple(objects[index].crop_bbox for index in selected)),
            matrix_bbox=_union_boxes((table_box, member_bbox)),
            evidence=(
                *first.evidence,
                "merged-stacked-wide-table-sections",
                f"table-sections={len(cluster)}",
                f"absorbed-objects={len(selected)}",
            ),
            matrix_basis=PartitionMatrixBasis.TOPOLOGY_SLICE,
        )
        consumed.update(index for index in selected if index != first_index)
    if not replacements:
        return objects
    result = [replacements.get(index, value) for index, value in enumerate(objects) if index not in consumed]
    result.sort(key=lambda value: (value.draft.bbox[1], value.draft.bbox[0]))
    return result


def _merge_parallel_rule_table_objects(
    objects: list[_ObjectDraft],
    *,
    rows: tuple[NumericRow, ...],
    segment_by_id: dict[str, PartitionSegment],
    page_bbox: BoxTuple,
) -> list[_ObjectDraft]:
    """Merge row objects enclosed by a stable horizontal-rule table grid.

    Borderless Markdown-style tables have no vertical rule network, so the
    recursive whitespace partition legitimately sees each payload row as a
    separate flow.  Three or more evenly spaced full-width horizontal rules,
    together with full-width payload rows at the same cadence, are sufficient
    structural evidence to recover one table before block planning.
    """

    page_width = page_bbox[2] - page_bbox[0]
    if page_width <= 0:
        return objects
    rule_rows = tuple(
        row
        for row in rows
        if any(cell.empty and cell.code == 7 and cell.bbox[2] - cell.bbox[0] >= page_width * 0.90 for cell in row.cells)
    )
    if len(rule_rows) < 3:
        return objects

    runs: list[tuple[NumericRow, ...]] = []
    current: list[NumericRow] = []
    for row in rule_rows:
        if current and row.top - current[-1].top > 96:
            if len(current) >= 3:
                runs.append(tuple(current))
            current = []
        current.append(row)
    if len(current) >= 3:
        runs.append(tuple(current))

    consumed: set[int] = set()
    merged: list[_ObjectDraft] = []
    for run in runs:
        steps = tuple(second.top - first.top for first, second in zip(run, run[1:]))
        cadence = float(median(steps))
        if cadence < 8 or max(steps) - min(steps) > max(3.0, cadence * 0.20):
            continue
        region_top = int(run[0].top - cadence)
        region_bottom = int(run[-1].bottom + cadence)
        selected = tuple(
            index
            for index, value in enumerate(objects)
            if index not in consumed
            and (value.kind == "flow" or "single-visual-row-paragraph" in value.evidence)
            and value.matrix_basis is PartitionMatrixBasis.TOPOLOGY_SLICE
            and region_top <= (value.draft.bbox[1] + value.draft.bbox[3]) / 2.0 < region_bottom
        )
        if len(selected) < len(run) + 1:
            continue
        member_ids = {segment_id for index in selected for segment_id in objects[index].draft.segment_ids}
        draft = _Draft(
            segment_ids=member_ids,
            bbox=_union_boxes(tuple(segment_by_id[value].bbox for value in member_ids)),
        )
        visual_rows, _, _ = _visual_flow_rows(draft, segment_by_id)
        if len(visual_rows) < len(run) + 1 or any(row.bbox[2] - row.bbox[0] < page_width * 0.75 for row in visual_rows):
            continue
        consumed.update(selected)
        merged.append(
            _ObjectDraft(
                kind="table",
                draft=draft,
                crop_bbox=draft.bbox,
                matrix_bbox=draft.bbox,
                evidence=(
                    "stable-parallel-horizontal-rules",
                    f"rule-rows={len(run)}",
                    f"payload-rows={len(visual_rows)}",
                ),
                matrix_basis=PartitionMatrixBasis.TOPOLOGY_SLICE,
            )
        )

    if not merged:
        return objects
    result = [value for index, value in enumerate(objects) if index not in consumed]
    result.extend(merged)
    result.sort(key=lambda value: (value.draft.bbox[1], value.draft.bbox[0]))
    return result


def _merge_detached_marker_lanes(
    objects: list[_ObjectDraft],
    *,
    segment_by_id: dict[str, PartitionSegment],
) -> list[_ObjectDraft]:
    """Attach a corridor-separated repeated marker lane to its text body."""

    consumed: set[int] = set()
    replacements: dict[int, _ObjectDraft] = {}
    for marker_index, marker in enumerate(objects):
        marker_segments = tuple(segment_by_id[value] for value in marker.draft.segment_ids)
        if (
            marker_index in consumed
            or len(marker_segments) < 2
            or any(
                segment.bbox[2] - segment.bbox[0] > 12 or segment.bbox[3] - segment.bbox[1] > 12
                for segment in marker_segments
            )
        ):
            continue
        candidates: list[tuple[int, int, _ObjectDraft]] = []
        for body_index, body in enumerate(objects):
            if (
                body_index == marker_index
                or body_index in consumed
                or body.kind not in {"paragraph", "list"}
                or body.draft.bbox[0] < marker.draft.bbox[2]
            ):
                continue
            horizontal_gap = body.draft.bbox[0] - marker.draft.bbox[2]
            vertical_overlap = min(body.draft.bbox[3], marker.draft.bbox[3]) - max(
                body.draft.bbox[1], marker.draft.bbox[1]
            )
            body_rows, _, body_height = _visual_flow_rows(
                body.draft,
                segment_by_id,
            )
            if (
                horizontal_gap > max(32.0, body_height * 3.0)
                or vertical_overlap <= 0
                or len(body_rows) < len(marker_segments)
                or any(
                    not any(max(segment.bbox[1], row.bbox[1]) < min(segment.bbox[3], row.bbox[3]) for row in body_rows)
                    for segment in marker_segments
                )
            ):
                continue
            candidates.append((horizontal_gap, body_index, body))
        if not candidates:
            continue
        _, body_index, body = min(
            candidates,
            key=lambda value: (
                value[0],
                value[2].draft.bbox[1],
                value[2].draft.bbox[0],
            ),
        )
        member_ids = set(marker.draft.segment_ids | body.draft.segment_ids)
        bbox = _union_boxes(tuple(segment_by_id[value].bbox for value in member_ids))
        replacements[body_index] = replace(
            body,
            kind="list",
            draft=_Draft(segment_ids=member_ids, bbox=bbox),
            crop_bbox=bbox,
            matrix_bbox=bbox,
            evidence=(
                *body.evidence,
                "detached-marker-lane",
                "repeated-marker-body-rows",
            ),
        )
        consumed.add(marker_index)
        consumed.add(body_index)

    if not replacements:
        return objects
    result = [
        replacements.get(index, value)
        for index, value in enumerate(objects)
        if index not in consumed or index in replacements
    ]
    result.sort(key=lambda value: (value.draft.bbox[1], value.draft.bbox[0]))
    return result


def _merge_explicit_horizontal_rule_table_objects(
    objects: list[_ObjectDraft],
    *,
    regions: tuple[HorizontalRuleTableRegion, ...],
    segment_by_id: dict[str, PartitionSegment],
    page_bbox: BoxTuple,
) -> list[_ObjectDraft]:
    """Merge table rows using Stage 1 rules that topology intentionally omits."""

    page_width = page_bbox[2] - page_bbox[0]
    result = list(objects)
    for region in regions:
        left, top, right, bottom = region.bbox
        selected: list[int] = []
        for index, value in enumerate(result):
            if value.kind not in ("flow", "paragraph"):
                continue
            if value.matrix_basis is not PartitionMatrixBasis.TOPOLOGY_SLICE:
                continue
            box = value.draft.bbox
            center_y = (box[1] + box[3]) / 2.0
            if center_y < top or center_y >= bottom:
                continue
            if max(left, box[0]) >= min(right, box[2]):
                continue
            selected.append(index)
        if len(selected) < 2:
            continue

        segment_ids = {segment_id for index in selected for segment_id in result[index].draft.segment_ids}
        payload_bbox = _union_boxes(tuple(segment_by_id[value].bbox for value in segment_ids))
        draft = _Draft(segment_ids=segment_ids, bbox=payload_bbox)
        visual_rows, _, _ = _visual_flow_rows(draft, segment_by_id)
        if len(visual_rows) < len(region.rule_boxes):
            continue
        if any(row.bbox[2] - row.bbox[0] < page_width * 0.75 for row in visual_rows):
            continue

        table_bbox = _union_boxes((payload_bbox, region.bbox))
        first = selected[0]
        selected_set = set(selected)
        merged = _ObjectDraft(
            kind="table",
            draft=_Draft(segment_ids=segment_ids, bbox=table_bbox),
            crop_bbox=table_bbox,
            matrix_bbox=table_bbox,
            evidence=(
                "stable-parallel-horizontal-rules",
                f"horizontal-rule-count={len(region.rule_boxes)}",
                f"payload-row-count={len(visual_rows)}",
            ),
            matrix_basis=PartitionMatrixBasis.TOPOLOGY_SLICE,
        )
        result = [
            merged if index == first else value
            for index, value in enumerate(result)
            if index not in selected_set or index == first
        ]
    result.sort(key=lambda value: (value.draft.bbox[1], value.draft.bbox[0]))
    return result


def _structural_rule_table_witness(
    value: _ObjectDraft,
    rules: tuple[StructuralRule, ...],
) -> tuple[int, int, int] | None:
    """Return independent grid evidence for a table with fragmented rules."""

    left, top, right, bottom = value.draft.bbox
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return None

    horizontal: list[tuple[int, int, int]] = []
    vertical: list[tuple[int, int, int]] = []
    for rule in rules:
        rule_left, rule_top, rule_right, rule_bottom = rule.bbox
        if rule_right <= left or rule_left >= right or rule_bottom <= top or rule_top >= bottom:
            continue
        if rule.axis == "horizontal":
            overlap = min(right, rule_right) - max(left, rule_left)
            if overlap < max(24.0, width * 0.20):
                continue
            horizontal.append(
                (
                    (rule_top + rule_bottom) // 2,
                    max(left, rule_left),
                    min(right, rule_right),
                )
            )
        else:
            overlap = min(bottom, rule_bottom) - max(top, rule_top)
            if overlap < max(24.0, height * 0.20):
                continue
            vertical.append(
                (
                    (rule_left + rule_right) // 2,
                    max(top, rule_top),
                    min(bottom, rule_bottom),
                )
            )

    def merge_lines(
        lines: list[tuple[int, int, int]],
    ) -> tuple[tuple[int, int, int], ...]:
        merged: list[list[int]] = []
        for coordinate, start, stop in sorted(lines):
            if not merged or coordinate - merged[-1][0] > 2:
                merged.append([coordinate, start, stop])
                continue
            previous = merged[-1]
            previous[0] = (previous[0] + coordinate) // 2
            previous[1] = min(previous[1], start)
            previous[2] = max(previous[2], stop)
        return tuple(tuple(item) for item in merged)  # type: ignore[return-value]

    horizontal_lines = merge_lines(horizontal)
    vertical_lines = merge_lines(vertical)
    if len(horizontal_lines) < 3 or len(vertical_lines) < 3:
        return None
    crossings = sum(
        horizontal_start - 2 <= vertical_coordinate <= horizontal_stop + 2
        and vertical_start - 2 <= horizontal_coordinate <= vertical_stop + 2
        for horizontal_coordinate, horizontal_start, horizontal_stop in horizontal_lines
        for vertical_coordinate, vertical_start, vertical_stop in vertical_lines
    )
    if crossings < 6:
        return None
    return len(horizontal_lines), len(vertical_lines), crossings


def partition_recursive_objects(
    *,
    segments: tuple[PartitionSegment, ...],
    nodes: tuple[PartitionNode, ...],
    rows: tuple[NumericRow, ...],
    networks: tuple[RuledNetwork, ...],
    horizontal_rule_regions: tuple[HorizontalRuleTableRegion, ...] = (),
    structural_rules: tuple[StructuralRule, ...] = (),
) -> tuple[PartitionedObject, ...]:
    """Return an exact object partition using table anchors and recursion."""

    if not nodes:
        raise ValueError("recursive partition requires retained Stage 1 nodes")
    segment_by_id = {segment.segment_id: segment for segment in segments}
    if len(segment_by_id) != len(segments):
        raise ValueError("segment identifiers must be unique")
    node_by_id = {node.node_id: node for node in nodes}
    if len(node_by_id) != len(nodes):
        raise ValueError("node identifiers must be unique")
    roots = tuple(node for node in nodes if node.node_id == "geo-root")
    if len(roots) != 1:
        raise ValueError("recursive partition requires exactly one geo-root")
    for segment in segments:
        _validate_box(segment.bbox, "segment bbox")
    for node in nodes:
        _validate_box(node.bbox, "node bbox")
        if any(child not in node_by_id for child in node.child_ids):
            raise ValueError("recursive node references an unknown child")

    # Empty topology cuts are discovered before any semantic classification.
    # Stop at the highest recursion camera containing a persistent finite
    # corridor, so a header above a T-shaped body split remains outside the
    # two body chambers.
    def find_topology_chambers(node_id: str) -> list[_TopologyChamber]:
        node = node_by_id[node_id]
        own = set(node.segment_ids)
        if not own:
            return []
        active_segments = tuple(segment_by_id[value] for value in own)
        walls = _spanning_empty_corridors(
            node=node,
            rows=rows,
            active_segments=active_segments,
        )
        if walls:
            ordered_walls = tuple(sorted(walls, key=lambda value: value.bbox[0]))
            intervals = tuple(
                (interval_left, interval_right)
                for interval_left, interval_right in zip(
                    (node.bbox[0], *(wall.bbox[2] for wall in ordered_walls)),
                    (*(wall.bbox[0] for wall in ordered_walls), node.bbox[2]),
                )
                if interval_left < interval_right
            )
            groups: list[set[str]] = []
            for interval_left, interval_right in intervals:
                members = {
                    segment_id
                    for segment_id in own
                    if interval_left
                    <= (segment_by_id[segment_id].bbox[0] + segment_by_id[segment_id].bbox[2]) / 2.0
                    < interval_right
                }
                if members:
                    groups.append(members)
            assigned = {value for group in groups for value in group}
            if len(groups) >= 2 and assigned == own:
                separator_boxes = tuple(wall.bbox for wall in ordered_walls)
                frozen_groups = tuple(groups)
                if _parallel_grid_witness(frozen_groups, segment_by_id):
                    return [
                        _TopologyChamber(
                            draft=_Draft(
                                segment_ids=own,
                                bbox=_union_boxes(tuple(segment_by_id[value].bbox for value in own)),
                            ),
                            separator_boxes=separator_boxes,
                            parallel_grid=True,
                        )
                    ]
                return [
                    _TopologyChamber(
                        draft=_Draft(
                            segment_ids=members,
                            bbox=_union_boxes(tuple(segment_by_id[value].bbox for value in members)),
                        ),
                        separator_boxes=separator_boxes,
                    )
                    for members in frozen_groups
                ]
        return [chamber for child_id in node.child_ids for chamber in find_topology_chambers(child_id)]

    topology_chambers = find_topology_chambers("geo-root")
    chamber_segment_ids = {segment_id for chamber in topology_chambers for segment_id in chamber.draft.segment_ids}

    cycle_boxes = _mixed_axis_cycle_components(rows)
    # ``0 5 / 3 8`` (and its merged-cell variants) describes a finite 2-D
    # footprint.  It does not distinguish a table from one filled image or a
    # text strip split by scanlines.  Only a cycle independently supported by
    # an exact finite structural lattice may preclaim a table object.
    proven_components = tuple(
        (bbox, network)
        for bbox in cycle_boxes
        for network in (_rule_network_for_component(bbox, networks),)
        if network is not None
    )
    component_boxes = tuple(bbox for bbox, _ in proven_components)
    members_by_component: list[set[str]] = [set() for _ in proven_components]
    for segment in segments:
        if segment.segment_id in chamber_segment_ids:
            continue
        matches = tuple(index for index, bbox in enumerate(component_boxes) if _center_inside(segment.bbox, bbox))
        if not matches:
            continue
        # Nested/ragged component envelopes are resolved geometrically, not by
        # network enumeration order.  The smallest containing finite cycle is
        # the most local owner.
        selected = min(
            matches,
            key=lambda index: (
                (component_boxes[index][2] - component_boxes[index][0])
                * (component_boxes[index][3] - component_boxes[index][1]),
                component_boxes[index],
            ),
        )
        members_by_component[selected].add(segment.segment_id)

    table_drafts: list[_ObjectDraft] = []
    for (table_bbox, network), members in zip(
        proven_components,
        members_by_component,
        strict=True,
    ):
        if not members:
            continue
        member_bbox = _union_boxes(tuple(segment_by_id[value].bbox for value in members))
        matrix_bbox = table_bbox
        basis = PartitionMatrixBasis.RULE_LATTICE
        crop_bbox = _union_boxes((network_bbox_for_crop(matrix_bbox, networks), member_bbox))
        evidence = (
            "finite-mixed-axis-cycle",
            "independent-structural-lattice",
            "cycle-normalizes-to-0-5-3-8",
            network.evidence,
        )
        table_drafts.append(
            _ObjectDraft(
                kind="table",
                draft=_Draft(
                    segment_ids=members,
                    bbox=member_bbox,
                ),
                crop_bbox=crop_bbox,
                matrix_bbox=matrix_bbox,
                evidence=evidence,
                matrix_basis=basis,
            )
        )

    table_segment_ids = {segment_id for candidate in table_drafts for segment_id in candidate.draft.segment_ids}

    remaining = set(segment_by_id) - table_segment_ids - chamber_segment_ids

    def recurse(node_id: str) -> list[_Draft]:
        node = node_by_id[node_id]
        own = set(node.segment_ids) & remaining
        if not own:
            return []
        if not node.child_ids:
            return [
                _Draft(
                    segment_ids=own,
                    bbox=_union_boxes(tuple(segment_by_id[value].bbox for value in own)),
                )
            ]

        child_drafts = [recurse(child_id) for child_id in node.child_ids]
        drafts = [draft for values in child_drafts for draft in values]
        covered = {segment_id for draft in drafts for segment_id in draft.segment_ids}
        for segment_id in sorted(own - covered):
            drafts.append(
                _Draft(
                    segment_ids={segment_id},
                    bbox=segment_by_id[segment_id].bbox,
                )
            )
        if len(drafts) < 2:
            return drafts

        heights = tuple(
            segment_by_id[value].bbox[3] - segment_by_id[value].bbox[1]
            for value in own
            if segment_by_id[value].bbox[3] - segment_by_id[value].bbox[1] >= 3
        )
        # Markers, checkboxes and punctuation are real segments, but their
        # short height must not turn the ordinary spacing between list items
        # into a hard object boundary.  The upper quartile still follows the
        # local text scale while ignoring those small witnesses.
        typical_height = _upper_quartile(heights) if heights else 4.0
        child_owner = {id(draft): child_index for child_index, values in enumerate(child_drafts) for draft in values}
        node_height = node.bbox[3] - node.bbox[1]
        short_row_scope = node_height <= max(
            48.0,
            min(96.0, typical_height * 3.0),
        )
        hard_row_separator = _has_empty_row_separator(
            node,
            rows,
            typical_height=typical_height,
            active_segments=tuple(segment_by_id[segment_id] for segment_id in own),
        )
        pairs: list[tuple[int, int]] = []
        for first in range(len(drafts)):
            for second in range(first + 1, len(drafts)):
                first_child = child_owner.get(id(drafts[first]))
                second_child = child_owner.get(id(drafts[second]))
                if first_child is not None and first_child == second_child:
                    continue
                if (
                    node.axis == "rows"
                    and not hard_row_separator
                    and _row_link(drafts[first], drafts[second], typical_height)
                ):
                    pairs.append((first, second))
                elif (
                    node.axis == "columns"
                    and short_row_scope
                    and _column_link(drafts[first], drafts[second], typical_height)
                ):
                    pairs.append((first, second))
        return _merge_drafts(drafts, tuple(pairs))

    flow_drafts = _merge_indented_flow_sections(
        recurse("geo-root"),
        segment_by_id,
    )
    raw_objects: list[_ObjectDraft] = list(table_drafts)
    for chamber in topology_chambers:
        draft = chamber.draft
        visual_rows, _, lane_body_height = _visual_flow_rows(
            draft,
            segment_by_id,
        )
        contains_proven_lattice = any(_center_inside(component_bbox, draft.bbox) for component_bbox in component_boxes)
        marker_list = (
            _marker_list_span(
                visual_rows,
                segment_by_id,
                lane_body_height,
            )
            is not None
        )
        geometry_list = _geometry_list_witness(
            visual_rows,
            lane_body_height,
        )
        if contains_proven_lattice:
            kind = "table"
        elif marker_list or geometry_list:
            kind = "list"
        elif chamber.parallel_grid:
            kind = "table"
        elif visual_rows:
            kind = "paragraph"
        elif len(draft.segment_ids) == 1:
            kind = "paragraph"
        else:
            kind = "flow"
        evidence = (
            "spanning-finite-empty-corridor",
            *(("single-visual-row-paragraph",) if kind == "paragraph" and len(visual_rows) == 1 else ()),
            *(
                ("single-segment-paragraph",)
                if kind == "paragraph" and not visual_rows and len(draft.segment_ids) == 1
                else ()
            ),
            *(
                (
                    "finite-mixed-axis-cycle",
                    "independent-structural-lattice",
                )
                if contains_proven_lattice
                else ()
            ),
            *(("aligned-parallel-row-grid",) if chamber.parallel_grid else ()),
            *(
                ("repeated-marker-body-rows",)
                if marker_list
                else (("repeated-indented-rows",) if geometry_list else ())
            ),
            *(f"empty-corridor={box[0]}:{box[1]}:{box[2]}:{box[3]}" for box in chamber.separator_boxes),
        )
        raw_objects.append(
            _ObjectDraft(
                kind=kind,
                draft=draft,
                crop_bbox=draft.bbox,
                matrix_bbox=draft.bbox,
                evidence=evidence,
                matrix_basis=PartitionMatrixBasis.TOPOLOGY_SLICE,
            )
        )
    for draft in flow_drafts:
        for kind, semantic_draft, evidence in _semantic_flow_parts(
            draft,
            segment_by_id,
            roots[0].bbox,
        ):
            raw_objects.append(
                _ObjectDraft(
                    kind=kind,
                    draft=semantic_draft,
                    crop_bbox=semantic_draft.bbox,
                    matrix_bbox=semantic_draft.bbox,
                    evidence=evidence,
                    matrix_basis=PartitionMatrixBasis.TOPOLOGY_SLICE,
                )
            )
    raw_objects.sort(key=lambda value: (value.draft.bbox[1], value.draft.bbox[0]))
    raw_objects = _merge_detached_marker_lanes(
        raw_objects,
        segment_by_id=segment_by_id,
    )
    raw_objects = _merge_parallel_rule_table_objects(
        raw_objects,
        rows=rows,
        segment_by_id=segment_by_id,
        page_bbox=roots[0].bbox,
    )
    raw_objects = _merge_explicit_horizontal_rule_table_objects(
        raw_objects,
        regions=horizontal_rule_regions,
        segment_by_id=segment_by_id,
        page_bbox=roots[0].bbox,
    )

    promoted_objects: list[_ObjectDraft] = []
    page_width = roots[0].bbox[2] - roots[0].bbox[0]
    for value in raw_objects:
        value_width = value.draft.bbox[2] - value.draft.bbox[0]
        structural_witness = (
            _structural_rule_table_witness(value, structural_rules)
            if value.kind != "table" and value_width * 3 >= page_width
            else None
        )
        if structural_witness is not None and value.kind != "table":
            horizontal_lines, vertical_lines, crossings = structural_witness
            value = replace(
                value,
                kind="table",
                evidence=(
                    *value.evidence,
                    "stable-fragmented-rule-grid",
                    f"horizontal-rule-lines={horizontal_lines}",
                    f"vertical-rule-lines={vertical_lines}",
                    f"rule-crossings={crossings}",
                ),
            )
        witness = _stable_topology_table_witness(value, rows)
        if witness is None:
            promoted_objects.append(value)
            continue
        signature, repetitions = witness
        promoted_objects.append(
            replace(
                value,
                kind="table",
                evidence=(
                    *value.evidence,
                    "stable-multicolumn-topology",
                    f"repeated-column-rows={repetitions}",
                    f"column-count={len(signature)}",
                ),
            )
        )
    raw_objects = _merge_table_fragments(
        promoted_objects,
        segment_by_id=segment_by_id,
    )
    raw_objects = _merge_overlapping_structural_grid_tables(
        raw_objects,
        segment_by_id=segment_by_id,
    )
    raw_objects = _merge_stacked_wide_table_sections(
        raw_objects,
        segment_by_id=segment_by_id,
        page_bbox=roots[0].bbox,
    )

    owned = [segment_id for value in raw_objects for segment_id in value.draft.segment_ids]
    if len(owned) != len(set(owned)) or set(owned) != set(segment_by_id):
        raise ValueError("recursive objects must form an exact segment partition")
    return tuple(
        PartitionedObject(
            object_id=f"object-{index:06d}",
            kind=value.kind,
            bbox=value.crop_bbox,
            matrix_bbox=value.matrix_bbox,
            segment_ids=tuple(
                sorted(
                    value.draft.segment_ids,
                    key=lambda value: (
                        segment_by_id[value].bbox[1],
                        segment_by_id[value].bbox[0],
                        value,
                    ),
                )
            ),
            evidence=value.evidence,
            matrix_basis=value.matrix_basis,
        )
        for index, value in enumerate(raw_objects)
    )


def network_bbox_for_crop(
    matrix_bbox: BoxTuple,
    networks: tuple[RuledNetwork, ...],
) -> BoxTuple:
    """Return the full physical rule bbox for one logical table matrix bbox."""

    matches = tuple(
        network.bbox
        for network in networks
        if (
            network.x_lines[0],
            network.y_lines[0],
            network.x_lines[-1],
            network.y_lines[-1],
        )
        == matrix_bbox
    )
    if len(matches) == 1:
        return matches[0]
    containing = tuple(
        network
        for network in networks
        if network.x_lines[0] == matrix_bbox[0]
        and network.x_lines[-1] == matrix_bbox[2]
        and network.y_lines[0] <= matrix_bbox[1] < matrix_bbox[3]
        and matrix_bbox[3] <= network.y_lines[-1]
        and matrix_bbox[1] in network.y_lines
        and matrix_bbox[3] in network.y_lines
    )
    if len(containing) != 1:
        raise ValueError("table matrix bbox must identify exactly one rule network")
    # The physical network may begin with badges or another non-table band.
    # Once the local numeric witness starts lower, crop only that derived
    # matrix instead of reintroducing the rejected prefix.
    return matrix_bbox


__all__ = (
    "HorizontalRuleTableRegion",
    "MIN_OBJECT_HEIGHT",
    "MIN_OBJECT_WIDTH",
    "NumericCell",
    "NumericRow",
    "PartitionMatrixBasis",
    "PartitionNode",
    "PartitionSegment",
    "PartitionedObject",
    "RuledNetwork",
    "StructuralRule",
    "has_two_dimensional_object_extent",
    "horizontal_rule_table_regions",
    "partition_recursive_objects",
    "table_upper_left_boundary",
    "table_upper_left_witness",
)
