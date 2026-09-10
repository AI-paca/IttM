from __future__ import annotations

import hashlib
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

import app.sparse_pipeline.geometry as geometry_module
from app.sparse_pipeline.contracts import (
    AffineTransform,
    Box,
    GeometryStatus,
    RecursiveNode,
    RuleAxis,
    SegmentationResult,
    SplitAxis,
)
from app.sparse_pipeline.geometry import (
    GeometryAnalyzer,
    GeometryConfig,
    GeometryLimitError,
)
from app.sparse_pipeline.geometry_artifacts import GeometryArtifactWriter
from app.sparse_pipeline.block_planning import (
    BlockPlanningConfig,
    OverlappingBlockPlanner,
)
from app.sparse_pipeline.object_reconstruction import (
    ObjectKind,
    ObjectReconstructor,
)
from app.sparse_pipeline.synthetic_samples import (
    SyntheticSample,
    SyntheticSpec,
    available_fonts,
    render_sample,
    smoke_specs,
    tiny_specs,
)


def _assert_exact_pixel_ownership(bundle: object, expected_mask: np.ndarray) -> None:
    foreground = bundle.foreground_mask
    rule_mask = bundle.rule_mask
    segment_mask = bundle.ownership >= 0
    result = bundle.result

    np.testing.assert_array_equal(foreground, expected_mask)
    assert not np.logical_and(rule_mask, segment_mask).any()
    np.testing.assert_array_equal(np.logical_or(rule_mask, segment_mask), foreground)
    assert bundle.ownership.min(initial=-1) >= -1
    assert bundle.ownership.max(initial=-1) < len(result.segmentation.segments)
    for index, segment in enumerate(result.segmentation.segments):
        assert int(np.count_nonzero(bundle.ownership == index)) == segment.ink_pixels
    assert sum(segment.ink_pixels for segment in result.segmentation.segments) + sum(
        rule.foreground_pixels for rule in result.segmentation.rules
    ) == int(expected_mask.sum())


def _assert_no_segment_crosses_known_lines(
    bundle: object,
    sample: SyntheticSample,
) -> None:
    """Use the generator's per-line truth, not a segment-count proxy."""

    line_owner = np.full(sample.expected_text_mask.shape, -1, dtype=np.int32)
    for line_index, line_mask in enumerate(sample.expected_line_masks):
        assert not np.any(line_owner[line_mask] >= 0), sample.spec.case_id
        line_owner[line_mask] = line_index
    np.testing.assert_array_equal(line_owner >= 0, sample.expected_text_mask)
    np.testing.assert_array_equal(bundle.ownership >= 0, sample.expected_text_mask)
    np.testing.assert_array_equal(bundle.rule_mask, sample.expected_frame_mask)

    for segment_index, segment in enumerate(bundle.result.segmentation.segments):
        expected_lines = np.unique(line_owner[bundle.ownership == segment_index])
        expected_lines = expected_lines[expected_lines >= 0]
        assert len(expected_lines) == 1, (
            sample.spec.case_id,
            segment.segment_id,
            tuple(int(value) for value in expected_lines),
        )


def _draw_glyph_rows() -> Image.Image:
    image = Image.new("RGB", (400, 200), "white")
    draw = ImageDraw.Draw(image)
    for top in range(30, 180, 25):
        for left in range(30, 350, 35):
            draw.rectangle((left, top, left + 20, top + 5), fill="black")
    return image


def _draw_four_groups() -> Image.Image:
    image = Image.new("RGB", (100, 60), "white")
    draw = ImageDraw.Draw(image)
    for top in (5, 40):
        for left in (5, 70):
            draw.rectangle((left, top, left + 10, top + 5), fill="black")
    return image


def _render_tight_single_line(
    text: str,
    font_path: str,
    font_size: int,
    *,
    padding: tuple[int, int, int, int] = (1, 1, 1, 1),
) -> tuple[Image.Image, np.ndarray]:
    """Render exact text bounds; roomy canvases hide false row lattices."""

    left, top, right, bottom = padding
    font = ImageFont.truetype(font_path, font_size)
    probe = Image.new("RGB", (1, 1), "white")
    bbox = ImageDraw.Draw(probe).textbbox((0, 0), text, font=font, anchor="lt")
    width = max(1, bbox[2] - bbox[0] + left + right)
    height = max(1, bbox[3] - bbox[1] + top + bottom)
    origin = (left - bbox[0], top - bbox[1])
    image = Image.new("RGB", (width, height), "white")
    ImageDraw.Draw(image).text(origin, text, font=font, fill="black", anchor="lt")
    mask_image = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask_image).text(origin, text, font=font, fill=255, anchor="lt")
    return image, np.asarray(mask_image, dtype=np.uint8) > 0


def _assert_custom_line_oracle(
    bundle: object,
    line_masks: tuple[np.ndarray, ...],
) -> None:
    line_owner = np.full(bundle.foreground_mask.shape, -1, dtype=np.int32)
    expected_text = np.zeros(bundle.foreground_mask.shape, dtype=bool)
    for line_index, line_mask in enumerate(line_masks):
        assert not np.logical_and(expected_text, line_mask).any()
        line_owner[line_mask] = line_index
        expected_text |= line_mask
    np.testing.assert_array_equal(bundle.foreground_mask, expected_text)
    assert not bundle.rule_mask.any()
    np.testing.assert_array_equal(bundle.ownership >= 0, expected_text)
    for segment_index in range(len(bundle.result.segmentation.segments)):
        owners = np.unique(line_owner[bundle.ownership == segment_index])
        owners = owners[owners >= 0]
        assert len(owners) == 1
    for line_index, line_mask in enumerate(line_masks):
        assert np.any(bundle.ownership[line_mask] >= 0), line_index


