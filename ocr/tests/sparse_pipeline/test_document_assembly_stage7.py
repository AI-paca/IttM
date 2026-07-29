from __future__ import annotations

import hashlib
import inspect
import io
from dataclasses import dataclass, replace
from typing import Callable

import numpy as np
import pytest
from PIL import Image

from app.sparse_pipeline.block_crops import BlockCropPair, BlockCropper
from app.sparse_pipeline.block_planning import (
    BlockPlan,
    BlockPlanningConfig,
    BlockPlanningMode,
    OverlappingBlockPlanner,
)
from app.sparse_pipeline.contracts import (
    AffineTransform,
    AlignmentTrace,
    AxisInterval,
    Box,
    GeometryResult,
    RecursiveNode,
    Rule,
    RuleAxis,
    Segment,
    SegmentationResult,
    SegmentKind,
    SegmentSpan,
    SparseCell,
    SparseSegmentMatrix,
    StopReason,
)
from app.sparse_pipeline.crop_enhancement import CropInput
from app.sparse_pipeline.document_assembly import (
    AssemblyStatus,
    AttributionLevel,
    DocumentAssembler,
    DocumentAssemblyConfig,
    DocumentAssemblyInvariantError,
    DocumentAssemblyLimitError,
)
from app.sparse_pipeline.object_reconstruction import (
    ObjectKind,
    ObjectReconstructionResult,
    ObjectReconstructor,
)
from app.sparse_pipeline.ocr_adapter_contracts import (
    OcrFailureCode,
    OcrOutputGeometry,
    OcrResource,
)
from app.sparse_pipeline.ocr_fusion import (
    OcrEvidenceFusion,
    OcrFusionConfig,
    OcrFusionResult,
    OcrFusionStatus,
    OcrRoutingMode,
    compact_ocr_text,
)
from app.sparse_pipeline.ocr_queue import (
    OcrEngineOutput,
    OcrJobResult,
    OcrJobStatus,
    OcrQueueResult,
    OcrQueueStatus,
    OcrTransform,
    OcrWord,
)


@dataclass(frozen=True)
class _Fixture:
    geometry: GeometryResult
    objects: ObjectReconstructionResult
    plan: BlockPlan
    crops: tuple[BlockCropPair, ...]
    queue: OcrQueueResult
    fusion: OcrFusionResult
    page: CropInput
    planning_config: BlockPlanningConfig | None = None

    def __post_init__(self) -> None:
        expected_crops, aligned_rgb_sha256 = BlockCropper().crop_with_rgb_sha256(
            self.page,
            aligned_size=self.geometry.segmentation.aligned_size,
            plan=self.plan,
        )
        if self.crops != expected_crops:
            raise AssertionError(
                "test fixture crops must be derived from its aligned RGB page"
            )
        object.__setattr__(
            self,
            "geometry",
            replace(
                self.geometry,
                aligned_rgb_sha256=aligned_rgb_sha256,
            ),
        )


