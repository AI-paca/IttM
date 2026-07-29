import json
from dataclasses import asdict
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.chunking.vertical import LayoutRegion, TableCell, TableLayout
from app.layout.contracts import ComponentFeature, LayoutDecision, LayoutFeatures, LayoutStageSpec, SeparatorCandidate
from app.layout.features import ProjectionGeometryExtractor
from app.layout.recursive_grid import (
    RecursiveGridConfig,
    RecursiveGridLeaf,
    RegionDecision,
    SPARSE_SHADOW_CODES,
    SparseShadowProjection,
    SparseShadowSignature,
    analyze_recursive_grid,
    classify_sparse_shadow,
    _deduplicate_leaves,
    _dominant_vertical_rule_tracks,
    _horizontal_projection,
    _horizontal_projection_mask,
    _horizontal_separator,
    _left_tracks,
    _remove_dominant_rules,
    _vertical_lane_separator,
    group_recursive_leaves,
    project_sparse_shadow,
    recursive_grid_trace,
    segment_recursive_grid,
    sparse_shadow_signature,
)
from app.layout.selectors import select_layout_pipeline
from app.layout.stages import (
    _gradient_table_layout,
    _full_page_table_lacks_early_vertical_support,
    _is_gutter_card_table,
    _is_structural_only_leaf,
    _is_unreliable_partition_table,
    _merge_table_pair_through_bridge,
    _prefer_recursive_table_layout,
    _simple_track_table_layout,
    _simple_group_table_has_spanning_rules,
    _is_decorative_narrow_table,
    _is_decorative_partition_table,
    _table_region_with_outer_bands,
    _vertical_cuts_for_band,
    execute_layout_decision,
)


def test_rejects_tall_narrow_two_column_decoration_as_table():
    image = Image.new("RGB", (2550, 1626), "white")
    table = TableLayout(
        rows=68,
        cols=2,
        cells=(),
        bbox=(0, 0, 192, 1626),
        x_lines=(0, 96, 192),
        y_lines=tuple(round(index * 1626 / 68) for index in range(69)),
    )
    try:
        assert _is_decorative_narrow_table(image, table) is True
        region = LayoutRegion(
            kind="table",
            image=image.crop(table.bbox),
            bbox=table.bbox,
            table=table,
        )
        try:
            assert _is_decorative_partition_table(region, image.size) is True
        finally:
            region.image.close()
        assert (
            _is_decorative_narrow_table(
                image,
                TableLayout(
                    rows=14,
                    cols=10,
                    cells=(),
                    bbox=(90, 272, 2450, 1500),
                    x_lines=tuple(range(11)),
                    y_lines=tuple(range(15)),
                ),
            )
            is False
        )
    finally:
        image.close()


def test_table_fast_path_preserves_visible_outer_bands():
    image = Image.new("RGB", (600, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 20), "document heading", fill="black")
    draw.rectangle((20, 100, 580, 400), outline="black", width=3)
    draw.text((20, 460), "document footer", fill="black")
    table_image = image.crop((20, 100, 580, 400))
    table_region = LayoutRegion(
        kind="table",
        image=table_image,
        bbox=(20, 100, 580, 400),
        table=TableLayout(
            rows=1,
            cols=1,
            cells=(TableCell(row=0, col=0, bbox=(0, 0, 560, 300)),),
            bbox=(0, 0, 560, 300),
            x_lines=(0, 560),
            y_lines=(0, 300),
        ),
    )
    regions = []
    try:
        regions = _table_region_with_outer_bands(image, table_region)
        assert [(region.kind, region.bbox) for region in regions] == [
            ("image", (0, 0, 600, 100)),
            ("table", (20, 100, 580, 400)),
            ("image", (0, 400, 600, 500)),
        ]
        assert regions[0].metadata == {
            "layout_kind": "recursive_grid_outer_band",
            "position": "above",
        }
    finally:
        for region in regions:
            region.image.close()
        image.close()


def _marker_page(columns: int, rows: int = 4):
    width = 1200
    header_height = 160
    gap = 30
    row_height = 260
    height = header_height + gap + rows * (row_height + gap)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    header_marker = (190, 30, 80)
    draw.rectangle(
        (20, 20, width - 20, 130),
        fill=(245, 245, 245),
        outline="black",
        width=4,
    )
    draw.rectangle((40, 40, 60, 60), fill=header_marker)
    draw.text((90, 55), "FULL WIDTH", fill="black")

    cell_width = (width - (columns + 1) * gap) // columns
    markers = []
    top = header_height + gap
    for row in range(rows):
        for column in range(columns):
            left = gap + column * (cell_width + gap)
            marker = (
                20 + row * 35,
                40 + column * 25,
                80 + row * 15 + column,
            )
            markers.append(marker)
            draw.rectangle(
                (left, top, left + cell_width, top + row_height),
                outline="black",
                width=3,
            )
            draw.rectangle(
                (left + 12, top + 12, left + 24, top + 24),
                fill=marker,
            )
            draw.text(
                (left + 35, top + 45),
                f"R{row}-C{column}",
                fill="black",
            )
        top += row_height + gap
    return image, header_marker, markers


def _colors(image: Image.Image):
    return {color for _, color in image.convert("RGB").getcolors(maxcolors=image.width * image.height)}


def test_recursive_analysis_exposes_one_atomic_stage_handoff():
    image, _, _ = _marker_page(columns=2, rows=2)
    analysis = None
    try:
        analysis = analyze_recursive_grid(
            image,
            RecursiveGridConfig(
                max_region_height=500,
                deskew=False,
                preprocess_steps=(),
            ),
        )

        assert analysis.leaves
        assert tuple(analysis.leaves) == tuple(
            sorted(
                analysis.leaves,
                key=lambda leaf: (
                    leaf.source_bbox[1],
                    leaf.source_bbox[0],
                    leaf.source_bbox[3],
                ),
            )
        )
        assert analysis.signature == sparse_shadow_signature(analysis.projection)
        assert analysis.profile == classify_sparse_shadow(analysis.signature)
        projected_leaves = tuple(item[0] for item in analysis.projection.leaf_projection)
        assert projected_leaves == analysis.leaves
    finally:
        if analysis is not None:
            for leaf in analysis.leaves:
                leaf.image.close()
        image.close()


def test_recursive_analysis_trace_is_versioned_and_image_free():
    image, _, _ = _marker_page(columns=2, rows=2)
    analysis = None
    try:
        analysis = analyze_recursive_grid(
            image,
            RecursiveGridConfig(
                max_region_height=500,
                deskew=False,
                preprocess_steps=(),
            ),
        )

        trace = recursive_grid_trace(analysis)
        payload = asdict(trace)
        encoded = json.dumps(payload, sort_keys=True)

        assert payload["version"] == 1
        assert len(payload["leaves"]) == len(analysis.leaves)
        assert len(payload["projections"]) == len(analysis.leaves)
        assert "image" not in encoded
        assert json.loads(encoded)["profile"]["kind"] == analysis.profile.kind
    finally:
        if analysis is not None:
            for leaf in analysis.leaves:
                leaf.image.close()
        image.close()