def _render_irregular_lines(
    lines: tuple[str, ...],
    font_path: str,
    font_sizes: tuple[int, ...],
    spacings: tuple[int, ...],
    x_offsets: tuple[int, ...],
    *,
    padding: tuple[int, int, int, int],
) -> tuple[Image.Image, tuple[np.ndarray, ...]]:
    assert len(lines) == len(font_sizes) == len(x_offsets)
    assert len(spacings) == len(lines) - 1
    fonts = tuple(ImageFont.truetype(font_path, size) for size in font_sizes)
    probe = Image.new("RGB", (1, 1), "white")
    probe_draw = ImageDraw.Draw(probe)
    boxes = tuple(probe_draw.textbbox((0, 0), line, font=font, anchor="lt") for line, font in zip(lines, fonts))
    left_pad, top_pad, right_pad, bottom_pad = padding
    widths = tuple(box[2] - box[0] for box in boxes)
    heights = tuple(max(1, box[3] - box[1]) for box in boxes)
    width = max(offset + value for offset, value in zip(x_offsets, widths)) + left_pad + right_pad
    height = top_pad + sum(heights) + sum(spacings) + bottom_pad
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    masks: list[np.ndarray] = []
    y = top_pad
    for line, font, box, line_height, x_offset in zip(lines, fonts, boxes, heights, x_offsets):
        origin = (left_pad + x_offset - box[0], y - box[1])
        draw.text(origin, line, font=font, fill="black", anchor="lt")
        mask_image = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask_image).text(origin, line, font=font, fill=255, anchor="lt")
        masks.append(np.asarray(mask_image, dtype=np.uint8) > 0)
        if len(masks) <= len(spacings):
            y += line_height + spacings[len(masks) - 1]
    return image, tuple(masks)


def test_all_73_known_smoke_samples_are_pixel_lossless_and_consistent() -> None:
    try:
        specs = smoke_specs()
    except FileNotFoundError:
        pytest.skip("multilingual fonts are unavailable")
    assert len(specs) == 73

    for spec in specs:
        sample = render_sample(spec)
        source_bytes = sample.image.tobytes()
        bundle = GeometryAnalyzer().analyze_bundle(sample.image)

        assert sample.image.tobytes() == source_bytes, spec.case_id
        assert bundle.result.alignment.correction_degrees == pytest.approx(0.0), spec.case_id
        _assert_exact_pixel_ownership(bundle, sample.expected_ink_mask)
        _assert_no_segment_crosses_known_lines(bundle, sample)
        matrix = bundle.result.matrix
        segment_ids = tuple(segment.segment_id for segment in bundle.result.segmentation.segments)
        assert tuple(span.segment_id for span in matrix.spans) == segment_ids, spec.case_id
        assert {cell.segment_id for cell in matrix.cells} == set(segment_ids), spec.case_id
        assert matrix.cells == tuple(
            sorted(matrix.cells, key=lambda cell: (cell.row, cell.column, cell.segment_id))
        ), spec.case_id

        known_line_count = len(sample.expected_text.splitlines())
        # Zero spacing is part of the promised corpus, not a relaxed case.  A
        # lossless foreground mask can still be structurally wrong when
        # adjacent rendered lines touch and collapse into one segment.
        assert len(bundle.result.segmentation.segments) >= known_line_count, spec.case_id
        if spec.border_pt == 0 and spec.layout in {"paragraph", "list"}:
            assert bundle.result.segmentation.rules == (), spec.case_id


def test_all_2464_tiny_samples_follow_the_exact_known_line_lattice() -> None:
    try:
        specs = tiny_specs()
    except FileNotFoundError:
        pytest.skip("multilingual fonts are unavailable")
    assert len(specs) == 2464

    for spec in specs:
        sample = render_sample(spec)
        source_bytes = sample.image.tobytes()
        bundle = GeometryAnalyzer().analyze_bundle(sample.image)

        assert sample.image.tobytes() == source_bytes, spec.case_id
        assert bundle.result.status is GeometryStatus.COMPLETE, spec.case_id
        _assert_exact_pixel_ownership(bundle, sample.expected_ink_mask)
        _assert_no_segment_crosses_known_lines(bundle, sample)


@pytest.mark.parametrize("language", ("en", "zh"))
def test_zero_spacing_rows_use_lossless_component_fallback(language: str) -> None:
    try:
        fonts = available_fonts(language)
    except FileNotFoundError:
        pytest.skip(f"no local {language} font")
    font_path = next((path for path in fonts if "NotoSerifCJK" in path), fonts[0])
    sample = render_sample(
        SyntheticSpec(
            case_id=f"zero-spacing-{language}",
            language=language,
            layout="paragraph",
            line_spacing=0,
            margin_pt=0,
            border_pt=0,
            font_size=14,
            font_path=font_path,
            background_rgb=(28, 31, 38),
            foreground_rgb=(242, 244, 247),
        )
    )

    bundle = GeometryAnalyzer().analyze_bundle(sample.image)

    _assert_exact_pixel_ownership(bundle, sample.expected_ink_mask)
    assert len(bundle.result.segmentation.segments) >= len(sample.expected_text.splitlines())


def test_text_strokes_are_not_rules_but_a_compact_grid_is() -> None:
    try:
        font_path = available_fonts("mixed")[0]
    except FileNotFoundError:
        pytest.skip("Noto CJK font is unavailable")
    text_image = Image.new("RGB", (600, 220), "white")
    draw = ImageDraw.Draw(text_image)
    font = ImageFont.truetype(font_path, 30)
    draw.text((12, 12), "lIlI \u4e00\u4e28", font=font, fill="black")
    text_result = GeometryAnalyzer().analyze(text_image)
    assert text_result.segmentation.rules == ()

    grid = Image.new("RGB", (600, 400), "white")
    draw = ImageDraw.Draw(grid)
    for left in (200, 260, 320):
        draw.line((left, 150, left, 230), fill="black", width=1)
    for top in (150, 190, 230):
        draw.line((200, top, 320, top), fill="black", width=1)
    grid_result = GeometryAnalyzer().analyze(grid)
    axes = tuple(rule.axis for rule in grid_result.segmentation.rules)
    assert axes.count(RuleAxis.HORIZONTAL) == 3
    assert axes.count(RuleAxis.VERTICAL) == 3
    assert grid_result.matrix.horizontal_rule_rows
    assert grid_result.matrix.vertical_rule_columns


