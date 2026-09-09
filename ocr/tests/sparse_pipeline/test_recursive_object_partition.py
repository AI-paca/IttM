from __future__ import annotations

from app.sparse_pipeline.recursive_object_partition import (
    NumericCell,
    NumericRow,
    PartitionMatrixBasis,
    PartitionNode,
    PartitionSegment,
    PartitionedObject,
    RuledNetwork,
    _has_empty_row_separator,
    _mixed_axis_cycle_components,
    has_two_dimensional_object_extent,
    partition_recursive_objects,
    table_upper_left_boundary,
    table_upper_left_witness,
)


def _table_rows(top: int = 0) -> tuple[NumericRow, ...]:
    return (
        NumericRow(
            0,
            top,
            top + 10,
            (
                NumericCell((0, top, 10, top + 10), 0, False),
                NumericCell((10, top, 20, top + 10), 5, False),
            ),
        ),
        NumericRow(
            1,
            top + 10,
            top + 20,
            (
                NumericCell((0, top + 10, 10, top + 20), 3, False),
                NumericCell((10, top + 10, 20, top + 20), 8, False),
            ),
        ),
    )


def test_table_is_recognized_only_from_local_upper_left_anchor() -> None:
    network = RuledNetwork((0, 0, 20, 20), (0, 10, 20), (0, 10, 20))

    assert table_upper_left_witness(_table_rows(), network)
    assert table_upper_left_boundary(_table_rows(), network) == 10
    broken = (
        _table_rows()[0],
        NumericRow(
            1,
            10,
            20,
            (
                NumericCell((0, 10, 10, 20), 3, False),
                NumericCell((10, 10, 20, 20), 3, False),
            ),
        ),
    )
    assert not table_upper_left_witness(broken, network)
    assert table_upper_left_boundary(broken, network) is None


def test_numeric_cycle_without_independent_lattice_is_not_a_table() -> None:
    segments = (PartitionSegment("payload", (1, 1, 19, 19), ("geo-root",)),)
    nodes = (PartitionNode("geo-root", (0, 0, 20, 20), None, (), ("payload",)),)

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=_table_rows(),
        networks=(),
    )

    assert len(result) == 1
    assert result[0].kind == "flow"
    assert result[0].matrix_basis is PartitionMatrixBasis.TOPOLOGY_SLICE
    assert "finite-mixed-axis-cycle" not in result[0].evidence


def test_dangling_lower_left_eight_is_not_a_local_table_origin() -> None:
    rows = (
        NumericRow(
            0,
            0,
            10,
            (
                NumericCell((10, 0, 20, 10), 0, False),
                NumericCell((20, 0, 30, 10), 5, False),
            ),
        ),
        NumericRow(
            1,
            10,
            20,
            (
                NumericCell((10, 10, 20, 20), 8, False),
                NumericCell((20, 10, 30, 20), 8, False),
            ),
        ),
    )

    assert _mixed_axis_cycle_components(rows) == ()


def test_incoming_eight_expands_cycle_to_its_real_left_edge() -> None:
    rows = (
        NumericRow(
            0,
            0,
            10,
            (
                NumericCell((0, 0, 10, 10), 0, False),
                NumericCell((10, 0, 20, 10), 0, False),
                NumericCell((20, 0, 30, 10), 5, False),
            ),
        ),
        NumericRow(
            1,
            10,
            20,
            (
                NumericCell((0, 10, 10, 20), 3, False),
                NumericCell((10, 10, 20, 20), 8, False),
                NumericCell((20, 10, 30, 20), 8, False),
            ),
        ),
    )

    assert _mixed_axis_cycle_components(rows) == ((0, 0, 30, 20),)


def test_merged_cell_mixed_axis_cycle_is_a_table_component() -> None:
    rows = (
        NumericRow(
            0,
            0,
            10,
            (
                NumericCell((0, 0, 10, 10), 0, False),
                NumericCell((10, 0, 20, 10), 5, False),
            ),
        ),
        NumericRow(
            1,
            10,
            20,
            (NumericCell((0, 10, 20, 20), 3, False),),
        ),
    )

    assert _mixed_axis_cycle_components(rows) == ((0, 0, 20, 20),)


