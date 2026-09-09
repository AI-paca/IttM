from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


@dataclass(frozen=True, order=True)
class Box:
    """Half-open integer rectangle: ``[left, top, right, bottom)``."""

    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        if self.left < 0 or self.top < 0:
            raise ValueError("box coordinates must be non-negative")
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("box must have positive area")

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def center(self) -> tuple[float, float]:
        return ((self.left + self.right) / 2.0, (self.top + self.bottom) / 2.0)

    def intersection(self, other: "Box") -> "Box | None":
        left = max(self.left, other.left)
        top = max(self.top, other.top)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return None
        return Box(left, top, right, bottom)

    def intersection_area(self, other: "Box") -> int:
        overlap = self.intersection(other)
        return 0 if overlap is None else overlap.area

    def iou(self, other: "Box") -> float:
        overlap = self.intersection_area(other)
        if overlap == 0:
            return 0.0
        return overlap / (self.area + other.area - overlap)

    def contains_point(self, x: float, y: float) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def clamp(self, *, width: int, height: int) -> "Box":
        left = min(max(0, self.left), width - 1)
        top = min(max(0, self.top), height - 1)
        right = min(max(left + 1, self.right), width)
        bottom = min(max(top + 1, self.bottom), height)
        return Box(left, top, right, bottom)

    @classmethod
    def union(cls, boxes: Iterable["Box"]) -> "Box":
        values = tuple(boxes)
        if not values:
            raise ValueError("cannot build a union of zero boxes")
        return cls(
            min(box.left for box in values),
            min(box.top for box in values),
            max(box.right for box in values),
            max(box.bottom for box in values),
        )

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


Matrix3 = tuple[float, float, float, float, float, float, float, float, float]


def _apply_matrix(matrix: Matrix3, x: float, y: float) -> tuple[float, float]:
    denominator = matrix[6] * x + matrix[7] * y + matrix[8]
    if abs(denominator) < 1e-12:
        raise ValueError("transform maps a point to infinity")
    return (
        (matrix[0] * x + matrix[1] * y + matrix[2]) / denominator,
        (matrix[3] * x + matrix[4] * y + matrix[5]) / denominator,
    )


@dataclass(frozen=True)
class AffineTransform:
    original_size: tuple[int, int]
    aligned_size: tuple[int, int]
    forward: Matrix3
    inverse: Matrix3

    def __post_init__(self) -> None:
        if min(*self.original_size, *self.aligned_size) < 1:
            raise ValueError("transform image sizes must be positive")
        for matrix in (self.forward, self.inverse):
            if len(matrix) != 9 or not all(math.isfinite(value) for value in matrix):
                raise ValueError("transform matrices must contain nine finite values")
            if any(abs(matrix[index] - expected) > 1e-12 for index, expected in ((6, 0.0), (7, 0.0), (8, 1.0))):
                raise ValueError("an affine transform must have last row (0, 0, 1)")
        identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        for first, second in (
            (self.forward, self.inverse),
            (self.inverse, self.forward),
        ):
            product = tuple(
                sum(first[row * 3 + inner] * second[inner * 3 + column] for inner in range(3))
                for row in range(3)
                for column in range(3)
            )
            if any(abs(value - expected) > 1e-9 for value, expected in zip(product, identity)):
                raise ValueError("forward and inverse matrices are not exact inverses")

    @classmethod
    def identity(cls, size: tuple[int, int]) -> "AffineTransform":
        identity: Matrix3 = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        return cls(size, size, identity, identity)

    def point_to_aligned(self, x: float, y: float) -> tuple[float, float]:
        return _apply_matrix(self.forward, x, y)

    def point_to_source(self, x: float, y: float) -> tuple[float, float]:
        return _apply_matrix(self.inverse, x, y)

    def box_to_aligned(self, box: Box) -> Box:
        return self._map_box(box, self.forward, self.aligned_size)

    def box_to_source(self, box: Box) -> Box:
        return self._map_box(box, self.inverse, self.original_size)

    @staticmethod
    def _map_box(box: Box, matrix: Matrix3, target_size: tuple[int, int]) -> Box:
        points = (
            _apply_matrix(matrix, box.left, box.top),
            _apply_matrix(matrix, box.right, box.top),
            _apply_matrix(matrix, box.left, box.bottom),
            _apply_matrix(matrix, box.right, box.bottom),
        )
        epsilon = 1e-7
        raw_left = math.floor(min(point[0] for point in points) + epsilon)
        raw_top = math.floor(min(point[1] for point in points) + epsilon)
        raw_right = math.ceil(max(point[0] for point in points) - epsilon)
        raw_bottom = math.ceil(max(point[1] for point in points) - epsilon)
        width, height = target_size
        left = min(max(0, raw_left), width - 1)
        top = min(max(0, raw_top), height - 1)
        right = min(max(left + 1, raw_right), width)
        bottom = min(max(top + 1, raw_bottom), height)
        return Box(left, top, right, bottom)


