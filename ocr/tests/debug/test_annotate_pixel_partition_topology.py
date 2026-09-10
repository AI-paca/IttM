from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "debug" / "annotate_pixel_partition_topology.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("_annotate_pixel_partition_topology_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("thickness", (1, 2))
def test_thin_vertical_rule_inside_localization_window_remains_active(
    thickness: int,
) -> None:
    module = _load_script()
    rule_mask = np.zeros((32, 41), dtype=bool)
    # The stored boundary is x=20, while detector jitter placed its physical
    # pixels seven columns to the right, still inside the +/-8 search window.
    rule_mask[4:28, 27 : 27 + thickness] = True
    network = module.RuledNetwork(
        bbox=(0, 0, 41, 32),
        x_lines=(0, 20, 40),
        y_lines=(0, 32),
    )

    active = module._active_network_x_lines(
        network=network,
        rule_mask=rule_mask,
        top=0,
        bottom=32,
        coverage_threshold=0.75,
    )

    assert active == (0, 20, 40)


def test_network_touching_top_and_right_edges_keeps_outer_boundaries() -> None:
    module = _load_script()
    height, width = 40, 60
    rule_mask = np.zeros((height, width), dtype=bool)
    rule_mask[1:3, 10:60] = True
    rule_mask[27:29, 10:60] = True
    rule_mask[0:30, 10:12] = True
    rule_mask[0:30, 58:60] = True
    rule_boxes = (
        ("horizontal", (10, 1, 60, 3)),
        ("horizontal", (10, 27, 60, 29)),
        ("vertical", (10, 0, 12, 30)),
        ("vertical", (58, 0, 60, 30)),
    )

    networks = module._rule_networks(
        rule_boxes=rule_boxes,
        rule_mask=rule_mask,
        horizontal_coverage=0.50,
        vertical_coverage=0.50,
    )

    assert len(networks) == 1
    network = networks[0]
    assert network.bbox == (10, 0, width, 30)
    assert 0 in network.y_lines
    assert width in network.x_lines


def test_nearby_jpeg_double_edges_merge_into_one_vertical_boundary() -> None:
    module = _load_script()
    rule_mask = np.zeros((42, 64), dtype=bool)
    rule_mask[2:38, 5] = True
    rule_mask[2:38, 28] = True
    rule_mask[2:38, 31] = True
    rule_mask[2:38, 55] = True
    rule_mask[2, 5:56] = True
    rule_mask[37, 5:56] = True
    rule_boxes = (
        ("horizontal", (5, 2, 56, 3)),
        ("horizontal", (5, 37, 56, 38)),
        ("vertical", (5, 2, 6, 38)),
        ("vertical", (28, 2, 29, 38)),
        ("vertical", (31, 2, 32, 38)),
        ("vertical", (55, 2, 56, 38)),
    )

    networks = module._rule_networks(
        rule_boxes=rule_boxes,
        rule_mask=rule_mask,
        horizontal_coverage=0.90,
        vertical_coverage=0.90,
    )

    assert len(networks) == 1
    assert networks[0].x_lines == (5, 29, 55)


def test_bottom_open_repeated_cells_form_one_logical_finite_network() -> None:
    module = _load_script()
    height, width = 60, 160
    rule_mask = np.zeros((height, width), dtype=bool)
    rule_boxes: list[tuple[str, tuple[int, int, int, int]]] = []
    for left, right in ((10, 50), (50, 90), (90, 130)):
        horizontal = (left + 2, 20, right - 2, 21)
        rule_boxes.append(("horizontal", horizontal))
        rule_mask[20:21, left + 2 : right - 2] = True
    for left in (10, 50, 90, 130):
        vertical = (left, 21, left + 1, height)
        rule_boxes.append(("vertical", vertical))
        rule_mask[21:height, left : left + 1] = True
    before = rule_mask.copy()

    networks = module._rule_networks(
        rule_boxes=tuple(rule_boxes),
        rule_mask=rule_mask,
        horizontal_coverage=0.50,
        vertical_coverage=0.50,
    )

    np.testing.assert_array_equal(rule_mask, before)
    assert len(networks) == 1
    assert networks[0].bbox == (10, 20, 131, height)
    assert networks[0].x_lines == (10, 50, 90, 130)
    assert networks[0].y_lines == (20, height)