def _table_region(
    bbox: tuple[int, int, int, int],
    *,
    rows: int = 4,
    cols: int = 3,
) -> LayoutRegion:
    left, top, right, bottom = bbox
    width = right - left
    height = bottom - top
    x_lines = tuple(round(width * index / cols) for index in range(cols + 1))
    y_lines = tuple(round(height * index / rows) for index in range(rows + 1))
    return LayoutRegion(
        kind="table",
        image=Image.new("RGB", (width, height), "white"),
        bbox=bbox,
        table=TableLayout(
            bbox=(0, 0, width, height),
            rows=rows,
            cols=cols,
            x_lines=x_lines,
            y_lines=y_lines,
            cells=tuple(
                TableCell(
                    row=row,
                    col=col,
                    bbox=(
                        x_lines[col],
                        y_lines[row],
                        x_lines[col + 1],
                        y_lines[row + 1],
                    ),
                )
                for row in range(rows)
                for col in range(cols)
            ),
        ),
        metadata={"layout_kind": "recursive_grid_table"},
    )


def test_adjacent_table_merge_rejects_short_heading_bridge():
    page = Image.new("RGB", (1200, 900), "white")
    first = _table_region((50, 100, 1050, 300))
    second = _table_region((50, 360, 1050, 560))
    bridge = LayoutRegion(
        kind="image",
        image=Image.new("RGB", (1200, 40), "white"),
        bbox=(0, 310, 1200, 350),
        metadata={
            "layout_kind": "recursive_grid_cell",
            "content_bbox": (50, 320, 250, 345),
        },
    )

    try:
        assert (
            _merge_table_pair_through_bridge(
                first,
                bridge,
                second,
                page,
            )
            is None
        )
    finally:
        for image in (page, first.image, bridge.image, second.image):
            image.close()


def test_adjacent_table_merge_keeps_wide_row_bridge():
    page = Image.new("RGB", (1200, 900), "white")
    first = _table_region((50, 100, 1050, 300))
    second = _table_region((50, 360, 1050, 560))
    bridge = LayoutRegion(
        kind="image",
        image=Image.new("RGB", (1200, 40), "white"),
        bbox=(0, 310, 1200, 350),
        metadata={
            "layout_kind": "recursive_grid_cell",
            "content_bbox": (50, 320, 950, 345),
        },
    )

    merged = None
    try:
        merged = _merge_table_pair_through_bridge(
            first,
            bridge,
            second,
            page,
        )
        assert merged is not None
        assert merged.table is not None
        assert (merged.table.rows, merged.table.cols) == (9, 3)
    finally:
        if merged is not None:
            merged.image.close()
        for image in (page, first.image, bridge.image, second.image):
            image.close()


def test_unreliable_partition_table_requires_vertical_support():
    weak = _table_region((0, 0, 900, 600), rows=20, cols=9)
    try:
        assert _is_unreliable_partition_table(weak)
    finally:
        weak.image.close()


def test_drawn_partition_table_is_reliable():
    strong = _table_region((0, 0, 900, 600), rows=20, cols=9)
    draw = ImageDraw.Draw(strong.image)
    assert strong.table is not None
    for x in strong.table.x_lines:
        draw.line((x, 0, x, strong.image.height), fill="black", width=2)
    for y in strong.table.y_lines:
        draw.line((0, y, strong.image.width, y), fill="black", width=2)
    try:
        assert not _is_unreliable_partition_table(strong)
    finally:
        strong.image.close()


def test_long_document_page_is_not_simple_two_column_table():
    fixture = Path(__file__).resolve().parents[3] / "debug" / "fixtures" / "doc_docs_ru_architecture.png"
    if not fixture.exists():
        pytest.skip("architecture document fixture is not available")
    image = Image.open(fixture).convert("RGB")
    try:
        assert _simple_track_table_layout(image) is None
    finally:
        image.close()


def test_recursive_table_uses_its_locally_prepared_image(monkeypatch):
    image = Image.new("RGB", (600, 400), "white")
    draw = ImageDraw.Draw(image)
    for x in (0, 200, 400, 599):
        draw.line((x, 0, x, 399), fill="black", width=3)
    for y in (0, 100, 200, 300, 399):
        draw.line((0, y, 599, y), fill="black", width=3)

    def fake_prepare(region_image, _config):
        prepared = region_image.copy()
        ImageDraw.Draw(prepared).rectangle(
            (20, 20, 35, 35),
            fill=(255, 0, 0),
        )
        return (
            prepared,
            RegionDecision(
                depth=0,
                preprocess_steps=("test_local_filter",),
                mask_mode="local_dark",
                contrast_delta=18,
                deskew_angle=0.0,
            ),
        )

    monkeypatch.setattr(
        "app.layout.stages.prepare_recursive_region",
        fake_prepare,
    )
    prepared_layout = TableLayout(
        bbox=(0, 0, 596, 396),
        rows=4,
        cols=3,
        x_lines=(0, 199, 397, 596),
        y_lines=(0, 99, 198, 297, 396),
        cells=tuple(
            TableCell(
                row=row,
                col=col,
                bbox=(
                    (0, 199, 397)[col],
                    (0, 99, 198, 297)[row],
                    (199, 397, 596)[col],
                    (99, 198, 297, 396)[row],
                ),
            )
            for row in range(4)
            for col in range(3)
        ),
    )
    monkeypatch.setattr(
        "app.layout.stages._table_layout_after_recursive_preparation",
        lambda _image: prepared_layout,
    )
    decision = LayoutDecision(
        label="fixed",
        stages=(
            LayoutStageSpec(
                name="recursive_grid",
                parameters=(),
            ),
        ),
        confidence=1.0,
    )
    regions = execute_layout_decision(
        image,
        ProjectionGeometryExtractor().extract(image),
        decision,
        min_confirmed_cell_ratio=0,
    )
    try:
        table = next(region for region in regions if region.kind == "table")
        recursion = (table.metadata or {}).get("region_recursion")
        assert (255, 0, 0) in _colors(table.image)
        assert recursion
        assert recursion[0]["preprocess_steps"] == ("test_local_filter",)
    finally:
        for region in regions:
            if region.image is not image:
                region.image.close()
        image.close()