def test_cycle_crossing_a_lattice_boundary_does_not_preclaim_a_table() -> None:
    network = RuledNetwork(
        (20, 0, 40, 30),
        (20, 30, 40),
        (0, 10, 20, 30),
    )
    rows = (
        NumericRow(
            0,
            0,
            10,
            (
                NumericCell((0, 0, 20, 10), 0, False),
                NumericCell((20, 0, 30, 10), 7, True),
                NumericCell((30, 0, 40, 10), 10, True),
            ),
        ),
        NumericRow(
            1,
            10,
            20,
            (
                NumericCell((0, 10, 20, 20), 3, False),
                # The first network-local cell is globally merge-left=5
                # because payload exists immediately to its left.
                NumericCell((20, 10, 30, 20), 5, False),
                NumericCell((30, 10, 40, 20), 5, False),
            ),
        ),
        NumericRow(
            2,
            20,
            30,
            (
                NumericCell((0, 20, 20, 30), 3, False),
                NumericCell((20, 20, 30, 30), 8, False),
                NumericCell((30, 20, 40, 30), 8, False),
            ),
        ),
    )

    assert table_upper_left_witness(rows, network)
    assert _mixed_axis_cycle_components(rows) == ((0, 0, 40, 30),)

    segments = (
        PartitionSegment("prefix", (1, 1, 19, 9), ("geo-root",)),
        PartitionSegment("table", (21, 11, 39, 29), ("geo-root",)),
    )
    nodes = (
        PartitionNode(
            "geo-root",
            (0, 0, 40, 30),
            None,
            (),
            ("prefix", "table"),
        ),
    )
    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=rows,
        networks=(network,),
    )

    assert len(result) == 1
    combined = result[0]
    assert combined.kind == "paragraph"
    assert combined.segment_ids == ("prefix", "table")
    assert "independent-structural-lattice" not in combined.evidence


def test_two_ruled_tables_remain_two_objects_across_empty_ten() -> None:
    rows = (
        *_table_rows(0),
        NumericRow(2, 20, 30, (NumericCell((0, 20, 20, 30), 10, True),)),
        *(NumericRow(row.row + 3, row.top + 30, row.bottom + 30, row.cells) for row in _table_rows(0)),
    )
    # Translate the second table's cells as well as its row band.
    rows = (
        *rows[:3],
        NumericRow(
            3,
            30,
            40,
            (
                NumericCell((0, 30, 10, 40), 0, False),
                NumericCell((10, 30, 20, 40), 5, False),
            ),
        ),
        NumericRow(
            4,
            40,
            50,
            (
                NumericCell((0, 40, 10, 50), 3, False),
                NumericCell((10, 40, 20, 50), 8, False),
            ),
        ),
    )
    segments = (
        PartitionSegment("a", (1, 1, 19, 19), ("geo-root", "geo-root.0")),
        PartitionSegment("b", (1, 31, 19, 49), ("geo-root", "geo-root.1")),
    )
    nodes = (
        PartitionNode(
            "geo-root",
            (0, 0, 20, 50),
            "rows",
            ("geo-root.0", "geo-root.1"),
            ("a", "b"),
            ((0, 20, 20, 30),),
        ),
        PartitionNode("geo-root.0", (0, 0, 20, 20), None, (), ("a",)),
        PartitionNode("geo-root.1", (0, 30, 20, 50), None, (), ("b",)),
    )
    networks = (
        RuledNetwork((0, 0, 20, 20), (0, 10, 20), (0, 10, 20)),
        RuledNetwork((0, 30, 20, 50), (0, 10, 20), (30, 40, 50)),
    )

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=rows,
        networks=networks,
    )

    assert tuple(item.kind for item in result) == ("table", "table")
    assert tuple(item.segment_ids for item in result) == (("a",), ("b",))
    assert all(item.matrix_basis is PartitionMatrixBasis.RULE_LATTICE for item in result)


