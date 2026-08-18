from __future__ import annotations

from itertools import combinations
from random import Random

import pytest

from app.sparse_pipeline.block_planning import (
    BlockPlan,
    BlockPlanningConfig,
    BlockPlanningInvariantError,
    BlockPlanningLimitError,
    BlockPlanningMode,
    MembershipUnit,
    MembershipUnitKind,
    MatrixLocalIsland,
    MatrixLocalPlacement,
    OverlappingBlockPlanner,
    RecognitionBlock,
    _PlanningScope,
)
from app.sparse_pipeline.contracts import Box, Segment, SegmentKind


def _planner() -> OverlappingBlockPlanner:
    return OverlappingBlockPlanner(
        BlockPlanningConfig(
            mode=BlockPlanningMode.SPATIAL_2D,
            adaptive_table_windows=True,
            max_signature_candidates=512,
            max_signature_candidate_checks=50_000_000,
        )
    )


def _reduce(
    members: tuple[tuple[str, ...], ...],
    *,
    window_kinds: tuple[str | None, ...] | None = None,
) -> tuple[int, ...]:
    source_ids = tuple(
        dict.fromkeys(segment_id for block in members for segment_id in block)
    )
    selected, _statistics = _planner()._sparse_identifying_candidate_indexes(
        members=members,
        scope_ids=("scope-000000",) * len(members),
        windows=((0, 1, 0, 1),) * len(members),
        window_kinds=window_kinds or ("dyadic-mask",) * len(members),
        source_ids=source_ids,
    )
    return selected


def _signatures(
    members: tuple[tuple[str, ...], ...],
    selected: tuple[int, ...],
) -> tuple[tuple[int, ...], ...]:
    source_ids = tuple(
        dict.fromkeys(segment_id for block in members for segment_id in block)
    )
    return tuple(
        tuple(index for index in selected if segment_id in members[index])
        for segment_id in source_ids
    )


def test_feature_reducer_preserves_unique_nonzero_signatures() -> None:
    members = (
        ("segment-0", "segment-1"),
        ("segment-1", "segment-2"),
        ("segment-0", "segment-2"),
    )

    selected = _reduce(members)
    signatures = _signatures(members, selected)

    assert len(selected) <= len(members)
    assert all(signatures)
    assert len(signatures) == len(set(signatures))
    assert selected == _reduce(members)


def test_feature_reducer_fails_closed_without_mutating_impossible_input() -> None:
    members = (("segment-0", "segment-1"),)
    original = members

    with pytest.raises(
        BlockPlanningInvariantError,
        match="unique nonzero signature",
    ):
        _reduce(members)

    assert members == original


def test_feature_reducer_preserves_marked_header_block() -> None:
    members = (
        ("segment-0", "segment-1"),
        ("segment-1", "segment-2"),
        ("segment-0", "segment-2"),
    )
    kinds = ("dyadic-mask", "dyadic-mask", "structural-singleton")

    selected = _reduce(members, window_kinds=kinds)

    assert 2 in selected
    assert len(_signatures(members, selected)) == len(
        set(_signatures(members, selected))
    )


def test_b201_shaped_feature_matrix_reduces_to_sixty_blocks() -> None:
    feature_count = 60
    segment_count = 2_901
    codes = list(combinations(range(feature_count), 1))
    codes.extend(combinations(range(feature_count), 2))
    triples = list(combinations(range(feature_count), 3))
    Random(0).shuffle(triples)
    codes.extend(triples[: segment_count - len(codes)])
    source_ids = tuple(
        f"segment-{index:06d}" for index in range(segment_count)
    )
    base_members = tuple(
        tuple(
            segment_id
            for segment_id, code in zip(source_ids, codes)
            if feature_index in code
        )
        for feature_index in range(feature_count)
    )
    members = base_members + tuple(
        base_members[index % feature_count] for index in range(94)
    )

    assert len(members) == 154
    assert max(map(len, members)) <= 256

    selected = _reduce(members)
    signatures = _signatures(members, selected)

    assert len(selected) <= 60
    assert all(signatures)
    assert len(signatures) == segment_count
    assert len(signatures) == len(set(signatures))