def test_recursive_grid_groups_paragraph_lines_and_list_markers():
    specs = (
        ((0, 20, 800, 70), (90, 25, 700, 65), (90,)),
        ((0, 70, 800, 120), (90, 75, 680, 115), (90,)),
        ((0, 155, 800, 205), (90, 160, 730, 200), (90,)),
        ((0, 205, 800, 255), (90, 210, 720, 250), (90,)),
        ((0, 255, 800, 305), (90, 260, 640, 300), (90,)),
        ((0, 340, 800, 390), (80, 345, 720, 385), (80, 140)),
        ((0, 390, 800, 440), (140, 395, 690, 435), (140,)),
        ((0, 475, 800, 525), (80, 480, 650, 520), (80, 140)),
    )
    leaves = [
        RecursiveGridLeaf(
            source_bbox=bbox,
            image=Image.new("RGB", (bbox[2] - bbox[0], bbox[3] - bbox[1]), "white"),
            content_bbox=content_bbox,
            left_tracks=tracks,
            dash_track=None,
            merge_left_tracks=(),
            decisions=(),
        )
        for bbox, content_bbox, tracks in specs
    ]
    try:
        groups = group_recursive_leaves(leaves)
        assert [len(group.leaves) for group in groups] == [2, 3, 2, 1]
        assert [group.bbox for group in groups] == [
            (0, 20, 800, 120),
            (0, 155, 800, 305),
            (0, 340, 800, 440),
            (0, 475, 800, 525),
        ]
    finally:
        for leaf in leaves:
            leaf.image.close()


def test_recursive_grid_ignores_bright_annotation_overlay_for_row_splits():
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
    except OSError:
        font = ImageFont.load_default()

    def row_image(*, annotated: bool) -> Image.Image:
        image = Image.new("RGB", (900, 150), "white")
        draw = ImageDraw.Draw(image)
        if annotated:
            for box, color in (
                ((0, 0, 900, 40), (238, 214, 252)),
                ((0, 49, 900, 92), (254, 227, 166)),
                ((0, 102, 900, 144), (205, 208, 255)),
            ):
                draw.rectangle(box, fill=color)
            for y in (43, 96):
                draw.rectangle((0, y, 900, y + 2), fill=(61, 207, 67))
        for y, text in (
            (10, "resolve_pipeline_profile в ocr/app/pipeline_config.py:"),
            (58, "1. Если pipeline_profile не задан - взять default для engine_type"),
            (111, "backend_tesseract_standard / backend_easyocr_standard"),
        ):
            draw.text((30, y), text, fill=(26, 26, 26), font=font)
        return image

    config = RecursiveGridConfig(
        max_depth=12,
        min_cell_height=16,
        min_separator_gap=4,
        overlap=4,
        deskew=False,
        preprocess_steps=(),
    )
    plain_leaves = segment_recursive_grid(row_image(annotated=False), config)
    annotated_leaves = segment_recursive_grid(row_image(annotated=True), config)

    try:
        assert [leaf.source_bbox for leaf in annotated_leaves] == [
            leaf.source_bbox for leaf in plain_leaves
        ]
        assert len(annotated_leaves) == 3
    finally:
        for leaf in (*plain_leaves, *annotated_leaves):
            leaf.image.close()


def test_edge_noise_leaf_remains_structural_without_ocr_text():
    edge = RecursiveGridLeaf(
        source_bbox=(0, 0, 2000, 32),
        image=Image.new("RGB", (2000, 32), "white"),
        content_bbox=(0, 0, 2000, 12),
        left_tracks=(0, 800, 1600),
        dash_track=None,
        merge_left_tracks=(800, 1600),
        decisions=(),
    )
    content = RecursiveGridLeaf(
        source_bbox=(0, 220, 2000, 300),
        image=Image.new("RGB", (2000, 80), "white"),
        content_bbox=(90, 235, 1870, 295),
        left_tracks=(90,),
        dash_track=None,
        merge_left_tracks=(),
        decisions=(),
    )
    try:
        assert _is_structural_only_leaf(edge, (2000, 1200))
        assert not _is_structural_only_leaf(
            content,
            (2000, 1200),
        )
    finally:
        edge.image.close()
        content.image.close()


def test_sparse_shadow_separates_subtable_boundary_from_dash_marker():
    specs = (
        (
            (0, 0, 600, 50),
            (30, 5, 500, 45),
            (30, 400),
            None,
            (400,),
        ),
        (
            (0, 90, 600, 140),
            (30, 95, 550, 135),
            (30, 80),
            30,
            (),
        ),
        (
            (0, 140, 600, 190),
            (80, 145, 550, 185),
            (80,),
            None,
            (),
        ),
    )
    leaves = [
        RecursiveGridLeaf(
            source_bbox=bbox,
            image=Image.new(
                "RGB",
                (bbox[2] - bbox[0], bbox[3] - bbox[1]),
                "white",
            ),
            content_bbox=content_bbox,
            left_tracks=tracks,
            dash_track=dash_track,
            merge_left_tracks=merge_left_tracks,
            decisions=(),
        )
        for (
            bbox,
            content_bbox,
            tracks,
            dash_track,
            merge_left_tracks,
        ) in specs
    ]
    try:
        shadow = project_sparse_shadow(leaves)
        projections = {id(leaf): (anchor, codes) for leaf, anchor, codes in shadow.leaf_projection}

        title_anchor, title_codes = projections[id(leaves[0])]
        bullet_anchor, bullet_codes = projections[id(leaves[1])]
        continuation_anchor, continuation_codes = projections[id(leaves[2])]
        assert title_anchor == (0, 0)
        assert title_codes == ((0, 2, 16),)
        assert bullet_anchor == (2, 1)
        assert {code for _, _, code in shadow.codes}.issubset(SPARSE_SHADOW_CODES)
        assert bullet_codes == ()
        assert continuation_anchor == (3, 1)
        assert continuation_codes == ((3, 1, 3),)
    finally:
        for leaf in leaves:
            leaf.image.close()


def test_left_tracks_marks_compact_bullet_before_merged_text():
    mask = pytest.importorskip("numpy").zeros(
        (40, 300),
        dtype=bool,
    )
    marker = (
        (2, 5),
        (1, 6),
        (0, 7),
        (0, 7),
        (0, 7),
        (0, 7),
        (1, 6),
    )
    for row, (start, end) in enumerate(marker, start=16):
        mask[row, 68 + start : 68 + end] = True
    mask[8:32, 87:250] = True

    _, tracks, dash_track, merge_left_tracks = _left_tracks(
        mask,
        min_gap=8,
    )

    assert tracks == (60, 87)
    assert dash_track == 60
    assert merge_left_tracks == ()


def test_left_tracks_marks_plus_list_marker():
    mask = pytest.importorskip("numpy").zeros(
        (67, 2024),
        dtype=bool,
    )
    mask[21:24, 179:199] = True
    mask[12:32, 188:191] = True
    mask[8:40, 330:900] = True

    _, tracks, dash_track, merge_left_tracks = _left_tracks(
        mask,
        min_gap=8,
    )

    assert tracks == (171, 330)
    assert dash_track == 171
    assert merge_left_tracks == ()


@pytest.mark.parametrize(
    ("marker_top", "marker_bottom", "marker_left", "marker_right"),
    (
        (10, 24, 70, 80),
        (15, 18, 72, 75),
        (15, 17, 72, 76),
    ),
)
def test_left_tracks_rejects_dense_or_tiny_marker(
    marker_top,
    marker_bottom,
    marker_left,
    marker_right,
):
    mask = pytest.importorskip("numpy").zeros(
        (44, 420),
        dtype=bool,
    )
    mask[
        marker_top:marker_bottom,
        marker_left:marker_right,
    ] = True
    mask[8:36, 110:380] = True

    _, tracks, dash_track, _ = _left_tracks(mask, min_gap=8)

    assert len(tracks) == 1
    assert dash_track is None


