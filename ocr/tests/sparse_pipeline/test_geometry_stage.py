from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.sparse_pipeline.contracts import (
    Box,
    GeometryResult,
    RuleAxis,
    SplitAxis,
    StopReason,
)
from app.sparse_pipeline.geometry import (
    GeometryAnalyzer,
    GeometryConfig,
    _best_separator,
    _local_structure_rule_drafts,
    _select_layout_foreground,
)
from app.sparse_pipeline.local_structure import (
    LocalStructureConfig,
    detect_local_structures,
)

BACKGROUND = (246, 243, 235)
BLACK = (12, 13, 15)
BLUE = (8, 72, 181)
RED = (192, 23, 41)


def analyze(image: Image.Image) -> GeometryResult:
    return GeometryAnalyzer(GeometryConfig()).analyze(image)


def ink_mask(
    image: Image.Image, background: tuple[int, int, int] = BACKGROUND
) -> np.ndarray:
    pixels = np.asarray(image.convert("RGB"))
    return np.any(pixels != np.asarray(background, dtype=np.uint8), axis=2)


def result_boxes(result: GeometryResult) -> Iterator[tuple[Box, tuple[int, int]]]:
    aligned_size = result.alignment.transform.aligned_size
    source_size = result.alignment.transform.original_size
    if result.alignment.content_bbox is not None:
        yield result.alignment.content_bbox, aligned_size
    for segment in result.segmentation.segments:
        yield segment.bbox, aligned_size
        yield segment.source_bbox, source_size
    for rule in result.segmentation.rules:
        yield rule.bbox, aligned_size
        yield rule.source_bbox, source_size
    for node in result.segmentation.nodes:
        yield node.bbox, aligned_size
        for separator in node.separator_boxes:
            yield separator, aligned_size


def assert_half_open_and_in_bounds(result: GeometryResult) -> None:
    for box, (width, height) in result_boxes(result):
        assert 0 <= box.left < box.right <= width
        assert 0 <= box.top < box.bottom <= height
        assert box.width == box.right - box.left
        assert box.height == box.bottom - box.top
        assert box.area == box.width * box.height
        assert box.contains_point(box.left, box.top)
        assert box.contains_point(box.right - 1, box.bottom - 1)
        assert not box.contains_point(box.right, box.bottom - 1)
        assert not box.contains_point(box.right - 1, box.bottom)


def assert_exact_foreground_count(result: GeometryResult, expected: int) -> None:
    segment_pixels = sum(segment.ink_pixels for segment in result.segmentation.segments)
    rule_pixels = sum(rule.foreground_pixels for rule in result.segmentation.rules)
    assert result.alignment.foreground_pixels == expected
    assert result.segmentation.foreground_pixels == expected
    assert segment_pixels + rule_pixels == expected


def draw_glyph_group(draw: ImageDraw.ImageDraw, *, left: int, top: int) -> None:
    for offset, height in ((0, 7), (5, 5), (10, 7), (15, 6)):
        draw.rectangle(
            (left + offset, top, left + offset + 2, top + height - 1), fill=BLACK
        )


def test_blank_image_is_a_lossless_empty_geometry_result() -> None:
    image = Image.new("RGB", (17, 13), BACKGROUND)
    original_bytes = image.tobytes()

    result = analyze(image)

    assert image.tobytes() == original_bytes
    assert image.mode == "RGB"
    assert image.size == (17, 13)
    assert result.alignment.background_rgb == BACKGROUND
    assert result.alignment.content_bbox is None
    assert_exact_foreground_count(result, 0)
    assert result.segmentation.segments == ()
    assert result.segmentation.rules == ()
    assert result.matrix.rows == ()
    assert result.matrix.columns == ()
    assert result.matrix.cells == ()
    assert result.matrix.spans == ()

    root = next(
        node
        for node in result.segmentation.nodes
        if node.node_id == result.segmentation.root_node_id
    )
    assert root.bbox == Box(0, 0, 17, 13)
    assert root.parent_id is None
    assert root.child_ids == ()
    assert root.segment_ids == ()
    assert root.stop_reason is StopReason.EMPTY
    assert result.alignment.transform.point_to_aligned(16.0, 12.0) == (16.0, 12.0)
    assert result.alignment.transform.point_to_source(16.0, 12.0) == (16.0, 12.0)
    assert_half_open_and_in_bounds(result)