def test_ambiguous_b201_shape_is_repaired_without_segment_loss() -> None:
    segment_count = 2_901
    original_block_count = 154
    source_ids = tuple(
        f"segment-{index:06d}" for index in range(segment_count)
    )
    ambiguous_members = tuple(
        source_ids[offset::original_block_count]
        for offset in range(original_block_count)
    )
    planner = _planner()

    assert not planner._signature_family_is_identifying(
        members=ambiguous_members,
        source_ids=source_ids,
    )
    repaired, origins = planner._repair_nonidentifying_memberships(
        members=ambiguous_members,
        source_ids=source_ids,
        mandatory_indexes=(),
    )
    repeated, repeated_origins = planner._repair_nonidentifying_memberships(
        members=ambiguous_members,
        source_ids=source_ids,
        mandatory_indexes=(),
    )

    assert (repaired, origins) == (repeated, repeated_origins)
    assert len(repaired) <= 60
    assert all(2 <= len(block_members) <= 256 for block_members in repaired)
    assert {
        segment_id
        for block_members in repaired
        for segment_id in block_members
    } == set(source_ids)
    assert planner._signature_family_is_identifying(
        members=repaired,
        source_ids=source_ids,
    )

    selected, _statistics = planner._sparse_identifying_candidate_indexes(
        members=repaired,
        scope_ids=("scope-000000",) * len(repaired),
        windows=((0, 1, 0, 1),) * len(repaired),
        window_kinds=("dyadic-mask",) * len(repaired),
        source_ids=source_ids,
    )
    selected_members = tuple(repaired[index] for index in selected)

    assert len(selected_members) <= 60
    assert planner._signature_family_is_identifying(
        members=selected_members,
        source_ids=source_ids,
    )


def test_b201_locality_family_exports_two_island_layout_contract() -> None:
    source_ids = tuple(
        f"segment-{index:06d}" for index in range(2_949)
    )
    planner = _planner()
    regions = planner._locality_preserving_regions(source_ids)
    family = planner._locality_preserving_signature_candidate_family(source_ids)
    span_by_id = {
        segment_id: type(
            "Span",
            (),
            {
                "row_start": index // 94,
                "column_start": index % 94,
            },
        )()
        for index, segment_id in enumerate(source_ids)
    }

    assert len(regions) == 12
    assert family[:12] == regions
    assert len(family) == 60
    assert family == planner._locality_preserving_signature_candidate_family(
        source_ids
    )
    assert all(2 <= len(block_members) <= 256 for block_members in family)
    assert planner._signature_family_is_identifying(
        members=family,
        source_ids=source_ids,
    )

    for index, block_members in enumerate(family):
        islands, placements = planner._local_block_layout_metadata(
            members=block_members,
            regions=regions,
            span_by_id=span_by_id,
        )
        assert len(islands) == (1 if index < len(regions) else 2)
        assert tuple(item.segment_id for item in placements) == block_members
        assert {
            item.segment_id for island in islands for item in placements
            if item.island_id == island.island_id
        } == set(block_members)
        assert all(max(island.matrix_segment_shape) <= 16 for island in islands)
        assert len(
            {
                (item.island_id, item.matrix_row, item.matrix_column)
                for item in placements
            }
        ) == len(placements)


@pytest.mark.parametrize(
    ("segment_count", "expected_blocks"),
    (
        (257, 10),
        (2_948, 60),
        (3_072, 60),
        (3_073, 69),
        (4_771, 99),
    ),
)
def test_locality_family_uses_derived_resource_bound(
    segment_count: int,
    expected_blocks: int,
) -> None:
    source_ids = tuple(
        f"segment-{index:06d}" for index in range(segment_count)
    )
    planner = _planner()
    regions = planner._locality_preserving_regions(source_ids)
    region_codes = planner._locality_region_codes(regions)
    minimum, maximum = planner._locality_family_block_bounds(
        regions=regions,
        region_codes=region_codes,
    )
    family = planner._locality_preserving_signature_candidate_family(source_ids)

    assert len(regions) == (segment_count + 255) // 256
    assert minimum <= len(family) <= maximum
    assert len(family) == expected_blocks
    assert all(2 <= len(block_members) <= 256 for block_members in family)
    assert planner._signature_family_is_identifying(
        members=family,
        source_ids=source_ids,
    )