def _segment(
    segment_id: str,
    bbox: Box,
    *,
    row_index: int,
    component_id: int,
) -> Segment:
    return Segment(
        segment_id=segment_id,
        bbox=bbox,
        source_bbox=bbox,
        kind=SegmentKind.TEXT,
        ink_pixels=max(1, bbox.area // 4),
        row_index=row_index,
        order_key=(bbox.top, bbox.left),
        parent_path=("geo-root",),
        component_ids=(component_id,),
    )


def _rule(
    rule_id: str,
    bbox: Box,
    *,
    axis: RuleAxis,
) -> Rule:
    return Rule(
        rule_id=rule_id,
        bbox=bbox,
        source_bbox=bbox,
        axis=axis,
        foreground_pixels=max(1, bbox.area // 2),
        strength=1.0,
    )


def _matrix(
    *,
    row_edges: tuple[int, ...],
    column_edges: tuple[int, ...],
    placements: dict[str, tuple[tuple[int, int], ...]],
    horizontal_rule_rows: tuple[int, ...] = (),
    vertical_rule_columns: tuple[int, ...] = (),
) -> SparseSegmentMatrix:
    rows = tuple(
        AxisInterval(index, start, stop)
        for index, (start, stop) in enumerate(zip(row_edges, row_edges[1:]))
    )
    columns = tuple(
        AxisInterval(index, start, stop)
        for index, (start, stop) in enumerate(
            zip(column_edges, column_edges[1:])
        )
    )
    cells = tuple(
        sorted(
            (
                SparseCell(row, column, segment_id)
                for segment_id, coordinates in placements.items()
                for row, column in coordinates
            ),
            key=lambda item: (item.row, item.column, item.segment_id),
        )
    )
    spans = tuple(
        SegmentSpan(
            segment_id=segment_id,
            row_start=min(row for row, _ in coordinates),
            row_stop=max(row for row, _ in coordinates) + 1,
            column_start=min(column for _, column in coordinates),
            column_stop=max(column for _, column in coordinates) + 1,
        )
        for segment_id, coordinates in placements.items()
    )
    return SparseSegmentMatrix(
        rows=rows,
        columns=columns,
        cells=cells,
        spans=spans,
        horizontal_rule_rows=horizontal_rule_rows,
        vertical_rule_columns=vertical_rule_columns,
    )


def _geometry(
    *,
    aligned_size: tuple[int, int],
    segments: tuple[Segment, ...],
    matrix: SparseSegmentMatrix,
    rules: tuple[Rule, ...] = (),
) -> GeometryResult:
    width, height = aligned_size
    foreground_pixels = sum(item.ink_pixels for item in segments) + sum(
        item.foreground_pixels for item in rules
    )
    content_boxes = tuple(item.bbox for item in segments) + tuple(
        item.bbox for item in rules
    )
    return GeometryResult(
        alignment=AlignmentTrace(
            transform=AffineTransform.identity(aligned_size),
            correction_degrees=0.0,
            background_rgb=(255, 255, 255),
            content_bbox=Box.union(content_boxes) if content_boxes else None,
            foreground_pixels=foreground_pixels,
        ),
        segmentation=SegmentationResult(
            segments=segments,
            rules=rules,
            nodes=(
                RecursiveNode(
                    node_id="geo-root",
                    bbox=Box(0, 0, width, height),
                    depth=0,
                    parent_id=None,
                    segment_ids=tuple(item.segment_id for item in segments),
                    stop_reason=(StopReason.ATOMIC if segments else StopReason.EMPTY),
                ),
            ),
            root_node_id="geo-root",
            aligned_size=aligned_size,
            foreground_pixels=foreground_pixels,
        ),
        matrix=matrix,
        aligned_rgb_sha256="0" * 64,
    )


def _empty_geometry() -> GeometryResult:
    return _geometry(
        aligned_size=(32, 24),
        segments=(),
        matrix=SparseSegmentMatrix((), (), (), ()),
    )


def _paragraph_geometry() -> GeometryResult:
    segments = (
        _segment("p-0", Box(8, 4, 112, 16), row_index=0, component_id=0),
        _segment("p-1", Box(8, 24, 116, 36), row_index=1, component_id=1),
    )
    return _geometry(
        aligned_size=(128, 44),
        segments=segments,
        matrix=_matrix(
            row_edges=(0, 20, 40, 44),
            column_edges=(0, 128),
            placements={"p-0": ((0, 0),), "p-1": ((1, 0),)},
        ),
    )


def _two_object_geometry() -> GeometryResult:
    segments = (
        _segment("a-0", Box(8, 4, 112, 16), row_index=0, component_id=0),
        _segment("b-0", Box(8, 84, 112, 96), row_index=1, component_id=1),
    )
    return _geometry(
        aligned_size=(128, 104),
        segments=segments,
        matrix=_matrix(
            row_edges=(0, 20, 80, 104),
            column_edges=(0, 128),
            placements={"a-0": ((0, 0),), "b-0": ((2, 0),)},
        ),
    )


def _partial_object_geometry() -> GeometryResult:
    segments = (
        _segment("a-0", Box(8, 4, 112, 16), row_index=0, component_id=0),
        _segment("b-0", Box(8, 84, 112, 96), row_index=1, component_id=1),
        _segment("b-1", Box(8, 104, 112, 116), row_index=2, component_id=2),
    )
    return _geometry(
        aligned_size=(128, 124),
        segments=segments,
        matrix=_matrix(
            row_edges=(0, 20, 80, 100, 120, 124),
            column_edges=(0, 128),
            placements={
                "a-0": ((0, 0),),
                "b-0": ((2, 0),),
                "b-1": ((3, 0),),
            },
        ),
    )


def _list_geometry() -> GeometryResult:
    segments = tuple(
        segment
        for row, top in enumerate((4, 28, 52))
        for segment in (
            _segment(
                f"marker-{row}",
                Box(4, top, 14, top + 12),
                row_index=row,
                component_id=row * 2,
            ),
            _segment(
                f"item-{row}",
                Box(28, top, 148, top + 12),
                row_index=row,
                component_id=row * 2 + 1,
            ),
        )
    )
    return _geometry(
        aligned_size=(160, 72),
        segments=segments,
        matrix=_matrix(
            row_edges=(0, 20, 24, 44, 48, 68, 72),
            column_edges=(0, 20, 160),
            placements={
                "marker-0": ((0, 0),),
                "item-0": ((0, 1),),
                "marker-1": ((2, 0),),
                "item-1": ((2, 1),),
                "marker-2": ((4, 0),),
                "item-2": ((4, 1),),
            },
        ),
    )


def _table_geometry() -> GeometryResult:
    width, height = 166, 46
    segments = (
        _segment("t-00", Box(3, 3, 20, 20), row_index=0, component_id=0),
        _segment("t-01", Box(25, 3, 160, 20), row_index=0, component_id=1),
        _segment("t-10", Box(3, 25, 20, 42), row_index=1, component_id=2),
        _segment("t-11", Box(25, 25, 160, 42), row_index=1, component_id=3),
    )
    rules = (
        _rule("h-0", Box(0, 0, width, 2), axis=RuleAxis.HORIZONTAL),
        _rule("h-1", Box(0, 22, width, 24), axis=RuleAxis.HORIZONTAL),
        _rule("h-2", Box(0, 44, width, 46), axis=RuleAxis.HORIZONTAL),
        _rule("v-0", Box(0, 0, 2, height), axis=RuleAxis.VERTICAL),
        _rule("v-1", Box(22, 0, 24, height), axis=RuleAxis.VERTICAL),
        _rule("v-2", Box(164, 0, 166, height), axis=RuleAxis.VERTICAL),
    )
    return _geometry(
        aligned_size=(width, height),
        segments=segments,
        rules=rules,
        matrix=_matrix(
            row_edges=(0, 2, 22, 24, 44, 46),
            column_edges=(0, 2, 22, 24, 164, 166),
            placements={
                "t-00": ((1, 1),),
                "t-01": ((1, 3),),
                "t-10": ((3, 1),),
                "t-11": ((3, 3),),
            },
            horizontal_rule_rows=(0, 2, 4),
            vertical_rule_columns=(0, 2, 4),
        ),
    )


def _two_column_geometry() -> GeometryResult:
    segments = tuple(
        segment
        for row, top in enumerate((4, 28, 52))
        for segment in (
            _segment(
                f"left-{row}",
                Box(4, top, 64, top + 12),
                row_index=row,
                component_id=row * 2,
            ),
            _segment(
                f"right-{row}",
                Box(132, top, 196, top + 12),
                row_index=row,
                component_id=row * 2 + 1,
            ),
        )
    )
    return _geometry(
        aligned_size=(200, 72),
        segments=segments,
        matrix=_matrix(
            row_edges=(0, 20, 24, 44, 48, 68, 72),
            column_edges=(0, 100, 200),
            placements={
                "left-0": ((0, 0),),
                "right-0": ((0, 1),),
                "left-1": ((2, 0),),
                "right-1": ((2, 1),),
                "left-2": ((4, 0),),
                "right-2": ((4, 1),),
            },
        ),
    )


def _png_bytes(width: int, height: int, *, seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    pixels = rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
    output = io.BytesIO()
    image = Image.fromarray(pixels, mode="RGB")
    try:
        image.save(output, format="PNG", compress_level=1)
    finally:
        image.close()
    return output.getvalue()


def _crops(
    geometry: GeometryResult,
    plan: BlockPlan,
    *,
    seed: int = 7,
) -> tuple[CropInput, tuple[BlockCropPair, ...]]:
    width, height = geometry.segmentation.aligned_size
    page = CropInput("stage7-fixture", _png_bytes(width, height, seed=seed))
    return (
        page,
        BlockCropper().crop(
            page,
            aligned_size=geometry.segmentation.aligned_size,
            plan=plan,
        ),
    )


def _ownership_for_geometry(
    geometry: GeometryResult,
) -> tuple[np.ndarray, tuple[str, ...]]:
    width, height = geometry.segmentation.aligned_size
    ownership = np.full((height, width), -1, dtype=np.int32)
    segment_ids = tuple(
        segment.segment_id for segment in geometry.segmentation.segments
    )
    for label, segment in enumerate(geometry.segmentation.segments):
        box = segment.bbox
        ownership[box.top : box.bottom, box.left : box.right] = label
    ownership.setflags(write=False)
    return ownership, segment_ids


_OutputFactory = Callable[
    [object, OcrTransform],
    OcrEngineOutput,
]


def _queue_lanes(
    plan: BlockPlan,
    crops: tuple[BlockCropPair, ...],
    output_factories: dict[str, _OutputFactory],
) -> OcrQueueResult:
    crop_by_id = {item.block_id: item for item in crops}
    jobs: list[OcrJobResult] = []
    for block in plan.blocks:
        crop = crop_by_id[block.block_id]
        context_sha256 = hashlib.sha256(crop.raw.png_bytes).hexdigest()
        for transform in (OcrTransform.RAW, OcrTransform.GAMMA):
            payload = (
                crop.raw.png_bytes
                if transform is OcrTransform.RAW
                else crop.gamma.png_bytes
            )
            for lane_id, output_factory in output_factories.items():
                jobs.append(
                    OcrJobResult(
                        job_id=f"ocr-job-{len(jobs):08d}",
                        block_id=block.block_id,
                        transform=transform,
                        lane_id=lane_id,
                        resource=OcrResource.CPU,
                        status=OcrJobStatus.COMPLETE,
                        output=output_factory(block, transform),
                        error_type=None,
                        error_message=None,
                        elapsed_seconds=0.01,
                        input_sha256=hashlib.sha256(payload).hexdigest(),
                        context_sha256=context_sha256,
                        capability_id=f"{lane_id}-capability",
                    )
                )
    return OcrQueueResult(
        jobs=tuple(jobs),
        status=OcrQueueStatus.COMPLETE,
        complete=len(jobs),
        failed=0,
    )


def _queue(
    plan: BlockPlan,
    crops: tuple[BlockCropPair, ...],
    output_factory: _OutputFactory,
) -> OcrQueueResult:
    return _queue_lanes(plan, crops, {"stage7-lane": output_factory})


def _fail_scheduled_lane(
    queue: OcrQueueResult,
    *,
    lane_id: str,
) -> OcrQueueResult:
    jobs = tuple(
        replace(
            item,
            status=OcrJobStatus.FAILED,
            output=None,
            error_type="RuntimeError",
            error_message="scheduled capability failed",
            failure_code=OcrFailureCode.ENGINE_ERROR,
        )
        if item.lane_id == lane_id
        else item
        for item in queue.jobs
    )
    complete = sum(item.status is OcrJobStatus.COMPLETE for item in jobs)
    failed = len(jobs) - complete
    assert failed > 0
    return OcrQueueResult(
        jobs=jobs,
        status=OcrQueueStatus.PARTIAL,
        complete=complete,
        failed=failed,
    )


def _text_only(text_by_block: dict[str, str]) -> _OutputFactory:
    def output(block: object, transform: OcrTransform) -> OcrEngineOutput:
        del transform
        block_id = getattr(block, "block_id")
        return OcrEngineOutput(
            text=text_by_block[block_id],
            words=(),
            geometry=OcrOutputGeometry.TEXT_ONLY,
        )

    return output


def _word_boxes(
    geometry: GeometryResult,
    text_by_segment: dict[str, str],
    *,
    confidence: float = 0.99,
) -> _OutputFactory:
    segment_by_id = {
        item.segment_id: item for item in geometry.segmentation.segments
    }

    def output(block: object, transform: OcrTransform) -> OcrEngineOutput:
        del transform
        bbox = getattr(block, "bbox")
        segment_ids = getattr(block, "segment_ids")
        words = tuple(
            OcrWord(
                text=text_by_segment[segment_id],
                bbox=Box(
                    segment_by_id[segment_id].bbox.left - bbox.left,
                    segment_by_id[segment_id].bbox.top - bbox.top,
                    segment_by_id[segment_id].bbox.right - bbox.left,
                    segment_by_id[segment_id].bbox.bottom - bbox.top,
                ),
                confidence=confidence,
            )
            for segment_id in segment_ids
            if segment_id in text_by_segment
        )
        return OcrEngineOutput(
            text=" ".join(item.text for item in words),
            words=words,
            geometry=OcrOutputGeometry.WORD_BOXES,
        )

    return output


def _fixture(
    geometry: GeometryResult,
    output_factory: _OutputFactory | None = None,
    *,
    planning_config: BlockPlanningConfig | None = None,
    crop_seed: int = 7,
) -> _Fixture:
    objects = ObjectReconstructor().reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    plan = OverlappingBlockPlanner(planning_config).plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
        matrix=geometry.matrix,
    )
    page, crops = _crops(geometry, plan, seed=crop_seed)
    _, aligned_rgb_sha256 = BlockCropper().crop_with_rgb_sha256(
        page,
        aligned_size=geometry.segmentation.aligned_size,
        plan=plan,
    )
    geometry = replace(geometry, aligned_rgb_sha256=aligned_rgb_sha256)
    if not plan.blocks:
        queue = OcrQueueResult((), OcrQueueStatus.COMPLETE, 0, 0)
    else:
        if output_factory is None:
            raise AssertionError("a non-empty fixture needs OCR output")
        queue = _queue(plan, crops, output_factory)
    fusion = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=geometry.segmentation.segments,
        crops=crops,
        queue=queue,
    )
    return _Fixture(
        geometry,
        objects,
        plan,
        crops,
        queue,
        fusion,
        page,
        planning_config,
    )


def _multi_lane_fixture(
    geometry: GeometryResult,
    output_factories: dict[str, _OutputFactory],
) -> _Fixture:
    objects = ObjectReconstructor().reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    plan = OverlappingBlockPlanner().plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
    )
    page, crops = _crops(geometry, plan)
    queue = _queue_lanes(plan, crops, output_factories)
    fusion = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=geometry.segmentation.segments,
        crops=crops,
        queue=queue,
    )
    return _Fixture(geometry, objects, plan, crops, queue, fusion, page)