def test_left_tracks_rejects_short_word_as_dash():
    mask = pytest.importorskip("numpy").zeros(
        (44, 420),
        dtype=bool,
    )
    mask[15:29, 60:94] = True
    mask[10:34, 110:380] = True

    _, tracks, dash_track, _ = _left_tracks(mask, min_gap=8)

    assert len(tracks) == 1
    assert dash_track is None


def test_horizontal_separator_accepts_clipped_overlap_child():
    mask = pytest.importorskip("numpy").zeros(
        (99, 400),
        dtype=bool,
    )
    mask[26:64, 40:360] = True
    mask[78:99, 40:360] = True

    assert _horizontal_separator(
        mask,
        min_cell_height=24,
        min_separator_gap=8,
    ) == (71, 0.0)


def test_deduplicate_leaves_preserves_vertical_siblings_and_full_crop():
    side_by_side = [
        RecursiveGridLeaf(
            source_bbox=(0, 100, 300, 160),
            image=Image.new("RGB", (300, 60), "white"),
            content_bbox=(20, 110, 260, 150),
            left_tracks=(20,),
            dash_track=None,
            merge_left_tracks=(),
            decisions=(),
        ),
        RecursiveGridLeaf(
            source_bbox=(400, 100, 700, 160),
            image=Image.new("RGB", (300, 60), "white"),
            content_bbox=(420, 110, 680, 150),
            left_tracks=(420,),
            dash_track=None,
            merge_left_tracks=(),
            decisions=(),
        ),
    ]
    preserved = _deduplicate_leaves(side_by_side)
    try:
        assert len(preserved) == 2
    finally:
        for leaf in preserved:
            leaf.image.close()

    partial = RecursiveGridLeaf(
        source_bbox=(0, 200, 500, 235),
        image=Image.new("RGB", (500, 35), "white"),
        content_bbox=(40, 214, 420, 235),
        left_tracks=(40,),
        dash_track=None,
        merge_left_tracks=(),
        decisions=(),
    )
    complete = RecursiveGridLeaf(
        source_bbox=(0, 216, 500, 267),
        image=Image.new("RGB", (500, 51), "white"),
        content_bbox=(40, 216, 420, 255),
        left_tracks=(40,),
        dash_track=None,
        merge_left_tracks=(),
        decisions=(),
    )
    deduplicated = _deduplicate_leaves([partial, complete])
    try:
        assert deduplicated == [complete]
    finally:
        for leaf in deduplicated:
            leaf.image.close()


def test_dominant_rules_use_local_active_bounds():
    mask = pytest.importorskip("numpy").zeros(
        (160, 500),
        dtype=bool,
    )
    mask[20:100, 60] = True
    mask[20:100, 360] = True
    mask[20, 60:361] = True
    mask[60, 60:361] = True
    mask[99, 60:361] = True
    mask[34:48, 100:220] = True
    mask[72:86, 100:260] = True

    cleaned = _remove_dominant_rules(mask)

    assert not cleaned[20, 200]
    assert not cleaned[60, 200]
    assert not cleaned[80, 60]
    assert cleaned[40, 150]
    assert cleaned[78, 150]


def test_dominant_vertical_rules_become_sparse_tracks():
    mask = pytest.importorskip("numpy").zeros(
        (160, 500),
        dtype=bool,
    )
    mask[20:140, 40] = True
    mask[20:140, 180] = True
    mask[20:140, 360] = True
    mask[20:140, 460] = True
    for row in (35, 55, 75, 95, 115, 130):
        mask[row, 40:461] = True
    mask[40:55, 60:150] = True
    mask[90:105, 200:330] = True

    assert _dominant_vertical_rule_tracks(
        mask,
        min_gap=8,
    ) == (180, 360)


def test_vertical_lane_separator_requires_concurrent_content():
    np = pytest.importorskip("numpy")
    concurrent = np.zeros((220, 800), dtype=bool)
    concurrent[30:80, 40:300] = True
    concurrent[110:180, 40:300] = True
    concurrent[50:100, 520:760] = True
    concurrent[130:200, 520:760] = True

    separator = _vertical_lane_separator(
        concurrent,
        min_cell_height=24,
        min_gap=8,
    )

    assert separator is not None
    cut, gutter_width = separator
    assert 300 < cut < 520
    assert gutter_width == 220

    stacked = np.zeros((220, 800), dtype=bool)
    stacked[20:80, 40:300] = True
    stacked[130:200, 520:760] = True
    assert (
        _vertical_lane_separator(
            stacked,
            min_cell_height=24,
            min_gap=8,
        )
        is None
    )

    title_and_logo = np.zeros((220, 800), dtype=bool)
    title_and_logo[30:70, 40:300] = True
    title_and_logo[90:130, 40:300] = True
    title_and_logo[45:105, 520:760] = True
    assert (
        _vertical_lane_separator(
            title_and_logo,
            min_cell_height=24,
            min_gap=8,
        )
        is None
    )


def test_sparse_shadow_compresses_varying_paragraph_indents():
    leaves = [
        RecursiveGridLeaf(
            source_bbox=(0, top, 600, top + 50),
            image=Image.new("RGB", (600, 50), "white"),
            content_bbox=(left, top + 5, 550, top + 45),
            left_tracks=(left,),
            dash_track=None,
            merge_left_tracks=(),
            decisions=(),
        )
        for top, left in ((0, 120), (50, 40), (100, 80))
    ]
    try:
        shadow = project_sparse_shadow(leaves)

        assert shadow.x_tracks == (40,)
        assert [anchor for _, anchor, _ in shadow.leaf_projection] == [(0, 0), (1, 0), (2, 0)]
        assert shadow.codes == frozenset(
            {
                (1, 0, 3),
                (2, 0, 3),
            }
        )
    finally:
        for leaf in leaves:
            leaf.image.close()


def test_sparse_shadow_signature_does_not_retain_leaf_images():
    leaf = RecursiveGridLeaf(
        source_bbox=(0, 0, 100, 40),
        image=Image.new("RGB", (100, 40), "white"),
        content_bbox=(10, 5, 90, 35),
        left_tracks=(10,),
        dash_track=None,
        merge_left_tracks=(),
        decisions=(),
    )
    projection = SparseShadowProjection(
        codes=frozenset({(1, 0, 3)}),
        leaf_projection=((leaf, (1, 0), ((1, 0, 3),)),),
        x_tracks=(10,),
        rows=2,
        cols=1,
    )

    signature = sparse_shadow_signature(projection)

    assert signature.rows == 2
    assert signature.anchors == ((1, 0),)
    assert all(not isinstance(value, Image.Image) for value in signature.__dict__.values())
    leaf.image.close()