@dataclass(frozen=True)
class AlignmentTrace:
    transform: AffineTransform
    correction_degrees: float
    background_rgb: tuple[int, int, int]
    content_bbox: Box | None
    foreground_pixels: int

    def __post_init__(self) -> None:
        if not math.isfinite(self.correction_degrees):
            raise ValueError("correction angle must be finite")
        if len(self.background_rgb) != 3 or any(not 0 <= value <= 255 for value in self.background_rgb):
            raise ValueError("background_rgb must contain three bytes")
        if self.foreground_pixels < 0:
            raise ValueError("foreground pixel count must be non-negative")


class SegmentKind(str, Enum):
    TEXT = "text"
    WORD = "text"
    HORIZONTAL_RULE = "horizontal_rule"
    VERTICAL_RULE = "vertical_rule"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Segment:
    segment_id: str
    bbox: Box
    source_bbox: Box
    kind: SegmentKind
    ink_pixels: int
    row_index: int
    order_key: tuple[int, int]
    parent_path: tuple[str, ...]
    component_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.segment_id:
            raise ValueError("segment_id must not be empty")
        if self.ink_pixels < 1:
            raise ValueError("a segment must contain foreground evidence")
        if self.row_index < 0 or min(self.order_key) < 0:
            raise ValueError("segment order must be non-negative")
        if not self.parent_path:
            raise ValueError("segment parent_path must not be empty")


class RuleAxis(str, Enum):
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"


@dataclass(frozen=True)
class Rule:
    rule_id: str
    bbox: Box
    source_bbox: Box
    axis: RuleAxis
    foreground_pixels: int
    strength: float

    def __post_init__(self) -> None:
        if not self.rule_id:
            raise ValueError("rule_id must not be empty")
        if self.foreground_pixels < 1:
            raise ValueError("a rule must contain foreground evidence")
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError("rule strength must be between zero and one")


class SplitAxis(str, Enum):
    ROWS = "rows"
    COLUMNS = "columns"


class StopReason(str, Enum):
    ATOMIC = "atomic"
    CHARACTER_HEIGHT = "character_height"
    EMPTY = "empty"
    LIMIT = "limit"


class GeometryStatus(str, Enum):
    COMPLETE = "complete"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class RecursiveNode:
    node_id: str
    bbox: Box
    depth: int
    parent_id: str | None
    axis: SplitAxis | None = None
    child_ids: tuple[str, ...] = ()
    segment_ids: tuple[str, ...] = ()
    separator_boxes: tuple[Box, ...] = ()
    split_coordinate: int | None = None
    stop_reason: StopReason | None = None

    def __post_init__(self) -> None:
        if not self.node_id or self.depth < 0:
            raise ValueError("recursive node identity and depth must be valid")
        if len(self.child_ids) != len(set(self.child_ids)):
            raise ValueError("recursive child identifiers must be unique")
        if self.child_ids and (self.axis is None or self.stop_reason is not None):
            raise ValueError("an internal recursive node needs an axis and no stop reason")
        if not self.child_ids and (self.axis is not None or self.stop_reason is None):
            raise ValueError("a recursive leaf needs a stop reason and no split axis")
        split_evidence_count = int(bool(self.separator_boxes)) + int(self.split_coordinate is not None)
        if self.child_ids and split_evidence_count != 1:
            raise ValueError("an internal node needs one gap or component-boundary split trace")
        if not self.child_ids and split_evidence_count:
            raise ValueError("a recursive leaf cannot retain split evidence")
        if self.split_coordinate is not None and self.split_coordinate < 1:
            raise ValueError("component split coordinate must be positive")

    @property
    def is_leaf(self) -> bool:
        return not self.child_ids


