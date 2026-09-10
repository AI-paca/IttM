from __future__ import annotations

import hashlib
import io
from dataclasses import FrozenInstanceError, replace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.sparse_pipeline import SparsePipelineEvidence as LazyEvidenceExport
from app.sparse_pipeline.block_crops import (
    BlockCropConfig,
    BlockCropper,
)
from app.sparse_pipeline.block_planning import (
    BlockPlan,
    BlockSetAlgebra,
    BlockPlanningMode,
    BlockPlanningConfig,
    MembershipUnit,
    MembershipUnitKind,
    OverlappingBlockPlanner,
    RecognitionBlock,
    sparse_matrix_sha256,
)
from app.sparse_pipeline.contracts import (
    AffineTransform,
    AlignmentTrace,
    AxisInterval,
    Box,
    GeometryResult,
    RecursiveNode,
    Segment,
    SegmentationResult,
    SegmentKind,
    SegmentSpan,
    SparseCell,
    SparseSegmentMatrix,
    StopReason,
)
from app.sparse_pipeline.crop_enhancement import (
    CropEnhancementConfig,
    CropInput,
    EnhancementBackend,
    GammaDarkCropEnhancer,
)
from app.sparse_pipeline.object_reconstruction import (
    DocumentObject,
    ObjectKind,
    ObjectReconstructionConfig,
    ObjectReconstructionResult,
    ObjectReconstructor,
    SegmentObjectOwnership,
)
from app.sparse_pipeline.ocr_adapter_contracts import (
    OcrOutputGeometry,
    OcrResource,
)
from app.sparse_pipeline.ocr_fusion import (
    OcrEvidenceFusion,
    OcrFusionConfig,
    OcrRoutingMode,
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
from app.sparse_pipeline.pipeline_evidence import (
    SparsePipelineEvidence,
    SparsePipelineEvidenceInvariantError,
    _validate_segment_handoff,
)


def _page(*, foreground: tuple[int, int, int] = (0, 0, 0)) -> CropInput:
    output = io.BytesIO()
    image = Image.new("RGB", (64, 32), "white")
    try:
        ImageDraw.Draw(image).rectangle((8, 7, 31, 16), fill=foreground)
        image.save(output, format="PNG", compress_level=1)
    finally:
        image.close()
    return CropInput("evidence-page", output.getvalue())


def _geometry(page: CropInput) -> GeometryResult:
    segment = Segment(
        segment_id="segment-000000",
        bbox=Box(8, 7, 32, 17),
        source_bbox=Box(8, 7, 32, 17),
        kind=SegmentKind.TEXT,
        ink_pixels=240,
        row_index=0,
        order_key=(0, 0),
        parent_path=("geo-root",),
        component_ids=(0,),
    )
    aligned_size = (64, 32)
    with Image.open(io.BytesIO(page.png_bytes)) as opened:
        aligned_rgb_sha256 = hashlib.sha256(opened.convert("RGB").tobytes()).hexdigest()
    return GeometryResult(
        alignment=AlignmentTrace(
            transform=AffineTransform.identity(aligned_size),
            correction_degrees=0.0,
            background_rgb=(255, 255, 255),
            content_bbox=segment.bbox,
            foreground_pixels=segment.ink_pixels,
        ),
        segmentation=SegmentationResult(
            segments=(segment,),
            rules=(),
            nodes=(
                RecursiveNode(
                    node_id="geo-root",
                    bbox=Box(0, 0, *aligned_size),
                    depth=0,
                    parent_id=None,
                    segment_ids=(segment.segment_id,),
                    stop_reason=StopReason.ATOMIC,
                ),
            ),
            root_node_id="geo-root",
            aligned_size=aligned_size,
            foreground_pixels=segment.ink_pixels,
        ),
        matrix=SparseSegmentMatrix(
            rows=(AxisInterval(0, 0, aligned_size[1]),),
            columns=(AxisInterval(0, 0, aligned_size[0]),),
            cells=(SparseCell(0, 0, segment.segment_id),),
            spans=(SegmentSpan(segment.segment_id, 0, 1, 0, 1),),
        ),
        aligned_rgb_sha256=aligned_rgb_sha256,
    )


def _object_result_for_segments(
    segments: tuple[Segment, ...],
    *,
    aligned_size: tuple[int, int],
    single_object: bool = False,
) -> ObjectReconstructionResult:
    objects = (
        (
            DocumentObject(
                object_id="object-000000",
                kind=ObjectKind.PARAGRAPH,
                segment_ids=tuple(item.segment_id for item in segments),
                bbox=Box.union(item.bbox for item in segments),
                reading_index=0,
                row_start=min(item.row_index for item in segments),
                row_stop=max(item.row_index for item in segments) + 1,
                column_start=0,
                column_stop=len(segments),
                confidence=1.0,
            ),
        )
        if single_object
        else tuple(
            DocumentObject(
                object_id=f"object-{index:06d}",
                kind=ObjectKind.PARAGRAPH,
                segment_ids=(segment.segment_id,),
                bbox=segment.bbox,
                reading_index=index,
                row_start=segment.row_index,
                row_stop=segment.row_index + 1,
                column_start=index,
                column_stop=index + 1,
                confidence=1.0,
            )
            for index, segment in enumerate(segments)
        )
    )
    return ObjectReconstructionResult(
        aligned_size=aligned_size,
        source_segment_ids=tuple(item.segment_id for item in segments),
        objects=objects,
        segment_ownership=tuple(
            SegmentObjectOwnership(
                segment.segment_id,
                objects[0 if single_object else index].object_id,
            )
            for index, segment in enumerate(segments)
        ),
    )


def _evidence() -> SparsePipelineEvidence:
    page = _page()
    geometry = _geometry(page)
    object_config = ObjectReconstructionConfig()
    objects = ObjectReconstructor(object_config).reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    planning_config = BlockPlanningConfig()
    plan = OverlappingBlockPlanner(planning_config).plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
    )
    crop_config = BlockCropConfig(
        enhancement_backend=EnhancementBackend.NUMPY,
    )
    crops, page_rgb_sha256 = BlockCropper(crop_config).crop_with_rgb_sha256(
        page,
        aligned_size=geometry.segmentation.aligned_size,
        plan=plan,
    )
    assert page_rgb_sha256 == geometry.aligned_rgb_sha256

    crop = crops[0]
    segment = geometry.segmentation.segments[0]
    word_bbox = Box(
        segment.bbox.left - crop.bbox.left,
        segment.bbox.top - crop.bbox.top,
        segment.bbox.right - crop.bbox.left,
        segment.bbox.bottom - crop.bbox.top,
    )
    jobs = []
    context_sha256 = hashlib.sha256(crop.raw.png_bytes).hexdigest()
    for index, transform in enumerate((OcrTransform.RAW, OcrTransform.GAMMA)):
        payload = crop.raw.png_bytes if transform is OcrTransform.RAW else crop.gamma.png_bytes
        jobs.append(
            OcrJobResult(
                job_id=f"ocr-job-{index:08d}",
                block_id=crop.block_id,
                transform=transform,
                lane_id="test-lane",
                resource=OcrResource.CPU,
                status=OcrJobStatus.COMPLETE,
                output=OcrEngineOutput(
                    text="evidence",
                    words=(OcrWord("evidence", word_bbox, 0.99),),
                    geometry=OcrOutputGeometry.WORD_BOXES,
                ),
                error_type=None,
                error_message=None,
                elapsed_seconds=0.01,
                input_sha256=hashlib.sha256(payload).hexdigest(),
                context_sha256=context_sha256,
                capability_id="test-capability",
            )
        )
    queue = OcrQueueResult(
        jobs=tuple(jobs),
        status=OcrQueueStatus.COMPLETE,
        complete=len(jobs),
        failed=0,
    )
    fusion_config = OcrFusionConfig()
    fusion = OcrEvidenceFusion(fusion_config).fuse(
        plan=plan,
        segments=geometry.segmentation.segments,
        crops=crops,
        queue=queue,
    )
    stage4_config = CropEnhancementConfig(
        backend=EnhancementBackend.NUMPY,
    )
    stage4 = GammaDarkCropEnhancer(stage4_config).enhance(CropInput("stage4-page", page.png_bytes))
    return SparsePipelineEvidence(
        page=page,
        geometry=geometry,
        objects=objects,
        plan=plan,
        crops=crops,
        queue=queue,
        fusion=fusion,
        stage4=stage4,
        object_config=object_config,
        planning_config=planning_config,
        crop_config=crop_config,
        fusion_config=fusion_config,
        stage4_config=stage4_config,
    )


