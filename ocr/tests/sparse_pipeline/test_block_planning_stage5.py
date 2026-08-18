from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.sparse_pipeline.block_artifacts import BlockArtifactWriter
from app.sparse_pipeline.block_crops import (
    BlockCropConfig,
    BlockCropInvariantError,
    BlockCropLimitError,
    BlockCropPair,
    BlockCropper,
)
from app.sparse_pipeline.block_planning import (
    BlockPlan,
    BlockPlanStatus,
    BlockPlanningConfig,
    BlockPlanningInvariantError,
    BlockPlanningLimitError,
    BlockPlanningMode,
    BlockSetAlgebra,
    MembershipUnit,
    MembershipUnitKind,
    OverlappingBlockPlanner,
    RecognitionBlock,
    sparse_matrix_sha256,
)
from app.sparse_pipeline.contracts import (
    AxisInterval,
    Box,
    Segment,
    SegmentKind,
    SegmentSpan,
    SparseCell,
    SparseSegmentMatrix,
)
from app.sparse_pipeline.crop_enhancement import (
    RECIPE_ID,
    CropInput,
    EnhancedCrop,
    EnhancementBackend,
)
from app.sparse_pipeline.object_reconstruction import (
    DocumentObject,
    ObjectKind,
    ObjectReconstructionResult,
    SegmentObjectOwnership,
)


def _segment(index: int, bbox: Box | None = None) -> Segment:
    value = bbox or Box(10, 10 + index * 20, 90, 20 + index * 20)
    return Segment(
        segment_id=f"segment-{index:06d}",
        bbox=value,
        source_bbox=value,
        kind=SegmentKind.TEXT,
        ink_pixels=max(1, min(10, value.area)),
        row_index=index,
        order_key=(index, 0),
        parent_path=("root",),
        component_ids=(index,),
    )


def _objects_result(
    segments: tuple[Segment, ...],
    groups: tuple[tuple[int, ...], ...],
    *,
    aligned_size: tuple[int, int] = (100, 120),
) -> ObjectReconstructionResult:
    objects = []
    ownership: dict[str, str] = {}
    for object_index, indexes in enumerate(groups):
        members = tuple(segments[index] for index in indexes)
        object_id = f"object-{object_index:06d}"
        for member in members:
            ownership[member.segment_id] = object_id
        objects.append(
            DocumentObject(
                object_id=object_id,
                kind=ObjectKind.PARAGRAPH,
                segment_ids=tuple(member.segment_id for member in members),
                bbox=Box.union(member.bbox for member in members),
                reading_index=object_index,
                row_start=min(member.row_index for member in members),
                row_stop=max(member.row_index for member in members) + 1,
                column_start=0,
                column_stop=1,
                confidence=1.0,
            )
        )
    source_ids = tuple(segment.segment_id for segment in segments)
    return ObjectReconstructionResult(
        aligned_size=aligned_size,
        source_segment_ids=source_ids,
        objects=tuple(objects),
        segment_ownership=tuple(SegmentObjectOwnership(segment_id, ownership[segment_id]) for segment_id in source_ids),
    )


def _config(**overrides: object) -> BlockPlanningConfig:
    values: dict[str, object] = {
        "max_segments": 100,
        "max_objects": 100,
        "max_core_segments": 20,
        "max_block_pixels": 100_000,
        "context_segments": 1,
        "padding": 0,
        "max_blocks": 20,
        "max_pair_memberships": 1_000,
    }
    values.update(overrides)
    return BlockPlanningConfig(**values)


def _crop_config(**overrides: object) -> BlockCropConfig:
    return replace(BlockCropConfig(), **overrides)


def _plan(
    segments: tuple[Segment, ...],
    groups: tuple[tuple[int, ...], ...],
    *,
    aligned_size: tuple[int, int] = (100, 120),
    config: BlockPlanningConfig | None = None,
) -> BlockPlan:
    matrix = _matrix_for_segments(segments, aligned_size=aligned_size)
    return OverlappingBlockPlanner(config).plan(
        aligned_size=aligned_size,
        segments=segments,
        objects_result=_objects_result(
            segments,
            groups,
            aligned_size=aligned_size,
        ),
        matrix=matrix,
    )


def _matrix_for_segments(
    segments: tuple[Segment, ...],
    *,
    aligned_size: tuple[int, int],
) -> SparseSegmentMatrix:
    row_cuts = {0, aligned_size[1]}
    column_cuts = {0, aligned_size[0]}
    for segment in segments:
        row_cuts.update((segment.bbox.top, segment.bbox.bottom))
        column_cuts.update((segment.bbox.left, segment.bbox.right))
    rows = tuple(
        AxisInterval(index, start, stop)
        for index, (start, stop) in enumerate(
            zip(sorted(row_cuts), sorted(row_cuts)[1:])
        )
    )
    columns = tuple(
        AxisInterval(index, start, stop)
        for index, (start, stop) in enumerate(
            zip(sorted(column_cuts), sorted(column_cuts)[1:])
        )
    )
    cells = tuple(
        sorted(
            (
                SparseCell(row.index, column.index, segment.segment_id)
                for segment in segments
                for row in rows
                for column in columns
                if row.start < segment.bbox.bottom
                and segment.bbox.top < row.end
                and column.start < segment.bbox.right
                and segment.bbox.left < column.end
            ),
            key=lambda item: (item.row, item.column, item.segment_id),
        )
    )
    spans = tuple(
        SegmentSpan(
            segment.segment_id,
            min(item.row for item in cells if item.segment_id == segment.segment_id),
            max(item.row for item in cells if item.segment_id == segment.segment_id) + 1,
            min(item.column for item in cells if item.segment_id == segment.segment_id),
            max(item.column for item in cells if item.segment_id == segment.segment_id) + 1,
        )
        for segment in segments
    )
    return SparseSegmentMatrix(rows, columns, cells, spans)


def _grid_segments(
    rows: int,
    columns: int,
    *,
    cell_width: int = 10,
    cell_height: int = 10,
    column_pitch: int = 12,
    row_pitch: int = 15,
) -> tuple[Segment, ...]:
    values = []
    for row in range(rows):
        top = 5 + row * row_pitch
        for column in range(columns):
            left = 5 + column * column_pitch
            index = len(values)
            values.append(
                replace(
                    _segment(
                        index,
                        Box(
                            left,
                            top,
                            left + cell_width,
                            top + cell_height,
                        ),
                    ),
                    row_index=row,
                    order_key=(top, left),
                )
            )
    return tuple(values)


def _canonical_subset(
    source_ids: tuple[str, ...],
    values: set[str],
) -> tuple[str, ...]:
    return tuple(segment_id for segment_id in source_ids if segment_id in values)


def _assert_plan_identities(plan: BlockPlan) -> None:
    source_ids = plan.source_segment_ids
    core_ids = tuple(
        segment_id
        for block in plan.blocks
        for segment_id in block.core_segment_ids
    )
    if plan.mode is BlockPlanningMode.FULL_WIDTH:
        assert core_ids == source_ids
    else:
        assert len(core_ids) == len(set(core_ids))
        assert set(core_ids) == set(source_ids)
    if plan.mode is BlockPlanningMode.FULL_WIDTH:
        assert len(plan.adjacent_algebra) == max(0, len(plan.blocks) - 1)
    for block in plan.blocks:
        core = set(block.core_segment_ids)
        members = set(block.segment_ids)
        context = set(block.context_segment_ids)
        if plan.mode is BlockPlanningMode.FULL_WIDTH:
            assert block.bbox.left == 0
            assert block.bbox.right == plan.aligned_size[0]
        else:
            assert 0 <= block.bbox.left < block.bbox.right <= plan.aligned_size[0]
            assert 0 <= block.bbox.top < block.bbox.bottom <= plan.aligned_size[1]
        assert core <= members <= set(source_ids)
        assert context == members - core
        assert not core & context
        assert block.segment_ids == _canonical_subset(source_ids, members)
        assert block.core_segment_ids == _canonical_subset(source_ids, core)
        assert block.context_segment_ids == _canonical_subset(source_ids, context)
    block_by_id = {block.block_id: block for block in plan.blocks}
    for algebra in plan.adjacent_algebra:
        left = block_by_id[algebra.first_block_id]
        right = block_by_id[algebra.second_block_id]
        first = set(left.segment_ids)
        second = set(right.segment_ids)
        intersection = first & second
        union = first | second
        xor = first ^ second
        assert intersection
        assert algebra.first_block_id == left.block_id
        assert algebra.second_block_id == right.block_id
        assert algebra.intersection_segment_ids == _canonical_subset(source_ids, intersection)
        assert algebra.union_segment_ids == _canonical_subset(source_ids, union)
        assert algebra.xor_segment_ids == _canonical_subset(source_ids, xor)
        assert algebra.first_only_segment_ids == _canonical_subset(source_ids, first - second)
        assert algebra.second_only_segment_ids == _canonical_subset(source_ids, second - first)
        assert intersection == union - xor
        assert xor == (first - second) | (second - first)


def _assert_spatial_crop_closure(
    plan: BlockPlan,
    segments: tuple[Segment, ...],
) -> None:
    assert plan.mode is BlockPlanningMode.SPATIAL_2D
    segment_by_id = {item.segment_id: item for item in segments}
    for block in plan.blocks:
        members = set(block.segment_ids)
        member_bbox = Box.union(
            segment_by_id[item].bbox for item in block.segment_ids
        )
        assert member_bbox.intersection(block.bbox) == member_bbox
        for segment in segments:
            if segment.bbox.intersection(member_bbox) is not None:
                assert segment.segment_id in members
            if segment.segment_id not in members:
                assert segment.bbox.intersection(block.bbox) is None


def _png_bytes(array: np.ndarray, *, mode: str | None = None) -> bytes:
    output = io.BytesIO()
    image = Image.fromarray(array, mode=mode)
    try:
        image.save(output, format="PNG", compress_level=1)
    finally:
        image.close()
    return output.getvalue()


def _crop_pixels(payload: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(payload)) as image:
        image.load()
        return np.array(image, copy=True)


def _ownership_raster(
    segments: tuple[Segment, ...],
    *,
    aligned_size: tuple[int, int],
) -> np.ndarray:
    width, height = aligned_size
    ownership = np.full((height, width), -1, dtype=np.int32)
    for label, segment in enumerate(segments):
        ownership[
            segment.bbox.top : segment.bbox.bottom,
            segment.bbox.left : segment.bbox.right,
        ] = label
    ownership.flags.writeable = False
    return ownership


def _artifact_files(stage_dir: Path) -> tuple[str, ...]:
    return tuple(sorted(path.relative_to(stage_dir).as_posix() for path in stage_dir.rglob("*") if path.is_file()))


def test_one_block_preserves_exact_core_order_and_padded_bbox() -> None:
    segments = tuple(_segment(index) for index in range(4))
    plan = _plan(
        segments,
        ((0, 1), (2, 3)),
        config=_config(padding=3),
    )

    assert plan.status is BlockPlanStatus.COMPLETE
    assert plan.aligned_size == (100, 120)
    assert plan.source_segment_ids == tuple(item.segment_id for item in segments)
    assert len(plan.blocks) == 1
    block = plan.blocks[0]
    assert block.block_id == "block-000000"
    assert block.core_segment_ids == plan.source_segment_ids
    assert block.segment_ids == plan.source_segment_ids
    assert block.context_segment_ids == ()
    assert block.object_ids == ("object-000000", "object-000001")
    assert block.bbox == Box(0, 7, 100, 83)
    assert plan.adjacent_algebra == ()
    _assert_plan_identities(plan)


def test_padding_is_clamped_to_canvas_and_counted_in_pixel_budget() -> None:
    segment = _segment(0, Box(0, 0, 5, 5))
    plan = _plan(
        (segment,),
        ((0,),),
        aligned_size=(10, 10),
        config=_config(padding=8, max_block_pixels=100),
    )

    assert plan.blocks[0].bbox == Box(0, 0, 10, 10)
    with pytest.raises(BlockPlanningLimitError, match="fit|pixel|block"):
        _plan(
            (segment,),
            ((0,),),
            aligned_size=(10, 10),
            config=_config(padding=8, max_block_pixels=99),
        )


def test_objects_that_fit_are_not_split_and_adjacent_blocks_really_overlap() -> None:
    segments = tuple(_segment(index) for index in range(4))
    plan = _plan(
        segments,
        ((0, 1), (2, 3)),
        config=_config(max_block_pixels=5_000),
    )

    assert tuple(block.core_segment_ids for block in plan.blocks) == (
        (segments[0].segment_id, segments[1].segment_id),
        (segments[2].segment_id, segments[3].segment_id),
    )
    assert plan.blocks[0].object_ids == ("object-000000",)
    assert plan.blocks[1].object_ids == ("object-000001",)
    assert plan.blocks[1].context_segment_ids == (segments[1].segment_id,)
    assert plan.blocks[1].segment_ids == tuple(segment.segment_id for segment in segments[1:])
    assert all(block.bbox.area <= 5_000 for block in plan.blocks)
    _assert_plan_identities(plan)