@dataclass(frozen=True)
class SegmentationResult:
    segments: tuple[Segment, ...]
    rules: tuple[Rule, ...]
    nodes: tuple[RecursiveNode, ...]
    root_node_id: str
    aligned_size: tuple[int, int]
    foreground_pixels: int
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.aligned_size) != 2 or any(type(value) is not int or value < 1 for value in self.aligned_size):
            raise ValueError("aligned_size must contain two positive integers")
        segment_ids = tuple(segment.segment_id for segment in self.segments)
        rule_ids = tuple(rule.rule_id for rule in self.rules)
        node_ids = tuple(node.node_id for node in self.nodes)
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("segment identifiers must be unique")
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("rule identifiers must be unique")
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("recursive node identifiers must be unique")
        known_nodes = set(node_ids)
        if self.root_node_id not in known_nodes:
            raise ValueError("root node is missing")
        width, height = self.aligned_size
        canvas = Box(0, 0, width, height)
        nodes_by_id = {node.node_id: node for node in self.nodes}
        root = nodes_by_id[self.root_node_id]
        if root.parent_id is not None or root.depth != 0:
            raise ValueError("recursive root must have no parent and depth zero")
        if root.bbox != canvas:
            raise ValueError("recursive root must cover the aligned canvas")
        known_segments = set(segment_ids)
        for node in self.nodes:
            if node.parent_id is not None and node.parent_id not in known_nodes:
                raise ValueError("node references an unknown parent")
            if not set(node.child_ids).issubset(known_nodes):
                raise ValueError("node references an unknown child")
            if not set(node.segment_ids).issubset(known_segments):
                raise ValueError("node references an unknown segment")
            if node.bbox.intersection(canvas) != node.bbox:
                raise ValueError("recursive node lies outside the aligned canvas")
            if node.node_id != self.root_node_id and node.parent_id is None:
                raise ValueError("every non-root node needs a parent")
            for separator in node.separator_boxes:
                if separator.intersection(node.bbox) != separator:
                    raise ValueError("recursive separator lies outside its node")
            if node.split_coordinate is not None:
                if node.axis is SplitAxis.ROWS:
                    valid_coordinate = node.bbox.top < node.split_coordinate < node.bbox.bottom
                else:
                    valid_coordinate = node.bbox.left < node.split_coordinate < node.bbox.right
                if not valid_coordinate:
                    raise ValueError("recursive split coordinate lies outside its node")

        incoming = {node_id: 0 for node_id in node_ids}
        for parent in self.nodes:
            for child_id in parent.child_ids:
                child = nodes_by_id[child_id]
                incoming[child_id] += 1
                if child.parent_id != parent.node_id:
                    raise ValueError("recursive child and parent references disagree")
                if child.depth != parent.depth + 1:
                    raise ValueError("recursive child depth is not parent depth plus one")
                if child.bbox.intersection(parent.bbox) != child.bbox:
                    raise ValueError("recursive child lies outside its parent")
        if incoming[self.root_node_id] != 0 or any(
            count != 1 for node_id, count in incoming.items() if node_id != self.root_node_id
        ):
            raise ValueError("recursive nodes must form a single-parent tree")

        visiting: set[str] = set()
        visited: set[str] = set()
        stack = [(self.root_node_id, False)]
        while stack:
            node_id, leaving = stack.pop()
            if leaving:
                visiting.remove(node_id)
                visited.add(node_id)
                continue
            if node_id in visiting:
                raise ValueError("recursive tree contains a cycle")
            if node_id in visited:
                continue
            visiting.add(node_id)
            stack.append((node_id, True))
            stack.extend((child_id, False) for child_id in reversed(nodes_by_id[node_id].child_ids))
        if visited != known_nodes:
            raise ValueError("recursive tree contains unreachable nodes")

        for segment in self.segments:
            if segment.bbox.intersection(canvas) != segment.bbox:
                raise ValueError("segment lies outside the aligned canvas")
            if segment.parent_path[0] != self.root_node_id:
                raise ValueError("segment parent path must start at the recursive root")
            if any(node_id not in known_nodes for node_id in segment.parent_path):
                raise ValueError("segment parent path references an unknown node")
            for parent_id, child_id in zip(segment.parent_path, segment.parent_path[1:]):
                if child_id not in nodes_by_id[parent_id].child_ids:
                    raise ValueError("segment parent path is not a recursive path")
            leaf = nodes_by_id[segment.parent_path[-1]]
            if leaf.child_ids or segment.segment_id not in leaf.segment_ids:
                raise ValueError("segment parent path must end at its owning leaf")
        for rule in self.rules:
            if rule.bbox.intersection(canvas) != rule.bbox:
                raise ValueError("rule lies outside the aligned canvas")
        if set(root.segment_ids) != known_segments:
            raise ValueError("recursive root must reference every segment")
        if self.foreground_pixels < 0:
            raise ValueError("foreground pixel count must be non-negative")
        owned_pixels = sum(segment.ink_pixels for segment in self.segments) + sum(
            rule.foreground_pixels for rule in self.rules
        )
        if owned_pixels != self.foreground_pixels:
            raise ValueError("segments and rules must exactly own every foreground pixel")
        component_ids = tuple(component_id for segment in self.segments for component_id in segment.component_ids)
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("a foreground component cannot have multiple segment owners")