def _overlapping_bbox_spatial_evidence(
    *,
    single_object: bool = False,
) -> SparsePipelineEvidence:
    """Build full evidence where foreign bbox overlap needs literal masking."""

    from tests.sparse_pipeline.test_block_planning_stage5 import (
        _matrix_for_segments,
    )
    from tests.sparse_pipeline.test_document_artifacts_stage7 import _queue

    aligned_size = (40, 40)
    segments = (
        Segment(
            segment_id="segment-000000",
            bbox=Box(5, 5, 25, 25),
            source_bbox=Box(5, 5, 25, 25),
            kind=SegmentKind.TEXT,
            ink_pixels=1,
            row_index=0,
            order_key=(5, 5),
            parent_path=("geo-root",),
            component_ids=(0,),
        ),
        Segment(
            segment_id="segment-000001",
            bbox=Box(15, 15, 35, 35),
            source_bbox=Box(15, 15, 35, 35),
            kind=SegmentKind.TEXT,
            ink_pixels=1,
            row_index=1,
            order_key=(15, 15),
            parent_path=("geo-root",),
            component_ids=(1,),
        ),
    )
    segment_ids = tuple(item.segment_id for item in segments)
    matrix = _matrix_for_segments(segments, aligned_size=aligned_size)
    objects = _object_result_for_segments(
        segments,
        aligned_size=aligned_size,
        single_object=single_object,
    )
    planning_config = BlockPlanningConfig(
        mode=BlockPlanningMode.SPATIAL_2D,
        padding=0,
    )
    if single_object:
        owner = objects.objects[0].object_id
        scope = "scope-000000"
        plan = BlockPlan(
            aligned_size=aligned_size,
            source_segment_ids=segment_ids,
            blocks=(
                RecognitionBlock(
                    block_id="block-000000",
                    bbox=segments[0].bbox,
                    core_segment_ids=(segment_ids[0],),
                    segment_ids=(segment_ids[0],),
                    context_segment_ids=(),
                    object_ids=(owner,),
                    scope_id=scope,
                ),
                RecognitionBlock(
                    block_id="block-000001",
                    bbox=segments[1].bbox,
                    core_segment_ids=(segment_ids[1],),
                    segment_ids=(segment_ids[1],),
                    context_segment_ids=(),
                    object_ids=(owner,),
                    scope_id=scope,
                ),
                RecognitionBlock(
                    block_id="block-000002",
                    bbox=Box.union(item.bbox for item in segments),
                    core_segment_ids=(),
                    segment_ids=segment_ids,
                    context_segment_ids=segment_ids,
                    object_ids=(),
                    scope_id=scope,
                ),
            ),
            adjacent_algebra=(
                BlockSetAlgebra(
                    first_block_id="block-000000",
                    second_block_id="block-000002",
                    intersection_segment_ids=(segment_ids[0],),
                    union_segment_ids=segment_ids,
                    xor_segment_ids=(segment_ids[1],),
                    first_only_segment_ids=(),
                    second_only_segment_ids=(segment_ids[1],),
                ),
                BlockSetAlgebra(
                    first_block_id="block-000001",
                    second_block_id="block-000002",
                    intersection_segment_ids=(segment_ids[1],),
                    union_segment_ids=segment_ids,
                    xor_segment_ids=(segment_ids[0],),
                    first_only_segment_ids=(),
                    second_only_segment_ids=(segment_ids[0],),
                ),
            ),
            mode=BlockPlanningMode.SPATIAL_2D,
            membership_units=(
                MembershipUnit(
                    unit_id="membership-unit-000000",
                    kind=MembershipUnitKind.SEGMENT,
                    segment_ids=(segment_ids[0],),
                    block_ids=("block-000000", "block-000002"),
                    scope_id=scope,
                ),
                MembershipUnit(
                    unit_id="membership-unit-000001",
                    kind=MembershipUnitKind.SEGMENT,
                    segment_ids=(segment_ids[1],),
                    block_ids=("block-000001", "block-000002"),
                    scope_id=scope,
                ),
            ),
            matrix_sha256=sparse_matrix_sha256(matrix),
        )
    else:
        plan = OverlappingBlockPlanner(planning_config).plan(
            aligned_size=aligned_size,
            segments=segments,
            objects_result=objects,
            matrix=matrix,
        )
        assert tuple(item.segment_ids for item in plan.blocks) == (
            (segment_ids[0],),
            (segment_ids[1],),
        )

    ownership = np.full((40, 40), -1, dtype=np.int32)
    # Both labels reach their conservative bbox bounds, but never own the same
    # pixel.  One segment-1 pixel lies inside segment-0's bbox and must be
    # whitened from block 0 rather than forcing both objects into one block.
    ownership[5, 5:25] = 0
    ownership[24, 5:15] = 0
    ownership[5:25, 5] = 0
    ownership[34, 15:35] = 1
    ownership[15, 25:35] = 1
    ownership[15:35, 34] = 1
    ownership[25:35, 15] = 1
    ownership[20, 20] = 1
    pixels = np.full((40, 40, 3), 255, dtype=np.uint8)
    pixels[ownership >= 0] = 0
    output = io.BytesIO()
    image = Image.fromarray(pixels, mode="RGB")
    try:
        image.save(output, format="PNG", compress_level=1)
    finally:
        image.close()
    page = CropInput("overlapping-object-page", output.getvalue())

    foreground_pixels = int(np.count_nonzero(ownership >= 0))
    segments = tuple(
        replace(
            segment,
            ink_pixels=int(np.count_nonzero(ownership == index)),
        )
        for index, segment in enumerate(segments)
    )
    geometry = GeometryResult(
        alignment=AlignmentTrace(
            transform=AffineTransform.identity(aligned_size),
            correction_degrees=0.0,
            background_rgb=(255, 255, 255),
            content_bbox=Box.union(item.bbox for item in segments),
            foreground_pixels=foreground_pixels,
        ),
        segmentation=SegmentationResult(
            segments=segments,
            rules=(),
            nodes=(
                RecursiveNode(
                    node_id="geo-root",
                    bbox=Box(0, 0, *aligned_size),
                    depth=0,
                    parent_id=None,
                    segment_ids=segment_ids,
                    stop_reason=StopReason.ATOMIC,
                ),
            ),
            root_node_id="geo-root",
            aligned_size=aligned_size,
            foreground_pixels=foreground_pixels,
        ),
        matrix=matrix,
        aligned_rgb_sha256=hashlib.sha256(pixels.tobytes()).hexdigest(),
    )
    crop_config = BlockCropConfig(
        enhancement_backend=EnhancementBackend.NUMPY,
    )
    crops, aligned_rgb_sha256 = BlockCropper(crop_config).crop_with_rgb_sha256(
        page,
        aligned_size=aligned_size,
        plan=plan,
        ownership=ownership,
        ownership_segment_ids=segment_ids,
    )
    assert aligned_rgb_sha256 == geometry.aligned_rgb_sha256
    assert crops[0].masked_segment_ids == (segment_ids[1],)
    assert crops[0].isolation_mask_png is not None

    queue = _queue(
        geometry=geometry,
        plan=plan,
        crops=crops,
        text_by_segment={segment_ids[0]: "alpha", segment_ids[1]: "beta"},
        partial=False,
    )
    fusion_config = OcrFusionConfig(
        routing_mode=OcrRoutingMode.BLOCK_MEMBERSHIP,
        membership_assume_complete_observations=True,
    )
    fusion = OcrEvidenceFusion(fusion_config).fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=queue,
    )
    return SparsePipelineEvidence(
        page=page,
        geometry=geometry,
        objects=objects,
        plan=plan,
        crops=crops,
        queue=queue,
        fusion=fusion,
        planning_config=planning_config,
        crop_config=crop_config,
        fusion_config=fusion_config,
        ownership=ownership,
        ownership_segment_ids=segment_ids,
    )