def test_source_is_immutable_and_affine_matrices_are_exact_inverses() -> None:
    source = Image.new("RGB", (96, 56), BACKGROUND)
    draw = ImageDraw.Draw(source)
    draw_glyph_group(draw, left=8, top=12)
    draw_glyph_group(draw, left=48, top=34)
    image = source.rotate(
        2.0, resample=Image.Resampling.NEAREST, expand=False, fillcolor=BACKGROUND
    )
    original_bytes = image.tobytes()
    original_mode = image.mode
    original_size = image.size

    result = analyze(image)

    assert image.tobytes() == original_bytes
    assert image.mode == original_mode
    assert image.size == original_size
    transform = result.alignment.transform
    forward = np.asarray(transform.forward, dtype=np.float64).reshape(3, 3)
    inverse = np.asarray(transform.inverse, dtype=np.float64).reshape(3, 3)
    np.testing.assert_allclose(forward @ inverse, np.eye(3), rtol=0.0, atol=1e-9)
    np.testing.assert_allclose(inverse @ forward, np.eye(3), rtol=0.0, atol=1e-9)

    for point in ((0.0, 0.0), (95.0, 0.0), (0.0, 55.0), (95.0, 55.0), (37.25, 19.75)):
        aligned = transform.point_to_aligned(*point)
        restored = transform.point_to_source(*aligned)
        assert restored == pytest.approx(point, rel=0.0, abs=1e-6)
    assert_half_open_and_in_bounds(result)


def test_edge_touching_colored_single_pixels_are_not_discarded() -> None:
    width, height = 19, 13
    pixels = np.full((height, width, 3), BACKGROUND, dtype=np.uint8)
    pixels[0, 0] = BLUE
    pixels[height - 1, width - 1] = RED
    image = Image.fromarray(pixels, mode="RGB")

    result = analyze(image)

    assert result.alignment.correction_degrees == pytest.approx(0.0, abs=1e-9)
    assert result.alignment.content_bbox == Box(0, 0, width, height)
    assert_exact_foreground_count(result, 2)
    source_boxes = [
        *(segment.source_bbox for segment in result.segmentation.segments),
        *(rule.source_bbox for rule in result.segmentation.rules),
    ]
    assert source_boxes
    assert Box.union(source_boxes) == Box(0, 0, width, height)
    assert any(box.contains_point(0, 0) for box in source_boxes)
    assert any(box.contains_point(width - 1, height - 1) for box in source_boxes)
    assert_half_open_and_in_bounds(result)


def test_one_pixel_rules_are_retained_and_do_not_steal_or_duplicate_ink() -> None:
    image = Image.new("RGB", (80, 48), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.line((4, 14, 75, 14), fill=BLACK, width=1)
    draw.line((40, 3, 40, 44), fill=BLACK, width=1)
    draw.rectangle((8, 5, 10, 10), fill=BLUE)
    draw.rectangle((61, 31, 63, 37), fill=RED)
    expected_ink = int(ink_mask(image).sum())

    result = analyze(image)

    axes = {rule.axis for rule in result.segmentation.rules}
    assert axes == {RuleAxis.HORIZONTAL, RuleAxis.VERTICAL}
    horizontal = [
        rule for rule in result.segmentation.rules if rule.axis is RuleAxis.HORIZONTAL
    ]
    vertical = [
        rule for rule in result.segmentation.rules if rule.axis is RuleAxis.VERTICAL
    ]
    assert any(rule.bbox.height == 1 and rule.bbox.width >= 70 for rule in horizontal)
    assert any(rule.bbox.width == 1 and rule.bbox.height >= 40 for rule in vertical)
    assert result.matrix.horizontal_rule_rows
    assert result.matrix.vertical_rule_columns
    assert_exact_foreground_count(result, expected_ink)
    assert_half_open_and_in_bounds(result)


def test_partial_page_frame_corner_stays_out_of_text_segments() -> None:
    image = Image.new("RGB", (240, 180), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 239, 2), fill=BLACK)
    # Perspective may leave the perpendicular side only over a finite prefix.
    draw.rectangle((0, 0, 2, 105), fill=BLACK)
    draw_glyph_group(draw, left=40, top=130)

    result = GeometryAnalyzer(
        GeometryConfig(deskew_max_degrees=0.0)
    ).analyze(image)

    assert {rule.axis for rule in result.segmentation.rules} == {
        RuleAxis.HORIZONTAL,
        RuleAxis.VERTICAL,
    }
    assert all(segment.bbox.left >= 40 for segment in result.segmentation.segments)
    assert all(segment.bbox.top >= 130 for segment in result.segmentation.segments)
    assert_exact_foreground_count(result, int(ink_mask(image).sum()))