@dataclass(frozen=True)
class AxisInterval:
    index: int
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.index < 0 or self.start < 0 or self.end <= self.start:
            raise ValueError("invalid sparse-matrix axis interval")


@dataclass(frozen=True)
class SparseCell:
    row: int
    column: int
    segment_id: str

    def __post_init__(self) -> None:
        if self.row < 0 or self.column < 0:
            raise ValueError("cell coordinates must be non-negative")
        if not self.segment_id:
            raise ValueError("sparse cell must reference one segment")


@dataclass(frozen=True)
class SegmentSpan:
    segment_id: str
    row_start: int
    row_stop: int
    column_start: int
    column_stop: int

    def __post_init__(self) -> None:
        if not self.segment_id:
            raise ValueError("span segment_id must not be empty")
        if min(self.row_start, self.column_start) < 0:
            raise ValueError("span starts must be non-negative")
        if self.row_stop <= self.row_start or self.column_stop <= self.column_start:
            raise ValueError("span stops must be greater than starts")


class SparseCoordinateMode(str, Enum):
    """Meaning of sparse matrix axis coordinates.

    ``PIXEL_PARTITION`` is the native v20 contract: row and column intervals
    are physical half-open bands over the aligned raster.  A legacy recursive
    grid instead exposes logical row numbers and x-track indexes.  Its
    projection must remain literal, so ``LOGICAL_PROJECTION`` forbids callers
    from pretending those ordinals are pixel boxes.
    """

    PIXEL_PARTITION = "pixel_partition"
    LOGICAL_PROJECTION = "logical_projection"


@dataclass(frozen=True)
class SparseStructuralCode:
    """One literal non-payload signal from a logical sparse projection."""

    row: int
    column: int
    code: int
    segment_id: str

    def __post_init__(self) -> None:
        if self.row < 0 or self.column < 0 or self.code < 1:
            raise ValueError("invalid sparse structural code")
        if not self.segment_id:
            raise ValueError("sparse structural code needs source provenance")


