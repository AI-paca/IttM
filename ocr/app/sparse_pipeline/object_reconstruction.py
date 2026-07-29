from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from enum import Enum
from statistics import median

from app.sparse_pipeline.contracts import (
    AxisInterval,
    Box,
    Rule,
    RuleAxis,
    Segment,
    SegmentSpan,
    SparseCoordinateMode,
    SparseSegmentMatrix,
)
from app.sparse_pipeline.v16_sparse_codes import (
    MERGE_LEFT_CODE,
    sparse_code_components,
)


class ObjectKind(str, Enum):
    """A conservative structural classification for one document object."""

    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    UNKNOWN = "unknown"


class ObjectReconstructionStatus(str, Enum):
    COMPLETE = "complete"


class ObjectReconstructionLimitError(RuntimeError):
    """Raised before stage 6 would exceed an explicit work budget."""


class ObjectReconstructionInvariantError(ValueError):
    """Raised when geometry and sparse-matrix evidence disagree."""


@dataclass(frozen=True)
class ObjectReconstructionConfig:
    """Hard bounds and scale-independent grouping thresholds for stage 6."""

    max_segments: int = 100_000
    max_cells: int = 1_000_000
    max_rules: int = 100_000
    max_objects: int = 100_000
    max_axis_intervals: int = 1_000_000
    max_pairwise_checks: int = 2_000_000
    row_center_tolerance: float = 0.65
    fragment_gap_heights: float = 1.75
    adjacent_row_gap_heights: float = 2.0
    aligned_edge_heights: float = 1.25
    table_min_occupancy: float = 0.5

    def __post_init__(self) -> None:
        limits = (
            self.max_segments,
            self.max_cells,
            self.max_rules,
            self.max_objects,
            self.max_axis_intervals,
            self.max_pairwise_checks,
        )
        if any(type(value) is not int or value < 1 for value in limits):
            raise ValueError("object reconstruction limits must be positive integers")
        ratios = (
            self.row_center_tolerance,
            self.fragment_gap_heights,
            self.adjacent_row_gap_heights,
            self.aligned_edge_heights,
            self.table_min_occupancy,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0.0
            for value in ratios
        ):
            raise ValueError("object reconstruction ratios must be finite and positive")
        if self.table_min_occupancy > 1.0:
            raise ValueError("table_min_occupancy must not exceed one")


@dataclass(frozen=True)
class DocumentObject:
    object_id: str
    kind: ObjectKind
    segment_ids: tuple[str, ...]
    bbox: Box
    reading_index: int
    row_start: int
    row_stop: int
    column_start: int
    column_stop: int
    confidence: float
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.object_id) is not str or not self.object_id:
            raise ValueError("object_id must not be empty")
        if not isinstance(self.kind, ObjectKind):
            raise ValueError("object kind must be an ObjectKind")
        if type(self.segment_ids) is not tuple or any(
            type(segment_id) is not str or not segment_id
            for segment_id in self.segment_ids
        ):
            raise ValueError(
                "object segment identifiers must be an immutable string tuple"
            )
        if not self.segment_ids or len(self.segment_ids) != len(set(self.segment_ids)):
            raise ValueError("a document object must own unique segments")
        if not isinstance(self.bbox, Box):
            raise ValueError("object bbox must be a Box")
        if type(self.reading_index) is not int or self.reading_index < 0:
            raise ValueError("reading_index must be non-negative")
        span_values = (
            self.row_start,
            self.row_stop,
            self.column_start,
            self.column_stop,
        )
        if any(type(value) is not int for value in span_values):
            raise ValueError("object span coordinates must be integers")
        if min(self.row_start, self.column_start) < 0:
            raise ValueError("object span starts must be non-negative")
        if self.row_stop <= self.row_start or self.column_stop <= self.column_start:
            raise ValueError("object span stops must be greater than starts")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(float(self.confidence))
            or not 0.0 <= self.confidence <= 1.0
        ):
            raise ValueError("object confidence must be between zero and one")
        if type(self.evidence) is not tuple or any(
            type(item) is not str or not item for item in self.evidence
        ):
            raise ValueError("object evidence must be an immutable string tuple")


@dataclass(frozen=True)
class SegmentObjectOwnership:
    segment_id: str
    object_id: str

    def __post_init__(self) -> None:
        if (
            type(self.segment_id) is not str
            or not self.segment_id
            or type(self.object_id) is not str
            or not self.object_id
        ):
            raise ValueError("segment ownership identifiers must not be empty")