def test_recursive_safe_seams_never_cut_non_rule_ink() -> None:
    image = Image.new("RGB", (88, 52), BACKGROUND)
    draw = ImageDraw.Draw(image)
    for top in (5, 34):
        draw_glyph_group(draw, left=4, top=top)
        draw_glyph_group(draw, left=58, top=top)
    known_ink = ink_mask(image)

    result = analyze(image)

    assert result.alignment.correction_degrees == pytest.approx(0.0, abs=1e-9)
    assert result.alignment.transform.aligned_size == image.size
    assert result.segmentation.rules == ()
    internal_nodes = [node for node in result.segmentation.nodes if node.child_ids]
    assert (
        internal_nodes
    ), "the wide row and column gaps must trigger recursive splitting"
    assert {node.axis for node in internal_nodes} == {SplitAxis.ROWS, SplitAxis.COLUMNS}
    separators = [
        separator for node in internal_nodes for separator in node.separator_boxes
    ]
    assert separators, "every recursive split must retain its safe seam"
    for separator in separators:
        cut = known_ink[
            separator.top : separator.bottom, separator.left : separator.right
        ]
        assert (
            not cut.any()
        ), f"separator {separator.as_tuple()} cuts non-rule foreground"
    assert_exact_foreground_count(result, int(known_ink.sum()))
    assert_half_open_and_in_bounds(result)


def test_recursive_adaptive_crops_accumulate_both_text_polarities() -> None:
    image = Image.new("RGB", (260, 180), (45, 45, 45))
    draw = ImageDraw.Draw(image)
    # The dark full page selects light-on-fill evidence.  Once recursion has
    # isolated the yellow panel, its local median selects dark-on-fill
    # evidence.  Both observations belong to the same sparse object.
    draw.rectangle((20, 0, 130, 155), fill=(220, 190, 35))
    draw.rectangle((35, 45, 70, 52), fill=(252, 252, 252))
    draw.rectangle((80, 85, 115, 92), fill=(15, 15, 15))
    draw.rectangle((165, 25, 235, 70), fill=(80, 80, 80))
    draw.rectangle((180, 43, 220, 50), fill=(250, 250, 250))

    analyzer = GeometryAnalyzer(GeometryConfig(deskew_max_degrees=0.0))
    result = analyzer.analyze(image)
    bundle = analyzer.last_bundle

    assert bundle is not None
    assert any(
        value.startswith("foreground_mode=adaptive-dual:")
        for value in result.diagnostics
    )
    assert any(node.depth >= 2 for node in result.segmentation.nodes)
    assert bundle.foreground_mask[48, 40]  # light ink proved by the page
    assert bundle.foreground_mask[88, 90]  # dark ink proved by the crop
    assert bundle.foreground_mask[46, 185]  # another filled region
    assert not bundle.foreground_mask[100, 50]  # flat yellow remains empty
    assert not bundle.foreground_mask[30, 25]


def test_adaptive_root_keeps_dark_and_light_ink_on_mixed_flat_fills() -> None:
    image = np.empty((100, 300, 3), dtype=np.uint8)
    image[:, :220] = (28, 92, 48)
    image[:, 220:] = (218, 164, 14)
    image[18:21, 30:170] = (250, 250, 250)
    image[48:51, 225:295] = (250, 250, 250)
    image[78:81, 225:295] = (12, 12, 12)

    selected, mode = _select_layout_foreground(
        image,
        np.ones(image.shape[:2], dtype=bool),
        force_adaptive=True,
    )

    assert mode.startswith("adaptive-dual:")
    assert selected[19, 40:160].all()
    assert selected[49, 230:290].all()
    assert selected[79, 230:290].all()
    assert not selected[35, 80]
    assert not selected[65, 250]