def test_left_context_overlaps_only_adjacent_blocks() -> None:
    segments = tuple(_segment(index) for index in range(6))
    plan = _plan(
        segments,
        tuple((index,) for index in range(6)),
        aligned_size=(100, 140),
        config=_config(
            max_core_segments=2,
            max_block_pixels=100_000,
            context_segments=1,
        ),
    )

    assert tuple(block.core_segment_ids for block in plan.blocks) == (
        tuple(item.segment_id for item in segments[0:2]),
        tuple(item.segment_id for item in segments[2:4]),
        tuple(item.segment_id for item in segments[4:6]),
    )
    assert plan.blocks[0].context_segment_ids == ()
    assert plan.blocks[1].context_segment_ids == (segments[1].segment_id,)
    assert plan.blocks[2].context_segment_ids == (segments[3].segment_id,)
    assert set(plan.blocks[0].segment_ids).isdisjoint(plan.blocks[2].segment_ids)
    _assert_plan_identities(plan)


def test_same_occupied_row_fragments_are_an_indivisible_context_unit() -> None:
    segments = (
        replace(
            _segment(0, Box(0, 0, 10, 10)),
            row_index=0,
            order_key=(0, 0),
        ),
        replace(
            _segment(1, Box(20, 0, 30, 10)),
            row_index=0,
            order_key=(0, 20),
        ),
        replace(
            _segment(2, Box(40, 0, 50, 10)),
            row_index=0,
            order_key=(0, 40),
        ),
        replace(
            _segment(3, Box(0, 20, 10, 30)),
            row_index=1,
            order_key=(20, 0),
        ),
    )
    plan = _plan(
        segments,
        ((0,), (1,), (2,), (3,)),
        aligned_size=(60, 40),
        config=_config(
            max_core_segments=3,
            max_block_pixels=100_000,
            context_segments=1,
        ),
    )

    assert tuple(block.core_segment_ids for block in plan.blocks) == (
        tuple(item.segment_id for item in segments[:3]),
        (segments[3].segment_id,),
    )
    assert plan.blocks[1].context_segment_ids == tuple(item.segment_id for item in segments[:3])
    _assert_plan_identities(plan)


def test_vertically_overlapping_row_indexes_coalesce_into_one_physical_row() -> None:
    segments = (
        replace(
            _segment(0, Box(0, 0, 10, 10)),
            row_index=0,
            order_key=(0, 0),
        ),
        replace(
            _segment(1, Box(20, 5, 30, 10)),
            row_index=1,
            order_key=(5, 20),
        ),
        replace(
            _segment(2, Box(0, 20, 10, 30)),
            row_index=2,
            order_key=(20, 0),
        ),
    )
    plan = _plan(
        segments,
        ((0,), (1,), (2,)),
        aligned_size=(40, 40),
        config=_config(
            max_core_segments=2,
            max_block_pixels=100_000,
            context_segments=1,
        ),
    )

    assert tuple(block.core_segment_ids for block in plan.blocks) == (
        tuple(item.segment_id for item in segments[:2]),
        (segments[2].segment_id,),
    )
    assert plan.blocks[1].context_segment_ids == tuple(item.segment_id for item in segments[:2])
    _assert_plan_identities(plan)


def test_physical_row_overlap_closure_is_transitive_but_not_edge_touching() -> None:
    overlapping = (
        replace(_segment(0, Box(0, 0, 10, 4)), row_index=0, order_key=(0, 0)),
        replace(_segment(1, Box(12, 3, 22, 7)), row_index=1, order_key=(3, 12)),
        replace(_segment(2, Box(24, 6, 34, 10)), row_index=2, order_key=(6, 24)),
        replace(_segment(3, Box(0, 20, 10, 30)), row_index=3, order_key=(20, 0)),
    )
    transitive = _plan(
        overlapping,
        ((0,), (1,), (2,), (3,)),
        aligned_size=(40, 40),
        config=_config(
            max_core_segments=3,
            max_block_pixels=100_000,
            context_segments=1,
        ),
    )
    assert transitive.blocks[0].core_segment_ids == tuple(item.segment_id for item in overlapping[:3])
    assert transitive.blocks[1].context_segment_ids == tuple(item.segment_id for item in overlapping[:3])

    touching = (
        replace(_segment(0, Box(0, 0, 10, 5)), row_index=0, order_key=(0, 0)),
        replace(_segment(1, Box(0, 5, 10, 10)), row_index=1, order_key=(5, 0)),
    )
    separate = _plan(
        touching,
        ((0,), (1,)),
        aligned_size=(20, 20),
        config=_config(
            max_core_segments=1,
            max_block_pixels=100_000,
            context_segments=1,
        ),
    )
    assert tuple(block.core_segment_ids for block in separate.blocks) == (
        (touching[0].segment_id,),
        (touching[1].segment_id,),
    )
    _assert_plan_identities(transitive)
    _assert_plan_identities(separate)


def test_oversized_object_split_is_deterministic_under_input_reordering() -> None:
    segments = tuple(_segment(index) for index in range(5))
    objects_result = _objects_result(segments, ((0, 1, 2, 3, 4),))
    planner = OverlappingBlockPlanner(_config(max_core_segments=3, max_block_pixels=5_000))

    first = planner.plan(
        aligned_size=(100, 120),
        segments=segments,
        objects_result=objects_result,
    )
    second = planner.plan(
        aligned_size=(100, 120),
        segments=tuple(reversed(segments)),
        objects_result=objects_result,
    )

    assert first == second
    assert len(first.blocks) >= 2
    assert all(block.object_ids == ("object-000000",) for block in first.blocks)
    assert all(block.bbox.area <= 5_000 for block in first.blocks)
    _assert_plan_identities(first)


def test_planning_is_kind_agnostic_for_unknown_stage6_objects() -> None:
    segments = tuple(_segment(index) for index in range(4))
    baseline = _objects_result(segments, ((0, 1), (2, 3)))
    planner = OverlappingBlockPlanner(_config(max_block_pixels=5_000))
    plans = []
    for kind in ObjectKind:
        objects = tuple(replace(item, kind=kind) for item in baseline.objects)
        plans.append(
            planner.plan(
                aligned_size=(100, 120),
                segments=segments,
                objects_result=replace(baseline, objects=objects),
            )
        )

    assert plans and all(plan == plans[0] for plan in plans)
    _assert_plan_identities(plans[0])


def test_interleaved_object_ownership_closes_without_reordering_core_partition() -> None:
    segments = tuple(_segment(index) for index in range(6))
    result = _objects_result(
        segments,
        (
            (0, 1, 4, 5),
            (2,),
            (3,),
        ),
    )
    planner = OverlappingBlockPlanner(
        _config(
            max_core_segments=6,
            max_block_pixels=100_000,
        )
    )

    plan = planner.plan(
        aligned_size=(100, 120),
        segments=tuple(reversed(segments)),
        objects_result=result,
    )

    expected = tuple(segment.segment_id for segment in segments)
    assert plan.source_segment_ids == expected
    assert tuple(
        segment_id
        for block in plan.blocks
        for segment_id in block.core_segment_ids
    ) == expected
    assert len(plan.blocks) == 1
    assert plan.blocks[0].core_segment_ids == expected
    assert plan.blocks[0].object_ids == (
        "object-000000",
        "object-000001",
        "object-000002",
    )
    _assert_plan_identities(plan)


def test_oversized_interleaved_closure_splits_only_between_physical_rows() -> None:
    values = []
    for row in range(5):
        top = 10 + row * 20
        for column, (left, right) in enumerate(((0, 40), (50, 90))):
            index = len(values)
            values.append(
                replace(
                    _segment(index, Box(left, top, right, top + 10)),
                    row_index=index,
                    order_key=(top, left),
                )
            )
    segments = tuple(values)
    result = _objects_result(
        segments,
        (
            (0, 1, 4, 5, 6, 7, 8, 9),
            (2,),
            (3,),
        ),
    )

    plan = OverlappingBlockPlanner(
        _config(
            max_core_segments=4,
            max_block_pixels=100_000,
            context_segments=1,
        )
    ).plan(
        aligned_size=(100, 120),
        segments=tuple(reversed(segments)),
        objects_result=result,
    )

    expected_ids = tuple(segment.segment_id for segment in segments)
    assert tuple(block.core_segment_ids for block in plan.blocks) == (
        expected_ids[0:4],
        expected_ids[4:8],
        expected_ids[8:10],
    )
    assert tuple(block.object_ids for block in plan.blocks) == (
        ("object-000000", "object-000001", "object-000002"),
        ("object-000000",),
        ("object-000000",),
    )
    assert plan.blocks[1].context_segment_ids == expected_ids[2:4]
    assert plan.blocks[2].context_segment_ids == expected_ids[6:8]
    core_owner = {
        segment_id: block.block_id
        for block in plan.blocks
        for segment_id in block.core_segment_ids
    }
    assert all(
        core_owner[expected_ids[index]] == core_owner[expected_ids[index + 1]]
        for index in range(0, len(expected_ids), 2)
    )
    _assert_plan_identities(plan)


def test_randomized_interleaved_ownership_never_reorders_or_loses_segments() -> None:
    rng = np.random.default_rng(2026071908)
    for case_index in range(25):
        segments_list = []
        physical_rows = []
        top = 2
        for _ in range(int(rng.integers(6, 13))):
            row_ids = []
            fragments = int(rng.integers(1, 4))
            width = 180 // fragments
            for column in range(fragments):
                index = len(segments_list)
                left = column * width
                bbox = Box(left, top, min(180, left + width - 2), top + 8)
                segment = replace(
                    _segment(index, bbox),
                    row_index=index,
                    order_key=(bbox.top, bbox.left),
                )
                segments_list.append(segment)
                row_ids.append(segment.segment_id)
            physical_rows.append(tuple(row_ids))
            top += 14
        segments = tuple(segments_list)
        source_ids = tuple(item.segment_id for item in segments)
        singleton_count = min(
            int(rng.integers(1, 5)),
            max(1, len(segments) - 2),
        )
        singleton_indexes = tuple(
            sorted(
                int(value)
                for value in rng.choice(
                    np.arange(1, len(segments) - 1),
                    size=singleton_count,
                    replace=False,
                )
            )
        )
        main_indexes = tuple(
            index for index in range(len(segments)) if index not in singleton_indexes
        )
        groups = (main_indexes,) + tuple((index,) for index in singleton_indexes)
        aligned_size = (180, top + 2)
        result = _objects_result(
            segments,
            groups,
            aligned_size=aligned_size,
        )
        max_core_segments = max(3, max(len(row) for row in physical_rows))
        planner = OverlappingBlockPlanner(
            _config(
                max_core_segments=max_core_segments,
                max_block_pixels=aligned_size[0] * aligned_size[1],
                context_segments=2,
            )
        )

        plan = planner.plan(
            aligned_size=aligned_size,
            segments=tuple(reversed(segments)),
            objects_result=result,
        )
        repeated = planner.plan(
            aligned_size=aligned_size,
            segments=segments,
            objects_result=result,
        )

        assert plan == repeated, case_index
        assert tuple(
            segment_id
            for block in plan.blocks
            for segment_id in block.core_segment_ids
        ) == source_ids, case_index
        owner = {
            segment_id: item.object_id
            for item in result.objects
            for segment_id in item.segment_ids
        }
        for block in plan.blocks:
            expected_owners = tuple(
                dict.fromkeys(owner[segment_id] for segment_id in block.core_segment_ids)
            )
            assert block.object_ids == expected_owners, case_index
        core_block = {
            segment_id: block.block_id
            for block in plan.blocks
            for segment_id in block.core_segment_ids
        }
        assert all(
            len({core_block[segment_id] for segment_id in row}) == 1
            for row in physical_rows
        ), case_index
        _assert_plan_identities(plan)


def test_empty_plan_is_complete_and_contains_no_forged_work() -> None:
    result = ObjectReconstructionResult(
        aligned_size=(20, 10),
        source_segment_ids=(),
        objects=(),
        segment_ownership=(),
    )

    plan = OverlappingBlockPlanner(_config()).plan(
        aligned_size=(20, 10),
        segments=(),
        objects_result=result,
    )

    assert plan == BlockPlan(
        aligned_size=(20, 10),
        source_segment_ids=(),
        blocks=(),
        adjacent_algebra=(),
    )


def test_impossible_required_overlap_fails_closed() -> None:
    segments = (
        _segment(0, Box(0, 0, 10, 10)),
        _segment(1, Box(90, 90, 100, 100)),
    )

    with pytest.raises(BlockPlanningLimitError, match="overlap|context|pixel"):
        _plan(
            segments,
            ((0,), (1,)),
            aligned_size=(100, 100),
            config=_config(
                max_core_segments=1,
                max_block_pixels=1_000,
                context_segments=1,
            ),
        )


def test_planner_rejects_segment_and_object_scope_laundering() -> None:
    segments = tuple(_segment(index) for index in range(3))
    result = _objects_result(segments, ((0, 1, 2),))
    planner = OverlappingBlockPlanner(_config())

    with pytest.raises(BlockPlanningInvariantError, match="segment|source"):
        planner.plan(
            aligned_size=(100, 120),
            segments=segments[:-1],
            objects_result=result,
        )
    with pytest.raises(BlockPlanningInvariantError, match="duplicate|unique"):
        planner.plan(
            aligned_size=(100, 120),
            segments=(segments[0], segments[0], segments[1], segments[2]),
            objects_result=result,
        )
    with pytest.raises(BlockPlanningInvariantError, match="size|canvas|aligned"):
        planner.plan(
            aligned_size=(101, 120),
            segments=segments,
            objects_result=result,
        )


