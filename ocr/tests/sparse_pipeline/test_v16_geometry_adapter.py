from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.sparse_pipeline.block_planning import (
    BlockPlanningConfig,
    BlockPlanningMode,
    MembershipUnitKind,
    OverlappingBlockPlanner,
)
from app.sparse_pipeline.contracts import (
    AxisInterval,
    Box,
    GeometryStatus,
    Segment,
    SegmentKind,
    SegmentSpan,
    SparseCell,
    SparseCoordinateMode,
    SparseSegmentMatrix,
    SparseStructuralCode,
    StopReason,
)
from app.sparse_pipeline.geometry import GeometryConfig, GeometryLimitError
from app.sparse_pipeline.geometry_artifacts import GeometryArtifactWriter
from app.sparse_pipeline.object_reconstruction import (
    ObjectKind,
    ObjectReconstructor,
)
from app.sparse_pipeline.v16_geometry import V16GeometryAnalyzer
from app.sparse_pipeline.v16_recursive_grid import (
    RecursiveGridConfig,
    RecursiveStopFlag,
    analyze_recursive_grid,
)
from app.sparse_pipeline.v16_sparse_codes import MERGE_LEFT_CODE

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = REPOSITORY_ROOT / "debug" / "fixtures"
ORACLE_ROOT = REPOSITORY_ROOT / "debug" / "labs" / "legacy-grid-external-oracle-20260720-v1" / "official-v16-traces"


def test_external_character_height_flag_stops_one_recursive_crop() -> None:
    image = Image.new("RGB", (240, 140), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        23,
    )
    draw.text((24, 14), "first normal line", fill="black", font=font)
    draw.text((24, 88), "second normal line", fill="black", font=font)
    base = RecursiveGridConfig(
        deskew=False,
        preprocess_steps=(),
        min_cell_height=12,
        min_separator_gap=4,
    )
    ordinary = analyze_recursive_grid(image, base)
    stopped = analyze_recursive_grid(
        image,
        RecursiveGridConfig(
            deskew=False,
            preprocess_steps=(),
            min_cell_height=12,
            min_separator_gap=4,
            stop_flags=(
                RecursiveStopFlag(
                    node_path=(),
                    character_height_px=23,
                    source="test-oracle",
                ),
            ),
        ),
    )
    try:
        assert len(ordinary.leaves) == 2
        assert len(stopped.leaves) == 1
        metadata = stopped.leaves[0].decisions[-1].metadata()
        assert metadata["split"] == "stop:character-height-flag"
        assert metadata["stop_flag"] is True
        assert metadata["character_height_px"] == 23
        assert metadata["stop_source"] == "test-oracle"
        assert len(stopped.nodes) == 1
        assert stopped.nodes[0].stop_flag == RecursiveStopFlag(
            node_path=(),
            character_height_px=23,
            source="test-oracle",
        )
    finally:
        for analysis in (ordinary, stopped):
            for leaf in analysis.leaves:
                leaf.image.close()


def test_unvisited_recursive_stop_flag_is_rejected() -> None:
    image = Image.new("RGB", (120, 60), "white")
    ImageDraw.Draw(image).text((8, 18), "one line", fill="black")
    with pytest.raises(ValueError, match="stop flag path was not visited"):
        analyze_recursive_grid(
            image,
            RecursiveGridConfig(
                deskew=False,
                preprocess_steps=(),
                stop_flags=(
                    RecursiveStopFlag(
                        node_path=(9,),
                        character_height_px=12,
                    ),
                ),
            ),
        )


@pytest.fixture(scope="module")
def real_v16_bundles():
    bundles = {}
    for name in (
        "000041301_UchebPlan_sign000029629.pdf.raster.png",
        "09.03.03_05(ИУ1).pdf.raster.png",
    ):
        path = FIXTURE_ROOT / name
        if not path.is_file():
            pytest.skip(f"real v16 fixture is absent: {path}")
        with Image.open(path) as image:
            bundles[name] = V16GeometryAnalyzer().analyze_bundle(image)
    return bundles