def test_long_numeric_empty_wall_forms_header_flank_and_table() -> None:
    rows = (
        NumericRow(0, 0, 10, (NumericCell((0, 0, 40, 10), 0, False),)),
        NumericRow(1, 10, 20, (NumericCell((0, 10, 40, 20), 7, True),)),
        *(
            NumericRow(
                index + 2,
                top,
                top + 10,
                (
                    NumericCell((0, top, 18, top + 10), 0, False),
                    NumericCell(
                        (18, top, 20, top + 10),
                        7 if index == 0 else 10,
                        True,
                    ),
                    NumericCell(
                        (20, top, 30, top + 10),
                        0 if index == 0 else 3,
                        False,
                    ),
                    NumericCell(
                        (30, top, 40, top + 10),
                        5 if index == 0 else 8,
                        False,
                    ),
                ),
            )
            for index, top in enumerate((20, 30, 40, 50))
        ),
    )
    segments = (
        PartitionSegment("header", (1, 1, 39, 9), ("geo-root", "geo-root.0")),
        *(
            PartitionSegment(
                f"filter-{index}",
                (1, top + 1, 17, top + 9),
                ("geo-root", "geo-root.1"),
            )
            for index, top in enumerate((20, 30, 40, 50))
        ),
        PartitionSegment(
            "table",
            (21, 21, 39, 59),
            ("geo-root", "geo-root.1"),
        ),
    )
    nodes = (
        PartitionNode(
            "geo-root",
            (0, 0, 40, 60),
            "rows",
            ("geo-root.0", "geo-root.1"),
            tuple(item.segment_id for item in segments),
            ((0, 10, 40, 20),),
        ),
        PartitionNode("geo-root.0", (0, 0, 40, 10), None, (), ("header",)),
        PartitionNode(
            "geo-root.1",
            (0, 20, 40, 60),
            None,
            (),
            tuple(item.segment_id for item in segments[1:]),
        ),
    )

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=rows,
        networks=(
            RuledNetwork(
                (20, 20, 40, 60),
                (20, 30, 40),
                (20, 30, 40, 50, 60),
            ),
        ),
    )

    assert tuple(item.kind for item in result) == ("flow", "paragraph", "table")
    assert result[0].segment_ids == ("header",)
    assert result[1].segment_ids == (
        "filter-0",
        "filter-1",
        "filter-2",
        "filter-3",
    )
    assert "spanning-finite-empty-corridor" in result[1].evidence
    assert "opposite-finite-table-cycle" not in result[1].evidence
    assert result[2].segment_ids == ("table",)
    assert "independent-structural-lattice" in result[2].evidence


def test_empty_corridor_splits_two_non_table_columns_before_typing() -> None:
    rows = tuple(
        NumericRow(
            index,
            top,
            top + 10,
            (
                NumericCell((0, top, 10, top + 10), 0, False),
                NumericCell(
                    (10, top, 20, top + 10),
                    7 if index == 0 else 10,
                    True,
                ),
                NumericCell((20, top, 30, top + 10), 0, False),
            ),
        )
        for index, top in enumerate((0, 10, 20))
    )
    segments = (
        PartitionSegment("left-a", (1, 1, 9, 9), ("geo-root",)),
        PartitionSegment("right-a", (21, 1, 29, 9), ("geo-root",)),
        PartitionSegment("left-b", (1, 11, 9, 19), ("geo-root",)),
        PartitionSegment("right-b", (21, 21, 29, 29), ("geo-root",)),
    )
    nodes = (
        PartitionNode(
            "geo-root",
            (0, 0, 30, 30),
            None,
            (),
            tuple(item.segment_id for item in segments),
        ),
    )

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=rows,
        networks=(),
    )

    assert tuple(item.segment_ids for item in result) == (
        ("left-a", "left-b"),
        ("right-a", "right-b"),
    )
    assert all("spanning-finite-empty-corridor" in item.evidence for item in result)
    assert all("opposite-finite-table-cycle" not in item.evidence for item in result)


