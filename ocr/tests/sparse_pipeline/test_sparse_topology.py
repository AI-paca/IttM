from __future__ import annotations

import pytest

from app.sparse_pipeline.sparse_topology import (
    EMPTY,
    EMPTY_SLOT_CODE,
    MERGE_BOTH_CODE,
    MERGE_LEFT_CODE,
    MERGE_UP_CODE,
    ObservedTopologyRow,
    SpatialSlot,
    TopologyEntry,
    compose_sparse_code,
    encode_observed_topology,
    encode_ragged_topology,
    encode_spatial_topology,
    sparse_code_components,
)


def test_canonical_sparse_codes_are_additive() -> None:
    assert MERGE_UP_CODE == 3
    assert MERGE_LEFT_CODE == 5
    assert EMPTY_SLOT_CODE == 7
    assert MERGE_BOTH_CODE == 8
    assert compose_sparse_code() == 0
    assert compose_sparse_code(merge_up=True, empty=True) == 10
    assert compose_sparse_code(merge_left=True, empty=True) == 12
    assert compose_sparse_code(
        merge_up=True,
        merge_left=True,
        empty=True,
    ) == 15
    assert sparse_code_components(8) == frozenset((3, 5))
    assert sparse_code_components(10) == frozenset((3, 7))
    assert sparse_code_components(12) == frozenset((5, 7))


def test_rectangular_rule_network_spells_table_pattern() -> None:
    table = "one-ruled-network"
    result = encode_ragged_topology(
        (
            (table, table, table),
            (table, table, table),
        )
    )

    assert result == (
        (0, 5, 5),
        (3, 8, 8),
    )


def test_explicit_empty_column_separates_two_geometry_components() -> None:
    result = encode_ragged_topology(
        (
            ("left", EMPTY, "right"),
            ("left", EMPTY, "right"),
            ("left", EMPTY, "right"),
        )
    )

    assert result == (
        (0, 7, 0),
        (3, 10, 3),
        (3, 10, 3),
    )


def test_null_is_unstored_ragged_tail_not_empty_seven() -> None:
    result = encode_ragged_topology(
        (
            ("paragraph-left", "paragraph-right", None),
            ("paragraph-left", None),
        )
    )

    assert result == ((0, 0), (3,))
    assert all(value != 7 for row in result for value in row)


def test_null_cannot_replace_leading_or_internal_empty_segment() -> None:
    with pytest.raises(ValueError, match="preceding segment"):
        encode_ragged_topology(((None,),))
    with pytest.raises(ValueError, match="only in the row tail"):
        encode_ragged_topology((("left", None, "right"),))


def test_spatial_rows_do_not_invent_cells_inside_merged_table_row() -> None:
    table = "one-ruled-network"
    regular = (
        SpatialSlot(0, 10, table),
        SpatialSlot(10, 20, table),
        SpatialSlot(20, 30, table),
    )
    merged = (SpatialSlot(0, 30, table),)

    assert encode_spatial_topology((regular, regular, merged, regular)) == (
        (0, 5, 5),
        (3, 8, 8),
        (3,),
        (3, 8, 8),
    )


def test_spatial_empty_separator_continues_vertically_without_merge_left() -> None:
    row = (
        SpatialSlot(0, 10, "left"),
        SpatialSlot(10, 20, EMPTY),
        SpatialSlot(20, 30, "right"),
    )

    assert encode_spatial_topology((row, row)) == (
        (0, 7, 0),
        (3, 10, 3),
    )


def test_observed_topology_scans_literal_table_tracks() -> None:
    row = ObservedTopologyRow(
        payload_columns=(0, 1, 2),
        merge_left_columns=(1, 2),
    )

    assert encode_observed_topology((row, row)) == (
        TopologyEntry(0, 0, 0, False),
        TopologyEntry(0, 1, 5, False),
        TopologyEntry(0, 2, 5, False),
        TopologyEntry(1, 0, 3, False),
        TopologyEntry(1, 1, 8, False),
        TopologyEntry(1, 2, 8, False),
    )


def test_observed_topology_keeps_null_absent_and_empty_explicit() -> None:
    row = ObservedTopologyRow(
        payload_columns=(0, 2),
        empty_columns=(1,),
    )

    assert encode_observed_topology((row, row, ObservedTopologyRow(()))) == (
        TopologyEntry(0, 0, 0, False),
        TopologyEntry(0, 1, 7, True),
        TopologyEntry(0, 2, 0, False),
        TopologyEntry(1, 0, 3, False),
        TopologyEntry(1, 1, 10, True),
        TopologyEntry(1, 2, 3, False),
    )


def test_observed_topology_rejects_unmaterialized_left_area() -> None:
    with pytest.raises(
        ValueError,
        match="leading and internal coordinates",
    ):
        encode_observed_topology(
            (ObservedTopologyRow(payload_columns=(2,)),)
        )


def test_explicit_blank_row_breaks_payload_merge_up() -> None:
    assert encode_observed_topology(
        (
            ObservedTopologyRow(payload_columns=(0,)),
            ObservedTopologyRow(payload_columns=(), empty_columns=(0,)),
            ObservedTopologyRow(payload_columns=(0,)),
        )
    ) == (
        TopologyEntry(0, 0, 0, False),
        TopologyEntry(1, 0, 7, True),
        TopologyEntry(2, 0, 0, False),
    )


def test_empty_segment_breaks_requested_merge_left() -> None:
    assert encode_observed_topology(
        (
            ObservedTopologyRow(
                payload_columns=(0, 2, 3),
                merge_left_columns=(2, 3),
                empty_columns=(1,),
            ),
        )
    ) == (
        TopologyEntry(0, 0, 0, False),
        TopologyEntry(0, 1, 7, True),
        TopologyEntry(0, 2, 0, False),
        TopologyEntry(0, 3, 5, False),
    )


@pytest.mark.parametrize("name", ("merge_up", "merge_left", "empty"))
def test_code_composition_rejects_integer_booleans(name: str) -> None:
    values = {"merge_up": False, "merge_left": False, "empty": False}
    values[name] = 1
    with pytest.raises(TypeError, match=f"{name} must be a boolean"):
        compose_sparse_code(**values)