def _two_object_overlap_fixture(
    *,
    anchor_text: str,
    source_text: str,
) -> _Fixture:
    geometry = _two_object_geometry()
    objects = ObjectReconstructor().reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    planning_config = BlockPlanningConfig(
        max_core_segments=1,
        max_block_pixels=20_000,
        context_segments=1,
        padding=0,
    )
    plan = OverlappingBlockPlanner(planning_config).plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
    )
    assert len(plan.blocks) == 2
    assert plan.blocks[1].context_segment_ids == ("a-0",)
    page, crops = _crops(geometry, plan)
    _, aligned_rgb_sha256 = BlockCropper().crop_with_rgb_sha256(
        page,
        aligned_size=geometry.segmentation.aligned_size,
        plan=plan,
    )
    geometry = replace(geometry, aligned_rgb_sha256=aligned_rgb_sha256)
    queue = _queue(
        plan,
        crops,
        _text_only(
            {
                "block-000000": anchor_text,
                "block-000001": source_text,
            }
        ),
    )
    fusion = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=geometry.segmentation.segments,
        crops=crops,
        queue=queue,
    )
    return _Fixture(
        geometry,
        objects,
        plan,
        crops,
        queue,
        fusion,
        page,
        planning_config,
    )


def _assemble(fixture: _Fixture, **overrides: object) -> object:
    values: dict[str, object] = {
        "geometry": fixture.geometry,
        "objects": fixture.objects,
        "plan": fixture.plan,
        "crops": fixture.crops,
        "queue": fixture.queue,
        "fusion": fixture.fusion,
        "page": fixture.page,
        "planning_config": fixture.planning_config,
    }
    values.update(overrides)
    return DocumentAssembler().assemble(**values)


def _assert_exact_codepoints_in_order(haystack: str, values: tuple[str, ...]) -> None:
    compact = compact_ocr_text(haystack)
    cursor = 0
    for value in values:
        expected = compact_ocr_text(value)
        position = compact.find(expected, cursor)
        assert position >= cursor
        cursor = position + len(expected)


def test_empty_page_is_complete_without_fabricated_evidence() -> None:
    fixture = _fixture(_empty_geometry())

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.COMPLETE
    assert result.text == ""
    assert result.candidate_text == ""
    assert result.objects == ()
    assert result.segments == ()
    assert result.structural_units == ()
    assert result.evidence_slices == ()