def test_neutral_fills_keep_rule_bounded_cells_and_one_merged_row() -> None:
    image = Image.new("RGB", (480, 300), "white")
    draw = ImageDraw.Draw(image)
    left, right = 30, 450
    row_edges = (30, 70, 110, 150, 190, 230, 270)
    column_edges = (30, 114, 198, 282, 366, 450)

    draw.rectangle((left, row_edges[0], right, row_edges[1]), fill=(235, 235, 235))
    draw.rectangle((left, row_edges[2], right, row_edges[3]), fill=(245, 245, 245))
    for top in row_edges:
        draw.line((left, top, right, top), fill="black", width=2)
    for edge in (left, right):
        draw.line((edge, row_edges[0], edge, row_edges[-1]), fill="black", width=2)
    for edge in column_edges[1:-1]:
        draw.line((edge, row_edges[0], edge, row_edges[2]), fill="black", width=2)
        draw.line((edge, row_edges[3], edge, row_edges[-1]), fill="black", width=2)
    # Low-contrast PDF corner halos are foreground, but belong to the
    # adjacent structural border rather than becoming OCR segments.
    draw.line(
        (left - 1, row_edges[0] + 3, left - 1, row_edges[0] + 4),
        fill=(254, 254, 254),
        width=1,
    )
    draw.line(
        (left - 1, row_edges[-1] - 5, left - 1, row_edges[-1] - 2),
        fill=(254, 254, 254),
        width=1,
    )

    for row, (top, bottom) in enumerate(zip(row_edges, row_edges[1:])):
        if row == 2:
            for offset in (12, 82, 152):
                draw.rectangle((left + offset, top + 15, left + offset + 30, top + 21), fill="black")
            continue
        for cell_left, cell_right in zip(column_edges, column_edges[1:]):
            draw.rectangle(
                (cell_left + 2, top + 3, cell_right - 2, top + 4),
                fill=(250, 250, 250),
            )
            draw.rectangle((cell_left + 9, top + 15, cell_left + 27, top + 21), fill="black")
    # Break the synthetic canvas' otherwise perfectly symmetric white frame;
    # real document pages have headings/marginal evidence outside a table.
    draw.rectangle((0, 4, 3, 16), fill="black")

    geometry = GeometryAnalyzer().analyze(image)

    horizontal = tuple(rule for rule in geometry.segmentation.rules if rule.axis is RuleAxis.HORIZONTAL)
    vertical = tuple(rule for rule in geometry.segmentation.rules if rule.axis is RuleAxis.VERTICAL)
    assert len(horizontal) == len(row_edges)
    assert len(vertical) >= 2 + 2 * (len(column_edges) - 2)
    assert len(geometry.segmentation.segments) >= 26
    assert not any(
        segment.bbox.width == 1
        and segment.bbox.right == left
        and (segment.bbox.bottom <= row_edges[0] + 8 or segment.bbox.top >= row_edges[-1] - 8)
        for segment in geometry.segmentation.segments
    )

    merged = tuple(
        segment for segment in geometry.segmentation.segments if row_edges[2] < segment.bbox.center[1] < row_edges[3]
    )
    assert len(merged) == 1
    merged_span = next(span for span in geometry.matrix.spans if span.segment_id == merged[0].segment_id)
    assert merged[0].bbox.width >= right - left - 8
    assert merged_span.column_stop - merged_span.column_start >= 5

    objects = ObjectReconstructor().reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    tables = tuple(item for item in objects.objects if item.kind is ObjectKind.TABLE)
    assert len(tables) == 1
    assert len(tables[0].segment_ids) >= 26

    plan = OverlappingBlockPlanner(BlockPlanningConfig(max_core_segments=10, context_segments=2, padding=2)).plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
    )
    assert len(plan.blocks) >= 3
    assert len(plan.adjacent_algebra) == len(plan.blocks) - 1
    assert all(item.intersection_segment_ids for item in plan.adjacent_algebra)


@pytest.mark.parametrize(
    ("layout", "font_size", "margin_pt", "font_name", "background", "foreground"),
    (
        ("paragraph", 24, 0, "NotoSansCJK", (255, 255, 255), (0, 0, 0)),
        ("list", 10, 1, "NotoSerifCJK", (246, 243, 235), (12, 13, 15)),
    ),
)
def test_aligned_chinese_strokes_across_zero_spaced_rows_are_not_rules(
    layout: str,
    font_size: int,
    margin_pt: int,
    font_name: str,
    background: tuple[int, int, int],
    foreground: tuple[int, int, int],
) -> None:
    try:
        fonts = available_fonts("zh")
    except FileNotFoundError:
        pytest.skip("CJK fonts are unavailable")
    font_path = next((path for path in fonts if font_name in path), None)
    if font_path is None:
        pytest.skip(f"{font_name} is unavailable")
    sample = render_sample(
        SyntheticSpec(
            case_id=f"aligned-zh-{layout}",
            language="zh",
            layout=layout,
            line_spacing=0,
            margin_pt=margin_pt,
            border_pt=0,
            font_size=font_size,
            font_path=font_path,
            background_rgb=background,
            foreground_rgb=foreground,
        )
    )

    result = GeometryAnalyzer().analyze(sample.image)

    assert result.segmentation.rules == ()


def test_large_display_text_columns_are_not_structural_rules() -> None:
    """Exercise the real old fixture that exposed page-fraction false rules."""

    fixture = Path("/home/alpaca/GitHub/IttM-engine-v18-offline/debug/fixtures/SAMPLE_4k.png")
    if not fixture.is_file():
        pytest.skip("old SAMPLE_4k fixture is unavailable")

    with Image.open(fixture) as opened:
        opened.load()
        result = GeometryAnalyzer().analyze(opened)

    assert result.segmentation.rules == ()