@pytest.mark.parametrize(
    (
        "name",
        "leaves",
        "groups",
        "shape",
        "codes",
        "excluded",
        "duplicated",
        "table_indexes",
    ),
    (
        (
            "000041301_UchebPlan_sign000029629.pdf.raster.png",
            33,
            10,
            (42, 95),
            386,
            4468,
            9666,
            {5, 6, 7},
        ),
        (
            "09.03.03_05(ИУ1).pdf.raster.png",
            113,
            18,
            (130, 22),
            107,
            38314,
            16876,
            {1},
        ),
    ),
)
def test_adapter_replays_literal_v16_and_keeps_exclusion_separate(
    real_v16_bundles,
    name: str,
    leaves: int,
    groups: int,
    shape: tuple[int, int],
    codes: int,
    excluded: int,
    duplicated: int,
    table_indexes: set[int],
) -> None:
    oracle_path = ORACLE_ROOT / name / "02-recursive-grid-trace.json"
    if not oracle_path.is_file():
        pytest.skip(f"official v16 trace is absent: {oracle_path}")
    oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
    bundle = real_v16_bundles[name]
    trace = bundle.v16_trace
    matrix = bundle.result.matrix

    assert len(trace.leaves) == leaves
    assert len(trace.groups) == groups
    assert (trace.rows, trace.columns) == shape
    assert len(trace.codes) == codes
    assert trace.excluded_foreground_pixels == excluded
    assert trace.duplicated_foreground_pixels == duplicated
    assert matrix.coordinate_mode is SparseCoordinateMode.LOGICAL_PROJECTION
    assert matrix.projection_sha256 == trace.projection_sha256
    assert len(matrix.cells) == leaves
    assert len(matrix.structural_codes) == codes
    assert tuple((item.row, item.column) for item in matrix.cells) == tuple(item.anchor for item in trace.leaves)

    assert trace.rows == oracle["rows"]
    assert trace.columns == oracle["cols"]
    assert list(trace.x_tracks) == oracle["x_tracks"]
    assert [list(item) for item in trace.codes] == oracle["codes"]
    for actual, expected_leaf, expected_projection in zip(
        trace.leaves,
        oracle["leaves"],
        oracle["projections"],
    ):
        assert list(actual.source_bbox.as_tuple()) == expected_leaf["source_bbox"]
        assert list(actual.content_bbox.as_tuple()) == expected_leaf["content_bbox"]
        assert list(actual.left_tracks) == expected_leaf["left_tracks"]
        assert actual.dash_track == expected_leaf["dash_track"]
        assert list(actual.merge_left_tracks) == expected_leaf["merge_left_tracks"]
        assert list(actual.anchor) == expected_projection["anchor"]
        assert [list(item) for item in actual.codes] == expected_projection["codes"]
        assert json.loads(json.dumps([item.metadata() for item in actual.decisions])) == expected_leaf["decisions"]

    assert not np.any(bundle.foreground_mask & bundle.excluded_foreground_mask)
    assert np.all(bundle.ownership[bundle.foreground_mask] >= 0)
    assert np.all(bundle.ownership[~bundle.foreground_mask] == -1)
    ownership_counts = np.bincount(
        bundle.ownership[bundle.ownership >= 0],
        minlength=leaves,
    )
    assert np.all(ownership_counts > 0)
    assert tuple(int(item) for item in ownership_counts) == tuple(
        item.ink_pixels for item in bundle.result.segmentation.segments
    )

    objects = ObjectReconstructor().reconstruct(
        aligned_size=bundle.result.segmentation.aligned_size,
        segments=bundle.result.segmentation.segments,
        rules=bundle.result.segmentation.rules,
        matrix=matrix,
    )
    assert len(objects.objects) == groups
    assert {index for index, item in enumerate(objects.objects) if item.kind is ObjectKind.TABLE} == table_indexes


@pytest.mark.parametrize(
    ("name", "table_size", "sliding_blocks"),
    (
        ("000041301_UchebPlan_sign000029629.pdf.raster.png", 14, 13),
        ("09.03.03_05(ИУ1).pdf.raster.png", 17, 16),
    ),
)
def test_real_logical_tables_use_overlapping_pairs_not_one_giant_block(
    real_v16_bundles,
    name: str,
    table_size: int,
    sliding_blocks: int,
) -> None:
    bundle = real_v16_bundles[name]
    objects = ObjectReconstructor().reconstruct(
        aligned_size=bundle.result.segmentation.aligned_size,
        segments=bundle.result.segmentation.segments,
        rules=(),
        matrix=bundle.result.matrix,
    )
    target_index, target = next(
        (index, item)
        for index, item in enumerate(objects.objects)
        if item.kind is ObjectKind.TABLE and len(item.segment_ids) == table_size
    )
    plan = OverlappingBlockPlanner(
        BlockPlanningConfig(
            mode=BlockPlanningMode.SPATIAL_2D,
            padding=0,
            max_block_pixels=50_000_000,
        )
    ).plan(
        aligned_size=bundle.result.segmentation.aligned_size,
        segments=bundle.result.segmentation.segments,
        objects_result=objects,
        matrix=bundle.result.matrix,
    )
    scope_id = f"scope-{target_index:06d}"
    blocks = tuple(item for item in plan.blocks if item.scope_id == scope_id)
    units = tuple(item for item in plan.membership_units if item.scope_id == scope_id)

    assert len(blocks) == sliding_blocks
    assert all(len(item.segment_ids) == 2 for item in blocks)
    assert len(units) == table_size
    assert all(item.kind is MembershipUnitKind.SEGMENT and len(item.segment_ids) == 1 for item in units)