def test_repeated_internal_crossbars_do_not_invent_an_edge_network() -> None:
    module = _load_script()
    height, width = 80, 160
    rule_mask = np.zeros((height, width), dtype=bool)
    rule_boxes: list[tuple[str, tuple[int, int, int, int]]] = []
    for left, right in ((10, 50), (50, 90), (90, 130)):
        horizontal = (left + 2, 20, right - 2, 21)
        rule_boxes.append(("horizontal", horizontal))
        rule_mask[20:21, left + 2 : right - 2] = True
    for left in (10, 50, 90, 130):
        vertical = (left, 21, left + 1, 60)
        rule_boxes.append(("vertical", vertical))
        rule_mask[21:60, left : left + 1] = True

    networks = module._rule_networks(
        rule_boxes=tuple(rule_boxes),
        rule_mask=rule_mask,
        horizontal_coverage=0.50,
        vertical_coverage=0.50,
    )

    assert networks == ()


def test_sidebar_visual_rows_survive_inside_table_y_extent() -> None:
    module = _load_script()
    height, width = 100, 120
    rule_mask = np.zeros((height, width), dtype=bool)
    rule_mask[10:90, 40] = True
    rule_mask[10:90, 70] = True
    rule_mask[10:90, 100] = True
    network = module.RuledNetwork(
        bbox=(40, 10, 101, 90),
        x_lines=(40, 70, 100),
        y_lines=(10, 50, 90),
    )
    # Two word boxes share row_index=8. They must become one visual-line
    # segment, while row_index=9 must remain a separate sidebar row.
    visual_rows = (
        module.VisualRow(
            row_index=8,
            boxes=((5, 58, 12, 66), (16, 58, 25, 66)),
        ),
        module.VisualRow(row_index=9, boxes=((10, 73, 30, 81),)),
    )
    occupied_boxes = (
        (42, 12, 68, 48),
        (72, 12, 98, 48),
        (42, 52, 68, 88),
        (72, 52, 98, 88),
    )

    rows = module._ruled_network_rows(
        network=network,
        rule_mask=rule_mask,
        rows=((0, 100),),
        occupied_boxes=occupied_boxes,
        width=width,
        vertical_coverage=0.75,
        payload_value="table",
        visual_rows=visual_rows,
    )
    codes = module.encode_spatial_topology(tuple(row.slots for row in rows))

    assert tuple((row.top, row.bottom) for row in rows) == (
        (10, 50),
        (50, 58),
        (58, 66),
        (66, 73),
        (73, 81),
        (81, 90),
    )
    first_sidebar = rows[2]
    assert tuple((slot.start, slot.end) for slot in first_sidebar.slots) == (
        (0, 5),
        (5, 25),
        (25, 40),
        (40, 70),
        (70, 100),
        (100, 120),
    )
    assert first_sidebar.slots[1].value == ("visual-flow-row", 8)
    # The two table cells retain the local 0 5 / 3 8 pattern. Subdivision by
    # sidebar rows only adds vertical continuation; it never creates new
    # table cells or word-level sidebar segments.
    assert codes[0][1:3] == (0, 5)
    for row, row_codes in zip(rows[1:], codes[1:]):
        table_codes = tuple(code for slot, code in zip(row.slots, row_codes) if 40 <= slot.start and slot.end <= 100)
        assert table_codes == (3, 8)