def test_colored_old_table_keeps_row_rules_and_internal_divider() -> None:
    fixture = Path("/home/alpaca/GitHub/IttM-engine-v18-offline/debug/fixtures/image (10).png")
    if not fixture.is_file():
        pytest.skip("old colored-table fixture is unavailable")

    with Image.open(fixture) as opened:
        opened.load()
        result = GeometryAnalyzer().analyze(opened)

    horizontal = tuple(rule for rule in result.segmentation.rules if rule.axis is RuleAxis.HORIZONTAL)
    internal_vertical = tuple(
        rule
        for rule in result.segmentation.rules
        if rule.axis is RuleAxis.VERTICAL and 5 < rule.bbox.left < result.segmentation.aligned_size[0] - 5
    )
    # There are thirteen visible horizontal separators and one cyan divider;
    # allow slight grouping while still rejecting a one-segment false success.
    assert len(horizontal) >= 10
    assert internal_vertical
    for rule in (*horizontal, *internal_vertical):
        assert not any(
            (
                segment.bbox.left < rule.bbox.left and segment.bbox.right > rule.bbox.right
                if rule.axis is RuleAxis.VERTICAL
                else segment.bbox.top < rule.bbox.top and segment.bbox.bottom > rule.bbox.bottom
            )
            for segment in result.segmentation.segments
        )


def test_diacritics_and_dots_are_not_split_from_their_glyph_bodies() -> None:
    try:
        font_path = available_fonts("ru")[0]
    except FileNotFoundError:
        pytest.skip("Cyrillic font is unavailable")
    image = Image.new("RGB", (500, 100), "white")
    ImageDraw.Draw(image).text(
        (10, 10),
        "ё ё ё ё",
        font=ImageFont.truetype(font_path, 30),
        fill="black",
        anchor="lt",
    )

    result = GeometryAnalyzer().analyze(image)

    # Four whitespace-separated glyphs may stay together or become four
    # word-like segments, but accents must never become four extra rows.
    assert 1 <= len(result.segmentation.segments) <= 4
    assert all(segment.bbox.height >= 20 for segment in result.segmentation.segments)


@pytest.mark.parametrize(
    ("font_name", "font_size", "background", "foreground"),
    (
        ("LiberationSerif-Regular.ttf", 14, (28, 31, 38), (242, 244, 247)),
        ("DejaVuSerif.ttf", 30, (255, 255, 255), (0, 0, 0)),
    ),
)
def test_zero_spaced_cyrillic_list_keeps_accents_with_each_of_three_rows(
    font_name: str,
    font_size: int,
    background: tuple[int, int, int],
    foreground: tuple[int, int, int],
) -> None:
    try:
        fonts = available_fonts("ru")
    except FileNotFoundError:
        pytest.skip("Cyrillic fonts are unavailable")
    font_path = next((path for path in fonts if Path(path).name == font_name), None)
    if font_path is None:
        pytest.skip(f"{font_name} is unavailable")
    sample = render_sample(
        SyntheticSpec(
            case_id=f"zero-spaced-ru-{font_name}-{font_size}",
            language="ru",
            layout="list",
            line_spacing=0,
            margin_pt=3,
            border_pt=3,
            font_size=font_size,
            font_path=font_path,
            background_rgb=background,
            foreground_rgb=foreground,
        )
    )

    bundle = GeometryAnalyzer().analyze_bundle(sample.image)

    _assert_exact_pixel_ownership(bundle, sample.expected_ink_mask)
    assert len(bundle.result.segmentation.segments) == 3


def test_zero_spaced_short_row_is_not_merged_by_pixel_balance_guard() -> None:
    try:
        fonts = available_fonts("en")
    except FileNotFoundError:
        pytest.skip("English fonts are unavailable")
    font_path = next(
        (path for path in fonts if Path(path).name == "LiberationSerif-Regular.ttf"),
        None,
    )
    if font_path is None:
        pytest.skip("Liberation Serif is unavailable")
    sample = render_sample(
        SyntheticSpec(
            case_id="zero-spaced-short-en-row",
            language="en",
            layout="combined",
            line_spacing=0,
            margin_pt=0,
            border_pt=1,
            font_size=10,
            font_path=font_path,
        )
    )

    bundle = GeometryAnalyzer().analyze_bundle(sample.image)

    _assert_exact_pixel_ownership(bundle, sample.expected_ink_mask)
    assert len(bundle.result.segmentation.segments) >= len(sample.expected_text.splitlines())


@pytest.mark.parametrize(
    ("case_id", "font_name", "margin_pt", "border_pt", "background", "foreground"),
    (
        (
            "false-diacritic-bridge-between-list-rows",
            "DejaVuSans.ttf",
            0,
            0,
            (28, 31, 38),
            (242, 244, 247),
        ),
        (
            "merged-components-must-not-inflate-body-height",
            "LiberationSerif-Regular.ttf",
            3,
            3,
            (28, 31, 38),
            (242, 244, 247),
        ),
    ),
)
def test_zero_spaced_list_keeps_three_rows_across_body_and_diacritic_guards(
    case_id: str,
    font_name: str,
    margin_pt: int,
    border_pt: int,
    background: tuple[int, int, int],
    foreground: tuple[int, int, int],
) -> None:
    try:
        font_path = next(path for path in available_fonts("en") if Path(path).name == font_name)
    except (FileNotFoundError, StopIteration):
        pytest.skip(f"{font_name} is unavailable")
    sample = render_sample(
        SyntheticSpec(
            case_id=case_id,
            language="en",
            layout="list",
            line_spacing=0,
            margin_pt=margin_pt,
            border_pt=border_pt,
            font_size=14,
            font_path=font_path,
            background_rgb=background,
            foreground_rgb=foreground,
        )
    )

    bundle = GeometryAnalyzer().analyze_bundle(sample.image)

    _assert_exact_pixel_ownership(bundle, sample.expected_ink_mask)
    assert len(bundle.result.segmentation.segments) == 3