def test_column_crop_keeps_recursing_through_local_text_rows() -> None:
    image = Image.new("RGB", (240, 100), BACKGROUND)
    draw = ImageDraw.Draw(image)
    # A wide edge decoration first isolates the text as a column crop.  The
    # two text rows touch in their row projection, while their glyphs remain
    # distinct components on opposite sides of y=50.
    draw.rectangle((0, 0, 19, 99), fill=BLUE)
    for left in (70, 90, 110, 130):
        draw.rectangle((left, 30, left + 6, 49), fill=BLACK)
    for left in (78, 98, 118, 138):
        draw.rectangle((left, 50, left + 6, 69), fill=BLACK)

    result = GeometryAnalyzer(
        GeometryConfig(deskew_max_degrees=0.0)
    ).analyze(image)

    local_row_split = next(
        node
        for node in result.segmentation.nodes
        if node.bbox.left > 0 and node.split_coordinate == 50
    )
    assert local_row_split.axis is SplitAxis.ROWS
    assert local_row_split.separator_boxes == ()
    text_boxes = tuple(
        segment.bbox
        for segment in result.segmentation.segments
        if segment.bbox.left >= 70
    )
    assert text_boxes == (
        Box(70, 30, 137, 50),
        Box(78, 50, 145, 70),
    )
    assert_exact_foreground_count(result, int(ink_mask(image).sum()))


def test_best_separator_uses_widest_safe_row_before_balanced_column() -> None:
    mask = np.zeros((100, 100), dtype=bool)
    for top, bottom in ((0, 10), (20, 40), (42, 62)):
        mask[top:bottom, :45] = True
        mask[top:bottom, 55:] = True

    decision = _best_separator(
        mask,
        Box(0, 0, 100, 100),
        row_minimum_gap=1,
        column_minimum_gap=3,
        body_height=10.0,
        prefer_rows=True,
    )

    assert decision == (
        SplitAxis.ROWS,
        Box(0, 10, 100, 20),
        False,
    )


def test_full_width_header_is_isolated_before_body_columns() -> None:
    image = Image.new("RGB", (240, 140), BACKGROUND)
    draw = ImageDraw.Draw(image)
    for left, right in ((10, 90), (150, 230)):
        draw.rectangle((left, 5, right - 1, 14), fill=BLACK)
    for top, bottom in ((40, 60), (66, 86), (92, 122)):
        for left, right in ((10, 90), (150, 230)):
            draw.rectangle((left, top, right - 1, bottom - 1), fill=BLACK)

    result = GeometryAnalyzer(
        GeometryConfig(deskew_max_degrees=0.0)
    ).analyze(image)
    root = next(
        node
        for node in result.segmentation.nodes
        if node.node_id == result.segmentation.root_node_id
    )

    assert root.axis is SplitAxis.ROWS
    assert root.separator_boxes == (Box(0, 15, 240, 40),)
    body = next(
        node
        for node in result.segmentation.nodes
        if node.parent_id == root.node_id and node.bbox.top > 0
    )
    assert any(
        node.axis is SplitAxis.COLUMNS
        for node in result.segmentation.nodes
        if node.node_id == body.node_id or node.node_id.startswith(f"{body.node_id}.")
    )
    assert_exact_foreground_count(result, int(ink_mask(image).sum()))


def test_rule_partition_still_recurses_across_proven_row_seams() -> None:
    """A page-wide rule must not make every preceding text row atomic."""

    image = Image.new("RGB", (240, 190), BACKGROUND)
    draw = ImageDraw.Draw(image)
    row_tops = (18, 55, 92, 166)
    for top in row_tops:
        for left in (18, 88, 168):
            draw.rectangle((left, top, left + 26, top + 9), fill=BLACK)
    draw.line((8, 145, 231, 145), fill=BLACK, width=2)

    result = analyze(image)

    horizontal_rules = tuple(
        rule
        for rule in result.segmentation.rules
        if rule.axis is RuleAxis.HORIZONTAL
    )
    assert len(horizontal_rules) == 1
    assert len(result.segmentation.segments) == 3 * len(row_tops)
    for top in row_tops:
        row_segments = tuple(
            segment
            for segment in result.segmentation.segments
            if segment.bbox.top == top
        )
        assert len(row_segments) == 3
        assert all(segment.bbox.bottom == top + 10 for segment in row_segments)

    rule_partition = next(
        node
        for node in result.segmentation.nodes
        if node.parent_id == result.segmentation.root_node_id
        and node.bbox.top == 0
    )
    assert rule_partition.child_ids
    assert rule_partition.axis is SplitAxis.ROWS
    assert_exact_foreground_count(result, int(ink_mask(image).sum()))