def _segment(segment_id: str, row: int, left: int = 10) -> Segment:
    bbox = Box(left, 10 + row * 12, left + 80, 20 + row * 12)
    return Segment(
        segment_id=segment_id,
        bbox=bbox,
        source_bbox=bbox,
        kind=SegmentKind.TEXT,
        ink_pixels=10,
        row_index=row,
        order_key=(bbox.top, bbox.left),
        parent_path=("fixture-root",),
    )


def _logical_matrix(
    placements: dict[str, tuple[int, int]],
    *,
    rows: int,
    columns: int,
    codes: tuple[tuple[str, int, int, int], ...] = (),
) -> SparseSegmentMatrix:
    return SparseSegmentMatrix(
        rows=tuple(AxisInterval(index, index, index + 1) for index in range(rows)),
        columns=tuple(AxisInterval(index, index, index + 1) for index in range(columns)),
        cells=tuple(
            sorted(
                (SparseCell(row, column, segment_id) for segment_id, (row, column) in placements.items()),
                key=lambda item: (item.row, item.column, item.segment_id),
            )
        ),
        spans=tuple(
            SegmentSpan(segment_id, row, row + 1, column, column + 1)
            for segment_id, (row, column) in placements.items()
        ),
        coordinate_mode=SparseCoordinateMode.LOGICAL_PROJECTION,
        structural_codes=tuple(
            sorted(
                (SparseStructuralCode(row, column, code, segment_id) for segment_id, row, column, code in codes),
                key=lambda item: (
                    item.row,
                    item.column,
                    item.segment_id,
                    item.code,
                ),
            )
        ),
        projection_sha256="0" * 64,
    )


def test_logical_paragraph_and_list_are_not_promoted_to_tables() -> None:
    paragraph_segments = tuple(_segment(f"p-{row}", row) for row in range(3))
    paragraph_matrix = _logical_matrix(
        {item.segment_id: (row, 0) for row, item in enumerate(paragraph_segments)},
        rows=3,
        columns=2,
    )
    paragraph = ObjectReconstructor().reconstruct(
        aligned_size=(200, 100),
        segments=paragraph_segments,
        rules=(),
        matrix=paragraph_matrix,
    )
    assert tuple(item.kind for item in paragraph.objects) == (ObjectKind.PARAGRAPH,)

    list_segments = tuple(_segment(f"l-{row}", row) for row in range(3))
    list_matrix = _logical_matrix(
        {
            list_segments[0].segment_id: (0, 0),
            list_segments[1].segment_id: (1, 1),
            list_segments[2].segment_id: (2, 1),
        },
        rows=3,
        columns=2,
    )
    listing = ObjectReconstructor().reconstruct(
        aligned_size=(200, 100),
        segments=list_segments,
        rules=(),
        matrix=list_matrix,
    )
    assert tuple(item.kind for item in listing.objects) == (ObjectKind.LIST,)


def test_blank_row_keeps_two_consecutive_logical_tables_separate() -> None:
    segments = tuple(_segment(f"t-{row}", row) for row in range(4))
    placements = {
        segments[0].segment_id: (0, 0),
        segments[1].segment_id: (1, 0),
        segments[2].segment_id: (3, 0),
        segments[3].segment_id: (4, 0),
    }
    codes = tuple(
        (segment_id, row, column, MERGE_LEFT_CODE)
        for segment_id, row in (
            (segments[0].segment_id, 0),
            (segments[1].segment_id, 1),
            (segments[2].segment_id, 3),
            (segments[3].segment_id, 4),
        )
        for column in (1, 2, 3)
    )
    matrix = _logical_matrix(
        placements,
        rows=5,
        columns=4,
        codes=codes,
    )
    result = ObjectReconstructor().reconstruct(
        aligned_size=(200, 100),
        segments=segments,
        rules=(),
        matrix=matrix,
    )

    assert len(result.objects) == 2
    assert all(item.kind is ObjectKind.TABLE for item in result.objects)
    assert {frozenset(item.segment_ids) for item in result.objects} == {
        frozenset((segments[0].segment_id, segments[1].segment_id)),
        frozenset((segments[2].segment_id, segments[3].segment_id)),
    }


