from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "debug" / "extract_recursive_topology_objects.py"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "_extract_recursive_topology_objects_under_test",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_table_local_matrix_uses_only_its_rule_lattice_rows() -> None:
    module = _load_script()
    document_object = module.PartitionedObject(
        object_id="table",
        kind="table",
        bbox=(0, 0, 20, 20),
        matrix_bbox=(0, 0, 20, 20),
        segment_ids=("payload",),
        evidence=("upper-left-0-5-3-8",),
        matrix_basis=module.PartitionMatrixBasis.RULE_LATTICE,
    )
    segment_by_id = {
        "payload": module.PartitionSegment(
            "payload",
            (1, 1, 19, 19),
            ("geo-root",),
        )
    }
    # Page topology was subdivided by unrelated finite flow outside the table.
    rows = tuple(
        module.NumericRow(index, top, bottom, ())
        for index, (top, bottom) in enumerate(((0, 4), (4, 10), (10, 15), (15, 20)))
    )
    network = module.RuledNetwork(
        bbox=(0, 0, 20, 20),
        x_lines=(0, 10, 20),
        y_lines=(0, 10, 20),
    )

    matrix = module._local_matrix(
        document_object=document_object,
        rows=rows,
        segment_by_id=segment_by_id,
        networks=(network,),
    )

    assert [row["compressed_codes"] for row in matrix["rows"]] == [
        [0, 5, None],
        [3, 8, None],
    ]


def test_single_rule_interval_keeps_the_proven_table_anchor_pair() -> None:
    module = _load_script()
    document_object = module.PartitionedObject(
        object_id="table",
        kind="table",
        bbox=(0, 0, 20, 20),
        matrix_bbox=(0, 0, 20, 20),
        segment_ids=("left", "right"),
        evidence=("upper-left-0-5-3-8",),
        matrix_basis=module.PartitionMatrixBasis.RULE_LATTICE,
    )
    segment_by_id = {
        "left": module.PartitionSegment(
            "left",
            (1, 1, 9, 19),
            ("geo-root",),
        ),
        "right": module.PartitionSegment(
            "right",
            (11, 1, 19, 19),
            ("geo-root",),
        ),
    }
    # The network has one physical row interval, but the local 0/5 -> 3/8
    # witness was proved by two consecutive canonical bands inside it.
    rows = (
        module.NumericRow(0, 0, 2, ()),
        module.NumericRow(1, 2, 20, ()),
    )
    network = module.RuledNetwork(
        bbox=(0, 0, 20, 20),
        x_lines=(0, 10, 20),
        y_lines=(0, 20),
    )

    matrix = module._local_matrix(
        document_object=document_object,
        rows=rows,
        segment_by_id=segment_by_id,
        networks=(network,),
    )

    assert [row["compressed_codes"] for row in matrix["rows"]] == [
        [0, 5, None],
        [3, 8, None],
    ]


def test_single_interval_uses_numeric_anchor_when_only_two_columns_have_payload() -> None:
    module = _load_script()
    x_lines = (0, 10, 20, 30, 40, 50)
    document_object = module.PartitionedObject(
        object_id="edge-table",
        kind="table",
        bbox=(0, 0, 50, 30),
        matrix_bbox=(0, 0, 50, 30),
        segment_ids=("left", "right"),
        evidence=("upper-left-0-5-3-8",),
        matrix_basis=module.PartitionMatrixBasis.RULE_LATTICE,
    )
    segment_by_id = {
        "left": module.PartitionSegment("left", (1, 1, 9, 29), ("geo-root",)),
        "right": module.PartitionSegment("right", (11, 1, 19, 29), ("geo-root",)),
    }
    first_codes = (0, 5, 7, 10, 10)
    second_codes = (3, 8, 10, 10, 10)
    rows = tuple(
        module.NumericRow(
            index,
            top,
            bottom,
            tuple(
                module.NumericCell(
                    (left, top, right, bottom),
                    codes[column],
                    column >= 2,
                )
                for column, (left, right) in enumerate(zip(x_lines, x_lines[1:]))
            ),
        )
        for index, (top, bottom, codes) in enumerate(((0, 5, first_codes), (5, 30, second_codes)))
    )
    network = module.RuledNetwork(
        bbox=(0, 0, 50, 30),
        x_lines=x_lines,
        y_lines=(0, 30),
    )

    matrix = module._local_matrix(
        document_object=document_object,
        rows=rows,
        segment_by_id=segment_by_id,
        networks=(network,),
    )

    assert len(matrix["rows"]) == 2
    assert matrix["rows"][0]["compressed_codes"][:2] == [0, 5]
    assert matrix["rows"][1]["compressed_codes"][:2] == [3, 8]