def test_rule_partition_uses_row_valley_without_fragmenting_columns() -> None:
    image = Image.new("RGB", (240, 130), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rectangle((50, 1, 120, 5), fill=BLACK)
    draw.line((8, 12, 231, 12), fill=BLACK, width=2)
    # These two rows touch in the row projection, so there is no blank row
    # separator.  Their tapered endpoints prove a guarded boundary at y=35.
    # The rules on both sides prove that this is a rule-bounded row region:
    # its ordinary column gap must remain merged until the row valley wins.
    draw.rectangle((12, 25, 88, 33), fill=BLACK)
    draw.rectangle((12, 34, 22, 34), fill=BLACK)
    draw.rectangle((108, 35, 118, 35), fill=BLACK)
    draw.rectangle((108, 36, 188, 44), fill=BLACK)
    draw.line((8, 65, 231, 65), fill=BLACK, width=2)
    draw.rectangle((50, 100, 120, 108), fill=BLACK)

    result = analyze(image)

    assert tuple(segment.bbox for segment in result.segmentation.segments) == (
        Box(50, 1, 121, 6),
        Box(12, 25, 89, 35),
        Box(108, 35, 189, 45),
        Box(50, 100, 121, 109),
    )
    valley_node = next(
        node
        for node in result.segmentation.nodes
        if node.split_coordinate == 35
    )
    assert valley_node.axis is SplitAxis.ROWS
    assert valley_node.separator_boxes == ()
    assert not any(
        node.axis is SplitAxis.COLUMNS
        for node in result.segmentation.nodes
    )
    assert_exact_foreground_count(result, int(ink_mask(image).sum()))


def test_large_imbalanced_safe_gap_keeps_aligned_minor_row_separate() -> None:
    image = Image.new("RGB", (420, 280), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.ellipse((12, 12, 92, 92), outline=BLACK, width=5)
    for left in (18, 38, 58, 78):
        draw.rectangle((left, 220, left + 4, 225), fill=BLACK)
    # Keep the page-wide body-height estimate much taller than the small row,
    # as happens when a calendar row sits below a logo on a table-heavy page.
    for left in range(260, 401, 16):
        draw.rectangle((left, 20, left + 9, 51), fill=BLACK)

    result = GeometryAnalyzer(
        GeometryConfig(deskew_max_degrees=0.0)
    ).analyze(image)

    assert tuple(segment.bbox for segment in result.segmentation.segments) == (
        Box(12, 12, 93, 93),
        Box(260, 20, 398, 52),
        Box(18, 220, 83, 226),
    )
    minority = result.segmentation.segments[-1]
    assert len(minority.component_ids) == 4
    assert any(
        node.axis is SplitAxis.ROWS
        and node.separator_boxes == (Box(0, 93, 420, 220),)
        for node in result.segmentation.nodes
    )
    assert_exact_foreground_count(result, int(ink_mask(image).sum()))


def test_two_sided_rule_band_prefers_safe_rows_over_ordinary_columns() -> None:
    image = Image.new("RGB", (360, 150), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 2, 50, 7), fill=BLACK)
    draw.line((10, 20, 349, 20), fill=BLACK, width=2)
    for top in (40, 55):
        draw.rectangle((20, top, 50, top + 9), fill=BLACK)
        draw.rectangle((310, top, 340, top + 9), fill=BLACK)
    draw.line((10, 90, 349, 90), fill=BLACK, width=2)
    draw.rectangle((20, 130, 50, 135), fill=BLACK)

    result = analyze(image)

    assert tuple(segment.bbox for segment in result.segmentation.segments) == (
        Box(20, 2, 51, 8),
        Box(20, 40, 341, 50),
        Box(20, 55, 341, 65),
        Box(20, 130, 51, 136),
    )
    assert any(
        node.axis is SplitAxis.ROWS
        and node.separator_boxes == (Box(0, 50, 360, 55),)
        for node in result.segmentation.nodes
    )
    assert not any(
        node.axis is SplitAxis.COLUMNS
        for node in result.segmentation.nodes
    )
    assert_exact_foreground_count(result, int(ink_mask(image).sum()))


def test_local_broken_grid_comb_recovers_columns_before_second_partition() -> None:
    image = Image.new("RGB", (440, 170), BACKGROUND)
    draw = ImageDraw.Draw(image)
    boundaries = tuple(range(40, 401, 40))

    # Each horizontal piece is long enough to prove one locally dense row but
    # is not a global rule: nearby cell ink deliberately removes its lateral
    # clearance.  At each two-pixel column band only the first top pixel is
    # present, leaving a one-pixel break between neighboring row pieces.
    starts = (10, *(boundary + 2 for boundary in boundaries))
    stops = (*boundaries, 430)
    for left, right in zip(starts, stops, strict=True):
        draw.line((left, 45, right, 45), fill=BLACK, width=1)
        center = (left + right) // 2
        draw.rectangle((center - 7, 47, center + 7, 50), fill=BLACK)
    for boundary in boundaries:
        draw.point((boundary, 45), fill=BLACK)
        draw.rectangle((boundary, 46, boundary + 1, 130), fill=BLACK)

    bundle = GeometryAnalyzer(
        GeometryConfig(deskew_max_degrees=0.0)
    ).analyze_bundle(image)
    result = bundle.result

    recovered = next(
        item for item in result.diagnostics if item.startswith("recovered_rule_bands=")
    )
    assert recovered == f"recovered_rule_bands={len(boundaries) + 1}"
    assert "thin_line_segment_count=0" in result.diagnostics
    vertical_rules = tuple(
        rule
        for rule in result.segmentation.rules
        if rule.axis is RuleAxis.VERTICAL
    )
    horizontal_rules = tuple(
        rule
        for rule in result.segmentation.rules
        if rule.axis is RuleAxis.HORIZONTAL
    )
    assert len(vertical_rules) == len(boundaries)
    assert len(horizontal_rules) == 1
    assert horizontal_rules[0].bbox == Box(10, 45, 431, 46)
    assert all(rule.bbox.top == 45 and rule.bbox.bottom == 131 for rule in vertical_rules)
    assert max(segment.bbox.width for segment in result.segmentation.segments) <= 40
    assert not any(
        segment.bbox.height <= 2 and segment.bbox.width / segment.bbox.height >= 12.0
        for segment in result.segmentation.segments
    )
    assert_exact_foreground_count(result, int(ink_mask(image).sum()))


def test_low_contrast_vertical_continuation_extends_one_primary_grid() -> None:
    image = Image.new("RGB", (440, 380), (20, 20, 20))
    draw = ImageDraw.Draw(image)
    columns = (30, 90, 150, 210, 270, 330, 390)
    rows = (20, 56, 92, 128, 164, 200, 236, 272, 308, 344)

    for top in rows[:6]:
        draw.line((columns[0], top, columns[-1], top), fill=(42, 42, 42))
    for left in columns:
        draw.line((left, rows[0], left, rows[5]), fill=(42, 42, 42))

    # Only the lower four rows have contrast three.  They may continue the
    # proven upper grid, but are too weak to establish an independent object.
    for top in rows[6:]:
        draw.line((columns[0], top, columns[-1], top), fill=(23, 23, 23))
    for left in columns:
        draw.line((left, rows[5], left, rows[-1]), fill=(23, 23, 23))

    drafts = _local_structure_rule_drafts(
        np.asarray(image),
        GeometryConfig(),
    )
    horizontal = tuple(draft for draft in drafts if draft.axis is RuleAxis.HORIZONTAL)
    vertical = tuple(draft for draft in drafts if draft.axis is RuleAxis.VERTICAL)

    assert len(horizontal) == len(rows)
    assert len(vertical) == len(columns)
    assert {draft.bbox.top for draft in horizontal} == set(rows)
    assert {draft.bbox.left for draft in vertical} == set(columns)
    assert all(columns[0] <= draft.bbox.left and draft.bbox.right <= columns[-1] + 2 for draft in horizontal)
    assert all(rows[0] <= draft.bbox.top and draft.bbox.bottom <= rows[-1] + 2 for draft in vertical)
    assert all(draft.bbox.bottom >= rows[-1] for draft in vertical)


def test_low_contrast_network_does_not_join_adjacent_primary_card_cores() -> None:
    image = Image.new("RGB", (360, 270), (20, 20, 20))
    draw = ImageDraw.Draw(image)
    left_columns = (20, 80, 140)
    right_columns = (180, 240, 300)
    primary_rows = (20, 80, 140)

    for columns in (left_columns, right_columns):
        for top in primary_rows:
            draw.line((columns[0], top, columns[-1], top), fill=(42, 42, 42))
        for left in columns:
            draw.line((left, primary_rows[0], left, primary_rows[-1]), fill=(42, 42, 42))

    # This weak network touches both cards.  It is neither card's bounded
    # continuation, so promotion must leave it to the later object stage.
    for top in (185, 230):
        draw.line((left_columns[0], top, right_columns[-1], top), fill=(23, 23, 23))
    for left in (*left_columns, *right_columns):
        draw.line((left, primary_rows[-1], left, 230), fill=(23, 23, 23))
    draw.line(
        (left_columns[-1], primary_rows[-1], right_columns[0], primary_rows[-1]),
        fill=(23, 23, 23),
    )

    drafts = _local_structure_rule_drafts(
        np.asarray(image),
        GeometryConfig(),
    )
    horizontal = tuple(draft for draft in drafts if draft.axis is RuleAxis.HORIZONTAL)
    vertical = tuple(draft for draft in drafts if draft.axis is RuleAxis.VERTICAL)

    assert len(horizontal) == 6
    assert len(vertical) == 6
    assert all(draft.bbox.right <= left_columns[-1] + 2 or draft.bbox.left >= right_columns[0] for draft in horizontal)
    assert all(draft.bbox.bottom <= primary_rows[-1] + 2 for draft in vertical)


@pytest.mark.parametrize("edge", ("top", "bottom", "left", "right"))
def test_repeated_edge_open_cells_promote_only_observed_finite_lines(
    edge: str,
) -> None:
    if edge in ("top", "bottom"):
        image = Image.new("RGB", (380, 180), (20, 20, 20))
        draw = ImageDraw.Draw(image)
        cross_coordinate = 99 if edge == "top" else 80
        raster_edge = 0 if edge == "top" else image.height - 1
        for start, stop in ((20, 120), (130, 230), (240, 340)):
            draw.line(
                (start, cross_coordinate, stop, cross_coordinate),
                fill=(42, 42, 42),
            )
            draw.line(
                (start, cross_coordinate, start, raster_edge),
                fill=(42, 42, 42),
            )
            draw.line(
                (stop, cross_coordinate, stop, raster_edge),
                fill=(42, 42, 42),
            )
        crossbar_axis = RuleAxis.HORIZONTAL
    else:
        image = Image.new("RGB", (180, 380), (20, 20, 20))
        draw = ImageDraw.Draw(image)
        cross_coordinate = 99 if edge == "left" else 80
        raster_edge = 0 if edge == "left" else image.width - 1
        for start, stop in ((20, 120), (130, 230), (240, 340)):
            draw.line(
                (cross_coordinate, start, cross_coordinate, stop),
                fill=(42, 42, 42),
            )
            draw.line(
                (cross_coordinate, start, raster_edge, start),
                fill=(42, 42, 42),
            )
            draw.line(
                (cross_coordinate, stop, raster_edge, stop),
                fill=(42, 42, 42),
            )
        crossbar_axis = RuleAxis.VERTICAL

    rgb = np.asarray(image)
    config = GeometryConfig()
    observed = detect_local_structures(
        rgb,
        LocalStructureConfig(
            minimum_contrast=8,
            minimum_length=config.min_rule_length,
            maximum_gap=1,
            maximum_line_thickness=config.max_rule_thickness,
            junction_tolerance=2,
        ),
    )
    drafts = _local_structure_rule_drafts(rgb, config)

    assert len(drafts) == 9
    assert sum(draft.axis is crossbar_axis for draft in drafts) == 3
    assert {(draft.axis.value, draft.bbox) for draft in drafts} == {
        (
            "horizontal" if line.axis == "horizontal" else "vertical",
            Box(*line.bbox),
        )
        for line in observed.lines
    }


def test_edge_open_detector_rejects_internal_crossbars_on_edge_long_stems() -> None:
    image = Image.new("RGB", (380, 180), (20, 20, 20))
    draw = ImageDraw.Draw(image)
    for start, stop in ((20, 120), (130, 230), (240, 340)):
        draw.line((start, 40, start, 179), fill=(42, 42, 42))
        draw.line((stop, 40, stop, 179), fill=(42, 42, 42))
        draw.line((start, 100, stop, 100), fill=(42, 42, 42))

    drafts = _local_structure_rule_drafts(
        np.asarray(image),
        GeometryConfig(),
    )

    assert drafts == ()


def test_repeated_thin_printed_stems_without_grid_crossings_remain_text() -> None:
    image = Image.new("RGB", (360, 80), BACKGROUND)
    draw = ImageDraw.Draw(image)
    for left in (20, 160, 320):
        draw.rectangle((left, 20, left + 1, 49), fill=BLACK)

    result = GeometryAnalyzer(
        GeometryConfig(deskew_max_degrees=0.0)
    ).analyze(image)

    assert result.segmentation.rules == ()
    assert tuple(segment.bbox for segment in result.segmentation.segments) == (
        Box(20, 20, 22, 50),
        Box(160, 20, 162, 50),
        Box(320, 20, 322, 50),
    )
    assert_exact_foreground_count(result, int(ink_mask(image).sum()))


def test_standalone_printed_dashes_without_underline_context_remain_text() -> None:
    image = Image.new("RGB", (240, 80), BACKGROUND)
    draw = ImageDraw.Draw(image)
    for top in (16, 39, 62):
        draw.rectangle((40, top, 69, top + 1), fill=BLACK)

    result = GeometryAnalyzer(
        GeometryConfig(deskew_max_degrees=0.0)
    ).analyze(image)

    assert result.segmentation.rules == ()
    assert tuple(segment.bbox for segment in result.segmentation.segments) == (
        Box(40, 16, 70, 18),
        Box(40, 39, 70, 41),
        Box(40, 62, 70, 64),
    )
    assert_exact_foreground_count(result, int(ink_mask(image).sum()))


def test_every_non_rule_foreground_component_has_one_owner() -> None:
    image = Image.new("RGB", (54, 27), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rectangle((2, 3, 4, 8), fill=BLUE)
    draw.rectangle((14, 4, 17, 10), fill=BLACK)
    draw.rectangle((42, 17, 46, 23), fill=RED)
    expected_ink = int(ink_mask(image).sum())

    result = analyze(image)

    assert result.segmentation.rules == ()
    assert_exact_foreground_count(result, expected_ink)
    component_ids = [
        component_id
        for segment in result.segmentation.segments
        for component_id in segment.component_ids
    ]
    assert len(component_ids) == len(set(component_ids))
    assert len(component_ids) == 3
    assert all(segment.component_ids for segment in result.segmentation.segments)
    assert all(segment.ink_pixels > 0 for segment in result.segmentation.segments)


def test_sparse_cells_and_spans_are_complete_canonical_and_deterministic() -> None:
    image = Image.new("RGB", (88, 52), BACKGROUND)
    draw = ImageDraw.Draw(image)
    for top in (5, 34):
        draw_glyph_group(draw, left=4, top=top)
        draw_glyph_group(draw, left=58, top=top)

    first = analyze(image)
    second = analyze(image.copy())

    assert first.segmentation == second.segmentation
    assert first.matrix == second.matrix
    segment_ids = tuple(segment.segment_id for segment in first.segmentation.segments)
    assert segment_ids
    assert tuple(span.segment_id for span in first.matrix.spans) == segment_ids
    assert first.matrix.segment_ids() == frozenset(segment_ids)
    assert {cell.segment_id for cell in first.matrix.cells} == set(segment_ids)
    assert first.matrix.cells == tuple(
        sorted(
            first.matrix.cells,
            key=lambda cell: (cell.row, cell.column, cell.segment_id),
        )
    )
    assert tuple(interval.index for interval in first.matrix.rows) == tuple(
        range(len(first.matrix.rows))
    )
    assert tuple(interval.index for interval in first.matrix.columns) == tuple(
        range(len(first.matrix.columns))
    )

    for previous, current in zip(first.matrix.rows, first.matrix.rows[1:]):
        assert previous.end <= current.start
    for previous, current in zip(first.matrix.columns, first.matrix.columns[1:]):
        assert previous.end <= current.start
    for span in first.matrix.spans:
        cells = [
            cell for cell in first.matrix.cells if cell.segment_id == span.segment_id
        ]
        assert cells
        assert min(cell.row for cell in cells) == span.row_start
        assert max(cell.row for cell in cells) + 1 == span.row_stop
        assert min(cell.column for cell in cells) == span.column_start
        assert max(cell.column for cell in cells) + 1 == span.column_stop
        assert all(
            span.row_start <= cell.row < span.row_stop
            and span.column_start <= cell.column < span.column_stop
            for cell in cells
        )