def test_blank_faint_and_rgba_pages_share_canonical_source_policy() -> None:
    blank = Image.new("RGB", (1000, 300), "white")
    blank_bundle = V16GeometryAnalyzer().analyze_bundle(blank)
    blank.close()
    assert blank_bundle.result.status is GeometryStatus.COMPLETE
    assert not blank_bundle.result.segmentation.segments
    assert blank_bundle.result.segmentation.nodes[0].stop_reason is StopReason.EMPTY
    assert blank_bundle.result.matrix.coordinate_mode is (SparseCoordinateMode.LOGICAL_PROJECTION)
    assert not blank_bundle.result.matrix.rows

    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if not font_path.is_file():
        pytest.skip("DejaVuSans is unavailable")
    font = ImageFont.truetype(str(font_path), 48)
    faint = Image.new("RGB", (1000, 300), "white")
    ImageDraw.Draw(faint).text(
        (100, 100),
        "Faint context text",
        fill=(235, 235, 235),
        font=font,
    )
    faint_bundle = V16GeometryAnalyzer().analyze_bundle(faint)
    faint.close()
    assert len(faint_bundle.result.segmentation.segments) == 1
    assert faint_bundle.result.alignment.foreground_pixels > 0
    assert "faint_leaf_rescue_pixels=0" not in faint_bundle.result.diagnostics

    rgba = Image.new("RGBA", (1000, 300), (255, 255, 255, 0))
    ImageDraw.Draw(rgba).text(
        (100, 100),
        "RGBA context text",
        fill=(0, 0, 0, 255),
        font=font,
    )
    rgba_bundle = V16GeometryAnalyzer().analyze_bundle(rgba)
    rgba.close()
    assert len(rgba_bundle.result.segmentation.segments) == 1
    assert rgba_bundle.source_rgb.shape == (300, 1000, 3)
    assert np.all(rgba_bundle.source_rgb[0, 0] == 255)


def test_v16_adapter_honors_component_and_node_limits(real_v16_bundles) -> None:
    path = FIXTURE_ROOT / "000041301_UchebPlan_sign000029629.pdf.raster.png"
    with Image.open(path) as image:
        with pytest.raises(GeometryLimitError, match="leaf/component count"):
            V16GeometryAnalyzer(GeometryConfig(max_components=1)).analyze_bundle(image)
    with Image.open(path) as image:
        with pytest.raises(GeometryLimitError, match="node limit"):
            V16GeometryAnalyzer(GeometryConfig(max_nodes=1)).analyze_bundle(image)


def test_v16_artifact_writer_persists_literal_and_excluded_layers(
    tmp_path: Path,
) -> None:
    image = Image.new("RGB", (320, 240), "white")
    ImageDraw.Draw(image).text((20, 80), "artifact evidence", fill="black")
    bundle = V16GeometryAnalyzer().analyze_bundle(image)
    image.close()

    run_dir = GeometryArtifactWriter().write(
        tmp_path,
        run_id="v16-artifacts",
        bundle=bundle,
    )
    stage = run_dir / "01-geometry"
    required = (
        "legacy-matrix.json",
        "legacy-matrix.txt",
        "matrix-numbered-overlay.png",
        "matrix-numbered-overlay.txt",
        "excluded-foreground.png",
        "excluded-foreground-isolated.png",
        "legacy-ownership.json",
        "matrix.json",
        "matrix.txt",
    )
    assert all((stage / item).is_file() for item in required)
    manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    matrix = json.loads((stage / "matrix.json").read_text(encoding="utf-8"))
    legacy = json.loads((stage / "legacy-matrix.json").read_text(encoding="utf-8"))
    assert matrix["coordinate_mode"] == "logical_projection"
    assert manifest["legacy_projection"]["projection_sha256"] == (bundle.v16_trace.projection_sha256)
    assert legacy["projection_sha256"] == bundle.v16_trace.projection_sha256
    assert "leaves" in legacy and "groups" in legacy and "codes" in legacy