@dataclass(frozen=True)
class ObjectReconstructionResult:
    aligned_size: tuple[int, int]
    source_segment_ids: tuple[str, ...]
    objects: tuple[DocumentObject, ...]
    segment_ownership: tuple[SegmentObjectOwnership, ...]
    status: ObjectReconstructionStatus = ObjectReconstructionStatus.COMPLETE
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.aligned_size) is not tuple
            or len(self.aligned_size) != 2
            or any(type(value) is not int or value < 1 for value in self.aligned_size)
        ):
            raise ValueError("aligned_size must contain two positive integers")
        if type(self.source_segment_ids) is not tuple or any(
            type(segment_id) is not str or not segment_id
            for segment_id in self.source_segment_ids
        ):
            raise ValueError("source segment identifiers must be an immutable tuple")
        if type(self.objects) is not tuple or any(
            not isinstance(item, DocumentObject) for item in self.objects
        ):
            raise ValueError("objects must be an immutable DocumentObject tuple")
        if type(self.segment_ownership) is not tuple or any(
            not isinstance(item, SegmentObjectOwnership)
            for item in self.segment_ownership
        ):
            raise ValueError("segment ownership must be an immutable tuple")
        if not isinstance(self.status, ObjectReconstructionStatus):
            raise ValueError("object reconstruction status is invalid")
        if type(self.diagnostics) is not tuple or any(
            type(item) is not str or not item for item in self.diagnostics
        ):
            raise ValueError("diagnostics must be an immutable string tuple")
        if len(self.source_segment_ids) != len(set(self.source_segment_ids)):
            raise ValueError("source segment identifiers must be unique")
        object_ids = tuple(item.object_id for item in self.objects)
        if object_ids != tuple(
            f"object-{index:06d}" for index in range(len(object_ids))
        ):
            raise ValueError("object identifiers must be canonical")
        if tuple(item.reading_index for item in self.objects) != tuple(
            range(len(self.objects))
        ):
            raise ValueError("object reading indexes must be contiguous")
        owned_ids = tuple(
            segment_id for item in self.objects for segment_id in item.segment_ids
        )
        if len(owned_ids) != len(set(owned_ids)) or set(owned_ids) != set(
            self.source_segment_ids
        ):
            raise ValueError("objects must form an exact segment partition")
        ownership_ids = tuple(item.segment_id for item in self.segment_ownership)
        if ownership_ids != self.source_segment_ids:
            raise ValueError("segment ownership must follow canonical source order")
        expected_owner = {
            segment_id: item.object_id
            for item in self.objects
            for segment_id in item.segment_ids
        }
        if any(
            item.object_id != expected_owner.get(item.segment_id)
            for item in self.segment_ownership
        ):
            raise ValueError("segment ownership disagrees with document objects")
        source_order = {
            segment_id: index
            for index, segment_id in enumerate(self.source_segment_ids)
        }
        if any(
            tuple(source_order[segment_id] for segment_id in item.segment_ids)
            != tuple(
                sorted(source_order[segment_id] for segment_id in item.segment_ids)
            )
            for item in self.objects
        ):
            raise ValueError("object segments must follow canonical source order")


@dataclass(frozen=True)
class _Fragment:
    row: int
    segments: tuple[Segment, ...]
    bbox: Box


@dataclass(frozen=True)
class _ObjectDraft:
    kind: ObjectKind
    segments: tuple[Segment, ...]
    confidence: float
    evidence: tuple[str, ...]
    sparse_span: tuple[int, int, int, int] | None = None
    structural_barrier: Box | None = None

    @property
    def bbox(self) -> Box:
        return Box.union(segment.bbox for segment in self.segments)


class _DisjointSet:
    def __init__(self, size: int) -> None:
        self._parent = list(range(size))
        self._rank = [0] * size

    def find(self, value: int) -> int:
        parent = value
        while self._parent[parent] != parent:
            parent = self._parent[parent]
        while self._parent[value] != value:
            next_value = self._parent[value]
            self._parent[value] = parent
            value = next_value
        return parent

    def union(self, first: int, second: int) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root == second_root:
            return
        if self._rank[first_root] < self._rank[second_root]:
            first_root, second_root = second_root, first_root
        self._parent[second_root] = first_root
        if self._rank[first_root] == self._rank[second_root]:
            self._rank[first_root] += 1