def test_planner_rejects_object_bbox_that_does_not_contain_its_segments() -> None:
    segments = tuple(_segment(index) for index in range(2))
    result = _objects_result(segments, ((0, 1),))
    forged_object = replace(result.objects[0], bbox=Box(0, 0, 1, 1))
    forged = replace(result, objects=(forged_object,))

    with pytest.raises(BlockPlanningInvariantError, match="bbox|object|contain"):
        OverlappingBlockPlanner(_config()).plan(
            aligned_size=(100, 120),
            segments=segments,
            objects_result=forged,
        )


def test_block_plan_rejects_noncanonical_algebra_and_non_full_width_bbox() -> None:
    segments = tuple(_segment(index) for index in range(4))
    plan = _plan(
        segments,
        ((0, 1), (2, 3)),
        config=_config(max_block_pixels=5_000),
    )
    algebra = plan.adjacent_algebra[0]
    forged_algebra = replace(
        algebra,
        union_segment_ids=tuple(reversed(algebra.union_segment_ids)),
    )

    with pytest.raises(ValueError, match="canonical|order|algebra"):
        replace(plan, adjacent_algebra=(forged_algebra,))

    tight = replace(
        plan.blocks[0],
        bbox=Box(
            1,
            plan.blocks[0].bbox.top,
            plan.aligned_size[0],
            plan.blocks[0].bbox.bottom,
        ),
    )
    with pytest.raises(ValueError, match="full|width|bbox"):
        replace(plan, blocks=(tight, *plan.blocks[1:]))


def test_spatial_2d_mode_partitions_cores_and_overlaps_rows_and_columns() -> None:
    segments = _grid_segments(3, 6)
    aligned_size = (80, 50)
    plan = _plan(
        segments,
        (tuple(range(len(segments))),),
        aligned_size=aligned_size,
        config=_config(
            mode=BlockPlanningMode.SPATIAL_2D,
            spatial_rows=2,
            spatial_columns=3,
            spatial_row_overlap=1,
            spatial_column_overlap=1,
            padding=1,
        ),
    )

    source_ids = tuple(item.segment_id for item in segments)
    assert plan.mode is BlockPlanningMode.SPATIAL_2D
    assert len(plan.blocks) == 6
    assert tuple(block.core_segment_ids for block in plan.blocks[:2]) == (
        source_ids[0:6],
        source_ids[6:18],
    )
    assert plan.blocks[0].segment_ids == (
        *source_ids[0:6],
        *source_ids[6:12],
    )
    assert plan.blocks[0].context_segment_ids == (
        *source_ids[6:12],
    )
    assert plan.blocks[0].bbox == Box(4, 4, 76, 31)
    assert plan.blocks[1].bbox == Box(4, 19, 76, 46)
    assert plan.blocks[-1].core_segment_ids == ()
    assert plan.blocks[-1].segment_ids == tuple(
        source_ids[row * 6 + column]
        for row in range(3)
        for column in (3, 4)
    )
    assert all(block.bbox.right - block.bbox.left < aligned_size[0] for block in plan.blocks)
    assert any(
        set(algebra.intersection_segment_ids) & set(source_ids[0:6])
        and set(algebra.intersection_segment_ids) & set(source_ids[6:12])
        for algebra in plan.adjacent_algebra
    )
    assert "mode=spatial-2d" in plan.diagnostics
    assert "matrix-primary-row-families=2" in plan.diagnostics
    assert (
        "matrix-column-sliding2-or-orthogonal-families=4"
        in plan.diagnostics
    )
    _assert_plan_identities(plan)


def test_spatial_2d_uses_orthogonal_or_xor_code_for_three_by_two_grid() -> None:
    """The minimal example requested by the Stage 5 OR/XOR contract.

    Source rows are ``[0 1] / [2 3] / [4 5]``.  Two overlapping row
    blocks plus the first-column probe give every segment a unique code.
    The second row block absorbs the trailing core carrier, so no duplicate
    or context-free fourth OCR crop remains.
    """

    segments = _grid_segments(3, 2)
    source_ids = tuple(item.segment_id for item in segments)
    plan = _plan(
        segments,
        (tuple(range(len(segments))),),
        aligned_size=(40, 50),
        config=_config(
            mode=BlockPlanningMode.SPATIAL_2D,
            spatial_rows=2,
            spatial_columns=3,
            spatial_row_overlap=1,
            spatial_column_overlap=1,
            padding=0,
        ),
    )

    assert tuple(block.segment_ids for block in plan.blocks) == (
        (source_ids[0], source_ids[1], source_ids[2], source_ids[3]),
        (source_ids[2], source_ids[3], source_ids[4], source_ids[5]),
        (source_ids[0], source_ids[2], source_ids[4]),
    )
    assert tuple(block.core_segment_ids for block in plan.blocks) == (
        (source_ids[0], source_ids[1]),
        (source_ids[2], source_ids[3], source_ids[4], source_ids[5]),
        (),
    )
    signatures = {
        segment_id: tuple(
            block_index
            for block_index, block in enumerate(plan.blocks)
            if segment_id in block.segment_ids
        )
        for segment_id in source_ids
    }
    assert len(set(signatures.values())) == 6
    assert tuple(
        (
            algebra.first_block_id,
            algebra.second_block_id,
            algebra.intersection_segment_ids,
            algebra.xor_segment_ids,
        )
        for algebra in plan.adjacent_algebra
    ) == (
        (
            "block-000000",
            "block-000001",
            (source_ids[2], source_ids[3]),
            (source_ids[0], source_ids[1], source_ids[4], source_ids[5]),
        ),
        (
            "block-000000",
            "block-000002",
            (source_ids[0], source_ids[2]),
            (source_ids[1], source_ids[3], source_ids[4]),
        ),
        (
            "block-000001",
            "block-000002",
            (source_ids[2], source_ids[4]),
            (source_ids[0], source_ids[3], source_ids[5]),
        ),
    )
    assert "orthogonal-signature-probes=1" in plan.diagnostics
    assert "membership-signatures=unique-between-units" in plan.diagnostics
    assert all(
        item.kind is MembershipUnitKind.SEGMENT
        for item in plan.membership_units
    )
    _assert_spatial_crop_closure(plan, segments)
    _assert_plan_identities(plan)

    with pytest.raises(ValueError, match="membership|signature"):
        replace(
            plan,
            blocks=plan.blocks[:2],
            adjacent_algebra=plan.adjacent_algebra[:1],
        )


@pytest.mark.parametrize("segment_count", range(1, 9))
def test_spatial_membership_signatures_fail_closed_for_one_to_eight_segments(
    segment_count: int,
) -> None:
    """No ambiguous signature may masquerade as a decoded segment.

    A one-row/two-column object has no non-singleton orthogonal discriminator,
    so its two members are deliberately one SUBBLOCK.  Larger one-row objects
    may still retain a leading inseparable pair, but every collision must be
    represented by exactly one explicit SUBBLOCK membership unit.
    """

    segments = _grid_segments(1, segment_count)
    aligned_size = (max(20, 10 + 12 * segment_count), 20)
    plan = _plan(
        segments,
        (tuple(range(segment_count)),),
        aligned_size=aligned_size,
        config=_config(
            mode=BlockPlanningMode.SPATIAL_2D,
            padding=0,
            max_blocks=100,
        ),
    )

    signature_groups: dict[tuple[str, ...], list[str]] = {}
    for segment_id in plan.source_segment_ids:
        signature = tuple(
            block.block_id
            for block in plan.blocks
            if segment_id in block.segment_ids
        )
        signature_groups.setdefault(signature, []).append(segment_id)
    expected = {
        signature: tuple(segment_ids)
        for signature, segment_ids in signature_groups.items()
    }
    actual = {
        unit.block_ids: unit.segment_ids for unit in plan.membership_units
    }

    assert actual == expected
    assert tuple(
        segment_id
        for unit in plan.membership_units
        for segment_id in unit.segment_ids
    ) == plan.source_segment_ids
    assert all(
        unit.kind
        is (
            MembershipUnitKind.SEGMENT
            if len(unit.segment_ids) == 1
            else MembershipUnitKind.SUBBLOCK
        )
        for unit in plan.membership_units
    )
    assert len(actual) == len(plan.membership_units)


def test_spatial_membership_closes_a_two_dimensional_enclosure() -> None:
    """A physically enclosed segment cannot remain an undeclared crop pixel."""

    segments = (
        replace(_segment(0, Box(5, 5, 15, 35)), row_index=0),
        replace(_segment(1, Box(35, 5, 45, 35)), row_index=0),
        replace(_segment(2, Box(20, 15, 30, 25)), row_index=0),
    )
    planner = OverlappingBlockPlanner(
        _config(mode=BlockPlanningMode.SPATIAL_2D, padding=0)
    )

    closed, checks = planner._spatial_membership_closure(
        {segments[0].segment_id, segments[1].segment_id},
        ordered_segments=segments,
        max_checks=100,
    )

    assert closed == {item.segment_id for item in segments}
    assert checks == 6


def test_sixteen_pixel_legacy_overlap_is_absorbed_not_left_as_contamination() -> None:
    """Model the 16px overlap that contaminated the legacy sliding crops."""

    segments = (
        replace(
            _segment(0, Box(5, 5, 25, 31)),
            row_index=0,
            order_key=(5, 5),
        ),
        replace(
            _segment(1, Box(15, 15, 35, 41)),
            row_index=1,
            order_key=(15, 15),
        ),
    )
    assert segments[0].bbox.intersection(segments[1].bbox) == Box(
        15, 15, 25, 31
    )
    plan = _plan(
        segments,
        ((0, 1),),
        aligned_size=(40, 46),
        config=_config(mode=BlockPlanningMode.SPATIAL_2D, padding=0),
    )

    assert plan.blocks
    for block in plan.blocks:
        intersected = {
            segment.segment_id
            for segment in segments
            if segment.bbox.intersection(block.bbox) is not None
        }
        assert intersected == set(block.segment_ids)
    assert plan.membership_units == (
        MembershipUnit(
            "membership-unit-000000",
            MembershipUnitKind.SUBBLOCK,
            tuple(item.segment_id for item in segments),
            tuple(item.block_id for item in plan.blocks),
            "scope-000000",
        ),
    )


def test_spatial_2d_one_by_two_uses_context_block_and_one_subblock() -> None:
    segments = _grid_segments(1, 2)
    source_ids = tuple(item.segment_id for item in segments)

    plan = _plan(
        segments,
        ((0, 1),),
        aligned_size=(40, 20),
        config=_config(mode=BlockPlanningMode.SPATIAL_2D),
    )

    assert tuple(item.segment_ids for item in plan.blocks) == (source_ids,)
    assert plan.blocks[0].core_segment_ids == source_ids
    assert plan.membership_units == (
        MembershipUnit(
            "membership-unit-000000",
            MembershipUnitKind.SUBBLOCK,
            source_ids,
            ("block-000000",),
            "scope-000000",
        ),
    )
    assert not any(
        not item.core_segment_ids and len(item.segment_ids) == 1
        for item in plan.blocks
    )


def test_spatial_2d_remote_flow_objects_never_gain_a_long_bridge() -> None:
    segments = (
        replace(_segment(0, Box(5, 5, 25, 15)), row_index=0),
        replace(_segment(1, Box(5, 900, 25, 910)), row_index=1),
    )
    aligned_size = (40, 1_000)
    plan = _plan(
        segments,
        ((0,), (1,)),
        aligned_size=aligned_size,
        config=_config(
            mode=BlockPlanningMode.SPATIAL_2D,
            max_scope_span_pixels=512,
        ),
    )

    assert tuple(item.scope_id for item in plan.blocks) == (
        "scope-000000",
        "scope-000001",
    )
    assert plan.adjacent_algebra == ()
    assert all(item.bbox.height < 100 for item in plan.blocks)


def test_spatial_2d_adjacent_objects_keep_exact_owner_local_scopes() -> None:
    segments = _grid_segments(1, 2)
    plan = _plan(
        segments,
        ((0,), (1,)),
        aligned_size=(40, 20),
        config=_config(mode=BlockPlanningMode.SPATIAL_2D, padding=0),
    )

    assert tuple(item.scope_id for item in plan.blocks) == (
        "scope-000000",
        "scope-000001",
    )
    assert tuple(item.object_ids for item in plan.blocks) == (
        ("object-000000",),
        ("object-000001",),
    )
    assert plan.adjacent_algebra == ()
    assert all(len(item.segment_ids) == 1 for item in plan.membership_units)


def test_spatial_interleaved_objects_keep_local_cores_without_global_monotonicity() -> None:
    segments = _grid_segments(2, 2)
    plan = _plan(
        segments,
        ((0, 2), (1, 3)),
        aligned_size=(40, 40),
        config=_config(mode=BlockPlanningMode.SPATIAL_2D, padding=0),
    )
    owner = {
        segment.segment_id: (
            "object-000000" if index in (0, 2) else "object-000001"
        )
        for index, segment in enumerate(segments)
    }
    core_ids = tuple(
        segment_id
        for block in plan.blocks
        for segment_id in block.core_segment_ids
    )

    assert set(core_ids) == set(plan.source_segment_ids)
    assert len(core_ids) == len(set(core_ids))
    assert tuple(block.scope_id for block in plan.blocks) == (
        "scope-000000",
        "scope-000001",
    )
    assert all(
        {owner[segment_id] for segment_id in block.segment_ids}
        == {block.object_ids[0]}
        for block in plan.blocks
    )
    _assert_plan_identities(plan)