@pytest.mark.parametrize(
    ("signature", "kind"),
    (
        (
            SparseShadowSignature(
                rows=3,
                cols=1,
                anchors=((0, 0), (1, 0), (2, 0)),
                codes=((1, 0, 3), (2, 0, 3)),
                x_tracks=(10,),
            ),
            "text",
        ),
        (
            SparseShadowSignature(
                rows=2,
                cols=2,
                anchors=((0, 1), (1, 1)),
                codes=((1, 1, 3),),
                x_tracks=(10, 30),
            ),
            "list",
        ),
        (
            SparseShadowSignature(
                rows=2,
                cols=3,
                anchors=((0, 0), (1, 0)),
                codes=((0, 1, 5), (1, 1, 8)),
                x_tracks=(10, 30, 50),
            ),
            "table",
        ),
    ),
)
def test_sparse_shadow_profile_uses_compact_signature(
    signature,
    kind,
):
    assert classify_sparse_shadow(signature).kind == kind


def test_gradient_table_layout_supports_many_thin_columns():
    image = Image.new("RGB", (1200, 900), "white")
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = 80, 100, 1120, 800
    x_lines = [round(left + (right - left) * index / 10) for index in range(11)]
    y_lines = [round(top + (bottom - top) * index / 14) for index in range(15)]
    for x in x_lines:
        draw.line((x, top, x, bottom), fill=(90, 90, 90), width=2)
    for y in y_lines:
        draw.line((left, y, right, y), fill=(90, 90, 90), width=2)
    try:
        table = _gradient_table_layout(image)
    finally:
        image.close()

    assert table is not None
    assert (table.rows, table.cols) == (14, 10)
    assert len(table.cells) == 140


def test_recursive_table_prefers_rules_over_text_row_oversegmentation():
    def layout(
        bbox: tuple[int, int, int, int],
        *,
        rows: int,
        cols: int,
    ) -> TableLayout:
        left, top, right, bottom = bbox
        x_lines = tuple(round(left + (right - left) * index / cols) for index in range(cols + 1))
        y_lines = tuple(round(top + (bottom - top) * index / rows) for index in range(rows + 1))
        return TableLayout(
            bbox=bbox,
            rows=rows,
            cols=cols,
            x_lines=x_lines,
            y_lines=y_lines,
            cells=tuple(
                TableCell(
                    row=row,
                    col=col,
                    bbox=(
                        x_lines[col],
                        y_lines[row],
                        x_lines[col + 1],
                        y_lines[row + 1],
                    ),
                )
                for row in range(rows)
                for col in range(cols)
            ),
        )

    simple = layout((0, 0, 1200, 900), rows=31, cols=10)
    gradient = layout((80, 100, 1120, 800), rows=14, cols=10)

    assert (
        _prefer_recursive_table_layout(
            simple,
            gradient,
            (1200, 900),
        )
        is gradient
    )


def test_recursive_table_does_not_promote_single_column_internal_grid():
    single_column = TableLayout(
        bbox=(0, 0, 600, 900),
        rows=10,
        cols=1,
        x_lines=(0, 600),
        y_lines=tuple(range(0, 901, 90)),
        cells=tuple(
            TableCell(
                row=row,
                col=0,
                bbox=(0, row * 90, 600, (row + 1) * 90),
            )
            for row in range(10)
        ),
    )
    shorter_candidate = TableLayout(
        bbox=(0, 0, 600, 900),
        rows=5,
        cols=1,
        x_lines=(0, 600),
        y_lines=tuple(range(0, 901, 180)),
        cells=tuple(
            TableCell(
                row=row,
                col=0,
                bbox=(0, row * 180, 600, (row + 1) * 180),
            )
            for row in range(5)
        ),
    )

    assert (
        _prefer_recursive_table_layout(
            single_column,
            shorter_candidate,
            (600, 900),
        )
        is single_column
    )


def test_full_page_card_grid_with_late_columns_returns_to_recursion():
    image = Image.new("RGB", (1200, 700), "white")
    draw = ImageDraw.Draw(image)
    for x in (200, 400, 600, 800, 1000):
        draw.line((x, 180, x, 699), fill="black", width=2)
    table = TableLayout(
        bbox=(0, 0, 1200, 700),
        rows=15,
        cols=6,
        x_lines=(0, 200, 400, 600, 800, 1000, 1200),
        y_lines=tuple(round(700 * index / 15) for index in range(16)),
        cells=(),
    )
    try:
        assert _full_page_table_lacks_early_vertical_support(
            image,
            table,
        )
    finally:
        image.close()


def test_full_page_table_with_early_rules_keeps_table_path():
    image = Image.new("RGB", (1200, 700), "white")
    draw = ImageDraw.Draw(image)
    for x in (200, 400, 600, 800, 1000):
        draw.line((x, 0, x, 699), fill="black", width=2)
    table = TableLayout(
        bbox=(0, 0, 1200, 700),
        rows=15,
        cols=6,
        x_lines=(0, 200, 400, 600, 800, 1000, 1200),
        y_lines=tuple(round(700 * index / 15) for index in range(16)),
        cells=(),
    )
    try:
        assert not _full_page_table_lacks_early_vertical_support(
            image,
            table,
        )
    finally:
        image.close()


def test_narrow_middle_gutter_table_is_card_grid_not_table():
    card_grid = TableLayout(
        bbox=(0, 0, 1127, 657),
        rows=3,
        cols=3,
        x_lines=(0, 542, 585, 1127),
        y_lines=(0, 100, 556, 657),
        cells=(),
    )
    balanced_table = TableLayout(
        bbox=(0, 0, 900, 600),
        rows=4,
        cols=3,
        x_lines=(0, 300, 600, 900),
        y_lines=(0, 150, 300, 450, 600),
        cells=(),
    )

    assert _is_gutter_card_table(card_grid)
    assert not _is_gutter_card_table(balanced_table)


def test_recursive_simple_table_requires_rules_in_both_quarters():
    image = Image.new("RGB", (600, 400), "white")
    draw = ImageDraw.Draw(image)
    x_lines = (0, 100, 200, 300, 400, 500, 600)
    for x in x_lines[1:-1]:
        draw.line((x, 0, x, 399), fill="black", width=2)
    table = TableLayout(
        bbox=(0, 0, 600, 400),
        rows=4,
        cols=6,
        x_lines=x_lines,
        y_lines=(0, 100, 200, 300, 400),
        cells=(),
    )
    try:
        assert _simple_group_table_has_spanning_rules(image, table)
    finally:
        image.close()


def test_recursive_simple_table_rejects_middle_only_tracks():
    image = Image.new("RGB", (600, 400), "white")
    draw = ImageDraw.Draw(image)
    x_lines = (0, 100, 200, 300, 400, 500, 600)
    for x in x_lines[1:-1]:
        draw.line((x, 120, x, 280), fill="black", width=2)
    table = TableLayout(
        bbox=(0, 0, 600, 400),
        rows=4,
        cols=6,
        x_lines=x_lines,
        y_lines=(0, 100, 200, 300, 400),
        cells=(),
    )
    try:
        assert not _simple_group_table_has_spanning_rules(
            image,
            table,
        )
    finally:
        image.close()