def test_happy_path_is_frozen_and_lazily_exported() -> None:
    evidence = _evidence()

    assert LazyEvidenceExport is SparsePipelineEvidence
    assert evidence.geometry.aligned_rgb_sha256
    assert evidence.fusion.source_segment_ids == evidence.plan.source_segment_ids
    with pytest.raises(FrozenInstanceError):
        evidence.page = _page()  # type: ignore[misc]


def test_spatial_evidence_accepts_foreign_bbox_overlap_after_exact_isolation() -> None:
    evidence = _overlapping_bbox_spatial_evidence()

    assert evidence.plan.mode is BlockPlanningMode.SPATIAL_2D
    assert evidence.crops[0].masked_segment_ids == ("segment-000001",)
    assert evidence.crops[0].isolation_mask_png is not None
    assert evidence.ownership is not None
    assert evidence.ownership.flags.writeable is False


def test_spatial_evidence_accepts_same_object_nonmember_exact_isolation() -> None:
    evidence = _overlapping_bbox_spatial_evidence(single_object=True)

    owner_by_segment = {item.segment_id: item.object_id for item in evidence.objects.segment_ownership}
    masked = evidence.crops[0].masked_segment_ids
    assert masked == ("segment-000001",)
    assert evidence.crops[0].isolation_mask_png is not None
    assert owner_by_segment[masked[0]] == owner_by_segment["segment-000000"]


