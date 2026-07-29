from __future__ import annotations

import json
import subprocess
import sys
import tracemalloc
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path

import pytest

from app.sparse_pipeline.contracts import (
    AxisInterval,
    Box,
    Rule,
    RuleAxis,
    Segment,
    SegmentKind,
    SegmentSpan,
    SparseCell,
    SparseSegmentMatrix,
)
from app.sparse_pipeline.object_artifacts import ObjectArtifactWriter
from app.sparse_pipeline.object_reconstruction import (
    DocumentObject,
    ObjectKind,
    ObjectReconstructionConfig,
    ObjectReconstructionInvariantError,
    ObjectReconstructionLimitError,
    ObjectReconstructionResult,
    ObjectReconstructor,
)


@dataclass(frozen=True)
class ObjectFixture:
    aligned_size: tuple[int, int]
    segments: tuple[Segment, ...]
    rules: tuple[Rule, ...]
    matrix: SparseSegmentMatrix


def _segment(
    segment_id: str,
    bbox: Box,
    *,
    row_index: int,
) -> Segment:
    return Segment(
        segment_id=segment_id,
        bbox=bbox,
        source_bbox=bbox,
        kind=SegmentKind.TEXT,
        ink_pixels=max(1, bbox.area // 3),
        row_index=row_index,
        order_key=(bbox.top, bbox.left),
        parent_path=("fixture-root",),
    )


def _horizontal_rule(rule_id: str, width: int, top: int, bottom: int) -> Rule:
    bbox = Box(0, top, width, bottom)
    return Rule(
        rule_id=rule_id,
        bbox=bbox,
        source_bbox=bbox,
        axis=RuleAxis.HORIZONTAL,
        foreground_pixels=max(1, bbox.area // 2),
        strength=1.0,
    )


def _vertical_rule(
    rule_id: str,
    height: int,
    left: int,
    right: int,
) -> Rule:
    bbox = Box(left, 0, right, height)
    return Rule(
        rule_id=rule_id,
        bbox=bbox,
        source_bbox=bbox,
        axis=RuleAxis.VERTICAL,
        foreground_pixels=max(1, bbox.area // 2),
        strength=1.0,
    )


def _scoped_vertical_rule(
    rule_id: str,
    top: int,
    bottom: int,
    left: int,
    right: int,
) -> Rule:
    bbox = Box(left, top, right, bottom)
    return Rule(
        rule_id=rule_id,
        bbox=bbox,
        source_bbox=bbox,
        axis=RuleAxis.VERTICAL,
        foreground_pixels=max(1, bbox.area // 2),
        strength=1.0,
    )


def _matrix(
    *,
    row_edges: tuple[int, ...],
    column_edges: tuple[int, ...],
    placements: dict[str, tuple[tuple[int, int], ...]],
    horizontal_rule_rows: tuple[int, ...] = (),
    vertical_rule_columns: tuple[int, ...] = (),
) -> SparseSegmentMatrix:
    rows = tuple(AxisInterval(index, start, end) for index, (start, end) in enumerate(zip(row_edges, row_edges[1:])))
    columns = tuple(
        AxisInterval(index, start, end) for index, (start, end) in enumerate(zip(column_edges, column_edges[1:]))
    )
    cells = tuple(
        sorted(
            (
                SparseCell(row=row, column=column, segment_id=segment_id)
                for segment_id, coordinates in placements.items()
                for row, column in coordinates
            ),
            key=lambda cell: (cell.row, cell.column, cell.segment_id),
        )
    )
    spans = []
    for segment_id, coordinates in placements.items():
        segment_rows = tuple(row for row, _ in coordinates)
        segment_columns = tuple(column for _, column in coordinates)
        spans.append(
            SegmentSpan(
                segment_id=segment_id,
                row_start=min(segment_rows),
                row_stop=max(segment_rows) + 1,
                column_start=min(segment_columns),
                column_stop=max(segment_columns) + 1,
            )
        )
    return SparseSegmentMatrix(
        rows=rows,
        columns=columns,
        cells=cells,
        spans=tuple(spans),
        horizontal_rule_rows=horizontal_rule_rows,
        vertical_rule_columns=vertical_rule_columns,
    )


def _paragraph_fixture() -> ObjectFixture:
    segments = (
        _segment("p-0", Box(10, 2, 190, 18), row_index=0),
        _segment("p-1", Box(12, 32, 180, 48), row_index=1),
        _segment("p-2", Box(11, 62, 185, 78), row_index=2),
    )
    return ObjectFixture(
        aligned_size=(200, 90),
        segments=segments,
        rules=(),
        matrix=_matrix(
            row_edges=(0, 20, 30, 50, 60, 80, 90),
            column_edges=(0, 200),
            placements={"p-0": ((0, 0),), "p-1": ((2, 0),), "p-2": ((4, 0),)},
        ),
    )


def _list_fixture() -> ObjectFixture:
    segments = tuple(
        segment
        for row, top in enumerate((2, 32, 62))
        for segment in (
            _segment(f"marker-{row}", Box(5, top, 15, top + 16), row_index=row),
            _segment(f"item-{row}", Box(35, top, 220, top + 16), row_index=row),
        )
    )
    placements = {
        segment_id: ((row, column),)
        for row, pair in enumerate(
            (
                ("marker-0", "item-0"),
                ("marker-1", "item-1"),
                ("marker-2", "item-2"),
            )
        )
        for column, segment_id in enumerate(pair)
    }
    return ObjectFixture(
        aligned_size=(240, 100),
        segments=segments,
        rules=(),
        matrix=_matrix(
            row_edges=(0, 20, 30, 50, 60, 80, 100),
            column_edges=(0, 30, 240),
            placements={segment_id: ((2 * row, column),) for segment_id, ((row, column),) in placements.items()},
        ),
    )


def _table_fixture() -> ObjectFixture:
    width, height = 166, 46
    segments = (
        _segment("t-00", Box(3, 3, 20, 20), row_index=0),
        _segment("t-01", Box(25, 3, 160, 20), row_index=0),
        _segment("t-10", Box(3, 25, 20, 42), row_index=1),
        _segment("t-11", Box(25, 25, 160, 42), row_index=1),
    )
    rules = (
        _horizontal_rule("h-0", width, 0, 2),
        _horizontal_rule("h-1", width, 22, 24),
        _horizontal_rule("h-2", width, 44, 46),
        _vertical_rule("v-0", height, 0, 2),
        _vertical_rule("v-1", height, 22, 24),
        _vertical_rule("v-2", height, 164, 166),
    )
    return ObjectFixture(
        aligned_size=(width, height),
        segments=segments,
        rules=rules,
        matrix=_matrix(
            row_edges=(0, 2, 22, 24, 44, 46),
            column_edges=(0, 2, 22, 24, 164, 166),
            placements={
                "t-00": ((1, 1),),
                "t-01": ((1, 3),),
                "t-10": ((3, 1),),
                "t-11": ((3, 3),),
            },
            horizontal_rule_rows=(0, 2, 4),
            vertical_rule_columns=(0, 2, 4),
        ),
    )


def _unknown_fixture() -> ObjectFixture:
    segment = _segment("u-0", Box(15, 12, 65, 30), row_index=0)
    return ObjectFixture(
        aligned_size=(100, 50),
        segments=(segment,),
        rules=(),
        matrix=_matrix(
            row_edges=(0, 50),
            column_edges=(0, 100),
            placements={"u-0": ((0, 0),)},
        ),
    )


def _two_column_paragraph_fixture() -> ObjectFixture:
    segments = tuple(
        segment
        for row, top in enumerate((2, 32, 62))
        for segment in (
            _segment(f"left-{row}", Box(5, top, 88, top + 16), row_index=row),
            _segment(f"right-{row}", Box(122, top, 212, top + 16), row_index=row),
        )
    )
    return ObjectFixture(
        aligned_size=(220, 90),
        segments=segments,
        rules=(),
        matrix=_matrix(
            row_edges=(0, 20, 30, 50, 60, 80, 90),
            column_edges=(0, 95, 115, 220),
            placements={
                **{f"left-{row}": ((2 * row, 0),) for row in range(3)},
                **{f"right-{row}": ((2 * row, 2),) for row in range(3)},
            },
        ),
    )


def _mixed_table_then_paragraph_fixture() -> ObjectFixture:
    width, table_bottom, height = 162, 42, 102
    table_segments = (
        _segment("table-00", Box(3, 3, 19, 18), row_index=0),
        _segment("table-01", Box(23, 3, 158, 18), row_index=0),
        _segment("table-10", Box(3, 23, 19, 38), row_index=1),
        _segment("table-11", Box(23, 23, 158, 38), row_index=1),
    )
    paragraph_segments = (
        _segment("after-0", Box(5, 53, 157, 65), row_index=2),
        _segment("after-1", Box(6, 71, 150, 83), row_index=3),
        _segment("after-2", Box(5, 89, 155, 101), row_index=4),
    )
    rules = (
        _horizontal_rule("table-h-0", width, 0, 2),
        _horizontal_rule("table-h-1", width, 20, 22),
        _horizontal_rule("table-h-2", width, 40, 42),
        _scoped_vertical_rule("table-v-0", 0, table_bottom, 0, 2),
        _scoped_vertical_rule("table-v-1", 0, table_bottom, 20, 22),
        _scoped_vertical_rule("table-v-2", 0, table_bottom, 160, 162),
    )
    return ObjectFixture(
        aligned_size=(width, height),
        segments=table_segments + paragraph_segments,
        rules=rules,
        matrix=_matrix(
            row_edges=(0, 2, 20, 22, 40, 42, 52, 66, 70, 84, 88, 102),
            column_edges=(0, 2, 20, 22, 160, 162),
            placements={
                "table-00": ((1, 1),),
                "table-01": ((1, 3),),
                "table-10": ((3, 1),),
                "table-11": ((3, 3),),
                "after-0": ((6, 1), (6, 2), (6, 3)),
                "after-1": ((8, 1), (8, 2), (8, 3)),
                "after-2": ((10, 1), (10, 2), (10, 3)),
            },
            horizontal_rule_rows=(0, 2, 4),
            vertical_rule_columns=(0, 2, 4),
        ),
    )


def _reconstruct(
    fixture: ObjectFixture,
    config: ObjectReconstructionConfig | None = None,
) -> ObjectReconstructionResult:
    return ObjectReconstructor(config).reconstruct(
        aligned_size=fixture.aligned_size,
        segments=fixture.segments,
        rules=fixture.rules,
        matrix=fixture.matrix,
    )


def _assert_exact_partition(
    result: ObjectReconstructionResult,
    expected_segment_ids: tuple[str, ...],
) -> None:
    owned = tuple(segment_id for document_object in result.objects for segment_id in document_object.segment_ids)
    assert len(expected_segment_ids) == len(set(expected_segment_ids))
    assert len(owned) == len(expected_segment_ids)
    assert len(owned) == len(set(owned))
    assert set(owned) == set(expected_segment_ids)


@pytest.mark.parametrize(
    ("fixture_factory", "expected_kind", "expected_segment_ids"),
    (
        (_paragraph_fixture, ObjectKind.PARAGRAPH, ("p-0", "p-1", "p-2")),
        (
            _list_fixture,
            ObjectKind.LIST,
            (
                "marker-0",
                "item-0",
                "marker-1",
                "item-1",
                "marker-2",
                "item-2",
            ),
        ),
        (_table_fixture, ObjectKind.TABLE, ("t-00", "t-01", "t-10", "t-11")),
        (_unknown_fixture, ObjectKind.UNKNOWN, ("u-0",)),
    ),
)
def test_exact_image_free_object_classification(
    fixture_factory: object,
    expected_kind: ObjectKind,
    expected_segment_ids: tuple[str, ...],
) -> None:
    fixture = fixture_factory()

    result = _reconstruct(fixture)

    assert result.aligned_size == fixture.aligned_size
    assert len(result.objects) == 1
    document_object = result.objects[0]
    assert isinstance(document_object, DocumentObject)
    assert document_object.object_id == "object-000000"
    assert document_object.kind is expected_kind
    assert document_object.segment_ids == expected_segment_ids
    assert document_object.bbox == Box.union(segment.bbox for segment in fixture.segments)
    assert document_object.reading_index == 0
    _assert_exact_partition(result, tuple(segment.segment_id for segment in fixture.segments))


def test_table_evidence_wins_over_marker_like_first_column_and_is_scoped() -> None:
    fixture = _mixed_table_then_paragraph_fixture()

    result = _reconstruct(fixture)

    assert tuple(document_object.kind for document_object in result.objects) == (
        ObjectKind.TABLE,
        ObjectKind.PARAGRAPH,
    )
    assert result.objects[0].segment_ids == (
        "table-00",
        "table-01",
        "table-10",
        "table-11",
    )
    assert result.objects[1].segment_ids == ("after-0", "after-1", "after-2")
    assert tuple(document_object.reading_index for document_object in result.objects) == (
        0,
        1,
    )
    _assert_exact_partition(result, tuple(segment.segment_id for segment in fixture.segments))


def test_one_pixel_scoped_grid_is_sufficient_table_evidence() -> None:
    width, height = 101, 41
    segments = (
        _segment("cell-00", Box(15, 8, 40, 18), row_index=0),
        _segment("cell-01", Box(55, 8, 85, 18), row_index=0),
        _segment("cell-10", Box(15, 23, 40, 33), row_index=1),
        _segment("cell-11", Box(55, 23, 85, 33), row_index=1),
    )
    horizontal = tuple(
        Rule(
            rule_id=f"thin-h-{index}",
            bbox=Box(10, top, 91, top + 1),
            source_bbox=Box(10, top, 91, top + 1),
            axis=RuleAxis.HORIZONTAL,
            foreground_pixels=81,
            strength=1.0,
        )
        for index, top in enumerate((5, 20, 35))
    )
    vertical = tuple(
        _scoped_vertical_rule(f"thin-v-{index}", 5, 36, left, left + 1) for index, left in enumerate((10, 50, 90))
    )
    fixture = ObjectFixture(
        aligned_size=(width, height),
        segments=segments,
        rules=horizontal + vertical,
        matrix=_matrix(
            row_edges=(0, 5, 6, 20, 21, 35, 36, 41),
            column_edges=(0, 10, 11, 50, 51, 90, 91, 101),
            placements={
                "cell-00": ((2, 2),),
                "cell-01": ((2, 4),),
                "cell-10": ((4, 2),),
                "cell-11": ((4, 4),),
            },
            horizontal_rule_rows=(1, 3, 5),
            vertical_rule_columns=(1, 3, 5),
        ),
    )

    result = _reconstruct(fixture)

    assert len(result.objects) == 1
    assert result.objects[0].kind is ObjectKind.TABLE
    _assert_exact_partition(result, tuple(item.segment_id for item in segments))


def test_multiple_segments_in_one_table_cell_are_all_owned_by_the_table() -> None:
    fixture = _table_fixture()
    split_segments = (
        _segment("t-00-a", Box(3, 3, 10, 20), row_index=0),
        _segment("t-00-b", Box(12, 3, 20, 20), row_index=0),
    ) + fixture.segments[1:]
    split_fixture = ObjectFixture(
        aligned_size=fixture.aligned_size,
        segments=split_segments,
        rules=fixture.rules,
        matrix=_matrix(
            row_edges=(0, 2, 22, 24, 44, 46),
            column_edges=(0, 2, 22, 24, 164, 166),
            placements={
                "t-00-a": ((1, 1),),
                "t-00-b": ((1, 1),),
                "t-01": ((1, 3),),
                "t-10": ((3, 1),),
                "t-11": ((3, 3),),
            },
            horizontal_rule_rows=(0, 2, 4),
            vertical_rule_columns=(0, 2, 4),
        ),
    )

    result = _reconstruct(split_fixture)

    assert len(result.objects) == 1
    assert result.objects[0].kind is ObjectKind.TABLE
    assert result.objects[0].segment_ids == (
        "t-00-a",
        "t-00-b",
        "t-01",
        "t-10",
        "t-11",
    )
    _assert_exact_partition(result, tuple(item.segment_id for item in split_segments))


def test_logical_rule_lanes_survive_low_raw_sparse_axis_density() -> None:
    """Geometric matrix cuts are not logical table rows or columns."""

    segments = (
        _segment("cell-00", Box(10, 10, 20, 20), row_index=0),
        _segment("cell-01", Box(55, 30, 65, 40), row_index=1),
        _segment("cell-10", Box(30, 55, 40, 65), row_index=2),
        _segment("cell-11", Box(75, 75, 85, 85), row_index=3),
    )
    horizontal = tuple(
        Rule(
            rule_id=f"sparse-h-{index}",
            bbox=Box(5, top, 96, top + 1),
            source_bbox=Box(5, top, 96, top + 1),
            axis=RuleAxis.HORIZONTAL,
            foreground_pixels=91,
            strength=1.0,
        )
        for index, top in enumerate((5, 50, 95))
    )
    vertical = tuple(
        _scoped_vertical_rule(
            f"sparse-v-{index}",
            5,
            96,
            left,
            left + 1,
        )
        for index, left in enumerate((5, 50, 95))
    )
    fixture = ObjectFixture(
        aligned_size=(100, 100),
        segments=segments,
        rules=horizontal + vertical,
        matrix=_matrix(
            row_edges=(0, 5, 6, 25, 45, 50, 51, 70, 90, 95, 96, 100),
            column_edges=(0, 5, 6, 25, 45, 50, 51, 70, 90, 95, 96, 100),
            placements={
                "cell-00": ((2, 2),),
                "cell-01": ((3, 6),),
                "cell-10": ((6, 3),),
                "cell-11": ((7, 7),),
            },
            horizontal_rule_rows=(1, 5, 9),
            vertical_rule_columns=(1, 5, 9),
        ),
    )

    result = _reconstruct(fixture)

    assert len(result.objects) == 1
    assert result.objects[0].kind is ObjectKind.TABLE
    assert result.objects[0].segment_ids == tuple(
        segment.segment_id for segment in segments
    )


def test_merged_cell_is_one_table_without_joining_logical_lanes() -> None:
    segments = (
        _segment("merged-top", Box(20, 20, 45, 35), row_index=0),
        _segment("bottom-left", Box(20, 60, 40, 75), row_index=1),
        _segment("bottom-right", Box(60, 60, 80, 75), row_index=1),
    )
    horizontal = tuple(
        Rule(
            rule_id=f"merged-h-{index}",
            bbox=Box(5, top, 96, top + 1),
            source_bbox=Box(5, top, 96, top + 1),
            axis=RuleAxis.HORIZONTAL,
            foreground_pixels=91,
            strength=1.0,
        )
        for index, top in enumerate((5, 50, 95))
    )
    vertical = (
        _scoped_vertical_rule("merged-v-left", 5, 96, 5, 6),
        _scoped_vertical_rule("merged-v-middle", 50, 96, 50, 51),
        _scoped_vertical_rule("merged-v-right", 5, 96, 95, 96),
    )
    fixture = ObjectFixture(
        aligned_size=(100, 100),
        segments=segments,
        rules=horizontal + vertical,
        matrix=_matrix(
            row_edges=(0, 5, 6, 50, 51, 95, 96, 100),
            column_edges=(0, 5, 6, 50, 51, 95, 96, 100),
            placements={
                "merged-top": ((2, 2),),
                "bottom-left": ((4, 2),),
                "bottom-right": ((4, 4),),
            },
            horizontal_rule_rows=(1, 3, 5),
            vertical_rule_columns=(1, 3, 5),
        ),
    )

    result = _reconstruct(fixture)

    assert len(result.objects) == 1
    assert result.objects[0].kind is ObjectKind.TABLE
    assert result.objects[0].segment_ids == (
        "merged-top",
        "bottom-left",
        "bottom-right",
    )


def test_two_vertically_consecutive_rule_networks_remain_two_tables() -> None:
    segments = tuple(
        _segment(
            f"{table}-{row}{column}",
            Box(left, top, right, top + 12),
            row_index=table_index * 2 + row,
        )
        for table_index, (table, table_top) in enumerate(
            (("first", 8), ("second", 68))
        )
        for row, top in enumerate((table_top, table_top + 20))
        for column, (left, right) in enumerate(((10, 35), (50, 85)))
    )
    horizontal = tuple(
        Rule(
            rule_id=f"{table}-h-{index}",
            bbox=Box(5, top, 96, top + 1),
            source_bbox=Box(5, top, 96, top + 1),
            axis=RuleAxis.HORIZONTAL,
            foreground_pixels=91,
            strength=1.0,
        )
        for table, tops in (
            ("first", (5, 25, 45)),
            ("second", (65, 85, 105)),
        )
        for index, top in enumerate(tops)
    )
    vertical = tuple(
        _scoped_vertical_rule(
            f"{table}-v-{index}",
            top,
            bottom,
            left,
            left + 1,
        )
        for table, top, bottom in (
            ("first", 5, 46),
            ("second", 65, 106),
        )
        for index, left in enumerate((5, 45, 95))
    )
    fixture = ObjectFixture(
        aligned_size=(120, 115),
        segments=segments,
        rules=horizontal + vertical,
        matrix=_matrix(
            row_edges=(
                0,
                5,
                6,
                25,
                26,
                45,
                46,
                65,
                66,
                85,
                86,
                105,
                106,
                115,
            ),
            column_edges=(0, 5, 6, 45, 46, 95, 96, 120),
            placements={
                "first-00": ((2, 2),),
                "first-01": ((2, 4),),
                "first-10": ((4, 2),),
                "first-11": ((4, 4),),
                "second-00": ((8, 2),),
                "second-01": ((8, 4),),
                "second-10": ((10, 2),),
                "second-11": ((10, 4),),
            },
            horizontal_rule_rows=(1, 3, 5, 7, 9, 11),
            vertical_rule_columns=(1, 3, 5),
        ),
    )

    result = _reconstruct(fixture)

    assert tuple(item.kind for item in result.objects) == (
        ObjectKind.TABLE,
        ObjectKind.TABLE,
    )
    assert result.objects[0].segment_ids == (
        "first-00",
        "first-01",
        "first-10",
        "first-11",
    )
    assert result.objects[1].segment_ids == (
        "second-00",
        "second-01",
        "second-10",
        "second-11",
    )


def test_half_open_rules_that_only_touch_do_not_form_a_table_grid() -> None:
    segments = (
        _segment("left-0", Box(10, 40, 30, 50), row_index=0),
        _segment("right-0", Box(53, 40, 70, 50), row_index=0),
        _segment("left-1", Box(10, 60, 30, 70), row_index=1),
        _segment("right-1", Box(53, 60, 70, 70), row_index=1),
    )
    horizontal = tuple(
        Rule(
            rule_id=f"touch-h-{index}",
            bbox=Box(0, top, 50, top + 1),
            source_bbox=Box(0, top, 50, top + 1),
            axis=RuleAxis.HORIZONTAL,
            foreground_pixels=50,
            strength=1.0,
        )
        for index, top in enumerate((10, 20, 30))
    )
    vertical = tuple(_scoped_vertical_rule(f"touch-v-{index}", 0, 100, 50, 51 + index) for index in range(3))
    fixture = ObjectFixture(
        aligned_size=(100, 100),
        segments=segments,
        rules=horizontal + vertical,
        matrix=_matrix(
            row_edges=(0, 10, 11, 20, 21, 30, 31, 40, 50, 60, 70, 100),
            column_edges=(0, 10, 30, 50, 51, 52, 53, 70, 100),
            placements={
                "left-0": ((7, 1),),
                "right-0": ((7, 6),),
                "left-1": ((9, 1),),
                "right-1": ((9, 6),),
            },
            horizontal_rule_rows=(1, 3, 5),
            vertical_rule_columns=(3, 4, 5),
        ),
    )

    result = _reconstruct(fixture)

    assert all(item.kind is not ObjectKind.TABLE for item in result.objects)
    _assert_exact_partition(result, tuple(item.segment_id for item in segments))


def test_coincident_rule_duplicates_are_not_a_structural_table_grid() -> None:
    segments = (
        _segment("value-00", Box(15, 15, 35, 35), row_index=0),
        _segment("value-01", Box(55, 15, 75, 35), row_index=0),
        _segment("value-10", Box(15, 55, 35, 75), row_index=1),
        _segment("value-11", Box(55, 55, 75, 75), row_index=1),
    )
    horizontal_box = Box(10, 10, 90, 11)
    vertical_box = Box(10, 10, 11, 90)
    horizontal = tuple(
        Rule(
            rule_id=f"duplicate-h-{index}",
            bbox=horizontal_box,
            source_bbox=horizontal_box,
            axis=RuleAxis.HORIZONTAL,
            foreground_pixels=80,
            strength=1.0,
        )
        for index in range(3)
    )
    vertical = tuple(
        Rule(
            rule_id=f"duplicate-v-{index}",
            bbox=vertical_box,
            source_bbox=vertical_box,
            axis=RuleAxis.VERTICAL,
            foreground_pixels=80,
            strength=1.0,
        )
        for index in range(3)
    )
    fixture = ObjectFixture(
        aligned_size=(100, 100),
        segments=segments,
        rules=horizontal + vertical,
        matrix=_matrix(
            row_edges=(0, 10, 11, 40, 50, 80, 100),
            column_edges=(0, 10, 11, 40, 50, 80, 100),
            placements={
                "value-00": ((2, 2),),
                "value-01": ((2, 4),),
                "value-10": ((4, 2),),
                "value-11": ((4, 4),),
            },
            horizontal_rule_rows=(1,),
            vertical_rule_columns=(1,),
        ),
    )

    result = _reconstruct(fixture)

    assert all(item.kind is not ObjectKind.TABLE for item in result.objects)
    _assert_exact_partition(result, tuple(item.segment_id for item in segments))


def test_overlapping_near_coincident_rule_bands_do_not_inflate_grid_rank() -> None:
    segments = (
        _segment("near-00", Box(20, 20, 35, 35), row_index=0),
        _segment("near-01", Box(55, 20, 75, 35), row_index=0),
        _segment("near-10", Box(20, 55, 35, 75), row_index=1),
        _segment("near-11", Box(55, 55, 75, 75), row_index=1),
    )
    horizontal = tuple(
        Rule(
            rule_id=f"near-h-{index}",
            bbox=Box(10, top, 90, top + 2),
            source_bbox=Box(10, top, 90, top + 2),
            axis=RuleAxis.HORIZONTAL,
            foreground_pixels=80,
            strength=1.0,
        )
        for index, top in enumerate((10, 11, 12))
    )
    vertical = tuple(
        Rule(
            rule_id=f"near-v-{index}",
            bbox=Box(left, 10, left + 2, 90),
            source_bbox=Box(left, 10, left + 2, 90),
            axis=RuleAxis.VERTICAL,
            foreground_pixels=80,
            strength=1.0,
        )
        for index, left in enumerate((10, 11, 12))
    )
    fixture = ObjectFixture(
        aligned_size=(100, 100),
        segments=segments,
        rules=horizontal + vertical,
        matrix=_matrix(
            row_edges=(0, 10, 11, 12, 13, 14, 40, 50, 80, 100),
            column_edges=(0, 10, 11, 12, 13, 14, 40, 50, 80, 100),
            placements={
                "near-00": ((5, 5),),
                "near-01": ((5, 7),),
                "near-10": ((7, 5),),
                "near-11": ((7, 7),),
            },
            horizontal_rule_rows=(1, 2, 3, 4),
            vertical_rule_columns=(1, 2, 3, 4),
        ),
    )

    result = _reconstruct(fixture)

    assert all(item.kind is not ObjectKind.TABLE for item in result.objects)
    _assert_exact_partition(result, tuple(item.segment_id for item in segments))


def test_two_scoped_side_by_side_grids_remain_two_tables() -> None:
    width, height = 220, 60
    segments = (
        _segment("left-00", Box(10, 8, 35, 20), row_index=0),
        _segment("left-01", Box(50, 8, 85, 20), row_index=0),
        _segment("left-10", Box(10, 28, 35, 40), row_index=1),
        _segment("left-11", Box(50, 28, 85, 40), row_index=1),
        _segment("right-00", Box(130, 8, 155, 20), row_index=0),
        _segment("right-01", Box(170, 8, 205, 20), row_index=0),
        _segment("right-10", Box(130, 28, 155, 40), row_index=1),
        _segment("right-11", Box(170, 28, 205, 40), row_index=1),
    )
    horizontal = tuple(
        Rule(
            rule_id=f"{side}-h-{index}",
            bbox=Box(left, top, right, top + 1),
            source_bbox=Box(left, top, right, top + 1),
            axis=RuleAxis.HORIZONTAL,
            foreground_pixels=right - left,
            strength=1.0,
        )
        for side, left, right in (("left", 5, 96), ("right", 125, 216))
        for index, top in enumerate((5, 25, 45))
    )
    vertical = tuple(
        _scoped_vertical_rule(f"{side}-v-{index}", 5, 46, left, left + 1)
        for side, positions in (
            ("left", (5, 45, 95)),
            ("right", (125, 165, 215)),
        )
        for index, left in enumerate(positions)
    )
    fixture = ObjectFixture(
        aligned_size=(width, height),
        segments=segments,
        rules=horizontal + vertical,
        matrix=_matrix(
            row_edges=(0, 5, 6, 25, 26, 45, 46, 60),
            column_edges=(
                0,
                5,
                6,
                45,
                46,
                95,
                96,
                125,
                126,
                165,
                166,
                215,
                216,
                220,
            ),
            placements={
                "left-00": ((2, 2),),
                "left-01": ((2, 4),),
                "left-10": ((4, 2),),
                "left-11": ((4, 4),),
                "right-00": ((2, 8),),
                "right-01": ((2, 10),),
                "right-10": ((4, 8),),
                "right-11": ((4, 10),),
            },
            horizontal_rule_rows=(1, 3, 5),
            vertical_rule_columns=(1, 3, 5, 7, 9, 11),
        ),
    )

    result = _reconstruct(fixture)

    assert tuple(item.kind for item in result.objects) == (
        ObjectKind.TABLE,
        ObjectKind.TABLE,
    )
    assert result.objects[0].segment_ids == (
        "left-00",
        "left-01",
        "left-10",
        "left-11",
    )
    assert result.objects[1].segment_ids == (
        "right-00",
        "right-01",
        "right-10",
        "right-11",
    )
    _assert_exact_partition(result, tuple(item.segment_id for item in segments))


def test_large_vertical_gap_splits_lines_into_two_paragraph_objects() -> None:
    segments = (
        _segment("first-0", Box(5, 0, 95, 12), row_index=0),
        _segment("first-1", Box(5, 20, 90, 32), row_index=1),
        _segment("second-0", Box(5, 100, 95, 112), row_index=2),
        _segment("second-1", Box(5, 120, 90, 132), row_index=3),
    )
    fixture = ObjectFixture(
        aligned_size=(100, 140),
        segments=segments,
        rules=(),
        matrix=_matrix(
            row_edges=(0, 12, 20, 32, 100, 112, 120, 132, 140),
            column_edges=(0, 100),
            placements={
                "first-0": ((0, 0),),
                "first-1": ((2, 0),),
                "second-0": ((4, 0),),
                "second-1": ((6, 0),),
            },
        ),
    )

    result = _reconstruct(fixture)

    assert tuple(item.kind for item in result.objects) == (
        ObjectKind.PARAGRAPH,
        ObjectKind.PARAGRAPH,
    )
    assert result.objects[0].segment_ids == ("first-0", "first-1")
    assert result.objects[1].segment_ids == ("second-0", "second-1")
    _assert_exact_partition(result, tuple(item.segment_id for item in segments))


def test_tall_segment_does_not_transitively_swallow_paragraph_rows() -> None:
    segments = (
        _segment("tall-aside", Box(5, 0, 20, 100), row_index=0),
        _segment("paragraph-0", Box(40, 10, 180, 22), row_index=1),
        _segment("paragraph-1", Box(40, 30, 175, 42), row_index=2),
    )
    fixture = ObjectFixture(
        aligned_size=(200, 110),
        segments=segments,
        rules=(),
        matrix=_matrix(
            row_edges=(0, 10, 22, 30, 42, 100, 110),
            column_edges=(0, 20, 40, 200),
            placements={
                "tall-aside": ((0, 0), (1, 0), (2, 0), (3, 0), (4, 0)),
                "paragraph-0": ((1, 2),),
                "paragraph-1": ((3, 2),),
            },
        ),
    )

    result = _reconstruct(fixture)

    assert tuple(item.kind for item in result.objects) == (
        ObjectKind.UNKNOWN,
        ObjectKind.PARAGRAPH,
    )
    assert result.objects[0].segment_ids == ("tall-aside",)
    assert result.objects[1].segment_ids == ("paragraph-0", "paragraph-1")


def test_equal_width_columns_are_two_paragraphs_not_merged_or_misclassified() -> None:
    fixture = _two_column_paragraph_fixture()

    result = _reconstruct(fixture)

    assert tuple(document_object.kind for document_object in result.objects) == (
        ObjectKind.PARAGRAPH,
        ObjectKind.PARAGRAPH,
    )
    assert result.objects[0].segment_ids == ("left-0", "left-1", "left-2")
    assert result.objects[1].segment_ids == ("right-0", "right-1", "right-2")
    _assert_exact_partition(result, tuple(segment.segment_id for segment in fixture.segments))


def test_connected_multiline_flow_with_one_fragment_per_row_is_a_paragraph() -> None:
    result = _reconstruct(_paragraph_fixture())

    assert len(result.objects) == 1
    assert result.objects[0].kind is ObjectKind.PARAGRAPH
    assert result.objects[0].confidence == 0.85
    assert result.objects[0].evidence == (
        "connected-multiline-flow",
        "single-fragment-per-visual-row",
    )


@pytest.mark.parametrize("line_height", (6, 30))
def test_two_touching_whole_lines_at_supported_scales_are_one_paragraph(
    line_height: int,
) -> None:
    height = line_height * 2
    segments = (
        _segment("line-0", Box(5, 0, 95, line_height), row_index=0),
        _segment("line-1", Box(5, line_height, 90, height), row_index=1),
    )
    fixture = ObjectFixture(
        aligned_size=(100, height),
        segments=segments,
        rules=(),
        matrix=_matrix(
            row_edges=(0, line_height, height),
            column_edges=(0, 100),
            placements={"line-0": ((0, 0),), "line-1": ((1, 0),)},
        ),
    )

    result = _reconstruct(fixture)

    assert len(result.objects) == 1
    assert result.objects[0].kind is ObjectKind.PARAGRAPH
    assert result.objects[0].segment_ids == ("line-0", "line-1")
    _assert_exact_partition(result, ("line-0", "line-1"))


def test_nearby_same_row_segments_form_one_paragraph_fragment_not_a_list() -> None:
    segments = tuple(
        segment
        for row, top in enumerate((0, 20, 40))
        for segment in (
            _segment(f"left-{row}", Box(5, top, 75, top + 12), row_index=row),
            _segment(f"right-{row}", Box(84, top, 170, top + 12), row_index=row),
        )
    )
    fixture = ObjectFixture(
        aligned_size=(180, 52),
        segments=segments,
        rules=(),
        matrix=_matrix(
            row_edges=(0, 12, 20, 32, 40, 52),
            column_edges=(0, 80, 180),
            placements={
                **{f"left-{row}": ((2 * row, 0),) for row in range(3)},
                **{f"right-{row}": ((2 * row, 1),) for row in range(3)},
            },
        ),
    )

    result = _reconstruct(fixture)

    assert len(result.objects) == 1
    assert result.objects[0].kind is ObjectKind.PARAGRAPH
    assert result.objects[0].segment_ids == (
        "left-0",
        "right-0",
        "left-1",
        "right-1",
        "left-2",
        "right-2",
    )
    _assert_exact_partition(result, tuple(item.segment_id for item in segments))


def test_single_visual_line_remains_unknown_not_a_guessed_paragraph() -> None:
    fixture = _unknown_fixture()

    result = _reconstruct(fixture)

    assert len(result.objects) == 1
    assert result.objects[0].kind is ObjectKind.UNKNOWN
    assert result.objects[0].confidence == 0.0
    assert result.objects[0].segment_ids == ("u-0",)
    _assert_exact_partition(result, ("u-0",))


def test_connected_component_with_multiple_fragments_on_one_row_is_unknown() -> None:
    segments = (
        _segment("top-left", Box(5, 0, 45, 12), row_index=0),
        _segment("top-right", Box(125, 0, 165, 12), row_index=0),
        _segment("bottom-bridge", Box(5, 20, 165, 32), row_index=1),
    )
    fixture = ObjectFixture(
        aligned_size=(180, 40),
        segments=segments,
        rules=(),
        matrix=_matrix(
            row_edges=(0, 12, 20, 32, 40),
            column_edges=(0, 60, 100, 180),
            placements={
                "top-left": ((0, 0),),
                "top-right": ((0, 2),),
                "bottom-bridge": ((2, 0), (2, 1), (2, 2)),
            },
        ),
    )

    result = _reconstruct(fixture)

    assert len(result.objects) == 1
    assert result.objects[0].kind is ObjectKind.UNKNOWN
    assert result.objects[0].confidence == 0.0
    assert result.objects[0].segment_ids == (
        "top-left",
        "top-right",
        "bottom-bridge",
    )
    assert result.objects[0].evidence == (
        "insufficient-structural-evidence",
        "ambiguous-image-free-flow",
    )
    _assert_exact_partition(
        result,
        ("top-left", "top-right", "bottom-bridge"),
    )


def test_exactly_two_offset_marker_body_rows_are_a_list() -> None:
    segments = (
        _segment("marker-0", Box(5, 0, 13, 8), row_index=0),
        _segment("body-0", Box(25, 2, 220, 18), row_index=1),
        _segment("marker-1", Box(5, 30, 13, 38), row_index=2),
        _segment("body-1", Box(25, 32, 210, 48), row_index=3),
    )
    fixture = ObjectFixture(
        aligned_size=(240, 50),
        segments=segments,
        rules=(),
        matrix=_matrix(
            row_edges=(0, 20, 30, 50),
            column_edges=(0, 20, 240),
            placements={
                "marker-0": ((0, 0),),
                "body-0": ((0, 1),),
                "marker-1": ((2, 0),),
                "body-1": ((2, 1),),
            },
        ),
    )

    result = _reconstruct(fixture)

    assert len(result.objects) == 1
    assert result.objects[0].kind is ObjectKind.LIST
    assert result.objects[0].segment_ids == (
        "marker-0",
        "body-0",
        "marker-1",
        "body-1",
    )
    _assert_exact_partition(result, tuple(item.segment_id for item in segments))


def test_rules_only_stage1_output_is_a_valid_empty_object_result() -> None:
    rule = _horizontal_rule("only-rule", 100, 10, 11)
    fixture = ObjectFixture(
        aligned_size=(100, 40),
        segments=(),
        rules=(rule,),
        matrix=_matrix(
            row_edges=(0, 10, 11, 40),
            column_edges=(0, 100),
            placements={},
            horizontal_rule_rows=(1,),
        ),
    )

    result = _reconstruct(fixture)

    assert result.objects == ()
    assert result.source_segment_ids == ()
    assert result.segment_ownership == ()


def test_rule_missing_from_sparse_rule_indexes_fails_closed() -> None:
    rule = _horizontal_rule("hidden-rule", 100, 10, 11)
    fixture = ObjectFixture(
        aligned_size=(100, 40),
        segments=(),
        rules=(rule,),
        matrix=_matrix(
            row_edges=(0, 10, 11, 40),
            column_edges=(0, 100),
            placements={},
        ),
    )

    with pytest.raises(ObjectReconstructionInvariantError, match="rule|matrix|index"):
        _reconstruct(fixture)


def test_holey_overlapping_spans_preserve_sparse_ownership_without_dense_fill() -> None:
    segments = (
        _segment("diagonal-a", Box(0, 0, 60, 60), row_index=0),
        _segment("diagonal-b", Box(0, 0, 60, 60), row_index=0),
    )
    fixture = ObjectFixture(
        aligned_size=(60, 60),
        segments=segments,
        rules=(),
        matrix=_matrix(
            row_edges=(0, 30, 60),
            column_edges=(0, 30, 60),
            placements={
                "diagonal-a": ((0, 0), (1, 1)),
                "diagonal-b": ((0, 1), (1, 0)),
            },
        ),
    )

    result = _reconstruct(fixture)

    assert len(result.objects) == 1
    assert result.objects[0].kind is ObjectKind.UNKNOWN
    _assert_exact_partition(result, ("diagonal-a", "diagonal-b"))


def test_large_sparse_axis_product_stays_bounded_and_non_recursive() -> None:
    axis_size = 2_000
    segments = (
        _segment("first", Box(0, 0, 1, 1), row_index=0),
        _segment(
            "last",
            Box(axis_size - 1, axis_size - 1, axis_size, axis_size),
            row_index=1,
        ),
    )
    fixture = ObjectFixture(
        aligned_size=(axis_size, axis_size),
        segments=segments,
        rules=(),
        matrix=_matrix(
            row_edges=tuple(range(axis_size + 1)),
            column_edges=tuple(range(axis_size + 1)),
            placements={"first": ((0, 0),), "last": ((axis_size - 1, axis_size - 1),)},
        ),
    )
    previous_limit = sys.getrecursionlimit()
    tracemalloc.start()
    try:
        sys.setrecursionlimit(80)
        result = _reconstruct(fixture)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        sys.setrecursionlimit(previous_limit)
        tracemalloc.stop()

    assert len(result.objects) == 2
    _assert_exact_partition(result, ("first", "last"))
    assert peak_bytes < 1_000_000


def test_one_marker_body_pair_is_unknown_not_a_guessed_list() -> None:
    segments = (
        _segment("marker", Box(4, 3, 12, 20), row_index=0),
        _segment("body", Box(30, 3, 190, 20), row_index=0),
    )
    fixture = ObjectFixture(
        aligned_size=(200, 40),
        segments=segments,
        rules=(),
        matrix=_matrix(
            row_edges=(0, 25, 40),
            column_edges=(0, 20, 200),
            placements={"marker": ((0, 0),), "body": ((0, 1),)},
        ),
    )

    result = _reconstruct(fixture)

    assert len(result.objects) == 1
    assert result.objects[0].kind is ObjectKind.UNKNOWN
    assert result.objects[0].segment_ids == ("marker", "body")
    _assert_exact_partition(result, ("marker", "body"))


def test_multiple_segments_in_one_sparse_cell_are_owned_once_in_geometry_order() -> None:
    segments = (
        Segment(
            segment_id="a-right",
            bbox=Box(55, 5, 90, 25),
            source_bbox=Box(10, 5, 45, 25),
            kind=SegmentKind.TEXT,
            ink_pixels=200,
            row_index=0,
            order_key=(5, 55),
            parent_path=("fixture-root",),
        ),
        Segment(
            segment_id="z-left",
            bbox=Box(10, 5, 45, 25),
            source_bbox=Box(55, 5, 90, 25),
            kind=SegmentKind.TEXT,
            ink_pixels=200,
            row_index=0,
            order_key=(5, 10),
            parent_path=("fixture-root",),
        ),
    )
    fixture = ObjectFixture(
        aligned_size=(100, 40),
        segments=segments,
        rules=(),
        matrix=_matrix(
            row_edges=(0, 40),
            column_edges=(0, 100),
            placements={"a-right": ((0, 0),), "z-left": ((0, 0),)},
        ),
    )

    result = _reconstruct(fixture)

    assert len(result.objects) == 1
    assert result.objects[0].kind is ObjectKind.UNKNOWN
    assert result.objects[0].segment_ids == ("z-left", "a-right")
    _assert_exact_partition(result, ("z-left", "a-right"))


def test_empty_segments_produce_an_empty_complete_result() -> None:
    fixture = ObjectFixture(
        aligned_size=(20, 10),
        segments=(),
        rules=(),
        matrix=SparseSegmentMatrix(
            rows=(),
            columns=(),
            cells=(),
            spans=(),
        ),
    )

    result = _reconstruct(fixture)

    assert result.aligned_size == (20, 10)
    assert result.objects == ()
    assert isinstance(result.diagnostics, tuple)


def test_empty_evidence_with_nonempty_axes_fails_closed() -> None:
    fixture = ObjectFixture(
        aligned_size=(20, 10),
        segments=(),
        rules=(),
        matrix=_matrix(
            row_edges=(0, 10),
            column_edges=(0, 20),
            placements={},
        ),
    )

    with pytest.raises(ObjectReconstructionInvariantError, match="empty|axis|evidence"):
        _reconstruct(fixture)


def test_input_tuple_permutations_do_not_change_semantics_or_object_ids() -> None:
    fixture = _mixed_table_then_paragraph_fixture()
    variants = (
        fixture,
        ObjectFixture(
            fixture.aligned_size,
            tuple(reversed(fixture.segments)),
            tuple(reversed(fixture.rules)),
            fixture.matrix,
        ),
        ObjectFixture(
            fixture.aligned_size,
            fixture.segments[2:] + fixture.segments[:2],
            fixture.rules[3:] + fixture.rules[:3],
            fixture.matrix,
        ),
    )

    results = tuple(_reconstruct(variant) for variant in variants)

    assert results[1:] == (results[0], results[0])
    assert tuple(document_object.object_id for document_object in results[0].objects) == (
        "object-000000",
        "object-000001",
    )


@pytest.mark.parametrize(
    ("fixture_factory", "message"),
    (
        (
            lambda: ObjectFixture(
                (201, 90),
                _paragraph_fixture().segments,
                (),
                _paragraph_fixture().matrix,
            ),
            "aligned|axis|canvas",
        ),
        (
            lambda: ObjectFixture(
                _paragraph_fixture().aligned_size,
                _paragraph_fixture().segments + (_segment("orphan", Box(1, 82, 5, 86), row_index=3),),
                (),
                _paragraph_fixture().matrix,
            ),
            "segment|matrix|span",
        ),
        (
            lambda: ObjectFixture(
                (200, 90),
                (_segment("outside", Box(190, 5, 210, 20), row_index=0),),
                (),
                _matrix(
                    row_edges=(0, 90),
                    column_edges=(0, 200),
                    placements={"outside": ((0, 0),)},
                ),
            ),
            "segment|canvas|outside",
        ),
        (
            lambda: ObjectFixture(
                (100, 50),
                (_segment("text", Box(10, 5, 90, 20), row_index=0),),
                (),
                _matrix(
                    row_edges=(0, 25, 50),
                    column_edges=(0, 100),
                    placements={"text": ((0, 0),)},
                    horizontal_rule_rows=(1,),
                ),
            ),
            "rule|orphan",
        ),
        (
            lambda: ObjectFixture(
                (200, 90),
                (_segment("wrong-span", Box(80, 5, 120, 20), row_index=0),),
                (),
                _matrix(
                    row_edges=(0, 90),
                    column_edges=(0, 100, 200),
                    placements={"wrong-span": ((0, 0),)},
                ),
            ),
            "segment|span|outside",
        ),
        (
            lambda: ObjectFixture(
                _paragraph_fixture().aligned_size,
                _paragraph_fixture().segments + (_paragraph_fixture().segments[0],),
                (),
                _paragraph_fixture().matrix,
            ),
            "segment|unique|identifier",
        ),
        (
            lambda: ObjectFixture(
                _table_fixture().aligned_size,
                _table_fixture().segments,
                _table_fixture().rules + (_table_fixture().rules[0],),
                _table_fixture().matrix,
            ),
            "rule|unique|identifier",
        ),
    ),
)
def test_cross_input_invariants_fail_closed(
    fixture_factory: object,
    message: str,
) -> None:
    fixture = fixture_factory()

    with pytest.raises(ObjectReconstructionInvariantError, match=message):
        _reconstruct(fixture)


@pytest.mark.parametrize(
    ("config", "fixture_factory", "message"),
    (
        (ObjectReconstructionConfig(max_segments=2), _paragraph_fixture, "segment"),
        (ObjectReconstructionConfig(max_cells=2), _paragraph_fixture, "cell"),
        (ObjectReconstructionConfig(max_rules=2), _table_fixture, "rule"),
        (
            ObjectReconstructionConfig(max_objects=1),
            _two_column_paragraph_fixture,
            "object",
        ),
        (
            ObjectReconstructionConfig(max_axis_intervals=4),
            _paragraph_fixture,
            "axis|interval",
        ),
        (
            ObjectReconstructionConfig(max_pairwise_checks=8),
            _table_fixture,
            "pairwise|check",
        ),
    ),
)
def test_configured_budgets_raise_typed_limit_errors(
    config: ObjectReconstructionConfig,
    fixture_factory: object,
    message: str,
) -> None:
    with pytest.raises(ObjectReconstructionLimitError, match=message):
        _reconstruct(fixture_factory(), config)


def test_many_empty_sparse_rows_hit_axis_budget_without_dense_projection() -> None:
    row_count = 2_000
    segment = _segment(
        "tail",
        Box(1, row_count - 1, 9, row_count),
        row_index=0,
    )
    fixture = ObjectFixture(
        aligned_size=(10, row_count),
        segments=(segment,),
        rules=(),
        matrix=_matrix(
            row_edges=tuple(range(row_count + 1)),
            column_edges=(0, 10),
            placements={"tail": ((row_count - 1, 0),)},
        ),
    )

    with pytest.raises(ObjectReconstructionLimitError, match="axis|interval"):
        _reconstruct(
            fixture,
            ObjectReconstructionConfig(max_axis_intervals=128),
        )


def test_stage6_imports_no_image_numeric_or_ocr_runtime() -> None:
    repository = Path(__file__).resolve().parents[3]
    code = """
import json
import sys
sys.path.insert(0, 'ocr')
import app.sparse_pipeline.object_reconstruction
import app.sparse_pipeline.object_artifacts
banned_roots = {
    'PIL',
    'cv2',
    'easyocr',
    'keras',
    'numpy',
    'onnxruntime',
    'paddle',
    'paddleocr',
    'pytesseract',
    'tensorflow',
    'tesserocr',
    'torch',
}
banned = sorted(
    name for name in sys.modules
    if name.split('.', 1)[0] in banned_roots
    or name == 'app.sparse_pipeline.geometry'
    or name.startswith('app.engines')
    or name.startswith('app.recognition')
)
print(json.dumps(banned))
raise SystemExit(bool(banned))
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert json.loads(completed.stdout) == []


def test_public_result_payload_is_deeply_immutable() -> None:
    result = _reconstruct(_mixed_table_then_paragraph_fixture())

    with pytest.raises(FrozenInstanceError):
        result.objects = ()
    with pytest.raises(FrozenInstanceError):
        result.objects[0].kind = ObjectKind.UNKNOWN
    with pytest.raises(TypeError):
        result.objects[0].segment_ids[0] = "mutated"
    with pytest.raises(FrozenInstanceError):
        result.segment_ownership[0].object_id = "mutated"


def _assert_artifact_file_set(stage_dir: Path) -> None:
    assert sorted(path.name for path in stage_dir.iterdir()) == [
        "diagnostics.txt",
        "manifest.json",
        "objects.jsonl",
        "reading-order.txt",
        "segment-ownership.jsonl",
    ]
    assert not tuple(stage_dir.rglob("*.png"))
    for path in stage_dir.iterdir():
        path.read_text(encoding="utf-8")


def test_debug_artifacts_are_text_only_deterministic_and_exact(tmp_path: Path) -> None:
    fixture = _mixed_table_then_paragraph_fixture()
    first_result = _reconstruct(fixture)
    permuted = ObjectFixture(
        fixture.aligned_size,
        tuple(reversed(fixture.segments)),
        tuple(reversed(fixture.rules)),
        fixture.matrix,
    )
    second_result = _reconstruct(permuted)
    writer = ObjectArtifactWriter()

    first = writer.write(tmp_path / "first", run_id="sample", result=first_result)
    second = writer.write(tmp_path / "second", run_id="sample", result=second_result)
    first_stage = first / "06-objects"
    second_stage = second / "06-objects"

    _assert_artifact_file_set(first_stage)
    _assert_artifact_file_set(second_stage)
    for path in first_stage.iterdir():
        assert path.read_bytes() == (second_stage / path.name).read_bytes()

    manifest = json.loads((first_stage / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["semantic_stage"] == 6
    assert manifest["aligned_size"] == [162, 102]
    assert manifest["objects"] == 2
    assert manifest["segment_ownership"] == 7
    assert manifest["exact_partition"] is True
    assert manifest["deterministic_order"] is True

    object_lines = tuple(
        json.loads(line) for line in (first_stage / "objects.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert [value["object_id"] for value in object_lines] == [
        "object-000000",
        "object-000001",
    ]
    for payload, document_object in zip(object_lines, first_result.objects):
        assert payload["kind"] == document_object.kind.value
        assert payload["reading_index"] == document_object.reading_index
        assert payload["segment_ids"] == list(document_object.segment_ids)
        assert payload["bbox"] == {
            "bottom": document_object.bbox.bottom,
            "left": document_object.bbox.left,
            "right": document_object.bbox.right,
            "top": document_object.bbox.top,
        }
    ownership_lines = tuple(
        json.loads(line) for line in (first_stage / "segment-ownership.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert len(ownership_lines) == 7
    assert {value["segment_id"] for value in ownership_lines} == {segment.segment_id for segment in fixture.segments}
    assert tuple((value["segment_id"], value["object_id"]) for value in ownership_lines) == tuple(
        (value.segment_id, value.object_id) for value in first_result.segment_ownership
    )
    assert (first_stage / "reading-order.txt").read_text(encoding="utf-8").splitlines() == [
        "\t".join(
            (
                f"{value.reading_index:06d}",
                value.object_id,
                value.kind.value,
                ",".join(value.segment_ids),
            )
        )
        for value in first_result.objects
    ]


def test_debug_writer_never_overwrites_and_rejects_unsafe_ids(tmp_path: Path) -> None:
    result = _reconstruct(_paragraph_fixture())
    writer = ObjectArtifactWriter()
    writer.write(tmp_path, run_id="sample", result=result)

    with pytest.raises(FileExistsError):
        writer.write(tmp_path, run_id="sample", result=result)
    assert not tuple(tmp_path.glob(".sample.partial-*"))

    for run_id in ("", "../escape", "nested/path", "white space"):
        with pytest.raises(ValueError):
            writer.write(tmp_path, run_id=run_id, result=result)


def test_debug_publish_rolls_back_a_partial_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _reconstruct(_paragraph_fixture())
    writer = ObjectArtifactWriter()

    def fail_after_partial_write(stage_dir: Path, value: object) -> None:
        del value
        (stage_dir / "partial.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("injected serialization failure")

    monkeypatch.setattr(writer, "_write_result", fail_after_partial_write)

    with pytest.raises(RuntimeError, match="injected serialization failure"):
        writer.write(tmp_path, run_id="broken", result=result)

    assert not (tmp_path / "broken").exists()
    assert not tuple(tmp_path.glob(".broken.partial-*"))