def test_zero_spaced_cyrillic_paragraph_has_no_accent_only_micro_segments() -> None:
    try:
        fonts = available_fonts("ru")
    except FileNotFoundError:
        pytest.skip("Cyrillic fonts are unavailable")
    font_path = next((path for path in fonts if Path(path).name == "DejaVuSans.ttf"), None)
    if font_path is None:
        pytest.skip("DejaVu Sans is unavailable")
    sample = render_sample(
        SyntheticSpec(
            case_id="zero-spaced-ru-paragraph",
            language="ru",
            layout="paragraph",
            line_spacing=0,
            margin_pt=0,
            border_pt=0,
            font_size=24,
            font_path=font_path,
        )
    )

    bundle = GeometryAnalyzer().analyze_bundle(sample.image)

    _assert_exact_pixel_ownership(bundle, sample.expected_ink_mask)
    assert all(segment.bbox.height >= 6 for segment in bundle.result.segmentation.segments)


@pytest.mark.parametrize("border_pt", (1, 2, 3))
def test_page_frames_from_one_to_three_points_do_not_block_rows(border_pt: int) -> None:
    try:
        font_path = available_fonts("en")[0]
    except FileNotFoundError:
        pytest.skip("English font is unavailable")
    sample = render_sample(
        SyntheticSpec(
            case_id=f"frame-{border_pt}",
            language="en",
            layout="list",
            line_spacing=5,
            margin_pt=0,
            border_pt=border_pt,
            font_size=18,
            font_path=font_path,
        )
    )

    bundle = GeometryAnalyzer().analyze_bundle(sample.image)

    _assert_exact_pixel_ownership(bundle, sample.expected_ink_mask)
    assert len(bundle.result.segmentation.rules) >= 4
    assert len(bundle.result.segmentation.segments) >= 3


@pytest.mark.parametrize(
    ("language", "layout", "line_spacing", "border_pt"),
    (
        ("en", "list", 4, 1),
        ("ru", "combined", 0, 3),
    ),
)
def test_zero_margin_frame_never_claims_an_adjacent_text_row(
    language: str,
    layout: str,
    line_spacing: int,
    border_pt: int,
) -> None:
    try:
        font_path = next(path for path in available_fonts(language) if Path(path).name == "LiberationSerif-Regular.ttf")
    except (FileNotFoundError, StopIteration):
        pytest.skip("Liberation Serif is unavailable")
    sample = render_sample(
        SyntheticSpec(
            case_id=f"adjacent-frame-{language}",
            language=language,
            layout=layout,
            line_spacing=line_spacing,
            margin_pt=0,
            border_pt=border_pt,
            font_size=24,
            font_path=font_path,
        )
    )

    bundle = GeometryAnalyzer().analyze_bundle(sample.image)

    _assert_exact_pixel_ownership(bundle, sample.expected_ink_mask)
    _assert_no_segment_crosses_known_lines(bundle, sample)


def test_synthetic_margin_zero_never_clips_text_and_frame_never_overpaints_it() -> None:
    try:
        fonts = available_fonts("en")
    except FileNotFoundError:
        pytest.skip("English font is unavailable")
    font_path = next((path for path in fonts if "Serif" in Path(path).name), fonts[0])

    def render(*, margin_pt: int, border_pt: int) -> object:
        return render_sample(
            SyntheticSpec(
                case_id=f"synthetic-geometry-{margin_pt}-{border_pt}",
                language="en",
                layout="paragraph",
                line_spacing=5,
                margin_pt=margin_pt,
                border_pt=border_pt,
                font_size=18,
                font_path=font_path,
            )
        )

    unpadded = render(margin_pt=0, border_pt=0)
    padded = render(margin_pt=3, border_pt=0)
    framed = render(margin_pt=0, border_pt=3)
    text_ink = int(unpadded.expected_ink_mask.sum())
    assert int(padded.expected_ink_mask.sum()) == text_ink

    border_pixels = round(3 * 300 / 72)
    width, height = framed.image.size
    frame_ink = width * height - (width - 2 * border_pixels) * (height - 2 * border_pixels)
    assert int(framed.expected_ink_mask.sum()) == text_ink + frame_ink
    assert len(GeometryAnalyzer().analyze(framed.image).segmentation.rules) == 4