def test_cross_object_bbox_overlap_is_deferred_to_literal_ownership_gate() -> None:
    segments = (
        replace(_segment(0, Box(5, 5, 25, 25)), row_index=0),
        replace(_segment(1, Box(15, 15, 35, 35)), row_index=1),
    )
    aligned_size = (40, 40)
    plan = _plan(
        segments,
        ((0,), (1,)),
        aligned_size=aligned_size,
        config=_config(mode=BlockPlanningMode.SPATIAL_2D, padding=0),
    )
    assert segments[0].bbox.intersection(segments[1].bbox) == Box(
        15, 15, 25, 25
    )
    assert tuple(block.segment_ids for block in plan.blocks) == (
        (segments[0].segment_id,),
        (segments[1].segment_id,),
    )

    ownership = np.full((40, 40), -1, dtype=np.int32)
    # Each label reaches all four bounds of its declared bbox, while the
    # geometric overlap itself contains no ownership pixel.
    ownership[5, 5:25] = 0
    ownership[24, 5:15] = 0
    ownership[5:25, 5] = 0
    ownership[15:25, 24] = -1
    ownership[34, 15:35] = 1
    ownership[15, 25:35] = 1
    ownership[15:35, 34] = 1
    ownership[25:35, 15] = 1
    ownership.flags.writeable = False
    page = CropInput(
        "overlap-page",
        _png_bytes(np.full((40, 40, 3), 255, dtype=np.uint8), mode="RGB"),
    )

    crops = BlockCropper().crop(
        page,
        aligned_size=aligned_size,
        plan=plan,
        ownership=ownership,
        ownership_segment_ids=tuple(item.segment_id for item in segments),
    )
    assert len(crops) == 2

    contaminated = np.array(ownership, copy=True)
    contaminated[20, 20] = 1
    isolated = BlockCropper().crop(
        page,
        aligned_size=aligned_size,
        plan=plan,
        ownership=contaminated,
        ownership_segment_ids=tuple(item.segment_id for item in segments),
    )
    assert isolated[0].masked_segment_ids == (segments[1].segment_id,)
    assert isolated[0].isolation_mask_png is not None


def test_spatial_plan_rejects_dense_component_plus_same_scope_artifact() -> None:
    source_ids = tuple(f"segment-{index:06d}" for index in range(4))
    blocks = (
        RecognitionBlock(
            "block-000000",
            Box(0, 0, 20, 10),
            source_ids[:2],
            source_ids[:2],
            (),
            ("object-000000",),
            "scope-000000",
        ),
        RecognitionBlock(
            "block-000001",
            Box(10, 0, 30, 10),
            (source_ids[2],),
            (source_ids[1], source_ids[2]),
            (source_ids[1],),
            ("object-000000",),
            "scope-000000",
        ),
        RecognitionBlock(
            "block-000002",
            Box(80, 80, 90, 90),
            (source_ids[3],),
            (source_ids[3],),
            (),
            ("object-000000",),
            "scope-000000",
        ),
    )
    algebra = BlockSetAlgebra(
        "block-000000",
        "block-000001",
        (source_ids[1],),
        source_ids[:3],
        (source_ids[0], source_ids[2]),
        (source_ids[0],),
        (source_ids[2],),
    )
    units = (
        MembershipUnit("membership-unit-000000", MembershipUnitKind.SEGMENT, (source_ids[0],), ("block-000000",), "scope-000000"),
        MembershipUnit("membership-unit-000001", MembershipUnitKind.SEGMENT, (source_ids[1],), ("block-000000", "block-000001"), "scope-000000"),
        MembershipUnit("membership-unit-000002", MembershipUnitKind.SEGMENT, (source_ids[2],), ("block-000001",), "scope-000000"),
        MembershipUnit("membership-unit-000003", MembershipUnitKind.SEGMENT, (source_ids[3],), ("block-000002",), "scope-000000"),
    )

    with pytest.raises(ValueError, match="disconnected block artifact"):
        BlockPlan(
            aligned_size=(100, 100),
            source_segment_ids=source_ids,
            blocks=blocks,
            adjacent_algebra=(algebra,),
            mode=BlockPlanningMode.SPATIAL_2D,
            membership_units=units,
            matrix_sha256="0" * 64,
        )

def test_spatial_2d_ragged_sparse_matrix_uses_row_and_prefix_codes() -> None:
    positions = (
        (0, 0),
        (0, 2),
        (1, 0),
        (1, 1),
        (1, 2),
        (2, 1),
        (2, 2),
    )
    segments = tuple(
        replace(
            _segment(
                index,
                Box(
                    5 + column * 12,
                    5 + row * 15,
                    15 + column * 12,
                    15 + row * 15,
                ),
            ),
            row_index=row,
            order_key=(5 + row * 15, 5 + column * 12),
        )
        for index, (row, column) in enumerate(positions)
    )
    plan = _plan(
        segments,
        (tuple(range(len(segments))),),
        aligned_size=(50, 50),
        config=_config(
            mode=BlockPlanningMode.SPATIAL_2D,
            max_blocks=20,
            max_overlap_pairs=100,
        ),
    )
    assert len(plan.blocks) == 4
    assert "matrix-logical-row-bands=3" in plan.diagnostics
    assert "matrix-logical-column-bands=3" in plan.diagnostics
    signatures = {
        segment_id: tuple(
            index
            for index, block in enumerate(plan.blocks)
            if segment_id in block.segment_ids
        )
        for segment_id in plan.source_segment_ids
    }
    assert len(set(signatures.values())) == len(segments)
    _assert_spatial_crop_closure(plan, segments)
    _assert_plan_identities(plan)


def test_spatial_2d_keeps_disjoint_table_scopes_as_real_components() -> None:
    segments = (
        replace(_segment(0, Box(5, 5, 25, 15)), row_index=0, order_key=(5, 5)),
        replace(_segment(1, Box(5, 35, 25, 45)), row_index=1, order_key=(35, 5)),
    )
    result = _objects_result(
        segments,
        ((0,), (1,)),
        aligned_size=(40, 60),
    )
    result = replace(
        result,
        objects=tuple(replace(item, kind=ObjectKind.TABLE) for item in result.objects),
    )
    plan = OverlappingBlockPlanner(
        _config(mode=BlockPlanningMode.SPATIAL_2D)
    ).plan(
        aligned_size=(40, 60),
        segments=segments,
        objects_result=result,
        matrix=_matrix_for_segments(segments, aligned_size=(40, 60)),
    )

    assert tuple(block.segment_ids for block in plan.blocks) == (
        (segments[0].segment_id,),
        (segments[1].segment_id,),
    )
    assert plan.adjacent_algebra == ()
    assert "matrix-planning-scopes=2" in plan.diagnostics
    assert "overlap-components=2" in plan.diagnostics
    _assert_plan_identities(plan)


def test_object_local_table_uses_one_block_per_segment() -> None:
    segments = _grid_segments(2, 3)
    result = _objects_result(
        segments,
        (tuple(range(len(segments))),),
        aligned_size=(50, 40),
    )
    result = replace(
        result,
        objects=(replace(result.objects[0], kind=ObjectKind.TABLE),),
    )

    plan = OverlappingBlockPlanner(
        _config(
            mode=BlockPlanningMode.SPATIAL_2D,
            object_local=True,
            padding=0,
        )
    ).plan(
        aligned_size=(50, 40),
        segments=segments,
        objects_result=result,
        matrix=_matrix_for_segments(segments, aligned_size=(50, 40)),
    )

    assert tuple(block.segment_ids for block in plan.blocks) == tuple(
        (segment.segment_id,) for segment in segments
    )
    assert tuple(block.core_segment_ids for block in plan.blocks) == tuple(
        (segment.segment_id,) for segment in segments
    )
    assert all(block.context_segment_ids == () for block in plan.blocks)
    assert plan.adjacent_algebra == ()
    assert all(
        unit.kind is MembershipUnitKind.SEGMENT
        for unit in plan.membership_units
    )
    assert "table-blocks=one-block-per-segment" in plan.diagnostics
    _assert_spatial_crop_closure(plan, segments)
    _assert_plan_identities(plan)


def test_object_local_paragraph_uses_one_block_for_whole_object() -> None:
    segments = tuple(_segment(index) for index in range(4))
    result = _objects_result(
        segments,
        (tuple(range(len(segments))),),
    )

    plan = OverlappingBlockPlanner(
        _config(
            mode=BlockPlanningMode.SPATIAL_2D,
            object_local=True,
            padding=0,
        )
    ).plan(
        aligned_size=(100, 120),
        segments=segments,
        objects_result=result,
        matrix=_matrix_for_segments(segments, aligned_size=(100, 120)),
    )

    assert len(plan.blocks) == 1
    assert plan.blocks[0].segment_ids == tuple(
        segment.segment_id for segment in segments
    )
    assert plan.blocks[0].core_segment_ids == plan.blocks[0].segment_ids
    assert plan.blocks[0].context_segment_ids == ()
    assert plan.blocks[0].bbox == Box.union(
        segment.bbox for segment in segments
    )
    assert plan.membership_units == (
        MembershipUnit(
            "membership-unit-000000",
            MembershipUnitKind.SUBBLOCK,
            tuple(segment.segment_id for segment in segments),
            ("block-000000",),
            "scope-000000",
        ),
    )
    assert "flow-blocks=one-block-per-object" in plan.diagnostics
    _assert_plan_identities(plan)


def test_adaptive_table_uses_unique_non_cartesian_dyadic_masks() -> None:
    segments = _grid_segments(6, 5)
    result = _objects_result(
        segments,
        (tuple(range(len(segments))),),
        aligned_size=(70, 100),
    )
    result = replace(
        result,
        objects=(replace(result.objects[0], kind=ObjectKind.TABLE),),
    )

    plan = OverlappingBlockPlanner(
        _config(
            mode=BlockPlanningMode.SPATIAL_2D,
            adaptive_table_windows=True,
            padding=0,
        )
    ).plan(
        aligned_size=(70, 100),
        segments=segments,
        objects_result=result,
        matrix=_matrix_for_segments(segments, aligned_size=(70, 100)),
    )

    assert len(plan.blocks) <= 8
    assert plan.adjacent_algebra
    assert {
        item.matrix_window_kind for item in plan.blocks
    } == {"dyadic-mask"}
    assert all(len(item.segment_ids) >= 2 for item in plan.blocks)
    assert all(
        item.matrix_segment_shape is not None
        and max(item.matrix_segment_shape) <= 16
        for item in plan.blocks
    )
    assert (
        "matrix-table-window-mode=dyadic-axis-binary-code"
        in plan.diagnostics
    )
    assert (
        "matrix-table-dyadic-codes="
        in next(
            item
            for item in plan.diagnostics
            if item.startswith("matrix-table-dyadic-codes=")
        )
    )
    assert "matrix-table-generated-candidates=8" in plan.diagnostics
    assert "matrix-table-selected-candidates=7" in plan.diagnostics
    assert (
        "matrix-table-algebra=arbitrary-segment-set-and-xor"
        in plan.diagnostics
    )
    signatures = {
        segment_id: tuple(
            index
            for index, block in enumerate(plan.blocks)
            if segment_id in block.segment_ids
        )
        for segment_id in plan.source_segment_ids
    }
    assert all(signatures.values())
    assert len(set(signatures.values())) == len(signatures)
    _assert_plan_identities(plan)


def test_adaptive_table_window_shape_is_logarithmic_not_occupancy_scaled() -> None:
    window = OverlappingBlockPlanner._adaptive_table_window_shape(
        row_count=14,
        column_count=10,
    )

    assert window == (4, 3)
    assert window[1] < window[0]
    assert window[1] > window[0] / 2


def test_adaptive_table_codes_merged_and_regular_cells_as_distinct_units() -> None:
    top = _grid_segments(1, 3, column_pitch=10)
    merged = replace(
        _segment(3, Box(5, 20, 35, 30)),
        row_index=1,
        order_key=(20, 5),
    )
    bottom = tuple(
        replace(
            item,
            segment_id=f"segment-{index + 4:06d}",
            bbox=Box(
                item.bbox.left,
                item.bbox.top + 30,
                item.bbox.right,
                item.bbox.bottom + 30,
            ),
            source_bbox=Box(
                item.source_bbox.left,
                item.source_bbox.top + 30,
                item.source_bbox.right,
                item.source_bbox.bottom + 30,
            ),
            row_index=2,
            order_key=(item.order_key[0] + 30, item.order_key[1]),
            component_ids=(index + 4,),
        )
        for index, item in enumerate(top)
    )
    segments = (*top, merged, *bottom)
    aligned_size = (40, 55)
    matrix = _matrix_for_segments(segments, aligned_size=aligned_size)
    result = _objects_result(
        segments,
        (tuple(range(len(segments))),),
        aligned_size=aligned_size,
    )
    result = replace(
        result,
        objects=(replace(result.objects[0], kind=ObjectKind.TABLE),),
    )

    plan = OverlappingBlockPlanner(
        _config(
            mode=BlockPlanningMode.SPATIAL_2D,
            adaptive_table_windows=True,
            max_blocks=100,
            padding=0,
        )
    ).plan(
        aligned_size=aligned_size,
        segments=segments,
        objects_result=result,
        matrix=matrix,
    )

    signatures = {
        segment_id: tuple(
            index
            for index, block in enumerate(plan.blocks)
            if segment_id in block.segment_ids
        )
        for segment_id in plan.source_segment_ids
    }
    assert all(signatures.values())
    assert len(set(signatures.values())) == len(signatures)
    assert all(len(block.segment_ids) >= 2 for block in plan.blocks)
    assert {block.matrix_window_kind for block in plan.blocks} == {
        "dyadic-mask"
    }
    _assert_plan_identities(plan)