def test_gradient_table_layout_refines_partial_lower_rules():
    image = Image.new("RGB", (1200, 900), "white")
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = 80, 100, 1120, 800
    x_lines = [round(left + (right - left) * index / 10) for index in range(11)]
    y_lines = [round(top + (bottom - top) * index / 14) for index in range(15)]
    for x in x_lines:
        draw.line((x, top, x, bottom), fill=(90, 90, 90), width=2)
    for index, y in enumerate(y_lines):
        line_right = right if index <= 8 else 560
        draw.line((left, y, line_right, y), fill=(90, 90, 90), width=2)
    try:
        table = _gradient_table_layout(image)
    finally:
        image.close()

    assert table is not None
    assert abs(table.y_lines[-1] - bottom) <= 2
    assert table.rows == 14


def test_gradient_table_layout_rejects_text_like_bands():
    image = Image.new("RGB", (1200, 900), "white")
    draw = ImageDraw.Draw(image)
    for top in (100, 260, 420, 580):
        draw.rectangle((100, top, 1000, top + 45), fill="black")
    try:
        table = _gradient_table_layout(image)
    finally:
        image.close()

    assert table is None


def test_horizontal_projection_ignores_tall_edge_binding():
    mask = pytest.importorskip("numpy").zeros(
        (240, 400),
        dtype=bool,
    )
    mask[:, 4:16] = True
    mask[35:85, 80:330] = True
    mask[155:205, 80:330] = True

    assert (
        _horizontal_separator(
            mask,
            min_cell_height=24,
            min_separator_gap=8,
        )
        is None
    )

    projected = _horizontal_projection_mask(mask, min_gap=8)
    separator = _horizontal_separator(
        projected,
        min_cell_height=24,
        min_separator_gap=8,
    )

    assert separator is not None
    assert 85 <= separator[0] <= 155
    assert not projected[:, :16].any()
    assert projected[:, 80:330].any()


def test_horizontal_projection_keeps_short_edge_logo():
    mask = pytest.importorskip("numpy").zeros(
        (240, 400),
        dtype=bool,
    )
    mask[35:85, 80:280] = True
    mask[35:75, 365:392] = True
    mask[155:205, 80:280] = True

    projected = _horizontal_projection_mask(mask, min_gap=8)

    assert projected[:, 365:392].any()
    assert (
        _horizontal_separator(
            projected,
            min_cell_height=24,
            min_separator_gap=8,
        )
        is not None
    )
    _, tracks, _, merge_left_tracks = _left_tracks(
        projected,
        min_gap=8,
    )
    assert tracks == (72, 365)
    assert merge_left_tracks == (365,)


def test_left_tracks_rejects_stacked_text_as_parallel_columns():
    mask = pytest.importorskip("numpy").zeros(
        (72, 420),
        dtype=bool,
    )
    mask[8:28, 40:220] = True
    mask[38:58, 300:392] = True

    _, tracks, _, merge_left_tracks = _left_tracks(
        mask,
        min_gap=8,
    )

    assert tracks == (32,)
    assert merge_left_tracks == ()


def test_left_tracks_rejects_sparse_decorative_edge_as_column():
    mask = pytest.importorskip("numpy").zeros(
        (72, 420),
        dtype=bool,
    )
    mask[16:52, 80:300] = True
    mask[24:26, 380:412] = True

    _, tracks, _, merge_left_tracks = _left_tracks(
        mask,
        min_gap=8,
    )

    assert tracks == (72,)
    assert merge_left_tracks == ()


def test_horizontal_projection_removes_binding_from_sparse_tracks():
    mask = pytest.importorskip("numpy").zeros(
        (240, 400),
        dtype=bool,
    )
    mask[:, 4:16] = True
    mask[35:205, 80:330] = True

    projected = _horizontal_projection_mask(mask, min_gap=8)
    _, tracks, _, merge_left_tracks = _left_tracks(
        projected,
        min_gap=8,
    )

    assert tracks == (72,)
    assert merge_left_tracks == ()


def test_horizontal_projection_inherits_edge_clutter_after_split():
    np = pytest.importorskip("numpy")
    parent = np.zeros((240, 400), dtype=bool)
    parent[30:210, 80:330] = True
    for top in range(10, 220, 35):
        parent[top : top + 12, 380:396] = True

    _, insets = _horizontal_projection(
        parent,
        min_gap=8,
        inherited_insets=(0.0, 0.0),
    )
    child = np.zeros((60, 400), dtype=bool)
    child[10:45, 80:330] = True
    child[15:27, 380:396] = True
    projected, inherited = _horizontal_projection(
        child,
        min_gap=8,
        inherited_insets=insets,
    )

    assert insets[1] > 0
    assert inherited == insets
    assert projected[:, 80:330].any()
    assert not projected[:, 380:396].any()


def test_horizontal_projection_discards_edge_only_decoration():
    np = pytest.importorskip("numpy")
    mask = np.zeros((300, 400), dtype=bool)
    for top in range(10, 280, 35):
        mask[top : top + 12, 380:396] = True

    projected = _horizontal_projection_mask(mask, min_gap=8)

    assert not projected.any()


def test_horizontal_projection_does_not_redetect_edge_on_line_leaf():
    np = pytest.importorskip("numpy")
    mask = np.zeros((60, 400), dtype=bool)
    mask[8:52, 15:180] = True

    projected, insets = _horizontal_projection(
        mask,
        min_gap=8,
        inherited_insets=(0.0, 0.0),
    )

    assert projected is mask
    assert insets == (0.0, 0.0)


def test_left_track_uses_first_foreground_not_dense_letter_core():
    np = pytest.importorskip("numpy")
    mask = np.zeros((60, 400), dtype=bool)
    mask[20:22, 80:92] = True
    mask[10:48, 150:330] = True

    content_bbox, tracks, _, _ = _left_tracks(mask, min_gap=8)

    assert content_bbox[0] == 72
    assert tracks[0] == 72


def test_left_track_ignores_isolated_page_edge_stroke():
    np = pytest.importorskip("numpy")
    mask = np.zeros((60, 400), dtype=bool)
    mask[8:52, 0:3] = True
    mask[10:48, 90:330] = True

    content_bbox, tracks, _, _ = _left_tracks(mask, min_gap=8)

    assert content_bbox[0] == 82
    assert tracks[0] == 82