def test_unruled_band_keeps_empty_track_between_stage1_segments() -> None:
    module = _load_script()
    slots = module._row_slots(
        row=0,
        row_interval=(10, 20),
        columns=((0, 120),),
        occupied_columns=frozenset({0}),
        vertical_rules=(),
        visual_rows=(
            module.VisualRow(3, ((10, 10, 40, 20),)),
            module.VisualRow(4, ((70, 10, 100, 20),)),
        ),
        width=120,
    )
    codes = module.encode_spatial_topology((slots,))[0]

    assert tuple((slot.start, slot.end) for slot in slots) == (
        (0, 10),
        (10, 40),
        (40, 70),
        (70, 100),
        (100, 120),
    )
    assert tuple(slot.value is module.EMPTY for slot in slots) == (
        True,
        False,
        True,
        False,
        True,
    )
    assert codes == (7, 0, 7, 0, 7)


def test_missing_y_band_cannot_create_merge_up() -> None:
    module = _load_script()
    rows = (
        module.PhysicalRow(
            source_rows=(0,),
            top=0,
            bottom=10,
            slots=(module.SpatialSlot(0, 20, "payload"),),
        ),
        module.PhysicalRow(
            source_rows=(1,),
            top=20,
            bottom=30,
            slots=(module.SpatialSlot(0, 20, "payload"),),
        ),
    )

    assert module._encode_physical_rows(rows) == ((0,), (0,))


def test_side_by_side_networks_share_one_planar_y_sweep() -> None:
    module = _load_script()
    height, width = 100, 120
    rule_mask = np.zeros((height, width), dtype=bool)
    for x, top, bottom in (
        (10, 10, 90),
        (30, 10, 90),
        (50, 10, 90),
        (70, 20, 80),
        (90, 20, 80),
        (110, 20, 80),
    ):
        rule_mask[top:bottom, x] = True
    networks = (
        module.RuledNetwork(
            bbox=(10, 10, 51, 90),
            x_lines=(10, 30, 50),
            y_lines=(10, 50, 90),
        ),
        module.RuledNetwork(
            bbox=(70, 20, 111, 80),
            x_lines=(70, 90, 110),
            y_lines=(20, 40, 80),
        ),
    )
    occupied_boxes = (
        (12, 12, 28, 48),
        (32, 12, 48, 48),
        (12, 52, 28, 88),
        (32, 52, 48, 88),
        (72, 22, 88, 38),
        (92, 22, 108, 38),
        (72, 42, 88, 78),
        (92, 42, 108, 78),
    )

    rows = module._combined_ruled_network_rows(
        networks=networks,
        rule_mask=rule_mask,
        rows=((0, height),),
        occupied_boxes=occupied_boxes,
        width=width,
        vertical_coverage=0.75,
    )
    codes = module.encode_spatial_topology(tuple(row.slots for row in rows))

    # The overlap is one planar row sequence, not two page-wide sequences
    # containing duplicate 20..80 bands.
    assert tuple((row.top, row.bottom) for row in rows) == (
        (10, 20),
        (20, 40),
        (40, 50),
        (50, 80),
        (80, 90),
    )
    assert all(first.bottom == second.top for first, second in zip(rows, rows[1:]))
    # The right table's boundaries exist only during its finite Y extent.
    assert tuple((slot.start, slot.end) for slot in rows[0].slots) == (
        (0, 10),
        (10, 30),
        (30, 50),
        (50, 120),
    )
    assert tuple((slot.start, slot.end) for slot in rows[-1].slots) == (
        (0, 10),
        (10, 30),
        (30, 50),
        (50, 120),
    )
    middle = rows[1]
    middle_codes = codes[1]
    left_codes = tuple(code for slot, code in zip(middle.slots, middle_codes) if 10 <= slot.start and slot.end <= 50)
    right_codes = tuple(code for slot, code in zip(middle.slots, middle_codes) if 70 <= slot.start and slot.end <= 110)
    assert left_codes == (3, 8)
    assert right_codes == (0, 5)