def test_local_block_contract_rejects_missing_or_fake_placements() -> None:
    island = MatrixLocalIsland(
        local_region_id="local-region-000000",
        island_id="island-000000",
        segment_ids=("segment-0", "segment-1"),
        region_boundary=(0, 2),
        matrix_segment_shape=(1, 2),
    )
    placement = MatrixLocalPlacement(
        segment_id="segment-0",
        local_region_id=island.local_region_id,
        island_id=island.island_id,
        polar_order=0,
        region_order=0,
        table_row=0,
        table_column=0,
        matrix_row=0,
        matrix_column=0,
    )

    with pytest.raises(ValueError, match="placements must exactly"):
        RecognitionBlock(
            block_id="block-000000",
            bbox=Box(0, 0, 20, 10),
            core_segment_ids=("segment-0",),
            segment_ids=("segment-0", "segment-1"),
            context_segment_ids=("segment-1",),
            object_ids=("object-000000",),
            scope_id="scope-000000",
            matrix_window=(0, 1, 0, 2),
            matrix_window_kind="polar-local-full",
            matrix_segment_shape=(1, 2),
            local_islands=(island,),
            local_placements=(placement,),
        )


def test_wide_polar_members_use_compact_tile_pixel_footprint() -> None:
    planner = _planner()
    member_bboxes = (
        Box(0, 0, 40, 20),
        Box(20_000_000, 0, 20_000_040, 20),
        Box(40_000_000, 0, 40_000_040, 20),
    )
    cartesian_union = Box.union(member_bboxes)

    assert cartesian_union.area > planner.config.max_block_pixels
    compact_footprint = planner._validated_matrix_block_pixel_footprint(
        bbox=member_bboxes[0],
        member_bboxes=member_bboxes,
        window_kind="polar-signature",
    )

    assert compact_footprint <= planner.config.max_block_pixels
    with pytest.raises(BlockPlanningLimitError, match="matrix block pixel"):
        planner._validated_matrix_block_pixel_footprint(
            bbox=cartesian_union,
            member_bboxes=member_bboxes,
            window_kind="dyadic-mask",
        )


def test_oversized_compact_tiles_fail_without_mutating_signatures() -> None:
    planner = _planner()
    source_ids = ("segment-0", "segment-1", "segment-2")
    family = planner._balanced_sparse_signature_candidate_family(source_ids)
    original = family
    huge_by_segment = {
        "segment-0": Box(0, 0, 3_000, 3_000),
        "segment-1": Box(4_000, 0, 7_000, 3_000),
        "segment-2": Box(8_000, 0, 11_000, 3_000),
    }

    assert planner._signature_family_is_identifying(
        members=family,
        source_ids=source_ids,
    )
    for block_members in family:
        with pytest.raises(BlockPlanningLimitError, match="compact tile"):
            planner._validated_matrix_block_pixel_footprint(
                bbox=huge_by_segment[block_members[0]],
                member_bboxes=tuple(
                    huge_by_segment[segment_id]
                    for segment_id in block_members
                ),
                window_kind="polar-signature",
            )

    assert family == original
    assert planner._signature_family_is_identifying(
        members=family,
        source_ids=source_ids,
    )


def _segment(
    segment_id: str,
    bbox: Box,
    *,
    ink_pixels: int,
    row_index: int,
    component_ids: tuple[int, ...],
) -> Segment:
    return Segment(
        segment_id=segment_id,
        bbox=bbox,
        source_bbox=bbox,
        kind=SegmentKind.TEXT,
        ink_pixels=ink_pixels,
        row_index=row_index,
        order_key=(bbox.top, bbox.left),
        parent_path=("root",),
        component_ids=component_ids,
    )