def test_full_spatial_assembly_preserves_multiple_membership_groups() -> None:
    segments = (
        _segment("g-00", Box(5, 5, 20, 15), row_index=0, component_id=0),
        _segment("g-01", Box(25, 5, 40, 15), row_index=0, component_id=1),
        _segment("g-10", Box(5, 25, 20, 35), row_index=1, component_id=2),
        _segment("g-11", Box(25, 25, 40, 35), row_index=1, component_id=3),
    )
    geometry = _geometry(
        aligned_size=(50, 40),
        segments=segments,
        matrix=_matrix(
            row_edges=(0, 20, 40),
            column_edges=(0, 50),
            placements={
                "g-00": ((0, 0),),
                "g-01": ((0, 0),),
                "g-10": ((1, 0),),
                "g-11": ((1, 0),),
            },
        ),
    )
    objects = ObjectReconstructor().reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    assert tuple(item.segment_ids for item in objects.objects) == (
        ("g-00", "g-01", "g-10", "g-11"),
    )
    planning_config = BlockPlanningConfig(
        mode=BlockPlanningMode.SPATIAL_2D,
        padding=0,
    )
    plan = OverlappingBlockPlanner(planning_config).plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
        matrix=geometry.matrix,
    )
    assert tuple(item.segment_ids for item in plan.membership_units) == (
        ("g-00", "g-01"),
        ("g-10", "g-11"),
    )
    page, crops = _crops(geometry, plan)
    _, aligned_rgb_sha256 = BlockCropper().crop_with_rgb_sha256(
        page,
        aligned_size=geometry.segmentation.aligned_size,
        plan=plan,
    )
    geometry = replace(geometry, aligned_rgb_sha256=aligned_rgb_sha256)
    queue = _queue(
        plan,
        crops,
        _word_boxes(
            geometry,
            {"g-00": "A", "g-01": "B", "g-10": "C", "g-11": "D"},
        ),
    )
    fusion_config = OcrFusionConfig(
        routing_mode=OcrRoutingMode.BLOCK_MEMBERSHIP,
        membership_assume_complete_observations=True,
    )
    fusion = OcrEvidenceFusion(fusion_config).fuse(
        plan=plan,
        segments=geometry.segmentation.segments,
        crops=crops,
        queue=queue,
    )
    fixture = _Fixture(
        geometry,
        objects,
        plan,
        crops,
        queue,
        fusion,
        page,
        planning_config,
    )

    ownership, ownership_segment_ids = _ownership_for_geometry(geometry)
    result = _assemble(
        fixture,
        fusion_config=fusion_config,
        ownership=ownership,
        ownership_segment_ids=ownership_segment_ids,
    )

    assert result.status is AssemblyStatus.COMPLETE
    assert result.text is not None
    assert compact_ocr_text(result.text) == "ABCD"
    assert result.objects[0].attribution_level is AttributionLevel.SEGMENT_GROUP
    assert tuple(item.segment_ids for item in result.structural_units) == (
        ("g-00", "g-01"),
        ("g-10", "g-11"),
    )
    assert all(item.text is not None for item in result.structural_units)
    assert {
        item.segment_ids for item in result.evidence_slices
    } == {("g-00", "g-01"), ("g-10", "g-11")}


def test_spatial_group_covering_whole_paragraph_is_one_structural_unit() -> None:
    geometry = _paragraph_geometry()
    objects = ObjectReconstructor().reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    planning_config = BlockPlanningConfig(
        mode=BlockPlanningMode.SPATIAL_2D,
        padding=0,
    )
    plan = OverlappingBlockPlanner(planning_config).plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
        matrix=geometry.matrix,
    )
    assert tuple(item.segment_ids for item in plan.membership_units) == (
        ("p-0", "p-1"),
    )
    page, crops = _crops(geometry, plan)
    _, aligned_rgb_sha256 = BlockCropper().crop_with_rgb_sha256(
        page,
        aligned_size=geometry.segmentation.aligned_size,
        plan=plan,
    )
    geometry = replace(geometry, aligned_rgb_sha256=aligned_rgb_sha256)
    queue = _queue(
        plan,
        crops,
        _word_boxes(geometry, {"p-0": "FIRST", "p-1": "SECOND"}),
    )
    fusion_config = OcrFusionConfig(
        routing_mode=OcrRoutingMode.BLOCK_MEMBERSHIP,
    )
    fusion = OcrEvidenceFusion(fusion_config).fuse(
        plan=plan,
        segments=geometry.segmentation.segments,
        crops=crops,
        queue=queue,
    )
    fixture = _Fixture(
        geometry,
        objects,
        plan,
        crops,
        queue,
        fusion,
        page,
        planning_config,
    )

    ownership, ownership_segment_ids = _ownership_for_geometry(geometry)
    result = _assemble(
        fixture,
        fusion_config=fusion_config,
        ownership=ownership,
        ownership_segment_ids=ownership_segment_ids,
    )

    assert result.status is AssemblyStatus.COMPLETE
    assert result.text is not None
    assert compact_ocr_text(result.candidate_text) == "FIRSTSECOND"
    assert tuple(item.segment_ids for item in result.structural_units) == (
        ("p-0", "p-1"),
    )
    assert result.structural_units[0].text == result.objects[0].text
    assert (
        "segment-group-grammar-unsplittable"
        not in result.objects[0].reasons
    )


def test_one_object_accepts_stable_raw_gamma_text_only_evidence() -> None:
    geometry = _paragraph_geometry()
    fixture = _fixture(
        geometry,
        _text_only({"block-000000": "Привет world 中文"}),
    )
    assert len(fixture.objects.objects) == 1
    assert fixture.objects.objects[0].kind is ObjectKind.PARAGRAPH
    assert fixture.fusion.status is OcrFusionStatus.UNRESOLVED
    assert fixture.fusion.observations == ()
    assert all(
        item.selected_text is None for item in fixture.fusion.segments
    )

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.COMPLETE
    assert result.text == "Привет world 中文"
    assert result.candidate_text == result.text
    assert len(result.objects) == 1
    assert result.objects[0].attribution_level is AttributionLevel.OBJECT
    assert result.objects[0].text == result.text
    assert all(item.text is None for item in result.segments)
    assert {item.transform for item in result.evidence_slices} == {
        OcrTransform.RAW,
        OcrTransform.GAMMA,
    }


def test_failed_scheduled_capability_blocks_otherwise_stable_object_text() -> None:
    geometry = _paragraph_geometry()
    objects = ObjectReconstructor().reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    plan = OverlappingBlockPlanner().plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
    )
    page, crops = _crops(geometry, plan)
    output = _text_only({"block-000000": "GOOD"})
    complete_queue = _queue_lanes(
        plan,
        crops,
        {"good-lane": output, "failed-lane": output},
    )
    queue = _fail_scheduled_lane(complete_queue, lane_id="failed-lane")
    fusion = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=geometry.segmentation.segments,
        crops=crops,
        queue=queue,
    )
    fixture = _Fixture(geometry, objects, plan, crops, queue, fusion, page)
    assert queue.status is OcrQueueStatus.PARTIAL
    assert queue.failed == 2

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    assert result.candidate_text == "GOOD"
    assert result.objects[0].text is None
    reasons = result.objects[0].reasons + result.diagnostics
    assert any(
        "queue" in item.lower() and "partial" in item.lower()
        for item in reasons
    )


def test_independent_text_and_bbox_capabilities_can_corroborate() -> None:
    geometry = _paragraph_geometry()
    objects = ObjectReconstructor().reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    plan = OverlappingBlockPlanner().plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
    )
    page, crops = _crops(geometry, plan)
    queue = _queue_lanes(
        plan,
        crops,
        {
            "text-lane": _text_only({"block-000000": "RIGHT TEXT"}),
            "bbox-lane": _word_boxes(
                geometry,
                {"p-0": "RIGHT", "p-1": "TEXT"},
            ),
        },
    )
    fusion = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=geometry.segmentation.segments,
        crops=crops,
        queue=queue,
    )
    fixture = _Fixture(geometry, objects, plan, crops, queue, fusion, page)
    assert fusion.status is OcrFusionStatus.COMPLETE

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.COMPLETE
    assert result.text == "RIGHT TEXT"
    assert result.objects[0].text == "RIGHT TEXT"
    assert result.objects[0].attribution_level is AttributionLevel.OBJECT