def test_adaptive_table_rejects_an_isolated_one_by_one_ocr_block() -> None:
    segments = _grid_segments(1, 1)
    aligned_size = (20, 20)
    result = _objects_result(
        segments,
        ((0,),),
        aligned_size=aligned_size,
    )
    result = replace(
        result,
        objects=(replace(result.objects[0], kind=ObjectKind.TABLE),),
    )

    with pytest.raises(
        BlockPlanningInvariantError,
        match="isolated 1x1 table segment has no OCR context",
    ):
        OverlappingBlockPlanner(
            _config(
                mode=BlockPlanningMode.SPATIAL_2D,
                adaptive_table_windows=True,
                padding=0,
            )
        ).plan(
            aligned_size=aligned_size,
            segments=segments,
            objects_result=result,
            matrix=_matrix_for_segments(
                segments,
                aligned_size=aligned_size,
            ),
        )


def test_adaptive_table_masks_keep_every_cell_in_multi_segment_context() -> None:
    top = replace(
        _segment(0, Box(5, 5, 15, 15)),
        row_index=0,
        order_key=(5, 5),
    )
    merged = replace(
        _segment(1, Box(5, 20, 25, 30)),
        row_index=1,
        order_key=(20, 5),
    )
    bottom = (
        replace(
            _segment(2, Box(5, 35, 15, 45)),
            row_index=2,
            order_key=(35, 5),
        ),
        replace(
            _segment(3, Box(15, 35, 25, 45)),
            row_index=2,
            order_key=(35, 15),
        ),
    )
    segments = (top, merged, *bottom)
    aligned_size = (30, 50)
    result = _objects_result(
        segments,
        (tuple(range(len(segments))),),
        aligned_size=aligned_size,
    )
    result = replace(
        result,
        objects=(replace(result.objects[0], kind=ObjectKind.TABLE),),
    )

    plan = OverlappingBlockPlanner(
        _config(
            mode=BlockPlanningMode.SPATIAL_2D,
            adaptive_table_windows=True,
            max_blocks=100,
            padding=0,
        )
    ).plan(
        aligned_size=aligned_size,
        segments=segments,
        objects_result=result,
        matrix=_matrix_for_segments(segments, aligned_size=aligned_size),
    )

    assert min(len(block.segment_ids) for block in plan.blocks) >= 2
    signatures = {
        segment_id: tuple(
            index
            for index, block in enumerate(plan.blocks)
            if segment_id in block.segment_ids
        )
        for segment_id in plan.source_segment_ids
    }
    assert all(signatures.values())
    assert len(set(signatures.values())) == len(signatures)
    _assert_plan_identities(plan)


def test_adaptive_flow_object_keeps_whole_paragraph_context() -> None:
    segments = tuple(_segment(index) for index in range(4))
    result = _objects_result(segments, (tuple(range(len(segments))),))

    plan = OverlappingBlockPlanner(
        _config(
            mode=BlockPlanningMode.SPATIAL_2D,
            adaptive_table_windows=True,
            padding=0,
        )
    ).plan(
        aligned_size=(100, 120),
        segments=segments,
        objects_result=result,
        matrix=_matrix_for_segments(segments, aligned_size=(100, 120)),
    )

    assert len(plan.blocks) == 1
    assert plan.blocks[0].segment_ids == tuple(
        item.segment_id for item in segments
    )
    assert "matrix-table-window-shapes=none" in plan.diagnostics
    _assert_plan_identities(plan)


def test_spatial_2d_matrix_plan_is_deterministic_under_segment_reordering() -> None:
    segments = _grid_segments(4, 3)
    result = _objects_result(
        segments,
        (tuple(range(len(segments))),),
        aligned_size=(50, 70),
    )
    planner = OverlappingBlockPlanner(
        _config(
            mode=BlockPlanningMode.SPATIAL_2D,
            spatial_rows=2,
            spatial_columns=3,
            spatial_row_overlap=1,
            spatial_column_overlap=1,
        )
    )

    first = planner.plan(
        aligned_size=(50, 70),
        segments=segments,
        objects_result=result,
        matrix=_matrix_for_segments(segments, aligned_size=(50, 70)),
    )
    second = planner.plan(
        aligned_size=(50, 70),
        segments=tuple(reversed(segments)),
        objects_result=result,
        matrix=_matrix_for_segments(segments, aligned_size=(50, 70)),
    )

    assert first == second
    assert len(first.blocks) == 5
    assert "matrix-primary-row-families=3" in first.diagnostics
    assert (
        "matrix-column-sliding2-or-orthogonal-families=2"
        in first.diagnostics
    )
    _assert_plan_identities(first)


def test_spatial_2d_merged_header_splits_local_column_probe_region() -> None:
    header = replace(
        _segment(0, Box(5, 5, 27, 15)),
        row_index=0,
        order_key=(5, 5),
    )
    body = tuple(
        replace(item, segment_id=f"segment-{index + 1:06d}", component_ids=(index + 1,))
        for index, item in enumerate(
            _grid_segments(3, 2, column_pitch=12, row_pitch=15)
        )
    )
    body = tuple(
        replace(
            item,
            bbox=Box(item.bbox.left, item.bbox.top + 15, item.bbox.right, item.bbox.bottom + 15),
            source_bbox=Box(item.bbox.left, item.bbox.top + 15, item.bbox.right, item.bbox.bottom + 15),
            row_index=item.row_index + 1,
            order_key=(item.order_key[0] + 15, item.order_key[1]),
        )
        for item in body
    )
    segments = (header, *body)
    aligned_size = (40, 70)
    matrix = _matrix_for_segments(segments, aligned_size=aligned_size)
    gap_column = next(
        item.index for item in matrix.columns if item.start == 15 and item.end == 17
    )
    gap_rows = tuple(
        item.index
        for item in matrix.rows
        if item.start in (15, 30, 45) and item.end - item.start == 5
    )
    matrix = replace(
        matrix,
        horizontal_rule_rows=gap_rows,
        vertical_rule_columns=(gap_column,),
    )
    result = _objects_result(
        segments,
        (tuple(range(len(segments))),),
        aligned_size=aligned_size,
    )
    result = replace(
        result,
        objects=(replace(result.objects[0], kind=ObjectKind.TABLE),),
    )
    plan = OverlappingBlockPlanner(
        _config(
            mode=BlockPlanningMode.SPATIAL_2D,
            max_blocks=100,
            max_block_segments=100,
            max_overlap_pairs=1_000,
            max_pair_memberships=100_000,
        )
    ).plan(
        aligned_size=aligned_size,
        segments=segments,
        objects_result=result,
        matrix=matrix,
    )

    column_probe = next(
        block
        for block in plan.blocks
        if not block.core_segment_ids and header.segment_id not in block.segment_ids
    )
    assert column_probe.segment_ids == tuple(body[index].segment_id for index in (0, 2, 4))
    assert column_probe.bbox.top >= body[0].bbox.top
    _assert_spatial_crop_closure(plan, segments)
    _assert_plan_identities(plan)


def test_spatial_2d_merged_row_without_safe_cut_preserves_subblocks() -> None:
    regular = _grid_segments(1, 10)
    merged = replace(
        _segment(10, Box(5, 20, 123, 30)),
        row_index=1,
        order_key=(20, 5),
    )
    segments = (*regular, merged)
    plan = _plan(
        segments,
        (tuple(range(len(segments))),),
        aligned_size=(130, 40),
        config=_config(
            mode=BlockPlanningMode.SPATIAL_2D,
            max_core_segments=4,
            max_block_segments=100,
            max_overlap_pairs=1_000,
            max_pair_memberships=100_000,
        ),
    )

    assert any(
        item.kind is MembershipUnitKind.SUBBLOCK
        for item in plan.membership_units
    )
    assert all(
        block.core_segment_ids or len(block.segment_ids) != 1
        for block in plan.blocks
    )
    assert "singleton-signature-probes=0" in plan.diagnostics


def test_spatial_2d_signature_candidate_work_budget_fails_closed() -> None:
    segments = _grid_segments(3, 6)

    with pytest.raises(BlockPlanningLimitError, match="candidate checks|limit"):
        _plan(
            segments,
            (tuple(range(len(segments))),),
            aligned_size=(80, 50),
            config=_config(
                mode=BlockPlanningMode.SPATIAL_2D,
                spatial_rows=2,
                spatial_columns=3,
                spatial_row_overlap=1,
                spatial_column_overlap=1,
                max_signature_candidate_checks=1,
            ),
        )


def test_spatial_2d_rectangular_closure_obeys_member_budget() -> None:
    regular = _grid_segments(1, 10)
    merged = replace(
        _segment(10, Box(5, 20, 123, 30)),
        row_index=1,
        order_key=(20, 5),
    )
    segments = (*regular, merged)

    with pytest.raises(BlockPlanningLimitError, match="membership|limit"):
        _plan(
            segments,
            (tuple(range(len(segments))),),
            aligned_size=(130, 40),
            config=_config(
                mode=BlockPlanningMode.SPATIAL_2D,
                spatial_rows=2,
                spatial_columns=5,
                spatial_row_overlap=1,
                spatial_column_overlap=1,
                max_block_segments=6,
            ),
        )


def test_default_and_explicit_full_width_modes_are_identical() -> None:
    segments = tuple(_segment(index) for index in range(4))
    implicit = _plan(
        segments,
        ((0, 1), (2, 3)),
        config=_config(max_block_pixels=5_000),
    )
    explicit = _plan(
        segments,
        ((0, 1), (2, 3)),
        config=_config(
            mode=BlockPlanningMode.FULL_WIDTH,
            max_block_pixels=5_000,
        ),
    )

    assert implicit == explicit
    assert implicit.mode is BlockPlanningMode.FULL_WIDTH
    assert all(
        block.bbox.left == 0 and block.bbox.right == implicit.aligned_size[0]
        for block in implicit.blocks
    )


def test_deprecated_spatial_window_knobs_do_not_change_matrix_plan() -> None:
    segments = _grid_segments(3, 2)
    groups = (tuple(range(len(segments))),)
    first = _plan(
        segments,
        groups,
        aligned_size=(40, 50),
        config=_config(
            mode=BlockPlanningMode.SPATIAL_2D,
            spatial_rows=2,
            spatial_row_overlap=1,
            spatial_columns=3,
            spatial_column_overlap=1,
            max_singleton_probes=1,
        ),
    )
    second = _plan(
        segments,
        groups,
        aligned_size=(40, 50),
        config=_config(
            mode=BlockPlanningMode.SPATIAL_2D,
            spatial_rows=4,
            spatial_row_overlap=3,
            spatial_columns=7,
            spatial_column_overlap=1,
            max_singleton_probes=99,
        ),
    )

    assert first == second
    assert "deprecated-window-knobs=ignored" in first.diagnostics


@pytest.mark.parametrize(
    "config",
    (
        _config(
            mode=BlockPlanningMode.SPATIAL_2D,
            spatial_rows=2,
            spatial_columns=3,
            spatial_row_overlap=1,
            spatial_column_overlap=1,
            max_block_segments=5,
        ),
        _config(
            mode=BlockPlanningMode.SPATIAL_2D,
            spatial_rows=2,
            spatial_columns=3,
            spatial_row_overlap=1,
            spatial_column_overlap=1,
            max_segment_memberships=1,
        ),
        _config(
            mode=BlockPlanningMode.SPATIAL_2D,
            spatial_rows=2,
            spatial_columns=3,
            spatial_row_overlap=1,
            spatial_column_overlap=1,
            max_overlap_pairs=1,
        ),
        _config(
            mode=BlockPlanningMode.SPATIAL_2D,
            spatial_rows=2,
            spatial_columns=3,
            spatial_row_overlap=1,
            spatial_column_overlap=1,
            max_blocks=3,
        ),
    ),
)
def test_spatial_2d_budgets_fail_closed(config: BlockPlanningConfig) -> None:
    segments = _grid_segments(2, 4)

    with pytest.raises(BlockPlanningLimitError, match="limit|membership|block|pair"):
        _plan(
            segments,
            (tuple(range(len(segments))),),
            aligned_size=(60, 40),
            config=config,
        )


def test_matrix_candidate_scan_counts_long_empty_row_slots_before_range_work() -> None:
    segment = replace(
        _segment(0, Box(0, 0, 10, 64)),
        row_index=0,
        order_key=(0, 0),
    )
    matrix = SparseSegmentMatrix(
        rows=tuple(AxisInterval(index, index, index + 1) for index in range(64)),
        columns=(AxisInterval(0, 0, 10),),
        cells=(
            SparseCell(0, 0, segment.segment_id),
            SparseCell(63, 0, segment.segment_id),
        ),
        spans=(SegmentSpan(segment.segment_id, 0, 64, 0, 1),),
    )
    planner = OverlappingBlockPlanner(
        _config(
            mode=BlockPlanningMode.SPATIAL_2D,
            max_logical_row_checks=1_000,
            max_signature_candidate_checks=32,
        )
    )

    with pytest.raises(BlockPlanningLimitError, match="row-slot"):
        planner.plan(
            aligned_size=(10, 64),
            segments=(segment,),
            objects_result=_objects_result(
                (segment,),
                ((0,),),
                aligned_size=(10, 64),
            ),
            matrix=matrix,
        )