def test_marker_body_rows_win_over_an_aligned_parallel_grid() -> None:
    rows = tuple(
        NumericRow(
            index,
            top,
            top + 10,
            (
                NumericCell((0, top, 6, top + 10), 0, False),
                NumericCell(
                    (6, top, 10, top + 10),
                    7 if index == 0 else 10,
                    True,
                ),
                NumericCell((10, top, 32, top + 10), 0, False),
            ),
        )
        for index, top in enumerate((0, 10))
    )
    segments = tuple(
        segment
        for index, top in enumerate((0, 10))
        for segment in (
            PartitionSegment(
                f"marker-{index}",
                (0, top + 1, 6, top + 9),
                ("geo-root",),
            ),
            PartitionSegment(
                f"body-{index}",
                (11, top + 1, 31, top + 9),
                ("geo-root",),
            ),
        )
    )
    nodes = (
        PartitionNode(
            "geo-root",
            (0, 0, 32, 20),
            None,
            (),
            tuple(segment.segment_id for segment in segments),
        ),
    )

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=rows,
        networks=(),
    )

    assert len(result) == 1
    assert result[0].kind == "list"
    assert "aligned-parallel-row-grid" in result[0].evidence
    assert "repeated-marker-body-rows" in result[0].evidence


def test_tall_recursive_column_separator_prevents_flat_cross_column_merge() -> None:
    segments = (
        PartitionSegment("left", (0, 10, 40, 30), ("geo-root", "geo-root.0")),
        PartitionSegment("right", (60, 10, 100, 30), ("geo-root", "geo-root.1")),
    )
    nodes = (
        PartitionNode(
            "geo-root",
            (0, 0, 100, 200),
            "columns",
            ("geo-root.0", "geo-root.1"),
            ("left", "right"),
            ((40, 0, 60, 200),),
        ),
        PartitionNode("geo-root.0", (0, 0, 40, 200), None, (), ("left",)),
        PartitionNode("geo-root.1", (60, 0, 100, 200), None, (), ("right",)),
    )

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=(),
        networks=(),
    )

    assert tuple(item.segment_ids for item in result) == (("left",), ("right",))


def test_short_recursive_column_split_keeps_one_text_line_object() -> None:
    segments = (
        PartitionSegment("word-a", (0, 2, 20, 18), ("geo-root", "geo-root.0")),
        PartitionSegment("word-b", (25, 2, 45, 18), ("geo-root", "geo-root.1")),
    )
    nodes = (
        PartitionNode(
            "geo-root",
            (0, 0, 45, 20),
            "columns",
            ("geo-root.0", "geo-root.1"),
            ("word-a", "word-b"),
            ((20, 0, 25, 20),),
        ),
        PartitionNode("geo-root.0", (0, 0, 20, 20), None, (), ("word-a",)),
        PartitionNode("geo-root.1", (25, 0, 45, 20), None, (), ("word-b",)),
    )

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=(),
        networks=(),
    )

    assert len(result) == 1
    assert result[0].segment_ids == ("word-a", "word-b")


def test_blank_recursive_row_separator_survives_missing_topology_row() -> None:
    segments = (
        PartitionSegment("upper", (0, 0, 100, 20), ("geo-root", "geo-root.0")),
        PartitionSegment("lower", (0, 40, 100, 60), ("geo-root", "geo-root.1")),
    )
    nodes = (
        PartitionNode(
            "geo-root",
            (0, 0, 100, 60),
            "rows",
            ("geo-root.0", "geo-root.1"),
            ("upper", "lower"),
            ((0, 20, 100, 40),),
        ),
        PartitionNode("geo-root.0", (0, 0, 100, 20), None, (), ("upper",)),
        PartitionNode("geo-root.1", (0, 40, 100, 60), None, (), ("lower",)),
    )
    # The canonical topology retains the payload rows but has no synthetic
    # row for the recursive blank band at y=20:40.
    rows = (
        NumericRow(0, 0, 20, (NumericCell((0, 0, 100, 20), 0, False),)),
        NumericRow(1, 40, 60, (NumericCell((0, 40, 100, 60), 3, False),)),
    )

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=rows,
        networks=(),
    )

    assert tuple(item.segment_ids for item in result) == (("upper",), ("lower",))