def test_spatial_evidence_rejects_isolation_replayed_against_forged_ownership() -> None:
    evidence = _overlapping_bbox_spatial_evidence()
    assert evidence.ownership is not None
    forged_ownership = np.array(evidence.ownership, copy=True)
    # Remove the exact foreign pixel which justified crop 0's recorded mask.
    # The stale bbox-only gate would not detect this; literal replay must.
    forged_ownership[20, 20] = -1

    with pytest.raises(
        SparsePipelineEvidenceInvariantError,
        match="isolation disagrees with Stage 1 ownership",
    ):
        replace(evidence, ownership=forged_ownership)


def test_stage1_tree_order_may_differ_but_downstream_document_order_may_not() -> None:
    _validate_segment_handoff(
        ("segment-a", "segment-b"),
        ("segment-b", "segment-a"),
        ("segment-b", "segment-a"),
        ("segment-b", "segment-a"),
    )

    with pytest.raises(
        SparsePipelineEvidenceInvariantError,
        match="identifiers disagree",
    ):
        _validate_segment_handoff(
            ("segment-a", "segment-b"),
            ("segment-b", "segment-forged"),
            ("segment-b", "segment-forged"),
            ("segment-b", "segment-forged"),
        )
    with pytest.raises(
        SparsePipelineEvidenceInvariantError,
        match="canonical segment order",
    ):
        _validate_segment_handoff(
            ("segment-a", "segment-b"),
            ("segment-b", "segment-a"),
            ("segment-a", "segment-b"),
            ("segment-b", "segment-a"),
        )