class ObjectReconstructor:
    """Decode structural objects from bounded sparse geometry, without raster I/O."""

    def __init__(self, config: ObjectReconstructionConfig | None = None) -> None:
        if config is not None and not isinstance(config, ObjectReconstructionConfig):
            raise TypeError("config must be an ObjectReconstructionConfig")
        self.config = config or ObjectReconstructionConfig()

    def reconstruct(
        self,
        *,
        aligned_size: tuple[int, int],
        segments: tuple[Segment, ...],
        rules: tuple[Rule, ...],
        matrix: SparseSegmentMatrix,
    ) -> ObjectReconstructionResult:
        if type(aligned_size) is not tuple:
            raise ObjectReconstructionInvariantError(
                "aligned canvas size must be an immutable tuple"
            )
        if type(segments) is not tuple or any(
            not isinstance(item, Segment) for item in segments
        ):
            raise ObjectReconstructionInvariantError(
                "segments must be an immutable Segment tuple"
            )
        if type(rules) is not tuple or any(
            not isinstance(item, Rule) for item in rules
        ):
            raise ObjectReconstructionInvariantError(
                "rules must be an immutable Rule tuple"
            )
        if not isinstance(matrix, SparseSegmentMatrix):
            raise ObjectReconstructionInvariantError(
                "matrix must be a SparseSegmentMatrix"
            )
        self._check_budgets(segments=segments, rules=rules, matrix=matrix)
        spans = self._validate_inputs(
            aligned_size=aligned_size,
            segments=segments,
            rules=rules,
            matrix=matrix,
        )
        canonical_segments = tuple(
            sorted(segments, key=lambda item: self._segment_key(item, spans))
        )
        source_segment_ids = tuple(item.segment_id for item in canonical_segments)
        if not canonical_segments:
            return ObjectReconstructionResult(
                aligned_size=aligned_size,
                source_segment_ids=(),
                objects=(),
                segment_ownership=(),
            )

        if matrix.coordinate_mode is SparseCoordinateMode.LOGICAL_PROJECTION:
            # Legacy recursive-grid blank rows are explicit object barriers.
            # The ordinary visual-flow pass ignores sparse gaps and can join
            # consecutive tables or sections whose bboxes touch.  Keep every
            # occupied-row component as an independent reconstruction scope;
            # classification inside a scope remains the same conservative
            # geometry-only flow classifier.
            drafts = tuple(
                draft
                for group in self._logical_projection_groups(
                    canonical_segments,
                    matrix,
                )
                for draft in self._logical_group_drafts(group, matrix)
            )
        else:
            table_drafts, table_segment_ids = self._table_drafts(
                aligned_size=aligned_size,
                segments=canonical_segments,
                rules=rules,
                matrix=matrix,
                spans=spans,
            )
            remaining = tuple(
                segment
                for segment in canonical_segments
                if segment.segment_id not in table_segment_ids
            )
            table_barriers = tuple(
                draft.structural_barrier
                for draft in table_drafts
                if draft.structural_barrier is not None
            )
            drafts = table_drafts + self._flow_drafts(
                remaining,
                table_barriers=table_barriers,
            )
        drafts = tuple(
            sorted(
                drafts,
                key=lambda item: (
                    item.bbox.top,
                    item.bbox.left,
                    item.bbox.bottom,
                    item.bbox.right,
                    tuple(segment.segment_id for segment in item.segments),
                ),
            )
        )
        if len(drafts) > self.config.max_objects:
            raise ObjectReconstructionLimitError(
                f"object count exceeds configured limit {self.config.max_objects}"
            )

        objects = tuple(
            self._finish_draft(
                draft,
                index=index,
                spans=spans,
            )
            for index, draft in enumerate(drafts)
        )
        owners = {
            segment_id: item.object_id
            for item in objects
            for segment_id in item.segment_ids
        }
        ownership = tuple(
            SegmentObjectOwnership(segment_id, owners[segment_id])
            for segment_id in source_segment_ids
        )
        return ObjectReconstructionResult(
            aligned_size=aligned_size,
            source_segment_ids=source_segment_ids,
            objects=objects,
            segment_ownership=ownership,
        )

    def _check_budgets(
        self,
        *,
        segments: tuple[Segment, ...],
        rules: tuple[Rule, ...],
        matrix: SparseSegmentMatrix,
    ) -> None:
        if len(segments) > self.config.max_segments:
            raise ObjectReconstructionLimitError(
                f"segment count exceeds configured limit {self.config.max_segments}"
            )
        if len(matrix.cells) > self.config.max_cells:
            raise ObjectReconstructionLimitError(
                f"cell count exceeds configured limit {self.config.max_cells}"
            )
        if len(rules) > self.config.max_rules:
            raise ObjectReconstructionLimitError(
                f"rule count exceeds configured limit {self.config.max_rules}"
            )
        axis_count = len(matrix.rows) + len(matrix.columns)
        if axis_count > self.config.max_axis_intervals:
            raise ObjectReconstructionLimitError(
                "axis interval count exceeds configured limit "
                f"{self.config.max_axis_intervals}"
            )

    @staticmethod
    def _validate_inputs(
        *,
        aligned_size: tuple[int, int],
        segments: tuple[Segment, ...],
        rules: tuple[Rule, ...],
        matrix: SparseSegmentMatrix,
    ) -> dict[str, SegmentSpan]:
        if len(aligned_size) != 2 or any(
            type(value) is not int or value < 1 for value in aligned_size
        ):
            raise ObjectReconstructionInvariantError(
                "aligned canvas size must contain two positive integers"
            )
        width, height = aligned_size
        if not segments and not rules:
            if any(
                (
                    matrix.rows,
                    matrix.columns,
                    matrix.cells,
                    matrix.spans,
                    matrix.horizontal_rule_rows,
                    matrix.vertical_rule_columns,
                )
            ):
                raise ObjectReconstructionInvariantError(
                    "empty geometry evidence requires empty sparse axes"
                )
            return {}
        logical_projection = (
            matrix.coordinate_mode is SparseCoordinateMode.LOGICAL_PROJECTION
        )
        if logical_projection:
            if rules:
                raise ObjectReconstructionInvariantError(
                    "logical sparse projection cannot carry physical rules"
                )
            for name, intervals in (
                ("row", matrix.rows),
                ("column", matrix.columns),
            ):
                if any(
                    item.start != item.index or item.end != item.index + 1
                    for item in intervals
                ):
                    raise ObjectReconstructionInvariantError(
                        f"logical sparse {name} axis must use ordinal intervals"
                    )
        else:
            if not matrix.rows or matrix.rows[-1].end != height:
                raise ObjectReconstructionInvariantError(
                    "sparse row axis does not exactly cover the aligned canvas"
                )
            if not matrix.columns or matrix.columns[-1].end != width:
                raise ObjectReconstructionInvariantError(
                    "sparse column axis does not exactly cover the aligned canvas"
                )
        segment_ids = tuple(item.segment_id for item in segments)
        if len(segment_ids) != len(set(segment_ids)):
            raise ObjectReconstructionInvariantError(
                "segment identifiers must be unique"
            )
        span_ids = tuple(item.segment_id for item in matrix.spans)
        if set(segment_ids) != set(span_ids):
            raise ObjectReconstructionInvariantError(
                "segment set disagrees with sparse matrix spans"
            )
        spans = {item.segment_id: item for item in matrix.spans}
        canvas = Box(0, 0, width, height)
        if any(item.bbox.intersection(canvas) != item.bbox for item in segments):
            raise ObjectReconstructionInvariantError(
                "segment lies outside aligned canvas"
            )
        if not logical_projection:
            for segment in segments:
                span = spans[segment.segment_id]
                span_box = Box(
                    matrix.columns[span.column_start].start,
                    matrix.rows[span.row_start].start,
                    matrix.columns[span.column_stop - 1].end,
                    matrix.rows[span.row_stop - 1].end,
                )
                if segment.bbox.intersection(span_box) != segment.bbox:
                    raise ObjectReconstructionInvariantError(
                        f"segment {segment.segment_id} lies outside its sparse span"
                    )
        rule_ids = tuple(item.rule_id for item in rules)
        if len(rule_ids) != len(set(rule_ids)):
            raise ObjectReconstructionInvariantError("rule identifiers must be unique")
        if any(item.bbox.intersection(canvas) != item.bbox for item in rules):
            raise ObjectReconstructionInvariantError("rule lies outside aligned canvas")

        horizontal_rules = tuple(
            item for item in rules if item.axis is RuleAxis.HORIZONTAL
        )
        vertical_rules = tuple(item for item in rules if item.axis is RuleAxis.VERTICAL)
        ObjectReconstructor._validate_rule_projection(
            name="horizontal rule row",
            intervals=matrix.rows,
            declared=matrix.horizontal_rule_rows,
            projections=tuple(
                (rule.bbox.top, rule.bbox.bottom) for rule in horizontal_rules
            ),
        )
        ObjectReconstructor._validate_rule_projection(
            name="vertical rule column",
            intervals=matrix.columns,
            declared=matrix.vertical_rule_columns,
            projections=tuple(
                (rule.bbox.left, rule.bbox.right) for rule in vertical_rules
            ),
        )
        return spans

    @staticmethod
    def _logical_projection_groups(
        segments: tuple[Segment, ...],
        matrix: SparseSegmentMatrix,
    ) -> tuple[tuple[Segment, ...], ...]:
        """Split literal logical anchors at unoccupied sparse rows."""

        row_by_segment: dict[str, int] = {}
        for cell in matrix.cells:
            previous = row_by_segment.setdefault(cell.segment_id, cell.row)
            if previous != cell.row:
                raise ObjectReconstructionInvariantError(
                    "logical projection payload spans multiple rows"
                )
        if set(row_by_segment) != {item.segment_id for item in segments}:
            raise ObjectReconstructionInvariantError(
                "logical projection needs one payload anchor per segment"
            )
        occupied = tuple(sorted(set(row_by_segment.values())))
        row_group: dict[int, int] = {}
        group_index = -1
        previous: int | None = None
        for row in occupied:
            if previous is None or row != previous + 1:
                group_index += 1
            row_group[row] = group_index
            previous = row
        groups: list[list[Segment]] = [
            [] for _ in range(group_index + 1)
        ]
        for segment in segments:
            groups[row_group[row_by_segment[segment.segment_id]]].append(segment)
        return tuple(tuple(group) for group in groups)

    def _logical_group_drafts(
        self,
        segments: tuple[Segment, ...],
        matrix: SparseSegmentMatrix,
    ) -> tuple[_ObjectDraft, ...]:
        """Classify exactly one object inside one literal blank-row scope."""

        segment_ids = {item.segment_id for item in segments}
        anchors = tuple(
            cell for cell in matrix.cells if cell.segment_id in segment_ids
        )
        codes = tuple(
            item
            for item in matrix.structural_codes
            if item.segment_id in segment_ids
        )
        anchor_columns = {item.column for item in anchors}
        projection_columns = anchor_columns | {item.column for item in codes}
        merge_left = tuple(
            item
            for item in codes
            if MERGE_LEFT_CODE in sparse_code_components(item.code)
        )
        merge_left_rows = {item.row for item in merge_left}

        # v16 projected a table rail as many merge-left/empty coordinates
        # linked to the leaf anchor.  Require at least four logical tracks
        # and two independent merge-left witnesses.  A multirow group needs
        # either two witnessed rows or dense repeated witnesses; a singleton
        # is admitted only by at least four separate tracks.  In particular,
        # the real v16 ``09`` three-track singleton is a header/logo, not a
        # table.  Ordinary indented text has no merge-left evidence.
        table_witness = (
            len(projection_columns) >= 4
            and len(merge_left) >= 2
            and (
                len(segments) == 1
                or len(merge_left_rows) >= 2
                or len(merge_left) >= len(segments)
            )
        )
        if table_witness:
            kind = ObjectKind.TABLE
            confidence = 0.9
            evidence = (
                "logical-blank-row-scope",
                "v16-merge-left-grid",
                f"logical-columns={len(projection_columns)}",
                f"merge-left-witnesses={len(merge_left)}",
                f"merge-left-rows={len(merge_left_rows)}",
            )
        else:
            minimum_column = min(anchor_columns)
            indented = sum(
                item.column > minimum_column for item in anchors
            )
            list_witness = (
                not merge_left
                and len(anchor_columns) >= 2
                and indented * 2 >= len(anchors)
            )
            if list_witness:
                kind = ObjectKind.LIST
                confidence = 0.72
                evidence = (
                    "logical-blank-row-scope",
                    "v16-repeated-indented-anchor",
                )
            else:
                kind = ObjectKind.PARAGRAPH
                confidence = 0.68
                evidence = (
                    "logical-blank-row-scope",
                    "v16-contextual-row-flow",
                )
        return (
            _ObjectDraft(
                kind=kind,
                segments=segments,
                confidence=confidence,
                evidence=evidence,
            ),
        )

    @staticmethod
    def _validate_rule_projection(
        *,
        name: str,
        intervals: tuple[AxisInterval, ...],
        declared: tuple[int, ...],
        projections: tuple[tuple[int, int], ...],
    ) -> None:
        ordered = tuple(sorted(projections))
        starts = tuple(start for start, _ in ordered)
        maximum_stops: list[int] = []
        maximum_stop = -1
        for _, stop in ordered:
            maximum_stop = max(maximum_stop, stop)
            maximum_stops.append(maximum_stop)
        declared_set = set(declared)
        for interval in intervals:
            last_candidate = bisect_left(starts, interval.end) - 1
            covered = (
                last_candidate >= 0 and maximum_stops[last_candidate] > interval.start
            )
            if covered != (interval.index in declared_set):
                raise ObjectReconstructionInvariantError(
                    f"{name} projection disagrees with rule evidence at {interval.index}"
                )

    @staticmethod
    def _segment_key(
        segment: Segment,
        spans: dict[str, SegmentSpan],
    ) -> tuple[int, ...] | tuple[int, int, int, int, int, int, str]:
        span = spans[segment.segment_id]
        return (
            span.row_start,
            span.column_start,
            span.row_stop,
            span.column_stop,
            segment.bbox.top,
            segment.bbox.left,
            segment.segment_id,
        )

    def _table_drafts(
        self,
        *,
        aligned_size: tuple[int, int],
        segments: tuple[Segment, ...],
        rules: tuple[Rule, ...],
        matrix: SparseSegmentMatrix,
        spans: dict[str, SegmentSpan],
    ) -> tuple[tuple[_ObjectDraft, ...], frozenset[str]]:
        if not matrix.horizontal_rule_rows or not matrix.vertical_rule_columns:
            return (), frozenset()
        horizontal = tuple(
            sorted(
                (rule for rule in rules if rule.axis is RuleAxis.HORIZONTAL),
                key=lambda item: (item.bbox.top, item.bbox.left, item.rule_id),
            )
        )
        vertical = tuple(
            sorted(
                (rule for rule in rules if rule.axis is RuleAxis.VERTICAL),
                key=lambda item: (item.bbox.left, item.bbox.top, item.rule_id),
            )
        )
        if (
            self._rule_band_count(horizontal, RuleAxis.HORIZONTAL) < 3
            or self._rule_band_count(vertical, RuleAxis.VERTICAL) < 3
        ):
            return (), frozenset()

        combined = horizontal + vertical
        disjoint = _DisjointSet(len(combined))
        vertical_offset = len(horizontal)
        # Rules are normally few and this is bounded by max_rules.  We avoid any
        # rows*columns raster or dense sparse-matrix projection.
        pairwise_checks = 0
        for horizontal_index, horizontal_rule in enumerate(horizontal):
            for vertical_index, vertical_rule in enumerate(vertical):
                pairwise_checks += 1
                if pairwise_checks > self.config.max_pairwise_checks:
                    raise ObjectReconstructionLimitError(
                        "rule pairwise checks exceed configured limit "
                        f"{self.config.max_pairwise_checks}"
                    )
                if self._rules_intersect(horizontal_rule.bbox, vertical_rule.bbox):
                    disjoint.union(horizontal_index, vertical_offset + vertical_index)

        networks: dict[int, list[Rule]] = {}
        for index, rule in enumerate(combined):
            networks.setdefault(disjoint.find(index), []).append(rule)
        cells_by_segment: dict[str, list[tuple[int, int]]] = {
            segment.segment_id: [] for segment in segments
        }
        for cell in matrix.cells:
            cells_by_segment[cell.segment_id].append((cell.row, cell.column))
        candidates: list[
            tuple[
                Box,
                tuple[Segment, ...],
                tuple[int, int, int, int],
                dict[str, frozenset[tuple[int, int]]],
            ]
        ] = []
        for network in networks.values():
            network_horizontal = tuple(
                rule for rule in network if rule.axis is RuleAxis.HORIZONTAL
            )
            network_vertical = tuple(
                rule for rule in network if rule.axis is RuleAxis.VERTICAL
            )
            if (
                self._rule_band_count(network_horizontal, RuleAxis.HORIZONTAL) < 3
                or self._rule_band_count(network_vertical, RuleAxis.VERTICAL) < 3
            ):
                continue
            network_bbox = Box.union(rule.bbox for rule in network)
            if self._is_page_frame_only(
                network_bbox=network_bbox,
                horizontal=network_horizontal,
                vertical=network_vertical,
                aligned_size=aligned_size,
            ):
                continue
            horizontal_bands = self._rule_bands(
                network_horizontal,
                RuleAxis.HORIZONTAL,
            )
            vertical_bands = self._rule_bands(
                network_vertical,
                RuleAxis.VERTICAL,
            )
            network_sparse_span = self._network_sparse_span(
                horizontal=network_horizontal,
                vertical=network_vertical,
                matrix=matrix,
            )
            row_start, row_stop, column_start, column_stop = network_sparse_span
            horizontal_band_ends = tuple(stop for _, stop in horizontal_bands)
            vertical_band_ends = tuple(stop for _, stop in vertical_bands)
            members_list: list[Segment] = []
            logical_cells_by_segment: dict[
                str,
                frozenset[tuple[int, int]],
            ] = {}
            for segment in segments:
                pairwise_checks += 1
                if pairwise_checks > self.config.max_pairwise_checks:
                    raise ObjectReconstructionLimitError(
                        "table candidate checks exceed configured limit "
                        f"{self.config.max_pairwise_checks}"
                    )
                span = spans[segment.segment_id]
                if (
                    span.row_start < row_start
                    or span.row_stop > row_stop
                    or span.column_start < column_start
                    or span.column_stop > column_stop
                ):
                    continue
                logical_cells: set[tuple[int, int]] = set()
                for matrix_row, matrix_column in cells_by_segment[
                    segment.segment_id
                ]:
                    pairwise_checks += 1
                    if pairwise_checks > self.config.max_pairwise_checks:
                        raise ObjectReconstructionLimitError(
                            "table sparse-cell checks exceed configured limit "
                            f"{self.config.max_pairwise_checks}"
                        )
                    logical_row = self._logical_lane(
                        matrix.rows[matrix_row],
                        horizontal_bands,
                        horizontal_band_ends,
                    )
                    logical_column = self._logical_lane(
                        matrix.columns[matrix_column],
                        vertical_bands,
                        vertical_band_ends,
                    )
                    if logical_row is not None and logical_column is not None:
                        logical_cells.add((logical_row, logical_column))
                if logical_cells:
                    members_list.append(segment)
                    logical_cells_by_segment[segment.segment_id] = frozenset(
                        logical_cells
                    )
            members = tuple(members_list)
            if not self._is_populated_table(
                members,
                logical_cells_by_segment,
            ):
                continue
            candidates.append(
                (
                    network_bbox,
                    members,
                    network_sparse_span,
                    logical_cells_by_segment,
                )
            )

        candidates.sort(
            key=lambda item: (
                item[0].top,
                item[0].left,
                item[0].bottom,
                item[0].right,
            )
        )
        owned: set[str] = set()
        drafts: list[_ObjectDraft] = []
        for (
            network_bbox,
            members,
            sparse_span,
            logical_cells_by_segment,
        ) in candidates:
            unowned = tuple(
                segment for segment in members if segment.segment_id not in owned
            )
            if not self._is_populated_table(
                unowned,
                logical_cells_by_segment,
            ):
                continue
            ordered = tuple(
                sorted(unowned, key=lambda item: self._segment_key(item, spans))
            )
            drafts.append(
                _ObjectDraft(
                    kind=ObjectKind.TABLE,
                    segments=ordered,
                    confidence=1.0,
                    evidence=(
                        "orthogonal-rule-grid",
                        "populated-logical-rule-grid",
                    ),
                    sparse_span=sparse_span,
                    structural_barrier=network_bbox,
                )
            )
            owned.update(segment.segment_id for segment in ordered)
        return tuple(drafts), frozenset(owned)

    @staticmethod
    def _network_sparse_span(
        *,
        horizontal: tuple[Rule, ...],
        vertical: tuple[Rule, ...],
        matrix: SparseSegmentMatrix,
    ) -> tuple[int, int, int, int]:
        row_top = min(rule.bbox.top for rule in horizontal)
        row_bottom = max(rule.bbox.bottom for rule in horizontal)
        column_left = min(rule.bbox.left for rule in vertical)
        column_right = max(rule.bbox.right for rule in vertical)
        rows = tuple(
            interval.index
            for interval in matrix.rows
            if interval.start < row_bottom and interval.end > row_top
        )
        columns = tuple(
            interval.index
            for interval in matrix.columns
            if interval.start < column_right and interval.end > column_left
        )
        if not rows or not columns:
            raise ObjectReconstructionInvariantError(
                "table rule network lies outside the sparse matrix"
            )
        return (
            min(rows),
            max(rows) + 1,
            min(columns),
            max(columns) + 1,
        )

    @staticmethod
    def _rules_intersect(horizontal: Box, vertical: Box) -> bool:
        return horizontal.intersection(vertical) is not None

    @staticmethod
    def _rule_band_count(rules: tuple[Rule, ...], axis: RuleAxis) -> int:
        return len(ObjectReconstructor._rule_bands(rules, axis))

    @staticmethod
    def _rule_bands(
        rules: tuple[Rule, ...],
        axis: RuleAxis,
    ) -> tuple[tuple[int, int], ...]:
        intervals = sorted(
            (
                (rule.bbox.top, rule.bbox.bottom)
                if axis is RuleAxis.HORIZONTAL
                else (rule.bbox.left, rule.bbox.right)
            )
            for rule in rules
        )
        merged: list[tuple[int, int]] = []
        for start, stop in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
                continue
            merged.append((start, stop))
        return tuple(merged)

    @staticmethod
    def _logical_lane(
        interval: AxisInterval,
        rule_bands: tuple[tuple[int, int], ...],
        rule_band_ends: tuple[int, ...],
    ) -> int | None:
        """Return the open lane between two contiguous rule bands.

        Stage 1 places every rule edge on the sparse axis, so one matrix
        interval cannot legitimately straddle two lanes.  Intervals on a rule
        band or outside the outer grid remain unassigned instead of being
        guessed into a neighbouring cell.
        """

        completed_bands = bisect_right(rule_band_ends, interval.start)
        if completed_bands < 1 or completed_bands >= len(rule_bands):
            return None
        lane = completed_bands - 1
        lane_start = rule_bands[lane][1]
        lane_stop = rule_bands[lane + 1][0]
        if interval.start < lane_start or interval.end > lane_stop:
            return None
        return lane

    @staticmethod
    def _is_page_frame_only(
        *,
        network_bbox: Box,
        horizontal: tuple[Rule, ...],
        vertical: tuple[Rule, ...],
        aligned_size: tuple[int, int],
    ) -> bool:
        width, height = aligned_size
        if network_bbox != Box(0, 0, width, height):
            return False
        interior_horizontal = any(
            rule.bbox.top > 0 and rule.bbox.bottom < height for rule in horizontal
        )
        interior_vertical = any(
            rule.bbox.left > 0 and rule.bbox.right < width for rule in vertical
        )
        return not (interior_horizontal and interior_vertical)

    def _is_populated_table(
        self,
        members: tuple[Segment, ...],
        logical_cells_by_segment: dict[str, frozenset[tuple[int, int]]],
    ) -> bool:
        """Require a populated 2-D witness inside explicit rule-grid lanes.

        Sparse matrix rows and columns are geometric cuts, not logical table
        coordinates.  Their Cartesian density therefore approaches zero for a
        large or irregular table.  Population is instead measured in the
        lanes between the candidate network's contiguous rule bands.  Three
        occupied logical cells are the conservative minimum that still admits
        a 2-by-2 grid with one merged cell.
        """

        if len(members) < 3:
            return False
        occupied = {
            logical_cell
            for member in members
            for logical_cell in logical_cells_by_segment.get(
                member.segment_id,
                frozenset(),
            )
        }
        row_indexes = {row for row, _ in occupied}
        column_indexes = {column for _, column in occupied}
        if len(row_indexes) < 2 or len(column_indexes) < 2:
            return False
        if len(occupied) < 3:
            return False
        minimum_witness = max(
            3,
            math.ceil(4.0 * self.config.table_min_occupancy),
        )
        return len(occupied) >= minimum_witness

    def _flow_drafts(
        self,
        segments: tuple[Segment, ...],
        *,
        table_barriers: tuple[Box, ...],
    ) -> tuple[_ObjectDraft, ...]:
        if not segments:
            return ()
        rows = self._visual_rows(segments)
        fragments = tuple(
            fragment
            for row_index, row_segments in enumerate(rows)
            for fragment in self._row_fragments(row_index, row_segments)
        )
        disjoint = _DisjointSet(len(fragments))
        by_row: dict[int, list[int]] = {}
        for index, fragment in enumerate(fragments):
            by_row.setdefault(fragment.row, []).append(index)
        pairwise_checks = 0
        for row_index in range(len(rows) - 1):
            for first in by_row.get(
                row_index, ()
            ):  # pragma: no branch - canonical rows
                for second in by_row.get(row_index + 1, ()):
                    pairwise_checks += 1
                    if pairwise_checks > self.config.max_pairwise_checks:
                        raise ObjectReconstructionLimitError(
                            "fragment pairwise checks exceed configured limit "
                            f"{self.config.max_pairwise_checks}"
                        )
                    if self._fragments_link(
                        fragments[first],
                        fragments[second],
                        table_barriers=table_barriers,
                    ):
                        disjoint.union(first, second)
        components: dict[int, list[_Fragment]] = {}
        for index, fragment in enumerate(fragments):
            components.setdefault(disjoint.find(index), []).append(fragment)
        drafts = []
        for component in components.values():
            ordered_fragments = tuple(
                sorted(component, key=lambda item: (item.row, item.bbox.left))
            )
            component_segments = tuple(
                segment
                for fragment in ordered_fragments
                for segment in fragment.segments
            )
            if self._inside_table_barrier(
                ordered_fragments,
                table_barriers,
            ):
                kind, confidence, evidence = (
                    ObjectKind.UNKNOWN,
                    0.0,
                    (
                        "unowned-orthogonal-rule-region",
                        "insufficient-logical-lane-evidence",
                    ),
                )
            else:
                kind, confidence, evidence = self._classify_flow(
                    ordered_fragments
                )
            drafts.append(
                _ObjectDraft(
                    kind=kind,
                    segments=component_segments,
                    confidence=confidence,
                    evidence=evidence,
                )
            )
        return tuple(drafts)

    @staticmethod
    def _inside_table_barrier(
        fragments: tuple[_Fragment, ...],
        table_barriers: tuple[Box, ...],
    ) -> bool:
        return any(
            barrier.contains_point(*fragment.bbox.center)
            for barrier in table_barriers
            for fragment in fragments
        )

    def _visual_rows(
        self,
        segments: tuple[Segment, ...],
    ) -> tuple[tuple[Segment, ...], ...]:
        ordered = sorted(
            segments,
            key=lambda item: (
                item.bbox.top,
                item.bbox.left,
                item.bbox.bottom,
                item.segment_id,
            ),
        )
        rows: list[list[Segment]] = []
        for segment in ordered:
            if rows and self._same_visual_row(rows[-1], segment.bbox):
                rows[-1].append(segment)
            else:
                rows.append([segment])
        return tuple(
            tuple(sorted(row, key=lambda item: (item.bbox.left, item.segment_id)))
            for row in rows
        )

    def _same_visual_row(
        self,
        row: list[Segment],
        candidate: Box,
    ) -> bool:
        """Compare with a robust row representative, never its bbox union.

        A union grows transitively: one tall logo, merged cell or multi-line
        segment can overlap many independent rows and eventually make a whole
        page look like one visual line.  Median centre and height stay anchored
        to the already accepted peers.  A height outlier is deliberately
        capped to the smaller scale so it cannot bridge rows by itself.
        """

        row_center = float(
            median(
                (segment.bbox.top + segment.bbox.bottom) / 2.0
                for segment in row
            )
        )
        row_height = float(median(segment.bbox.height for segment in row))
        candidate_center = (candidate.top + candidate.bottom) / 2.0
        candidate_height = float(candidate.height)
        smaller_height = min(row_height, candidate_height)
        larger_height = max(row_height, candidate_height)
        if larger_height > 3.0 * smaller_height:
            scale = smaller_height
        else:
            scale = larger_height
        return abs(row_center - candidate_center) <= (
            self.config.row_center_tolerance * scale
        )

    def _row_fragments(
        self,
        row_index: int,
        segments: tuple[Segment, ...],
    ) -> tuple[_Fragment, ...]:
        typical_height = float(median(item.bbox.height for item in segments))
        threshold = max(12.0, self.config.fragment_gap_heights * typical_height)
        groups: list[list[Segment]] = []
        for segment in segments:
            if groups and segment.bbox.left - groups[-1][-1].bbox.right > threshold:
                groups.append([segment])
            elif groups:
                groups[-1].append(segment)
            else:
                groups.append([segment])
        return tuple(
            _Fragment(
                row=row_index,
                segments=tuple(group),
                bbox=Box.union(item.bbox for item in group),
            )
            for group in groups
        )

    def _fragments_link(
        self,
        first: _Fragment,
        second: _Fragment,
        *,
        table_barriers: tuple[Box, ...],
    ) -> bool:
        corridor_left = min(first.bbox.left, second.bbox.left)
        corridor_right = max(first.bbox.right, second.bbox.right)
        first_center = (first.bbox.top + first.bbox.bottom) / 2.0
        second_center = (second.bbox.top + second.bbox.bottom) / 2.0
        for barrier in table_barriers:
            if corridor_left >= barrier.right or corridor_right <= barrier.left:
                continue
            first_region = (
                -1
                if first_center < barrier.top
                else 1 if first_center >= barrier.bottom else 0
            )
            second_region = (
                -1
                if second_center < barrier.top
                else 1 if second_center >= barrier.bottom else 0
            )
            if first_region != second_region:
                return False
        vertical_gap = max(0, second.bbox.top - first.bbox.bottom)
        smaller_height = float(min(first.bbox.height, second.bbox.height))
        scale = smaller_height
        if vertical_gap > max(12.0, self.config.adjacent_row_gap_heights * scale):
            return False
        horizontal_overlap = min(first.bbox.right, second.bbox.right) - max(
            first.bbox.left, second.bbox.left
        )
        aligned_left = abs(first.bbox.left - second.bbox.left) <= max(
            4.0, self.config.aligned_edge_heights * scale
        )
        return horizontal_overlap > 0 or aligned_left

    def _classify_flow(
        self,
        fragments: tuple[_Fragment, ...],
    ) -> tuple[ObjectKind, float, tuple[str, ...]]:
        if self._is_list(fragments):
            return ObjectKind.LIST, 0.95, ("repeated-marker-body-rows",)
        if self._is_paragraph(fragments):
            return (
                ObjectKind.PARAGRAPH,
                0.85,
                (
                    "connected-multiline-flow",
                    "single-fragment-per-visual-row",
                ),
            )
        return (
            ObjectKind.UNKNOWN,
            0.0,
            (
                "insufficient-structural-evidence",
                "ambiguous-image-free-flow",
            ),
        )

    @staticmethod
    def _is_paragraph(fragments: tuple[_Fragment, ...]) -> bool:
        """Accept only an unambiguous connected multi-line flow.

        A single raster line can be a heading, caption, field or paragraph and
        remains UNKNOWN.  Multiple fragments on one visual row can represent
        columns or key/value structure and also remain UNKNOWN.  The flow graph
        has already proved connectivity across adjacent visual rows; this check
        only promotes the narrow one-fragment-per-row case.
        """

        by_row: dict[int, int] = {}
        for fragment in fragments:
            by_row[fragment.row] = by_row.get(fragment.row, 0) + 1
        if len(by_row) < 2 or any(count != 1 for count in by_row.values()):
            return False
        ordered_rows = tuple(sorted(by_row))
        return ordered_rows == tuple(
            range(ordered_rows[0], ordered_rows[-1] + 1)
        )

    @staticmethod
    def _is_list(fragments: tuple[_Fragment, ...]) -> bool:
        if len(fragments) < 2 or any(len(item.segments) < 2 for item in fragments):
            return False
        markers = tuple(item.segments[0] for item in fragments)
        bodies = tuple(item.segments[1] for item in fragments)
        scale = float(median(item.bbox.height for item in markers + bodies))
        if any(
            marker.bbox.width > max(2.0 * scale, body.bbox.width * 0.35)
            or body.bbox.width < marker.bbox.width * 2.0
            for marker, body in zip(markers, bodies)
        ):
            return False
        marker_lefts = tuple(item.bbox.left for item in markers)
        body_lefts = tuple(item.bbox.left for item in bodies)
        return (
            max(marker_lefts) - min(marker_lefts) <= max(4.0, scale)
            and max(body_lefts) - min(body_lefts) <= max(4.0, scale)
            and all(
                marker.bbox.right < body.bbox.left
                for marker, body in zip(markers, bodies)
            )
        )

    def _finish_draft(
        self,
        draft: _ObjectDraft,
        *,
        index: int,
        spans: dict[str, SegmentSpan],
    ) -> DocumentObject:
        ordered_segments = tuple(
            sorted(draft.segments, key=lambda item: self._segment_key(item, spans))
        )
        owned_spans = tuple(spans[item.segment_id] for item in ordered_segments)
        if draft.sparse_span is None:
            row_start = min(item.row_start for item in owned_spans)
            row_stop = max(item.row_stop for item in owned_spans)
            column_start = min(item.column_start for item in owned_spans)
            column_stop = max(item.column_stop for item in owned_spans)
        else:
            row_start, row_stop, column_start, column_stop = draft.sparse_span
            if any(
                item.row_start < row_start
                or item.row_stop > row_stop
                or item.column_start < column_start
                or item.column_stop > column_stop
                for item in owned_spans
            ):
                raise ObjectReconstructionInvariantError(
                    "table rule span does not contain every owned segment"
                )
        return DocumentObject(
            object_id=f"object-{index:06d}",
            kind=draft.kind,
            segment_ids=tuple(item.segment_id for item in ordered_segments),
            bbox=Box.union(item.bbox for item in ordered_segments),
            reading_index=index,
            row_start=row_start,
            row_stop=row_stop,
            column_start=column_start,
            column_stop=column_stop,
            confidence=draft.confidence,
            evidence=draft.evidence,
        )