def test_separator_ignores_payload_that_only_grazes_its_context_edges() -> None:
    node = PartitionNode(
        "geo-root",
        (0, 0, 100, 200),
        "rows",
        ("geo-root.0", "geo-root.1"),
        ("upper", "lower"),
        ((0, 86, 100, 109),),
    )
    edge_grazing_rows = (
        NumericRow(
            0,
            86,
            88,
            (NumericCell((0, 86, 100, 88), 0, False),),
        ),
        NumericRow(
            1,
            108,
            109,
            (NumericCell((0, 108, 100, 109), 3, False),),
        ),
    )

    assert _has_empty_row_separator(
        node,
        edge_grazing_rows,
        typical_height=12,
        active_segments=(
            PartitionSegment("upper", (0, 70, 100, 88), ("geo-root",)),
            PartitionSegment("lower", (0, 108, 100, 130), ("geo-root",)),
        ),
    )

    crossing_rows = (
        *edge_grazing_rows,
        NumericRow(
            2,
            96,
            100,
            (NumericCell((0, 96, 100, 100), 3, False),),
        ),
    )
    assert not _has_empty_row_separator(
        node,
        crossing_rows,
        typical_height=12,
        active_segments=(
            PartitionSegment("upper", (0, 70, 100, 88), ("geo-root",)),
            PartitionSegment("crossing", (0, 96, 100, 100), ("geo-root",)),
            PartitionSegment("lower", (0, 108, 100, 130), ("geo-root",)),
        ),
    )


def test_short_unmaterialized_row_gap_does_not_split_flow_object() -> None:
    segments = (
        PartitionSegment("upper", (0, 0, 100, 20), ("geo-root", "geo-root.0")),
        PartitionSegment("lower", (0, 27, 100, 47), ("geo-root", "geo-root.1")),
    )
    nodes = (
        PartitionNode(
            "geo-root",
            (0, 0, 100, 47),
            "rows",
            ("geo-root.0", "geo-root.1"),
            ("upper", "lower"),
            ((0, 20, 100, 27),),
        ),
        PartitionNode("geo-root.0", (0, 0, 100, 20), None, (), ("upper",)),
        PartitionNode("geo-root.1", (0, 27, 100, 47), None, (), ("lower",)),
    )
    rows = (
        NumericRow(0, 0, 20, (NumericCell((0, 0, 100, 20), 0, False),)),
        NumericRow(1, 27, 47, (NumericCell((0, 27, 100, 47), 3, False),)),
    )

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=rows,
        networks=(),
    )

    assert len(result) == 1
    assert result[0].segment_ids == ("upper", "lower")


def test_small_list_markers_do_not_turn_item_spacing_into_object_separator() -> None:
    segments = (
        PartitionSegment("marker-0", (0, 17, 8, 23), ("geo-root", "geo-root.0")),
        PartitionSegment("body-0", (20, 0, 180, 40), ("geo-root", "geo-root.0")),
        PartitionSegment("marker-1", (0, 82, 8, 88), ("geo-root", "geo-root.1")),
        PartitionSegment("body-1", (20, 65, 180, 105), ("geo-root", "geo-root.1")),
    )
    nodes = (
        PartitionNode(
            "geo-root",
            (0, 0, 180, 105),
            "rows",
            ("geo-root.0", "geo-root.1"),
            tuple(item.segment_id for item in segments),
            ((0, 40, 180, 65),),
        ),
        PartitionNode(
            "geo-root.0",
            (0, 0, 180, 40),
            None,
            (),
            ("marker-0", "body-0"),
        ),
        PartitionNode(
            "geo-root.1",
            (0, 65, 180, 105),
            None,
            (),
            ("marker-1", "body-1"),
        ),
    )

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=(),
        networks=(),
    )

    assert len(result) == 1
    assert result[0].kind == "list"
    assert set(result[0].segment_ids) == {
        "marker-0",
        "body-0",
        "marker-1",
        "body-1",
    }