def test_topology_table_needs_no_rule_network_and_is_locally_reencoded() -> None:
    module = _load_script()
    document_object = module.PartitionedObject(
        object_id="numeric-table",
        kind="table",
        bbox=(0, 0, 20, 20),
        matrix_bbox=(0, 0, 20, 20),
        segment_ids=("payload",),
        evidence=("finite-2x2-merge-cycle",),
        matrix_basis=module.PartitionMatrixBasis.TOPOLOGY_SLICE,
    )
    segment_by_id = {
        "payload": module.PartitionSegment(
            "payload",
            (1, 1, 19, 19),
            ("geo-root",),
        )
    }
    rows = (
        module.NumericRow(
            0,
            0,
            10,
            (
                module.NumericCell((0, 0, 10, 10), 0, False),
                module.NumericCell((10, 0, 20, 10), 5, False),
            ),
        ),
        module.NumericRow(
            1,
            10,
            20,
            (
                module.NumericCell((0, 10, 10, 20), 3, False),
                module.NumericCell((10, 10, 20, 20), 8, False),
            ),
        ),
    )

    matrix = module._local_matrix(
        document_object=document_object,
        rows=rows,
        segment_by_id=segment_by_id,
        networks=(),
    )

    assert matrix["matrix_basis"] == "topology-slice"
    assert [row["compressed_codes"] for row in matrix["rows"]] == [
        [0, 5, None],
        [3, 8, None],
    ]


def test_topology_slice_preserves_explicit_empty_cell() -> None:
    module = _load_script()
    document_object = module.PartitionedObject(
        object_id="split-flow",
        kind="flow",
        bbox=(0, 0, 30, 10),
        matrix_bbox=(0, 0, 30, 10),
        segment_ids=("left", "right"),
        evidence=("numeric-topology",),
        matrix_basis=module.PartitionMatrixBasis.TOPOLOGY_SLICE,
    )
    segment_by_id = {
        # Deliberately broad bboxes overlap the finite empty corridor.  The
        # canonical 7 is stronger evidence than rectangular bbox overlap.
        "left": module.PartitionSegment("left", (0, 0, 16, 10), ("geo-root",)),
        "right": module.PartitionSegment("right", (14, 0, 30, 10), ("geo-root",)),
    }
    rows = (
        module.NumericRow(
            0,
            0,
            10,
            (
                module.NumericCell((0, 0, 10, 10), 0, False),
                module.NumericCell((10, 0, 20, 10), 7, True),
                module.NumericCell((20, 0, 30, 10), 0, False),
            ),
        ),
    )

    matrix = module._local_matrix(
        document_object=document_object,
        rows=rows,
        segment_by_id=segment_by_id,
        networks=(),
    )

    assert [segment["state"] for segment in matrix["rows"][0]["segments"]] == [
        "payload",
        "empty",
        "payload",
    ]
    assert matrix["rows"][0]["compressed_codes"] == [0, 7, 0, None]


def test_topology_slice_does_not_invent_merge_edges_inside_one_object() -> None:
    module = _load_script()
    document_object = module.PartitionedObject(
        object_id="layout",
        kind="table",
        bbox=(0, 0, 20, 20),
        matrix_bbox=(0, 0, 20, 20),
        segment_ids=("left", "right"),
        evidence=("aligned-parallel-row-grid",),
        matrix_basis=module.PartitionMatrixBasis.TOPOLOGY_SLICE,
    )
    segment_by_id = {
        "left": module.PartitionSegment("left", (0, 0, 10, 20), ("geo-root",)),
        "right": module.PartitionSegment("right", (10, 0, 20, 20), ("geo-root",)),
    }
    rows = (
        module.NumericRow(
            0,
            0,
            10,
            (
                module.NumericCell((0, 0, 10, 10), 0, False),
                module.NumericCell((10, 0, 20, 10), 0, False),
            ),
        ),
        module.NumericRow(
            1,
            10,
            20,
            (
                module.NumericCell((0, 10, 10, 20), 0, False),
                module.NumericCell((10, 10, 20, 20), 0, False),
            ),
        ),
    )

    matrix = module._local_matrix(
        document_object=document_object,
        rows=rows,
        segment_by_id=segment_by_id,
        networks=(),
    )

    assert [row["compressed_codes"] for row in matrix["rows"]] == [
        [0, 0, None],
        [0, 0, None],
    ]


def test_owned_speck_inside_coarse_empty_cell_stays_explicit_payload() -> None:
    module = _load_script()
    document_object = module.PartitionedObject(
        object_id="speck-owner",
        kind="flow",
        bbox=(0, 0, 20, 10),
        matrix_bbox=(0, 0, 20, 10),
        segment_ids=("speck",),
        evidence=("numeric-topology",),
        matrix_basis=module.PartitionMatrixBasis.TOPOLOGY_SLICE,
    )
    segment_by_id = {
        "speck": module.PartitionSegment(
            "speck",
            (10, 4, 11, 5),
            ("geo-root",),
        )
    }
    rows = (
        module.NumericRow(
            0,
            0,
            10,
            (module.NumericCell((0, 0, 20, 10), 7, True),),
        ),
    )

    matrix = module._local_matrix(
        document_object=document_object,
        rows=rows,
        segment_by_id=segment_by_id,
        networks=(),
    )

    payload_slots = [slot for row in matrix["rows"] for slot in row["segments"] if slot["state"] == "payload"]
    assert len(payload_slots) == 1
    assert payload_slots[0]["source_segment_ids"] == ["speck"]