def test_vertical_cuts_follow_neighbor_whitespace_and_line_pairs():
    features = LayoutFeatures(
        width=1280,
        height=666,
        foreground_ratio=0.16,
        separators=(
            SeparatorCandidate("x", 96, 233, 320, 666, "whitespace", 0.95),
            SeparatorCandidate("x", 201, 227, 0, 640, "whitespace", 0.46),
            SeparatorCandidate("x", 201, 228, 0, 666, "whitespace", 0.47),
            SeparatorCandidate("x", 233, 235, 0, 666, "ink", 0.26),
            SeparatorCandidate("x", 425, 427, 0, 666, "ink", 0.26),
            SeparatorCandidate("x", 427, 443, 0, 640, "whitespace", 0.41),
            SeparatorCandidate("x", 427, 443, 320, 666, "whitespace", 0.99),
            SeparatorCandidate("x", 443, 445, 0, 666, "ink", 0.22),
            SeparatorCandidate("x", 634, 636, 0, 666, "ink", 0.22),
            SeparatorCandidate("x", 652, 654, 0, 666, "ink", 0.30),
            SeparatorCandidate("x", 673, 675, 0, 666, "ink", 0.22),
            SeparatorCandidate("x", 821, 823, 0, 666, "ink", 0.22),
            SeparatorCandidate("x", 843, 845, 0, 666, "ink", 0.30),
            SeparatorCandidate("x", 861, 863, 0, 666, "ink", 0.26),
            SeparatorCandidate("x", 1052, 1054, 0, 666, "ink", 0.26),
            SeparatorCandidate("x", 1054, 1070, 0, 640, "whitespace", 0.58),
            SeparatorCandidate("x", 1054, 1070, 320, 666, "whitespace", 1.0),
            SeparatorCandidate("x", 1070, 1072, 0, 666, "ink", 0.28),
            SeparatorCandidate("x", 1240, 1241, 0, 666, "ink", 0.18),
            SeparatorCandidate("x", 1261, 1263, 0, 666, "ink", 0.28),
        ),
    )

    assert _vertical_cuts_for_band(
        features,
        top=0,
        bottom=666,
        min_cell_width=102,
        min_coverage=0.55,
    ) == [214, 435, 644, 853, 1062]


def test_line_pair_cut_survives_wide_merged_component():
    features = LayoutFeatures(
        width=1000,
        height=500,
        foreground_ratio=0.1,
        separators=(
            SeparatorCandidate("x", 493, 495, 0, 500, "ink", 0.9),
            SeparatorCandidate("x", 505, 507, 0, 500, "ink", 0.9),
        ),
        components=(
            # Geometry-only wide content, like a merged header cell. It must not
            # erase a structural line-pair cut that creates placeholder cells.
            ComponentFeature((200, 40, 800, 460), 250_000, 0.3),
        ),
    )

    assert _vertical_cuts_for_band(
        features,
        top=0,
        bottom=500,
        min_cell_width=100,
        min_coverage=0.55,
    ) == [500]


def test_whitespace_cut_crossing_wide_component_is_rejected():
    features = LayoutFeatures(
        width=1000,
        height=500,
        foreground_ratio=0.1,
        separators=(SeparatorCandidate("x", 490, 510, 0, 500, "whitespace", 1.0),),
        components=(ComponentFeature((200, 40, 800, 460), 250_000, 0.3),),
    )

    assert (
        _vertical_cuts_for_band(
            features,
            top=0,
            bottom=500,
            min_cell_width=100,
            min_coverage=0.55,
        )
        == []
    )


def test_low_density_grid_component_does_not_block_whitespace_cut():
    features = LayoutFeatures(
        width=1070,
        height=825,
        foreground_ratio=0.14,
        separators=(
            SeparatorCandidate("x", 442, 487, 0, 825, "whitespace", 0.9),
            SeparatorCandidate("y", 120, 123, 0, 1070, "ink", 0.8),
            SeparatorCandidate("y", 240, 243, 0, 1070, "ink", 0.8),
            SeparatorCandidate("y", 360, 363, 0, 1070, "ink", 0.8),
        ),
        components=(
            # A connected table/grid row can span the whole page while still
            # being mostly empty. It is structure, not text crossing a gutter.
            ComponentFeature((23, 36, 1070, 543), 60_000, 0.014),
            ComponentFeature((23, 726, 1070, 820), 48_000, 0.048),
        ),
    )

    assert _vertical_cuts_for_band(
        features,
        top=0,
        bottom=825,
        min_cell_width=85,
        min_coverage=0.55,
    ) == [464]


def test_single_text_row_whitespace_gutters_stay_one_paragraph_row():
    features = LayoutFeatures(
        width=1072,
        height=77,
        foreground_ratio=0.1,
        separators=(
            SeparatorCandidate("x", 180, 210, 0, 77, "whitespace", 1.0),
            SeparatorCandidate("x", 480, 510, 0, 77, "whitespace", 1.0),
            SeparatorCandidate("x", 600, 630, 0, 77, "whitespace", 1.0),
            SeparatorCandidate("x", 178, 180, 0, 77, "ink", 0.7),
            SeparatorCandidate("x", 210, 212, 0, 77, "ink", 0.7),
        ),
    )

    assert (
        _vertical_cuts_for_band(
            features,
            top=0,
            bottom=77,
            min_cell_width=80,
            min_coverage=0.55,
        )
        == []
    )


def test_single_timeline_gutter_stays_one_column_without_table_evidence():
    features = LayoutFeatures(
        width=1562,
        height=926,
        foreground_ratio=0.052,
        separators=(
            SeparatorCandidate("x", 1050, 1080, 0, 926, "whitespace", 0.9),
            SeparatorCandidate("y", 110, 114, 0, 1500, "ink", 0.8),
            SeparatorCandidate("y", 360, 364, 0, 1500, "ink", 0.8),
            SeparatorCandidate("y", 700, 704, 0, 1500, "ink", 0.8),
        ),
    )

    assert (
        _vertical_cuts_for_band(
            features,
            top=0,
            bottom=926,
            min_cell_width=125,
            min_coverage=0.55,
        )
        == []
    )


def test_low_foreground_ink_supported_gutter_stays_one_column():
    features = LayoutFeatures(
        width=1240,
        height=1278,
        foreground_ratio=0.072,
        separators=(
            SeparatorCandidate("x", 585, 590, 0, 1278, "whitespace", 0.9),
            SeparatorCandidate("x", 580, 582, 0, 1278, "ink", 0.8),
        ),
    )

    assert (
        _vertical_cuts_for_band(
            features,
            top=0,
            bottom=1278,
            min_cell_width=100,
            min_coverage=0.55,
        )
        == []
    )