def test_contradictory_unresolved_segment_blocks_stable_object_text() -> None:
    geometry = _paragraph_geometry()
    fixture = _multi_lane_fixture(
        geometry,
        {
            "text-lane": _text_only({"block-000000": "RIGHT TEXT"}),
            "bbox-lane": _word_boxes(
                geometry,
                {"p-0": "WRONG"},
                confidence=0.1,
            ),
        },
    )
    first = fixture.fusion.segments[0]
    assert first.selected_text == "WRONG"
    assert first.unresolved
    assert "low_confidence" in first.uncertainty_reasons

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    assert result.objects[0].candidate_text == "RIGHT TEXT"
    assert result.objects[0].text is None
    assert "object-text-segment-conflict" in result.objects[0].reasons


def test_agreeing_unresolved_segments_allow_stable_object_text() -> None:
    geometry = _paragraph_geometry()
    fixture = _multi_lane_fixture(
        geometry,
        {
            "text-lane": _text_only({"block-000000": "RIGHT TEXT"}),
            "bbox-lane": _word_boxes(
                geometry,
                {"p-0": "RIGHT", "p-1": "TEXT"},
                confidence=0.1,
            ),
        },
    )
    assert tuple(
        item.selected_text for item in fixture.fusion.segments
    ) == ("RIGHT", "TEXT")
    assert all(item.unresolved for item in fixture.fusion.segments)

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.COMPLETE
    assert result.text == "RIGHT TEXT"
    assert result.objects[0].text == result.text
    assert result.objects[0].attribution_level is AttributionLevel.OBJECT
    assert all(item.text is None for item in result.segments)


def test_one_capability_changing_output_geometry_has_no_stable_pair() -> None:
    geometry = _paragraph_geometry()
    objects = ObjectReconstructor().reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    plan = OverlappingBlockPlanner().plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
    )
    page, crops = _crops(geometry, plan)
    text_only = _text_only({"block-000000": "RIGHT TEXT"})
    bbox = _word_boxes(
        geometry,
        {"p-0": "RIGHT", "p-1": "TEXT"},
    )

    def mixed_geometry(block: object, transform: OcrTransform) -> OcrEngineOutput:
        if transform is OcrTransform.RAW:
            return text_only(block, transform)
        return bbox(block, transform)

    queue = _queue(plan, crops, mixed_geometry)
    fusion = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=geometry.segmentation.segments,
        crops=crops,
        queue=queue,
    )
    fixture = _Fixture(geometry, objects, plan, crops, queue, fusion, page)
    assert fusion.status is OcrFusionStatus.UNRESOLVED

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    assert result.candidate_text == "RIGHT TEXT"
    assert result.objects[0].text is None
    reasons = result.objects[0].reasons + result.diagnostics
    assert any("missing-raw-gamma" in item for item in reasons), reasons


def test_exact_object_text_conflicting_with_complete_segments_is_unresolved() -> None:
    geometry = _paragraph_geometry()
    objects = ObjectReconstructor().reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    plan = OverlappingBlockPlanner().plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
    )
    page, crops = _crops(geometry, plan)
    queue = _queue_lanes(
        plan,
        crops,
        {
            "text-lane": _text_only({"block-000000": "OBJECT TEXT"}),
            "bbox-lane": _word_boxes(
                geometry,
                {"p-0": "DIFFERENT", "p-1": "WORDS"},
            ),
        },
    )
    fusion = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=geometry.segmentation.segments,
        crops=crops,
        queue=queue,
    )
    assert fusion.status is OcrFusionStatus.COMPLETE
    fixture = _Fixture(geometry, objects, plan, crops, queue, fusion, page)

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    assert result.objects[0].candidate_text == "OBJECT TEXT"
    assert result.objects[0].text is None
    assert any("conflict" in item for item in result.objects[0].reasons)


def test_object_text_conflicts_with_one_complete_target_segment() -> None:
    geometry = _paragraph_geometry()
    objects = ObjectReconstructor().reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    plan = OverlappingBlockPlanner().plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
    )
    page, crops = _crops(geometry, plan)
    queue = _queue_lanes(
        plan,
        crops,
        {
            "text-lane": _text_only({"block-000000": "WRONG"}),
            "bbox-lane": _word_boxes(geometry, {"p-0": "RIGHT"}),
        },
    )
    fusion = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=geometry.segmentation.segments,
        crops=crops,
        queue=queue,
    )
    fixture = _Fixture(geometry, objects, plan, crops, queue, fusion, page)
    assert fusion.status is OcrFusionStatus.UNRESOLVED
    assert fusion.segments[0].selected_text == "RIGHT"
    assert not fusion.segments[0].unresolved
    assert fusion.segments[1].selected_text is None

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    assert result.objects[0].candidate_text == "WRONG"
    assert result.objects[0].text is None
    assert any("conflict" in item for item in result.objects[0].reasons)


def test_text_only_block_covering_two_objects_never_guesses_a_split() -> None:
    geometry = _two_object_geometry()
    fixture = _fixture(
        geometry,
        _text_only({"block-000000": "first object second object"}),
    )
    assert len(fixture.objects.objects) == 2
    assert len(fixture.plan.blocks) == 1

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    assert result.candidate_text == "first object second object"
    assert all(item.text is None for item in result.objects)
    assert not any(
        item.attribution_level is AttributionLevel.OBJECT
        for item in result.evidence_slices
    )


def test_unattributable_fallback_markdown_equals_candidate_text() -> None:
    geometry = _two_object_geometry()
    fixture = _fixture(
        geometry,
        _text_only({"block-000000": "fallback text"}),
    )

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.candidate_text == "fallback text"
    assert result.candidate_markdown == result.candidate_text
    assert result.markdown is None
    assert all(not item.candidate_text for item in result.objects)
    assert result.evidence_slices
    block = fixture.plan.blocks[0]
    assert all(
        item.attribution_level is AttributionLevel.UNATTRIBUTABLE
        and item.object_id is None
        and item.block_id == block.block_id
        and item.segment_ids == block.segment_ids
        and item.text == result.candidate_text
        and item.output_start == 0
        and item.output_stop == len(result.candidate_text)
        for item in result.evidence_slices
    )
    assert all(not item.evidence_slice_ids for item in result.objects)
    assert all(not item.evidence_slice_ids for item in result.segments)
    assert all(not item.evidence_slice_ids for item in result.structural_units)