def test_mixed_flow_is_split_into_heading_paragraph_and_list() -> None:
    segments = (
        PartitionSegment("heading-0", (0, 0, 180, 10), ("geo-root",)),
        PartitionSegment("heading-1", (0, 12, 170, 22), ("geo-root",)),
        PartitionSegment("paragraph-0", (10, 50, 190, 60), ("geo-root",)),
        PartitionSegment("paragraph-1", (10, 62, 185, 72), ("geo-root",)),
        PartitionSegment("paragraph-2", (10, 74, 180, 84), ("geo-root",)),
        PartitionSegment("marker-0", (0, 113, 7, 119), ("geo-root",)),
        PartitionSegment("item-0", (20, 110, 190, 122), ("geo-root",)),
        PartitionSegment("continuation", (20, 130, 180, 142), ("geo-root",)),
        PartitionSegment("marker-1", (0, 163, 7, 169), ("geo-root",)),
        PartitionSegment("item-1", (20, 160, 175, 172), ("geo-root",)),
        PartitionSegment("marker-2", (0, 193, 7, 199), ("geo-root",)),
        PartitionSegment("item-2", (20, 190, 185, 202), ("geo-root",)),
    )
    nodes = (
        PartitionNode(
            "geo-root",
            (0, 0, 200, 210),
            None,
            (),
            tuple(item.segment_id for item in segments),
        ),
    )

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=(),
        networks=(),
    )

    assert tuple(item.kind for item in result) == (
        "paragraph",
        "paragraph",
        "list",
    )
    assert result[0].segment_ids == ("heading-0", "heading-1")
    assert result[1].segment_ids == (
        "paragraph-0",
        "paragraph-1",
        "paragraph-2",
    )
    assert set(result[2].segment_ids) == {
        "marker-0",
        "item-0",
        "continuation",
        "marker-1",
        "item-1",
        "marker-2",
        "item-2",
    }


def test_repeated_indented_rows_are_classified_as_list_without_glyph_ocr() -> None:
    segments = (
        PartitionSegment("title", (0, 0, 80, 10), ("geo-root",)),
        PartitionSegment("item-0", (20, 22, 100, 32), ("geo-root",)),
        PartitionSegment("item-1", (20, 37, 95, 47), ("geo-root",)),
        PartitionSegment("item-2", (20, 52, 110, 62), ("geo-root",)),
    )
    nodes = (
        PartitionNode(
            "geo-root",
            (0, 0, 120, 70),
            "rows",
            ("geo-root.0", "geo-root.1"),
            tuple(item.segment_id for item in segments),
            ((0, 10, 120, 22),),
        ),
        PartitionNode(
            "geo-root.0",
            (0, 0, 120, 10),
            None,
            (),
            ("title",),
        ),
        PartitionNode(
            "geo-root.1",
            (0, 22, 120, 70),
            None,
            (),
            ("item-0", "item-1", "item-2"),
        ),
    )

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=(),
        networks=(),
    )

    assert len(result) == 1
    assert result[0].kind == "list"


def test_list_survives_rows_with_fused_and_separate_markers() -> None:
    segments = (
        PartitionSegment("title", (0, 0, 80, 10), ("geo-root", "geo-root.0")),
        PartitionSegment("combined-0", (0, 22, 100, 32), ("geo-root", "geo-root.1")),
        PartitionSegment("body-1", (20, 37, 95, 47), ("geo-root", "geo-root.1")),
        PartitionSegment("combined-2", (0, 52, 110, 62), ("geo-root", "geo-root.1")),
        PartitionSegment("body-3", (20, 67, 105, 77), ("geo-root", "geo-root.1")),
        PartitionSegment("combined-4", (0, 82, 102, 92), ("geo-root", "geo-root.1")),
        PartitionSegment("combined-5", (0, 97, 98, 107), ("geo-root", "geo-root.1")),
    )
    nodes = (
        PartitionNode(
            "geo-root",
            (0, 0, 120, 110),
            "rows",
            ("geo-root.0", "geo-root.1"),
            tuple(item.segment_id for item in segments),
            ((0, 10, 120, 22),),
        ),
        PartitionNode(
            "geo-root.0",
            (0, 0, 120, 10),
            None,
            (),
            ("title",),
        ),
        PartitionNode(
            "geo-root.1",
            (0, 22, 120, 110),
            None,
            (),
            tuple(item.segment_id for item in segments[1:]),
        ),
    )

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=(),
        networks=(),
    )

    assert len(result) == 1
    assert result[0].kind == "list"
    assert set(result[0].segment_ids) == {item.segment_id for item in segments}