def test_table_ocr_eligibility_rejects_sparse_hairline_but_keeps_dash() -> None:
    segments = (
        _segment(
            "segment-text-a",
            Box(0, 0, 20, 6),
            ink_pixels=48,
            row_index=0,
            component_ids=(1, 2),
        ),
        _segment(
            "segment-text-b",
            Box(24, 0, 44, 6),
            ink_pixels=50,
            row_index=0,
            component_ids=(3, 4),
        ),
        _segment(
            "segment-dash",
            Box(48, 3, 51, 4),
            ink_pixels=3,
            row_index=0,
            component_ids=(5,),
        ),
        _segment(
            "segment-empty-hairline",
            Box(54, 3, 68, 4),
            ink_pixels=4,
            row_index=0,
            component_ids=(6,),
        ),
        _segment(
            "segment-sparse-fragment",
            Box(70, 3, 74, 4),
            ink_pixels=2,
            row_index=0,
            component_ids=(7, 8),
        ),
        _segment(
            "segment-square-speck",
            Box(76, 2, 78, 4),
            ink_pixels=2,
            row_index=0,
            component_ids=(9,),
        ),
    )
    scope = _PlanningScope(
        "scope-000000",
        tuple(item.segment_id for item in segments),
        True,
    )

    eligible = _planner()._table_ocr_eligible_segment_ids(
        scope=scope,
        segment_by_id={item.segment_id: item for item in segments},
    )

    assert eligible == (
        "segment-text-a",
        "segment-text-b",
        "segment-dash",
    )


def test_table_ocr_eligibility_rejects_rules_but_keeps_text_and_dash() -> None:
    segments = (
        _segment(
            "segment-text-a",
            Box(0, 0, 20, 6),
            ink_pixels=48,
            row_index=0,
            component_ids=(1, 2),
        ),
        _segment(
            "segment-text-b",
            Box(24, 0, 44, 6),
            ink_pixels=52,
            row_index=0,
            component_ids=(3, 4),
        ),
        _segment(
            "segment-dash",
            Box(48, 2, 51, 3),
            ink_pixels=2,
            row_index=0,
            component_ids=(5,),
        ),
        _segment(
            "segment-grid-rule",
            Box(0, 10, 200, 11),
            ink_pixels=12,
            row_index=1,
            component_ids=tuple(range(10, 22)),
        ),
        _segment(
            "segment-speck",
            Box(0, 12, 1, 13),
            ink_pixels=1,
            row_index=2,
            component_ids=(30,),
        ),
    )
    scope = _PlanningScope(
        scope_id="scope-000000",
        segment_ids=tuple(item.segment_id for item in segments),
        ruled=True,
    )

    eligible = OverlappingBlockPlanner._table_ocr_eligible_segment_ids(
        scope=scope,
        segment_by_id={item.segment_id: item for item in segments},
    )

    assert eligible == (
        "segment-text-a",
        "segment-text-b",
        "segment-dash",
    )


def test_structural_table_source_stays_in_plan_but_not_ocr_membership() -> None:
    text_id = "segment-text"
    structural_id = "segment-grid-rule"
    block = RecognitionBlock(
        block_id="block-000000",
        bbox=Box(0, 0, 20, 6),
        core_segment_ids=(text_id,),
        segment_ids=(text_id,),
        context_segment_ids=(),
        object_ids=("object-000000",),
        scope_id="scope-000000",
        matrix_window=(0, 1, 0, 1),
        matrix_window_kind="dyadic-mask",
        matrix_segment_shape=(1, 1),
    )
    unit = MembershipUnit(
        unit_id="membership-unit-000000",
        kind=MembershipUnitKind.SEGMENT,
        segment_ids=(text_id,),
        block_ids=(block.block_id,),
        scope_id="scope-000000",
    )

    plan = BlockPlan(
        aligned_size=(200, 100),
        source_segment_ids=(text_id, structural_id),
        blocks=(block,),
        adjacent_algebra=(),
        mode=BlockPlanningMode.SPATIAL_2D,
        membership_units=(unit,),
        matrix_sha256="0" * 64,
        ocr_eligible_segment_ids=(text_id,),
    )

    assert structural_id in plan.source_segment_ids
    assert structural_id not in plan.ocr_eligible_segment_ids
    assert all(
        structural_id not in item.segment_ids for item in plan.membership_units
    )