@pytest.mark.parametrize(
    ("geometry_factory", "texts", "kind", "unit_count"),
    (
        (
            _paragraph_geometry,
            {"p-0": "Привет", "p-1": "world中文"},
            ObjectKind.PARAGRAPH,
            2,
        ),
        (
            _list_geometry,
            {
                "marker-0": "1.",
                "item-0": "один",
                "marker-1": "2.",
                "item-1": "two",
                "marker-2": "3.",
                "item-2": "三",
            },
            ObjectKind.LIST,
            3,
        ),
        (
            _table_geometry,
            {"t-00": "A", "t-01": "Б", "t-10": "中", "t-11": "D"},
            ObjectKind.TABLE,
            4,
        ),
    ),
)
def test_segment_evidence_assembles_each_structural_kind_without_loss(
    geometry_factory: Callable[[], GeometryResult],
    texts: dict[str, str],
    kind: ObjectKind,
    unit_count: int,
) -> None:
    geometry = geometry_factory()
    fixture = _fixture(geometry, _word_boxes(geometry, texts))
    assert tuple(item.kind for item in fixture.objects.objects) == (kind,)
    assert fixture.fusion.status is OcrFusionStatus.COMPLETE

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.COMPLETE
    assert result.text is not None
    assert result.candidate_text == result.text
    _assert_exact_codepoints_in_order(result.text, tuple(texts.values()))
    assert all(
        item.attribution_level is AttributionLevel.SEGMENT
        for item in result.segments
    )
    expected_unit_ids = tuple(
        f"unit-{index:08d}" for index in range(unit_count)
    )
    assert len(result.structural_units) == unit_count
    assert all(item.kind is kind for item in result.structural_units)
    assert tuple(item.unit_id for item in result.structural_units) == expected_unit_ids
    assert result.objects[0].structural_unit_ids == expected_unit_ids
    unit_payload = "".join(
        item.candidate_text for item in result.structural_units
    )
    assert compact_ocr_text(unit_payload) == compact_ocr_text(
        result.objects[0].candidate_text
    )


def test_spanning_table_segment_is_emitted_once_not_once_per_sparse_cell() -> None:
    geometry = _table_geometry()
    spans = list(geometry.matrix.spans)
    cells = list(geometry.matrix.cells)
    target = next(item for item in spans if item.segment_id == "t-01")
    spans[spans.index(target)] = replace(target, column_start=1, column_stop=4)
    cells.extend(
        (
            SparseCell(1, 1, "t-01"),
            SparseCell(1, 2, "t-01"),
        )
    )
    matrix = replace(
        geometry.matrix,
        cells=tuple(sorted(cells, key=lambda item: (item.row, item.column, item.segment_id))),
        spans=tuple(spans),
    )
    geometry = replace(geometry, matrix=matrix)
    texts = {"t-00": "A", "t-01": "SPANNING", "t-10": "C", "t-11": "D"}
    fixture = _fixture(geometry, _word_boxes(geometry, texts))

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.COMPLETE
    assert result.text is not None
    assert result.text.count("SPANNING") == 1


def test_mixed_scripts_case_and_confusables_remain_exact_codepoints() -> None:
    geometry = _paragraph_geometry()
    observed = {"p-0": "AА pр eе", "p-1": "Русский EN 中文"}
    fixture = _fixture(geometry, _word_boxes(geometry, observed))

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.COMPLETE
    assert result.text is not None
    _assert_exact_codepoints_in_order(result.text, tuple(observed.values()))
    assert "AА" in result.text
    assert "pр" in result.text
    assert "eе" in result.text


def test_block_context_is_attributed_by_segment_ownership_not_block_object_ids() -> None:
    geometry = _two_object_geometry()
    fixture = _fixture(
        geometry,
        _word_boxes(geometry, {"a-0": "FIRST", "b-0": "SECOND"}),
        planning_config=BlockPlanningConfig(
            max_core_segments=1,
            max_block_pixels=20_000,
            context_segments=1,
            padding=0,
        ),
    )
    assert len(fixture.plan.blocks) == 2
    second = fixture.plan.blocks[1]
    assert second.context_segment_ids == ("a-0",)
    assert fixture.objects.segment_ownership[0].object_id not in second.object_ids

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.COMPLETE
    assert tuple(item.object_id for item in result.objects) == (
        "object-000000",
        "object-000001",
    )
    assert result.objects[0].text == "FIRST"
    assert result.objects[1].text == "SECOND"
    assert result.text is not None
    assert result.text.count("FIRST") == 1


def test_forged_object_result_is_rejected_by_reconstruction_boundary() -> None:
    geometry = _paragraph_geometry()
    fixture = _fixture(geometry, _word_boxes(geometry, {"p-0": "a", "p-1": "b"}))
    forged_object = replace(fixture.objects.objects[0], confidence=0.123)
    forged = replace(fixture.objects, objects=(forged_object,))

    with pytest.raises(DocumentAssemblyInvariantError, match="object|reconstruct|stage 6"):
        _assemble(fixture, objects=forged)


def test_forged_plan_is_rejected_even_when_plan_dataclass_accepts_it() -> None:
    geometry = _paragraph_geometry()
    fixture = _fixture(geometry, _word_boxes(geometry, {"p-0": "a", "p-1": "b"}))
    forged_block = replace(fixture.plan.blocks[0], object_ids=("object-forged",))
    forged = replace(fixture.plan, blocks=(forged_block,))

    with pytest.raises(DocumentAssemblyInvariantError, match="plan|block|stage 5"):
        _assemble(fixture, plan=forged)


def test_forged_block_bbox_is_rejected_after_regenerating_all_downstream_data() -> None:
    geometry = _paragraph_geometry()
    objects = ObjectReconstructor().reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    planning_config = BlockPlanningConfig(padding=0)
    canonical_plan = OverlappingBlockPlanner(planning_config).plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
    )
    assert canonical_plan.blocks[0].bbox == Box(0, 4, 128, 36)
    forged_block = replace(
        canonical_plan.blocks[0],
        bbox=Box(0, 3, 128, 37),
    )
    forged_plan = replace(canonical_plan, blocks=(forged_block,))
    forged_page, forged_crops = _crops(geometry, forged_plan)
    forged_queue = _queue(
        forged_plan,
        forged_crops,
        _word_boxes(geometry, {"p-0": "RIGHT", "p-1": "TEXT"}),
    )
    forged_fusion = OcrEvidenceFusion().fuse(
        plan=forged_plan,
        segments=geometry.segmentation.segments,
        crops=forged_crops,
        queue=forged_queue,
    )
    fixture = _Fixture(
        geometry,
        objects,
        forged_plan,
        forged_crops,
        forged_queue,
        forged_fusion,
        forged_page,
        planning_config,
    )

    with pytest.raises(
        DocumentAssemblyInvariantError,
        match="plan|bbox|stage 5",
    ):
        _assemble(fixture)


def test_forged_fusion_is_rejected_against_queue_evidence() -> None:
    geometry = _paragraph_geometry()
    fixture = _fixture(geometry, _word_boxes(geometry, {"p-0": "a", "p-1": "b"}))
    segment = fixture.fusion.segments[0]
    forged_segment = replace(segment, selected_text="forged")
    forged = replace(
        fixture.fusion,
        segments=(forged_segment, *fixture.fusion.segments[1:]),
    )

    with pytest.raises(DocumentAssemblyInvariantError, match="fusion|queue|stage 2"):
        _assemble(fixture, fusion=forged)


def test_crop_digest_mismatch_is_rejected_before_text_assembly() -> None:
    geometry = _paragraph_geometry()
    fixture = _fixture(geometry, _word_boxes(geometry, {"p-0": "a", "p-1": "b"}))
    _, unrelated_crops = _crops(geometry, fixture.plan, seed=999)

    with pytest.raises(DocumentAssemblyInvariantError, match="crop|digest|queue"):
        _assemble(fixture, crops=unrelated_crops)