def test_first_line_indent_remains_a_paragraph() -> None:
    segments = (
        PartitionSegment("line-0", (20, 0, 180, 10), ("geo-root",)),
        PartitionSegment("line-1", (0, 12, 190, 22), ("geo-root",)),
        PartitionSegment("line-2", (0, 24, 185, 34), ("geo-root",)),
        PartitionSegment("line-3", (0, 36, 180, 46), ("geo-root",)),
        PartitionSegment("line-4", (0, 48, 175, 58), ("geo-root",)),
    )
    nodes = (
        PartitionNode(
            "geo-root",
            (0, 0, 200, 60),
            None,
            (),
            tuple(item.segment_id for item in segments),
        ),
    )

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=(),
        networks=(),
    )

    assert len(result) == 1
    assert result[0].kind == "paragraph"


def test_degenerate_geometry_is_accounted_for_but_is_not_an_object() -> None:
    def candidate(
        kind: str,
        bbox: tuple[int, int, int, int],
        basis: PartitionMatrixBasis = PartitionMatrixBasis.TOPOLOGY_SLICE,
    ) -> PartitionedObject:
        return PartitionedObject(
            "candidate",
            kind,
            bbox,
            bbox,
            ("segment",),
            (),
            basis,
        )

    assert not has_two_dimensional_object_extent(candidate("flow", (0, 0, 3468, 5)))
    assert not has_two_dimensional_object_extent(candidate("flow", (0, 0, 8, 21)))
    assert not has_two_dimensional_object_extent(candidate("flow", (0, 0, 1, 1)))
    assert has_two_dimensional_object_extent(candidate("flow", (0, 0, 28, 7)))
    assert has_two_dimensional_object_extent(candidate("flow", (0, 0, 20, 20)))
    assert not has_two_dimensional_object_extent(candidate("table", (0, 0, 1, 1)))
    assert has_two_dimensional_object_extent(
        candidate(
            "table",
            (0, 0, 1, 1),
            PartitionMatrixBasis.RULE_LATTICE,
        )
    )


def test_wide_thin_page_edge_fill_is_a_structural_residual() -> None:
    segments = (PartitionSegment("bottom-edge", (8, 95, 100, 100), ("geo-root",)),)
    nodes = (
        PartitionNode(
            "geo-root",
            (0, 0, 100, 100),
            None,
            (),
            ("bottom-edge",),
        ),
    )

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=(),
        networks=(),
    )

    assert len(result) == 1
    assert result[0].kind == "structural-residual"
    assert not has_two_dimensional_object_extent(result[0])


def test_tall_page_edge_fringe_is_not_attached_to_paragraph_crop() -> None:
    segments = (
        PartitionSegment("left-edge", (0, 0, 2, 40), ("geo-root",)),
        PartitionSegment("line-0", (10, 5, 100, 15), ("geo-root",)),
        PartitionSegment("line-1", (10, 20, 100, 30), ("geo-root",)),
    )
    nodes = (
        PartitionNode(
            "geo-root",
            (0, 0, 120, 50),
            None,
            (),
            tuple(item.segment_id for item in segments),
        ),
    )

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=(),
        networks=(),
    )

    paragraph = next(item for item in result if item.kind == "paragraph")
    residual = next(item for item in result if item.kind == "structural-residual")
    assert paragraph.bbox == (10, 5, 100, 30)
    assert paragraph.segment_ids == ("line-0", "line-1")
    assert residual.segment_ids == ("left-edge",)
    assert not has_two_dimensional_object_extent(residual)