def test_optional_stage4_may_be_omitted() -> None:
    evidence = _evidence()

    without_stage4 = replace(
        evidence,
        stage4=None,
        stage4_config=None,
    )

    assert without_stage4.stage4 is None


def test_rejects_same_size_page_from_another_run() -> None:
    evidence = _evidence()
    foreign_page = _page(foreground=(120, 0, 0))

    with pytest.raises(
        SparsePipelineEvidenceInvariantError,
        match="aligned RGB digest",
    ):
        replace(evidence, page=foreign_page)


def test_rejects_forged_queue_crop_digest() -> None:
    evidence = _evidence()
    forged_job = replace(
        evidence.queue.jobs[0],
        input_sha256="f" * 64,
    )
    forged_queue = replace(
        evidence.queue,
        jobs=(forged_job, *evidence.queue.jobs[1:]),
    )

    with pytest.raises(
        SparsePipelineEvidenceInvariantError,
        match="input digest",
    ):
        replace(evidence, queue=forged_queue)


def test_rejects_forged_optional_stage4_source() -> None:
    evidence = _evidence()
    assert evidence.stage4 is not None
    forged_stage4 = replace(evidence.stage4, source_sha256="f" * 64)

    with pytest.raises(
        SparsePipelineEvidenceInvariantError,
        match="not derived from the page",
    ):
        replace(evidence, stage4=forged_stage4)