def test_regenerated_downstream_crop_tree_cannot_be_bound_to_another_page() -> None:
    geometry = _paragraph_geometry()
    output = _word_boxes(geometry, {"p-0": "a", "p-1": "b"})
    fixture = _fixture(geometry, output, crop_seed=7)
    _, unrelated_crops = _crops(geometry, fixture.plan, seed=999)
    unrelated_queue = _queue(fixture.plan, unrelated_crops, output)
    unrelated_fusion = OcrEvidenceFusion().fuse(
        plan=fixture.plan,
        segments=geometry.segmentation.segments,
        crops=unrelated_crops,
        queue=unrelated_queue,
    )

    with pytest.raises(
        DocumentAssemblyInvariantError,
        match="page|crop|stage 4",
    ):
        _assemble(
            fixture,
            crops=unrelated_crops,
            queue=unrelated_queue,
            fusion=unrelated_fusion,
        )


def test_rebuilt_stage4_stage5_stage2_cannot_replace_geometry_page_pixels() -> None:
    geometry = _paragraph_geometry()
    output = _word_boxes(geometry, {"p-0": "a", "p-1": "b"})
    fixture = _fixture(geometry, output, crop_seed=7)
    rebuilt_plan = OverlappingBlockPlanner(fixture.planning_config).plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=fixture.objects,
    )
    assert rebuilt_plan == fixture.plan
    replacement_page, replacement_crops = _crops(
        geometry,
        rebuilt_plan,
        seed=999,
    )
    replacement_queue = _queue(rebuilt_plan, replacement_crops, output)
    replacement_fusion = OcrEvidenceFusion().fuse(
        plan=rebuilt_plan,
        segments=geometry.segmentation.segments,
        crops=replacement_crops,
        queue=replacement_queue,
    )

    with pytest.raises(
        DocumentAssemblyInvariantError,
        match="stage 1 geometry was not derived from the supplied aligned RGB page",
    ):
        _assemble(
            fixture,
            page=replacement_page,
            plan=rebuilt_plan,
            crops=replacement_crops,
            queue=replacement_queue,
            fusion=replacement_fusion,
        )


def test_unresolved_selected_text_remains_candidate_not_certified_output() -> None:
    geometry = _paragraph_geometry()
    observed = {"p-0": "low", "p-1": "confidence"}
    fixture = _fixture(
        geometry,
        _word_boxes(geometry, observed, confidence=0.1),
    )
    assert fixture.fusion.status is OcrFusionStatus.UNRESOLVED
    assert all(item.selected_text is not None for item in fixture.fusion.segments)

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    _assert_exact_codepoints_in_order(result.candidate_text, tuple(observed.values()))
    assert all(item.text is None for item in result.segments)


def test_repeated_anchor_alignment_ambiguity_stays_unresolved() -> None:
    fixture = _two_object_overlap_fixture(
        anchor_text="same",
        source_text="same same",
    )
    assert all(item.selected_text is None for item in fixture.fusion.segments)

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    assert compact_ocr_text(result.candidate_text) == "same"
    assert result.objects[1].text is None
    assert all(
        item.block_id == "block-000000" for item in result.evidence_slices
    )


def test_unique_overlap_or_xor_recovers_a_segment_group_without_guessing() -> None:
    fixture = _two_object_overlap_fixture(
        anchor_text="FIRST",
        source_text="FIRST SECOND",
    )
    assert fixture.plan.adjacent_algebra[0].intersection_segment_ids == ("a-0",)
    assert fixture.plan.adjacent_algebra[0].second_only_segment_ids == ("b-0",)

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.COMPLETE
    assert result.text is not None
    assert compact_ocr_text(result.text) == "FIRSTSECOND"
    assert result.objects[0].text == "FIRST"
    assert result.objects[0].attribution_level is AttributionLevel.OBJECT
    assert result.objects[1].text == "SECOND"
    assert result.objects[1].attribution_level is AttributionLevel.SEGMENT_GROUP
    recovered = tuple(
        item
        for item in result.evidence_slices
        if item.object_id == "object-000001"
    )
    assert recovered
    assert all(
        item.block_id == "block-000001"
        and item.segment_ids == ("b-0",)
        and item.attribution_level is AttributionLevel.SEGMENT_GROUP
        and item.text == "SECOND"
        for item in recovered
    )


def test_recovered_segment_group_conflicting_with_segments_is_unresolved() -> None:
    geometry = _two_object_geometry()
    objects = ObjectReconstructor().reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    planning_config = BlockPlanningConfig(
        max_core_segments=1,
        max_block_pixels=20_000,
        context_segments=1,
        padding=0,
    )
    plan = OverlappingBlockPlanner(planning_config).plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
    )
    page, crops = _crops(geometry, plan)
    text_only = _text_only(
        {
            "block-000000": "FIRST",
            "block-000001": "FIRST SECOND",
        }
    )
    bbox = _word_boxes(
        geometry,
        {"a-0": "FIRST", "b-0": "WRONG"},
    )
    queue = _queue_lanes(
        plan,
        crops,
        {"text-lane": text_only, "bbox-lane": bbox},
    )
    fusion = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=geometry.segmentation.segments,
        crops=crops,
        queue=queue,
    )
    assert fusion.status is OcrFusionStatus.COMPLETE
    fixture = _Fixture(
        geometry,
        objects,
        plan,
        crops,
        queue,
        fusion,
        page,
        planning_config,
    )

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    assert result.objects[1].candidate_text == "SECOND"
    assert result.objects[1].text is None
    assert any("conflict" in item for item in result.objects[1].reasons)


def test_recovered_group_conflicts_with_one_complete_target_segment() -> None:
    geometry = _partial_object_geometry()
    objects = ObjectReconstructor().reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    assert tuple(item.segment_ids for item in objects.objects) == (
        ("a-0",),
        ("b-0", "b-1"),
    )
    planning_config = BlockPlanningConfig(
        max_core_segments=2,
        max_block_pixels=20_000,
        context_segments=1,
        padding=0,
    )
    plan = OverlappingBlockPlanner(planning_config).plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
    )
    assert tuple(item.core_segment_ids for item in plan.blocks) == (
        ("a-0",),
        ("b-0", "b-1"),
    )
    assert plan.adjacent_algebra[0].second_only_segment_ids == ("b-0", "b-1")
    page, crops = _crops(geometry, plan)
    queue = _queue_lanes(
        plan,
        crops,
        {
            "text-lane": _text_only(
                {
                    "block-000000": "ANCHOR",
                    "block-000001": "ANCHOR GROUP",
                }
            ),
            "bbox-lane": _word_boxes(
                geometry,
                {"a-0": "ANCHOR", "b-0": "WRONG"},
            ),
        },
    )
    fusion = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=geometry.segmentation.segments,
        crops=crops,
        queue=queue,
    )
    fixture = _Fixture(
        geometry,
        objects,
        plan,
        crops,
        queue,
        fusion,
        page,
        planning_config,
    )
    assert fusion.status is OcrFusionStatus.UNRESOLVED
    assert fusion.segments[1].selected_text == "WRONG"
    assert not fusion.segments[1].unresolved
    assert fusion.segments[2].selected_text is None

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    assert result.objects[1].candidate_text == "GROUP"
    assert result.objects[1].text is None
    assert any("conflict" in item for item in result.objects[1].reasons)