def test_transparent_png_is_composited_without_losing_opaque_ink() -> None:
    image = Image.new("RGBA", (100, 50), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((10, 10, 20, 30), fill=(0, 0, 0, 255))
    source_bytes = image.tobytes()

    bundle = GeometryAnalyzer().analyze_bundle(image)

    assert image.tobytes() == source_bytes
    assert bundle.result.alignment.background_rgb == (255, 255, 255)
    assert bundle.result.alignment.foreground_pixels == 231
    assert bundle.result.alignment.content_bbox == Box(10, 10, 21, 31)


def test_deskew_applies_the_estimated_correction_in_the_same_convention() -> None:
    skewed = _draw_glyph_rows().rotate(
        2.0,
        resample=Image.Resampling.NEAREST,
        expand=False,
        fillcolor="white",
    )

    bundle = GeometryAnalyzer().analyze_bundle(skewed)

    assert bundle.result.alignment.correction_degrees == pytest.approx(-2.0, abs=0.11)
    assert np.count_nonzero(bundle.foreground_mask.sum(axis=1) == 0) >= 100
    assert len(bundle.result.segmentation.segments) >= 6
    transform = bundle.result.alignment.transform
    for point in (
        (0.0, 0.0),
        (399.0, 0.0),
        (0.0, 199.0),
        (399.0, 199.0),
        (147.25, 83.75),
    ):
        assert transform.point_to_source(*transform.point_to_aligned(*point)) == pytest.approx(point, rel=0.0, abs=1e-6)
    aligned_width, aligned_height = transform.aligned_size
    for box in (
        Box(0, 0, 1, 1),
        Box(aligned_width - 1, 0, aligned_width, 1),
        Box(0, aligned_height - 1, 1, aligned_height),
        Box(aligned_width - 1, aligned_height - 1, aligned_width, aligned_height),
    ):
        source = transform.box_to_source(box)
        assert 0 <= source.left < source.right <= transform.original_size[0]
        assert 0 <= source.top < source.bottom <= transform.original_size[1]


def test_affine_contract_rejects_a_matrix_that_only_matches_origin_and_center() -> None:
    identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    wrong_inverse = (1.5, -1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)

    with pytest.raises(ValueError, match="inverse|round-trip"):
        AffineTransform((100, 50), (100, 50), identity, wrong_inverse)


def test_segmentation_contract_rejects_a_cyclic_recursive_graph() -> None:
    root = RecursiveNode(
        node_id="root",
        bbox=Box(0, 0, 2, 2),
        depth=0,
        parent_id="child",
        axis=SplitAxis.ROWS,
        child_ids=("child",),
        separator_boxes=(Box(0, 1, 2, 2),),
    )
    child = RecursiveNode(
        node_id="child",
        bbox=Box(0, 0, 2, 1),
        depth=1,
        parent_id="root",
        axis=SplitAxis.ROWS,
        child_ids=("root",),
        separator_boxes=(Box(0, 1, 2, 2),),
    )

    with pytest.raises(ValueError, match="cycle|root|parent"):
        SegmentationResult(
            segments=(),
            rules=(),
            nodes=(root, child),
            root_node_id="root",
            aligned_size=(2, 2),
            foreground_pixels=0,
        )


@pytest.mark.parametrize(
    ("config", "message"),
    (
        (GeometryConfig(max_input_pixels=399), "input pixel limit"),
        (
            GeometryConfig(max_input_pixels=400, max_aligned_pixels=399),
            "aligned pixel limit",
        ),
    ),
)
def test_pixel_limits_fail_before_rgb_allocation(
    monkeypatch: pytest.MonkeyPatch,
    config: GeometryConfig,
    message: str,
) -> None:
    image = Image.new("RGB", (20, 20), "white")

    def allocation_must_not_start(*_: object, **__: object) -> np.ndarray:
        raise AssertionError("RGB allocation started before the configured pixel bound")

    monkeypatch.setattr(geometry_module, "_source_rgb", allocation_must_not_start)
    with pytest.raises(GeometryLimitError, match=message):
        GeometryAnalyzer(config).analyze(image)


def test_expanded_deskew_limit_fails_before_aligned_raster_allocation() -> None:
    skewed = _draw_glyph_rows().rotate(
        2.0,
        resample=Image.Resampling.NEAREST,
        expand=False,
        fillcolor="white",
    )
    config = GeometryConfig(max_input_pixels=80_000, max_aligned_pixels=82_000)

    with pytest.raises(GeometryLimitError, match="aligned pixel limit"):
        GeometryAnalyzer(config).analyze(skewed)


def test_run_and_component_limits_bound_pathological_rasters() -> None:
    yy, xx = np.indices((20, 20))
    checker = np.full((20, 20, 3), 255, dtype=np.uint8)
    checker[(xx + yy) % 2 == 0] = 0
    with pytest.raises(GeometryLimitError, match="run limit"):
        GeometryAnalyzer(GeometryConfig(max_runs=50, deskew_max_degrees=0.0)).analyze(
            Image.fromarray(checker, mode="RGB")
        )

    isolated = np.full((24, 24, 3), 255, dtype=np.uint8)
    isolated[1:24:3, 1:24:3] = 0
    with pytest.raises(GeometryLimitError, match="component count limit"):
        GeometryAnalyzer(GeometryConfig(max_runs=100, max_components=5, deskew_max_degrees=0.0)).analyze(
            Image.fromarray(isolated, mode="RGB")
        )


def test_body_height_ignores_abundant_single_pixel_raster_fragments() -> None:
    mask = np.zeros((24, 80), dtype=bool)
    mask[0, ::2] = True
    for index in range(8):
        left = index * 9
        mask[8:20, left : left + 5] = True
    components = geometry_module._connected_components(mask)

    assert len(components) > 40
    assert geometry_module._body_component_height(components, 75) == 12.0


@pytest.mark.parametrize("body_count", range(1, 9))
def test_body_height_ignores_dust_for_short_text_too(body_count: int) -> None:
    mask = np.zeros((30, 160), dtype=bool)
    for index in range(40):
        mask[index % 2, (index * 3) % 150] = True
    for index in range(body_count):
        left = 5 + index * 18
        mask[10:22, left : left + 5] = True
    components = geometry_module._connected_components(mask)

    assert sum(component.pixels >= 4 and component.bbox.height >= 3 for component in components) == body_count
    assert geometry_module._body_component_height(components, 75) == 12.0


@pytest.mark.parametrize("text", ("ё", "й"))
@pytest.mark.parametrize("with_dust", (False, True))
def test_single_cyrillic_diacritic_stays_with_its_body_despite_dust(
    text: str,
    with_dust: bool,
) -> None:
    try:
        font_path = next(path for path in available_fonts("ru") if Path(path).name == "DejaVuSans.ttf")
    except (FileNotFoundError, StopIteration):
        pytest.skip("DejaVu Sans Cyrillic font is unavailable")
    image = Image.new("RGB", (360, 100), "white")
    draw = ImageDraw.Draw(image)
    draw.text(
        (150, 25),
        text,
        font=ImageFont.truetype(font_path, 30),
        fill="black",
        anchor="lt",
    )
    if with_dust:
        for index in range(40):
            draw.point(((index * 7) % 140, 2 + (index % 2) * 3), fill="black")

    result = GeometryAnalyzer(GeometryConfig(deskew_max_degrees=0.0)).analyze(image)
    glyph_segments = tuple(segment for segment in result.segmentation.segments if segment.bbox.right > 140)

    assert len(glyph_segments) == 1
    assert glyph_segments[0].bbox.height >= 20


def test_tiny_valley_relaxation_does_not_slice_single_text_rows() -> None:
    texts = {
        "en": "B8gQWMWM",
        "ru": "ЖФЩЦЖФЩЦ",
        "zh": "中華語文測試",
    }
    try:
        fonts = {language: available_fonts(language) for language in texts}
    except FileNotFoundError:
        pytest.skip("multilingual fonts are unavailable")

    for language, text in texts.items():
        for font_path in fonts[language]:
            for font_size in range(6, 11):
                image = Image.new("RGB", (240, 40), "white")
                ImageDraw.Draw(image).text(
                    (5, 5),
                    text,
                    font=ImageFont.truetype(font_path, font_size),
                    fill="black",
                    anchor="lt",
                )

                result = GeometryAnalyzer(GeometryConfig(deskew_max_degrees=0.0)).analyze(image)

                assert len(result.segmentation.segments) == 1, (
                    language,
                    Path(font_path).name,
                    font_size,
                )
                assert result.segmentation.rules == ()


def test_tight_symmetric_single_rows_never_invent_a_repeated_row_lattice() -> None:
    """A one-line raster must not be explained as three harmonic rows."""

    texts = {
        "en": "A simple document keeps enough context.",
        "ru": "Ёж и йод сохраняют контекст.",
        "zh": "简单文档保留识别上下文。",
    }
    try:
        fonts = {language: available_fonts(language) for language in texts}
    except FileNotFoundError:
        pytest.skip("multilingual fonts are unavailable")

    for language, text in texts.items():
        for font_path in fonts[language]:
            for font_size in range(6, 31):
                image, expected_mask = _render_tight_single_line(
                    text,
                    font_path,
                    font_size,
                )
                bundle = GeometryAnalyzer(GeometryConfig(deskew_max_degrees=0.0)).analyze_bundle(image)

                np.testing.assert_array_equal(bundle.foreground_mask, expected_mask)
                assert bundle.result.segmentation.rules == ()
                assert not any(node.axis is SplitAxis.ROWS for node in bundle.result.segmentation.nodes), (
                    language,
                    Path(font_path).name,
                    font_size,
                    bundle.result.diagnostics,
                )


def test_all_cyrillic_diacritic_glyphs_keep_one_owner_with_and_without_dust() -> None:
    try:
        fonts = available_fonts("ru")
    except FileNotFoundError:
        pytest.skip("Cyrillic fonts are unavailable")

    for font_path in fonts:
        for font_size in range(6, 31):
            for text in ("ё", "й", "Ё", "Й"):
                glyph_image, glyph_mask = _render_tight_single_line(
                    text,
                    font_path,
                    font_size,
                    padding=(40, 7, 24, 11),
                )
                for with_dust in (False, True):
                    image = glyph_image.copy()
                    if with_dust:
                        draw = ImageDraw.Draw(image)
                        for index in range(24):
                            draw.point(
                                (
                                    2 + (index * 7) % 31,
                                    1 + (index * 5) % max(1, image.height - 2),
                                ),
                                fill="black",
                            )
                    bundle = GeometryAnalyzer(GeometryConfig(deskew_max_degrees=0.0)).analyze_bundle(image)
                    glyph_owners = np.unique(bundle.ownership[glyph_mask])
                    glyph_owners = glyph_owners[glyph_owners >= 0]
                    assert len(glyph_owners) == 1, (
                        Path(font_path).name,
                        font_size,
                        text,
                        with_dust,
                        bundle.result.diagnostics,
                    )
                    assert not bundle.rule_mask[glyph_mask].any()


@pytest.mark.parametrize("language", ("en", "ru", "zh"))
def test_asymmetric_pages_keep_irregular_rows_semantically_separate(
    language: str,
) -> None:
    try:
        font_path = available_fonts(language)[-1]
    except FileNotFoundError:
        pytest.skip(f"no local {language} font")
    lines = {
        "en": (
            "Short left",
            "A much wider centered second row",
            "tail",
            "Final uneven baseline",
        ),
        "ru": (
            "Коротко слева",
            "Широкая вторая строка по центру",
            "хвост",
            "Неровная последняя строка",
        ),
        "zh": ("左侧短行", "较宽的第二行文本", "尾行", "不规则的最后一行"),
    }[language]
    image, line_masks = _render_irregular_lines(
        lines,
        font_path,
        (11, 19, 8, 15),
        (0, 7, 2),
        (0, 47, 9, 83),
        padding=(2, 1, 137, 29),
    )

    bundle = GeometryAnalyzer(GeometryConfig(deskew_max_degrees=0.0)).analyze_bundle(image)

    _assert_custom_line_oracle(bundle, line_masks)
    for node in bundle.result.segmentation.nodes:
        if node.axis is not SplitAxis.ROWS or node.split_coordinate is None:
            continue
        coordinate = node.split_coordinate
        assert not any(line_mask[:coordinate].any() and line_mask[coordinate:].any() for line_mask in line_masks), (
            language,
            node.node_id,
            coordinate,
            bundle.result.diagnostics,
        )


@pytest.mark.parametrize("border_pt", (0, 1, 2, 3))
@pytest.mark.parametrize(
    ("background", "foreground"),
    (
        ((255, 255, 255), (0, 0, 0)),
        ((246, 243, 235), (12, 13, 15)),
        ((28, 31, 38), (242, 244, 247)),
    ),
)
def test_frame_aware_background_keeps_tiny_text_and_only_real_frames(
    border_pt: int,
    background: tuple[int, int, int],
    foreground: tuple[int, int, int],
) -> None:
    try:
        font_path = next(path for path in available_fonts("en") if Path(path).name == "LiberationSans-Regular.ttf")
    except (FileNotFoundError, StopIteration):
        pytest.skip("Liberation Sans is unavailable")
    sample = render_sample(
        SyntheticSpec(
            case_id=f"frame-background-{border_pt}-{background}",
            language="en",
            layout="paragraph",
            line_spacing=1,
            margin_pt=0,
            border_pt=border_pt,
            font_size=7,
            font_path=font_path,
            background_rgb=background,
            foreground_rgb=foreground,
        )
    )

    bundle = GeometryAnalyzer().analyze_bundle(sample.image)

    _assert_exact_pixel_ownership(bundle, sample.expected_ink_mask)
    assert bundle.result.alignment.background_rgb == background
    assert len(bundle.result.segmentation.segments) == 3
    if border_pt:
        assert len(bundle.result.segmentation.rules) == 4
    else:
        assert bundle.result.segmentation.rules == ()


def test_root_segment_ids_follow_global_segment_order_and_tree_debug_is_preorder() -> None:
    result = GeometryAnalyzer().analyze(_draw_four_groups())
    root = next(node for node in result.segmentation.nodes if node.node_id == result.segmentation.root_node_id)
    expected_ids = tuple(segment.segment_id for segment in result.segmentation.segments)
    assert root.segment_ids == expected_ids

    positions = {node.node_id: index for index, node in enumerate(result.segmentation.nodes)}
    for node in result.segmentation.nodes:
        if not node.child_ids:
            continue
        assert positions[node.node_id] < positions[node.child_ids[0]]
        for first, second in zip(node.child_ids, node.child_ids[1:]):
            first_prefix = f"{first}."
            first_subtree_end = max(
                index
                for candidate, index in positions.items()
                if candidate == first or candidate.startswith(first_prefix)
            )
            assert first_subtree_end < positions[second]


def test_geometry_is_deterministic_under_parallel_independent_runs() -> None:
    image = _draw_four_groups()

    def run(_: int) -> tuple[object, str]:
        bundle = GeometryAnalyzer().analyze_bundle(image)
        digest = hashlib.sha256(
            bundle.foreground_mask.tobytes() + bundle.rule_mask.tobytes() + bundle.ownership.tobytes()
        ).hexdigest()
        return bundle.result, digest

    with ThreadPoolExecutor(max_workers=8) as executor:
        values = tuple(executor.map(run, range(24)))
    assert len({repr(result) for result, _ in values}) == 1
    assert len({digest for _, digest in values}) == 1


def test_artifact_publish_has_one_concurrent_winner_and_no_partial_dirs(
    tmp_path: Path,
) -> None:
    bundle = GeometryAnalyzer().analyze_bundle(_draw_four_groups())

    def publish(_: int) -> str:
        try:
            GeometryArtifactWriter().write(tmp_path, run_id="race", bundle=bundle)
        except FileExistsError:
            return "exists"
        return "published"

    with ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = tuple(executor.map(publish, range(24)))
    assert outcomes.count("published") == 1
    assert outcomes.count("exists") == 23
    assert (tmp_path / "race" / "01-geometry" / "manifest.json").is_file()
    assert not tuple(tmp_path.glob(".race.partial-*"))


def test_failed_artifact_write_rolls_back_the_partial_directory(tmp_path: Path) -> None:
    bundle = GeometryAnalyzer().analyze_bundle(_draw_four_groups())

    class BrokenWriter(GeometryArtifactWriter):
        def _write_bundle(self, stage_dir: Path, bundle: object) -> None:
            (stage_dir / "partial.txt").write_text("partial", encoding="utf-8")
            raise OSError("injected debug write failure")

    with pytest.raises(OSError, match="injected debug write failure"):
        BrokenWriter().write(tmp_path, run_id="broken", bundle=bundle)
    assert not (tmp_path / "broken").exists()
    assert not tuple(tmp_path.glob(".broken.partial-*"))


def test_limit_is_explicitly_degraded_in_result_and_artifact(tmp_path: Path) -> None:
    bundle = GeometryAnalyzer(GeometryConfig(max_nodes=1)).analyze_bundle(_draw_four_groups())

    assert bundle.result.status is GeometryStatus.DEGRADED
    assert bundle.result.limit_leaf_count == 1
    run_dir = GeometryArtifactWriter().write(tmp_path, run_id="limited", bundle=bundle)
    manifest = json.loads((run_dir / "01-geometry" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "degraded"
    assert manifest["limit_leaf_count"] == 1
    crop_root = run_dir / "01-geometry" / "segment-crops"
    crop_manifest = json.loads((crop_root / "manifest.json").read_text(encoding="utf-8"))
    assert crop_manifest["segments"] == len(bundle.result.segmentation.segments)
    assert (crop_root / "gallery.md").is_file()
    for item in crop_manifest["items"]:
        assert (crop_root / item["raw"]).is_file()
        assert (crop_root / item["isolated"]).is_file()
        assert item["ownership_pixels"] == item["ink_pixels"]


def test_large_segment_crop_evidence_uses_one_archive(tmp_path: Path) -> None:
    bundle = GeometryAnalyzer().analyze_bundle(_draw_four_groups())

    class CompactWriter(GeometryArtifactWriter):
        INDIVIDUAL_SEGMENT_CROP_LIMIT = 1

    run_dir = CompactWriter().write(tmp_path, run_id="compact", bundle=bundle)
    crop_root = run_dir / "01-geometry" / "segment-crops"
    manifest = json.loads((crop_root / "manifest.json").read_text("utf-8"))
    assert manifest["storage"] == "archive"
    assert manifest["archive"] == "segments.zip"
    assert manifest["contact_sheets"]
    assert all(value.endswith(".svg") for value in manifest["contact_sheets"])
    assert not (crop_root / "raw").exists()
    with zipfile.ZipFile(crop_root / "segments.zip") as archive:
        members = set(archive.namelist())
    for item in manifest["items"]:
        assert item["raw"] is None
        assert item["isolated"] is None
        assert item["raw_archive_member"] in members
        assert item["isolated_archive_member"] in members


def test_unstable_paper_background_uses_adaptive_ink() -> None:
    height, width = 240, 400
    gradient = np.linspace(180, 252, width, dtype=np.uint8)
    gray = np.repeat(gradient[None, :], height, axis=0)
    rgb = np.repeat(gray[:, :, None], 3, axis=2)
    rgb[40:48, 45:355] = 35
    rgb[90:98, 70:330] = 45
    physical = np.ones((height, width), dtype=bool)

    selected, mode = geometry_module._select_layout_foreground(rgb, physical)

    assert mode.startswith("adaptive-")
    assert 0 < int(selected.sum()) < int(physical.sum() * 0.20)


def test_dominant_digital_background_keeps_physical_ink() -> None:
    rgb = np.full((120, 240, 3), 255, dtype=np.uint8)
    rgb[20:100, 35:205] = 0
    physical = np.any(rgb != 255, axis=2)

    selected, mode = geometry_module._select_layout_foreground(rgb, physical)

    assert mode == "physical"
    assert np.array_equal(selected, physical)