@dataclass(frozen=True)
class SparseSegmentMatrix:
    rows: tuple[AxisInterval, ...]
    columns: tuple[AxisInterval, ...]
    cells: tuple[SparseCell, ...]
    spans: tuple[SegmentSpan, ...]
    horizontal_rule_rows: tuple[int, ...] = ()
    vertical_rule_columns: tuple[int, ...] = ()
    coordinate_mode: SparseCoordinateMode = SparseCoordinateMode.PIXEL_PARTITION
    structural_codes: tuple[SparseStructuralCode, ...] = ()
    projection_sha256: str | None = None

    def __post_init__(self) -> None:
        self._validate_axis("row", self.rows)
        self._validate_axis("column", self.columns)
        entries = tuple((cell.row, cell.column, cell.segment_id) for cell in self.cells)
        if len(entries) != len(set(entries)):
            raise ValueError("sparse matrix entries must be unique")
        span_ids = tuple(span.segment_id for span in self.spans)
        if len(span_ids) != len(set(span_ids)):
            raise ValueError("a segment must have exactly one sparse span")
        known_segments = set(span_ids)
        if any(cell.segment_id not in known_segments for cell in self.cells):
            raise ValueError("sparse cell references a segment without a span")
        if any(cell.row >= len(self.rows) or cell.column >= len(self.columns) for cell in self.cells):
            raise ValueError("sparse cell lies outside the declared axes")
        if (
            tuple(
                sorted(
                    self.cells,
                    key=lambda cell: (cell.row, cell.column, cell.segment_id),
                )
            )
            != self.cells
        ):
            raise ValueError("sparse cells must use canonical row-major order")
        for span in self.spans:
            owned = tuple(cell for cell in self.cells if cell.segment_id == span.segment_id)
            if not owned:
                raise ValueError("every segment span must contain a sparse cell")
            actual = (
                min(cell.row for cell in owned),
                max(cell.row for cell in owned) + 1,
                min(cell.column for cell in owned),
                max(cell.column for cell in owned) + 1,
            )
            declared = (
                span.row_start,
                span.row_stop,
                span.column_start,
                span.column_stop,
            )
            if declared != actual:
                raise ValueError("segment span does not match its sparse cells")
        if self.horizontal_rule_rows != tuple(sorted(set(self.horizontal_rule_rows))):
            raise ValueError("horizontal rule rows must be unique and sorted")
        if self.vertical_rule_columns != tuple(sorted(set(self.vertical_rule_columns))):
            raise ValueError("vertical rule columns must be unique and sorted")
        if any(index >= len(self.rows) or index < 0 for index in self.horizontal_rule_rows):
            raise ValueError("horizontal rule row lies outside the matrix")
        if any(index >= len(self.columns) or index < 0 for index in self.vertical_rule_columns):
            raise ValueError("vertical rule column lies outside the matrix")
        if not isinstance(self.coordinate_mode, SparseCoordinateMode):
            raise ValueError("sparse coordinate mode is invalid")
        structural_entries = tuple(
            (item.row, item.column, item.segment_id, item.code) for item in self.structural_codes
        )
        if len(structural_entries) != len(set(structural_entries)):
            raise ValueError("sparse structural codes must be unique")
        if tuple(sorted(structural_entries)) != structural_entries:
            raise ValueError("sparse structural codes must use canonical order")
        if any(
            item.row >= len(self.rows) or item.column >= len(self.columns) or item.segment_id not in known_segments
            for item in self.structural_codes
        ):
            raise ValueError("sparse structural code lies outside its projection")
        if self.projection_sha256 is not None and (
            type(self.projection_sha256) is not str
            or len(self.projection_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.projection_sha256)
        ):
            raise ValueError("sparse projection SHA-256 must be lowercase hexadecimal")
        if self.coordinate_mode is SparseCoordinateMode.LOGICAL_PROJECTION:
            if self.projection_sha256 is None:
                raise ValueError("logical sparse projection requires immutable provenance")
            if self.horizontal_rule_rows or self.vertical_rule_columns:
                raise ValueError("logical projection cannot claim physical rule bands")
        elif self.structural_codes or self.projection_sha256 is not None:
            raise ValueError("legacy projection evidence requires logical coordinate mode")

    @staticmethod
    def _validate_axis(name: str, intervals: tuple[AxisInterval, ...]) -> None:
        if tuple(interval.index for interval in intervals) != tuple(range(len(intervals))):
            raise ValueError(f"sparse {name} indexes must be contiguous")
        if intervals and intervals[0].start != 0:
            raise ValueError(f"sparse {name} axis must start at zero")
        if any(previous.end != current.start for previous, current in zip(intervals, intervals[1:])):
            raise ValueError(f"sparse {name} intervals must exactly cover their axis")

    def segment_ids(self) -> frozenset[str]:
        return frozenset(span.segment_id for span in self.spans)

    def cell_map(self) -> dict[tuple[int, int], tuple[str, ...]]:
        values: dict[tuple[int, int], list[str]] = {}
        for cell in self.cells:
            values.setdefault((cell.row, cell.column), []).append(cell.segment_id)
        return {coordinate: tuple(segment_ids) for coordinate, segment_ids in values.items()}


@dataclass(frozen=True)
class GeometryResult:
    alignment: AlignmentTrace
    segmentation: SegmentationResult
    matrix: SparseSegmentMatrix
    aligned_rgb_sha256: str
    status: GeometryStatus = GeometryStatus.COMPLETE
    diagnostics: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if (
            type(self.aligned_rgb_sha256) is not str
            or len(self.aligned_rgb_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.aligned_rgb_sha256)
        ):
            raise ValueError("aligned RGB SHA-256 must be 64 lowercase hexadecimal characters")
        if self.alignment.transform.aligned_size != self.segmentation.aligned_size:
            raise ValueError("alignment and segmentation canvas sizes differ")
        if self.alignment.foreground_pixels != self.segmentation.foreground_pixels:
            raise ValueError("alignment and segmentation foreground counts differ")
        expected_segment_ids = frozenset(segment.segment_id for segment in self.segmentation.segments)
        if self.matrix.segment_ids() != expected_segment_ids:
            raise ValueError("sparse matrix does not contain every segment exactly once")
        limit_leaf_count = sum(node.stop_reason is StopReason.LIMIT for node in self.segmentation.nodes)
        expected_status = GeometryStatus.DEGRADED if limit_leaf_count else GeometryStatus.COMPLETE
        if self.status is not expected_status:
            raise ValueError("geometry status does not match recursion limit leaves")

    @property
    def limit_leaf_count(self) -> int:
        return sum(node.stop_reason is StopReason.LIMIT for node in self.segmentation.nodes)