def test_overlap_anchor_with_text_on_both_sides_is_not_subtracted() -> None:
    fixture = _two_object_overlap_fixture(
        anchor_text="ANCHOR",
        source_text="LEFT ANCHOR RIGHT",
    )

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    assert result.objects[0].candidate_text == "ANCHOR"
    assert result.objects[0].text is None
    assert result.objects[0].status is AssemblyStatus.UNRESOLVED
    assert "overlap-evidence-missing" in result.objects[0].reasons
    assert result.structural_units[0].candidate_text == "ANCHOR"
    assert result.structural_units[0].text is None
    assert result.structural_units[0].status is AssemblyStatus.UNRESOLVED
    assert result.objects[1].text is None
    assert all(
        item.attribution_level is not AttributionLevel.SEGMENT_GROUP
        for item in result.evidence_slices
    )


def test_overlap_source_without_exact_anchor_is_not_subtracted() -> None:
    fixture = _two_object_overlap_fixture(
        anchor_text="ANCHOR",
        source_text="OTHER SECOND",
    )

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    assert result.objects[0].candidate_text == "ANCHOR"
    assert result.objects[0].text is None
    assert result.objects[0].status is AssemblyStatus.UNRESOLVED
    assert "overlap-evidence-missing" in result.objects[0].reasons
    assert result.structural_units[0].candidate_text == "ANCHOR"
    assert result.structural_units[0].text is None
    assert result.structural_units[0].status is AssemblyStatus.UNRESOLVED
    assert result.objects[1].text is None
    assert all(
        item.attribution_level is not AttributionLevel.SEGMENT_GROUP
        for item in result.evidence_slices
    )


def test_overlap_residual_on_wrong_reading_side_is_not_recovered() -> None:
    fixture = _two_object_overlap_fixture(
        anchor_text="ANCHOR",
        source_text="SECOND ANCHOR",
    )
    assert tuple(item.reading_index for item in fixture.objects.objects) == (0, 1)

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    assert result.objects[1].text is None
    assert all(
        item.attribution_level is not AttributionLevel.SEGMENT_GROUP
        for item in result.evidence_slices
    )


@pytest.mark.parametrize(
    "source_text",
    ("ANCHORSECOND", "FIRSTANCHOR"),
)
def test_token_internal_overlap_anchor_is_not_subtracted(
    source_text: str,
) -> None:
    fixture = _two_object_overlap_fixture(
        anchor_text="ANCHOR",
        source_text=source_text,
    )

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    assert result.objects[1].text is None
    assert all(
        item.attribution_level is not AttributionLevel.SEGMENT_GROUP
        for item in result.evidence_slices
    )


def test_overlap_residual_that_is_only_part_of_an_object_is_not_certified() -> None:
    geometry = _partial_object_geometry()
    objects = ObjectReconstructor().reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    assert tuple(item.segment_ids for item in objects.objects) == (
        ("a-0",),
        ("b-0", "b-1"),
    )
    planning_config = BlockPlanningConfig(
        max_core_segments=1,
        max_block_pixels=20_000,
        context_segments=1,
        padding=0,
    )
    plan = OverlappingBlockPlanner(planning_config).plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
    )
    assert tuple(item.core_segment_ids for item in plan.blocks) == (
        ("a-0",),
        ("b-0",),
        ("b-1",),
    )
    assert plan.adjacent_algebra[0].second_only_segment_ids == ("b-0",)
    page, crops = _crops(geometry, plan)
    text_only = _text_only(
        {
            "block-000000": "ANCHOR",
            "block-000001": "ANCHOR PART",
        }
    )
    low_confidence = _word_boxes(
        geometry,
        {"b-1": "REST"},
        confidence=0.1,
    )

    def outputs(block: object, transform: OcrTransform) -> OcrEngineOutput:
        if getattr(block, "block_id") == "block-000002":
            return low_confidence(block, transform)
        return text_only(block, transform)

    queue = _queue(plan, crops, outputs)
    fusion = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=geometry.segmentation.segments,
        crops=crops,
        queue=queue,
    )
    fixture = _Fixture(
        geometry,
        objects,
        plan,
        crops,
        queue,
        fusion,
        page,
        planning_config,
    )

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    assert result.objects[1].text is None
    assert result.objects[1].attribution_level is AttributionLevel.SEGMENT
    assert all(
        item.attribution_level is not AttributionLevel.SEGMENT_GROUP
        for item in result.evidence_slices
    )


def test_limits_are_checked_before_alignment_or_output_construction() -> None:
    geometry = _paragraph_geometry()
    fixture = _fixture(geometry, _word_boxes(geometry, {"p-0": "a", "p-1": "b"}))
    assembler = DocumentAssembler(DocumentAssemblyConfig(max_segments=1))

    with pytest.raises(DocumentAssemblyLimitError, match="segment|limit"):
        assembler.assemble(
            geometry=fixture.geometry,
            objects=fixture.objects,
            plan=fixture.plan,
            crops=fixture.crops,
            queue=fixture.queue,
            fusion=fixture.fusion,
            page=fixture.page,
            planning_config=fixture.planning_config,
        )


def test_public_assembly_api_has_no_reference_or_expected_text_channel() -> None:
    signature = inspect.signature(DocumentAssembler.assemble)
    forbidden = ("reference", "expected", "ground_truth", "truth", "answer")

    assert not any(
        token in name.lower()
        for name in signature.parameters
        for token in forbidden
    )


def test_interleaved_segment_owners_preserve_object_reading_order() -> None:
    geometry = _two_column_geometry()
    texts = {
        "left-0": "L0",
        "right-0": "R0",
        "left-1": "L1",
        "right-1": "R1",
        "left-2": "L2",
        "right-2": "R2",
    }
    fixture = _fixture(geometry, _word_boxes(geometry, texts))
    assert len(fixture.objects.objects) == 2
    owners = tuple(item.object_id for item in fixture.objects.segment_ownership)
    assert owners == (
        "object-000000",
        "object-000001",
        "object-000000",
        "object-000001",
        "object-000000",
        "object-000001",
    )

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.COMPLETE
    assert tuple(item.object_id for item in result.objects) == (
        "object-000000",
        "object-000001",
    )
    assert compact_ocr_text(result.objects[0].text or "") == "L0L1L2"
    assert compact_ocr_text(result.objects[1].text or "") == "R0R1R2"
    assert result.text is not None
    assert compact_ocr_text(result.text) == "L0L1L2R0R1R2"


def test_every_non_whitespace_output_codepoint_has_exact_evidence_provenance() -> None:
    geometry = _list_geometry()
    texts = {
        "marker-0": "•",
        "item-0": "AА",
        "marker-1": "-",
        "item-1": "中文",
        "marker-2": "3.",
        "item-2": "тест",
    }
    fixture = _fixture(geometry, _word_boxes(geometry, texts))

    result = _assemble(fixture)

    assert result.status is AssemblyStatus.COMPLETE
    assert result.text is not None
    certified = compact_ocr_text(result.text)
    covered = [False] * len(certified)
    for evidence in result.evidence_slices:
        assert evidence.output_start < evidence.output_stop
        assert compact_ocr_text(
            result.text[evidence.output_start : evidence.output_stop]
        ) == compact_ocr_text(evidence.text)
        for index in range(evidence.output_start, evidence.output_stop):
            if not result.text[index].isspace():
                covered[len(compact_ocr_text(result.text[:index]))] = True
    assert all(covered)