def test_validation_does_not_rerun_stage_algorithms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _evidence()

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("pipeline algorithm was re-run")

    monkeypatch.setattr(ObjectReconstructor, "reconstruct", forbidden)
    monkeypatch.setattr(OverlappingBlockPlanner, "plan", forbidden)
    monkeypatch.setattr(BlockCropper, "crop_with_rgb_sha256", forbidden)
    monkeypatch.setattr(OcrEvidenceFusion, "fuse", forbidden)
    monkeypatch.setattr(GammaDarkCropEnhancer, "enhance", forbidden)

    assert replace(evidence) == evidence


def test_rejects_plan_relabelled_with_a_stricter_block_pixel_limit() -> None:
    evidence = _evidence()
    assert evidence.planning_config is not None
    largest_block_area = max(block.bbox.area for block in evidence.plan.blocks)
    assert largest_block_area > 1
    stricter = replace(
        evidence.planning_config,
        max_block_pixels=largest_block_area - 1,
    )

    with pytest.raises(
        SparsePipelineEvidenceInvariantError,
        match="pixel footprint",
    ):
        replace(evidence, planning_config=stricter)


def test_rejects_plan_relabelled_with_a_stricter_pair_membership_limit() -> None:
    from tests.sparse_pipeline.test_tutorial_artifacts import _fixture

    evidence = _fixture().evidence
    assert evidence.planning_config is not None
    pair_memberships = sum(len(item.union_segment_ids) for item in evidence.plan.adjacent_algebra)
    assert pair_memberships > 1
    stricter = replace(
        evidence.planning_config,
        max_pair_memberships=pair_memberships - 1,
    )

    with pytest.raises(
        SparsePipelineEvidenceInvariantError,
        match="pair memberships",
    ):
        replace(evidence, planning_config=stricter)