def test_spatial_2d_artifact_keeps_or_xor_manifest_with_local_bboxes(
    tmp_path: Path,
) -> None:
    segments = _grid_segments(2, 4)
    aligned_size = (60, 40)
    plan = _plan(
        segments,
        (tuple(range(len(segments))),),
        aligned_size=aligned_size,
        config=_config(
            mode=BlockPlanningMode.SPATIAL_2D,
            spatial_rows=2,
            spatial_columns=3,
            spatial_row_overlap=1,
            spatial_column_overlap=1,
        ),
    )
    matrix = _matrix_for_segments(segments, aligned_size=aligned_size)
    page = CropInput(
        "page",
        _png_bytes(np.zeros((40, 60, 3), dtype=np.uint8), mode="RGB"),
    )
    crops = BlockCropper().crop(
        page,
        aligned_size=aligned_size,
        plan=plan,
    )

    artifact = BlockArtifactWriter().write(
        tmp_path,
        run_id="spatial",
        page=page,
        plan=plan,
        crops=crops,
        matrix=matrix,
    )
    manifest = json.loads(
        (artifact / "05-blocks/manifest.json").read_text(encoding="utf-8")
    )
    matrix_record = json.loads(
        (artifact / "05-blocks/matrix.json").read_text(encoding="utf-8")
    )

    assert manifest["planning_mode"] == "spatial_2d"
    assert matrix_record["sha256"] == sparse_matrix_sha256(matrix)
    assert manifest["invariants"]["full_width"] is False
    assert manifest["invariants"]["adjacent_overlap"] is True
    assert manifest["adjacent_algebra"] == [
        json.loads(line)
        for line in (artifact / "05-blocks/algebra.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    for item in manifest["adjacent_algebra"]:
        intersection = set(item["intersection_segment_ids"])
        union = set(item["union_segment_ids"])
        xor = set(item["xor_segment_ids"])
        assert intersection
        assert intersection == union - xor


def test_spatial_block_artifact_rejects_forged_matrix_cells_and_spans(
    tmp_path: Path,
) -> None:
    segments = _grid_segments(1, 2)
    aligned_size = (40, 20)
    matrix = _matrix_for_segments(segments, aligned_size=aligned_size)
    plan = _plan(
        segments,
        (tuple(range(len(segments))),),
        aligned_size=aligned_size,
        config=_config(mode=BlockPlanningMode.SPATIAL_2D),
    )
    page = CropInput(
        "page",
        _png_bytes(np.zeros((20, 40, 3), dtype=np.uint8), mode="RGB"),
    )
    crops = BlockCropper().crop(page, aligned_size=aligned_size, plan=plan)
    first_id, second_id = (item.segment_id for item in segments)
    remap = {first_id: second_id, second_id: first_id}
    forged = SparseSegmentMatrix(
        rows=matrix.rows,
        columns=matrix.columns,
        cells=tuple(
            SparseCell(item.row, item.column, remap[item.segment_id])
            for item in matrix.cells
        ),
        spans=tuple(
            replace(item, segment_id=remap[item.segment_id])
            for item in matrix.spans
        ),
        horizontal_rule_rows=matrix.horizontal_rule_rows,
        vertical_rule_columns=matrix.vertical_rule_columns,
    )
    assert forged.segment_ids() == matrix.segment_ids()
    assert sparse_matrix_sha256(forged) != plan.matrix_sha256

    with pytest.raises(ValueError, match="matrix disagrees"):
        BlockArtifactWriter().write(
            tmp_path,
            run_id="forged-matrix",
            page=page,
            plan=plan,
            crops=crops,
            matrix=forged,
        )
    assert not (tmp_path / "forged-matrix").exists()


def test_three_by_two_artifact_contains_three_distinct_actual_block_pngs(
    tmp_path: Path,
) -> None:
    segments = _grid_segments(3, 2)
    aligned_size = (40, 50)
    matrix = _matrix_for_segments(segments, aligned_size=aligned_size)
    plan = _plan(
        segments,
        (tuple(range(len(segments))),),
        aligned_size=aligned_size,
        config=_config(
            mode=BlockPlanningMode.SPATIAL_2D,
            padding=0,
        ),
    )
    assert tuple(block.segment_ids for block in plan.blocks) == (
        tuple(item.segment_id for item in segments[:4]),
        tuple(item.segment_id for item in segments[2:]),
        tuple(item.segment_id for item in segments[::2]),
    )

    colors = (
        (220, 20, 60),
        (255, 140, 0),
        (255, 215, 0),
        (50, 205, 50),
        (30, 144, 255),
        (138, 43, 226),
    )
    pixels = np.full((50, 40, 3), 255, dtype=np.uint8)
    for segment, color in zip(segments, colors):
        pixels[
            segment.bbox.top : segment.bbox.bottom,
            segment.bbox.left : segment.bbox.right,
        ] = color
    page = CropInput("colored-grid", _png_bytes(pixels, mode="RGB"))
    crops = BlockCropper().crop(page, aligned_size=aligned_size, plan=plan)
    artifact = BlockArtifactWriter().write(
        tmp_path,
        run_id="colored-three-by-two",
        page=page,
        plan=plan,
        crops=crops,
        matrix=matrix,
    )

    raw_payloads = tuple(
        (artifact / "05-blocks/raw" / f"{block.block_id}.png").read_bytes()
        for block in plan.blocks
    )
    assert len(raw_payloads) == 3
    assert len({hashlib.sha256(value).hexdigest() for value in raw_payloads}) == 3
    actual_color_sets = []
    for payload in raw_payloads:
        with Image.open(io.BytesIO(payload)) as image:
            actual_color_sets.append(
                {
                    tuple(int(channel) for channel in value)
                    for value in np.asarray(image.convert("RGB")).reshape(-1, 3)
                    if tuple(int(channel) for channel in value) != (255, 255, 255)
                }
            )
    assert actual_color_sets == [
        set(colors[:4]),
        set(colors[2:]),
        set(colors[::2]),
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("max_segments", 0),
        ("max_objects", 0),
        ("max_core_segments", 0),
        ("max_block_pixels", 0),
        ("context_segments", -1),
        ("padding", -1),
        ("max_blocks", 0),
        ("max_pair_memberships", 0),
        ("max_block_segments", 0),
        ("max_segment_memberships", 0),
        ("max_overlap_pairs", 0),
        ("max_singleton_probes", 0),
        ("max_signature_candidates", 0),
        ("max_signature_candidate_checks", 0),
        ("max_total_block_memberships", 0),
        ("max_scope_gap_pixels", -1),
        ("max_scope_span_pixels", 0),
    ),
)
def test_planner_config_rejects_invalid_bounds(field: str, value: int) -> None:
    values = {
        "max_segments": 100,
        "max_objects": 100,
        "max_core_segments": 20,
        "max_block_pixels": 100_000,
        "context_segments": 1,
        "padding": 0,
        "max_blocks": 20,
        "max_pair_memberships": 1_000,
        "max_block_segments": 100,
        "max_segment_memberships": 10,
        "max_overlap_pairs": 100,
        "max_singleton_probes": 8,
        "max_signature_candidates": 100,
        "max_signature_candidate_checks": 10_000,
        "max_total_block_memberships": 10_000,
        "max_scope_gap_pixels": 64,
        "max_scope_span_pixels": 512,
    }
    values[field] = value

    with pytest.raises(
        ValueError,
        match=field.replace("_", " ") + "|bound|positive|negative",
    ):
        BlockPlanningConfig(**values)


@pytest.mark.parametrize(
    "overrides",
    (
        {"mode": "spatial_2d"},
        {"spatial_rows": 1},
        {"spatial_columns": 1},
        {"spatial_rows": 2, "spatial_row_overlap": 2},
        {"spatial_columns": 3, "spatial_column_overlap": 3},
        {
            "mode": BlockPlanningMode.SPATIAL_2D,
            "spatial_rows": 3,
            "spatial_row_overlap": 1,
        },
        {
            "mode": BlockPlanningMode.SPATIAL_2D,
            "spatial_columns": 5,
            "spatial_column_overlap": 1,
            "max_core_segments": 3,
        },
    ),
)
def test_spatial_config_rejects_ambiguous_or_unbounded_windows(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="mode|spatial|overlap|core|dimension"):
        BlockPlanningConfig(**overrides)


def test_planner_budgets_fail_before_returning_partial_blocks() -> None:
    segments = tuple(_segment(index) for index in range(4))
    result = _objects_result(segments, ((0, 1), (2, 3)))

    for config in (
        _config(max_segments=3),
        _config(max_objects=1),
        _config(max_blocks=1, max_core_segments=2),
        _config(max_pair_memberships=1, max_block_pixels=5_000),
    ):
        with pytest.raises(BlockPlanningLimitError):
            OverlappingBlockPlanner(config).plan(
                aligned_size=(100, 120),
                segments=segments,
                objects_result=result,
            )


def test_deterministic_random_row_plans_preserve_set_identities() -> None:
    rng = np.random.default_rng(2026071907)
    for case_index in range(30):
        segments_list: list[Segment] = []
        physical_rows: list[tuple[str, ...]] = []
        top = 2
        row_index = 0
        for _ in range(int(rng.integers(3, 9))):
            height = int(rng.integers(6, 11))
            fragments = []
            fragment_specs = sorted(
                (
                    int(rng.integers(0, 3)),
                    int(rng.integers(0, 160)),
                    int(rng.integers(10, 35)),
                )
                for _ in range(int(rng.integers(1, 4)))
            )
            for top_offset, left, width in fragment_specs:
                index = len(segments_list)
                bbox = Box(
                    left,
                    top + top_offset,
                    min(200, left + width),
                    top + height,
                )
                segment = replace(
                    _segment(index, bbox),
                    row_index=row_index,
                    order_key=(bbox.top, bbox.left),
                )
                row_index += 1
                segments_list.append(segment)
                fragments.append(segment.segment_id)
            physical_rows.append(tuple(fragments))
            top += height + int(rng.integers(4, 10))
        segments = tuple(segments_list)
        aligned_size = (200, top + 2)
        result = _objects_result(
            segments,
            tuple((index,) for index in range(len(segments))),
            aligned_size=aligned_size,
        )
        planner = OverlappingBlockPlanner(
            _config(
                max_core_segments=4,
                max_block_pixels=aligned_size[0] * aligned_size[1],
                context_segments=2,
            )
        )

        first = planner.plan(
            aligned_size=aligned_size,
            segments=segments,
            objects_result=result,
        )
        second = planner.plan(
            aligned_size=aligned_size,
            segments=tuple(reversed(segments)),
            objects_result=result,
        )

        assert first == second, case_index
        _assert_plan_identities(first)
        core_owner = {segment_id: block.block_id for block in first.blocks for segment_id in block.core_segment_ids}
        assert all(len({core_owner[segment_id] for segment_id in row}) == 1 for row in physical_rows), case_index
        assert all(
            set(first.blocks[left].segment_ids).isdisjoint(first.blocks[right].segment_ids)
            for left in range(len(first.blocks))
            for right in range(left + 2, len(first.blocks))
        ), case_index


def test_stage5_planner_contracts_are_deeply_immutable() -> None:
    plan = _plan((segment := _segment(0),), ((0,),))

    with pytest.raises(FrozenInstanceError):
        plan.blocks = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        plan.blocks[0].segment_ids = (segment.segment_id,)  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        plan.status = BlockPlanStatus.COMPLETE  # type: ignore[misc]


def test_spatial_crops_certify_literal_stage1_ownership_membership() -> None:
    segments = _grid_segments(3, 2)
    aligned_size = (40, 50)
    plan = _plan(
        segments,
        (tuple(range(len(segments))),),
        aligned_size=aligned_size,
        config=_config(mode=BlockPlanningMode.SPATIAL_2D, padding=0),
    )
    ownership = _ownership_raster(segments, aligned_size=aligned_size)
    segment_ids = tuple(item.segment_id for item in segments)
    page = CropInput(
        "ownership-page",
        _png_bytes(np.full((50, 40, 3), 255, dtype=np.uint8), mode="RGB"),
    )

    crops = BlockCropper().crop(
        page,
        aligned_size=aligned_size,
        plan=plan,
        ownership=ownership,
        ownership_segment_ids=segment_ids,
    )

    assert tuple(item.segment_ids for item in crops) == tuple(
        item.segment_ids for item in plan.blocks
    )


def test_spatial_crop_isolates_foreign_and_rejects_missing_literal_ownership(
    tmp_path: Path,
) -> None:
    segments = _grid_segments(3, 2)
    aligned_size = (40, 50)
    plan = _plan(
        segments,
        (tuple(range(len(segments))),),
        aligned_size=aligned_size,
        config=_config(mode=BlockPlanningMode.SPATIAL_2D, padding=0),
    )
    segment_ids = tuple(item.segment_id for item in segments)
    page = CropInput(
        "ownership-page",
        _png_bytes(np.full((50, 40, 3), 255, dtype=np.uint8), mode="RGB"),
    )
    ownership = np.array(
        _ownership_raster(segments, aligned_size=aligned_size),
        copy=True,
    )
    first = plan.blocks[0]
    foreign_label = next(
        index
        for index, segment_id in enumerate(segment_ids)
        if segment_id not in first.segment_ids
    )
    background = np.argwhere(
        ownership[
            first.bbox.top : first.bbox.bottom,
            first.bbox.left : first.bbox.right,
        ]
        == -1
    )[0]
    ownership[
        first.bbox.top + int(background[0]),
        first.bbox.left + int(background[1]),
    ] = foreign_label

    isolated = BlockCropper().crop(
        page,
        aligned_size=aligned_size,
        plan=plan,
        ownership=ownership,
        ownership_segment_ids=segment_ids,
    )
    assert isolated[0].masked_segment_ids == (segment_ids[foreign_label],)
    assert isolated[0].isolation_mask_png is not None
    raw_pixels = _crop_pixels(isolated[0].raw.png_bytes)
    assert tuple(raw_pixels[tuple(background)]) == (255, 255, 255)
    replayed = BlockCropper().crop(
        page,
        aligned_size=aligned_size,
        plan=plan,
        ownership=ownership,
        ownership_segment_ids=segment_ids,
        isolation_source=isolated,
    )
    assert replayed == isolated
    artifact = BlockArtifactWriter().write(
        tmp_path,
        run_id="ownership-isolation",
        page=page,
        plan=plan,
        crops=isolated,
        matrix=_matrix_for_segments(segments, aligned_size=aligned_size),
    )
    entries = tuple(
        json.loads(line)
        for line in (artifact / "05-blocks/blocks.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert entries[0]["masked_segment_ids"] == [segment_ids[foreign_label]]
    assert entries[0]["isolation_mask"] == (
        "isolation-masks/block-000000.png"
    )
    assert (
        artifact / "05-blocks/isolation-masks/block-000000.png"
    ).read_bytes() == isolated[0].isolation_mask_png

    missing = np.array(
        _ownership_raster(segments, aligned_size=aligned_size),
        copy=True,
    )
    missing[missing == 0] = -1
    with pytest.raises(BlockCropInvariantError, match="missing=.*segment"):
        BlockCropper().crop(
            page,
            aligned_size=aligned_size,
            plan=plan,
            ownership=missing,
            ownership_segment_ids=segment_ids,
        )


def test_spatial_crop_rejects_partial_or_forged_ownership_evidence() -> None:
    segments = _grid_segments(1, 2)
    aligned_size = (40, 20)
    plan = _plan(
        segments,
        ((0, 1),),
        aligned_size=aligned_size,
        config=_config(mode=BlockPlanningMode.SPATIAL_2D, padding=0),
    )
    ownership = _ownership_raster(segments, aligned_size=aligned_size)
    segment_ids = tuple(item.segment_id for item in segments)
    page = CropInput(
        "ownership-page",
        _png_bytes(np.full((20, 40, 3), 255, dtype=np.uint8), mode="RGB"),
    )

    with pytest.raises(BlockCropInvariantError, match="supplied together"):
        BlockCropper().crop(
            page,
            aligned_size=aligned_size,
            plan=plan,
            ownership=ownership,
        )
    with pytest.raises(BlockCropInvariantError, match="segment order"):
        BlockCropper().crop(
            page,
            aligned_size=aligned_size,
            plan=plan,
            ownership=ownership,
            ownership_segment_ids=tuple(reversed(segment_ids[:-1])),
        )
    forged = np.array(ownership, copy=True)
    forged[0, 0] = len(segment_ids)
    with pytest.raises(BlockCropInvariantError, match="unknown segment label"):
        BlockCropper().crop(
            page,
            aligned_size=aligned_size,
            plan=plan,
            ownership=forged,
            ownership_segment_ids=segment_ids,
        )


def test_spatial_isolation_replay_rejects_erased_member_and_wrong_foreign_id() -> None:
    segments = _grid_segments(3, 2)
    aligned_size = (40, 50)
    plan = _plan(
        segments,
        (tuple(range(len(segments))),),
        aligned_size=aligned_size,
        config=_config(mode=BlockPlanningMode.SPATIAL_2D, padding=0),
    )
    segment_ids = tuple(item.segment_id for item in segments)
    ownership = np.array(
        _ownership_raster(segments, aligned_size=aligned_size),
        copy=True,
    )
    page = CropInput(
        "adversarial-isolation-page",
        _png_bytes(np.full((50, 40, 3), 255, dtype=np.uint8), mode="RGB"),
    )
    first = plan.blocks[0]
    member_label = segment_ids.index(first.segment_ids[0])
    foreign_labels = tuple(
        index
        for index, segment_id in enumerate(segment_ids)
        if segment_id not in first.segment_ids
    )
    assert len(foreign_labels) >= 2

    forged_ownership = np.array(ownership, copy=True)
    member_pixel = next(
        (int(row), int(column))
        for row, column in np.argwhere(forged_ownership == member_label)
        if first.bbox.top <= row < first.bbox.bottom
        and first.bbox.left <= column < first.bbox.right
    )
    forged_ownership[member_pixel] = foreign_labels[0]
    member_erasing_crops = BlockCropper().crop(
        page,
        aligned_size=aligned_size,
        plan=plan,
        ownership=forged_ownership,
        ownership_segment_ids=segment_ids,
    )
    with pytest.raises(BlockCropInvariantError, match="Stage 1 ownership"):
        BlockCropper().crop(
            page,
            aligned_size=aligned_size,
            plan=plan,
            ownership=ownership,
            ownership_segment_ids=segment_ids,
            isolation_source=member_erasing_crops,
        )

    contaminated = np.array(ownership, copy=True)
    local_background = np.argwhere(
        contaminated[
            first.bbox.top : first.bbox.bottom,
            first.bbox.left : first.bbox.right,
        ]
        == -1
    )[0]
    contaminated[
        first.bbox.top + int(local_background[0]),
        first.bbox.left + int(local_background[1]),
    ] = foreign_labels[0]
    isolated = BlockCropper().crop(
        page,
        aligned_size=aligned_size,
        plan=plan,
        ownership=contaminated,
        ownership_segment_ids=segment_ids,
    )
    with pytest.raises(
        BlockCropInvariantError,
        match="requires the Stage 1 ownership raster",
    ):
        BlockCropper().crop(
            page,
            aligned_size=aligned_size,
            plan=plan,
            isolation_source=isolated,
        )

    expected_foreign_pixel = (
        int(local_background[0]),
        int(local_background[1]),
    )
    alternative_pixel = next(
        (int(row), int(column))
        for row, column in np.argwhere(
            np.ones(
                (first.bbox.height, first.bbox.width),
                dtype=np.bool_,
            )
        )
        if (int(row), int(column)) != expected_foreign_pixel
    )
    forged_mask = np.zeros(
        (first.bbox.height, first.bbox.width),
        dtype=np.uint8,
    )
    forged_mask[alternative_pixel] = 255
    wrong_mask = replace(
        isolated[0],
        isolation_mask_png=_png_bytes(forged_mask, mode="L"),
    )
    with pytest.raises(BlockCropInvariantError, match="mask disagrees"):
        BlockCropper().crop(
            page,
            aligned_size=aligned_size,
            plan=plan,
            ownership=contaminated,
            ownership_segment_ids=segment_ids,
            isolation_source=(wrong_mask, *isolated[1:]),
        )

    wrong_id = replace(
        isolated[0],
        masked_segment_ids=(segment_ids[foreign_labels[1]],),
    )
    with pytest.raises(BlockCropInvariantError, match="masked segment IDs"):
        BlockCropper().crop(
            page,
            aligned_size=aligned_size,
            plan=plan,
            ownership=contaminated,
            ownership_segment_ids=segment_ids,
            isolation_source=(wrong_id, *isolated[1:]),
        )


def test_block_crop_pairs_preserve_raw_pixels_and_bind_gamma_to_raw() -> None:
    page_pixels = np.arange(100 * 120 * 3, dtype=np.uint32).reshape(120, 100, 3)
    page_pixels = (page_pixels % 256).astype(np.uint8)
    page = CropInput("page", _png_bytes(page_pixels, mode="RGB"))
    segments = tuple(_segment(index) for index in range(4))
    plan = _plan(
        segments,
        ((0, 1), (2, 3)),
        config=_config(max_block_pixels=5_000),
    )
    cropper = BlockCropper()

    first = cropper.crop(page, aligned_size=(100, 120), plan=plan)
    second = cropper.crop(page, aligned_size=(100, 120), plan=plan)

    assert first == second
    assert len(first) == len(plan.blocks)
    original_digest = hashlib.sha256(page.png_bytes).hexdigest()
    assert hashlib.sha256(page.png_bytes).hexdigest() == original_digest
    for block, pair in zip(plan.blocks, first):
        assert isinstance(pair, BlockCropPair)
        assert pair.block_id == block.block_id
        assert pair.bbox == block.bbox
        assert pair.segment_ids == block.segment_ids
        assert pair.raw == CropInput(f"{block.block_id}-raw", pair.raw.png_bytes)
        assert isinstance(pair.gamma, EnhancedCrop)
        assert pair.gamma.crop_id == f"{block.block_id}-gamma"
        assert pair.gamma.source_sha256 == hashlib.sha256(pair.raw.png_bytes).hexdigest()
        assert pair.gamma.recipe == RECIPE_ID
        assert pair.gamma.backend is EnhancementBackend.NUMPY
        with Image.open(io.BytesIO(pair.raw.png_bytes)) as raw_image:
            raw_dpi = raw_image.info["dpi"]
        assert all(abs(float(value) - pair.gamma.dpi) <= 0.01 for value in raw_dpi)
        expected = page_pixels[
            block.bbox.top : block.bbox.bottom,
            block.bbox.left : block.bbox.right,
        ]
        assert np.array_equal(_crop_pixels(pair.raw.png_bytes), expected)
        assert _crop_pixels(pair.gamma.png_bytes).shape == expected.shape[:2]


def test_block_cropper_flattens_alpha_to_white_without_changing_opaque_pixels() -> None:
    pixels = np.zeros((20, 20, 4), dtype=np.uint8)
    pixels[:, :, :] = (12, 34, 56, 255)
    pixels[5, 5] = (255, 0, 0, 0)
    page = CropInput("page", _png_bytes(pixels, mode="RGBA"))
    segment = _segment(0, Box(0, 0, 20, 20))
    plan = _plan(
        (segment,),
        ((0,),),
        aligned_size=(20, 20),
        config=_config(),
    )

    pair = BlockCropper().crop(page, aligned_size=(20, 20), plan=plan)[0]
    raw = _crop_pixels(pair.raw.png_bytes)

    assert raw.shape == (20, 20, 3)
    assert tuple(raw[0, 0]) == (12, 34, 56)
    assert tuple(raw[5, 5]) == (255, 255, 255)


def test_block_cropper_rejects_page_size_mismatch_and_non_png() -> None:
    segment = _segment(0, Box(0, 0, 20, 20))
    plan = _plan(
        (segment,),
        ((0,),),
        aligned_size=(20, 20),
        config=_config(),
    )
    wrong_size = CropInput(
        "page",
        _png_bytes(np.zeros((21, 20, 3), dtype=np.uint8), mode="RGB"),
    )

    with pytest.raises(BlockCropInvariantError, match="size|aligned|dimension"):
        BlockCropper().crop(wrong_size, aligned_size=(20, 20), plan=plan)
    with pytest.raises(BlockCropInvariantError, match="PNG|image"):
        BlockCropper().crop(
            CropInput("page", b"not a png"),
            aligned_size=(20, 20),
            plan=plan,
        )


def test_block_cropper_rejects_animated_png_and_unresolved_orientation() -> None:
    segment = _segment(0, Box(0, 0, 20, 20))
    plan = _plan(
        (segment,),
        ((0,),),
        aligned_size=(20, 20),
        config=_config(),
    )
    frames = [Image.new("RGB", (20, 20), color) for color in ("white", "black")]
    animated = io.BytesIO()
    try:
        frames[0].save(
            animated,
            format="PNG",
            save_all=True,
            append_images=frames[1:],
            duration=10,
            loop=0,
        )
    finally:
        for frame in frames:
            frame.close()
    oriented = io.BytesIO()
    image = Image.new("RGB", (20, 20), "white")
    exif = Image.Exif()
    exif[274] = 6
    try:
        image.save(oriented, format="PNG", exif=exif)
    finally:
        image.close()

    for payload in (animated.getvalue(), oriented.getvalue()):
        with pytest.raises(BlockCropInvariantError, match="frame|orientation|PNG"):
            BlockCropper().crop(
                CropInput("page", payload),
                aligned_size=(20, 20),
                plan=plan,
            )


def test_block_cropper_rejects_plan_scope_mismatch() -> None:
    segment = _segment(0, Box(0, 0, 20, 20))
    plan = _plan(
        (segment,),
        ((0,),),
        aligned_size=(20, 20),
        config=_config(),
    )
    page = CropInput(
        "page",
        _png_bytes(np.zeros((20, 20, 3), dtype=np.uint8), mode="RGB"),
    )

    with pytest.raises(BlockCropInvariantError, match="plan|aligned|size"):
        BlockCropper().crop(page, aligned_size=(21, 20), plan=plan)


def test_aggregate_crop_budget_fails_before_page_decode_or_enhancer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    segments = tuple(_segment(index) for index in range(4))
    plan = _plan(
        segments,
        ((0, 1), (2, 3)),
        config=_config(max_block_pixels=5_000),
    )
    page = CropInput(
        "page",
        _png_bytes(np.zeros((120, 100, 3), dtype=np.uint8), mode="RGB"),
    )
    cropper = BlockCropper(
        _crop_config(
            max_block_pixels=5_000,
            max_total_crop_pixels=sum(block.bbox.area for block in plan.blocks) - 1,
        )
    )
    called = {"decode": False, "enhancer": False}

    def forbidden_decode(*args: object, **kwargs: object) -> Image.Image:
        del args, kwargs
        called["decode"] = True
        raise AssertionError("aggregate budget must fail before page decode")

    def forbidden_enhancer() -> object:
        called["enhancer"] = True
        raise AssertionError("aggregate budget must fail before enhancer creation")

    monkeypatch.setattr(cropper, "_decode_page", forbidden_decode)
    monkeypatch.setattr(cropper, "_enhancer", forbidden_enhancer)

    with pytest.raises(BlockCropLimitError, match="aggregate|total|pixel"):
        cropper.crop(page, aligned_size=(100, 120), plan=plan)
    assert called == {"decode": False, "enhancer": False}


def test_cropper_flushes_a_bounded_gamma_batch_before_cropping_all_raw_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    segments = tuple(_segment(index) for index in range(4))
    plan = _plan(
        segments,
        ((0, 1), (2, 3)),
        config=_config(max_block_pixels=5_000),
    )
    rng = np.random.default_rng(2026071905)
    page = CropInput(
        "page",
        _png_bytes(
            rng.integers(0, 256, size=(120, 100, 3), dtype=np.uint8),
            mode="RGB",
        ),
    )
    cropper = BlockCropper(
        _crop_config(
            max_block_pixels=5_000,
            max_total_crop_pixels=10_000,
            max_batch_items=1,
            max_batch_pixels=5_000,
        )
    )
    original_raw_crop = cropper._raw_crop
    original_enhancer = cropper._enhancer()
    events: list[str] = []

    def recording_raw(
        image: Image.Image,
        block_id: str,
        bbox: Box,
        *,
        isolation_mask_png: bytes | None = None,
    ) -> CropInput:
        events.append(f"raw:{block_id}")
        return original_raw_crop(
            image,
            block_id,
            bbox,
            isolation_mask_png=isolation_mask_png,
        )

    class RecordingEnhancer:
        def enhance_many(
            self,
            items: tuple[CropInput, ...],
        ) -> tuple[EnhancedCrop, ...]:
            events.extend(f"gamma:{item.crop_id}" for item in items)
            return original_enhancer.enhance_many(items)

    monkeypatch.setattr(cropper, "_raw_crop", recording_raw)
    monkeypatch.setattr(cropper, "_enhancer", RecordingEnhancer)

    pairs = cropper.crop(page, aligned_size=(100, 120), plan=plan)

    assert len(pairs) == 2
    assert events.index("gamma:block-000000-gamma") < events.index("raw:block-000001")


def test_cropper_enforces_cumulative_actual_raw_and_gamma_output_bytes() -> None:
    segments = tuple(_segment(index) for index in range(4))
    plan = _plan(
        segments,
        ((0, 1), (2, 3)),
        config=_config(max_block_pixels=5_000),
    )
    rng = np.random.default_rng(2026071906)
    page = CropInput(
        "page",
        _png_bytes(
            rng.integers(0, 256, size=(120, 100, 3), dtype=np.uint8),
            mode="RGB",
        ),
    )
    cropper = BlockCropper(
        _crop_config(
            max_block_pixels=5_000,
            max_total_crop_pixels=10_000,
            max_crop_bytes=20_000,
            max_total_output_bytes=20_000,
            max_batch_items=1,
            max_batch_pixels=5_000,
        )
    )

    with pytest.raises(BlockCropLimitError, match="aggregate|total|byte"):
        cropper.crop(page, aligned_size=(100, 120), plan=plan)


def test_stage5_artifacts_are_atomic_deterministic_and_auditable(
    tmp_path: Path,
) -> None:
    segments = tuple(_segment(index) for index in range(4))
    plan = _plan(
        segments,
        ((0, 1), (2, 3)),
        config=_config(max_block_pixels=5_000),
    )
    pixels = np.arange(120 * 100 * 3, dtype=np.uint32).reshape(120, 100, 3)
    page = CropInput("page", _png_bytes((pixels % 256).astype(np.uint8), mode="RGB"))
    crops = BlockCropper().crop(page, aligned_size=(100, 120), plan=plan)
    writer = BlockArtifactWriter()

    first = writer.write(
        tmp_path / "first",
        run_id="sample",
        page=page,
        plan=plan,
        crops=crops,
    )
    second = writer.write(
        tmp_path / "second",
        run_id="sample",
        page=page,
        plan=plan,
        crops=crops,
    )
    first_stage = first / "05-blocks"
    second_stage = second / "05-blocks"

    assert _artifact_files(first_stage) == (
        "algebra.jsonl",
        "blocks.jsonl",
        "diagnostics.txt",
        "gamma/block-000000.png",
        "gamma/block-000001.png",
        "manifest.json",
        "page/source.png",
        "raw/block-000000.png",
        "raw/block-000001.png",
    )
    assert _artifact_files(second_stage) == _artifact_files(first_stage)
    for relative in _artifact_files(first_stage):
        assert (first_stage / relative).read_bytes() == (second_stage / relative).read_bytes()
    assert (first_stage / "page/source.png").read_bytes() == page.png_bytes
    for pair in crops:
        assert (first_stage / "raw" / f"{pair.block_id}.png").read_bytes() == (pair.raw.png_bytes)
        assert (first_stage / "gamma" / f"{pair.block_id}.png").read_bytes() == (pair.gamma.png_bytes)

    manifest = json.loads((first_stage / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["semantic_stage"] == 5
    assert manifest["execution_step"] == 5
    assert manifest["selection_stage"] == 2
    assert manifest["selection_deferred_to_stage2"] is True
    assert manifest["source_page_sha256"] == hashlib.sha256(page.png_bytes).hexdigest()
    assert manifest["source_segment_ids"] == list(plan.source_segment_ids)
    assert manifest["order"] == [block.block_id for block in plan.blocks]
    assert manifest["invariants"] == {
        "adjacent_overlap": True,
        "core_partition_exact": True,
        "full_width": True,
        "raw_gamma_pairs": True,
        "source_page_preserved": True,
    }
    assert manifest["adjacent_algebra"] == [
        json.loads(line) for line in (first_stage / "algebra.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    entries = tuple(
        json.loads(line) for line in (first_stage / "blocks.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert tuple(entry["block_id"] for entry in entries) == tuple(block.block_id for block in plan.blocks)
    for entry, block, pair in zip(entries, plan.blocks, crops):
        assert entry["bbox"] == list(block.bbox.as_tuple())
        assert entry["segment_ids"] == list(block.segment_ids)
        assert entry["raw_crop_id"] == pair.raw.crop_id
        assert entry["gamma_crop_id"] == pair.gamma.crop_id
        assert entry["gamma_recipe"] == RECIPE_ID
        assert entry["gamma_backend"] == EnhancementBackend.NUMPY.value
        assert entry["dpi"] == pair.gamma.dpi
        assert entry["raw_sha256"] == hashlib.sha256(pair.raw.png_bytes).hexdigest()
        assert entry["gamma_sha256"] == hashlib.sha256(pair.gamma.png_bytes).hexdigest()
        assert entry["gamma_source_sha256"] == entry["raw_sha256"]

    diagnostics = (first_stage / "diagnostics.txt").read_text(encoding="utf-8")
    for invariant in (
        "core_partition_exact=true",
        "adjacent_overlap=true",
        "full_width=true",
        "raw_gamma_pairs=true",
        "selection_deferred_to_stage2=true",
    ):
        assert invariant in diagnostics


def test_stage5_artifact_writer_never_overwrites_or_loses_atomic_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    segment = _segment(0, Box(0, 0, 20, 20))
    plan = _plan(
        (segment,),
        ((0,),),
        aligned_size=(20, 20),
        config=_config(),
    )
    page = CropInput(
        "page",
        _png_bytes(np.zeros((20, 20, 3), dtype=np.uint8), mode="RGB"),
    )
    crops = BlockCropper().crop(page, aligned_size=(20, 20), plan=plan)
    writer = BlockArtifactWriter()
    writer.write(tmp_path, run_id="existing", page=page, plan=plan, crops=crops)

    with pytest.raises(FileExistsError):
        writer.write(tmp_path, run_id="existing", page=page, plan=plan, crops=crops)
    for run_id in ("", "../escape", "nested/path", "white space"):
        with pytest.raises(ValueError, match="run_id|unsafe"):
            writer.write(tmp_path, run_id=run_id, page=page, plan=plan, crops=crops)

    destination = tmp_path / "raced"
    original_mkdir = Path.mkdir
    injected = False

    def inject_race(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal injected
        if path == destination and not injected:
            original_mkdir(destination)
            injected = True
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", inject_race)
    with pytest.raises(FileExistsError):
        writer.write(tmp_path, run_id="raced", page=page, plan=plan, crops=crops)
    assert injected is True
    assert destination.is_dir() and not tuple(destination.iterdir())
    assert not tuple(tmp_path.glob(".raced.partial-*"))


def test_stage5_artifact_writer_rolls_back_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    segment = _segment(0, Box(0, 0, 20, 20))
    plan = _plan(
        (segment,),
        ((0,),),
        aligned_size=(20, 20),
        config=_config(),
    )
    page = CropInput(
        "page",
        _png_bytes(np.zeros((20, 20, 3), dtype=np.uint8), mode="RGB"),
    )
    crops = BlockCropper().crop(page, aligned_size=(20, 20), plan=plan)
    writer = BlockArtifactWriter()

    def fail_after_write(
        stage_dir: Path,
        *,
        page: CropInput,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        matrix: SparseSegmentMatrix | None,
    ) -> None:
        del page, plan, crops, matrix
        (stage_dir / "partial.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("injected Stage5 artifact failure")

    monkeypatch.setattr(writer, "_write_stage", fail_after_write)
    with pytest.raises(RuntimeError, match="injected Stage5 artifact failure"):
        writer.write(tmp_path, run_id="broken", page=page, plan=plan, crops=crops)
    assert not (tmp_path / "broken").exists()
    assert not tuple(tmp_path.glob(".broken.partial-*"))


def test_stage5_artifact_writer_rejects_crops_from_a_different_same_size_page(
    tmp_path: Path,
) -> None:
    segment = _segment(0, Box(0, 0, 20, 20))
    plan = _plan(
        (segment,),
        ((0,),),
        aligned_size=(20, 20),
        config=_config(),
    )
    first_page = CropInput(
        "first-page",
        _png_bytes(np.zeros((20, 20, 3), dtype=np.uint8), mode="RGB"),
    )
    second_page = CropInput(
        "second-page",
        _png_bytes(np.full((20, 20, 3), 255, dtype=np.uint8), mode="RGB"),
    )
    first_crops = BlockCropper().crop(
        first_page,
        aligned_size=(20, 20),
        plan=plan,
    )

    with pytest.raises(ValueError, match="page|source|raw|crop"):
        BlockArtifactWriter().write(
            tmp_path,
            run_id="cross-page",
            page=second_page,
            plan=plan,
            crops=first_crops,
        )
    assert not (tmp_path / "cross-page").exists()


def test_stage5_imports_do_not_load_ocr_or_optional_gpu_runtimes() -> None:
    repository = Path(__file__).resolve().parents[3]
    code = """
import json
import sys
sys.path.insert(0, 'ocr')
import app.sparse_pipeline.block_planning
import app.sparse_pipeline.block_crops
banned_roots = {
    'cv2', 'easyocr', 'kornia', 'onnxruntime', 'paddle', 'paddleocr',
    'pytesseract', 'tensorflow', 'tesserocr', 'torch',
}
banned = sorted(
    name for name in sys.modules
    if name.split('.', 1)[0] in banned_roots
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


def test_image_free_planner_import_does_not_load_raster_modules() -> None:
    repository = Path(__file__).resolve().parents[3]
    code = """
import json
import sys
sys.path.insert(0, 'ocr')
import app.sparse_pipeline.block_planning
banned_roots = {'PIL', 'cv2', 'numpy', 'torch'}
banned = sorted(
    name for name in sys.modules
    if name.split('.', 1)[0] in banned_roots
    or name in {
        'app.sparse_pipeline.block_artifacts',
        'app.sparse_pipeline.block_crops',
        'app.sparse_pipeline.crop_enhancement',
        'app.sparse_pipeline.geometry',
    }
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


def test_block_crop_config_rejects_non_config_and_typed_limit_is_distinct() -> None:
    with pytest.raises(TypeError, match="config"):
        BlockCropper(object())  # type: ignore[arg-type]
    assert issubclass(BlockCropLimitError, RuntimeError)
    assert not issubclass(BlockCropLimitError, BlockCropInvariantError)
    assert isinstance(BlockCropConfig(), BlockCropConfig)


def test_stage5_value_objects_reject_mutable_collection_payloads() -> None:
    with pytest.raises(ValueError, match="tuple|immutable"):
        RecognitionBlock(  # type: ignore[arg-type]
            block_id="block-000000",
            bbox=Box(0, 0, 10, 10),
            core_segment_ids=["segment-000000"],
            segment_ids=("segment-000000",),
            context_segment_ids=(),
            object_ids=("object-000000",),
        )
    with pytest.raises(ValueError, match="tuple|immutable"):
        BlockSetAlgebra(  # type: ignore[arg-type]
            first_block_id="block-000000",
            second_block_id="block-000001",
            intersection_segment_ids=["segment-000000"],
            union_segment_ids=("segment-000000",),
            xor_segment_ids=(),
            first_only_segment_ids=(),
            second_only_segment_ids=(),
        )