def test_component_rows_are_full_width_paragraph_bands():
    features = LayoutFeatures(
        width=600,
        height=360,
        foreground_ratio=0.12,
        separators=(),
        components=(
            ComponentFeature((20, 30, 360, 80), 8000, 0.35),
            ComponentFeature((420, 36, 560, 72), 1800, 0.30),
            ComponentFeature((20, 140, 330, 190), 7000, 0.34),
            ComponentFeature((430, 146, 560, 182), 1700, 0.30),
            ComponentFeature((20, 250, 350, 300), 7600, 0.35),
            ComponentFeature((430, 256, 560, 292), 1700, 0.30),
        ),
    )
    decision = LayoutDecision(
        label="spatial",
        stages=(
            LayoutStageSpec(
                "spatial_regions",
                (
                    ("max_region_height", 500),
                    ("min_region_height", 80),
                ),
            ),
        ),
        confidence=1,
    )
    image = Image.new("RGB", (600, 360), "white")
    draw = ImageDraw.Draw(image)
    for bbox in (
        (20, 30, 360, 80),
        (420, 36, 560, 72),
        (20, 140, 330, 190),
        (430, 146, 560, 182),
        (20, 250, 350, 300),
        (430, 256, 560, 292),
    ):
        draw.rectangle(bbox, fill="black")
    try:
        regions = execute_layout_decision(
            image,
            features,
            decision,
            min_confirmed_cell_ratio=0.35,
        )
    finally:
        for region in locals().get("regions", []):
            if region.image is not image:
                region.image.close()
        image.close()

    assert [region.bbox for region in regions] == [
        (0, 0, 600, 110),
        (0, 110, 600, 220),
        (0, 220, 600, 360),
    ]


def test_low_foreground_component_rows_are_full_width_paragraph_bands():
    features = LayoutFeatures(
        width=800,
        height=500,
        foreground_ratio=0.052,
        separators=(),
        components=(
            ComponentFeature((40, 40, 320, 65), 4200, 0.42),
            ComponentFeature((40, 135, 470, 165), 6400, 0.39),
            ComponentFeature((40, 245, 520, 275), 7000, 0.36),
            ComponentFeature((40, 355, 390, 385), 5200, 0.38),
        ),
    )
    decision = LayoutDecision(
        label="spatial",
        stages=(
            LayoutStageSpec(
                "spatial_regions",
                (
                    ("max_region_height", 700),
                    ("min_region_height", 160),
                ),
            ),
        ),
        confidence=1,
    )
    image = Image.new("RGB", (800, 500), "white")
    draw = ImageDraw.Draw(image)
    for component in features.components:
        draw.rectangle(component.bbox, fill="black")
    try:
        regions = execute_layout_decision(
            image,
            features,
            decision,
            min_confirmed_cell_ratio=0.35,
        )
    finally:
        for region in locals().get("regions", []):
            if region.image is not image:
                region.image.close()
        image.close()

    assert [region.bbox for region in regions] == [
        (0, 0, 800, 100),
        (0, 100, 800, 205),
        (0, 205, 800, 315),
        (0, 315, 800, 500),
    ]


def test_low_fill_components_do_not_create_paragraph_bands():
    features = LayoutFeatures(
        width=800,
        height=500,
        foreground_ratio=0.052,
        separators=(),
        components=(
            ComponentFeature((0, 30, 800, 85), 500, 0.01),
            ComponentFeature((0, 150, 800, 210), 600, 0.012),
            ComponentFeature((0, 275, 800, 340), 650, 0.013),
            ComponentFeature((0, 405, 800, 470), 650, 0.013),
        ),
    )
    decision = LayoutDecision(
        label="spatial",
        stages=(
            LayoutStageSpec(
                "spatial_regions",
                (
                    ("max_region_height", 700),
                    ("min_region_height", 160),
                ),
            ),
        ),
        confidence=1,
    )
    image = Image.new("RGB", (800, 500), "white")
    try:
        regions = execute_layout_decision(
            image,
            features,
            decision,
            min_confirmed_cell_ratio=0.35,
        )
    finally:
        for region in locals().get("regions", []):
            if region.image is not image:
                region.image.close()
        image.close()

    assert [region.bbox for region in regions] == [(0, 0, 800, 500)]


@pytest.mark.parametrize("columns", [1, 2, 3, 6])
def test_spatial_stage_preserves_all_markers_for_arbitrary_column_counts(
    columns,
):
    pytest.importorskip("cv2")
    image, header_marker, markers = _marker_page(columns)
    try:
        features = ProjectionGeometryExtractor().extract(image)
        decision = select_layout_pipeline(
            features,
            selector_name="uniform_spatial_v1",
            allowed_stages=("spatial_regions",),
            default_parameters=(
                ("max_region_height", 700),
                ("min_region_height", 180),
            ),
        )
        regions = execute_layout_decision(
            image,
            features,
            decision,
            min_confirmed_cell_ratio=0.35,
        )
        region_colors = [_colors(region.image) for region in regions]
    finally:
        for region in locals().get("regions", []):
            if region.image is not image:
                region.image.close()
        image.close()

    assert sum(header_marker in colors for colors in region_colors) == 1
    assert all(sum(marker in colors for colors in region_colors) == 1 for marker in markers)
    header_region = next(region for region, colors in zip(regions, region_colors) if header_marker in colors)
    assert header_region.bbox[0] == 0
    assert header_region.bbox[2] == image.width


def test_spatial_stage_keeps_single_large_word_in_one_region():
    pytest.importorskip("cv2")
    image = Image.new("RGB", (3840, 2160), "white")
    draw = ImageDraw.Draw(image)
    draw.text((1500, 1000), "SALE", fill="black", stroke_width=8)
    try:
        features = ProjectionGeometryExtractor().extract(image)
        decision = select_layout_pipeline(
            features,
            selector_name="uniform_spatial_v1",
            allowed_stages=("spatial_regions",),
            default_parameters=(
                ("max_region_height", 3000),
                ("min_region_height", 300),
            ),
        )
        regions = execute_layout_decision(
            image,
            features,
            decision,
            min_confirmed_cell_ratio=0.35,
        )
    finally:
        for region in locals().get("regions", []):
            if region.image is not image:
                region.image.close()
        image.close()

    assert len(regions) == 1


def test_table_partition_preserves_content_on_both_sides_of_grid():
    pytest.importorskip("cv2")
    image = Image.new("RGB", (1000, 500), "white")
    draw = ImageDraw.Draw(image)
    left_marker = (180, 20, 20)
    right_marker = (20, 20, 180)
    draw.rectangle((30, 210, 50, 230), fill=left_marker)
    draw.text((60, 200), "LEFT", fill="black")
    draw.rectangle((950, 210, 970, 230), fill=right_marker)
    draw.text((880, 200), "RIGHT", fill="black")
    for x in (250, 500, 750):
        draw.line((x, 80, x, 420), fill="black", width=4)
    for y in (80, 250, 420):
        draw.line((250, y, 750, y), fill="black", width=4)

    try:
        features = ProjectionGeometryExtractor().extract(image)
        decision = select_layout_pipeline(
            features,
            selector_name="fixed",
            allowed_stages=("table_regions",),
            default_parameters=(),
        )
        regions = execute_layout_decision(
            image,
            features,
            decision,
            min_confirmed_cell_ratio=0,
        )
        colors = [_colors(region.image) for region in regions]
    finally:
        for region in locals().get("regions", []):
            if region.image is not image:
                region.image.close()
        image.close()

    assert any(region.kind == "table" for region in regions)
    assert any(left_marker in value for value in colors)
    assert any(right_marker in value for value in colors)