def test_spatial_plan_roundtrip_validates_clamped_non_cascading_padding() -> None:
    from tests.sparse_pipeline.test_block_planning_stage5 import (
        _matrix_for_segments,
    )

    segments = tuple(
        Segment(
            segment_id=f"segment-{index:06d}",
            bbox=Box(left, 5, left + 10, 15),
            source_bbox=Box(left, 5, left + 10, 15),
            kind=SegmentKind.TEXT,
            ink_pixels=100,
            row_index=0,
            order_key=(5, left),
            parent_path=("geo-root",),
            component_ids=(index,),
        )
        for index, left in enumerate((5, 20, 35))
    )
    source_ids = tuple(item.segment_id for item in segments)
    object_result = _object_result_for_segments(
        segments,
        aligned_size=(50, 20),
        single_object=True,
    )
    config = BlockPlanningConfig(
        mode=BlockPlanningMode.SPATIAL_2D,
        spatial_rows=2,
        spatial_columns=2,
        spatial_row_overlap=1,
        spatial_column_overlap=1,
        max_core_segments=4,
        padding=10,
    )
    plan = OverlappingBlockPlanner(config).plan(
        aligned_size=(50, 20),
        segments=segments,
        objects_result=object_result,
        matrix=_matrix_for_segments(segments, aligned_size=(50, 20)),
    )

    # Naive padding would end at x=40 and expose segment-000002.  The spatial
    # crop stops at its half-open left boundary without adding it as a member.
    block_index, target = next(
        (index, block) for index, block in enumerate(plan.blocks) if block.segment_ids == source_ids[:2]
    )
    assert target.bbox == Box(0, 0, 35, 20)
    validator = object.__new__(SparsePipelineEvidence)
    object.__setattr__(validator, "plan", plan)
    validator._validate_plan(
        segments=segments,
        owner_by_segment={item.segment_id: item.object_id for item in object_result.segment_ownership},
        planning_config=config,
        matrix=_matrix_for_segments(segments, aligned_size=(50, 20)),
    )

    object.__setattr__(
        validator,
        "plan",
        replace(plan, matrix_sha256="0" * 64),
    )
    with pytest.raises(
        SparsePipelineEvidenceInvariantError,
        match="matrix provenance|Stage 1",
    ):
        validator._validate_plan(
            segments=segments,
            owner_by_segment={item.segment_id: item.object_id for item in object_result.segment_ownership},
            planning_config=config,
            matrix=_matrix_for_segments(segments, aligned_size=(50, 20)),
        )
    object.__setattr__(validator, "plan", plan)

    naive_block = replace(target, bbox=Box(0, 0, 40, 20))
    forged_blocks = list(plan.blocks)
    forged_blocks[block_index] = naive_block
    forged_plan = replace(plan, blocks=tuple(forged_blocks))
    object.__setattr__(validator, "plan", forged_plan)
    with pytest.raises(
        SparsePipelineEvidenceInvariantError,
        match="bbox disagrees",
    ):
        validator._validate_plan(
            segments=segments,
            owner_by_segment={item.segment_id: item.object_id for item in object_result.segment_ownership},
            planning_config=config,
            matrix=_matrix_for_segments(segments, aligned_size=(50, 20)),
        )


def test_spatial_evidence_preserves_unseparable_matrix_as_subblock() -> None:
    from tests.sparse_pipeline.test_block_planning_stage5 import (
        _matrix_for_segments,
    )

    regular = tuple(
        Segment(
            segment_id=f"segment-{index:06d}",
            bbox=Box(5 + index * 12, 5, 15 + index * 12, 15),
            source_bbox=Box(5 + index * 12, 5, 15 + index * 12, 15),
            kind=SegmentKind.TEXT,
            ink_pixels=100,
            row_index=0,
            order_key=(5, 5 + index * 12),
            parent_path=("geo-root",),
            component_ids=(index,),
        )
        for index in range(10)
    )
    merged = Segment(
        segment_id="segment-000010",
        bbox=Box(5, 20, 123, 30),
        source_bbox=Box(5, 20, 123, 30),
        kind=SegmentKind.TEXT,
        ink_pixels=1_180,
        row_index=1,
        order_key=(20, 5),
        parent_path=("geo-root",),
        component_ids=(10,),
    )
    segments = (*regular, merged)
    object_result = _object_result_for_segments(
        segments,
        aligned_size=(130, 40),
        single_object=True,
    )
    config = BlockPlanningConfig(
        mode=BlockPlanningMode.SPATIAL_2D,
        spatial_rows=2,
        spatial_columns=5,
        spatial_row_overlap=1,
        spatial_column_overlap=1,
        max_core_segments=20,
        max_block_pixels=100_000,
        max_pair_memberships=100_000,
        padding=8,
    )
    matrix = _matrix_for_segments(segments, aligned_size=(130, 40))
    plan = OverlappingBlockPlanner(config).plan(
        aligned_size=(130, 40),
        segments=segments,
        objects_result=object_result,
        matrix=matrix,
    )

    assert any(len(item.segment_ids) > 1 for item in plan.membership_units)
    validator = object.__new__(SparsePipelineEvidence)
    object.__setattr__(validator, "plan", plan)
    validator._validate_plan(
        segments=segments,
        owner_by_segment={item.segment_id: item.object_id for item in object_result.segment_ownership},
        planning_config=config,
        matrix=matrix,
    )
